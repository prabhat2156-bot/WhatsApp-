const mongoose = require("mongoose");

let connected = false;

async function connectDB() {
  if (connected) return;
  const uri = process.env.MONGODB_URI;
  if (!uri) throw new Error("❌ MONGODB_URI environment variable not set!");
  await mongoose.connect(uri, { serverSelectionTimeoutMS: 15000 });
  connected = true;
  console.log("✅ MongoDB connected");
}

module.exports = { connectDB };
