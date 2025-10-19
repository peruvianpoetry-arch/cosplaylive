import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Variables de entorno ---
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "5000"))

# --- Flask (para que Render vea un puerto abierto) ---
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ Cosplaylive bot está corriendo"

# --- Handlers del bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot activo y funcionando correctamente!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📨 Recibido: {update.message.text}")

def run_bot():
    if not TOKEN:
        print("⚠️ Falta TELEGRAM_TOKEN en Environment.")
        return
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    print("🤖 Iniciando bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Bot en hilo aparte
    threading.Thread(target=run_bot, daemon=True).start()
    # Web en el hilo principal (Render necesita un puerto abierto)
    web.run(host="0.0.0.0", port=PORT)
 os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print("✅ Bot iniciado con éxito")
    app.run_polling()

if __name__ == "__main__":
    main()


