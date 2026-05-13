/**
 * WhatsApp Group Manager Bot — Final Version
 *
 * KEY CHANGES IN THIS VERSION:
 *  - withRetry: 3 retries + exponential backoff (100% success target)
 *  - All delays tuned to WhatsApp rate limits
 *  - Change Name VCF: auto-scans ALL groups (no links needed) — members + pending
 *  - CTC Checker: pending vs VCF check with LID-aware filtering
 */

const { Telegraf, Markup } = require("telegraf");
const { connectDB }        = require("./src/db");
const { getSession, updateSession, resetSession, defaultGroupFlow, defaultFeatureFlow } = require("./src/session");
const {
  setCallbacks, getStatus, getPhone, getConnectedCount,
  connectAccount, disconnectAccount, reconnectSavedAccounts,
  createGroup, updateGroupDescription, updateGroupPhoto,
  setDisappearingMessages, promoteToAdmin, setGroupPermissions,
  getGroupInviteLink, joinGroupViaLink,
  getAllGroupsWithDetails,
  leaveGroup, removeAllMembers,
  makeAdminByNumbers,
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
} = require("./src/whatsapp-manager");
const express = require("express");
const http    = require("http");
const https   = require("https");

const TOKEN    = process.env.TELEGRAM_BOT_TOKEN;
if (!TOKEN) { console.error("TELEGRAM_BOT_TOKEN not set!"); process.exit(1); }
const OWNER_ID = parseInt(process.env.OWNER_ID || "0", 10);

const bot        = new Telegraf(TOKEN);
const sleep      = (ms) => new Promise((r) => setTimeout(r, ms));
const PAGE_SIZE  = 10;
const startTimes = new Map();
const aaLiveIntervals = new Map();

// ─── Per-feature delay constants (tuned for WA rate limits) ───────────────
const D = {
  getLinks:       1500,   // safe for metadata reads
  leave:          3000,
  removeMembers:  4000,
  makeAdmin:      3000,
  demoteAdmin:    2500,
  approvalToggle: 2000,
  approvePending: 4500,
  memberList:     1500,
  pendingList:    1500,
  resetLink:      2000,
  changeName:     2000,
  createGroup:    2500,
  joinGroup:      2500,
  addMembers:     2500,
  ctcCheck:       1200,
  vcfAutoMatch:   2000,   // per-group delay while scanning for VCF matches
  pendingCheck:   1000,   // extra per-group when checking pending API
};

// ─── Owner guard ───────────────────────────────────────────────────────────
bot.use(async (ctx, next) => {
  if (OWNER_ID && ctx.from?.id !== OWNER_ID) {
    if (ctx.callbackQuery) await ctx.answerCbQuery("⛔ Unauthorized.", { show_alert: true }).catch(() => {});
    else await ctx.reply("⛔ This bot is private.").catch(() => {});
    return;
  }
  return next();
});

// ─── Pairing callbacks ─────────────────────────────────────────────────────
const pendingPairingCbs = new Map();
const pendingReadyCbs   = new Map();

setCallbacks({
  onPairingCode:  async (i, code)  => { const cb = pendingPairingCbs.get(i); if (cb) { pendingPairingCbs.delete(i); await cb(code); } },
  onReady:        async (i)        => { const cb = pendingReadyCbs.get(i);   if (cb) { pendingReadyCbs.delete(i);   await cb();    } },
  onDisconnected: async ()         => {},
});

// ─── withTimeout: wraps any promise with a hard timeout ───────────────────
function withTimeout(promise, ms = 15000, label = "Operation") {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`${label} timed out after ${ms / 1000}s`)), ms);
    promise.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
  });
}

// ─── withRetry: exponential backoff ───────────────────────────────────────
/**
 * Retries fn up to `retries` times.
 * Delay schedule: baseDelay × 1, × 1.5, × 2.25 (exponential)
 * e.g. baseDelay=2000 → 2s, 3s, 4.5s on failures
 */
async function withRetry(fn, retries = 3, baseDelay = 2000) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try { return await fn(); }
    catch (err) {
      if (attempt < retries) {
        await sleep(Math.round(baseDelay * Math.pow(1.5, attempt)));
      } else {
        throw err;
      }
    }
  }
}

// ─── Progress message helpers (SINGLE message — progress + cancel) ─────────
async function startProgress(ctx, uid, text) {
  const m = await ctx.reply(text, {
    parse_mode: "Markdown",
    ...Markup.inlineKeyboard([[Markup.button.callback("🛑 Cancel", "cancel_exec")]]),
  });
  startTimes.set(uid, Date.now());
  updateSession(uid, { cancelMsgId: m.message_id, cancelPending: false });
  return m;
}

async function editProgress(chatId, msgId, text) {
  try {
    await bot.telegram.editMessageText(chatId, msgId, undefined, text, {
      parse_mode: "Markdown",
      reply_markup: Markup.inlineKeyboard([[Markup.button.callback("🛑 Cancel", "cancel_exec")]]).reply_markup,
    });
  } catch {}
}

// doneProgress: kept for compatibility but sendSummary now edits the same message
async function doneProgress(chatId, msgId, text) {
  try {
    await bot.telegram.editMessageText(chatId, msgId, undefined, text, { parse_mode: "Markdown" });
  } catch {}
}

async function showCancelBtn()   {}   // no-op — cancel is inline in progress msg
async function removeCancelBtn() {}   // no-op

bot.action("cancel_exec", async (ctx) => {
  await ctx.answerCbQuery("Cancelling...");
  updateSession(ctx.from.id, { cancelPending: true });
  try { await ctx.editMessageText("🛑 *Cancelling...*", { parse_mode: "Markdown" }); } catch {}
});

function isCancelled(uid) { return getSession(uid).cancelPending === true; }

// ─── Misc helpers ──────────────────────────────────────────────────────────
async function reply(ctx, text, extra = {})       { return await ctx.reply(text, extra); }
async function editOrReply(ctx, text, extra = {}) {
  try { return await ctx.editMessageText(text, extra); }
  catch { return await ctx.reply(text, extra); }
}

function bar(done, total) {
  const p = total > 0 ? Math.round((done / total) * 10) : 0;
  return `[${"█".repeat(p)}${"░".repeat(10 - p)}] ${total > 0 ? Math.round((done / total) * 100) : 0}%`;
}

function elapsed(uid) {
  const t = startTimes.get(uid);
  return t ? Math.round((Date.now() - t) / 1000) : 0;
}

// ─── LID-aware phone extractor (for participants in index.js) ─────────────
function extractParticipantPhone(p) {
  const allJids = [p.jid, p.id, p.lid, p.userJid]
    .filter((j) => j && typeof j === "string");
  const phoneJid = allJids.find((j) => j.endsWith("@s.whatsapp.net"));
  const displayJid = phoneJid || allJids[0] || "";
  return displayJid.split("@")[0].split(":")[0];
}

// ─── VCF Parser ───────────────────────────────────────────────────────────
function parseVcf(content) {
  const contacts = [];
  const blocks = content.split(/(?=BEGIN:VCARD)/gi);
  for (const block of blocks) {
    if (!block.toUpperCase().includes("BEGIN:VCARD")) continue;
    const nameMatch = block.match(/^FN:(.+)$/m) || block.match(/^N:([^;\r\n]+)/m);
    const name = nameMatch ? nameMatch[1].trim().replace(/\\/g, "") : "";
    const telMatches = [...block.matchAll(/^TEL[^:]*:([^\r\n]+)/gim)];
    for (const m of telMatches) {
      const digits = m[1].trim().replace(/[\s()\-+]/g, "").replace(/[^0-9]/g, "");
      if (digits.length >= 10) contacts.push({ name, phone: digits });
    }
  }
  return contacts;
}

function extractCodes(text) {
  const matches = [...text.matchAll(/chat\.whatsapp\.com\/([A-Za-z0-9_-]+)/g)];
  return [...new Set(matches.map((m) => m[1]))];
}

async function downloadFile(ctx, fileId) {
  const u = await ctx.telegram.getFileLink(fileId);
  const r = await fetch(u.href);
  return Buffer.from(await r.arrayBuffer());
}

// ─── Feature Labels ────────────────────────────────────────────────────────
const FEAT_LABEL = {
  get_links:       "🔗 Get Links",
  leave:           "🚪 Leave Groups",
  remove_members:  "🧹 Remove Members",
  make_admin:      "👑 Make Admin",
  approval:        "🔀 Approval Toggle",
  approve_pending: "✅ Approve Pending",
  member_list:     "📋 Member List",
  pending_list:    "⏳ Pending List",
  join_groups:     "🔗 Join Groups",
  create_groups:   "➕ Create Groups",
  add_members:     "➕ Add Members",
  edit_settings:   "⚙️ Edit Settings",
  change_name:     "✏️ Change Name",
  reset_link:      "🔄 Reset Link",
  demote_admin:    "⬇️ Demote Admin",
  auto_accept:     "⏰ Auto Accept",
  ctc_checker:     "🔍 CTC Checker",
};

// ─── Summary ───────────────────────────────────────────────────────────────
// Edits the existing progress message instead of sending a new one.
// boxLines = array of strings shown inside a white code block box.
async function sendSummary(ctx, opts) {
  const { feature, total, success, failed, cancelled, extra = [], boxLines = [] } = opts;
  const uid  = ctx.from?.id;
  const secs = uid ? elapsed(uid) : 0;
  if (uid) startTimes.delete(uid);

  const statusLine = cancelled
    ? "🚫 *Cancelled*"
    : failed === 0
      ? "✅ *All done!*"
      : `⚠️ *Done with ${failed} failure(s)*`;

  let text =
    `📊 *${FEAT_LABEL[feature] || feature}*\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `${statusLine}\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `Total   : ${total}\n` +
    `Success : ${success}\n` +
    `Failed  : ${failed}\n` +
    `Time    : ${secs}s\n`;

  if (extra.length) text += `━━━━━━━━━━━━━━━━━━━━\n` + extra.join("\n") + "\n";
  text += `━━━━━━━━━━━━━━━━━━━━`;
  if (text.length > 4000) text = text.slice(0, 3990) + "\n_...more_";

  const replyMarkup = Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]);

  // Try editing the existing progress message so no new message is sent
  const session = uid ? getSession(uid) : null;
  const cancelMsgId = session?.cancelMsgId;
  if (cancelMsgId && ctx.chat?.id) {
    try {
      await bot.telegram.editMessageText(ctx.chat.id, cancelMsgId, undefined, text, {
        parse_mode: "Markdown",
        reply_markup: replyMarkup.reply_markup,
      });
      if (uid) updateSession(uid, { cancelMsgId: null });
    } catch {
      await ctx.reply(text, { parse_mode: "Markdown", ...replyMarkup });
    }
  } else {
    await ctx.reply(text, { parse_mode: "Markdown", ...replyMarkup });
  }

  // Send white box (code block) with group list if provided
  if (boxLines.length) {
    const CHUNK = 50;
    for (let c = 0; c < boxLines.length; c += CHUNK) {
      const chunk = boxLines.slice(c, c + CHUNK).join("\n");
      try {
        await ctx.reply("```\n" + chunk + "\n```", { parse_mode: "Markdown" });
      } catch {
        await ctx.reply(chunk);
      }
      if (c + CHUNK < boxLines.length) await sleep(400);
    }
  }
}

// ─── Main Menu ─────────────────────────────────────────────────────────────
function buildMainMenu() {
  const c = getStatus(0) === "connected", p = getPhone(0);
  const b = (label, cb) => Markup.button.callback(label, c ? cb : "need_connect");
  return Markup.inlineKeyboard([
    [Markup.button.callback(c ? `📱 WhatsApp ✅ +${p}` : `📱 WhatsApp ❌ Not Connected`, "menu_account")],
    [b("➕ Create Groups",   "create_groups_start"), b("🔗 Join Groups",     "join_groups_start")],
    [b("🔗 Get Links",       "feat_getlinks"),       b("🚪 Leave Groups",    "feat_leave")],
    [b("🧹 Remove Members",  "feat_removemem"),      b("👑 Make Admin",      "feat_makeadmin")],
    [b("⬇️ Demote Admin",    "feat_demoteadmin"),    b("🔀 Approval Toggle", "feat_approval")],
    [b("✅ Approve Pending", "feat_approvepending"), b("🔄 Reset Link",      "feat_resetlink")],
    [b("📋 Member List",     "feat_memberlist"),     b("➕ Add Members",     "feat_addmembers")],
    [b("⚙️ Edit Settings",   "feat_editsettings"),   b("✏️ Change Name",     "feat_changename")],
    [b("⏰ Auto Accept",     "feat_autoaccept"),     b("🔍 CTC Checker",     "feat_ctcchecker")],
    [Markup.button.callback("📊 Status", "menu_status")],
  ]);
}

async function sendMainMenu(ctx) {
  const c = getStatus(0) === "connected", p = getPhone(0);
  const uid = ctx.from?.id;
  updateSession(uid, { cancelPending: false, awaitingVcf: null });
  const userName = [ctx.from?.first_name, ctx.from?.last_name].filter(Boolean).join(" ") || "User";
  await ctx.reply(
    `🤖 *ᴡꜱ ᴀᴜᴛᴏᴍᴀᴛɪᴏɴ* 🤖\n` +
    `▰▰▰▰▰▰▰▰▰▰▰▰▰\n\n` +
    `👋 Hey *${userName}*, Welcome!\n\n` +
    `╭─── 📡 Status ─────────╮\n` +
    `│ ${c ? "✅" : "❌"}  WhatsApp: ${c ? `Connected (+${p})` : "Not Connected"}\n` +
    `╰───────────────────────╯\n\n` +
    `› Choose an option:`,
    { parse_mode: "Markdown", ...buildMainMenu() }
  );
}

bot.start(async (ctx) => { resetSession(ctx.from.id); await sendMainMenu(ctx); });
bot.command("menu", async (ctx) => {
  updateSession(ctx.from.id, { awaitingPhoneForIndex: null, groupFlow: null, joinFlow: null, featureFlow: null, cancelPending: false, awaitingVcf: null });
  await sendMainMenu(ctx);
});
bot.action("need_connect", async (ctx) => { await ctx.answerCbQuery("⚠️ Connect WhatsApp first!", { show_alert: true }); });
bot.action("back_menu", async (ctx) => {
  await ctx.answerCbQuery();
  const uid = ctx.from.id;
  if (aaLiveIntervals.has(uid)) { clearInterval(aaLiveIntervals.get(uid)); aaLiveIntervals.delete(uid); }
  updateSession(uid, { awaitingPhoneForIndex: null, groupFlow: null, joinFlow: null, featureFlow: null, cancelPending: false, awaitingVcf: null });
  await sendMainMenu(ctx);
});

// ─── Status ────────────────────────────────────────────────────────────────
bot.action("menu_status", async (ctx) => {
  await ctx.answerCbQuery();
  const s = getStatus(0), p = getPhone(0);
  const icon = s === "connected" ? "✅" : s === "connecting" ? "⏳" : "❌";
  await editOrReply(ctx,
    `📊 *Bot Status*\n━━━━━━━━━━━━━━━━━━━━\n${icon} WhatsApp: *${s}*${s === "connected" ? `\n📞 +${p}` : ""}\n━━━━━━━━━━━━━━━━━━━━`,
    { parse_mode: "Markdown", reply_markup: Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]).reply_markup }
  );
});

// ─── Account ───────────────────────────────────────────────────────────────
bot.action("menu_account", async (ctx) => {
  await ctx.answerCbQuery();
  const status = getStatus(0), phone = getPhone(0);
  if (status === "connected") {
    await editOrReply(ctx,
      `📱 *WhatsApp Account*\n━━━━━━━━━━━━━━━━━━━━\n✅ Connected\n📞 +${phone}\n━━━━━━━━━━━━━━━━━━━━\nLogout?`,
      { parse_mode: "Markdown", reply_markup: Markup.inlineKeyboard([[Markup.button.callback("🔌 Logout", "logout_wa")], [Markup.button.callback("🏠 Main Menu", "back_menu")]]).reply_markup }
    );
  } else if (status === "connecting") {
    await editOrReply(ctx,
      `📱 *WhatsApp Account*\n━━━━━━━━━━━━━━━━━━━━\n⏳ Connecting...\n━━━━━━━━━━━━━━━━━━━━`,
      { parse_mode: "Markdown", reply_markup: Markup.inlineKeyboard([[Markup.button.callback("🔄 Reset", "reset_wa")], [Markup.button.callback("🏠 Main Menu", "back_menu")]]).reply_markup }
    );
  } else {
    updateSession(ctx.from.id, { awaitingPhoneForIndex: 0 });
    await editOrReply(ctx,
      `📱 *Connect WhatsApp*\n━━━━━━━━━━━━━━━━━━━━\n\nSend your phone number with country code:\n\n*Example:* \`919876543210\`\n\n⚠️ Pairing code expires in 60 seconds!`,
      { parse_mode: "Markdown", reply_markup: Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]).reply_markup }
    );
  }
});
bot.action("logout_wa", async (ctx) => {
  await ctx.answerCbQuery("Logging out...");
  await editOrReply(ctx, `⏳ *Logging out...*`, { parse_mode: "Markdown" });
  await disconnectAccount(0); await sleep(800); await sendMainMenu(ctx);
});
bot.action("reset_wa", async (ctx) => {
  await ctx.answerCbQuery("Resetting...");
  await disconnectAccount(0);
  updateSession(ctx.from.id, { awaitingPhoneForIndex: 0 });
  await editOrReply(ctx,
    `📱 *Connect WhatsApp*\n━━━━━━━━━━━━━━━━━━━━\n\nSend your phone number:\n*Example:* \`919876543210\``,
    { parse_mode: "Markdown", reply_markup: Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]).reply_markup }
  );
});

// ══════════════════════════════════════════════════════════════════════════
// ─── GROUP SELECTION SYSTEM ───────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

async function showGroupTypeSelect(ctx, feature) {
  const label = FEAT_LABEL[feature] || feature;
  await reply(ctx,
    `${label}\n━━━━━━━━━━━━━━━━━━━━\n*Select groups:*`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([
      [Markup.button.callback("🔍 Similar Groups", `gs_similar_${feature}`)],
      [Markup.button.callback("📋 All Groups",      `gs_all_${feature}`)],
      [Markup.button.callback("☑️ Select Groups",   `gs_select_${feature}`)],
      [Markup.button.callback("🏠 Main Menu", "back_menu")],
    ]) }
  );
}

// ─── Feature entry points ─────────────────────────────────────────────────
const FEAT_MAP = {
  getlinks: "get_links", leave: "leave", removemem: "remove_members",
  makeadmin: "make_admin", approval: "approval", approvepending: "approve_pending",
  editsettings: "edit_settings", resetlink: "reset_link", demoteadmin: "demote_admin",
  autoaccept: "auto_accept",
};

Object.keys(FEAT_MAP).forEach((key) => {
  bot.action(`feat_${key}`, async (ctx) => {
    await ctx.answerCbQuery();
    if (getStatus(0) !== "connected") { await ctx.answerCbQuery("⚠️ WhatsApp not connected!", { show_alert: true }); return; }
    const feature = FEAT_MAP[key];
    updateSession(ctx.from.id, { featureFlow: defaultFeatureFlow(feature), cancelPending: false });
    await showGroupTypeSelect(ctx, feature);
  });
});

bot.action("feat_memberlist", async (ctx) => {
  await ctx.answerCbQuery();
  if (getStatus(0) !== "connected") { await ctx.answerCbQuery("⚠️ WhatsApp not connected!", { show_alert: true }); return; }
  updateSession(ctx.from.id, { featureFlow: defaultFeatureFlow("member_list"), cancelPending: false });
  await reply(ctx,
    `📋 *Member List*\n━━━━━━━━━━━━━━━━━━━━\n*What to view?*`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([
      [Markup.button.callback("👥 Member Count",     "ml_sub_members")],
      [Markup.button.callback("⏳ Pending Requests", "ml_sub_pending")],
      [Markup.button.callback("🏠 Main Menu", "back_menu")],
    ]) }
  );
});
bot.action("ml_sub_members", async (ctx) => {
  await ctx.answerCbQuery();
  updateSession(ctx.from.id, { featureFlow: { ...getSession(ctx.from.id).featureFlow, feature: "member_list" } });
  await showGroupTypeSelect(ctx, "member_list");
});
bot.action("ml_sub_pending", async (ctx) => {
  await ctx.answerCbQuery();
  updateSession(ctx.from.id, { featureFlow: { ...getSession(ctx.from.id).featureFlow, feature: "pending_list" } });
  await showGroupTypeSelect(ctx, "pending_list");
});

bot.action("feat_addmembers", async (ctx) => {
  await ctx.answerCbQuery();
  if (getStatus(0) !== "connected") { await ctx.answerCbQuery("⚠️ WhatsApp not connected!", { show_alert: true }); return; }
  updateSession(ctx.from.id, {
    featureFlow: { ...defaultFeatureFlow("add_members"), step: "am_links", links: [], vcfs: [], currentVcfIdx: 0, addMode: "bulk" },
    cancelPending: false,
  });
  await reply(ctx,
    `➕ *Add Members*\n━━━━━━━━━━━━━━━━━━━━\n\nSend group invite links — one per line:\n\`\`\`\nhttps://chat.whatsapp.com/ABC\nhttps://chat.whatsapp.com/DEF\n\`\`\``,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
  );
});

// ─── Change Name Entry ────────────────────────────────────────────────────
bot.action("feat_changename", async (ctx) => {
  await ctx.answerCbQuery();
  if (getStatus(0) !== "connected") { await ctx.answerCbQuery("⚠️ WhatsApp not connected!", { show_alert: true }); return; }
  updateSession(ctx.from.id, {
    featureFlow: { ...defaultFeatureFlow("change_name"), step: "cn_mode" },
    cancelPending: false,
  });
  await reply(ctx,
    `✏️ *Change Name*\n━━━━━━━━━━━━━━━━━━━━\n*Select naming method:*`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([
      [Markup.button.callback("🔀 Custom Name",       "cn_random")],
      [Markup.button.callback("📛 Match VCF Filename","cn_vcf")],
      [Markup.button.callback("🏠 Main Menu",          "back_menu")],
    ]) }
  );
});

// ─── CTC Checker: Start Check button ──────────────────────────────────────
bot.action("ctc_start_check", async (ctx) => {
  await ctx.answerCbQuery("Starting...");
  const uid  = ctx.from.id;
  const flow = getSession(uid).featureFlow;
  if (!flow || flow.step !== "ctc_vcf_collecting") {
    await ctx.answerCbQuery("⚠️ No active CTC session.", { show_alert: true }); return;
  }
  const vcfList = flow.vcfList || [];
  if (!vcfList.length) {
    await ctx.answerCbQuery("⚠️ Upload at least 1 VCF first!", { show_alert: true }); return;
  }
  updateSession(uid, { featureFlow: { ...flow, step: "ctc_running" }, awaitingVcf: null });
  await ctx.reply(`⏳ *Starting CTC check — ${vcfList.length} VCF(s) vs ${(flow.links||[]).length} group(s)...*`, { parse_mode: "Markdown" });
  await runCtcChecker(ctx);
});

// ─── CTC Checker Entry ────────────────────────────────────────────────────
bot.action("feat_ctcchecker", async (ctx) => {
  await ctx.answerCbQuery();
  if (getStatus(0) !== "connected") { await ctx.answerCbQuery("⚠️ WhatsApp not connected!", { show_alert: true }); return; }
  updateSession(ctx.from.id, {
    featureFlow: { ...defaultFeatureFlow("ctc_checker"), step: "ctc_links", links: [], vcfList: [], ctcVcfIdx: 0 },
    cancelPending: false,
  });
  await reply(ctx,
    `🔍 *CTC Checker*\n━━━━━━━━━━━━━━━━━━━━\n\n*Step 1:* Send all group invite links — one per line:\n\`\`\`\nhttps://chat.whatsapp.com/ABC\nhttps://chat.whatsapp.com/DEF\n\`\`\`\n_Then in Step 2 send ALL VCFs at once (1st VCF = 1st group, 2nd = 2nd, etc.)_`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
  );
});

// ─── Similar Groups ───────────────────────────────────────────────────────
bot.action(/^gs_similar_(.+)$/, async (ctx) => {
  await ctx.answerCbQuery("Detecting groups...");
  const feature = ctx.match[1];
  try {
    const all = await getAllGroupsWithDetails(0);
    if (!all.length) { await reply(ctx, "❌ No groups found.", Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]])); return; }
    const wordMap = {};
    for (const g of all) {
      const firstWord = (g.name.trim().split(/\s+/)[0] || g.name).toLowerCase();
      if (!wordMap[firstWord]) wordMap[firstWord] = [];
      wordMap[firstWord].push(g.id);
    }
    const entries = Object.entries(wordMap).sort((a, b) => b[1].length - a[1].length);
    updateSession(ctx.from.id, {
      featureFlow: { ...getSession(ctx.from.id).featureFlow, feature, allGroups: all, wordGroups: wordMap, step: "similar_pick" },
    });
    const visEntries = entries.slice(0, 20);
    const rows = [];
    for (let i = 0; i < visEntries.length; i += 2) {
      const row = [];
      for (let j = i; j < Math.min(i + 2, visEntries.length); j++) {
        const [word, ids] = visEntries[j];
        const idx = entries.findIndex(([w]) => w === word);
        row.push(Markup.button.callback(`${word} (${ids.length})`, `gs_swp_${idx}`));
      }
      rows.push(row);
    }
    rows.push([Markup.button.callback("🔍 Custom Keyword", "gs_sim_custom")]);
    rows.push([Markup.button.callback("🏠 Main Menu", "back_menu")]);
    await reply(ctx,
      `🔍 *Similar Groups*\n━━━━━━━━━━━━━━━━━━━━\nTotal: *${all.length}* groups\n\n*Auto-detected prefixes — tap to select:*`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard(rows) }
    );
  } catch (err) { await reply(ctx, `❌ Error: ${err.message}`, Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]])); }
});

bot.action(/^gs_swp_(\d+)$/, async (ctx) => {
  await ctx.answerCbQuery();
  const idx  = parseInt(ctx.match[1]);
  const flow = getSession(ctx.from.id).featureFlow;
  const entries = Object.entries(flow.wordGroups || {}).sort((a, b) => b[1].length - a[1].length);
  if (idx >= entries.length) return;
  const [word, ids] = entries[idx];
  const matching = flow.allGroups.filter((g) => ids.includes(g.id));
  updateSession(ctx.from.id, { featureFlow: { ...flow, selectedIds: ids, keyword: word, step: "confirm" } });
  await reply(ctx,
    `✅ *"${word}" — ${matching.length} group(s):*\n━━━━━━━━━━━━━━━━━━━━\n${matching.slice(0, 20).map((g, i) => `${i + 1}. ${g.name}`).join("\n")}${matching.length > 20 ? `\n_...and ${matching.length - 20} more_` : ""}`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([
      [Markup.button.callback("🚀 Proceed", "gs_sim_proceed")],
      [Markup.button.callback("🔙 Back",    `gs_similar_${flow.feature}`)],
      [Markup.button.callback("🏠 Main Menu", "back_menu")],
    ]) }
  );
});

bot.action("gs_sim_custom", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  updateSession(ctx.from.id, { featureFlow: { ...flow, step: "similar_query" } });
  await reply(ctx, `🔍 *Custom Keyword Search*\n━━━━━━━━━━━━━━━━━━━━\nType a keyword to search group names:`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) });
});

bot.action(/^gs_all_(.+)$/, async (ctx) => {
  await ctx.answerCbQuery("Loading groups...");
  const feature = ctx.match[1];
  try {
    const groups = await getAllGroupsWithDetails(0);
    if (!groups.length) { await reply(ctx, "❌ No groups found.", Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]])); return; }
    updateSession(ctx.from.id, {
      featureFlow: { ...getSession(ctx.from.id).featureFlow, feature, allGroups: groups, selectedIds: groups.map(g=>g.id), step: "confirm" },
    });
    await reply(ctx,
      `✅ *All Groups Selected — ${groups.length} groups*\n━━━━━━━━━━━━━━━━━━━━\n${groups.slice(0, 10).map((g, i) => `${i + 1}. ${g.name}`).join("\n")}${groups.length > 10 ? `\n_...and ${groups.length - 10} more_` : ""}`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard([
        [Markup.button.callback("🚀 Proceed", "gs_sim_proceed")],
        [Markup.button.callback("🏠 Main Menu", "back_menu")],
      ]) }
    );
  } catch (err) { await reply(ctx, `❌ Error: ${err.message}`, Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]])); }
});

bot.action(/^gs_select_(.+)$/, async (ctx) => {
  await ctx.answerCbQuery("Loading...");
  const feature = ctx.match[1];
  try {
    const groups = await getAllGroupsWithDetails(0);
    if (!groups.length) { await reply(ctx, "❌ No groups found.", Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]])); return; }
    updateSession(ctx.from.id, {
      featureFlow: { ...getSession(ctx.from.id).featureFlow, feature, allGroups: groups, selectedIds: [], page: 0, step: "select" },
    });
    await showPaginatedGroups(ctx);
  } catch (err) { await reply(ctx, `❌ Error: ${err.message}`, Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]])); }
});

async function showPaginatedGroups(ctx) {
  const flow = getSession(ctx.from.id).featureFlow;
  const { allGroups, selectedIds, page } = flow;
  const selSet = new Set(selectedIds);
  const totalPages = Math.ceil(allGroups.length / PAGE_SIZE);
  const slice = allGroups.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const rows = [];
  for (let i = 0; i < slice.length; i++) {
    const idx = page * PAGE_SIZE + i, g = slice[i];
    const name = g.name.length > 40 ? g.name.slice(0, 39) + "…" : g.name;
    rows.push([Markup.button.callback(`${selSet.has(g.id) ? "✅" : "◻️"} ${name}`, `gs_tog_${idx}`)]);
  }
  const nav = [];
  if (page > 0)              nav.push(Markup.button.callback("◀️", "gs_prev"));
  nav.push(Markup.button.callback(`${page + 1}/${totalPages}`, "gs_noop"));
  if (page < totalPages - 1) nav.push(Markup.button.callback("▶️", "gs_next"));
  rows.push(nav);
  rows.push([Markup.button.callback(`✅ Confirm (${selSet.size} selected)`, "gs_confirm")]);
  rows.push([Markup.button.callback("🏠 Main Menu", "back_menu")]);
  const text = `☑️ *Select Groups* — Page ${page + 1}/${totalPages}\n━━━━━━━━━━━━━━━━━━━━\nTotal: *${allGroups.length}*  •  Selected: *${selSet.size}*\n_Tap to select/deselect_`;
  try { await ctx.editMessageText(text, { parse_mode: "Markdown", reply_markup: Markup.inlineKeyboard(rows).reply_markup }); }
  catch { await ctx.reply(text, { parse_mode: "Markdown", ...Markup.inlineKeyboard(rows) }); }
}

bot.action("gs_noop", async (ctx) => { await ctx.answerCbQuery(); });
bot.action("gs_next", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  if (flow.page < Math.ceil(flow.allGroups.length / PAGE_SIZE) - 1)
    updateSession(ctx.from.id, { featureFlow: { ...flow, page: flow.page + 1 } });
  await showPaginatedGroups(ctx);
});
bot.action("gs_prev", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  if (flow.page > 0) updateSession(ctx.from.id, { featureFlow: { ...flow, page: flow.page - 1 } });
  await showPaginatedGroups(ctx);
});
bot.action(/^gs_tog_(\d+)$/, async (ctx) => {
  await ctx.answerCbQuery();
  const idx = parseInt(ctx.match[1]);
  const flow = getSession(ctx.from.id).featureFlow;
  const gid  = flow.allGroups[idx]?.id; if (!gid) return;
  const sel  = new Set(flow.selectedIds);
  sel.has(gid) ? sel.delete(gid) : sel.add(gid);
  updateSession(ctx.from.id, { featureFlow: { ...flow, selectedIds: [...sel] } });
  await showPaginatedGroups(ctx);
});
bot.action("gs_confirm", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  if (!flow.selectedIds.length) { await ctx.answerCbQuery("⚠️ Select at least 1 group!", { show_alert: true }); return; }
  await onGroupsConfirmed(ctx, flow.feature, flow.selectedIds, flow.allGroups);
});
bot.action("gs_sim_proceed", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  await onGroupsConfirmed(ctx, flow.feature, flow.selectedIds, flow.allGroups);
});

// ─── Route after group selection ──────────────────────────────────────────
async function onGroupsConfirmed(ctx, feature, selectedIds, allGroups) {
  const s = getSession(ctx.from.id);
  if (feature === "make_admin") {
    updateSession(ctx.from.id, { featureFlow: { ...s.featureFlow, selectedIds, allGroups, step: "admin_numbers" } });
    await reply(ctx,
      `👑 *Make Admin*\n━━━━━━━━━━━━━━━━━━━━\n*${selectedIds.length} group(s) selected*\n\nSend phone numbers to make admin — one per line:\n\`\`\`\n919876543210\n918765432109\n\`\`\`\n_Country code required_`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
    );
    return;
  }
  if (feature === "demote_admin") {
    updateSession(ctx.from.id, { featureFlow: { ...s.featureFlow, selectedIds, allGroups, step: "demote_numbers" } });
    await reply(ctx,
      `⬇️ *Demote Admin*\n━━━━━━━━━━━━━━━━━━━━\n*${selectedIds.length} group(s) selected*\n\nSend admin phone numbers to demote:`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
    );
    return;
  }
  if (feature === "edit_settings") {
    updateSession(ctx.from.id, { featureFlow: { ...s.featureFlow, selectedIds, allGroups, step: "es_configure",
      desiredSettings: { announce: null, restrict: null, joinApproval: null, memberAddMode: null } } });
    await showEditSettingsConfig(ctx);
    return;
  }
  if (feature === "auto_accept") {
    updateSession(ctx.from.id, { featureFlow: { ...s.featureFlow, selectedIds, allGroups, step: "aa_duration" } });
    await showAutoAcceptDuration(ctx);
    return;
  }
  updateSession(ctx.from.id, { featureFlow: { ...s.featureFlow, selectedIds, allGroups } });
  await runFeature(ctx, feature, selectedIds, allGroups, []);
}

// ══════════════════════════════════════════════════════════════════════════
// ─── EDIT SETTINGS — ON/OFF DISPLAY ──────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

function esFmt(val) {
  if (val === null || val === undefined) return "Skip";
  return val ? "✅ ON" : "❌ OFF";
}

// For "All Can Send": ON means announce=false (unlocked), OFF means announce=true (locked)
// So we invert the display: announce=false → ON (✅), announce=true → OFF (❌)
function esFmtSend(val) {
  if (val === null || val === undefined) return "Skip";
  return val === false ? "✅ ON" : "❌ OFF"; // val=false means unlocked=all can send
}

function settingsKb(d) {
  return Markup.inlineKeyboard([
    [Markup.button.callback(`💬 All Can Send      : ${esFmtSend(d.announce)}`,  "es_tog_announce")],
    [Markup.button.callback(`✏️ Edit Info (lock)  : ${esFmt(d.restrict)}`,      "es_tog_restrict")],
    [Markup.button.callback(`🔐 Join Approval     : ${esFmt(d.joinApproval)}`,  "es_tog_joinApproval")],
    [Markup.button.callback(`➕ All Can Add       : ${esFmt(d.memberAddMode)}`, "es_tog_memberAddMode")],
    [Markup.button.callback("💾 Apply Settings", "es_apply")],
    [Markup.button.callback("🏠 Main Menu", "back_menu")],
  ]);
}

async function showEditSettingsConfig(ctx) {
  const flow = getSession(ctx.from.id).featureFlow;
  const d    = flow.desiredSettings;
  await reply(ctx,
    `⚙️ *Edit Settings*\n━━━━━━━━━━━━━━━━━━━━\n*${flow.selectedIds.length} group(s) selected*\n\nTap to toggle — cycles: Skip → ON → OFF\n• *Skip* = don't change this setting`,
    { parse_mode: "Markdown", ...settingsKb(d) }
  );
}

["announce", "restrict", "joinApproval", "memberAddMode"].forEach((key) => {
  bot.action(`es_tog_${key}`, async (ctx) => {
    await ctx.answerCbQuery();
    const flow = getSession(ctx.from.id).featureFlow;
    const cur  = flow.desiredSettings[key];
    let next;
    if (key === "announce") {
      // "All Can Send": Skip → ON(false=unlocked) → OFF(true=locked) → Skip
      next = cur === null ? false : cur === false ? true : null;
    } else {
      // Other keys: Skip → ON(true) → OFF(false) → Skip
      next = cur === null ? true : cur === true ? false : null;
    }
    const newSettings = { ...flow.desiredSettings, [key]: next };
    updateSession(ctx.from.id, { featureFlow: { ...flow, desiredSettings: newSettings } });
    try { await ctx.editMessageReplyMarkup(settingsKb(newSettings).reply_markup); }
    catch { await showEditSettingsConfig(ctx); }
  });
});

bot.action("es_apply", async (ctx) => {
  await ctx.answerCbQuery("Applying...");
  const uid  = ctx.from.id;
  const flow = getSession(uid).featureFlow;
  const d    = flow.desiredSettings;
  if (d.announce === null && d.restrict === null && d.joinApproval === null && d.memberAddMode === null) {
    await ctx.answerCbQuery("⚠️ No settings selected!", { show_alert: true }); return;
  }
  const sel   = flow.allGroups.filter((g) => flow.selectedIds.includes(g.id));
  const total = sel.length;
  updateSession(uid, { cancelPending: false });
  const pm = await startProgress(ctx, uid, `⚙️ Applying settings — ${total} group(s)...\n${bar(0, total)}`);
  let changed = 0, alreadyOk = 0, failed = 0, cancelled = false;
  for (let i = 0; i < total; i++) {
    if (isCancelled(uid)) { cancelled = true; break; }
    const g = sel[i];
    await editProgress(ctx.chat.id, pm.message_id,
      `⚙️ Applying settings...\nDone: ${i}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
    try {
      const result = await withRetry(() => applyGroupSettings(0, g.id, d), 2, 2000);
      if (result.changes.length) { changed++; }
      else { alreadyOk++; }
    } catch (err) { failed++; }
    await sleep(D.approvalToggle);
  }
  await sendSummary(ctx, { feature: "edit_settings", total, success: changed, failed, cancelled,
    extra: [`Total Selected: ${total}`, `Changed       : ${changed}`, `Already OK    : ${alreadyOk}`, `Failed        : ${failed}`] });
  updateSession(uid, { featureFlow: null });
});

// ══════════════════════════════════════════════════════════════════════════
// ─── CHANGE NAME — CUSTOM ─────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

bot.action("cn_random", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  updateSession(ctx.from.id, { featureFlow: { ...flow, step: "cn_random_name", cnMethod: "random" } });
  await reply(ctx,
    `✏️ *Change Name — Custom*\n━━━━━━━━━━━━━━━━━━━━\n\nType the base name:\n_Example:_ \`Madara\` → groups become _Madara 1, Madara 2..._`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
  );
});

bot.action("cn_numbering_yes", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  updateSession(ctx.from.id, { featureFlow: { ...flow, numbering: true, step: "cn_random_links" } });
  await reply(ctx,
    `✏️ *Numbering: ON*\nNames: _${flow.cnBaseName} 1, ${flow.cnBaseName} 2..._\n\nNow send group invite links (one per line):`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
  );
});
bot.action("cn_numbering_no", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  updateSession(ctx.from.id, { featureFlow: { ...flow, numbering: false, step: "cn_random_links" } });
  await reply(ctx,
    `✏️ *Numbering: OFF*\nAll groups: _${flow.cnBaseName}_\n\nNow send group invite links (one per line):`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
  );
});

async function runChangeNameRandom(ctx, links, baseName, numbering) {
  const uid = ctx.from.id;
  const total = links.length;
  updateSession(uid, { cancelPending: false });
  const pm = await startProgress(ctx, uid, `✏️ Renaming ${total} group(s)...\n${bar(0, total)}`);
  let done = 0, failed = 0, cancelled = false;
  const boxLines = [];
  for (let i = 0; i < total; i++) {
    if (isCancelled(uid)) { cancelled = true; break; }
    const code    = links[i];
    const newName = numbering ? `${baseName} ${i + 1}` : baseName;
    await editProgress(ctx.chat.id, pm.message_id,
      `✏️ Renaming...\nDone: ${done}/${total}  ❌ ${failed}\n→ "${newName}"\n${bar(i, total)}`);
    try {
      const info = await withTimeout(withRetry(() => getGroupInfoFromLink(0, code), 2, 1500), 12000, "GetGroupInfo");
      if (!info) throw new Error("Invalid/expired link");
      await withTimeout(withRetry(() => renameGroup(0, info.id, newName), 2, 1500), 12000, "RenameGroup");
      done++; boxLines.push(`${info.name} ➡️ ${newName}`);
    } catch (err) { failed++; boxLines.push(`❌ Group ${i + 1}: ${err.message}`); }
    await sleep(D.changeName);
  }
  await sendSummary(ctx, { feature: "change_name", total, success: done, failed, cancelled, boxLines });
  updateSession(uid, { featureFlow: null });
}

// ══════════════════════════════════════════════════════════════════════════
// ─── CHANGE NAME AS VCF — NEW AUTO-SCAN FLOW ──────────────────────────────
//
// NO LINKS NEEDED! Bot scans ALL groups automatically.
// 1. User sends VCF files one by one
// 2. User taps "Start Renaming"
// 3. Bot fetches ALL groups, checks members + pending for VCF numbers
// 4. Groups with matching numbers get renamed to the VCF filename
// ══════════════════════════════════════════════════════════════════════════

bot.action("cn_vcf", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  updateSession(ctx.from.id, {
    featureFlow: { ...flow, step: "cn_vcf_collecting", cnMethod: "vcf", vcfList: [] },
    awaitingVcf: { feature: "change_name", step: "cn_vcf" },
  });
  await reply(ctx,
    `📛 *Change Name — Match VCF Filename*\n━━━━━━━━━━━━━━━━━━━━\n\n*How it works:*\n• Send multiple VCF files at once (e.g. 50 VCFs)\n• Bot scans ALL your groups (even 100+ groups)\n• For each group, checks *members + pending requests* against every VCF\n• Whichever VCF has the most matching numbers → group gets renamed to that VCF filename\n• Partial match is fine (e.g. VCF has 50 numbers, 20-30 found = match)\n\n*No group links needed!*\n\n📎 *Send all VCF files now, then tap Start Renaming:*`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) }
  );
});

async function showVcfCollectStatus(ctx, vcfList) {
  const lines = vcfList.map((v, i) => `${i + 1}. *${v.name}* — ${v.contacts.length} contacts`).join("\n");
  await reply(ctx,
    `📛 *VCFs collected: ${vcfList.length}*\n━━━━━━━━━━━━━━━━━━━━\n${lines}\n━━━━━━━━━━━━━━━━━━━━\nSend more VCF files or tap *Start Renaming*:`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([
      [Markup.button.callback(`▶️ Start Renaming (${vcfList.length} VCF${vcfList.length > 1 ? "s" : ""})`, "cn_vcf_start")],
      [Markup.button.callback("🏠 Main Menu", "back_menu")],
    ]) }
  );
}

bot.action("cn_vcf_start", async (ctx) => {
  await ctx.answerCbQuery("Starting...");
  const uid  = ctx.from.id;
  const flow = getSession(uid).featureFlow;
  const vcfList = flow.vcfList || [];
  if (!vcfList.length) { await ctx.answerCbQuery("⚠️ Send at least one VCF first!", { show_alert: true }); return; }
  updateSession(uid, { awaitingVcf: null });
  await runChangeNameAsVcfAuto(ctx, vcfList);
});

/**
 * runChangeNameAsVcfAuto — Scans ALL groups and renames matching ones.
 *
 * NEW Matching logic (LID-safe via onWhatsApp resolution):
 *  1. Pre-resolve ALL VCF phone numbers → real JIDs (phoneJid + lid) via onWhatsApp()
 *     This is done ONCE per VCF before the group loop (not repeated per group).
 *  2. For each group:
 *     a. Build a JID set from existing members (all fields: @lid, @s.whatsapp.net, etc.)
 *     b. Add pending request JIDs from getPendingRawJids()
 *  3. For each VCF: count how many resolved contacts have phoneJid OR lid in that group's JID set
 *  4. Best-match VCF wins → group renamed to that VCF's filename
 *
 * This completely bypasses the LID problem — we never compare raw phone strings.
 * Instead we compare WhatsApp's own JID/LID identifiers against each other.
 */
async function runChangeNameAsVcfAuto(ctx, vcfList) {
  const uid = ctx.from.id;
  updateSession(uid, { cancelPending: false });

  // Step 1: fetch all groups
  const loadMsg = await ctx.reply(`📛 *Loading all groups...*`, { parse_mode: "Markdown" });
  let allGroups;
  try {
    allGroups = await withRetry(() => getAllGroupsWithDetails(0));
  } catch (err) {
    try { await bot.telegram.deleteMessage(ctx.chat.id, loadMsg.message_id); } catch {}
    await ctx.reply(`❌ Failed to load groups: ${err.message}`); return;
  }
  try { await bot.telegram.deleteMessage(ctx.chat.id, loadMsg.message_id); } catch {}

  const totalGroups = allGroups.length;

  // ── Step 2: Pre-resolve ALL VCF phones via onWhatsApp (ONCE per VCF) ──────
  // resolveVcfPhones returns [{phone, phoneJid, lid}] for each number.
  // phoneJid = "123...@s.whatsapp.net", lid = "xxx@lid" (if Baileys returns it)
  const resolveMsg = await ctx.reply(
    `📛 *Resolving ${vcfList.length} VCF(s) via WhatsApp...*\n_(this may take a moment)_`,
    { parse_mode: "Markdown" }
  );
  const resolvedVcfs = [];
  for (const v of vcfList) {
    const phones = (v.contacts || []).map((c) => c.phone);
    const resolved = phones.length ? await resolveVcfPhones(0, phones) : [];
    resolvedVcfs.push({ name: v.name, resolved });
    await sleep(300);
  }
  try { await bot.telegram.deleteMessage(ctx.chat.id, resolveMsg.message_id); } catch {}

  const pm = await startProgress(ctx, uid,
    `📛 Scanning ${totalGroups} group(s)...\nVCFs: ${vcfList.length}\n${bar(0, totalGroups)}`);

  let renamed = 0, skipped = 0, failed = 0, cancelled = false;
  const boxLines = [];

  for (let i = 0; i < totalGroups; i++) {
    if (isCancelled(uid)) { cancelled = true; break; }
    const g = allGroups[i];
    await editProgress(ctx.chat.id, pm.message_id,
      `📛 Scanning groups...\nRenamed: ${renamed}  Skipped: ${skipped}  ❌ ${failed}\n→ ${g.name}\n${bar(i, totalGroups)}`);

    try {
      // ── Build JID set + phone set for this group: members + pending ────────
      const groupJids   = new Set();
      const groupPhones = new Set();

      for (const p of (g.participants || [])) {
        const fields = [p.jid, p.id, p.lid, p.participant, p.userJid]
          .filter((j) => j && typeof j === "string");
        for (const j of fields) {
          const norm = j.replace(/:\d+@/, "@").toLowerCase().trim();
          groupJids.add(norm);
          if (norm.endsWith("@s.whatsapp.net")) {
            const ph = norm.split("@")[0];
            if (ph && ph.length >= 7) groupPhones.add(ph);
          }
        }
      }

      try {
        const { jids: pendingJids, phones: pendingPhones } =
          await withTimeout(withRetry(() => getPendingRawJids(0, g.id), 2, 1500), 10000, "PendingJids");
        pendingJids.forEach((j) => groupJids.add(j));
        pendingPhones.forEach((ph) => groupPhones.add(ph));
      } catch {}
      await sleep(D.pendingCheck);

      // ── Find best-matching VCF (TRIPLE FALLBACK) ──────────────────────────
      let bestVcf   = null;
      let bestCount = 0;

      for (const vcf of resolvedVcfs) {
        let count = 0;
        for (const r of vcf.resolved) {
          if (
            (r.phoneJid && groupJids.has(r.phoneJid)) ||
            (r.lid      && groupJids.has(r.lid))
          ) { count++; continue; }
          if (r.phone && groupPhones.has(r.phone)) { count++; continue; }
          if (r.phone && groupPhones.size > 0) {
            for (const gph of groupPhones) {
              if (numberMatches(gph, r.phone)) { count++; break; }
            }
          }
        }
        if (count > bestCount) { bestCount = count; bestVcf = vcf; }
      }

      if (bestVcf && bestCount > 0) {
        await withTimeout(withRetry(() => renameGroup(0, g.id, bestVcf.name), 2, 1500), 12000, "RenameGroup");
        renamed++;
        boxLines.push(`${g.name} ➡️ ${bestVcf.name}`);
      } else {
        skipped++;
      }
    } catch (err) {
      failed++;
      boxLines.push(`❌ ${g.name}: ${err.message}`);
    }

    await sleep(D.vcfAutoMatch);
  }

  const extra = [
    `Groups scanned : ${totalGroups}`,
    `Renamed        : ${renamed}`,
    `No match (skip): ${skipped}`,
  ];
  await sendSummary(ctx, { feature: "change_name", total: totalGroups, success: renamed, failed, cancelled, extra, boxLines });
  updateSession(uid, { featureFlow: null, awaitingVcf: null });
}

// ══════════════════════════════════════════════════════════════════════════
// ─── AUTO ACCEPT FLOW ─────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

async function showAutoAcceptDuration(ctx) {
  const flow = getSession(ctx.from.id).featureFlow;
  await reply(ctx,
    `⏰ *Auto Accept*\n━━━━━━━━━━━━━━━━━━━━\n*${flow.selectedIds.length} group(s) selected*\n\nSelect duration:`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([
      [Markup.button.callback("5 min",  "aa_dur_300"),    Markup.button.callback("10 min", "aa_dur_600")],
      [Markup.button.callback("30 min", "aa_dur_1800"),   Markup.button.callback("1 hour", "aa_dur_3600")],
      [Markup.button.callback("2 hrs",  "aa_dur_7200"),   Markup.button.callback("6 hrs",  "aa_dur_21600")],
      [Markup.button.callback("✏️ Custom (minutes)", "aa_dur_custom")],
      [Markup.button.callback("🏠 Main Menu", "back_menu")],
    ]) }
  );
}

bot.action(/^aa_dur_(\d+)$/, async (ctx) => {
  await ctx.answerCbQuery();
  const secs = parseInt(ctx.match[1]);
  const flow = getSession(ctx.from.id).featureFlow;
  updateSession(ctx.from.id, { featureFlow: { ...flow, aaDuration: secs, step: "aa_confirm" } });
  const mins = secs / 60, label = mins >= 60 ? `${mins / 60}h` : `${mins}min`;
  await reply(ctx,
    `⏰ *Auto Accept — Confirm*\n━━━━━━━━━━━━━━━━━━━━\nGroups   : *${flow.selectedIds.length}*\nDuration : *${label}*\n\n_Checks every 5 seconds. Approval must be ON._`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([
      [Markup.button.callback("▶️ Start", "aa_start")],
      [Markup.button.callback("🔙 Change Duration", "aa_back_duration")],
      [Markup.button.callback("🏠 Main Menu", "back_menu")],
    ]) }
  );
});

bot.action("aa_dur_custom", async (ctx) => {
  await ctx.answerCbQuery();
  const flow = getSession(ctx.from.id).featureFlow;
  updateSession(ctx.from.id, { featureFlow: { ...flow, step: "aa_custom_duration" } });
  await reply(ctx, `⏰ *Custom Duration*\nType minutes:\n_Example:_ \`120\` = 2 hours`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu", "back_menu")]]) });
});

bot.action("aa_back_duration", async (ctx) => { await ctx.answerCbQuery(); await showAutoAcceptDuration(ctx); });

function buildLiveAutoAcceptText(sel, label, endTime, stats) {
  const totalAccepted = Object.values(stats).reduce((s, v) => s + (v?.accepted || 0), 0);
  const groupLines    = sel.map((g) => `• *${g.name}*: ${stats[g.id]?.accepted || 0}`).join("\n");
  return (
    `⏰ *Auto Accept — ACTIVE* 🟢\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `Groups   : *${sel.length}*\n` +
    `Duration : *${label}*\n` +
    `Ends at  : ${endTime}\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `✅ *Total Accepted: ${totalAccepted}*\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `${groupLines}\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `_Checks every 5 sec._`
  );
}

bot.action("aa_start", async (ctx) => {
  await ctx.answerCbQuery("Starting...");
  const uid  = ctx.from.id;
  const flow = getSession(uid).featureFlow;
  const secs = flow.aaDuration;
  const sel  = (flow.allGroups || []).filter((g) => flow.selectedIds.includes(g.id));
  const mins = secs / 60, label = mins >= 60 ? `${mins / 60}h` : `${mins}min`;
  const endTime = new Date(Date.now() + secs * 1000).toLocaleTimeString();
  if (aaLiveIntervals.has(uid)) { clearInterval(aaLiveIntervals.get(uid)); aaLiveIntervals.delete(uid); }
  startAutoAcceptForGroups(flow.selectedIds);
  updateSession(uid, { featureFlow: { ...flow, step: "aa_running" } });
  const initialStats = getAutoAcceptStats(flow.selectedIds);
  const statusMsg = await reply(ctx,
    buildLiveAutoAcceptText(sel, label, endTime, initialStats),
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("🛑 Stop", "aa_stop")]]) }
  );
  const liveInterval = setInterval(async () => {
    try {
      const stats = getAutoAcceptStats(flow.selectedIds);
      await bot.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, undefined,
        buildLiveAutoAcceptText(sel, label, endTime, stats),
        { parse_mode: "Markdown", reply_markup: Markup.inlineKeyboard([[Markup.button.callback("🛑 Stop", "aa_stop")]]).reply_markup }
      );
    } catch {}
  }, 5000);
  aaLiveIntervals.set(uid, liveInterval);
  setTimeout(async () => {
    if (!aaLiveIntervals.has(uid)) return;
    clearInterval(aaLiveIntervals.get(uid)); aaLiveIntervals.delete(uid);
    const stats = getAutoAcceptStats(flow.selectedIds);
    stopAutoAcceptForGroups(flow.selectedIds);
    const totalAccepted = Object.values(stats).reduce((s, v) => s + (v?.accepted || 0), 0);
    const boxLines = sel.map((g) => `${g.name}: ${stats[g.id]?.accepted || 0} accepted`);
    try { await bot.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, undefined,
      `⏰ *Auto Accept — Finished*  ✅ Accepted: *${totalAccepted}*`, { parse_mode: "Markdown" }); } catch {}
    await sendSummary(ctx, { feature: "auto_accept", total: sel.length, success: sel.length, failed: 0, cancelled: false,
      extra: [`Total Groups : ${sel.length}`, `Total Accepted: ${totalAccepted}`, `Duration      : ${label}`],
      boxLines });
    updateSession(uid, { featureFlow: null });
  }, secs * 1000);
});

bot.action("aa_stop", async (ctx) => {
  await ctx.answerCbQuery("Stopping...");
  const uid  = ctx.from.id;
  const flow = getSession(uid).featureFlow;
  if (aaLiveIntervals.has(uid)) { clearInterval(aaLiveIntervals.get(uid)); aaLiveIntervals.delete(uid); }
  if (!flow?.selectedIds) { await sendMainMenu(ctx); return; }
  const stats = getAutoAcceptStats(flow.selectedIds);
  stopAutoAcceptForGroups(flow.selectedIds);
  const totalAccepted = Object.values(stats).reduce((s, v) => s + (v?.accepted || 0), 0);
  const sel     = (flow.allGroups || []).filter((g) => flow.selectedIds.includes(g.id));
  const boxLines = sel.map((g) => `${g.name}: ${stats[g.id]?.accepted || 0} accepted`);
  try { await ctx.editMessageText(`🛑 *Auto Accept Stopped*  Total: *${totalAccepted}*`, { parse_mode: "Markdown" }); } catch {}
  await sendSummary(ctx, { feature: "auto_accept", total: sel.length, success: sel.length, failed: 0, cancelled: true,
    extra: [`Total Groups : ${sel.length}`, `Total Accepted: ${totalAccepted}`], boxLines });
  updateSession(uid, { featureFlow: null });
});

// ══════════════════════════════════════════════════════════════════════════
// ─── MAIN FEATURE RUNNER (All operation delays tuned) ─────────────────────
// ══════════════════════════════════════════════════════════════════════════

async function runFeature(ctx, feature, selectedIds, allGroups, extraNums) {
  const uid   = ctx.from.id;
  const sel   = allGroups.filter((g) => selectedIds.includes(g.id));
  const total = sel.length;
  if (!total) { await reply(ctx, "❌ No groups selected."); return; }
  updateSession(uid, { cancelPending: false });

  // ── GET LINKS ──────────────────────────────────────────────────────────
  if (feature === "get_links") {
    const pm = await startProgress(ctx, uid, `🔗 Getting links — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, cancelled = false;
    const results = [], fails = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `🔗 Getting links...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try { results.push({ name: g.name, link: await withRetry(() => getGroupInviteLink(0, g.id)) }); done++; }
      catch (err) { fails.push(g.name); failed++; }
      await sleep(D.getLinks);
    }
    // Build boxLines: group name + link
    const boxLines = results.map((r) => `${r.name}\n${r.link}`);
    if (fails.length) fails.forEach((n) => boxLines.push(`❌ ${n}: failed`));
    await sendSummary(ctx, { feature: "get_links", total, success: done, failed, cancelled,
      extra: [`Total Groups : ${total}`, `Successful   : ${done}`, `Failed       : ${failed}`],
      boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── LEAVE GROUPS (direct leave — no member removal) ──────────────────
  if (feature === "leave") {
    const pm = await startProgress(ctx, uid, `🚪 Leaving ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, cancelled = false;
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `🚪 Leaving groups...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        await withRetry(() => leaveGroup(0, g.id));
        done++;
      } catch (err) { failed++; }
      await sleep(D.leave);
    }
    await sendSummary(ctx, { feature, total, success: done, failed, cancelled,
      extra: [`Total Selected: ${total}`, `Leave Success : ${done}`, `Leave Failed  : ${failed}`] });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── REMOVE MEMBERS (1 by 1, bot stays in group) ───────────────────────
  if (feature === "remove_members") {
    const pm = await startProgress(ctx, uid, `🧹 Removing members — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, totalRem = 0, cancelled = false;
    const boxLines = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `🧹 Removing members (1 by 1)...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        // batchSize=1, skipAdmins=true: removes regular members 1 by 1, admins + bot stay safe
        const n = await withRetry(() => removeAllMembers(0, g.id, 1, true));
        totalRem += n; done++; boxLines.push(`${g.name}: ${n} members removed`);
      } catch (err) { failed++; boxLines.push(`❌ ${g.name}: failed`); }
      await sleep(D.removeMembers);
    }
    await sendSummary(ctx, { feature, total, success: done, failed, cancelled,
      extra: [`Total Selected: ${total}`, `Total Removed : ${totalRem}`, `Bot left group: ❌ No (stays in group)`], boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── MAKE ADMIN ────────────────────────────────────────────────────────
  if (feature === "make_admin") {
    const pm = await startProgress(ctx, uid, `👑 Making admin — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, totalProm = 0, cancelled = false;
    const boxLines = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `👑 Making admin...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        const n = await makeAdminByNumbers(0, g.id, extraNums);
        totalProm += n; done++;
        boxLines.push(n > 0 ? `${g.name}: ${n} admin set` : `${g.name}: not found`);
      } catch (err) { failed++; boxLines.push(`❌ ${g.name}: failed`); }
      await sleep(D.makeAdmin);
    }
    const numbersStr = extraNums.map((n) => `+${n}`).join(", ");
    await sendSummary(ctx, { feature, total, success: done, failed, cancelled,
      extra: [`Number(s)     : ${numbersStr}`, `Total Selected: ${total}`, `Admin Set     : ${totalProm}`],
      boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── DEMOTE ADMIN ──────────────────────────────────────────────────────
  if (feature === "demote_admin") {
    const pm = await startProgress(ctx, uid, `⬇️ Demoting admins — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, totalDem = 0, cancelled = false;
    const boxLines = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `⬇️ Demoting admins...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        const n = await demoteAdminInGroup(0, g.id, extraNums);
        totalDem += n; done++;
        boxLines.push(n > 0 ? `${g.name}: ${n} demoted` : `${g.name}: not an admin`);
      } catch (err) { failed++; boxLines.push(`❌ ${g.name}: failed`); }
      await sleep(D.demoteAdmin);
    }
    const numbersStr = extraNums.map((n) => `+${n}`).join(", ");
    await sendSummary(ctx, { feature, total, success: done, failed, cancelled,
      extra: [`Number(s)     : ${numbersStr}`, `Total Selected: ${total}`, `Total Demoted : ${totalDem}`],
      boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── RESET LINK ────────────────────────────────────────────────────────
  if (feature === "reset_link") {
    const pm = await startProgress(ctx, uid, `🔄 Resetting links — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, cancelled = false;
    const results = [], fails = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `🔄 Resetting links...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try { results.push({ name: g.name, link: await withRetry(() => resetGroupInviteLink(0, g.id)) }); done++; }
      catch { fails.push(g.name); failed++; }
      await sleep(D.resetLink);
    }
    const boxLines = results.map((r) => `${r.name}\n${r.link}`);
    if (fails.length) fails.forEach((n) => boxLines.push(`❌ ${n}: failed`));
    await sendSummary(ctx, { feature: "reset_link", total, success: done, failed, cancelled,
      extra: [`Total Selected: ${total}`, `Success       : ${done}`, `Failed        : ${failed}`],
      boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── APPROVAL TOGGLE ───────────────────────────────────────────────────
  if (feature === "approval") {
    const pm = await startProgress(ctx, uid, `🔀 Toggling approval — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, cancelled = false;
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `🔀 Toggling approval...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        const cur = await withRetry(() => getGroupApprovalStatus(0, g.id)), next = !cur;
        await withRetry(() => setGroupApproval(0, g.id, next));
        done++;
      } catch (err) { failed++; }
      await sleep(D.approvalToggle);
    }
    await sendSummary(ctx, { feature, total, success: done, failed, cancelled,
      extra: [`Total Selected: ${total}`, `Toggle Success : ${done}`, `Toggle Failed  : ${failed}`] });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── APPROVE PENDING ───────────────────────────────────────────────────
  if (feature === "approve_pending") {
    const pm = await startProgress(ctx, uid, `✅ Approving pending — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, totPend = 0, totApproved = 0, cancelled = false;
    const boxLines = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `✅ Approving pending...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        const r = await withRetry(() => approveAllPending(0, g.id), 2, 5000);
        totPend += r.pendingCount; totApproved += r.approved; done++;
        boxLines.push(`${i + 1}. ${g.name} ${r.approved} member add`);
      } catch (err) { failed++; boxLines.push(`${i + 1}. ${g.name}: failed`); }
      await sleep(D.approvePending);
    }
    await sendSummary(ctx, { feature, total, success: done, failed, cancelled,
      extra: [`Total Groups  : ${total}`, `Total Pending : ${totPend}`, `Total Approved: ${totApproved}`],
      boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── MEMBER LIST ───────────────────────────────────────────────────────
  if (feature === "member_list") {
    const pm = await startProgress(ctx, uid, `📋 Counting members — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, grandTotal = 0, cancelled = false;
    const boxLines = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `📋 Member list...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        const members = await withRetry(() => getGroupMembers(0, g.id));
        grandTotal += members.length;
        boxLines.push(`${i + 1} = ${members.length} members`);
        done++;
      } catch { failed++; boxLines.push(`${g.name}\nfailed`); }
      await sleep(D.memberList);
    }
    await sendSummary(ctx, { feature: "member_list", total, success: done, failed, cancelled,
      extra: [`Total Groups  : ${total}`, `Total Members : ${grandTotal}`], boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }

  // ── PENDING LIST ──────────────────────────────────────────────────────
  if (feature === "pending_list") {
    const pm = await startProgress(ctx, uid, `⏳ Fetching pending — ${total} group(s)...\n${bar(0, total)}`);
    let done = 0, failed = 0, grandPending = 0, cancelled = false;
    const boxLines = [];
    for (let i = 0; i < total; i++) {
      if (isCancelled(uid)) { cancelled = true; break; }
      const g = sel[i];
      await editProgress(ctx.chat.id, pm.message_id,
        `⏳ Pending list...\nDone: ${done}/${total}  ❌ ${failed}\n→ ${g.name}\n${bar(i, total)}`);
      try {
        const { list: pending } = await withRetry(() => getGroupPendingRequests(0, g.id));
        grandPending += pending.length;
        boxLines.push(`${i + 1} = ${pending.length} pending`);
        done++;
      } catch { failed++; boxLines.push(`${g.name}\nfailed`); }
      await sleep(D.pendingList);
    }
    await sendSummary(ctx, { feature: "pending_list", total, success: done, failed, cancelled,
      extra: [`Total Groups  : ${total}`, `Total Pending : ${grandPending}`], boxLines });
    updateSession(uid, { featureFlow: null }); return;
  }
}

// ══════════════════════════════════════════════════════════════════════════
// ─── JOIN GROUPS ──────────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

bot.action("join_groups_start", async (ctx) => {
  await ctx.answerCbQuery();
  if (getStatus(0) !== "connected") { await ctx.answerCbQuery("⚠️ WhatsApp not connected!", { show_alert: true }); return; }
  updateSession(ctx.from.id, { joinFlow: { step: "links" }, cancelPending: false });
  await reply(ctx,
    `🔗 *Join Groups*\n━━━━━━━━━━━━━━━━━━━━\n\nSend invite links — one per line:\n\`\`\`\nhttps://chat.whatsapp.com/ABC123\n\`\`\``,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("❌ Cancel", "back_menu")]]) }
  );
});

// ══════════════════════════════════════════════════════════════════════════
// ─── CREATE GROUPS FLOW ───────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

bot.action("create_groups_start", async (ctx) => {
  await ctx.answerCbQuery();
  if (getStatus(0) !== "connected") { await ctx.answerCbQuery("⚠️ WhatsApp not connected!", { show_alert: true }); return; }
  updateSession(ctx.from.id, { groupFlow: defaultGroupFlow() });
  await reply(ctx,
    `➕ *Create Groups — Step 1/9*\n━━━━━━━━━━━━━━━━━━━━\n\n*Group name?*`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("❌ Cancel", "back_menu")]]) }
  );
});

async function askNumbering(ctx) {
  const flow = getSession(ctx.from.id).groupFlow;
  await reply(ctx, `➕ *Create Groups — Step 3/9*\n━━━━━━━━━━━━━━━━━━━━\n\n*Add numbering?*\n\nYes → _${flow.name} 1, ${flow.name} 2..._\nNo  → All named _${flow.name}_`,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("✅ Yes","gf_num_yes"),Markup.button.callback("❌ No","gf_num_no")],[Markup.button.callback("❌ Cancel","back_menu")]]) });
}
bot.action("gf_num_yes",async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,numbering:true,step:"description"}});await askDescription(ctx);});
bot.action("gf_num_no", async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,numbering:false,step:"description"}});await askDescription(ctx);});

async function askDescription(ctx) {
  await reply(ctx,`➕ *Create Groups — Step 4/9*\n━━━━━━━━━━━━━━━━━━━━\n\n*Group description:*\n_Skip to leave empty._`,
    {parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("⏭ Skip","gf_desc_skip")],[Markup.button.callback("❌ Cancel","back_menu")]])});
}
bot.action("gf_desc_skip",async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,description:"",step:"photo"}});await askPhoto(ctx);});

async function askPhoto(ctx) {
  await reply(ctx,`➕ *Create Groups — Step 5/9*\n━━━━━━━━━━━━━━━━━━━━\n\n*Group photo:*\n_Skip for default._`,
    {parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("⏭ Skip","gf_photo_skip")],[Markup.button.callback("❌ Cancel","back_menu")]])});
}
bot.action("gf_photo_skip",async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,photo:null,step:"disappearing"}});await askDisappearing(ctx);});

async function askDisappearing(ctx) {
  await reply(ctx,`➕ *Create Groups — Step 6/9*\n━━━━━━━━━━━━━━━━━━━━\n\n*Disappearing messages:*`,
    {parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("24h","gf_dis_86400"),Markup.button.callback("7 Days","gf_dis_604800"),Markup.button.callback("90 Days","gf_dis_7776000")],[Markup.button.callback("⏭ Off","gf_dis_0")],[Markup.button.callback("❌ Cancel","back_menu")]])});
}
[0,86400,604800,7776000].forEach((s)=>{bot.action(`gf_dis_${s}`,async(ctx)=>{await ctx.answerCbQuery();const ss=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...ss.groupFlow,disappearing:s,step:"members"}});await askMembers(ctx);});});

async function askMembers(ctx) {
  await reply(ctx,`➕ *Create Groups — Step 7/9*\n━━━━━━━━━━━━━━━━━━━━\n\n*Add members? (one number per line)*`,
    {parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("⏭ Skip","gf_mem_skip")],[Markup.button.callback("❌ Cancel","back_menu")]])});
}
bot.action("gf_mem_skip",async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,members:[],makeAdmin:false,step:"permissions"}});await askPermissions(ctx);});

async function askAdmin(ctx) {
  const flow=getSession(ctx.from.id).groupFlow;
  await reply(ctx,`➕ *Create Groups — Step 8/9*\n━━━━━━━━━━━━━━━━━━━━\n\n👥 *${flow.members.length} member(s)* added.\n\n*Make them admin?*`,
    {parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("✅ Yes","gf_admin_yes"),Markup.button.callback("❌ No","gf_admin_no")],[Markup.button.callback("❌ Cancel","back_menu")]])}); 
}
bot.action("gf_admin_yes",async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,makeAdmin:true,step:"permissions"}});await askPermissions(ctx);});
bot.action("gf_admin_no", async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,makeAdmin:false,step:"permissions"}});await askPermissions(ctx);});

function permKb(p) {
  return Markup.inlineKeyboard([
    [Markup.button.callback(`💬 All Can Send   : ${p.sendMessages?"✅ ON":"❌ OFF"}`,   "gf_pt_sendMessages")],
    [Markup.button.callback(`✏️ All Can Edit   : ${p.editInfo?"✅ ON":"❌ OFF"}`,       "gf_pt_editInfo")],
    [Markup.button.callback(`➕ All Can Add    : ${p.addMembers?"✅ ON":"❌ OFF"}`,     "gf_pt_addMembers")],
    [Markup.button.callback(`🔐 Join Approval : ${p.approveMembers?"✅ ON":"❌ OFF"}`, "gf_pt_approveMembers")],
    [Markup.button.callback("💾 Save & Continue","gf_perm_save")],
    [Markup.button.callback("❌ Cancel","back_menu")],
  ]);
}
async function askPermissions(ctx) {
  const p=getSession(ctx.from.id).groupFlow.permissions;
  await reply(ctx,`➕ *Create Groups — Step 9/9*\n━━━━━━━━━━━━━━━━━━━━\n\n*Set permissions:*\n_Tap to toggle, then Save._`,
    {parse_mode:"Markdown",...permKb(p)});
}
["sendMessages","editInfo","addMembers","approveMembers"].forEach((key)=>{
  bot.action(`gf_pt_${key}`,async(ctx)=>{
    await ctx.answerCbQuery();
    const s=getSession(ctx.from.id),p={...s.groupFlow.permissions,[key]:!s.groupFlow.permissions[key]};
    updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,permissions:p}});
    try{await ctx.editMessageReplyMarkup(permKb(p).reply_markup);}catch{await askPermissions(ctx);}
  });
});
bot.action("gf_perm_save",async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,step:"confirm"}});await showConfirm(ctx);});

function fmtDis(s){return !s?"Off":s===86400?"24h":s===604800?"7 Days":s===7776000?"90 Days":`${s}s`;}

async function showConfirm(ctx) {
  const flow=getSession(ctx.from.id).groupFlow,p=flow.permissions;
  const prev=flow.numbering
    ?Array.from({length:Math.min(flow.count,3)},(_,i)=>`${flow.name} ${i+1}`).join(", ")+(flow.count>3?` ...(${flow.count})`:"")
    :`${flow.name} ×${flow.count}`;
  await reply(ctx,
    `✅ *Review — Create Groups*\n━━━━━━━━━━━━━━━━━━━━\n`+
    `Name       : *${flow.name}*\n`+
    `Count      : ${flow.count} groups\n`+
    `Numbering  : ${flow.numbering?"Yes":"No"}\n`+
    `Preview    : _${prev}_\n`+
    `Description: ${flow.description?`_${flow.description.slice(0,40)}_`:"None"}\n`+
    `Photo      : ${flow.photo?"✅ Set":"None"}\n`+
    `Disappear  : ${fmtDis(flow.disappearing)}\n`+
    `Members    : ${flow.members.length||"None"}${flow.members.length?` | Admin: ${flow.makeAdmin?"Yes":"No"}`:""}\n`+
    `━━━━━━━━━━━━━━━━━━━━\n`+
    `All Can Send  : ${p.sendMessages?"✅ ON":"❌ OFF"}\n`+
    `All Can Edit  : ${p.editInfo?"✅ ON":"❌ OFF"}\n`+
    `All Can Add   : ${p.addMembers?"✅ ON":"❌ OFF"}\n`+
    `Join Approval : ${p.approveMembers?"✅ ON":"❌ OFF"}\n`+
    `━━━━━━━━━━━━━━━━━━━━\n_Everything look good?_`,
    {parse_mode:"Markdown",...Markup.inlineKeyboard([
      [Markup.button.callback("✏️ Edit","gf_edit_menu")],
      [Markup.button.callback("🚀 Create Now","gf_create_now")],
      [Markup.button.callback("❌ Cancel","back_menu")],
    ])}
  );
}

bot.action("gf_edit_menu",async(ctx)=>{
  await ctx.answerCbQuery();
  await reply(ctx,`✏️ *What to edit?*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([
    [Markup.button.callback("Name","ge_name"),          Markup.button.callback("Count","ge_count")],
    [Markup.button.callback("Numbering","ge_numbering"),Markup.button.callback("Description","ge_desc")],
    [Markup.button.callback("Photo","ge_photo"),         Markup.button.callback("Disappearing","ge_disappearing")],
    [Markup.button.callback("Members","ge_members"),     Markup.button.callback("Permissions","ge_perms")],
    [Markup.button.callback("🔙 Back to Summary","gf_back_confirm")],
  ])});
});
bot.action("gf_back_confirm",async(ctx)=>{await ctx.answerCbQuery();await showConfirm(ctx);});
bot.action("ge_name",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,step:"name_edit"}});await reply(ctx,`📝 *New name:*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🔙 Cancel","gf_back_confirm")]])});});
bot.action("ge_count",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,step:"count_edit"}});await reply(ctx,`🔢 *New count (1–50):*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🔙 Cancel","gf_back_confirm")]])});});
bot.action("ge_numbering",async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,numbering:!s.groupFlow.numbering,step:"confirm"}});await showConfirm(ctx);});
bot.action("ge_desc",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,step:"description_edit"}});await reply(ctx,`📄 *New description:*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("⏭ Remove","ge_desc_rm")],[Markup.button.callback("🔙 Cancel","gf_back_confirm")]])});});
bot.action("ge_desc_rm",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,description:"",step:"confirm"}});await showConfirm(ctx);});
bot.action("ge_photo",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,step:"photo_edit"}});await reply(ctx,`🖼️ *Send new photo:*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🗑 Remove","ge_photo_rm")],[Markup.button.callback("🔙 Cancel","gf_back_confirm")]])});});
bot.action("ge_photo_rm",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,photo:null,step:"confirm"}});await showConfirm(ctx);});
bot.action("ge_disappearing",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,step:"disappearing_edit"}});await reply(ctx,`⏳ *Disappearing:*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("24h","ge_dis_86400"),Markup.button.callback("7d","ge_dis_604800"),Markup.button.callback("90d","ge_dis_7776000")],[Markup.button.callback("⏭ Off","ge_dis_0")],[Markup.button.callback("🔙 Cancel","gf_back_confirm")]])});});
[0,86400,604800,7776000].forEach((s)=>{bot.action(`ge_dis_${s}`,async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,disappearing:s,step:"confirm"}});await showConfirm(ctx);});});
bot.action("ge_members",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,step:"members_edit"}});await reply(ctx,`👥 *New member numbers (one per line):*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("⏭ Remove All","ge_mem_rm")],[Markup.button.callback("🔙 Cancel","gf_back_confirm")]])});});
bot.action("ge_mem_rm",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,members:[],makeAdmin:false,step:"confirm"}});await showConfirm(ctx);});
bot.action("ge_perms",async(ctx)=>{await ctx.answerCbQuery();updateSession(ctx.from.id,{groupFlow:{...getSession(ctx.from.id).groupFlow,step:"permissions_edit"}});await askPermissions(ctx);});

bot.action("gf_create_now",async(ctx)=>{
  await ctx.answerCbQuery("Starting...");
  const uid=ctx.from.id,flow=getSession(uid).groupFlow;
  if(!flow?.name||!flow?.count){await reply(ctx,"⚠️ Settings incomplete.",Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu","back_menu")]]));return;}
  if(getStatus(0)!=="connected"){await reply(ctx,"❌ WhatsApp not connected!",Markup.inlineKeyboard([[Markup.button.callback("📱 Connect","menu_account")]]));return;}
  const jids=flow.members.map((n)=>`${n.replace(/[^0-9]/g,"")}@s.whatsapp.net`);
  updateSession(uid,{cancelPending:false});
  const pm=await startProgress(ctx,uid,`🚀 Creating ${flow.count} group(s)...\n${bar(0,flow.count)}`);
  const created=[],failed=[];
  let cancelled=false;
  for(let i=0;i<flow.count;i++){
    if(isCancelled(uid)){cancelled=true;break;}
    const gname=flow.numbering?`${flow.name} ${i+1}`:flow.name;
    await editProgress(ctx.chat.id,pm.message_id,`🚀 Creating groups...\nDone: ${i}/${flow.count}\n→ ${gname}\n${bar(i,flow.count)}`);
    try{
      const r=await withRetry(()=>createGroup(0,gname,jids));const gid=r.id;
      await sleep(3000); // give WA time to register the new group
      if(flow.description){await updateGroupDescription(0,gid,flow.description).catch(()=>{});await sleep(600);}
      if(flow.photo){
        // Reconstruct Buffer if session stored it as a plain object (MongoDB round-trip)
        const photoBuf = Buffer.isBuffer(flow.photo) ? flow.photo
          : (flow.photo?.data ? Buffer.from(flow.photo.data) : Buffer.from(Object.values(flow.photo)));
        await withRetry(()=>updateGroupPhoto(0,gid,photoBuf),3,3000).catch(()=>{});
        await sleep(800);
      }
      if(flow.disappearing){await setDisappearingMessages(0,gid,flow.disappearing).catch(()=>{});await sleep(500);}
      if(flow.makeAdmin&&jids.length){await makeAdminByNumbers(0,gid,flow.members).catch(()=>{});await sleep(1000);}
      await setGroupPermissions(0,gid,flow.permissions).catch(()=>{});
      let link="";try{link=await getGroupInviteLink(0,gid);}catch{link="(unavailable)";}
      created.push({name:gname,link});
    }catch(err){failed.push(gname);}
    await sleep(D.createGroup);
  }
  const boxLines=created.map((g)=>`${g.name}\n${g.link}`);
  if(failed.length)failed.forEach((n)=>boxLines.push(`❌ ${n}: failed`));
  await sendSummary(ctx,{feature:"create_groups",total:flow.count,success:created.length,failed:failed.length,cancelled,
    extra:[`Total Count   : ${flow.count}`,`Created       : ${created.length}`,`Failed        : ${failed.length}`],
    boxLines});
  updateSession(uid,{groupFlow:null});
});

[1,5,10,20,50].forEach((n)=>{bot.action(`gf_count_${n}`,async(ctx)=>{await ctx.answerCbQuery();const s=getSession(ctx.from.id);updateSession(ctx.from.id,{groupFlow:{...s.groupFlow,count:n,step:"numbering"}});await askNumbering(ctx);});});

// ══════════════════════════════════════════════════════════════════════════
// ─── ADD MEMBERS — VCF ────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

bot.action("am_mode_onebyone",async(ctx)=>{await ctx.answerCbQuery();const flow=getSession(ctx.from.id).featureFlow;updateSession(ctx.from.id,{featureFlow:{...flow,addMode:"onebyone",step:"am_awaiting_vcf"}});await askNextVcf(ctx);});
bot.action("am_mode_bulk",    async(ctx)=>{await ctx.answerCbQuery();const flow=getSession(ctx.from.id).featureFlow;updateSession(ctx.from.id,{featureFlow:{...flow,addMode:"bulk",step:"am_awaiting_vcf"}});await askNextVcf(ctx);});

async function askNextVcf(ctx) {
  const flow  = getSession(ctx.from.id).featureFlow;
  const idx   = flow.currentVcfIdx || 0;
  const total = (flow.links || []).length;
  if (idx >= total) { await runAddMembersFromVcfs(ctx); return; }
  const code  = flow.links[idx];
  updateSession(ctx.from.id, { awaitingVcf: { feature: "add_members", step: "am_vcf", linkIdx: idx } });
  await reply(ctx,
    `➕ *Add Members — VCF ${idx + 1}/${total}*\n━━━━━━━━━━━━━━━━━━━━\n\nSend VCF for group ${idx + 1}:\n\`https://chat.whatsapp.com/${code}\``,
    { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("⏭ Skip This Group","am_skip_vcf")],[Markup.button.callback("🏠 Main Menu","back_menu")]]) }
  );
}

bot.action("am_skip_vcf",async(ctx)=>{
  await ctx.answerCbQuery("Skipped");
  const uid=ctx.from.id,flow=getSession(uid).featureFlow;
  const newVcfs=[...(flow.vcfs||[])];newVcfs[flow.currentVcfIdx||0]=null;
  updateSession(uid,{featureFlow:{...flow,currentVcfIdx:(flow.currentVcfIdx||0)+1,vcfs:newVcfs},awaitingVcf:null});
  await askNextVcf(ctx);
});

async function runAddMembersFromVcfs(ctx) {
  const uid  = ctx.from.id;
  const flow = getSession(uid).featureFlow;
  const links = flow.links||[], vcfs = flow.vcfs||[], total = links.length;
  updateSession(uid, { cancelPending: false, awaitingVcf: null });
  const pm = await startProgress(ctx, uid, `➕ Adding members — ${total} group(s)...\n${bar(0, total)}`);
  let doneGroups=0, failedGroups=0, totAdded=0, totFailed=0, totSkipped=0, cancelled=false;
  const boxLines = [];
  for (let i=0; i<total; i++) {
    if (isCancelled(uid)) { cancelled=true; break; }
    const vcfEntry = vcfs[i];
    if (!vcfEntry) { doneGroups++; boxLines.push(`Group ${i+1}: skipped (no VCF)`); continue; }
    const contacts = Array.isArray(vcfEntry) ? vcfEntry : (vcfEntry.contacts || []);
    await editProgress(ctx.chat.id, pm.message_id,
      `➕ Adding members...\nGroup: ${i+1}/${total}  Added: ${totAdded}\n→ Group ${i+1}\n${bar(i, total)}`);
    try {
      const info = await withTimeout(withRetry(() => getGroupInfoFromLink(0, links[i]), 2, 1500), 12000, "GetGroupInfo");
      if (!info) throw new Error("Invalid link");
      const result = await addMembersToGroup(0, info.id, contacts.map(c=>c.phone), flow.addMode==="onebyone");
      totAdded+=result.added; totFailed+=result.failed; totSkipped+=result.skipped; doneGroups++;
      boxLines.push(`${info.name}: ${result.added} members added`);
    } catch (err) { failedGroups++; boxLines.push(`❌ Group ${i+1}: failed`); }
    await sleep(D.addMembers);
  }
  await sendSummary(ctx, { feature: "add_members", total, success: doneGroups, failed: failedGroups, cancelled,
    extra: [`Total Groups : ${total}`, `Total Added  : ${totAdded}`, `Total Failed : ${totFailed}`, `Total Skipped: ${totSkipped}`],
    boxLines });
  updateSession(uid, { featureFlow: null, awaitingVcf: null });
}

// ══════════════════════════════════════════════════════════════════════════
// ─── TEXT HANDLER ─────────────────────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

bot.on("text", async (ctx) => {
  const uid = ctx.from.id, s = getSession(uid), text = ctx.message.text.trim();
  if (text.startsWith("/")) return;

  // WA phone input
  if (s.awaitingPhoneForIndex !== null && s.awaitingPhoneForIndex !== undefined) {
    const phone = text.replace(/[^0-9]/g, "");
    if (phone.length < 10) { await ctx.reply(`❌ Invalid number. Example: \`919876543210\``,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu","back_menu")]])}); return; }
    updateSession(uid, { awaitingPhoneForIndex: null });
    const wm = await ctx.reply(`⏳ *Generating pairing code...*`, { parse_mode: "Markdown" });
    pendingPairingCbs.set(0, async (code) => {
      try { await ctx.telegram.deleteMessage(ctx.chat.id, wm.message_id); } catch {}
      if (!code) { await ctx.reply(`❌ *Failed to generate code. Try again.*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🔄 Try Again","menu_account")],[Markup.button.callback("🏠 Main Menu","back_menu")]])}); return; }
      await ctx.reply(
        `🔑 *Pairing Code*\n━━━━━━━━━━━━━━━━━━━━\n\n\`${code}\`\n\n━━━━━━━━━━━━━━━━━━━━\n*How to link:*\n1. Open WhatsApp\n2. Settings → Linked Devices → Link a Device\n3. Tap "Link with phone number"\n4. Enter the code above\n\n⚠️ Expires in *60 seconds*!\n⏳ Waiting for connection...`,
        {parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🔄 New Code","reset_wa")],[Markup.button.callback("🏠 Main Menu","back_menu")]])}
      );
    });
    pendingReadyCbs.set(0, async () => { await sendMainMenu(ctx); });
    connectAccount(0, phone).catch(async (err) => {
      pendingPairingCbs.delete(0); pendingReadyCbs.delete(0);
      await ctx.reply(`❌ Error: \`${err.message}\``,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🏠 Main Menu","back_menu")]])});
    });
    return;
  }

  // Join Groups
  if (s.joinFlow?.step === "links") {
    const codes = extractCodes(text);
    if (!codes.length) { await ctx.reply(`❌ No valid links found.`,{...Markup.inlineKeyboard([[Markup.button.callback("🔙 Try Again","join_groups_start")],[Markup.button.callback("🏠 Main Menu","back_menu")]])}); return; }
    updateSession(uid, { joinFlow: null });
    const pm = await startProgress(ctx, uid, `🔗 Joining ${codes.length} group(s)...\n${bar(0, codes.length)}`);
    let joined=0, failed=0, cancelled=false;
    for (let i=0; i<codes.length; i++) {
      if (isCancelled(uid)) { cancelled=true; break; }
      await editProgress(ctx.chat.id, pm.message_id,
        `🔗 Joining groups...\n✅ ${joined}  ❌ ${failed}\nGroup ${i+1}/${codes.length}\n${bar(i, codes.length)}`);
      try { await withRetry(() => joinGroupViaLink(0, codes[i])); joined++; }
      catch { failed++; }
      await sleep(D.joinGroup);
    }
    await sendSummary(ctx,{feature:"join_groups",total:codes.length,success:joined,failed,cancelled,
      extra:[`Total Links  : ${codes.length}`,`Joined       : ${joined}`,`Failed       : ${failed}`]});
    return;
  }

  // Similar keyword
  if (s.featureFlow?.step === "similar_query") {
    const kw = text.toLowerCase();
    try {
      const allGroups = s.featureFlow.allGroups?.length ? s.featureFlow.allGroups : await getAllGroupsWithDetails(0);
      const filtered  = allGroups.filter((g) => g.name.toLowerCase().includes(kw));
      if (!filtered.length) { await ctx.reply(`❌ No groups match *"${text}"*.`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🔙 Try Again","gs_sim_custom")],[Markup.button.callback("🏠 Main Menu","back_menu")]])}); return; }
      updateSession(uid, { featureFlow: { ...s.featureFlow, allGroups, selectedIds: filtered.map(g=>g.id), keyword: kw, step: "confirm" } });
      await ctx.reply(
        `✅ *${filtered.length} group(s) matched:*\n━━━━━━━━━━━━━━━━━━━━\n${filtered.slice(0,15).map((g,i)=>`${i+1}. ${g.name}`).join("\n")}${filtered.length>15?`\n_...and ${filtered.length-15} more_`:""}`,
        {parse_mode:"Markdown",...Markup.inlineKeyboard([[Markup.button.callback("🚀 Proceed","gs_sim_proceed")],[Markup.button.callback("🏠 Main Menu","back_menu")]]) }
      );
    } catch (err) { await ctx.reply(`❌ Error: ${err.message}`); }
    return;
  }

  // Make Admin numbers
  if (s.featureFlow?.step === "admin_numbers") {
    const nums = text.split(/[\n,\s]+/).map(n=>n.replace(/[^0-9]/g,"")).filter(n=>n.length>=7);
    if (!nums.length) { await ctx.reply("⚠️ No valid numbers found. Include country code."); return; }
    const flow = s.featureFlow;
    updateSession(uid, { featureFlow: { ...flow, adminNumbers: nums, step: "executing" } });
    await runFeature(ctx, flow.feature, flow.selectedIds, flow.allGroups, nums);
    return;
  }

  // Demote Admin numbers
  if (s.featureFlow?.step === "demote_numbers") {
    const nums = text.split(/[\n,\s]+/).map(n=>n.replace(/[^0-9]/g,"")).filter(n=>n.length>=7);
    if (!nums.length) { await ctx.reply("⚠️ No valid numbers found."); return; }
    const flow = s.featureFlow;
    updateSession(uid, { featureFlow: { ...flow, adminNumbers: nums, step: "executing" } });
    await runFeature(ctx, "demote_admin", flow.selectedIds, flow.allGroups, nums);
    return;
  }

  // Auto Accept custom duration
  if (s.featureFlow?.step === "aa_custom_duration") {
    const mins = parseInt(text, 10);
    if (isNaN(mins) || mins < 1) { await ctx.reply("⚠️ Enter valid minutes. Example: `120`", {parse_mode:"Markdown"}); return; }
    const flow = s.featureFlow, secs = mins * 60;
    const label = mins >= 60 ? `${mins/60}h` : `${mins}min`;
    updateSession(uid, { featureFlow: { ...flow, aaDuration: secs, step: "aa_confirm" } });
    await ctx.reply(
      `⏰ *Auto Accept — Confirm*\n━━━━━━━━━━━━━━━━━━━━\nGroups: *${flow.selectedIds.length}*  Duration: *${label}*`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard([[Markup.button.callback("▶️ Start","aa_start")],[Markup.button.callback("🔙 Change","aa_back_duration")],[Markup.button.callback("🏠 Main Menu","back_menu")]]) }
    );
    return;
  }

  // Add Members links
  if (s.featureFlow?.step === "am_links") {
    const codes = extractCodes(text);
    if (!codes.length) { await ctx.reply("❌ No valid links found."); return; }
    updateSession(uid, { featureFlow: { ...s.featureFlow, links: codes, currentVcfIdx: 0, vcfs: [], step: "am_mode" } });
    await ctx.reply(
      `➕ *Add Members — ${codes.length} group(s) found*\n━━━━━━━━━━━━━━━━━━━━\n\n*How to add?*`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard([
        [Markup.button.callback("🐢 One by One (Safe)","am_mode_onebyone")],
        [Markup.button.callback("⚡ Bulk (Fast)",       "am_mode_bulk")],
        [Markup.button.callback("🏠 Main Menu","back_menu")],
      ]) }
    );
    return;
  }

  // Change Name custom: base name
  if (s.featureFlow?.step === "cn_random_name") {
    const name = text.slice(0, 100);
    updateSession(uid, { featureFlow: { ...s.featureFlow, cnBaseName: name, step: "cn_random_numbering" } });
    await ctx.reply(
      `✏️ *Change Name*\n━━━━━━━━━━━━━━━━━━━━\nBase name: *${name}*\n\n*Add numbering?*\nYes → _${name} 1, ${name} 2..._`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard([
        [Markup.button.callback("✅ Yes — add numbers","cn_numbering_yes"),Markup.button.callback("❌ No","cn_numbering_no")],
        [Markup.button.callback("🏠 Main Menu","back_menu")],
      ]) }
    );
    return;
  }

  // Change Name custom: links
  if (s.featureFlow?.step === "cn_random_links") {
    const codes = extractCodes(text);
    if (!codes.length) { await ctx.reply("❌ No valid links found."); return; }
    await runChangeNameRandom(ctx, codes, s.featureFlow.cnBaseName, s.featureFlow.numbering !== false);
    return;
  }

  // CTC Checker links → then collect VCFs (any number), user presses Start when ready
  if (s.featureFlow?.step === "ctc_links") {
    const codes = extractCodes(text);
    if (!codes.length) { await ctx.reply("❌ No valid links found."); return; }
    updateSession(uid, {
      featureFlow: { ...s.featureFlow, links: codes, vcfList: [], ctcVcfIdx: 0, step: "ctc_vcf_collecting" },
      awaitingVcf: { feature: "ctc_checker", step: "ctc_vcf_collecting" },
    });
    await ctx.reply(
      `🔍 *CTC Checker — ${codes.length} group(s)*\n━━━━━━━━━━━━━━━━━━━━\n*Step 2:* Upload VCF files for these groups.\n\n📌 *Order must match groups:*\n• VCF 1 → Group 1\n• VCF 2 → Group 2 …\n\n📎 *Send all ${codes.length} VCF files now, then press Start Check:*`,
      { parse_mode: "Markdown", ...Markup.inlineKeyboard([
        [Markup.button.callback(`▶️ Start Check (0 VCFs uploaded)`, "ctc_start_check")],
        [Markup.button.callback("🏠 Main Menu","back_menu")],
      ]) }
    );
    return;
  }

  // Create Groups steps
  const flow = s.groupFlow;
  if (!flow) { await sendMainMenu(ctx); return; }
  if (flow.step==="name")          { const name=text.slice(0,100); updateSession(uid,{groupFlow:{...flow,name,step:"count"}});await ctx.reply(`➕ *Create Groups — Step 2/9*\n━━━━━━━━━━━━━━━━━━━━\nName: *${name}*\n\n*How many groups? (1–50)*`,{parse_mode:"Markdown",...Markup.inlineKeyboard([[1,5,10,20,50].map(n=>Markup.button.callback(`${n}`,`gf_count_${n}`)),[Markup.button.callback("❌ Cancel","back_menu")]])}); return; }
  if (flow.step==="name_edit")     { updateSession(uid,{groupFlow:{...flow,name:text.slice(0,100),step:"confirm"}});await showConfirm(ctx); return; }
  if (flow.step==="count"||flow.step==="count_edit") {
    const n=parseInt(text,10);
    if(isNaN(n)||n<1||n>50){await ctx.reply("⚠️ Enter a number between 1 and 50.");return;}
    if(flow.step==="count_edit"){updateSession(uid,{groupFlow:{...flow,count:n,step:"confirm"}});await showConfirm(ctx);}
    else{updateSession(uid,{groupFlow:{...flow,count:n,step:"numbering"}});await askNumbering(ctx);}
    return;
  }
  if (flow.step==="description")      { updateSession(uid,{groupFlow:{...flow,description:text.slice(0,512),step:"photo"}});await askPhoto(ctx); return; }
  if (flow.step==="description_edit") { updateSession(uid,{groupFlow:{...flow,description:text.slice(0,512),step:"confirm"}});await showConfirm(ctx); return; }
  if (flow.step==="members"||flow.step==="members_edit") {
    const nums=text.split(/[\n,\s]+/).map(n=>n.replace(/[^0-9]/g,"")).filter(n=>n.length>=10);
    if(!nums.length){await ctx.reply("⚠️ No valid numbers found.");return;}
    if(flow.step==="members_edit"){updateSession(uid,{groupFlow:{...flow,members:nums,step:"confirm"}});await showConfirm(ctx);}
    else{updateSession(uid,{groupFlow:{...flow,members:nums,step:"admin"}});await askAdmin(ctx);}
    return;
  }
  await sendMainMenu(ctx);
});

// ══════════════════════════════════════════════════════════════════════════
// ─── DOCUMENT HANDLER (VCF Files) ─────────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════════

bot.on("document", async (ctx) => {
  const uid = ctx.from.id, s = getSession(uid);
  const doc = ctx.message.document;
  const isVcf = doc.mime_type==="text/vcard"||doc.mime_type==="text/x-vcard"||doc.file_name?.toLowerCase().endsWith(".vcf");
  const awaitingVcf = s.awaitingVcf;

  // ── Change Name VCF — collecting mode (NEW auto-scan flow) ───────────
  if (awaitingVcf?.feature === "change_name" && s.featureFlow?.step === "cn_vcf_collecting") {
    if (!isVcf) { await ctx.reply("⚠️ Please send a .vcf file."); return; }
    try {
      const vcfName    = (doc.file_name || "").replace(/\.vcf$/i, "").trim() || "Unnamed";
      const contacts   = parseVcf((await downloadFile(ctx, doc.file_id)).toString("utf8"));
      if (!contacts.length) { await ctx.reply("⚠️ No valid numbers in VCF."); return; }
      const flow       = s.featureFlow;
      const newVcfList = [...(flow.vcfList || []), { name: vcfName, contacts }];
      updateSession(uid, { featureFlow: { ...flow, vcfList: newVcfList } });
      await showVcfCollectStatus(ctx, newVcfList);
    } catch (err) { await ctx.reply(`❌ VCF read error: ${err.message}`); }
    return;
  }

  // ── Add Members VCF ───────────────────────────────────────────────────
  if (awaitingVcf?.feature === "add_members" && s.featureFlow?.step === "am_awaiting_vcf") {
    if (!isVcf) { await ctx.reply("⚠️ Please send a .vcf file."); return; }
    try {
      const contacts = parseVcf((await downloadFile(ctx, doc.file_id)).toString("utf8"));
      if (!contacts.length) { await ctx.reply("⚠️ No valid numbers in VCF."); return; }
      const flow = s.featureFlow, idx = flow.currentVcfIdx || 0;
      const newVcfs = [...(flow.vcfs || [])];
      newVcfs[idx] = contacts;
      updateSession(uid, { featureFlow: { ...flow, vcfs: newVcfs, currentVcfIdx: idx + 1 }, awaitingVcf: null });
      await ctx.reply(`✅ *VCF received!* ${contacts.length} numbers found.`, { parse_mode: "Markdown" });
      if (idx + 1 >= (flow.links||[]).length) { await runAddMembersFromVcfs(ctx); }
      else { await askNextVcf(ctx); }
    } catch (err) { await ctx.reply(`❌ VCF read error: ${err.message}`); }
    return;
  }

  // ── CTC Checker VCF — accumulate all VCFs, user presses "Start Check" when done ──
  if (awaitingVcf?.feature === "ctc_checker" && s.featureFlow?.step === "ctc_vcf_collecting") {
    if (!isVcf) { await ctx.reply("⚠️ Please send a .vcf file."); return; }
    try {
      const contacts = parseVcf((await downloadFile(ctx, doc.file_id)).toString("utf8"));
      if (!contacts.length) { await ctx.reply("⚠️ No valid numbers in VCF."); return; }
      const flow     = s.featureFlow;
      const groupTotal = (flow.links || []).length;
      const newList  = [...(flow.vcfList || []), { contacts }];
      const received = newList.length;
      // Just accumulate — do NOT auto-start. User presses Start Check button.
      updateSession(uid, { featureFlow: { ...flow, vcfList: newList, ctcVcfIdx: received } });
      await ctx.reply(
        `✅ *VCF ${received} received!* (${contacts.length} numbers)\n📊 VCFs so far: *${received}/${groupTotal}*\n\n📎 Send more VCF files or press *Start Check* when ready:`,
        { parse_mode: "Markdown", ...Markup.inlineKeyboard([
          [Markup.button.callback(`▶️ Start Check (${received} VCF${received > 1 ? "s" : ""} uploaded)`, "ctc_start_check")],
          [Markup.button.callback("🏠 Main Menu","back_menu")],
        ]) }
      );
    } catch (err) { await ctx.reply(`❌ VCF read error: ${err.message}`); }
    return;
  }
});

// ══════════════════════════════════════════════════════════════════════════
// ─── CTC CHECKER — FINAL (LID-aware unknown detection) ────────────────────
//
// Checks pending requests in each group against a trusted VCF.
// Uses triple-fallback matching: JID-level, phone string, numberMatches suffix
// Real unknown numbers → reported with phone numbers
// ══════════════════════════════════════════════════════════════════════════

/**
 * runCtcChecker — NEW FLOW
 * Group[i] pending is checked against vcfList[i] contacts.
 * 1st link = 1st VCF, 2nd link = 2nd VCF, etc.
 * Reports unknown numbers per group.
 */
async function runCtcChecker(ctx) {
  const uid   = ctx.from.id, flow = getSession(uid).featureFlow;
  const links  = flow.links   || [];
  const vcfList = flow.vcfList || [];
  const total  = links.length;
  updateSession(uid, { cancelPending: false });

  const pm = await startProgress(ctx, uid,
    `🔍 CTC Check — ${total} group(s)...\n${bar(0, total)}`);

  let done = 0, failed = 0, cancelled = false;
  const reportLines = [];

  for (let i = 0; i < total; i++) {
    if (isCancelled(uid)) { cancelled = true; break; }
    await editProgress(ctx.chat.id, pm.message_id,
      `🔍 Checking CTC...\nDone: ${done}/${total}  ❌ ${failed}\nGroup ${i + 1}/${total}\n${bar(i, total)}`);

    try {
      const info = await withTimeout(withRetry(() => getGroupInfoFromLink(0, links[i]), 2, 1500), 12000, "GetGroupInfo");
      if (!info) throw new Error("Invalid/expired link");

      // VCF for this group
      const vcfEntry      = vcfList[i] || { contacts: [] };
      const vcfPhones     = (vcfEntry.contacts || []).map((c) => c.phone);

      // Step 1: Get ALL pending JIDs + phone numbers
      //   jids  = Set of every JID string found (both @lid and @s.whatsapp.net)
      //   phones = Set of phone numbers extracted from any @s.whatsapp.net JIDs
      //   count  = number of pending people
      const { jids: pendingJids, phones: pendingPhones, count: pendingCount } =
        await withTimeout(withRetry(() => getPendingRawJids(0, info.id), 2, 1500), 12000, "GetPendingJids");

      // Step 2: Resolve VCF phones via onWhatsApp to get real JIDs + LIDs
      const resolved = vcfPhones.length
        ? await withTimeout(resolveVcfPhones(0, vcfPhones), 20000, "ResolveVcf")
        : [];


      // Build VCF phone set for fallback matching
      const vcfPhoneSet = new Set(vcfPhones.map((p) => String(p).replace(/\D/g, "")));

      // Step 3: TRIPLE FALLBACK MATCHING
      // Method A: JID-level — resolved phoneJid or lid is in pendingJids set
      //           Works when: onWhatsApp returns lid AND pending is @lid format
      //                   OR: pending is @s.whatsapp.net format
      // Method B: Phone string — VCF number is in pendingPhones (from @s.whatsapp.net pending JIDs)
      //           Works when: pending JID is @s.whatsapp.net (older WhatsApp/Baileys)
      // Method C: numberMatches — flexible suffix match between VCF phones and pendingPhones
      //           Works when: country code prefix differs (e.g. "9198..." vs "98...")
      let verifiedCount = 0;
      for (const r of resolved) {
        // Method A
        if (
          (r.phoneJid && pendingJids.has(r.phoneJid)) ||
          (r.lid      && pendingJids.has(r.lid))
        ) { verifiedCount++; continue; }
        // Method B + C (fallback for non-LID pending)
        if (pendingPhones.size > 0) {
          const ph = r.phone;
          if (pendingPhones.has(ph)) { verifiedCount++; continue; }
          // Method C: suffix match
          for (const pp of pendingPhones) {
            if (numberMatches(pp, ph)) { verifiedCount++; break; }
          }
        }
      }
      // Also check: VCF phones directly against pendingPhones (Method B for non-resolved)
      if (verifiedCount === 0 && pendingPhones.size > 0 && vcfPhoneSet.size > 0) {
        for (const vph of vcfPhoneSet) {
          if (pendingPhones.has(vph)) { verifiedCount++; continue; }
          for (const pp of pendingPhones) {
            if (numberMatches(pp, vph)) { verifiedCount++; break; }
          }
        }
      }

      const unknownCount = Math.max(0, pendingCount - verifiedCount);

      done++;

      // Build box line: GroupName: valid X member, Y wrong contact number
      const wrongCount = unknownCount;
      reportLines.push(`${info.name}: valid ${verifiedCount} member, ${wrongCount} wrong contact number`);

    } catch (err) { failed++; reportLines.push(`❌ Group ${i + 1}: ${err.message}`); }

    await sleep(D.ctcCheck);
  }

  await sendSummary(ctx, { feature: "ctc_checker", total, success: done, failed, cancelled,
    extra: [`Total Groups : ${total}`, `Checked      : ${done}`, `Failed       : ${failed}`],
    boxLines: reportLines });
  updateSession(uid, { featureFlow: null, awaitingVcf: null });
}

// ─── Photo Handler ────────────────────────────────────────────────────────
bot.on("photo", async (ctx) => {
  const uid  = ctx.from.id, flow = getSession(uid).groupFlow;
  if (!flow || (flow.step !== "photo" && flow.step !== "photo_edit")) return;
  try {
    const p  = ctx.message.photo[ctx.message.photo.length - 1];
    const u  = await ctx.telegram.getFileLink(p.file_id);
    const r  = await fetch(u.href);
    const buf = Buffer.from(await r.arrayBuffer());
    const ns  = flow.step === "photo_edit" ? "confirm" : "disappearing";
    updateSession(uid, { groupFlow: { ...flow, photo: buf, step: ns } });
    await ctx.reply("✅ *Photo saved!*", { parse_mode: "Markdown" });
    if (ns === "confirm") await showConfirm(ctx); else await askDisappearing(ctx);
  } catch { await ctx.reply("❌ Could not save photo. Please try again."); }
});

bot.catch((err) => console.error("[Bot Error]", err.message));

// ─── Health server ─────────────────────────────────────────────────────────
const app = express(), PORT = process.env.PORT || 3000;
app.get("/", (_, res) => res.send(`<html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#111;color:#fff"><h2>✅ WA Group Manager Bot</h2><p style="color:#4ade80">Running 🟢</p><p>WA: ${getConnectedCount()>0?"Connected ✅":"Disconnected ❌"}</p></body></html>`));
app.get("/health", (_, res) => res.json({ status:"ok", whatsapp:getStatus(0), phone:getPhone(0)||null, ts:new Date().toISOString() }));
app.listen(PORT, () => console.log(`HTTP server on port ${PORT}`));

function selfPing() {
  const url = process.env.RENDER_EXTERNAL_URL || process.env.SELF_URL; if (!url) return;
  const full = url.startsWith("http") ? url : `https://${url}`;
  (full.startsWith("https") ? https : http).get(`${full}/health`, r => console.log(`[Ping] ${r.statusCode}`)).on("error", e => console.error("[Ping]", e.message));
}
setTimeout(() => { selfPing(); setInterval(selfPing, 120000); }, 60000);

async function main() {
  await connectDB();
  await reconnectSavedAccounts();
  await bot.launch({ dropPendingUpdates: true });
  console.log(`WA Group Manager Bot running — Owner: ${OWNER_ID || "NOT SET"}`);
}
main().catch(err => { console.error("Fatal:", err.message); process.exit(1); });
process.once("SIGINT",  () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));
