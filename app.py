import os
import threading
import logging
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cosplaylive")

# ---------- ENV ----------
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# ---------- FLASK (para que Render vea un puerto) ----------
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot está corriendo"

def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ---------- HANDLERS ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    log.info(f"[DM] /start de {user}")
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt = update.message.text or ""
    log.info(f"[DM] {user}: {txt}")
    await update.message.reply_text(f"📨 {txt}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.text:
        canal = update.effective_chat.title or "Canal"
        txt = update.channel_post.text
        log.info(f"[CANAL] {canal}: {txt}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("❌ Handler error", exc_info=context.error)

# ---------- MAIN (polling SÍNCRONO, sin asyncio.run) ----------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    # 1) Levanta Flask en segundo plano
    threading.Thread(target=run_web, daemon=True).start()

    # 2) Construye la app de Telegram
    app = ApplicationBuilder().token(TOKEN).build()

    # 3) Borra webhook para evitar conflictos con polling
    try:
        import asyncio
        asyncio.run(app.bot.delete_webhook(drop_pending_updates=True))
    except Exception as e:
        log.warning(f"No se pudo borrar webhook (seguimos): {e}")

    # 4) Registra handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))
    app.add_error_handler(on_error)

    log.info("🤖 Iniciando bot (polling síncrono)…")

    # 5) Arranca polling síncrono (no cierra loops, no señales raras)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=None
    )
