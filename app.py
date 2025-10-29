# app.py — CosplayLive (estable + overlay + IA + modos LIVE/IDLE + Stripe)
import os, sys, time, threading, logging, queue, random, json
from typing import List
from flask import Flask, Response, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========= Logging =========
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ========= Config =========
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

# Stripe (opcional, pero mantenemos activo si ya lo configuraste)
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_API_KEY") or ""
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET") or ""
if STRIPE_SECRET:
    try:
        import stripe  # type: ignore
        stripe.api_key = STRIPE_SECRET
    except Exception as e:
        log.warning("Stripe no disponible: %s", e)
        stripe = None
else:
    stripe = None

# Admins / moderación
ADMIN_IDS: List[int] = []
try:
    ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
except:
    ADMIN_IDS = []

ALLOW_ADULT = os.getenv("ALLOW_ADULT", "0") == "1"
INACTIVITY_MIN = int(os.getenv("INACTIVITY_MINUTES", "20"))

# ========= Estado del show =========
LIVE_MODE = False
LAST_ACTIVITY = time.time()

def set_live(state: bool):
    global LIVE_MODE
    LIVE_MODE = state
    push_event("🟢 SHOW EN VIVO" if state else "⚪ Show en espera")
    log.info("Estado LIVE_MODE = %s", LIVE_MODE)

def touch_activity():
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()

def auto_idle_watcher():
    """Si no hay actividad por X min, vuelve a modo espera."""
    while True:
        try:
            if LIVE_MODE and (time.time() - LAST_ACTIVITY) > (INACTIVITY_MIN * 60):
                set_live(False)
                log.info("Auto cambio a ESPERA por inactividad.")
        except Exception as e:
            log.warning("auto_idle_watcher: %s", e)
        time.sleep(30)

# ========= Cola de eventos Overlay =========
events: "queue.Queue[str]" = queue.Queue(maxsize=200)
def push_event(text: str):
    text = (text or "").replace("\n", " ").strip()
    if not text: return
    try:
        events.put_nowait(text)
    except queue.Full:
        try: events.get_nowait()
        except queue.Empty: pass
        events.put_nowait(text)

# ========= Flask =========
web = Flask(__name__)

@web.get("/")
def home(): 
    return "✅ CosplayLive bot está corriendo (IA + modos + Stripe)"

@web.get("/events")
def sse():
    def stream():
        yield "event: ping\ndata: 💓\n\n"
        while True:
            try:
                msg = events.get(timeout=20)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "event: ping\ndata: 💓\n\n"
    headers = {"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# ======== Stripe Webhook (mantener operativo) =========
@web.post("/stripe/webhook")
def stripe_webhook():
    if not stripe:
        log.warning("Webhook recibido pero Stripe no está inicializado.")
        return ("ok", 200)

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_ENDPOINT_SECRET)
    except Exception as e:
        log.error("Stripe signature error: %s", e)
        return ("signature error", 400)

    etype = event["type"]
    log.info("Evento Stripe ✅ recibido: %s", etype)

    # Ejemplo: checkout.session.completed
    if etype == "checkout.session.completed":
        data = event["data"]["object"]
        amount = (data.get("amount_total", 0) or 0) / 100.0
        currency = (data.get("currency") or "").upper()
        user = (data.get("customer_details", {}) or {}).get("email", "usuario")
        msg = f"🎉 Nueva donación: {user} — {amount:.2f} {currency}"
        log.info(msg)
        push_event(msg)
    return ("ok", 200)

# ========= Moderación light (permite erótico, bloquea lo prohibido) =========
BANNED_WORDS = ["nazi", "kill yourself", "suicídate"]  # ejemplo abreviado
UNDERAGE_MARKERS = ["menor", "underage", "niña", "niño", "teen real", "colegiala real"]

def is_disallowed(txt: str) -> bool:
    t = txt.lower()
    if any(w in t for w in UNDERAGE_MARKERS):
        return True
    if any(w in t for w in BANNED_WORDS):
        return True
    return False

# ========= IA Conversacional =========
def ai_reply(user_text: str) -> str:
    t = (user_text or "").lower().strip()
    # Filtros mínimos
    if is_disallowed(t):
        return "⚠️ Ese tema está prohibido aquí. Cambiemos de asunto."

    # Respuestas base (permitimos coqueteo/erótico light si ALLOW_ADULT)
    base_idle = [
        "😄 ¡Bienvenido! Puedo contarte horarios, donaciones y sorpresas.",
        "🎁 Si llegamos a la meta de hoy, habrá recompensa especial 💃",
        "💬 Estoy 24/7 para acompañarte mientras esperas el show.",
        "✨ ¿Dudas? Escribe y te ayudo como asistente del canal.",
    ]
    if ALLOW_ADULT:
        base_idle += [
            "😉 La modelo está calentando motores. ¿Quieres saber cómo apoyar el show?",
            "🔥 Hoy se viene un show subidito de tono… ¿te quedas?",
        ]

    base_live = [
        "🎥 Estamos EN VIVO. ¡No parpadees!",
        "🧭 Si quieres hacer un pedido especial, pregunta cómo donarlo.",
        "🎯 Tu apoyo hace que el show suba de nivel.",
    ]
    if ALLOW_ADULT:
        base_live += [
            "🔥 Está que arde… ¿pedimos un giro más atrevido?",
            "😉 Si quieres algo específico, dilo y vemos si la modelo acepta.",
        ]

    # Intenciones rápidas
    if any(w in t for w in ["hola","buenas","hey","hi"]):
        return "👋 ¡Hola! Soy el asistente del canal."
    if "donar" in t or "pagar" in t:
        return "💳 Para donar usa el botón/URL del chat. ¡Gracias por apoyar!"
    if "cuando" in t and ("show" in t or "empieza" in t):
        return "⏰ Aviso en este chat apenas inicie. Activa notificaciones."
    if "modelo" in t:
        return "💃 Nuestra modelo prepara el escenario. Mantente atento 😉"
    if "gracias" in t:
        return "🙏 ¡Gracias a ti por estar aquí!"

    # Respuesta según modo
    pool = base_live if LIVE_MODE else base_idle
    return random.choice(pool)

# ========= Handlers =========
def is_admin(update: Update) -> bool:
    uid = (update.effective_user.id if update.effective_user else 0)
    return uid in ADMIN_IDS

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ¡Bot listo! Puedo chatear 24/7 y anunciar metas/donaciones.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "🟢 EN VIVO" if LIVE_MODE else "⚪ EN ESPERA"
    await update.message.reply_text(f"Estado: {state} · Inactividad: {INACTIVITY_MIN} min")

async def live_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("Solo admin puede cambiar el estado.")
    set_live(True)
    await update.message.reply_text("✅ Marcado como EN VIVO.")

async def live_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("Solo admin puede cambiar el estado.")
    set_live(False)
    await update.message.reply_text("⏸️ Marcado como EN ESPERA.")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_activity()
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt = update.message.text or ""
    log.info("[DM] %s: %s", user, txt)
    reply = ai_reply(txt)
    push_event(f"💬 {user}: {txt}")
    push_event(f"🤖 Bot: {reply}")
    await update.message.reply_text(reply)

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_activity()
    canal = update.effective_chat.title or "Canal"
    txt = (update.channel_post.text or "").strip()
    log.info("[CANAL] %s: %s", canal, txt)
    if txt and not txt.startswith("/"):
        reply = ai_reply(txt)
        push_event(f"📢 [{canal}] {txt}")
        push_event(f"🤖 Respuesta: {reply}")
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        except Exception as e:
            log.warning("No se pudo responder en canal: %s", e)

# (Opcional) señales de videochat en supergrupos — por si migras a grupo
async def on_videochat_started(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        set_live(True)

async def on_videochat_ended(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        set_live(False)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("⚠️ Error", exc_info=context.error)
    push_event("⚠️ Error interno del bot")

# ========= Run =========
def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def run_polling_sync():
    if not TOKEN: raise SystemExit("Falta TELEGRAM_TOKEN en Environment")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("live_on", live_on_cmd))
    app.add_handler(CommandHandler("live_off", live_off_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, echo_msg))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, channel_post))
    # En supergrupos (si los usas):
    app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_STARTED, on_videochat_started))
    app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_ENDED, on_videochat_ended))
    app.add_error_handler(on_error)
    log.info("🤖 Iniciando bot (IA + LIVE/IDLE)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=auto_idle_watcher, daemon=True).start()
    run_polling_sync()
