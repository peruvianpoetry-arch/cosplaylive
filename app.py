import os
import json
import logging
from pathlib import Path
from datetime import datetime

from flask import Flask

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("cosplaylive")

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN en Environment Variables")

# Persistencia en Render Disk (si existe)
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

MODELS_FILE = os.path.join(DATA_DIR, "models.json")   # model_user_id -> {"name": "..."}
ROOMS_FILE  = os.path.join(DATA_DIR, "rooms.json")    # model_user_id -> {"chat_id": -100...}
LIVE_FILE   = os.path.join(DATA_DIR, "live.json")     # model_user_id -> true/false

DEFAULT_TO_MODEL_LANG = os.environ.get("TO_MODEL_LANG", "pt")  # DE -> PT para la modelo
DEFAULT_TO_CHAT_LANG  = os.environ.get("TO_CHAT_LANG", "de")  # PT -> DE para el grupo/canal

# Traducción (opcional)
USE_TRANSLATION = True
try:
    from deep_translator import GoogleTranslator
except Exception:
    USE_TRANSLATION = False
    GoogleTranslator = None


def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.exception(f"Error leyendo {path}: {e}")
    return default


def _save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"Error guardando {path}: {e}")


def translate(text: str, src: str, dest: str) -> str:
    if not USE_TRANSLATION or not text.strip():
        return text
    try:
        return GoogleTranslator(source=src, target=dest).translate(text)
    except Exception as e:
        logger.warning(f"Falló traducción {src}->{dest}: {e}")
        return text


# =========================
# FLASK keep-alive
# =========================
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK"

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)


# =========================
# BOT LOGIC
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot funcionando correctamente")


async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    La MODELO lo ejecuta en privado:
    /setmodel Aurora
    """
    if update.effective_chat.type != "private":
        await update.message.reply_text("Este comando se usa SOLO en privado.")
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Uso: /setmodel TuNombre")
        return

    models = _load_json(MODELS_FILE, {})
    uid = str(update.effective_user.id)
    models[uid] = {"name": name, "ts": datetime.utcnow().isoformat()}
    _save_json(MODELS_FILE, models)

    await update.message.reply_text(f"✅ Modelo registrada como: {name}\nTu ID: {uid}")


async def cmd_bindchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Se ejecuta EN EL GRUPO (o canal con discussion group):
    /bindchat <model_user_id>
    Vincula este grupo al user_id de la modelo.
    """
    if update.effective_chat.type == "private":
        await update.message.reply_text("Este comando se usa en un GRUPO, no en privado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return

    model_uid = context.args[0].strip()
    models = _load_json(MODELS_FILE, {})
    if model_uid not in models:
        await update.message.reply_text("❌ Ese model_user_id no existe. La modelo debe hacer /setmodel primero.")
        return

    rooms = _load_json(ROOMS_FILE, {})
    rooms[model_uid] = {
        "chat_id": update.effective_chat.id,
        "title": update.effective_chat.title,
        "ts": datetime.utcnow().isoformat()
    }
    _save_json(ROOMS_FILE, rooms)

    await update.message.reply_text(
        f"✅ Vinculado.\nModelo: {models[model_uid]['name']} ({model_uid})\nChat ID: {update.effective_chat.id}"
    )


async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Este comando se usa SOLO en privado.")
        return

    uid = str(update.effective_user.id)
    rooms = _load_json(ROOMS_FILE, {})
    if uid not in rooms:
        await update.message.reply_text("❌ No hay sala vinculada. Primero usa /bindchat en el grupo.")
        return

    live = _load_json(LIVE_FILE, {})
    live[uid] = True
    _save_json(LIVE_FILE, live)

    await update.message.reply_text("✅ LIVE ON (traducción activada)")

    # Aviso en el grupo
    chat_id = rooms[uid]["chat_id"]
    await context.bot.send_message(chat_id=chat_id, text="🔴 LIVE ON ✅")


async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Este comando se usa SOLO en privado.")
        return

    uid = str(update.effective_user.id)
    live = _load_json(LIVE_FILE, {})
    live[uid] = False
    _save_json(LIVE_FILE, live)

    rooms = _load_json(ROOMS_FILE, {})
    if uid in rooms:
        await context.bot.send_message(chat_id=rooms[uid]["chat_id"], text="⚫ LIVE OFF")

    await update.message.reply_text("✅ LIVE OFF")


async def relay_group_to_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mensajes del grupo -> traducir DE->PT -> mandar a la modelo (privado) si está LIVE ON
    """
    if not update.message or not update.effective_chat:
        return

    rooms = _load_json(ROOMS_FILE, {})
    live = _load_json(LIVE_FILE, {})

    # Encontrar qué modelo corresponde a este chat
    chat_id = update.effective_chat.id
    model_uid = None
    for uid, info in rooms.items():
        if info.get("chat_id") == chat_id:
            model_uid = uid
            break
    if not model_uid:
        return

    if not live.get(model_uid, False):
        return

    text = update.message.text or ""
    if not text.strip():
        return

    sender = update.effective_user.full_name if update.effective_user else "Anon"
    translated = translate(text, src=DEFAULT_TO_CHAT_LANG, dest=DEFAULT_TO_MODEL_LANG)
    out = f"💬 {sender}:\n{translated}"
    await context.bot.send_message(chat_id=int(model_uid), text=out)


async def relay_model_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mensajes privados de la modelo -> traducir PT->DE -> publicar en el grupo vinculado si está LIVE ON
    """
    if not update.message or update.effective_chat.type != "private":
        return

    uid = str(update.effective_user.id)
    rooms = _load_json(ROOMS_FILE, {})
    live = _load_json(LIVE_FILE, {})

    if uid not in rooms:
        return
    if not live.get(uid, False):
        return

    text = update.message.text or ""
    if not text.strip():
        return

    models = _load_json(MODELS_FILE, {})
    name = models.get(uid, {}).get("name", "Modelo")

    translated = translate(text, src=DEFAULT_TO_MODEL_LANG, dest=DEFAULT_TO_CHAT_LANG)
    chat_id = rooms[uid]["chat_id"]
    await context.bot.send_message(chat_id=chat_id, text=f"🔥 {name}:\n{translated}")


def main():
    # Iniciar Flask en un thread (keep-alive)
    import threading
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("setmodel", cmd_setmodel))
    application.add_handler(CommandHandler("bindchat", cmd_bindchat))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # Grupo -> modelo (texto)
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, relay_group_to_model))
    # Privado modelo -> grupo (texto)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, relay_model_to_group))

    logger.info("Bot iniciado (PTB 20.8 + Python 3.11.9)")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
