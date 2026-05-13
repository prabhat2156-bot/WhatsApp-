/**
 * whatsapp-manager.js — Complete Rewrite
 *
 * KEY FIX: Uses s.onWhatsApp(phone) to resolve real phone JID before
 * any group operation. This completely bypasses the LID (Linked ID)
 * problem — we never need to match against @lid entries.
 *
 * FIX SUMMARY:
 *  makeAdminByNumbers  — onWhatsApp() → find in members or approve pending by phone JID
 *  demoteAdminInGroup  — onWhatsApp() → find real JID in participants
 *  getGroupMembers     — LID-aware extraction for display
 *  getGroupPendingRequests — LID-aware, {list,error} return
 *  getPendingForGroup  — LID-aware for CTC checker
 */

const {
  default: makeWASocket,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const { useMongoAuthState, clearMongoAuth } = require("./mongoAuthState");
const { AccountInfo } = require("./models");

const logger = pino({ level: "silent" });
const accounts = [];
function ensureAccount(index) {
  if (!accounts[index]) {
    accounts[index] = { index, socket: null, status: "disconnected", phoneNumber: "" };
  }
  return accounts[index];
}

let onPairingCode  = async () => {};
let onReady        = async () => {};
let onDisconnected = async () => {};

function setCallbacks(opts) {
  if (opts.onPairingCode)  onPairingCode  = opts.onPairingCode;
  if (opts.onReady)        onReady        = opts.onReady;
  if (opts.onDisconnected) onDisconnected = opts.onDisconnected;
}

function getStatus(index = 0)  { return accounts[index]?.status ?? "disconnected"; }
function getPhone(index = 0)   { return accounts[index]?.phoneNumber ?? ""; }
function getConnectedCount()   { return accounts.filter(Boolean).filter((a) => a.status === "connected").length; }
function getSocket(index = 0)  {
  const acc = accounts[index];
  if (!acc?.socket || acc.status !== "connected") return null;
  return acc.socket;
}

// ─── JID helpers ─────────────────────────────────────────────────────────
function normJid(jid) {
  return (jid || "").replace(/:\d+@/, "@").toLowerCase().trim();
}

/**
 * Extract real phone number from a participant object.
 * WhatsApp new LID system: participant.id may be @lid instead of @s.whatsapp.net.
 * We check all fields to find a @s.whatsapp.net one.
 */
function extractPhoneNumber(p) {
  const allJids = [p.id, p.lid, p.jid, p.userJid, p.participant]
    .filter((j) => j && typeof j === "string");
  const phoneJid = allJids.find((j) => j.endsWith("@s.whatsapp.net"));
  const displayJid = phoneJid || allJids[0] || "";
  return displayJid.split("@")[0].split(":")[0];
}

/**
 * Flexible suffix match — handles missing/extra country code.
 * e.g. stored "919876543210" matches input "9876543210" and vice versa.
 */
function numberMatches(stored, input) {
  if (!stored || !input) return false;
  const s = stored.replace(/\D/g, "");
  const i = input.replace(/\D/g, "");
  if (s === i) return true;
  if (i.length >= 8 && s.endsWith(i)) return true;
  if (s.length >= 8 && i.endsWith(s)) return true;
  return false;
}

/**
 * Resolve a phone number to its WhatsApp JID using the onWhatsApp API.
 * Returns the JID string (@s.whatsapp.net) or null if not on WhatsApp.
 * This is the KEY function that makes make-admin/demote-admin reliable
 * regardless of LID vs phone JID in group participant lists.
 */
async function resolvePhoneJid(socket, digits) {
  try {
    const results = await socket.onWhatsApp(digits);
    if (Array.isArray(results) && results.length > 0) {
      const found = results.find((r) => r.exists && r.jid);
      return found ? found.jid : null;
    }
  } catch {}
  return null;
}

// ─── Auto Accept ──────────────────────────────────────────────────────────
const autoAcceptGroups = new Map();
let autoAcceptTimer = null;

/**
 * TWO-LAYER filter for "requested to join" vs "requested to add":
 *
 * Layer 1 — method field check (all known Baileys field names):
 *   "invite_link"   → self-join ✅
 *   "non_admin_add" → someone added them ❌
 *   null/undefined  → treated as invite_link ✅
 *
 * Layer 2 — adder field check:
 *   If ANY adder-related field exists in the entry → non_admin_add ❌
 *   Baileys sometimes doesn't set method but does set an adder JID field.
 */
function isSelfJoinRequest(entry) {
  if (typeof entry === "string") return true; // raw JID string = invite_link (old format)

  // Check method in ALL known Baileys field names across versions
  const method =
    entry.method ??
    entry.requestMethod ??
    entry.request_method ??
    null;

  // Layer 1: explicit method-based rejection
  if (method === "non_admin_add") return false;

  // Layer 2: adder field check — if ANY of these exist, someone else added them
  if (
    entry.addedBy       ||
    entry.adder         ||
    entry.requestedBy   ||
    entry.addRequest    ||
    entry.add_request   ||
    entry.addedByJid    ||
    entry.addRequestJid
  ) return false;

  // Accept: invite_link or null/undefined (self-join)
  return method === "invite_link" || method === null || method === undefined;
}

function startAutoAcceptForGroups(groupIds, index = 0) {
  groupIds.forEach((gid) => autoAcceptGroups.set(gid, { accepted: 0 }));
  if (autoAcceptTimer) return;
  autoAcceptTimer = setInterval(async () => {
    const active = [...autoAcceptGroups.keys()];
    if (!active.length) { clearInterval(autoAcceptTimer); autoAcceptTimer = null; return; }
    const s = getSocket(index); if (!s) return;
    for (const gid of active) {
      if (!autoAcceptGroups.has(gid)) continue;
      try {
        // ── Get pending request list ──────────────────────────────────────
        const pending = await s.groupRequestParticipantsList(gid);

        // ── Layer 3: Cross-reference with group metadata ──────────────────
        // groupMetadata.pendingParticipants = ONLY "requested to add" entries.
        // If a JID appears there, it's non_admin_add regardless of method field.
        const nonAdminAddSet = new Set();
        try {
          const meta = await s.groupMetadata(gid);
          for (const p of (meta.pendingParticipants || [])) {
            const j = normJid(
              typeof p === "string" ? p : (p.jid || p.id || p.participant || "")
            );
            if (j) nonAdminAddSet.add(j);
          }
        } catch {}

        // ── Accept only genuine self-join requests ────────────────────────
        const toAccept = (pending || []).filter((p) => {
          // Cross-reference check: if in nonAdminAddSet → "requested to add" → skip
          const jid = normJid(
            typeof p === "string" ? p : (p.jid || p.id || "")
          );
          if (jid && nonAdminAddSet.has(jid)) return false;
          // Two-layer method + adder field check
          return isSelfJoinRequest(p);
        });

        const jids = toAccept
          .map((p) => (typeof p === "string" ? p : (p.jid || p.id)))
          .filter(Boolean);

        if (jids.length) {
          await s.groupRequestParticipantsUpdate(gid, jids, "approve").catch(() => {});
          const rec = autoAcceptGroups.get(gid);
          if (rec) rec.accepted += jids.length;
        }
      } catch {}
      await new Promise((r) => setTimeout(r, 600));
    }
  }, 8000);
}

function stopAutoAcceptForGroups(groupIds) {
  groupIds.forEach((gid) => autoAcceptGroups.delete(gid));
  if (autoAcceptGroups.size === 0 && autoAcceptTimer) {
    clearInterval(autoAcceptTimer); autoAcceptTimer = null;
  }
}

function getAutoAcceptStats(groupIds) {
  const result = {};
  groupIds.forEach((gid) => { result[gid] = autoAcceptGroups.get(gid) || { accepted: 0 }; });
  return result;
}

// ─── Connection ───────────────────────────────────────────────────────────
async function connectAccount(index, phoneNumber, freshStart = true) {
  const acc = ensureAccount(index);
  if (acc.socket) { try { acc.socket.end(undefined); } catch {} acc.socket = null; }

  const accountId = `account${index + 1}`;
  if (freshStart) {
    await clearMongoAuth(accountId);
    await AccountInfo.findOneAndUpdate(
      { accountIndex: index },
      { accountIndex: index, phoneNumber, hasAuth: false },
      { upsert: true }
    );
  }

  acc.status = "connecting"; acc.phoneNumber = phoneNumber;
  const { state, saveCreds } = await useMongoAuthState(accountId);
  const { version }          = await fetchLatestBaileysVersion();

  const socket = makeWASocket({
    version, logger, auth: state, printQRInTerminal: false,
    browser: ["Ubuntu", "Chrome", "120.0.0.0"],
    syncFullHistory:              false,
    generateHighQualityLinkPreview: false,
    markOnlineOnConnect:          false,
    connectTimeoutMs:             30000,
    defaultQueryTimeoutMs:        20000,
    keepAliveIntervalMs:          25000,
    retryRequestDelayMs:          2000,
    maxMsgRetryCount:             2,
    fireInitQueries:              false,
    emitOwnEvents:                false,
    shouldSyncHistoryMessage:     () => false,
    getMessage:                   async () => undefined,
    cachedGroupMetadata:          async () => undefined,
  });

  acc.socket = socket;
  socket.ev.on("creds.update", saveCreds);
  const clean = phoneNumber.replace(/[^0-9]/g, "");
  let pairingRequested = false;

  socket.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr && !pairingRequested) {
      pairingRequested = true;
      await _requestPairingWithRetry(socket, index, clean);
    }
    if (connection === "open") {
      acc.status = "connected";
      await AccountInfo.findOneAndUpdate(
        { accountIndex: index },
        { accountIndex: index, phoneNumber: clean, hasAuth: true },
        { upsert: true }
      );
      await onReady(index);
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      acc.status = "disconnected";
      if (code === DisconnectReason.loggedOut) {
        await clearMongoAuth(accountId);
        await AccountInfo.findOneAndUpdate(
          { accountIndex: index },
          { accountIndex: index, phoneNumber: "", hasAuth: false },
          { upsert: true }
        );
        acc.phoneNumber = ""; await onDisconnected(index);
      } else if (acc.phoneNumber) {
        setTimeout(() => connectAccount(index, acc.phoneNumber, false).catch(console.error), 5000);
      }
    }
  });
}

async function _requestPairingWithRetry(socket, index, clean, attempt = 1) {
  try {
    const code = await socket.requestPairingCode(clean);
    if (code) {
      const formatted = code.replace(/[^A-Z0-9]/gi, "").match(/.{1,4}/g)?.join("-") ?? code;
      await onPairingCode(index, formatted);
    }
  } catch {
    if (attempt < 3) {
      await new Promise((r) => setTimeout(r, 4000));
      await _requestPairingWithRetry(socket, index, clean, attempt + 1);
    } else {
      await onPairingCode(index, null);
    }
  }
}

async function disconnectAccount(index = 0) {
  const acc = accounts[index]; if (!acc) return;
  // full logout - clears MongoDB auth
  if (acc.socket) { try { acc.socket.end(undefined); } catch {} acc.socket = null; }
  const accountId = `account${index + 1}`;
  await clearMongoAuth(accountId);
  await AccountInfo.findOneAndUpdate(
    { accountIndex: index },
    { accountIndex: index, phoneNumber: "", hasAuth: false },
    { upsert: true }
  );
  acc.status = "disconnected"; acc.phoneNumber = "";
}

async function reconnectSavedAccounts() {
  const saved = await AccountInfo.find({ hasAuth: true });
  if (!saved.length) return;
  for (const acc of saved) {
    await connectAccount(acc.accountIndex, acc.phoneNumber, false)
      .catch((e) => console.error("[Startup]", e.message));
  }
}

// ─── Group utilities ──────────────────────────────────────────────────────
async function getAllGroupsWithDetails(index) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const groups = await s.groupFetchAllParticipating();
  return Object.entries(groups).map(([id, g]) => ({
    id, name: g.subject || id,
    participantCount: g.participants?.length ?? 0,
    participants: g.participants ?? [],
    announce: g.announce ?? false,
    restrict: g.restrict ?? false,
    joinApprovalMode: g.joinApprovalMode,
    memberAddMode: g.memberAddMode,
  }));
}

async function createGroup(index, name, jids) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  return await s.groupCreate(name, jids);
}
async function updateGroupDescription(index, gid, desc) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupUpdateDescription(gid, desc);
}
async function updateGroupPhoto(index, gid, buf) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.updateProfilePicture(gid, buf);
}
async function setDisappearingMessages(index, gid, sec) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupToggleEphemeral(gid, sec);
}
async function promoteToAdmin(index, gid, jids) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupParticipantsUpdate(gid, jids, "promote");
}
async function getGroupInviteLink(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const c = await s.groupInviteCode(gid);
  return `https://chat.whatsapp.com/${c}`;
}
async function joinGroupViaLink(index, code) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  return await s.groupAcceptInvite(code);
}
async function leaveGroup(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupLeave(gid);
}
async function getGroupInfoFromLink(index, code) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  try {
    const info = await s.groupGetInviteInfo(code);
    return { id: info.id, name: info.subject, participants: info.participants || [] };
  } catch { return null; }
}
async function renameGroup(index, gid, newName) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupUpdateSubject(gid, newName);
}
async function setGroupPermissions(index, gid, p) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupSettingUpdate(gid, p.sendMessages ? "not_announcement" : "announcement").catch(() => {});
  await s.groupSettingUpdate(gid, p.editInfo ? "unlocked" : "locked").catch(() => {});
  await s.groupMemberAddMode(gid, p.addMembers ? "all_member_add" : "admin_add").catch(() => {});
  await s.groupJoinApprovalMode(gid, p.approveMembers ? "on" : "off").catch(() => {});
}

// ─── Remove members ───────────────────────────────────────────────────────
// batchSize=1 + skipAdmins=true  → remove only regular members 1 by 1 (Remove Members feature)
// batchSize=5 + skipAdmins=false → remove everyone in bulk (legacy/other uses)
async function removeAllMembers(index, gid, batchSize = 5, skipAdmins = false) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const meta  = await s.groupMetadata(gid);
  const myJid = normJid(s.user?.id || "");
  // skipAdmins=true → only regular members removed (admins + bot stay safe)
  // skipAdmins=false → everyone removed except bot itself
  const toRm  = meta.participants
    .filter((p) => {
      if (normJid(p.id) === myJid) return false; // never remove bot itself
      if (skipAdmins && (p.admin === "admin" || p.admin === "superadmin")) return false;
      return true;
    })
    .map((p) => p.id);
  if (!toRm.length) return 0;
  for (let i = 0; i < toRm.length; i += batchSize) {
    await s.groupParticipantsUpdate(gid, toRm.slice(i, i + batchSize), "remove").catch(() => {});
    await new Promise((r) => setTimeout(r, batchSize === 1 ? 1500 : 1000));
  }
  return toRm.length;
}

// ─── Get Group Members (LID-aware) ────────────────────────────────────────
async function getGroupMembers(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const meta = await s.groupMetadata(gid);
  return (meta.participants || []).map((p) => {
    const number = extractPhoneNumber(p);
    return {
      id: p.id,
      jid: p.id,
      number,
      phone: number,
      admin: p.admin === "admin" || p.admin === "superadmin",
      superadmin: p.admin === "superadmin",
    };
  });
}

// ─── Get Pending Requests (LID-aware, returns {list, error}) ─────────────
async function getGroupPendingRequests(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");

  const parseEntry = (p, defaultMethod) => {
    // Raw JID for API use (approve/reject calls)
    const rawJid = typeof p === "string"
      ? p
      : (p.jid || p.id || p.participant || String(p));

    // LID-aware: look for @s.whatsapp.net among all fields
    const allJids = typeof p === "object" && p !== null
      ? [p.jid, p.id, p.participant, p.userJid].filter((j) => j && typeof j === "string")
      : [rawJid];
    const phoneJid = allJids.find((j) => j.endsWith("@s.whatsapp.net"));
    const displayJid = phoneJid || allJids[0] || rawJid;
    const number = displayJid.split("@")[0].split(":")[0];
    const isLid  = !phoneJid && rawJid.endsWith("@lid");

    const method = typeof p === "object" && p !== null
      ? (p.method || p.requestMethod || defaultMethod)
      : defaultMethod;

    return { id: rawJid, jid: rawJid, number, phone: number, method, isLid };
  };

  let results  = [];
  let errorMsg = null;

  try {
    const list = await s.groupRequestParticipantsList(gid);
    if (Array.isArray(list)) {
      results.push(...list.map((p) => parseEntry(p, "invite_link")));
    }
  } catch (err) {
    errorMsg = err.message;
  }

  // Also check pendingParticipants in metadata
  try {
    const meta = await s.groupMetadata(gid);
    const extra = meta.pendingParticipants || [];
    for (const p of extra) {
      const entry = parseEntry(p, "non_admin_add");
      if (!results.find((r) => r.id === entry.id)) results.push(entry);
    }
  } catch {}

  return { list: results, error: results.length === 0 ? errorMsg : null };
}

// ─── MAKE ADMIN (FIXED — onWhatsApp for reliable JID resolution) ──────────
/**
 * HOW IT WORKS:
 * 1. For each phone number, call s.onWhatsApp() to get the real phone JID.
 *    This bypasses LID entirely — we now know the exact @s.whatsapp.net JID.
 * 2. If the real JID is already in group participants → promote directly.
 * 3. If NOT in participants:
 *    a. Try approving using the phone JID directly (WhatsApp maps phone→LID internally).
 *    b. Wait for them to join.
 *    c. Re-fetch metadata to find their actual participant JID and promote.
 * 4. If onWhatsApp() fails (not on WA), fall back to numberMatches on participants.
 */
async function makeAdminByNumbers(index, gid, phones) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  let promoted = 0;

  for (const phone of phones) {
    const digits = phone.replace(/\D/g, "");
    if (!digits || digits.length < 7) continue;

    try {
      // ── Step 1: Resolve real phone JID via onWhatsApp() ──
      let realPhoneJid = await resolvePhoneJid(s, digits);

      // ── Step 2: Fetch fresh metadata ──
      const meta = await s.groupMetadata(gid);

      // ── Step 3: Search in current participants ──
      // Use both real JID (if resolved) and numberMatches as fallback
      const memberInGroup = meta.participants.find((p) => {
        if (realPhoneJid && normJid(p.id) === normJid(realPhoneJid)) return true;
        const num = extractPhoneNumber(p);
        return numberMatches(num, digits);
      });

      if (memberInGroup) {
        // Already a member — promote using their actual participant JID
        await s.groupParticipantsUpdate(gid, [memberInGroup.id], "promote");
        promoted++;
        continue;
      }

      // ── Step 4: Not in group — check pending ──
      // Try to approve using the real phone JID (WhatsApp handles LID mapping internally)
      const approveJid = realPhoneJid || `${digits}@s.whatsapp.net`;

      // Also check pending list for a matching entry (for direct LID approval)
      let pendingRawJid = null;
      try {
        const pendingList = await s.groupRequestParticipantsList(gid);
        for (const p of (pendingList || [])) {
          const allJids = [p.jid, p.id, p.participant, p.userJid]
            .filter((j) => j && typeof j === "string");

          // Check by real phone JID match
          if (realPhoneJid && allJids.some((j) => normJid(j) === normJid(realPhoneJid))) {
            pendingRawJid = p.jid || p.id;
            break;
          }
          // Check by phone number match (for non-LID entries)
          const phoneJid = allJids.find((j) => j.endsWith("@s.whatsapp.net"));
          if (phoneJid) {
            const num = phoneJid.split("@")[0].split(":")[0];
            if (numberMatches(num, digits)) {
              pendingRawJid = p.jid || p.id;
              break;
            }
          }
        }
      } catch {}

      // Approve using:
      //   1. The matched raw JID from pending (could be LID)
      //   2. Or the real phone JID directly (WhatsApp should handle the mapping)
      const jidToApprove = pendingRawJid || approveJid;

      let approveOk = false;
      try {
        await s.groupRequestParticipantsUpdate(gid, [jidToApprove], "approve");
        approveOk = true;
      } catch {
        // If approving LID failed, try with phone JID
        if (pendingRawJid && pendingRawJid !== approveJid) {
          try {
            await s.groupRequestParticipantsUpdate(gid, [approveJid], "approve");
            approveOk = true;
          } catch {}
        }
      }

      if (!approveOk) continue;

      // ── Step 5: Wait for them to join, then promote ──
      await new Promise((r) => setTimeout(r, 5000));

      const metaAfter = await s.groupMetadata(gid);
      const newMember = metaAfter.participants.find((p) => {
        if (realPhoneJid && normJid(p.id) === normJid(realPhoneJid)) return true;
        const num = extractPhoneNumber(p);
        return numberMatches(num, digits);
      });

      if (newMember) {
        await s.groupParticipantsUpdate(gid, [newMember.id], "promote");
        promoted++;
      }
    } catch (err) {
      console.error("[makeAdmin] error for", digits, ":", err.message);
    }

    await new Promise((r) => setTimeout(r, 800));
  }

  return promoted;
}

// ─── DEMOTE ADMIN (FIXED — onWhatsApp for reliable JID resolution) ────────
async function demoteAdminInGroup(index, gid, phones) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  let demoted = 0;

  for (const phone of phones) {
    const digits = phone.replace(/\D/g, "");
    if (!digits || digits.length < 7) continue;

    try {
      // ── Step 1: Resolve real phone JID ──
      const realPhoneJid = await resolvePhoneJid(s, digits);

      // ── Step 2: Fetch metadata ──
      const meta = await s.groupMetadata(gid);

      // ── Step 3: Find member using real JID + numberMatches fallback ──
      const member = meta.participants.find((p) => {
        if (realPhoneJid && normJid(p.id) === normJid(realPhoneJid)) return true;
        const num = extractPhoneNumber(p);
        return numberMatches(num, digits);
      });

      if (!member) continue;

      const isAdmin = member.admin === "admin" || member.admin === "superadmin" || member.admin === true;
      if (!isAdmin) continue;

      await s.groupParticipantsUpdate(gid, [member.id], "demote");
      demoted++;
    } catch (err) {
      console.error("[demoteAdmin] error for", digits, ":", err.message);
    }

    await new Promise((r) => setTimeout(r, 600));
  }

  return demoted;
}

// ─── Approval toggle ──────────────────────────────────────────────────────
async function getGroupApprovalStatus(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const meta = await s.groupMetadata(gid);
  return meta.joinApprovalMode === "on" || meta.joinApprovalMode === true;
}
async function setGroupApproval(index, gid, enable) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupJoinApprovalMode(gid, enable ? "on" : "off");
}

// ─── Approve all pending ──────────────────────────────────────────────────
async function approveAllPending(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const metaBefore  = await s.groupMetadata(gid);
  const beforeCount = metaBefore.participants?.length ?? 0;

  let pendingJids = [];
  try {
    const pending = await s.groupRequestParticipantsList(gid);
    pendingJids = (pending ?? []).map((p) => p.jid || p.id).filter(Boolean);
  } catch {
    return { pendingCount: 0, approved: 0, failed: 0, beforeCount, afterCount: beforeCount };
  }

  if (!pendingJids.length) {
    return { pendingCount: 0, approved: 0, failed: 0, beforeCount, afterCount: beforeCount };
  }

  let approved = 0, failed = 0;
  for (let i = 0; i < pendingJids.length; i += 20) {
    const batch = pendingJids.slice(i, i + 20);
    try {
      await s.groupRequestParticipantsUpdate(gid, batch, "approve");
      approved += batch.length;
    } catch {
      for (const jid of batch) {
        try {
          await s.groupRequestParticipantsUpdate(gid, [jid], "approve");
          approved++;
        } catch { failed++; }
        await new Promise((r) => setTimeout(r, 400));
      }
    }
    await new Promise((r) => setTimeout(r, 1500));
  }

  await new Promise((r) => setTimeout(r, 3000));
  let afterCount = beforeCount;
  try {
    const metaAfter = await s.groupMetadata(gid);
    afterCount = metaAfter.participants?.length ?? beforeCount;
  } catch {}

  return { pendingCount: pendingJids.length, approved, failed, actuallyJoined: afterCount - beforeCount, beforeCount, afterCount };
}

// ─── Reset invite link ────────────────────────────────────────────────────
async function resetGroupInviteLink(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  await s.groupRevokeInvite(gid);
  await new Promise((r) => setTimeout(r, 1200));
  const code = await s.groupInviteCode(gid);
  return `https://chat.whatsapp.com/${code}`;
}

// ─── Group settings ───────────────────────────────────────────────────────
async function getGroupSettings(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const meta = await s.groupMetadata(gid);
  return {
    announce:      meta.announce === true,
    restrict:      meta.restrict === true,
    joinApproval:  meta.joinApprovalMode === "on" || meta.joinApprovalMode === true,
    memberAddMode: meta.memberAddMode === "all_member_add",
  };
}

async function applyGroupSettings(index, gid, desired) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const cur = await getGroupSettings(index, gid);
  const changes = [], skipped = [];

  const applyOne = async (key, settingFn, label, trueLabel, falseLabel) => {
    if (desired[key] === null || desired[key] === undefined) return;
    if (desired[key] !== cur[key]) {
      await settingFn(desired[key]).catch(() => {});
      changes.push(`${label}: ${desired[key] ? trueLabel : falseLabel}`);
      await new Promise((r) => setTimeout(r, 600));
    } else {
      skipped.push(`${label} already: ${cur[key] ? trueLabel : falseLabel}`);
    }
  };

  await applyOne("announce",      (v) => s.groupSettingUpdate(gid, v ? "announcement" : "not_announcement"), "💬 Messages",    "Admins Only", "All Members");
  await applyOne("restrict",      (v) => s.groupSettingUpdate(gid, v ? "locked" : "unlocked"),               "✏️ Edit Info",   "Admins Only", "All Members");
  await applyOne("joinApproval",  (v) => s.groupJoinApprovalMode(gid, v ? "on" : "off"),                     "🔐 Join Approval","On",         "Off");
  await applyOne("memberAddMode", (v) => s.groupMemberAddMode(gid, v ? "all_member_add" : "admin_add"),      "➕ Add Members",  "All Members", "Admins Only");

  return { changes, skipped };
}

// ─── Add members to group ─────────────────────────────────────────────────
async function addMembersToGroup(index, gid, phones, oneByOne = false) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const jids = [...new Set(phones.map((n) => `${n.replace(/\D/g, "")}@s.whatsapp.net`))];
  const results = { added: 0, failed: 0, skipped: 0, failedNums: [] };

  if (oneByOne) {
    for (const jid of jids) {
      try {
        const res = await s.groupParticipantsUpdate(gid, [jid], "add");
        const status = String(res?.[0]?.status ?? "200");
        if (status === "200") results.added++;
        else if (["403","409"].includes(status)) results.skipped++;
        else { results.failed++; results.failedNums.push(normJid(jid).replace("@s.whatsapp.net","")); }
      } catch {
        results.failed++; results.failedNums.push(normJid(jid).replace("@s.whatsapp.net",""));
      }
      await new Promise((r) => setTimeout(r, 2500));
    }
  } else {
    for (let i = 0; i < jids.length; i += 5) {
      const batch = jids.slice(i, i + 5);
      try {
        const res = await s.groupParticipantsUpdate(gid, batch, "add");
        if (Array.isArray(res)) {
          res.forEach((r, idx) => {
            const st = String(r.status ?? "200");
            if (st === "200") results.added++;
            else if (["403","409"].includes(st)) results.skipped++;
            else { results.failed++; results.failedNums.push(batch[idx]?.replace("@s.whatsapp.net","") || ""); }
          });
        } else { results.added += batch.length; }
      } catch {
        results.failed += batch.length;
        batch.forEach((j) => results.failedNums.push(normJid(j).replace("@s.whatsapp.net","")));
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
  return results;
}

// ─── Get pending JID set (for CTC / Change-Name matching) ─────────────────
/**
 * Returns ALL JIDs of pending join-request participants as a Set (normalized).
 * Collects from BOTH groupRequestParticipantsList AND groupMetadata.pendingParticipants
 * so we catch every format Baileys may return (string, object with @lid or @s.whatsapp.net).
 * Also returns `count` = number of actual pending people.
 */
async function getPendingRawJids(index, gid) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const jidSet   = new Set();  // ALL JIDs (both @lid and @s.whatsapp.net)
  const phoneSet = new Set();  // phone numbers extracted from @s.whatsapp.net JIDs
  let pendingCount = 0;

  const addEntry = (p) => {
    const candidates = typeof p === "string"
      ? [p]
      : [p.jid, p.id, p.participant, p.userJid, p.lid]
          .filter((j) => j && typeof j === "string");

    for (const j of candidates) {
      const norm = normJid(j);
      if (norm) jidSet.add(norm);
      // If it's a real phone JID, also extract the phone number for fallback matching
      if (norm.endsWith("@s.whatsapp.net")) {
        const ph = norm.split("@")[0].split(":")[0];
        if (ph && ph.length >= 7) phoneSet.add(ph);
      }
    }
  };

  try {
    const list = await s.groupRequestParticipantsList(gid);
    pendingCount = (list || []).length;
    (list || []).forEach(addEntry);
  } catch {}

  try {
    const meta = await s.groupMetadata(gid);
    for (const p of (meta.pendingParticipants || [])) {
      const primary = normJid(typeof p === "string" ? p : (p.jid || p.id || ""));
      if (!jidSet.has(primary)) pendingCount++;
      addEntry(p);
    }
  } catch {}

  return { jids: jidSet, phones: phoneSet, count: pendingCount };
}

// ─── Resolve VCF phone numbers → real JIDs via onWhatsApp ─────────────────
/**
 * For each phone number, calls onWhatsApp() to get the real @s.whatsapp.net JID
 * AND the @lid JID (if returned by Baileys 6.7+).
 * Returns array of { phone, phoneJid, lid } for matched contacts.
 * This is the KEY to matching VCF contacts against LID-based pending lists.
 */
async function resolveVcfPhones(index, phones) {
  const s = getSocket(index); if (!s) throw new Error("Not connected!");
  const digits = [...new Set(
    phones.map((p) => String(p).replace(/\D/g, "")).filter((p) => p.length >= 7)
  )];
  if (!digits.length) return [];

  const out = [];

  // Try batch resolve first (Baileys accepts multiple args)
  try {
    const res = await s.onWhatsApp(...digits);
    for (const r of (res || [])) {
      if (r.exists && r.jid) {
        out.push({
          phone: r.jid.split("@")[0].split(":")[0],
          phoneJid: normJid(r.jid),
          lid: normJid(r.lid || ""),
        });
      }
    }
    if (out.length > 0) return out;
  } catch {}

  // Fallback: one-by-one
  for (const d of digits) {
    try {
      const res = await s.onWhatsApp(d);
      const found = (res || []).find((r) => r.exists && r.jid);
      if (found) {
        out.push({
          phone: d,
          phoneJid: normJid(found.jid),
          lid: normJid(found.lid || ""),
        });
      }
    } catch {}
  }
  return out;
}


// ─── Suspend (auto-timeout): closes socket but keeps MongoDB auth ─────────────
// User can reconnect without re-pairing.
async function suspendAccount(index) {
  const acc = accounts[index];
  if (!acc) return;
  if (acc.socket) {
    try { acc.socket.ev.removeAllListeners(); } catch {}
    try { acc.socket.end(undefined); } catch {}
    acc.socket = null;
  }
  acc.status = "disconnected";
  // Hint V8 to collect freed memory
  if (global.gc) { try { global.gc(); } catch {} }
}

// Reconnect from saved MongoDB auth (no fresh start, no re-pairing)
async function resumeAccount(index, phoneNumber) {
  await connectAccount(index, phoneNumber, false);
}

// ─── Exports ──────────────────────────────────────────────────────────────
module.exports = {
  setCallbacks,
  getStatus, getPhone, getConnectedCount,
  connectAccount, disconnectAccount, suspendAccount, resumeAccount, reconnectSavedAccounts,
  createGroup, updateGroupDescription, updateGroupPhoto,
  setDisappearingMessages, promoteToAdmin, setGroupPermissions,
  getGroupInviteLink, joinGroupViaLink,
  getAllGroupsWithDetails,
  leaveGroup, removeAllMembers,
  makeAdminByNumbers,
  numberMatches,
  getGroupApprovalStatus, setGroupApproval,
  approveAllPending,
  getGroupMembers, getGroupPendingRequests,
  resetGroupInviteLink,
  demoteAdminInGroup,
  getGroupSettings, applyGroupSettings,
  renameGroup,
  addMembersToGroup,
  getGroupInfoFromLink,
  getPendingRawJids,
  resolveVcfPhones,
  startAutoAcceptForGroups, stopAutoAcceptForGroups, getAutoAcceptStats,
};
