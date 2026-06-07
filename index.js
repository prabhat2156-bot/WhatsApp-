const TelegramBot = require("node-telegram-bot-api");
const axios = require("axios");
const chalk = require("chalk");
const figlet = require("figlet");
const moment = require("moment");

const TOKEN = "YOUR_BOT_TOKEN";

const bot = new TelegramBot(TOKEN, { polling: true });

console.log(
  chalk.green(
    figlet.textSync("TG BOT")
  )
);

bot.onText(/\/start/, (msg) => {
  bot.sendMessage(
    msg.chat.id,
    `🤖 Welcome ${msg.from.first_name}

Commands:
/ping - Check bot
/time - Current time
/quote - Random quote`
  );
});

bot.onText(/\/ping/, (msg) => {
  bot.sendMessage(msg.chat.id, "🏓 Pong!");
});

bot.onText(/\/time/, (msg) => {
  bot.sendMessage(
    msg.chat.id,
    `🕒 ${moment().format("YYYY-MM-DD HH:mm:ss")}`
  );
});

bot.onText(/\/quote/, async (msg) => {
  try {
    const res = await axios.get("https://api.github.com/zen");
    bot.sendMessage(msg.chat.id, `💬 ${res.data}`);
  } catch {
    bot.sendMessage(msg.chat.id, "❌ Failed to fetch quote.");
  }
});

bot.on("polling_error", console.log);

console.log(chalk.cyan("Bot Started Successfully"));
