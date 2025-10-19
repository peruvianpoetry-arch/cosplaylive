import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "5000"))

# --- Flask para mantener un puerto abierto en Render ---
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ Cosplaylive bot está corriendo"

def run_web():
    web.run(host="0.0.0.0", port=PORT)

# --- Handlers del bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📨 Recibido: {update.message.text}")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    # 1) Iniciar Flask en segundo plano
    threading.Thread(target=run_web, daemon=True).start()

    # 2) Iniciar el bot en el hilo principal (evita error de event loop)
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    print("🤖 Iniciando bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
