# app.py — CosplayLive (bot + overlay OBS + Stripe Webhook)
import sys, os, threading, logging, queue
from flask import Flask, Response, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ===== Logging consistente en Render =====
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ===== Config =====
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# ===== Cola de eventos para el overlay (SSE) =====
events: "queue.Queue[str]" = queue.Queue(maxsize=200)

def push_event(text: str) -> None:
    """Encola mensajes para el overlay sin bloquear."""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return
    try:
        events.put_nowait(text)
    except queue.Full:
        try:
            events.get_nowait()  # descartar el más viejo
        except queue.Empty:
            pass
        events.put_nowait(text)

# ===== Flask (mantiene vivo el servicio y sirve el overlay) =====
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot está corriendo"

@web.get("/overlay")
def overlay():
    # Overlay simple para OBS (Browser Source -> URL: /overlay)
    html = """
<!doctype html>
<html>
<head><meta charset="utf-8">
<style>
  html,body{background:transparent;margin:0;padding:0;overflow:hidden}
  #chat{font:18px/1.35 system-ui,Segoe UI,Roboto,Arial,sans-serif;color:#fff;
        text-shadow:0 1px 2px rgba(0,0,0,.6); padding:12px; box-sizing:border-box;
        display:flex; flex-direction:column; gap:6px; width:100vw; height:100vh}
  .msg{background:rgba(0,0,0,.35); border-radius:10px; padding:8px 12px; max-width:90%}
</style></head>
<body>
  <div id="chat"></div>
  <script>
    const chat = document.getElementById("chat");
    const es = new EventSource("/events");
    es.onmessage = (e) => {
      const div = document.createElement("div");
      div.className = "msg";
      div.textContent = e.data;
      chat.appendChild(div);
      while (chat.children.length > 40) chat.removeChild(chat.firstChild);
      window.scrollTo(0, document.body.scrollHeight);
    };
  </script>
</body></html>
"""
    return html

@web.get("/events")
def sse():
    def stream():
        yield "retry: 2000\n\n"  # reconexión automática del EventSource
        while True:
            msg = events.get()
            yield f"data: {msg}\n\n"
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# ===== Stripe Webhook (recibe pagos / donaciones) =====
@web.post("/stripe/webhook")
def stripe_webhook():
    import stripe

    # Claves desde variables de entorno
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # sk_test_****
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")  # whsec_****

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        log.info(f"✅ Evento Stripe recibido: {event['type']}")
    except ValueError as e:
        log.error(f"❌ Payload inválido: {e}")
        return "Bad request", 400
    except stripe.error.SignatureVerificationError as e:
        log.error(f"❌ Firma inválida: {e}")
        return "Unauthorized", 400

    # Pagos confirmados (Payment Links / Checkout)
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        total = (session.get("amount_total") or 0) / 100.0
        currency = (session.get("currency") or "").upper()
        email = session.get("customer_email") or "usuario"
        msg = f"🎉 Nueva donación: {email} — {total:.2f} {currency}"
        log.info(msg)
        push_event(msg)  # aparece en el overlay OBS

    # También puedes escuchar payment_intent.succeeded si usas Payment Intents
    if event["type"] == "payment_intent.succeeded":
        pi = event["data"]["object"]
        total = (pi.get("amount") or 0) / 100.0
        currency = (pi.get("currency") or "").upper()
        msg = f"💳 Pago confirmado: {total:.2f} {currency}"
        log.info(msg)
        push_event(msg)

    return "OK", 200

def run_web():
    # No usar reloader para no duplicar procesos en Render
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ===== Handlers del bot =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    log.info(f"[DM] start de {user}")
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt = update.message.text or ""
    log.info(f"[DM] {user}: {txt}")
    push_event(f"✉️ {user}: {txt}")  # -> overlay
    await update.message.reply_text(f"✉️ Recibido: {txt}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Mensajes escritos en el canal (si el bot es admin)
    if not update.channel_post:
        return
    canal = update.effective_chat.title or "Canal"
    txt = update.channel_post.text or ""
    log.info(f"[CANAL] #{canal}: {txt}")
    push_event(f"📣 #{canal}: {txt}")  # -> overlay

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("❌ Handler error", exc_info=context.error)

def start_bot():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, echo_msg))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, channel_post))
    app.add_error_handler(on_error)

    log.info("🚀 Iniciando bot (polling)…")
    # Ejecutar polling de PTB en este hilo
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

# ===== Main =====
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ FALTA TELEGRAM_TOKEN en Environment.")
    # Bot en un hilo background, Flask en hilo principal (evita conflictos de asyncio)
    threading.Thread(target=start_bot, daemon=True).start()
    run_web()
