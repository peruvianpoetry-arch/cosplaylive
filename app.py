import os, json, time, threading, asyncio, logging
from collections import deque
from flask import Flask, Response, send_from_directory, stream_with_context
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cosplaylive")

# ---------- ENV ----------
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION", "0") == "1"
TARGET_LANG = os.getenv("TRANSLATE_TO", "de")
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
SUCCESS_URL = os.getenv("SUCCESS_URL", "https://example.com/success")
CANCEL_URL = os.getenv("CANCEL_URL", "https://example.com/cancel")

# ---------- Traducción (opcional) ----------
translator = None
if ENABLE_TRANSLATION:
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source="auto", target=TARGET_LANG)
        log.info(f"🌐 Traducción habilitada → {TARGET_LANG}")
    except Exception as e:
        log.warning(f"No se pudo habilitar traducción: {e}")
        translator = None

def maybe_translate(text: str) -> str:
    if translator and text:
        try:
            return translator.translate(text)
        except Exception as e:
            log.warning(f"Fallo traduciendo: {e}")
            return text
    return text

# ---------- Stripe (opcional) ----------
stripe = None
if STRIPE_API_KEY:
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = STRIPE_API_KEY
        stripe = stripe_lib
        log.info("💳 Stripe habilitado (sk_test en pruebas).")
    except Exception as e:
        log.warning(f"No se pudo habilitar Stripe: {e}")
        stripe = None

# ---------- Flask + Overlay (SSE) ----------
web = Flask(__name__)
BUFFER = deque(maxlen=50)

@web.get("/")
def home():
    return "✅ Cosplaylive bot está corriendo"

@web.get("/overlay")
def overlay_page():
    return send_from_directory(".", "overlay.html")

@web.get("/events")
def events():
    @stream_with_context
    def gen():
        last = 0
        while True:
            if last < len(BUFFER):
                user, text = BUFFER[last]; last += 1
                yield f"data: {json.dumps({'user': user, 'text': text})}\n\n"
            else:
                time.sleep(0.4)
    return Response(gen(), mimetype="text/event-stream")

def push_to_overlay(user: str, text: str):
    BUFFER.append((user, text or ""))

def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ---------- Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    msg = "🤖 ¡Bot activo y funcionando correctamente!"
    log.info(f"[DM] /start de {user}")
    push_to_overlay(user, msg)
    await update.message.reply_text(msg)

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    text_in = update.message.text or ""
    text_out = maybe_translate(text_in)
    log.info(f"[DM] {user}: {text_in}  ->  {text_out}")
    push_to_overlay(user, text_out)
    await update.message.reply_text(f"📨 {text_out}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.text:
        canal = update.effective_chat.title or "Canal"
        text_in = update.channel_post.text
        text_out = maybe_translate(text_in)
        log.info(f"[CANAL] {canal}: {text_in}  ->  {text_out}")
        push_to_overlay(canal, text_out)

async def donate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not stripe:
        await update.message.reply_text("⚠️ Pagos no habilitados.")
        return
    amount = 500
    if context.args:
        try: amount = int(float(context.args[0]) * 100)
        except Exception: pass
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {"currency": "usd", "product_data": {"name": "Donación"}, "unit_amount": amount},
                "quantity": 1,
            }],
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        log.info(f"[PAGO] Checkout creado por {update.effective_user.id if update.effective_user else 'N/A'}: {amount} -> {session.url}")
        await update.message.reply_text(f"💖 Gracias por apoyar: {session.url}")
    except Exception as e:
        log.error(f"Error Stripe: {e}")
        await update.message.reply_text(f"⚠️ Error al crear pago: {e}")

# ---------- Main ----------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    asyncio.run(app.bot.delete_webhook(drop_pending_updates=True))

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("donar", donate_cmd))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))

    log.info("🤖 Iniciando bot (polling)…")
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
