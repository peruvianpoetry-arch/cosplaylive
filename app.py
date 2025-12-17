import os
import json
import logging
import threading
from datetime import datetime
from typing import Dict, Any, Optional

from flask import Flask, jsonify
from telegram import Update
from telegram.constants import ChatType
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
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")

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

def set_channel_for_user(user_id: int, channel_id: int, channel_title: str = "", channel_username: str = "") -> None:
    sessions = get_sessions()
    sessions[str(user_id)] = {
        "tg_channel_id": int(channel_id),
        "channel_title": channel_title or "",
        "channel_username": channel_username or "",
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    _write_json(SESSIONS_FILE, sessions)

def get_channel_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    return get_sessions().get(str(user_id))

# -----------------------------
# TELEGRAM HANDLERS
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

async def cmd_bindhere(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Se ejecuta DENTRO del canal (o grupo). Guarda el chat_id como canal destino
    para el usuario que ejecutó el comando (tú lo harás una vez).
    """
    uid = update.effective_user.id if update.effective_user else None
    chat = update.effective_chat
    if not uid or not chat:
        return

    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
        await update.message.reply_text("❌ Usa /bindhere dentro del canal (o grupo), no en privado.")
        return

    # Nota: si es grupo, también funciona, pero el objetivo normalmente es canal.
    set_channel_for_user(
        user_id=uid,
        channel_id=chat.id,
        channel_title=getattr(chat, "title", "") or "",
        channel_username=getattr(chat, "username", "") or "",
    )

    await update.message.reply_text(
        f"✅ Canal vinculado para tu usuario.\n"
        f"ID: {chat.id}\n"
        f"Título: {getattr(chat, 'title', '') or '(sin título)'}\n"
        f"Ahora en privado usa /channel para verificar."
    )

async def cmd_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return
    s = get_channel_for_user(uid)
    if not s:
        await update.message.reply_text("ℹ️ No hay canal vinculado. Entra al canal y escribe /bindhere (una sola vez).")
        return

    title = s.get("channel_title", "")
    username = s.get("channel_username", "")
    channel_id = s.get("tg_channel_id")
    pretty = f"@{username}" if username else "(sin @)"
    await update.message.reply_text(
        f"📌 Canal vinculado:\n"
        f"ID: {channel_id}\n"
        f"Título: {title or '(sin título)'}\n"
        f"Username: {pretty}"
    )

async def _post_to_bound_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return False

    sess = get_channel_for_user(uid)
    if not sess:
        await update.message.reply_text("ℹ️ No hay canal vinculado. Entra al canal y escribe /bindhere (una sola vez).")
        return False

    channel_id = sess.get("tg_channel_id")
    try:
        await context.bot.send_message(chat_id=channel_id, text=text)
        return True
    except Exception as e:
        logger.exception("No pude publicar en el canal %s: %s", channel_id, e)
        await update.message.reply_text(
            "❌ No pude publicar en el canal.\n"
            "Causas típicas: el bot no es admin del canal, o no tiene permiso de publicar."
        )
        return False

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return
    set_live_state(uid, True)

    ok = await _post_to_bound_channel(
        update, context,
        "🟢 LIVE ON\nLa modelo está en vivo ahora. Entra al canal para ver el show."
    )
    if ok:
        await update.message.reply_text("🟢 LIVE ON activado y publicado en el canal.")
    else:
        await update.message.reply_text("🟢 LIVE ON activado (guardado), pero no se pudo publicar.")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return
    set_live_state(uid, False)

    ok = await _post_to_bound_channel(
        update, context,
        "⚫ LIVE OFF\nEl live terminó por ahora."
    )
    if ok:
        await update.message.reply_text("⚫ LIVE OFF activado y publicado en el canal.")
    else:
        await update.message.reply_text("⚫ LIVE OFF activado (guardado), pero no se pudo publicar.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return

    live_data = get_live_state().get(str(uid))
    sess = get_channel_for_user(uid)

    if not sess:
        await update.message.reply_text("ℹ️ No hay sesión/canal configurado.\nEntra al canal y escribe /bindhere.")
        return

    await update.message.reply_text(
        f"✅ Sesión OK\n"
        f"Canal ID: {sess.get('tg_channel_id')}\n"
        f"Canal: {sess.get('channel_title','')}\n"
        f"Live: {json.dumps(live_data, ensure_ascii=False)}"
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
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

# -----------------------------
# MAIN
# -----------------------------
def main():
    t = threading.Thread(target=run_flask, name="flask_thread", daemon=True)
    t.start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("bindhere", cmd_bindhere))
    application.add_handler(CommandHandler("channel", cmd_channel))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))
    application.add_handler(CommandHandler("status", cmd_status))

    logger.info("Bot iniciado. Polling activo.")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
