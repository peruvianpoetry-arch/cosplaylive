import os
import json
import logging
import random
from threading import Thread
from typing import Dict, Any, Optional

from flask import Flask
from deep_translator import GoogleTranslator

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, CallbackContext, filters

# ───────── LOGGING ─────────
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("translator_poster_bot")

# ───────── ENV ─────────
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN")

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")   # model_user_id -> room_chat_id (grupo live)
MODELS_FILE = os.path.join(DATA_DIR, "models.json") # model_user_id -> model_display_name
STATE_FILE = os.path.join(DATA_DIR, "state.json")   # last_user per model

# ───────── TRANSLATORS ─────────
# Usuarios escriben DE (o mezcla) -> modelo recibe PT
t_de_to_pt = GoogleTranslator(source="auto", target="pt")
# Modelo escribe PT -> sala recibe DE
t_pt_to_de = GoogleTranslator(source="auto", target="de")

# ───────── MINI GLOSARIO (para evitar cagadas tipo "consolador") ─────────
# Puedes ampliar esto cuando quieras.
GLOSSARY_PT_TO_DE = {
    "consolador": "Toy",
    "brinquedo": "Toy",
    "brinquedo sexual": "Toy",
}
GLOSSARY_DE_TO_PT = {
    "Spielzeug": "brinquedo",
    "Toy": "brinquedo",
}

def apply_glossary(text: str, mapping: Dict[str, str]) -> str:
    out = text
    for k, v in mapping.items():
        out = out.replace(k, v)
    return out

# ───────── PLANTILLAS VISTOSAS PARA POSTS ─────────
POST_TEMPLATES = [
    "🔥 <b>{model}</b> ist live & in Spiellaune 😈\n\n{msg}\n\n💬 Schreib im Chat…",
    "💥 <b>{model}</b> ist jetzt online 💥\n\n{msg}\n\n🔥 Wer ist auch da? 👀",
    "🥵 <b>{model}</b> bringt die Stimmung zum Kochen 🥵\n\n{msg}\n\n💋 Sag hallo!",
    "✨ <b>{model}</b> ist da… und heute wird’s wild ✨\n\n{msg}\n\n🔥🔥🔥",
]

# ───────── JSON HELPERS ─────────
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error("Error leyendo %s: %s", path, e)
        return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Error guardando %s: %s", path, e)

def rooms() -> Dict[str, int]:
    return load_json(ROOMS_FILE, {})

def save_rooms(data: Dict[str, int]):
    save_json(ROOMS_FILE, data)

def models() -> Dict[str, str]:
    return load_json(MODELS_FILE, {})

def save_models(data: Dict[str, str]):
    save_json(MODELS_FILE, data)

def state() -> Dict[str, Any]:
    return load_json(STATE_FILE, {"last_user": {}})

def save_state(data: Dict[str, Any]):
    save_json(STATE_FILE, data)

def get_model_name(model_id: int) -> str:
    m = models().get(str(model_id))
    return m.strip() if m else "Model"

def get_room_for_model(model_id: int) -> Optional[int]:
    return rooms().get(str(model_id))

# ───────── COMMANDS ─────────
def cmd_start(update: Update, context: CallbackContext):
    update.effective_message.reply_text(
        "✅ Translator+Poster Bot läuft.\n\n"
        "Setup (Model im PRIVATCHAT):\n"
        "1) /setmodel <Name>\n"
        "2) Geh in den Live-Chat (GRUPPE), schreib irgendwas, dann:\n"
        "   - Leite diese Nachricht an mich weiter\n"
        "   - Antworte darauf mit: /setroom\n\n"
        "Live:\n"
        "- User schreibt im Live-Chat -> ich sende PT Übersetzung an Model (DM)\n"
        "- Model schreibt mir (DM) -> ich poste DE Übersetzung in den Live-Chat\n\n"
        "Posting:\n"
        "- Model schickt Foto/Video in DM und schreibt danach /post <Text PT>\n"
        "  oder antwortet auf Media mit /post <Text PT>"
    )

def cmd_setmodel(update: Update, context: CallbackContext):
    if update.effective_chat.type != ChatType.PRIVATE:
        update.effective_message.reply_text("❗ Bitte /setmodel im Privat-Chat mit mir.")
        return
    if not context.args:
        update.effective_message.reply_text("Benutzung: /setmodel <Name>")
        return
    model_id = update.effective_user.id
    name = " ".join(context.args).strip()
    data = models()
    data[str(model_id)] = name
    save_models(data)
    update.effective_message.reply_text(f"✅ Name gespeichert: {name}")

def cmd_setroom(update: Update, context: CallbackContext):
    # Debe ser en privado, respondiendo a un mensaje reenviado desde el grupo
    if update.effective_chat.type != ChatType.PRIVATE:
        update.effective_message.reply_text("❗ Bitte /setroom im Privat-Chat mit mir.")
        return

    msg = update.effective_message
    if not msg.reply_to_message or not msg.reply_to_message.forward_from_chat:
        update.effective_message.reply_text(
            "So geht's:\n"
            "1) Geh in den Live-Chat (GRUPPE) und schreib irgendwas.\n"
            "2) Leite diese Nachricht an mich weiter.\n"
            "3) Antworte auf die weitergeleitete Nachricht mit /setroom\n"
        )
        return

    room = msg.reply_to_message.forward_from_chat
    model_id = update.effective_user.id
    data = rooms()
    data[str(model_id)] = room.id
    save_rooms(data)

    update.effective_message.reply_text(
        f"✅ Live-Chat verbunden:\n<b>{room.title}</b>\n(room_id: {room.id})",
        parse_mode=ParseMode.HTML
    )

def cmd_post(update: Update, context: CallbackContext):
    """
    Model en DM:
    - envía media (foto/video), luego /post texto
    - o responde al media con /post texto
    Se publica en room con texto traducido DE + plantilla vistosa.
    """
    if update.effective_chat.type != ChatType.PRIVATE:
        update.effective_message.reply_text("❗ /post solo en privado conmigo.")
        return

    model_id = update.effective_user.id
    room_id = get_room_for_model(model_id)
    if not room_id:
        update.effective_message.reply_text("⚠️ Primero configura tu sala con /setroom.")
        return

    if not context.args:
        update.effective_message.reply_text("Benutzung: /post <Text (PT)>")
        return

    # texto que escribe la modelo (PT)
    raw_pt = " ".join(context.args).strip()
    # glosario para evitar traducciones raras
    raw_pt = apply_glossary(raw_pt, GLOSSARY_PT_TO_DE)

    try:
        translated_de = t_pt_to_de.translate(raw_pt)
    except Exception as e:
        logger.error("Error traducción PT->DE: %s", e)
        translated_de = raw_pt  # fallback

    model_name = get_model_name(model_id)
    template = random.choice(POST_TEMPLATES)
    final_text = template.format(model=model_name, msg=translated_de)

    bot = context.bot
    msg = update.effective_message

    # Detectar media: en el mensaje actual o en reply
    media_msg = None
    if msg.photo or msg.video:
        media_msg = msg
    elif msg.reply_to_message and (msg.reply_to_message.photo or msg.reply_to_message.video):
        media_msg = msg.reply_to_message

    try:
        if media_msg and media_msg.photo:
            photo = media_msg.photo[-1]
            bot.send_photo(
                chat_id=room_id,
                photo=photo.file_id,
                caption=final_text,
                parse_mode=ParseMode.HTML
            )
        elif media_msg and media_msg.video:
            bot.send_video(
                chat_id=room_id,
                video=media_msg.video.file_id,
                caption=final_text,
                parse_mode=ParseMode.HTML
            )
        else:
            bot.send_message(
                chat_id=room_id,
                text=final_text,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error("Error posteando en sala: %s", e)
        update.effective_message.reply_text("❌ Error posteando en la sala (revisa permisos del bot).")
        return

    update.effective_message.reply_text("✅ Posteado en la sala.")

# ───────── LIVE TRANSLATION (ROOM -> MODEL DM) ─────────
def handle_room_message(update: Update, context: CallbackContext):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if msg.from_user and msg.from_user.is_bot:
        return

    room_id = update.effective_chat.id
    all_rooms = rooms()

    # Encontrar qué modelo tiene este room_id
    model_id = None
    for mid, rid in all_rooms.items():
        if int(rid) == int(room_id):
            model_id = int(mid)
            break
    if not model_id:
        return  # este grupo no está vinculado

    user = msg.from_user
    username = f"@{user.username}" if user and user.username else (user.full_name if user else "User")

    original = msg.text.strip()
    # aplicar glosario de DE->PT antes de traducir (por si hay palabras clave)
    original_for_translate = apply_glossary(original, GLOSSARY_DE_TO_PT)

    try:
        translated_pt = t_de_to_pt.translate(original_for_translate)
    except Exception as e:
        logger.error("Error traducción DE->PT: %s", e)
        translated_pt = original_for_translate

    # Guardar "último usuario" para respuestas
    st = state()
    st.setdefault("last_user", {})
    st["last_user"][str(model_id)] = {
        "username": username,
        "user_id": user.id if user else None,
        "ts": msg.date.isoformat() if msg.date else "",
        "room_id": room_id,
        "original": original,
    }
    save_state(st)

    # Enviar a la modelo en privado
    payload = (
        f"👤 <b>{username}</b>\n"
        f"💬 <b>DE:</b> {original}\n"
        f"🌍 <b>PT:</b> {translated_pt}"
    )
    try:
        context.bot.send_message(chat_id=model_id, text=payload, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("No pude enviar DM al modelo: %s", e)

# ───────── MODEL DM -> ROOM (PT -> DE) ─────────
def handle_model_private(update: Update, context: CallbackContext):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if msg.from_user and msg.from_user.is_bot:
        return

    if msg.text.startswith("/"):
        return  # comandos se manejan aparte

    model_id = update.effective_user.id
    room_id = get_room_for_model(model_id)
    if not room_id:
        update.effective_message.reply_text("⚠️ Primero configura tu sala con /setroom.")
        return

    model_name = get_model_name(model_id)

    raw_pt = msg.text.strip()
    raw_pt = apply_glossary(raw_pt, GLOSSARY_PT_TO_DE)

    try:
        translated_de = t_pt_to_de.translate(raw_pt)
    except Exception as e:
        logger.error("Error traducción PT->DE: %s", e)
        translated_de = raw_pt

    # Etiquetar al último usuario
    st = state()
    last = (st.get("last_user") or {}).get(str(model_id)) or {}
    tag = last.get("username", "")

    text = f"🌸 <b>{model_name}</b>"
    if tag:
        text += f" → <b>{tag}</b>"
    text += f"\n{translated_de}"

    try:
        context.bot.send_message(chat_id=room_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("Error enviando respuesta a sala: %s", e)
        update.effective_message.reply_text("❌ No pude publicar en la sala (permisos?).")

# ───────── FLASK KEEP ALIVE ─────────
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "OK - Translator+Poster Bot"

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# ───────── MAIN ─────────
def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))
    dp.add_handler(CommandHandler("setroom", cmd_setroom))
    dp.add_handler(CommandHandler("post", cmd_post))

    # Grupo live: leer mensajes y traducir a la modelo
    dp.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & (~filters.ChatType.PRIVATE), handle_room_message))

    # Privado modelo: responder (PT->DE) hacia la sala
    dp.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.ChatType.PRIVATE, handle_model_private))

    # Flask en hilo
    t = Thread(target=run_flask, daemon=True)
    t.start()

    updater.start_polling(drop_pending_updates=True)
    logger.info("Translator+Poster Bot läuft…")
    updater.idle()

if __name__ == "__main__":
    main()
