import json
import logging
import os
from threading import Thread
from typing import Dict, Optional, Any, List

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

ROOMS_FILE  = os.path.join(DATA_DIR, "rooms.json")    # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")   # model_user_id -> model_name
LIVE_FILE   = os.path.join(DATA_DIR, "live.json")     # model_user_id -> true/false
PROMOS_FILE = os.path.join(DATA_DIR, "promos.json")   # model_user_id -> [items...]

# Intervalo promos
PROMO_INTERVAL_MINUTES = int(os.environ.get("PROMO_INTERVAL_MINUTES", "120"))
PROMO_INTERVAL_SECONDS = PROMO_INTERVAL_MINUTES * 60

# Traducción (coloquial)
translator_de_to_pt = GoogleTranslator(source="de", target="pt")
translator_pt_to_de = GoogleTranslator(source="pt", target="de")

# Jobs de promos por modelo (en memoria)
PROMO_JOBS: Dict[int, Any] = {}

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

def load_promos() -> Dict[str, List[dict]]:
    return load_json(PROMOS_FILE, {})

def save_promos(d: Dict[str, List[dict]]):
    save_json(PROMOS_FILE, d)

def get_model_name(user_id: int) -> str:
    models = load_models()
    name = models.get(str(user_id))
    if name and isinstance(name, str) and name.strip():
        return name.strip()
    return "Model"

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
    """Encuentra qué modelo está vinculada a este grupo."""
    rooms = load_rooms()
    for uid_str, room_id in rooms.items():
        if int(room_id) == int(chat_id):
            return int(uid_str)
    return None

# ───────────────── Ajustes “coloquial” ─────────────────

def make_german_more_casual(text: str) -> str:
    """
    Ajuste ligero para sonar menos formal.
    """
    t = text.strip()
    # cambios simples / suaves
    t = t.replace("Sie ", "du ").replace(" Ihnen", " dir").replace("Ihr ", "dein ")
    return t

def normalize_pt_before_translate(text: str) -> str:
    """
    Pequeños arreglos para evitar traducciones raras.
    (Aquí puedes meter excepciones puntuales si lo necesitas.)
    """
    t = text.strip()
    # ejemplo: evitar palabras ambiguas si en tu caso se repite mucho
    # t = t.replace("consolador", "vibrador")  # si quieres
    return t

def normalize_de_after_translate(text: str) -> str:
    t = text.strip()
    return t

# ───────────────── PROMOS: cola por modelo ─────────────────

def add_promo_item(model_id: int, item: dict) -> None:
    promos = load_promos()
    q = promos.get(str(model_id), [])
    q.append(item)
    promos[str(model_id)] = q
    save_promos(promos)

def get_promo_count(model_id: int) -> int:
    promos = load_promos()
    q = promos.get(str(model_id), [])
    return len(q)

def pop_next_promo(model_id: int) -> Optional[dict]:
    promos = load_promos()
    q = promos.get(str(model_id), [])
    if not q:
        return None
    item = q.pop(0)
    promos[str(model_id)] = q
    save_promos(promos)
    return item

def clear_promos(model_id: int) -> None:
    promos = load_promos()
    promos[str(model_id)] = []
    save_promos(promos)

def stop_promo_job(model_id: int):
    global PROMO_JOBS
    job = PROMO_JOBS.get(model_id)
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass
        PROMO_JOBS.pop(model_id, None)

def ensure_promo_job(context: CallbackContext, model_id: int):
    """
    Inicia job si:
    - Modelo está LIVE ON
    - Tiene grupo vinculado
    - Hay cola de promos
    Y no existe job aún.
    """
    if not is_live(model_id):
        return
    room = get_room_for_model(model_id)
    if not room:
        return
    if get_promo_count(model_id) <= 0:
        return

    global PROMO_JOBS
    if model_id in PROMO_JOBS:
        return

    job = context.job_queue.run_repeating(
        promo_post_job,
        interval=PROMO_INTERVAL_SECONDS,
        first=PROMO_INTERVAL_SECONDS,
        context=model_id,
        name=f"promos_{model_id}",
    )
    PROMO_JOBS[model_id] = job
    logger.info(f"[PROMOS] Job iniciado para model_id={model_id} cada {PROMO_INTERVAL_MINUTES} min.")

def promo_post_job(context: CallbackContext):
    """
    Cada 120 min:
    - Si LIVE OFF → parar job
    - Si cola vacía → parar job
    - Si hay item → publicar en grupo vinculado
    """
    model_id = int(context.job.context)
    bot = context.bot

    if not is_live(model_id):
        stop_promo_job(model_id)
        return

    room = get_room_for_model(model_id)
    if not room:
        stop_promo_job(model_id)
        return

    item = pop_next_promo(model_id)
    if not item:
        stop_promo_job(model_id)
        try:
            bot.send_message(chat_id=model_id, text="✅ Cola de promos vacía. Se detuvo el envío automático.")
        except Exception:
            pass
        return

    kind = item.get("kind")
    file_id = item.get("file_id")
    caption = item.get("caption") or ""

    try:
        if kind == "photo":
            bot.send_photo(chat_id=room, photo=file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif kind == "video":
            bot.send_video(chat_id=room, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
        else:
            bot.send_message(chat_id=room, text=caption, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"[PROMOS] Error publicando promo en grupo {room}: {e}")

    # si después de publicar queda vacío, parar
    if get_promo_count(model_id) <= 0:
        stop_promo_job(model_id)
        try:
            bot.send_message(chat_id=model_id, text="✅ Última promo enviada. Cola vacía, envío automático detenido.")
        except Exception:
            pass

# ───────────────── COMANDOS ─────────────────

def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "✅ CosplayLive Translate Bot listo.\n\n"
        "SETUP (una vez):\n"
        "1) En el GRUPO del live (discusión): /bindchat\n"
        "2) En PRIVADO: /setmodel <Nombre>\n\n"
        "LIVE:\n"
        "3) En PRIVADO: /liveon (activa traducción + promos)\n"
        "4) En PRIVADO: /liveoff\n\n"
        "PROMOS (privado):\n"
        "• Envía foto/video con caption PT\n"
        "• Responde a esa media con: /add\n"
        "• /queue = ver cuántas promos hay\n"
        "• /postnow = mandar la siguiente ahora\n"
        "• /clearqueue = vaciar cola\n",
    )

def cmd_whereami(update: Update, context: CallbackContext):
    chat = update.effective_chat
    update.message.reply_text(f"Chat ID: {chat.id} | type: {chat.type}")

def cmd_bindchat(update: Update, context: CallbackContext):
    """
    EN EL GRUPO del live.
    Vincula ESTE grupo al user_id que ejecuta /bindchat (modelo).
    """
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type not in ("group", "supergroup"):
        update.message.reply_text("❌ /bindchat se usa en el GRUPO del live, no en privado.")
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
    """EN PRIVADO: guarda nombre por user_id."""
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
    EN PRIVADO: activa traducción y, si hay cola, activa job de promos.
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
            "Ve al GRUPO del live (discusión) y escribe:\n"
            "/bindchat\n\n"
            "Luego vuelve aquí y escribe /liveon."
        )
        return

    set_live(uid, True)
    name = get_model_name(uid)

    # iniciar job si ya hay cola
    ensure_promo_job(context, uid)

    update.message.reply_text(
        f"🔥 LIVE ON para <b>{name}</b>.\n"
        "✅ Traducción activada.\n"
        f"✅ Promos: se enviarán cada {PROMO_INTERVAL_MINUTES} minutos (si hay cola).\n\n"
        "Ahora:\n"
        "• Lo que escribas aquí (PT) → lo publico en el grupo (DE)\n"
        "• Lo que escriban en el grupo (DE) → te lo mando aquí (PT)\n\n"
        "PROMOS:\n"
        "• Envíame una foto/video con caption en portugués.\n"
        "• Responde a esa media con /add.\n",
        parse_mode=ParseMode.HTML
    )

def cmd_liveoff(update: Update, context: CallbackContext):
    """
    EN PRIVADO: apaga traducción y detiene promos.
    """
    chat = update.effective_chat
    if not chat or chat.type != "private":
        update.message.reply_text("❌ /liveoff se usa en PRIVADO conmigo.")
        return
    uid = update.effective_user.id
    set_live(uid, False)
    stop_promo_job(uid)
    update.message.reply_text("⛔ LIVE OFF. Traducción desactivada y promos detenidas.")

# ───────────────── PROMOS: comandos ─────────────────

def cmd_add(update: Update, context: CallbackContext):
    """
    EN PRIVADO:
    - La modelo envía una foto/video con caption PT
    - Responde a esa media con /add
    El bot guarda la media y el caption traducido a DE.
    """
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type != "private":
        msg.reply_text("❌ /add se usa en PRIVADO y respondiendo a una foto/video.")
        return

    uid = update.effective_user.id

    if not msg.reply_to_message:
        msg.reply_text("Usa /add respondiendo a una foto o video que me enviaste.")
        return

    m = msg.reply_to_message
    caption_pt = (m.caption or "").strip()

    caption_de = ""
    if caption_pt:
        try:
            caption_pt2 = normalize_pt_before_translate(caption_pt)
            caption_de = translator_pt_to_de.translate(caption_pt2)
            caption_de = normalize_de_after_translate(caption_de)
            caption_de = make_german_more_casual(caption_de)
        except Exception as e:
            logger.error(f"Error traduciendo caption PT->DE en /add: {e}")
            caption_de = caption_pt

    if m.photo:
        file_id = m.photo[-1].file_id
        item = {"kind": "photo", "file_id": file_id, "caption": caption_de}
        add_promo_item(uid, item)
        msg.reply_text(f"✅ Foto agregada a la cola. Total: {get_promo_count(uid)}")
    elif m.video:
        file_id = m.video.file_id
        item = {"kind": "video", "file_id": file_id, "caption": caption_de}
        add_promo_item(uid, item)
        msg.reply_text(f"✅ Video agregado a la cola. Total: {get_promo_count(uid)}")
    else:
        msg.reply_text("Ese mensaje no tiene foto ni video. Envíame media y responde con /add.")
        return

    # si está live, asegurar job
    ensure_promo_job(context, uid)

def cmd_queue(update: Update, context: CallbackContext):
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type != "private":
        msg.reply_text("❌ /queue se usa en PRIVADO.")
        return
    uid = update.effective_user.id
    msg.reply_text(f"📦 Promos en cola: {get_promo_count(uid)}")

def cmd_clearqueue(update: Update, context: CallbackContext):
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type != "private":
        msg.reply_text("❌ /clearqueue se usa en PRIVADO.")
        return
    uid = update.effective_user.id
    clear_promos(uid)
    stop_promo_job(uid)
    msg.reply_text("🧹 Cola vaciada y envío automático detenido.")

def cmd_postnow(update: Update, context: CallbackContext):
    """
    EN PRIVADO: manda la siguiente promo AHORA.
    """
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type != "private":
        msg.reply_text("❌ /postnow se usa en PRIVADO.")
        return

    uid = update.effective_user.id
    room = get_room_for_model(uid)
    if not room:
        msg.reply_text("⚠️ No hay grupo vinculado. Usa /bindchat en el grupo primero.")
        return

    item = pop_next_promo(uid)
    if not item:
        msg.reply_text("📭 No hay promos en cola.")
        stop_promo_job(uid)
        return

    kind = item.get("kind")
    file_id = item.get("file_id")
    caption = item.get("caption") or ""

    try:
        if kind == "photo":
            context.bot.send_photo(chat_id=room, photo=file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif kind == "video":
            context.bot.send_video(chat_id=room, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
        else:
            context.bot.send_message(chat_id=room, text=caption, parse_mode=ParseMode.HTML)
        msg.reply_text("✅ Enviado ahora.")
    except Exception as e:
        logger.error(f"Error en /postnow: {e}")
        msg.reply_text("❌ Error enviando. Revisa logs.")

    # si aún está live y quedan promos, mantener job; si no, detener
    if get_promo_count(uid) <= 0:
        stop_promo_job(uid)
    else:
        ensure_promo_job(context, uid)

# ───────────────── TRADUCCIÓN ─────────────────

def handle_group_messages(update: Update, context: CallbackContext):
    """
    EN GRUPO:
    - Identifica modelo vinculada al grupo
    - Si LIVE ON: traduce DE->PT y manda al privado de la modelo,
      incluyendo el nombre del usuario.
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

def handle_model_private(update: Update, context: CallbackContext):
    """
    EN PRIVADO (modelo):
    - Si LIVE ON: traduce PT->DE y publica en el grupo vinculado.
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
        pt2 = normalize_pt_before_translate(text)
        de = translator_pt_to_de.translate(pt2)
        de = normalize_de_after_translate(de)
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

    # comandos básicos
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whereami", cmd_whereami))

    # setup
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))

    # live
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # promos
    dp.add_handler(CommandHandler("add", cmd_add))
    dp.add_handler(CommandHandler("queue", cmd_queue))
    dp.add_handler(CommandHandler("postnow", cmd_postnow))
    dp.add_handler(CommandHandler("clearqueue", cmd_clearqueue))

    # traducción
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_messages))
    dp.add_handler(MessageHandler(Filters.chat_type.private & Filters.text & ~Filters.command, handle_model_private))

    # Flask en hilo
    Thread(target=run_flask, daemon=True).start()

    updater.start_polling(drop_pending_updates=True)
    logger.info("CosplayLive Translate Bot running...")
    updater.idle()

if __name__ == "__main__":
    main()
