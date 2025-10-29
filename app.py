# app.py — CosplayLive (estable + overlay + IA chat simulada)
import os, sys, threading, logging, queue, random
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
def home(): return "✅ CosplayLive bot está corriendo con IA"

@web.get("/events")
def sse():
    def stream():
        import time
        yield "event: ping\ndata: 💓\n\n"
        while True:
            try:
                msg = events.get(timeout=20)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "event: ping\ndata: 💓\n\n"
    headers = {"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# ========= Respuestas IA (modo entretenido) =========
def ai_reply(user_text: str) -> str:
    t = user_text.lower()
    respuestas = [
        "😄 ¡Qué buena vibra! Cuéntame, ¿vienes por la modelo o solo a mirar?",
        "🔥 La modelo estará en vivo más tarde… pero puedo contarte cómo donar si quieres 😉",
        "🎁 Si llegamos a la meta de hoy, habrá una sorpresa especial 💃",
        "💬 Estoy aquí 24h para hacerte compañía mientras esperas el show 😎",
        "✨ Si tienes preguntas, escríbelas, soy el asistente oficial del canal 💡",
        "😂 No soy humano, pero igual sé coquetear... ¿o quieres que te lo demuestre?",
        "🎶 La música está lista, solo falta que ella entre al escenario...",
        "🥳 Hoy promete ser un show 🔥🔥🔥, ¿quieres reservar tu asiento?",
    ]
    if any(w in t for w in ["hola", "buenas", "hey", "hi"]):
        return random.choice(["👋 ¡Hola! Bienvenido al show 😄", "✨ Hola, soy el asistente de la modelo.", "😎 ¡Hey! Pasa y siéntate, el show está por comenzar."])
    if "donar" in t or "pagar" in t:
        return "💳 Puedes donar con el comando /donar o hacer clic en el botón del chat. ¡Cada aporte cuenta!"
    if "modelo" in t or "ella" in t:
        return "💃 La modelo está preparándose, pronto entrará en vivo. Mientras tanto, te puedo contar curiosidades o ayudarte con los comandos 😉"
    if "show" in t:
        return "🎥 El próximo show comienza en unas horas. Te avisaré cuando esté en vivo 😉"
    if "gracias" in t:
        return "🙏 ¡Gracias a ti por apoyar el canal! Eres parte de la comunidad 💖"
    if "adiós" in t or "chau" in t:
        return "👋 ¡Nos vemos pronto! No te pierdas el próximo show 🔥"
    return random.choice(respuestas)

# ========= Handlers =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ¡Bot listo! Puedes hablar conmigo cuando quieras 😎")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt = update.message.text or ""
    log.info("[DM] %s: %s", user, txt)
    reply = ai_reply(txt)
    push_event(f"💬 {user}: {txt}")
    push_event(f"🤖 Bot: {reply}")
    await update.message.reply_text(reply)

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    canal = update.effective_chat.title or "Canal"
    txt = (update.channel_post.text or "").strip()
    log.info("[CANAL] %s: %s", canal, txt)
    reply = ai_reply(txt)
    push_event(f"📢 [{canal}] {txt}")
    push_event(f"🤖 Respuesta: {reply}")
    # Responde en el canal solo si el mensaje es texto normal
    if txt and not txt.startswith("/"):
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        except Exception as e:
            log.warning("No se pudo responder en canal: %s", e)

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
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, echo_msg))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, channel_post))
    app.add_error_handler(on_error)
    log.info("🤖 Iniciando bot con IA conversacional (polling sync)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    run_polling_sync()
