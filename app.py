import json
import logging
import os
from threading import Thread
from typing import Dict, Optional

from flask import Flask
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

from deep_translator import GoogleTranslator

# ───────────────── CONFIG BÁSICA ─────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cosplaylive_translate")

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_ANNOUNCE")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN (o TELEGRAM_ANNOUNCE) en Render")

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")   # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json") # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")     # model_user_id -> true/false

# Traducción (coloquial)
# Grupo suele ser alemán -> modelo portugués
translator_de_to_pt = GoogleTranslator(source="de", target="pt")
# Modelo portugués -> grupo alemán
translator_pt_to_de = GoogleTranslator(source="pt", target="de")

# ───────────────── UTILIDADES JSON ─────────────────

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error(f"Error cargando {path}: {e}")
        return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error guardando {path}: {e}")

def load_rooms() -> Dict[str, int]:
    return load_json(ROOMS_FILE, {})

def save_rooms(d: Dict[str, int]):
    save_json(ROOMS_FILE, d)

def load_models() -> Dict[str, str]:
    return load_json(MODELS_FILE, {})

def save_models(d: Dict[str, str]):
    save_json(MODELS_FILE, d)

def load_live() -> Dict[str, bool]:
    return load_json(LIVE_FILE, {})

def save_live(d: Dict[str, bool]):
    save_json(LIVE_FILE, d)

def get_model_name(user_id: int) -> str:
    models = load_models()
    return models.get(str(user_id), "Model")

def is_live(user_id: int) -> bool:
    live = load_live()
    return bool(live.get(str(user_id), False))

def set_live(user_id: int, on: bool):
    live = load_live()
    live[str(user_id)] = on
    save_live(live)

def get_room_for_model(user_id: int) -> Optional[int]:
    rooms = load_rooms()
    cid = rooms.get(str(user_id))
    return cid

def find_model_by_room(chat_id: int) -> Optional[int]:
    """Encuentra qué modelo está vinculada a este grupo."""
    rooms = load_rooms()
    for uid_str, room_id in rooms.items():
        if int(room_id) == int(chat_id):
            return int(uid_str)
    return None

# ───────────────── “COLOQUIALIZAR” ALEMÁN ─────────────────

def make_german_more_casual(text: str) -> str:
    """
    Ajuste ligero para que suene menos formal.
    No es perfecto, pero ayuda bastante.
    """
    t = text
    # cambios simples
    t = t.replace("Sie ", "du ").replace(" Ihnen", " dir").replace("Ihr ", "dein ")
    t = t.replace("Bitte", "Bitte")  # mantenemos
    return t

# ───────────────── COMANDOS ─────────────────

def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "✅ CosplayLive Translate Bot listo.\n\n"
        "Uso:\n"
        "1) En el GRUPO del live: /bindchat (una sola vez)\n"
        "2) En PRIVADO: /setmodel <Nombre>\n"
        "3) En PRIVADO: /liveon  (activa traducción)\n"
        "4) En PRIVADO: /liveoff (apaga)\n",
    )

def cmd_bindchat(update: Update, context: CallbackContext):
    """
    Se usa EN EL GRUPO del live.
    Guarda el chat_id del grupo vinculado A LA PERSONA QUE EJECUTA el comando.
    Lo ideal: que Aurora lo ejecute 1 vez.
    """
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        update.message.reply_text("❌ /bindchat se usa en el GRUPO del live (discusión), no en privado.")
        return

    rooms = load_rooms()
    rooms[str(user.id)] = int(chat.id)
    save_rooms(rooms)

    name = get_model_name(user.id)
    update.message.reply_text(
        f"✅ Vinculado.\n\n"
        f"Modelo: <b>{name}</b>\n"
        f"Grupo ID: <code>{chat.id}</code>\n\n"
        "Ahora activa traducción con /liveon en PRIVADO conmigo.",
        parse_mode=ParseMode.HTML,
    )

def cmd_setmodel(update: Update, context: CallbackContext):
    """Se usa EN PRIVADO. Guarda nombre por user_id."""
    chat = update.effective_chat
    if not chat or chat.type != "private":
        update.message.reply_text("❌ /setmodel se usa en PRIVADO conmigo.")
        return
    if not context.args:
        update.message.reply_text("Uso: /setmodel Aurora")
        return

    name = " ".join(context.args).strip()
    models = load_models()
    models[str(update.effective_user.id)] = name
    save_models(models)

    update.message.reply_text(f"✅ Nombre guardado: {name}")

def cmd_liveon(update: Update, context: CallbackContext):
    """Se usa EN PRIVADO. Activa traducción solo para esa modelo."""
    chat = update.effective_chat
    if not chat or chat.type != "private":
        update.message.reply_text("❌ /liveon se usa en PRIVADO conmigo.")
        return

    uid = update.effective_user.id
    room = get_room_for_model(uid)
    if not room:
        update.message.reply_text(
            "⚠️ Aún no hay grupo vinculado.\n\n"
            "Ve al GRUPO del live (discusión) y escribe allí:\n"
            "/bindchat\n\n"
            "Luego vuelve aquí y escribe /liveon."
        )
        return

    set_live(uid, True)
    name = get_model_name(uid)

    update.message.reply_text(
        f"🔥 LIVE ON para <b>{name}</b>.\n"
        "✅ Traducción activada.\n\n"
        "Ahora:\n"
        "• Lo que escribas aquí (PT) → lo publico en el grupo (DE)\n"
        "• Lo que escriban en el grupo (DE) → te lo mando aquí (PT)\n",
        parse_mode=ParseMode.HTML
    )

def cmd_liveoff(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if not chat or chat.type != "private":
        update.message.reply_text("❌ /liveoff se usa en PRIVADO conmigo.")
        return
    uid = update.effective_user.id
    set_live(uid, False)
    update.message.reply_text("⛔ LIVE OFF. Traducción desactivada.")

def cmd_whereami(update: Update, context: CallbackContext):
    chat = update.effective_chat
    update.message.reply_text(f"Chat ID: {chat.id} | type: {chat.type}")

# ───────────────── TRADUCCIÓN ─────────────────

def handle_group_messages(update: Update, context: CallbackContext):
    """
    En el GRUPO:
    - Si el grupo está vinculado a una modelo
    - y esa modelo está LIVE ON
    entonces: traducir DE->PT y mandar a la modelo (privado) con nombre del usuario.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        return

    model_id = find_model_by_room(chat.id)
    if not model_id:
        return
    if not is_live(model_id):
        return

    text = msg.text or ""
    if not text.strip():
        return

    try:
        pt = translator_de_to_pt.translate(text)
    except Exception as e:
        logger.error(f"Error traduciendo DE->PT: {e}")
        return

    username = f"@{user.username}" if user.username else user.first_name
    payload = (
        f"💬 <b>{username}</b>\n"
        f"🇩🇪 {text}\n"
        f"🇵🇹 {pt}"
    )

    try:
        context.bot.send_message(chat_id=model_id, text=payload, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error enviando a modelo en privado: {e}")

def handle_model_private(update: Update, context: CallbackContext):
    """
    En PRIVADO:
    - Solo si el usuario es una modelo que está LIVE ON
    entonces: traducir PT->DE y publicar en su grupo vinculado.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type != "private":
        return
    if user.is_bot:
        return

    uid = user.id
    if not is_live(uid):
        return

    room = get_room_for_model(uid)
    if not room:
        return

    text = msg.text or ""
    if not text.strip():
        return

    try:
        de = translator_pt_to_de.translate(text)
        de = make_german_more_casual(de)
    except Exception as e:
        logger.error(f"Error traduciendo PT->DE: {e}")
        return

    model_name = get_model_name(uid)
    out = f"🎙️ <b>{model_name}</b>: {de}"

    try:
        context.bot.send_message(chat_id=room, text=out, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error publicando en grupo: {e}")

# ───────────────── FLASK KEEP ALIVE ─────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "OK - CosplayLive Translate Bot"

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# ───────────────── MAIN ─────────────────

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # comandos
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whereami", cmd_whereami))
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # mensajes
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_messages))
    dp.add_handler(MessageHandler(Filters.chat_type.private & Filters.text & ~Filters.command, handle_model_private))

    # Flask en hilo
    Thread(target=run_flask, daemon=True).start()

    updater.start_polling(drop_pending_updates=True)
    logger.info("CosplayLive Translate Bot running...")
    updater.idle()

if __name__ == "__main__":
    main()
