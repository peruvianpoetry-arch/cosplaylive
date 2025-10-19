import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# --- Variables de entorno ---
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "5000"))

# --- Flask: mantiene el servicio vivo en Render ---
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot está corriendo"

def start_web():
    web.run(host="0.0.0.0", port=PORT)

# --- Handlers del bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📩 Recibido: {update.message.text}")

# 🟢 Captura mensajes de canal
async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post:
        text = update.channel_post.text or "(mensaje vacío)"
        print(f"📢 Mensaje del canal: {text}")

# --- Hilo Flask + Bot ---
def start_bot():
    app_tg = ApplicationBuilder().token(TOKEN).build()

    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))  # aquí el cambio
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    app_tg.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    threading.Thread(target=start_web).start()
    start_bot()
