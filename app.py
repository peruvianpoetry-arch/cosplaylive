# app.py — CosplayLive (bot activo + anuncios visuales + overlay)

import os, sys, logging, queue, threading, time
from typing import Optional
from flask import Flask, Response
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ChannelPostHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ===== Traducción opcional =====
try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

# ===== Logging =====
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ===== ENV =====
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
CHANNEL_TARGET = os.getenv("CHANNEL_USERNAME", "").strip()  # ej. @cosplay_emma_live
DONATION_LINK = os.getenv("DONATION_LINK", "").strip()
AUTO_INTERVAL = int(os.getenv("AUTO_INTERVAL_MIN", "45"))  # cada 45 min por defecto
BANNER_URL = os.getenv("BANNER_URL", "").strip()  # imagen opcional para anuncios

# ===== Overlay (SSE) =====
events: "queue.Queue[str]" = queue.Queue(maxsize=200)

def push_event(text: str) -> None:
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return
    try:
        events.put_nowait(text)
    except queue.Full:
        try: events.get_nowait()
        except queue.Empty: pass
        events.put_nowait(text)

web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot activo y en modo permanente"

@web.get("/events")
def sse():
    def stream():
        while True:
            msg = events.get()
            yield f"data: {msg}\n\n"
    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)

def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ===== Utilidad =====
def safe_name(update: Update) -> str:
    u = update.effective_user
    if u and (u.full_name or u.username):
        return u.full_name or f"@{u.username}"
    ch = update.effective_chat
    if ch and (ch.title or ch.username):
        return ch.title or f"@{ch.username}"
    return "Usuario"

def donation_keyboard():
    buttons = [
        [InlineKeyboardButton("💳 Donar / Pedido", url=DONATION_LINK)],
        [
            InlineKeyboardButton("💃 Baile 3€", callback_data="3"),
            InlineKeyboardButton("👙 Topless 5€", callback_data="5"),
        ],
        [
            InlineKeyboardButton("🧵 Lencería 10€", callback_data="10"),
            InlineKeyboardButton("🎯 Meta grupal 50€", callback_data="50"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)

# ===== Handlers =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = safe_name(update)
    text = f"👋 Hola {name}! Soy el asistente del canal.\n\nPulsa el botón para donar o ver los precios."
    await update.effective_message.reply_text(text, reply_markup=donation_keyboard())
    push_event(f"🟢 {name} ha iniciado chat con el bot")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text or ""
    name = safe_name(update)

    # Detectar palabras clave
    if any(k in text.lower() for k in ["precio", "precios", "donar", "spenden", "donate"]):
        await msg.reply_text("💋 Apoya el show o haz tu pedido:", reply_markup=donation_keyboard())
    else:
        reply = f"💬 {name}: {text}"
        if GoogleTranslator:
            try:
                es = GoogleTranslator(source='auto', target='es').translate(text)
                if es and es.strip().lower() != text.strip().lower():
                    reply += f"\n🌐 (ES) {es}"
            except Exception:
                pass
        await msg.reply_text(reply)
    push_event(f"💬 {name}: {text}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    txt = msg.text or msg.caption or ""
    ch = safe_name(update)
    push_event(f"📢 [{ch}] {txt}")

async def cb_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("💳 Abre la ventana de pago:", reply_markup=donation_keyboard())

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("⚠️ Error en handler", exc_info=context.error)

# ===== Anuncio automático =====
async def periodic_announce(app):
    while True:
        try:
            if CHANNEL_TARGET:
                text = (
                    "💋 *Apoya el show con una donación o propina*\n"
                    "Cada aporte acerca la *meta grupal de 50€* 🔥\n"
                    "Gracias por tu apoyo 💖"
                )
                if BANNER_URL:
                    await app.bot.send_photo(
                        chat_id=CHANNEL_TARGET,
                        photo=BANNER_URL,
                        caption=text,
                        reply_markup=donation_keyboard(),
                        parse_mode="Markdown",
                    )
                else:
                    await app.bot.send_message(
                        chat_id=CHANNEL_TARGET,
                        text=text,
                        reply_markup=donation_keyboard(),
                        parse_mode="Markdown",
                    )
                push_event("📣 Mensaje automático enviado al canal")
            await asyncio.sleep(AUTO_INTERVAL * 60)
        except Exception as e:
            log.error(f"Error en auto_announce: {e}")
            await asyncio.sleep(60)

# ===== Main =====
def main():
    import asyncio
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN")

    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))
    app.add_handler(ChannelPostHandler(channel_post))
    app.add_handler(CallbackQueryHandler(cb_query))
    app.add_error_handler(on_error)

    # Lanzar tarea en segundo plano
    app.job_queue.run_once(lambda _: asyncio.create_task(periodic_announce(app)), when=10)

    log.info(f"🚀 Bot activo permanente (cada {AUTO_INTERVAL} min envía anuncios)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
