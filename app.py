# app.py — CosplayLive (bot activo + marketing automático + overlay SSE)

import os, sys, logging, queue, threading
from flask import Flask, Response
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ===== Traducción opcional (si está instalado) =====
try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

# ===== Logging consistente para Render =====
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

# IMPORTANTE: debe iniciar con @
CHANNEL_TARGET = (os.getenv("CHANNEL_USERNAME") or "").strip()   # p.ej. @cosplay_ema_live
DONATION_LINK  = (os.getenv("DONATION_LINK")  or "").strip()     # Stripe Checkout
AUTO_INTERVAL  = int(os.getenv("AUTO_INTERVAL_MIN", "45"))       # minutos
BANNER_URL     = (os.getenv("BANNER_URL")     or "").strip()     # imagen opcional

# ===== Overlay (SSE) =====
events: "queue.Queue[str]" = queue.Queue(maxsize=200)

def push_event(text: str) -> None:
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return
    try:
        events.put_nowait(text)
    except queue.Full:
        try:
            events.get_nowait()
        except queue.Empty:
            pass
        events.put_nowait(text)

web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot activo"

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

# ===== Utilidades =====
def pretty_name(update: Update) -> str:
    u = update.effective_user
    if u and (u.full_name or u.username):
        return u.full_name or f"@{u.username}"
    ch = update.effective_chat
    if ch and (ch.title or ch.username):
        return ch.title or f"@{ch.username}"
    return "Usuario"

def donation_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💳 Donar / Pedido", url=DONATION_LINK or "https://example.com")],
        [
            InlineKeyboardButton("💃 Baile 3€",   callback_data="p_3"),
            InlineKeyboardButton("👙 Topless 5€", callback_data="p_5"),
        ],
        [
            InlineKeyboardButton("🧵 Lencería 10€",   callback_data="p_10"),
            InlineKeyboardButton("🎯 Meta grupal 50€", callback_data="p_50"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)

# ===== Handlers =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = pretty_name(update)
    txt = ("👋 ¡Hola, {n}!\n\n"
           "Apoya el show o haz tu pedido con los botones. "
           "Cada aporte suma para la *meta grupal de 50€* 🔥").format(n=nombre)
    await update.effective_message.reply_text(
        txt, reply_markup=donation_keyboard(), parse_mode="Markdown"
    )
    push_event(f"🟢 {nombre} inició chat con el bot")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text or ""
    nombre = pretty_name(update)

    # marketing si detecta keywords
    if any(k in text.lower() for k in ["precio", "precios", "donar", "donate", "spenden"]):
        await msg.reply_text("💋 Aquí tienes:", reply_markup=donation_keyboard())
    else:
        # responde mostrando traducción al ES si es posible
        reply = f"💬 {nombre}: {text}"
        if GoogleTranslator:
            try:
                es = GoogleTranslator(source='auto', target='es').translate(text)
                if es and es.strip() and es.strip().lower() != text.strip().lower():
                    reply += f"\n🌐 (ES) {es}"
            except Exception:
                pass
        await msg.reply_text(reply)
    push_event(f"💬 {nombre}: {text}")

# Publicaciones en el canal (texto o captions)
async def channel_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.effective_message
    contenido = post.text or post.caption or ""
    push_event(f"📢 [CANAL] {contenido}")

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("💳 Abre la ventana de pago:", reply_markup=donation_keyboard())

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("⚠️ Error en handler", exc_info=context.error)

# ===== Anuncios automáticos al canal =====
async def auto_announce(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_TARGET:
        return
    texto = (
        "💋 *Apoya el show con una donación o propina*\n"
        "Cada aporte acerca la *meta grupal de 50€* 🔥\n"
        "Gracias por tu apoyo 💖"
    )
    try:
        if BANNER_URL:
            await context.bot.send_photo(
                chat_id=CHANNEL_TARGET,
                photo=BANNER_URL,
                caption=texto,
                reply_markup=donation_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_TARGET,
                text=texto,
                reply_markup=donation_keyboard(),
                parse_mode="Markdown",
            )
        push_event("📣 Mensaje automático enviado al canal")
    except Exception as e:
        log.error(f"Error enviando anuncio: {e}")

# ===== Main =====
def main():
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN")

    # Flask para mantener vivo + overlay
    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    # /start y mensajes privados/grupos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))

    # Posts del canal (no existe ChannelPostHandler en PTB 20.x)
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION),
        channel_text
    ))

    # Botones
    app.add_handler(CallbackQueryHandler(on_cb))

    # Errores
    app.add_error_handler(on_error)

    # Job de marketing cada AUTO_INTERVAL minutos
    if CHANNEL_TARGET:
        app.job_queue.run_repeating(
            auto_announce,
            interval=AUTO_INTERVAL * 60,
            first=15  # primer anuncio a los 15s del arranque
        )
        log.info(f"⏱️ Anuncios automáticos cada {AUTO_INTERVAL} min en {CHANNEL_TARGET}")

    log.info("🚀 Bot activo permanente")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
