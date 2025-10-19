import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# --- ENV ---
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "5000"))

# --- Flask (para que Render vea un puerto abierto) ---
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ Cosplaylive bot está corriendo"

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📨 Recibido: {update.message.text}")

def run_bot():
    if not TOKEN:
        print("⚠️ Falta TELEGRAM_TOKEN en Environment.")
        return
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    print("🤖 Iniciando bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Bot en segundo plano
    threading.Thread(target=run_bot, daemon=True).start()
    # Web para Render (evita el “no open ports detected”)
    web.run(host="0.0.0.0", port=PORT)


