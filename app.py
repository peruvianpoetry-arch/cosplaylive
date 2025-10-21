import os
import threading
import asyncio
import logging
from flask import Flask

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

# -------- LOGGING --------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cosplaylive")

# -------- ENV --------
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# -------- FLASK (mantiene vivo el servicio web de Render) --------
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot está corriendo"

def run_web():
    # importante: no usar reloader para evitar 2 procesos
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# -------- HANDLERS DEL BOT --------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    log.info(f"[DM] /start de {user}")
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt  = update.message.text or ""
    log.info(f"[DM] {user}: {txt}")
    await update.message.reply_text(f"📨 {txt}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.text:
        canal = update.effective_chat.title or "Canal"
        txt   = update.channel_post.text
        log.info(f"[CANAL] {canal}: {txt}")

# -------- MAIN ASYNC (estable en Py 3.10) --------
async def main():
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    # 1) Levanta Flask en segundo plano
    threading.Thread(target=run_web, daemon=True).start()

    # 2) Construye aplicación PTB
    app = ApplicationBuilder().token(TOKEN).build()

    # 3) Limpia webhook para evitar conflictos con polling
    await app.bot.delete_webhook(drop_pending_updates=True)

    # 4) Registra handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))

    log.info("🤖 Iniciando bot (polling)…")

    # 5) Secuencia recomendada PTB 20.x
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await app.updater.idle()

if __name__ == "__main__":
    asyncio.run(main())
