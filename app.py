# app.py — CosplayLive (estable + overlay OBS)
import sys, os, threading, logging, queue
from flask import Flask, Response
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ===== Logging consistente a Render =====
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ===== Config =====
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT  = int(os.getenv("PORT", "10000"))

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
    # Fondo transparente y escucha de /events (Server-Sent Events)
    return """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html,body{background:transparent;margin:0;padding:0;overflow:hidden}
  #chat{font:18px/1.35 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#fff;
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

@web.get("/events")
def sse():
    def stream():
        # Mantener el stream abierto y entregar mensajes en tiempo real
        while True:
            msg = events.get()
            yield f"data: {msg}\n\n"
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream(), mimetype="text/event-stream", headers=headers)

def run_web():
    # No usar reloader para que no duplique procesos
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ===== Handlers del bot =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    log.info(f"[DM] /start de {user}")
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt  = update.message.text or ""
    log.info(f"[DM] {user}: {txt}")
    push_event(f"{user}: {txt}")           # -> overlay
    await update.message.reply_text(f"📨 {txt}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.text:
        canal = update.effective_chat.title or "Canal"
        txt   = update.channel_post.text
        log.info(f"[CANAL] {canal}: {txt}")
        push_event(f"[{canal}] {txt}")     # -> overlay

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("❌ Handler error", exc_info=context.error)

# ===== Main =====
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    # 1) Web server para overlay
    threading.Thread(target=run_web, daemon=True).start()

    # 2) Bot (polling síncrono, estable en Render)
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_msg))
    app.add_error_handler(on_error)

    log.info("🤖 Iniciando bot (polling SÍNCRONO)…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=None
    )
