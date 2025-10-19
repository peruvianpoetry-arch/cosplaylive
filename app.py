import os
import time
import json
import threading
from collections import deque

from flask import Flask, Response, send_from_directory, stream_with_context

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChannelPostHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV VARIABLES
# =========================
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# Traducción (opcional)
ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION", "0") == "1"
TARGET_LANG = os.getenv("TRANSLATE_TO", "de")

# Stripe (opcional)
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
SUCCESS_URL = os.getenv("SUCCESS_URL", "https://example.com/success")
CANCEL_URL = os.getenv("CANCEL_URL", "https://example.com/cancel")

# =========================
# TRADUCCIÓN (deep-translator)
# =========================
translator = None
if ENABLE_TRANSLATION:
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source="auto", target=TARGET_LANG)
        print(f"🌐 Traducción habilitada → {TARGET_LANG}")
    except Exception as e:
        print(f"⚠️ No se pudo habilitar traducción: {e}")
        translator = None

def maybe_translate(text: str) -> str:
    if translator:
        try:
            return translator.translate(text)
        except Exception:
            return text
    return text

# =========================
# STRIPE (opcional)
# =========================
stripe = None
if STRIPE_API_KEY:
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = STRIPE_API_KEY
        stripe = stripe_lib
        print("💳 Stripe habilitado (modo prueba si usas sk_test_...).")
    except Exception as e:
        print(f"⚠️ No se pudo habilitar Stripe: {e}")
        stripe = None

# =========================
# FLASK (mantener web viva para Render + overlay)
# =========================
web = Flask(__name__)
BUFFER = deque(maxlen=50)  # últimos mensajes para overlay

@web.get("/")
def home():
    return "✅ Cosplaylive bot está corriendo"

@web.get("/overlay")
def overlay_page():
    # Sirve el archivo overlay.html desde la raíz del repo
    return send_from_directory(".", "overlay.html")

@web.get("/events")
def events():
    @stream_with_context
    def gen():
        last = 0
        while True:
            if last < len(BUFFER):
                user, text = BUFFER[last]
                last += 1
                yield f"data: {json.dumps({'user': user, 'text': text})}\n\n"
            else:
                time.sleep(0.4)
    return Response(gen(), mimetype="text/event-stream")

def push_to_overlay(user: str, text: str):
    BUFFER.append((user, text))

def run_web():
    # MUY IMPORTANTE: sin reloader para evitar doble proceso
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# =========================
# HANDLERS DEL BOT
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    msg = "🤖 ¡Bot activo y funcionando correctamente!"
    push_to_overlay(user, msg)
    await update.message.reply_text(msg)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    text = update.message.text or ""
    out = maybe_translate(text)
    push_to_overlay(user, out)
    await update.message.reply_text(f"📨 {out}")

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.text:
        canal = update.effective_chat.title or "Canal"
        text = update.channel_post.text
        out = maybe_translate(text)
        push_to_overlay(canal, out)
        # Si quieres que el bot conteste en el canal, podrías habilitar:
        # await context.bot.send_message(chat_id=update.channel_post.chat_id, text=f"🧵 (canal) {out}")

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /donar → crea sesión de pago """
    if not stripe:
        await update.message.reply_text("⚠️ Pagos no habilitados.")
        return
    amount = 500  # 5.00 USD por defecto
    # Si el usuario escribe /donar 10, tomar 10 USD
    if context.args:
        try:
            amount = int(float(context.args[0]) * 100)
        except Exception:
            pass
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Donación"},
                    "unit_amount": amount,
                },
                "quantity": 1,
            }],
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        await update.message.reply_text(f"💖 Gracias por apoyar: {session.url}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error al crear pago: {e}")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    # 1) levantar Flask en un hilo aparte
    threading.Thread(target=run_web, daemon=True).start()

    # 2) construir aplicación de Telegram en el hilo principal
    app = ApplicationBuilder().token(TOKEN).build()

    # limpiar cualquier webhook previo y descartar pendientes (evita conflicts)
    import asyncio
    asyncio.run(app.bot.delete_webhook(drop_pending_updates=True))

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("donar", donate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_handler(ChannelPostHandler(on_channel_post, filters.TEXT))

    print("🤖 Iniciando bot (polling)…")
    # drop_pending_updates también aquí por si se reinicia
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
