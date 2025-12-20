import os
import json
import time
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask

from telegram import Update
from telegram.parsemode import ParseMode  # ✅ FIX: PTB v13
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None


# =========================
# Config
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or os.getenv("TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Environment Variables")

DATA_DIR = os.getenv("DATA_DIR", "/var/data")
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")      # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")    # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")        # model_user_id -> True/False
STREAMER_FILE = os.path.join(DATA_DIR, "streamer.json")  # group_chat_id -> model_user_id


GROUP_LANGUAGE = os.getenv("GROUP_LANGUAGE", "de")   # idioma del grupo (destino cuando streamer escribe)
MODEL_LANGUAGE = os.getenv("MODEL_LANGUAGE", "pt")   # idioma del streamer (destino cuando grupo escribe)
TZ = os.getenv("TZ", "Europe/Berlin")

# =========================
# Helpers JSON
# =========================
def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def rooms():
    return _load_json(ROOMS_FILE, {})


def models():
    return _load_json(MODELS_FILE, {})


def live_state():
    return _load_json(LIVE_FILE, {})


def streamer_map():
    return _load_json(STREAMER_FILE, {})


def set_room(model_user_id: int, group_chat_id: int):
    r = rooms()
    r[str(model_user_id)] = int(group_chat_id)
    _save_json(ROOMS_FILE, r)


def set_model_name(model_user_id: int, name: str):
    m = models()
    m[str(model_user_id)] = name.strip()
    _save_json(MODELS_FILE, m)


def set_live(model_user_id: int, is_on: bool):
    l = live_state()
    l[str(model_user_id)] = bool(is_on)
    _save_json(LIVE_FILE, l)


def set_streamer_for_group(group_chat_id: int, model_user_id: int):
    sm = streamer_map()
    sm[str(group_chat_id)] = int(model_user_id)
    _save_json(STREAMER_FILE, sm)


def get_streamer_for_group(group_chat_id: int):
    sm = streamer_map()
    v = sm.get(str(group_chat_id))
    return int(v) if v else None


def translate_text(text: str, source: str, target: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if not GoogleTranslator:
        # Sin traductor instalado, devuelve texto original
        return text
    try:
        return GoogleTranslator(source=source, target=target).translate(text)
    except Exception:
        return text


def get_model_name(model_user_id: int) -> str:
    m = models()
    return m.get(str(model_user_id), "Streamer")


def now_tag():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =========================
# Bot commands
# =========================
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text("✅ Bot funcionando correctamente")


def cmd_whoami(update: Update, context: CallbackContext):
    u = update.effective_user
    chat = update.effective_chat
    update.message.reply_text(
        f"👤 user_id: {u.id}\n"
        f"👤 username: @{u.username if u.username else '(sin username)'}\n"
        f"💬 chat_id: {chat.id}\n"
        f"💬 chat_type: {chat.type}\n"
        f"🕒 {now_tag()}"
    )


def cmd_bindhere(update: Update, context: CallbackContext):
    # alias práctico: /bindhere en el grupo, usando el streamer actual del grupo si existe
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        update.message.reply_text("Este comando se usa en un grupo/supergrupo.")
        return

    model_id = get_streamer_for_group(chat.id)
    if not model_id:
        update.message.reply_text("No hay streamer configurado. Usa /setstreamer primero.")
        return

    set_room(model_id, chat.id)
    update.message.reply_text(f"✅ Grupo vinculado a streamer user_id={model_id}")


def cmd_bindchat(update: Update, context: CallbackContext):
    # /bindchat <model_user_id>  (en el grupo)
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        update.message.reply_text("Este comando se usa en un grupo/supergrupo.")
        return

    if not context.args:
        update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return

    try:
        model_id = int(context.args[0])
    except Exception:
        update.message.reply_text("El model_user_id debe ser numérico.")
        return

    set_room(model_id, chat.id)
    update.message.reply_text(f"✅ Grupo vinculado.\nStreamer user_id: {model_id}\nchat_id: {chat.id}")


def cmd_setmodel(update: Update, context: CallbackContext):
    # /setmodel <Nombre> (en privado, cada streamer se setea a sí mismo)
    chat = update.effective_chat
    if chat.type != "private":
        update.message.reply_text("Este comando se usa en privado con el bot.")
        return

    if not context.args:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return

    name = " ".join(context.args).strip()
    uid = update.effective_user.id
    set_model_name(uid, name)
    update.message.reply_text(f"✅ Modelo guardada.\nuser_id: {uid}\nNombre: {name}")


def cmd_liveon(update: Update, context: CallbackContext):
    # /liveon (en privado del streamer)
    if update.effective_chat.type != "private":
        update.message.reply_text("Este comando se usa en privado.")
        return
    uid = update.effective_user.id
    set_live(uid, True)
    update.message.reply_text("✅ LIVE ON (activo)")


def cmd_liveoff(update: Update, context: CallbackContext):
    if update.effective_chat.type != "private":
        update.message.reply_text("Este comando se usa en privado.")
        return
    uid = update.effective_user.id
    set_live(uid, False)
    update.message.reply_text("⛔ LIVE OFF (pausado)")


def cmd_setstreamer(update: Update, context: CallbackContext):
    """
    En el grupo:
    - Responde al mensaje de Aurora y escribe /setstreamer
      o
    - /setstreamer <user_id>
    """
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        update.message.reply_text("Este comando se usa en un grupo/supergrupo.")
        return

    target_user_id = None
    target_name = None

    # Caso 1: reply a un mensaje
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_user_id = u.id
        target_name = u.first_name or u.username or "Streamer"

    # Caso 2: /setstreamer <id>
    if not target_user_id and context.args:
        try:
            target_user_id = int(context.args[0])
            target_name = f"user_id {target_user_id}"
        except Exception:
            pass

    if not target_user_id:
        update.message.reply_text(
            "Uso:\n"
            "1) Responde al mensaje de Aurora y escribe /setstreamer\n"
            "o\n"
            "2) /setstreamer <user_id>"
        )
        return

    # Guardar streamer para el grupo
    set_streamer_for_group(chat.id, target_user_id)

    # Asegurar que exista nombre por si no tiene /setmodel
    if target_user_id and target_name:
        m = models()
        if str(target_user_id) not in m:
            set_model_name(target_user_id, target_name)

    update.message.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {get_model_name(target_user_id)}\n"
        f"user_id: {target_user_id}\n\n"
        "Prueba ahora:\n"
        "- En el grupo escribe algo en alemán → se enviará traducido al privado del streamer.\n"
        "- En privado, el streamer escribe algo → se publicará traducido aquí."
    )


# =========================
# Message routing (Live translation)
# =========================
def handle_group_message(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # streamer por grupo
    model_id = get_streamer_for_group(chat.id)
    if not model_id:
        return

    # solo si LIVE ON
    l = live_state()
    if not l.get(str(model_id), False):
        return

    # enviar al privado del streamer traducido DE->PT
    translated = translate_text(text, source=GROUP_LANGUAGE, target=MODEL_LANGUAGE)
    sender = update.effective_user.first_name or "Alguien"
    payload = f"💬 {sender} ({GROUP_LANGUAGE}→{MODEL_LANGUAGE}):\n{translated}"
    try:
        context.bot.send_message(chat_id=model_id, text=payload)
    except Exception:
        # si no puede mandar privado (no inició chat), no hacemos ruido en grupo
        pass


def handle_private_message(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type != "private":
        return

    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return

    # si LIVE ON
    l = live_state()
    if not l.get(str(uid), False):
        return

    # buscar grupo vinculado
    r = rooms()
    group_id = r.get(str(uid))
    if not group_id:
        return

    # traducir PT->DE
    translated = translate_text(text, source=MODEL_LANGUAGE, target=GROUP_LANGUAGE)
    name = get_model_name(uid)

    # Publicar en el grupo
    # Nota: Telegram no permite postear "como Aurora" sin cuenta/bot adicional; esto es lo máximo estable.
    payload = f"🔥 {name}:\n{translated}"
    try:
        context.bot.send_message(chat_id=int(group_id), text=payload, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# =========================
# Flask keep-alive
# =========================
web_app = Flask(__name__)

@web_app.get("/")
def index():
    return "OK"


def run_web():
    port = int(os.getenv("PORT", "10000"))
    web_app.run(host="0.0.0.0", port=port)


# =========================
# Main
# =========================
def main():
    # Flask en thread para Render
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whoami", cmd_whoami))

    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))

    dp.add_handler(CommandHandler("setstreamer", cmd_setstreamer))
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("bindhere", cmd_bindhere))

    # Grupo -> streamer
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_message))
    dp.add_handler(MessageHandler(Filters.chat_type.supergroup & Filters.text & ~Filters.command, handle_group_message))

    # Privado streamer -> grupo
    dp.add_handler(MessageHandler(Filters.chat_type.private & Filters.text & ~Filters.command, handle_private_message))

    updater.start_polling(drop_pending_updates=True)
    updater.idle()


if __name__ == "__main__":
    main()
