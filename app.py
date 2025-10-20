import os
import json
import time
import threading
import asyncio
from collections import deque

from flask import Flask, Response, send_from_directory, stream_with_context
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
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
# Traducción (deep-translator)
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
    if translator and text:
        try:
            return translator.translate(text)
        except Exception:
            return text
    return text

# =========================
# Stripe (opcional)
# =========================
stripe = None
if STRIPE_API_KEY:
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = STRIPE_API_KEY
        stripe = stripe_lib
        print("💳 Stripe habilitado (usa claves sk_test_... en pruebas).")
    except Exception as e:
        print(f"⚠️ No se pudo habilitar Stripe: {e}")
        stripe = None

# =========================
# Flask + Overlay (SSE)
# =========================
web = Flask(__name__)
BUFFER = deque(maxlen=50)  # últimos mensajes

@web.get("/")
def home():
    return "✅ Cosplaylive bot está corriendo"

@web.get("/overlay")
def overlay_page():
    # Sirve overlay.html desde la raíz
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
                payload = json.dumps({"user": user, "text": text})
                yield f"data: {payload}\n\n"
            else:
                time.sleep(0.4)
    return Response(gen(), mimetype="text/event-stream")

def push_to_overlay(user: str, text: str):
    BUFFER.append((user, text or ""))

def run_web():
    # ¡Importante! sin reloader para evitar doble proceso
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# =========================
# Handlers del bot
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "🤖 ¡Bot activo y funcionando correctamente!"
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    push_to_overlay(user, msg)
    await update.message.reply_text(msg)

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    text_in = update.message.text or ""
    text_out = maybe_translate(text_in)
    push_to_overlay(user, text_out)
    await update.message.reply_text(f"📨 {text_out}")

# Mensajes publicados en CANAL (no DM)
async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.text:
        canal = update.effective_chat.title or "Canal"
        text_out = maybe_translate(update.channel_post.text)
        print(f"📢 Mensaje del canal ({canal}): {text_out}")
        push_to_overlay(canal, text_out)
        # Si quisieras responder en el canal, podrías enviar un mensaje aquí.

# Donaciones con Stripe (opcional)
async def donate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not stripe:
        await update.message.reply_text("⚠️ Pagos no habilitados.")
        return
    amount = 500  # 5.00 USD por defecto
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
# Main
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment (Render → Settings → Environment).")

    # 1) Arranca Flask en un hilo
    threading.Thread(target=run_web, daemon=True).start()

    # 2) Construye el bot y limpia webhook (evita conflictos)
    app = ApplicationBuilder().token(TOKEN).build()
    asyncio.run(app.bot.delete_webhook(drop_pending_updates=True))

    # 3) Registra handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("donar", donate_cmd))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))     # canal
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))  # DM/grupos

    print("🤖 Iniciando bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
