/**
 * MongoDB-backed Baileys auth state.
 * Replaces useMultiFileAuthState — no file system needed.
 */
const {
  initAuthCreds,
  BufferJSON,
  proto,
  makeCacheableSignalKeyStore,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const { AuthState } = require("./models");

const logger = pino({ level: "silent" });

async function _read(accountId, key) {
  try {
    const doc = await AuthState.findOne({ accountId, type: key }).lean();
    return doc ? JSON.parse(doc.data, BufferJSON.reviver) : null;
  } catch {
    return null;
  }
}

async function _write(accountId, key, data) {
  await AuthState.findOneAndUpdate(
    { accountId, type: key },
    { accountId, type: key, data: JSON.stringify(data, BufferJSON.replacer) },
    { upsert: true, new: true }
  );
}

async function _delete(accountId, key) {
  await AuthState.deleteOne({ accountId, type: key });
}

async function useMongoAuthState(accountId) {
  const savedCreds = await _read(accountId, "creds");
  const creds = savedCreds || initAuthCreds();

  const keys = {
    get: async (type, ids) => {
      const result = {};
      await Promise.all(
        ids.map(async (id) => {
          let value = await _read(accountId, `${type}__${id}`);
          if (type === "app-state-sync-key" && value) {
            value = proto.Message.AppStateSyncKeyData.fromObject(value);
          }
          result[id] = value ?? undefined;
        })
      );
      return result;
    },
    set: async (data) => {
      const tasks = [];
      for (const category of Object.keys(data)) {
        for (const id of Object.keys(data[category])) {
          const value = data[category][id];
          const key = `${category}__${id}`;
          if (value) {
            tasks.push(_write(accountId, key, value));
          } else {
            tasks.push(_delete(accountId, key));
          }
        }
      }
      await Promise.all(tasks);
    },
  };

  return {
    state: {
      creds,
      keys: makeCacheableSignalKeyStore(keys, logger),
    },
    saveCreds: async () => {
      await _write(accountId, "creds", creds);
    },
  };
}

async function clearMongoAuth(accountId) {
  await AuthState.deleteMany({ accountId });
}

async function hasMongoAuth(accountId) {
  const doc = await AuthState.findOne({ accountId, type: "creds" }).lean();
  return !!doc;
}

module.exports = { useMongoAuthState, clearMongoAuth, hasMongoAuth };
