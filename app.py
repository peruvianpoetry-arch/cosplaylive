import json
import logging
import os
import random
from threading import Thread
from typing import Dict, Optional, List, Any

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

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")    # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")  # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")      # model_user_id -> true/false
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")    # model_user_id -> [items...]

# Intervalo de publicación (2 horas)
QUEUE_INTERVAL_SECONDS = int(os.environ.get("QUEUE_INTERVAL_SECONDS", str(120 * 60)))

# Traducción (coloquial)
translator_de_to_pt = GoogleTranslator(source="de", target="pt")
translator_pt_to_de = GoogleTranslator(source="pt", target="de")

# Frases sexy automáticas (DE) cuando no hay caption
AUTO_CAPTIONS_DE = [
    "🔥 Hey ihr Süßen… ich bin jetzt da 😈💋",
    "🥵 Ich hab heute richtig Lust auf euch… schreibt mir was Heißes 🔥",
    "💃 Komm näher… ich zeig euch gleich, worauf ich Bock hab 😏",
    "😈 Heute wird’s frech… wer traut sich zuerst zu schreiben? 💋",
    "🔥 Ich will euch hören… was wollt ihr gerade mit mir machen? 😘",
    "🥵 Ich bin in Stimmung… seid ihr auch so schön versaut? 😈",
    "💋 Ich hab euch vermisst… jetzt wird’s richtig heiß hier 🔥",
]

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

def load_queue() -> Dict[str, List[Dict[str, Any]]]:
    return load_json(QUEUE_FILE, {})

def save_queue(d: Dict[str, List[Dict[str, Any]]]):
    save_json(QUEUE_FILE, d)

def get_model_name(user_id: int) -> str:
    models = load_models()
    name = models.get(str(user_id))
    return name.strip() if isinstance(name, str) and name.strip() else "Model"

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
    return int(cid) if cid is not None else None

def find_model_by_room(chat_id: int) -> Optional[int]:
    rooms = load_rooms()
    for uid_str, room_id in rooms.items():
        try:
            if int(room_id) == int(chat_id):
                return int(uid_str)
        except Exception:
            continue
    return None

# ───────────────── TEXTO “MÁS CASUAL” EN DE ─────────────────

def make_german_more_casual(text: str) -> str:
    """
    Ajuste simple para mantener tono informal y evitar mezcla Sie/du.
    OJO: no es perfecto, pero reduce muchísimo el 'Sie'.
    """
    t = text

    # Cambios típicos Sie -> du/ihr
    replacements = [
        ("Sie sind", "du bist"),
        ("Sind Sie", "Bist du"),
        ("Haben Sie", "Hast du"),
        ("Können Sie", "Kannst du"),
        ("Möchten Sie", "Willst du"),
        ("Wollen Sie", "Willst du"),
        ("Ihr ", "dein "),
        ("Ihnen", "dir"),
        ("Sie ", "du "),
        ("Sie?", "du?"),
        ("Sie!", "du!"),
        ("Sie.", "du."),
    ]
    for a, b in replacements:
        t = t.replace(a, b)

    # Si el mensaje claramente es para varios (grupo), preferimos "ihr"
    # (pequeño truco: si aparece "euch" o "ihr", no tocamos)
    return t

# ───────────────── COLA DE MEDIA ─────────────────

def enqueue_media(model_id: int, item: Dict[str, Any]) -> int:
    q = load_queue()
    key = str(model_id)
    if key not in q:
        q[key] = []
    q[key].append(item)
    save_queue(q)
    return len(q[key])

def pop_next_media(model_id: int) -> Optional[Dict[str, Any]]:
    q = load_queue()
    key = str(model_id)
    items = q.get(key) or []
    if not items:
        return None
    item = items.pop(0)
    q[key] = items
    save_queue(q)
    return item

def queue_size(model_id: int) -> int:
    q = load_queue()
    items = q.get(str(model_id)) or []
    return len(items)

# ───────────────── COMANDOS ─────────────────

def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "✅ CosplayLive Translate Bot listo.\n\n"
        "Uso rápido:\n"
        "1) En el GRUPO: /bindchat (1 vez)\n"
        "2) En PRIVADO: /setmodel <Nombre> (1 vez)\n"
        "3) En PRIVADO: /liveon  (activa traducción + cola)\n"
        "4) En PRIVADO: /liveoff (apaga)\n\n"
        "Media:\n"
        "• Foto/Video con #cola -> se publica cada 120 min\n"
        "• Foto/Video sin #cola -> se publica al instante\n",
    )

def cmd_whoami(update: Update, context: CallbackContext):
    u = update.effective_user
    update.message.reply_text(f"✅ ID: {u.id} | user: @{u.username}" if u.username else f"✅ ID: {u.id}")

def cmd_whereami(update: Update, context: CallbackContext):
    chat = update.effective_chat
    update.message.reply_text(f"Chat ID: {chat.id} | type: {chat.type}")

def cmd_bindchat(update: Update, context: CallbackContext):
    """
    Se usa EN EL GRUPO del live.
    Vincula este grupo al usuario que ejecuta el comando (modelo).
    """
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type not in ("group", "supergroup"):
        update.message.reply_text("❌ /bindchat se usa en el GRUPO (discusión), no en privado.")
        return

    rooms = load_rooms()
    rooms[str(user.id)] = int(chat.id)
    save_rooms(rooms)

    name = get_model_name(user.id)
    update.message.reply_text(
        f"✅ Vinculado.\n"
        f"Modelo: <b>{name}</b>\n"
        f"Grupo ID: <code>{chat.id}</code>\n\n"
        "Ahora ve a PRIVADO conmigo y usa: /liveon",
        parse_mode=ParseMode.HTML,
    )

def cmd_setmodel(update: Update, context: CallbackContext):
    """
    Se usa EN PRIVADO. Guarda nombre por user_id.
    """
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
    """
    Se usa EN PRIVADO. Activa traducción + job de cola (cada 2h) para esa modelo.
    """
    chat = update.effective_chat
    if not chat or chat.type != "private":
        update.message.reply_text("❌ /liveon se usa en PRIVADO conmigo.")
        return

    uid = update.effective_user.id
    room = get_room_for_model(uid)
    if not room:
        update.message.reply_text(
            "⚠️ Aún no hay grupo vinculado.\n\n"
            "Ve al GRUPO del live y escribe allí:\n"
            "/bindchat\n\n"
            "Luego vuelve aquí y escribe /liveon."
        )
        return

    set_live(uid, True)

    # Crear/recrear job de cola
    job_name = f"queue_job_{uid}"
    # borrar si existe
    for j in context.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()

    context.job_queue.run_repeating(
        queue_post_job,
        interval=QUEUE_INTERVAL_SECONDS,
        first=QUEUE_INTERVAL_SECONDS,
        context={"model_id": uid},
        name=job_name,
    )

    name = get_model_name(uid)
    update.message.reply_text(
        f"🔥 LIVE ON para <b>{name}</b>.\n\n"
        "✅ Traducción activada.\n"
        "✅ Cola activada (cada 120 min).\n\n"
        "Cómo usar:\n"
        "• Texto aquí (PT) -> sale en el grupo (DE)\n"
        "• Mensajes del grupo (DE) -> te llegan aquí (PT)\n\n"
        "Media:\n"
        "• Foto/Video con #cola -> se programa\n"
        "• Foto/Video sin #cola -> sale al instante\n",
        parse_mode=ParseMode.HTML,
    )

def cmd_liveoff(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if not chat or chat.type != "private":
        update.message.reply_text("❌ /liveoff se usa en PRIVADO conmigo.")
        return

    uid = update.effective_user.id
    set_live(uid, False)

    # parar job de cola
    job_name = f"queue_job_{uid}"
    for j in context.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()

    update.message.reply_text("⛔ LIVE OFF. Traducción y cola desactivadas.")

def cmd_queuesize(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if not chat or chat.type != "private":
        update.message.reply_text("❌ /queuesize se usa en PRIVADO.")
        return
    uid = update.effective_user.id
    n = queue_size(uid)
    update.message.reply_text(f"📦 En cola: {n}")

# ───────────────── JOB: PUBLICAR COLA ─────────────────

def queue_post_job(context: CallbackContext):
    """
    Cada 120 min: si hay items en cola y la modelo está LIVE ON,
    publica 1 item en el grupo.
    """
    data = context.job.context or {}
    model_id = int(data.get("model_id", 0))
    if not model_id:
        return

    if not is_live(model_id):
        return

    room = get_room_for_model(model_id)
    if not room:
        return

    item = pop_next_media(model_id)
    if not item:
        return

    item_type = item.get("type")
    file_id = item.get("file_id")
    caption_pt = (item.get("caption_pt") or "").strip()

    # preparar caption final (DE)
    if caption_pt:
        try:
            caption_de = translator_pt_to_de.translate(caption_pt)
            caption_de = make_german_more_casual(caption_de)
        except Exception as e:
            logger.error(f"[queue_post_job] Error PT->DE: {e}")
            caption_de = ""
    else:
        caption_de = ""

    if not caption_de:
        caption_de = random.choice(AUTO_CAPTIONS_DE)

    model_name = get_model_name(model_id)
    out_caption = f"🎙️ <b>{model_name}</b>\n{caption_de}"

    try:
        if item_type == "photo":
            context.bot.send_photo(chat_id=room, photo=file_id, caption=out_caption, parse_mode=ParseMode.HTML)
        elif item_type == "video":
            context.bot.send_video(chat_id=room, video=file_id, caption=out_caption, parse_mode=ParseMode.HTML)
        else:
            # fallback por si algo raro
            context.bot.send_message(chat_id=room, text=out_caption, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"[queue_post_job] Error publicando en grupo: {e}")

# ───────────────── TRADUCCIÓN EN GRUPO ─────────────────

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

    username = f"@{user.username}" if user.username else (user.first_name or "User")
    payload = (
        f"💬 <b>{username}</b>\n"
        f"🇩🇪 {text}\n"
        f"🇵🇹 {pt}"
    )

    try:
        context.bot.send_message(chat_id=model_id, text=payload, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error enviando a modelo en privado: {e}")

# ───────────────── TRADUCCIÓN EN PRIVADO (TEXTO) ─────────────────

def handle_model_private_text(update: Update, context: CallbackContext):
    """
    En PRIVADO (texto):
    - Solo si el usuario está LIVE ON
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

# ───────────────── MEDIA EN PRIVADO (FOTO/VIDEO) ─────────────────

def handle_model_private_media(update: Update, context: CallbackContext):
    """
    En PRIVADO (foto/video):
    - Solo si el usuario está LIVE ON
    - Si caption contiene #cola -> se encola y se publica cada 2 horas
    - Si NO -> se publica al instante
    - Si caption vacío -> frase sexy automática (DE)
    - Si caption en PT -> se traduce a DE y se publica
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
        # si no está live, ignoramos para no “spamear” por accidente
        return

    room = get_room_for_model(uid)
    if not room:
        return

    # detectar media
    media_type = None
    file_id = None

    if msg.photo:
        media_type = "photo"
        file_id = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"
        file_id = msg.video.file_id
    else:
        return

    caption_raw = (msg.caption or "").strip()
    caption_lower = caption_raw.lower()

    send_mode = "instant"
    clean_caption = caption_raw

    if "#cola" in caption_lower:
        send_mode = "queue"
        clean_caption = clean_caption.replace("#cola", "").strip()
    if "#ahora" in caption_lower:
        send_mode = "instant"
        clean_caption = clean_caption.replace("#ahora", "").strip()

    # Si es cola: guardar y listo
    if send_mode == "queue":
        n = enqueue_media(uid, {
            "type": media_type,
            "file_id": file_id,
            "caption_pt": clean_caption
        })
        msg.reply_text(f"📦 Guardado en cola. En cola ahora: {n}")
        return

    # Publicación inmediata
    if clean_caption:
        try:
            caption_de = translator_pt_to_de.translate(clean_caption)
            caption_de = make_german_more_casual(caption_de)
        except Exception as e:
            logger.error(f"Error traduciendo caption PT->DE: {e}")
            caption_de = ""
    else:
        caption_de = ""

    if not caption_de:
        caption_de = random.choice(AUTO_CAPTIONS_DE)

    model_name = get_model_name(uid)
    out_caption = f"🎙️ <b>{model_name}</b>\n{caption_de}"

    try:
        if media_type == "photo":
            context.bot.send_photo(chat_id=room, photo=file_id, caption=out_caption, parse_mode=ParseMode.HTML)
        else:
            context.bot.send_video(chat_id=room, video=file_id, caption=out_caption, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error publicando media inmediata: {e}")

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
    dp.add_handler(CommandHandler("whoami", cmd_whoami))
    dp.add_handler(CommandHandler("whereami", cmd_whereami))
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))
    dp.add_handler(CommandHandler("queuesize", cmd_queuesize))

    # mensajes en grupo (solo texto)
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_messages))

    # privado: media primero (foto/video)
    dp.add_handler(MessageHandler(Filters.chat_type.private & (Filters.photo | Filters.video), handle_model_private_media))

    # privado: texto
    dp.add_handler(MessageHandler(Filters.chat_type.private & Filters.text & ~Filters.command, handle_model_private_text))

    # Flask en hilo
    Thread(target=run_flask, daemon=True).start()

    updater.start_polling(drop_pending_updates=True)
    logger.info("CosplayLive Translate Bot running...")
    updater.idle()

if __name__ == "__main__":
    main()
