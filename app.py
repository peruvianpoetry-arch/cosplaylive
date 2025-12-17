import os
import json
import time
import logging
import threading
from datetime import datetime
from typing import Dict, Any

from flask import Flask, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -----------------------------
# LOGGING
# -----------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cosplaylive")

# -----------------------------
# ENV
# -----------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Render (Environment).")

DATA_DIR = os.environ.get("DATA_DIR", "/var/data").strip()
PORT = int(os.environ.get("PORT", "10000"))

LIVE_FILE = os.path.join(DATA_DIR, "live.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")  # opcional, preparado

os.makedirs(DATA_DIR, exist_ok=True)

# -----------------------------
# FILE HELPERS
# -----------------------------
def _read_json(path: str, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("No se pudo leer %s: %s", path, e)
        return default

def _write_json(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def get_live_state() -> Dict[str, Any]:
    return _read_json(LIVE_FILE, default={})

def set_live_state(user_id: int, is_live: bool) -> None:
    data = get_live_state()
    data[str(user_id)] = {
        "live": bool(is_live),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    _write_json(LIVE_FILE, data)

def get_sessions() -> Dict[str, Any]:
    return _read_json(SESSIONS_FILE, default={})

# -----------------------------
# TELEGRAM HANDLERS
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text("✅ Bot funcionando correctamente")

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    chat = update.effective_chat
    text = (
        f"👤 user_id: {u.id if u else 'N/A'}\n"
        f"👤 username: @{u.username if u and u.username else 'N/A'}\n"
        f"💬 chat_id: {chat.id if chat else 'N/A'}\n"
        f"💬 chat_type: {chat.type if chat else 'N/A'}"
    )
    await update.message.reply_text(text)

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return
    set_live_state(uid, True)
    await update.message.reply_text("🟢 LIVE ON activado (guardado).")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return
    set_live_state(uid, False)
    await update.message.reply_text("⚫ LIVE OFF activado (guardado).")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return

    live_data = get_live_state().get(str(uid))
    sessions = get_sessions().get(str(uid))

    if not sessions:
        # Esto es tu mensaje “No hay sesión configurada.” pero sin romper nada.
        await update.message.reply_text("ℹ️ No hay sesión configurada.\n✅ El bot está OK.\nUsa /liveon o /liveoff.")
        return

    await update.message.reply_text(
        f"📌 Sesión: {json.dumps(sessions, ensure_ascii=False)}\n"
        f"📡 Live: {json.dumps(live_data, ensure_ascii=False)}"
    )

# -----------------------------
# FLASK (KEEP-ALIVE)
# -----------------------------
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return jsonify({"ok": True, "service": "cosplaylive-bot", "time": datetime.utcnow().isoformat() + "Z"})

@flask_app.get("/health")
def health():
    return jsonify({"ok": True})

def run_flask():
    # Render espera un puerto abierto. Flask lo mantiene vivo.
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

# -----------------------------
# MAIN
# -----------------------------
def main():
    # 1) Flask thread
    t = threading.Thread(target=run_flask, name="flask_thread", daemon=True)
    t.start()

    # 2) Telegram app (SIN Updater)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))
    application.add_handler(CommandHandler("status", cmd_status))

    logger.info("Bot iniciado. Polling activo.")
    # run_polling bloquea el hilo principal: perfecto para Render
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()

