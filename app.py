# app.py — CosplayLive (canal + anuncios + botones + overlay)

import os, sys, logging, queue, threading
from flask import Flask, Response
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ====== Logging ======
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ====== ENV ======
TOKEN  = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT   = int(os.getenv("PORT", "10000"))
# Canal (privado con ID o público con @username)
CHANNEL_ID_ENV = (os.getenv("CHANNEL_ID") or "").strip()
CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "").strip()  # ej. @cosplay_ema_live
DONATION_LINK = (os.getenv("DONATION_LINK") or "https://example.com").strip()
AUTO_INTERVAL = int(os.getenv("AUTO_INTERVAL_MIN", "5"))  # prueba: 5 min
BANNER_URL = (os.getenv("BANNER_URL") or "").strip()

# ====== Overlay SSE ======
events: "queue.Queue[str]" = queue.Queue(maxsize=200)

def push_event(text: str) -> None:
    t = (text or "").replace("\n", " ").strip()
    if not t: return
    try:
        events.put_nowait(t)
    except queue.Full:
        try: events.get_nowait()
        except queue.Empty: pass
        events.put_nowait(t)

web = Flask(__name__)

@web.get("/")
def home(): return "✅ CosplayLive bot activo"

@web.get("/events")
def sse():
    def stream():
        while True:
            yield f"data: {events.get()}\n\n"
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","Connection":"keep-alive"})

def run_web(): web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ====== Util ======
def donation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Donar / Pedido", url=DONATION_LINK)],
        [InlineKeyboardButton("💃 Baile 3€", callback_data="p_3"),
         InlineKeyboardButton("👙 Topless 5€", callback_data="p_5")],
        [InlineKeyboardButton("🧵 Lencería 10€", callback_data="p_10"),
         InlineKeyboardButton("🎯 Meta grupal 50€", callback_data="p_50")],
    ])

async def resolve_channel_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Devuelve chat_id del canal (int negativo) o None si no puede."""
    # Prioridad: CHANNEL_ID si viene directo
    if CHANNEL_ID_ENV:
        try:
            cid = int(CHANNEL_ID_ENV)
            log.info(f"Canal por CHANNEL_ID: {cid}")
            return cid
        except Exception as e:
            log.error(f"CHANNEL_ID inválido: {CHANNEL_ID_ENV} -> {e}")

    # Si tenemos @username, que sea público y el bot debe ser admin
    if CHANNEL_USERNAME.startswith("@"):
        try:
            chat = await context.bot.get_chat(CHANNEL_USERNAME)
            log.info(f"Resuelto {CHANNEL_USERNAME} -> chat_id {chat.id}")
            return chat.id
        except Exception as e:
            log.error(f"No pude resolver {CHANNEL_USERNAME}. ¿Es público y el bot es admin? {e}")
            return None
    log.error("No se proporcionó CHANNEL_ID ni CHANNEL_USERNAME.")
    return None

# ====== Handlers usuario ======
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🤖 ¡Hola! Aquí tienes los botones 👇",
        reply_markup=donation_keyboard()
    )

async def donate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "💖 Apoya el show o haz tu pedido:",
        reply_markup=donation_keyboard()
    )

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    txt = update.effective_message.text or ""
    await update.effective_message.reply_text(f"💬 {u.full_name if u else 'Usuario'}: {txt}")

# Publicaciones/captions del canal
async def channel_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    texto = msg.text or msg.caption or ""
    push_event(f"📢 [CANAL] {texto}")

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("ℹ️ Abre la ventana de pago")
    await update.callback_query.message.reply_text(
        "💳 Selecciona tu opción:", reply_markup=donation_keyboard()
    )

async def auto_announce(context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.chat_data.get("channel_id")
    if not channel_id:
        channel_id = await resolve_channel_id(context)
        if not channel_id: return
        context.chat_data["channel_id"] = channel_id

    texto = ("💋 *Apoya el show con una donación o propina*\n"
             "Cada aporte acerca la *meta grupal de 50€* 🔥\n"
             "Gracias por tu apoyo 💖")
    try:
        if BANNER_URL:
            await context.bot.send_photo(channel_id, BANNER_URL, caption=texto,
                                         reply_markup=donation_keyboard(), parse_mode="Markdown")
        else:
            await context.bot.send_message(channel_id, texto,
                                           reply_markup=donation_keyboard(), parse_mode="Markdown")
        log.info("📣 Anuncio automático enviado al canal")
        push_event("📣 Anuncio automático enviado al canal")
    except Exception as e:
        log.error(f"Error anunciando en canal: {e}")

async def on_startup(app):
    log.info("⏳ Resolviendo canal y enviando anuncio de prueba…")
    try:
        # usamos job queue con first=5s para anuncio inicial
        app.job_queue.run_once(lambda ctx: auto_announce(ctx), when=5)
    except Exception as e:
        log.error(f"Startup error: {e}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("⚠️ Handler error", exc_info=context.error)

# ====== Main ======
def main():
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN")

    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

    # comandos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("donate", donate_cmd))

    # mensajes normales (MD/Grupos)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))

    # publicaciones del canal (texto o captions)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION),
                                   channel_text))

    # botones
    app.add_handler(CallbackQueryHandler(on_cb))

    # anuncios automáticos cada X minutos
    app.job_queue.run_repeating(auto_announce, interval=AUTO_INTERVAL*60, first=30)

    app.add_error_handler(on_error)
    log.info(f"🚀 Bot activo. Anuncios cada {AUTO_INTERVAL} min.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
