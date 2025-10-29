# app.py — CosplayLive (estable + overlay + Stripe + canal TG)
# Modo: polling SÍNCRONO (compat. Render Free/Starter)

import os, sys, threading, logging, queue
from flask import Flask, Response, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========= Logging consistente en Render =========
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ========= Config =========
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT  = int(os.getenv("PORT", "10000"))

# ========= Cola de eventos para el overlay (SSE) =========
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

# ========= Flask (keep-alive + overlay) =========
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot está corriendo"

@web.get("/overlay")
def overlay():
    # Overlay ultraligero (fondo transparente)
    html = """<!doctype html>
<html><head><meta charset="utf-8">
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
    const chat = document.getElementById('chat');
    const es = new EventSource('/events');
    es.onmessage = (e) => {
      const div = document.createElement('div');
      div.className = 'msg';
      div.textContent = e.data;
      chat.appendChild(div);
      while (chat.children.length > 40) chat.removeChild(chat.firstChild);
      window.scrollTo(0, document.body.scrollHeight);
    };
  </script>
</body></html>"""
    return html

@web.get("/events")
def sse():
    def stream():
        # enviar ping cada 20s para mantener vivo
        import time
        yield "event: ping\ndata: 💓\n\n"
        while True:
            try:
                msg = events.get(timeout=20)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "event: ping\ndata: 💓\n\n"
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# ========= Stripe (webhook) =========
# Variables de entorno:
# STRIPE_SECRET_KEY  -> sk_test_...
# STRIPE_WEBHOOK_SECRET -> whsec_...
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WHSEC = os.getenv("STRIPE_WEBHOOK_SECRET")

@web.post("/stripe/webhook")
def stripe_webhook():
    import stripe  # asegurado en requirements.txt
    if not STRIPE_SECRET or not STRIPE_WHSEC:
        log.warning("Stripe no configurado; ignorando webhook.")
        return ("ok", 200)

    stripe.api_key = STRIPE_SECRET
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WHSEC)
    except Exception as e:
        log.exception("❌ Stripe firma inválida / error parseando evento")
        return ("bad signature", 400)

    etype = event.get("type")
    log.info("✅ Evento Stripe recibido: %s", etype)

    if etype == "checkout.session.completed":
        sess = event["data"]["object"]
        amount_total = (sess.get("amount_total") or 0) / 100.0
        currency = (sess.get("currency") or "").upper()
        user = (sess.get("client_reference_id") or "usuario")
        txt = f"💸 Nueva donación: {user} – {amount_total:.2f} {currency}"
        log.info(txt)
        push_event(txt)

    return ("ok", 200)

# ========= Handlers del bot =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    log.info("[DM] start de %s", user)
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")
    push_event(f"📣 {user} ha iniciado el bot")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt = update.message.text or ""
    log.info("[DM] %s: %s", user, txt)
    push_event(f"✉️ {user}: {txt}")
    await update.message.reply_text(f"✉️ {txt}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Mensajes publicados en un CANAL (no chat normal)
    canal = update.effective_chat.title or "Canal"
    txt = (update.channel_post.text or "").strip()
    log.info("[CANAL] %s: %s", canal, txt)
    push_event(f"📢 [{canal}] {txt}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("❌ Handler error", exc_info=context.error)
    push_event("⚠️ Error interno del bot (ver logs)")

# ========= Runner =========
def run_web():
    # sin reloader para no duplicar procesos en Render
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def run_polling_sync():
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    app = ApplicationBuilder().token(TOKEN).build()

    # DM / comandos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, echo_msg))

    # Canal (sustituye ChannelPostHandler)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, channel_post))

    # Errores
    app.add_error_handler(on_error)

    log.info("🤖 Iniciando bot (polling SINCRONO)…")
    # drop_pending_updates=True evita procesar colas viejas tras un deploy
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    # Lanzar Flask en thread aparte
    t = threading.Thread(target=run_web, name="flask", daemon=True)
    t.start()
    # Polling principal (bloqueante, en el hilo main)
    run_polling_sync()
