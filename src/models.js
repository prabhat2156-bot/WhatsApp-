const mongoose = require("mongoose");
const { Schema } = mongoose;

const AuthStateSchema = new Schema(
  {
    accountId: { type: String, required: true },
    type:      { type: String, required: true },
    data:      { type: String, required: true },
  },
  { collection: "auth_states" }
);
AuthStateSchema.index({ accountId: 1, type: 1 }, { unique: true });

const AccountInfoSchema = new Schema(
  {
    accountIndex:   { type: Number, required: true, unique: true },
    telegramUserId: { type: Number, default: 0, index: true },
    phoneNumber:    { type: String, default: "" },
    hasAuth:        { type: Boolean, default: false },
  },
  { collection: "account_infos" }
);

// Tracks all registered users + their index mapping
const UserRegistrySchema = new Schema(
  {
    telegramUserId: { type: Number, required: true, unique: true },
    accountIndex:   { type: Number, required: true },
    registeredAt:   { type: Date, default: Date.now },
  },
  { collection: "user_registry" }
);

const AuthState    = mongoose.model("AuthState", AuthStateSchema);
const AccountInfo  = mongoose.model("AccountInfo", AccountInfoSchema);
const UserRegistry = mongoose.model("UserRegistry", UserRegistrySchema);

module.exports = { AuthState, AccountInfo, UserRegistry };
