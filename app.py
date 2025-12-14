import json
import logging
import os
from datetime import datetime
from threading import Thread

from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ───────────────────────── LOGGING ─────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("cosplaylive")

# ───────────────────────── ENV ─────────────────────────

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Render")

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Traducción
ENABLE_TRANSLATION = os.environ.get("ENABLE_TRANSLATION", "1").strip().lower() in ("1", "true", "yes", "on")
TRANSLATE_TO = os.environ.get("TRANSLATE_TO", "de").strip()  # público -> alemán (salida para público)
TRANSLATE_MODEL_TO = os.environ.get("TRANSLATE_MODEL_TO", "pt").strip()  # público -> modelo (portugués)
MAX_TEXT = int(os.environ.get("MAX_TEXT", "3500"))

# Admin (tu user_id para proteger comandos). Puedes poner varios separados por coma.
ADMIN_IDS = set()
_raw_admins = os.environ.get("ADMIN_IDS", "").strip()
if _raw_admins:
    for part in _raw_admins.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))

# ───────────────────────── STORAGE ─────────────────────────
# data.json estructura:
# {
#   "sessions": {
#     "<model_user_id>": {
#       "group_chat_id": -100...,         # chat/grupo donde escriben los usuarios (chat del live)
#       "model_name": "Aurora",
#       "enabled": true
#     }
#   }
# }

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"sessions": {}}
    except Exception as e:
        logger.error(f"Error leyendo data.json: {e}")
        return {"sessions": {}}

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error guardando data.json: {e}")

def get_session(model_user_id: int):
    data = load_data()
    return data.get("sessions", {}).get(str(model_user_id))

def set_session(model_user_id: int, session_obj: dict):
    data = load_data()
    data.setdefault("sessions", {})
    data["sessions"][str(model_user_id)] = session_obj
    save_data(data)

def is_admin(user_id: int) -> bool:
    # Si no configuraste ADMIN_IDS, dejamos pasar igual (modo simple).
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)

# ───────────────────────── TRANSLATOR ─────────────────────────

_TRANSLATOR_OK = False
if ENABLE_TRANSLATION:
    try:
        from deep_translator import GoogleTranslator
        _TRANSLATOR_OK = True
    except Exception as e:
        logger.warning(f"ENABLE_TRANSLATION=1 pero deep-translator no está disponible: {e}")

def translate_text(text: str, target: str) -> str:
    """Traduce de auto -> target. Si falla, devuelve el texto original."""
    if not ENABLE_TRANSLATION or not _TRANSLATOR_OK:
        return text
    try:
        # auto-detect
        out = GoogleTranslator(source="auto", target=target).translate(text)
        return out or text
    except Exception as e:
        logger.warning(f"Falló traducción ({target}): {e}")
        return text

def safe_trim(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + "…"
    return text

def user_label(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "User"
    # nombre visible + @username si existe
    name = (u.full_name or "User").strip()
    if u.username:
        return f"{name} (@{u.username})"
    return name

# ───────────────────────── COMMANDS ─────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "✅ <b>CosplayLive – Translator Mode</b>\n\n"
        "Este bot sirve para traducir en vivo:\n"
        "• Chat del live (usuarios) → Portugués para la modelo\n"
        "• Modelo → Alemán para el chat\n\n"
        "<b>Comandos (en privado con el bot):</b>\n"
        "• /whereami  (úsalo en el chat del live para ver el Chat ID)\n"
        "• /setroom <CHAT_ID>  (guardar el chat del live para TU cuenta)\n"
        "• /setmodel <Nombre>  (guardar tu nombre)\n"
        "• /liveon  (activar puente traducción)\n"
        "• /liveoff (desactivar)\n\n"
        "Notas:\n"
        "• La modelo escribe al bot en privado → el bot lo publica traducido.\n"
        "• Los usuarios escriben en el chat del live → el bot se lo manda traducido a la modelo.\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"Chat ID: <code>{chat.id}</code>", parse_mode=ParseMode.HTML)

async def cmd_setroom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Solo en privado
    if update.effective_chat.type != "private":
        await update.message.reply_text("Usa /setroom en PRIVADO conmigo.")
        return

    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("No autorizado.")
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Uso: /setroom <CHAT_ID>\nTip: usa /whereami en el chat del live para obtenerlo.")
        return

    chat_id = int(context.args[0])
    session = get_session(user_id) or {"model_name": "Modelo", "enabled": False}
    session["group_chat_id"] = chat_id
    set_session(user_id, session)

    await update.message.reply_text(
        f"✅ Listo. Tu chat del live quedó guardado como:\n<code>{chat_id}</code>",
        parse_mode=ParseMode.HTML,
    )

async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Usa /setmodel en PRIVADO conmigo.")
        return

    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /setmodel <Nombre>")
        return

    name = " ".join(context.args).strip()
    session = get_session(user_id) or {"enabled": False}
    session["model_name"] = name
    set_session(user_id, session)

    await update.message.reply_text(f"✅ Nombre guardado: <b>{name}</b>", parse_mode=ParseMode.HTML)

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Usa /liveon en PRIVADO conmigo.")
        return

    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("No autorizado.")
        return

    session = get_session(user_id) or {}
    if not session.get("group_chat_id"):
        await update.message.reply_text("Falta configurar el chat del live. Haz: /setroom <CHAT_ID>")
        return

    session.setdefault("model_name", "Modelo")
    session["enabled"] = True
    set_session(user_id, session)

    await update.message.reply_text(
        "✅ Traducción LIVE activada.\n"
        "• Lo que escribas aquí (privado) se publicará en el chat del live en alemán.\n"
        "• Lo que escriban en el chat del live te llegará aquí traducido al portugués."
    )

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Usa /liveoff en PRIVADO conmigo.")
        return

    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("No autorizado.")
        return

    session = get_session(user_id)
    if not session:
        await update.message.reply_text("No hay sesión configurada.")
        return

    session["enabled"] = False
    set_session(user_id, session)
    await update.message.reply_text("⛔ Traducción LIVE desactivada.")

# ───────────────────────── MESSAGE BRIDGE ─────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usuario -> (grupo del live) -> bot -> privado modelo (traducido a PT)
    """
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    if update.effective_user and update.effective_user.is_bot:
        return

    text = safe_trim(update.message.text or "")
    if not text:
        return

    data = load_data()
    sessions = data.get("sessions", {})

    # Encontrar qué modelo tiene este grupo como room y está enabled
    target_model_id = None
    model_session = None
    for mid, sess in sessions.items():
        try:
            if sess.get("enabled") and int(sess.get("group_chat_id", 0)) == int(chat_id):
                target_model_id = int(mid)
                model_session = sess
                break
        except Exception:
            continue

    if not target_model_id or not model_session:
        return

    # Formato: "Nombre (user) : [DE original] -> [PT traducido]"
    sender = user_label(update)
    original = text
    translated = translate_text(original, target=TRANSLATE_MODEL_TO)

    msg_to_model = (
        f"💬 <b>{sender}</b>\n"
        f"🇩🇪 <i>{original}</i>\n"
        f"🇵🇹 <b>{translated}</b>"
    )

    try:
        await context.bot.send_message(
            chat_id=target_model_id,
            text=msg_to_model,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"No pude enviar mensaje a la modelo (privado): {e}")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Modelo -> (privado) -> bot -> grupo del live (traducido a DE)
    """
    if not update.message or not update.effective_chat:
        return

    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    if update.effective_user and update.effective_user.is_bot:
        return

    text = safe_trim(update.message.text or "")
    if not text:
        return

    # ignorar comandos
    if text.startswith("/"):
        return

    session = get_session(user_id)
    if not session or not session.get("enabled"):
        return

    group_chat_id = session.get("group_chat_id")
    model_name = session.get("model_name", "Modelo")

    # Traducir a alemán para el público
    de_text = translate_text(text, target=TRANSLATE_TO)

    out = f"🎙️ <b>{model_name}</b>: {de_text}"

    try:
        await context.bot.send_message(
            chat_id=int(group_chat_id),
            text=out,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"No pude publicar en el chat del live: {e}")

# ───────────────────────── FLASK KEEP ALIVE ─────────────────────────

flask_app = Flask(__name__)

@flask_app.get("/")
def index():
    return "OK - CosplayLive Translator"

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# ───────────────────────── MAIN ─────────────────────────

async def post_init(app):
    logger.info("Bot iniciado.")

def main():
    # Flask en hilo (Render Web Service)
    Thread(target=run_flask, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whereami", cmd_whereami))
    app.add_handler(CommandHandler("setroom", cmd_setroom))
    app.add_handler(CommandHandler("setmodel", cmd_setmodel))
    app.add_handler(CommandHandler("liveon", cmd_liveon))
    app.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # puente
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, handle_group_message))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_private_message))

    logger.info("CosplayLive Translator corriendo (polling)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
