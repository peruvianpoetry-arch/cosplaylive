import os
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from flask import Flask

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────────────────────
# CONFIG / LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cosplaylive")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Render (Environment).")

DATA_DIR = os.environ.get("DATA_DIR", "/var/data").strip()
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

AUTO_INTERVAL_MIN = int(os.environ.get("AUTO_INTERVAL_MIN", "120").strip())

ENABLE_TRANSLATION = os.environ.get("ENABLE_TRANSLATION", "1").strip() == "1"

# Idiomas fijos del proyecto:
# - Grupo se ve en Alemán (DE)
# - Modelo escribe en Portugués (PT)
LANG_GROUP = "de"
LANG_MODEL = "pt"

# Archivos de persistencia
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")    # group_chat_id, admin_user_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")    # model_user_id -> {username, name}
LIVE_FILE = os.path.join(DATA_DIR, "live.json")        # model_user_id -> true/false
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")      # model_user_id -> [items...]

# Job name por modelo
JOB_PREFIX = "promo_job_"

# ─────────────────────────────────────────────────────────────
# Flask (Render keep-alive)
# ─────────────────────────────────────────────────────────────
web = Flask(__name__)

@web.get("/")
def index():
    return "OK", 200

@web.get("/health")
def health():
    return "healthy", 200

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    web.run(host="0.0.0.0", port=port)

# ─────────────────────────────────────────────────────────────
# Helpers persistencia
# ─────────────────────────────────────────────────────────────
def _load_json(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.exception("Error leyendo %s: %s", path, e)
        return default

def _save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def get_config() -> Dict[str, Any]:
    return _load_json(CONFIG_FILE, {})

def set_config(cfg: Dict[str, Any]) -> None:
    _save_json(CONFIG_FILE, cfg)

def get_models() -> Dict[str, Any]:
    return _load_json(MODELS_FILE, {})

def set_models(m: Dict[str, Any]) -> None:
    _save_json(MODELS_FILE, m)

def get_live() -> Dict[str, Any]:
    return _load_json(LIVE_FILE, {})

def set_live(l: Dict[str, Any]) -> None:
    _save_json(LIVE_FILE, l)

def get_queue() -> Dict[str, List[Dict[str, Any]]]:
    return _load_json(QUEUE_FILE, {})

def set_queue(q: Dict[str, List[Dict[str, Any]]]) -> None:
    _save_json(QUEUE_FILE, q)

def str_id(x: int) -> str:
    return str(x)

# ─────────────────────────────────────────────────────────────
# Traducción
# ─────────────────────────────────────────────────────────────
def translate_text(text: str, source: str, target: str) -> str:
    if not text:
        return ""
    if not ENABLE_TRANSLATION:
        return text
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source=source, target=target).translate(text)
    except Exception as e:
        logger.warning("Fallo traducción (%s->%s): %s", source, target, e)
        return text

# ─────────────────────────────────────────────────────────────
# Utilidades de sesión
# ─────────────────────────────────────────────────────────────
def get_bound_group_id() -> Optional[int]:
    cfg = get_config()
    gid = cfg.get("group_chat_id")
    if isinstance(gid, int):
        return gid
    return None

def is_session_ready() -> bool:
    return get_bound_group_id() is not None

def get_model_name(user_id: int, username: str = "") -> str:
    models = get_models()
    info = models.get(str_id(user_id))
    if info and isinstance(info, dict):
        return info.get("name") or info.get("username") or username or "Modelo"
    return username or "Modelo"

def is_live_on(model_user_id: int) -> bool:
    live = get_live()
    return bool(live.get(str_id(model_user_id), False))

def set_live_state(model_user_id: int, state: bool) -> None:
    live = get_live()
    live[str_id(model_user_id)] = bool(state)
    set_live(live)

# ─────────────────────────────────────────────────────────────
# Comandos
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✅ Bot listo.\n\n"
        "En el *GRUPO*: /bindchat\n"
        "En privado (Aurora): /setmodel Aurora, /liveon, /liveoff\n",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)

async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"Chat ID: {chat.id}\nTipo: {chat.type}")

async def cmd_bindchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Se ejecuta dentro del GRUPO (donde la gente chatea)
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Este comando se usa *dentro del grupo*.", parse_mode="Markdown")
        return

    cfg = get_config()
    cfg["group_chat_id"] = chat.id
    cfg["admin_user_id"] = user.id
    set_config(cfg)

    await update.message.reply_text(
        "✅ Grupo configurado.\n"
        "Ahora: Aurora debe escribirle al bot en privado /start y luego /setmodel Aurora.\n"
        "Después tú activas /liveon desde la cuenta de Aurora (una sola vez mientras estés)."
    )

async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Aurora lo usa en privado: /setmodel Aurora
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Usa /setmodel en privado conmigo.")
        return

    if not is_session_ready():
        await update.message.reply_text("No hay sesión configurada. Primero ejecuta /bindchat en el grupo.")
        return

    user = update.effective_user
    username = user.username or ""
    name = " ".join(context.args).strip()
    if not name:
        name = username or "Aurora"

    models = get_models()
    models[str_id(user.id)] = {"username": username, "name": name}
    set_models(models)

    await update.message.reply_text(
        f"✅ Modelo configurada: *{name}*\n"
        f"Usuario: @{username}" if username else f"✅ Modelo configurada: *{name}*",
        parse_mode="Markdown",
    )

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Usa /liveon en privado conmigo.")
        return

    if not is_session_ready():
        await update.message.reply_text("No hay sesión configurada. Primero /bindchat en el grupo.")
        return

    user = update.effective_user
    set_live_state(user.id, True)

    # Programar job promo
    job_name = JOB_PREFIX + str_id(user.id)
    # eliminar si existe
    for j in context.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()

    context.job_queue.run_repeating(
        promo_job_tick,
        interval=AUTO_INTERVAL_MIN * 60,
        first=10,  # arranca rápido (10s) para probar
        name=job_name,
        data={"model_user_id": user.id},
    )

    await update.message.reply_text(
        f"✅ LIVE ON.\n"
        f"⏱️ Publicación automática cada {AUTO_INTERVAL_MIN} minutos (si hay cola)."
    )

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Usa /liveoff en privado conmigo.")
        return

    user = update.effective_user
    set_live_state(user.id, False)

    # detener job
    job_name = JOB_PREFIX + str_id(user.id)
    for j in context.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()

    await update.message.reply_text("⛔ LIVE OFF. Se detuvo la publicación automática y traducciones hacia el grupo.")

# ─────────────────────────────────────────────────────────────
# Manejo de mensajes (traducción)
# ─────────────────────────────────────────────────────────────
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Mensajes de usuarios en el grupo -> traducir DE->PT y enviar a Aurora por privado
    if not update.message:
        return

    group_id = get_bound_group_id()
    if group_id is None:
        return

    if update.effective_chat.id != group_id:
        return

    # ignorar mensajes del propio bot
    if update.effective_user and update.effective_user.is_bot:
        return

    text = update.message.text or update.message.caption or ""
    if not text.strip():
        return

    # Encontrar a la modelo activa (liveon) (solo 1 modelo para este setup)
    live = get_live()
    model_user_id = None
    for k, v in live.items():
        if v is True:
            model_user_id = int(k)
            break
    if model_user_id is None:
        return  # liveoff = no enviamos a modelo

    user = update.effective_user
    display = user.full_name if user else "Usuario"
    pt = translate_text(text, source=LANG_GROUP, target=LANG_MODEL)

    msg_to_model = f"👤 {display}:\n{pt}"
    try:
        await context.bot.send_message(chat_id=model_user_id, text=msg_to_model)
    except Exception as e:
        logger.warning("No pude enviar a modelo: %s", e)

async def handle_private_text_from_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Aurora en privado escribe PT -> publicar en grupo en DE
    if not update.message:
        return

    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    if not user:
        return

    if not is_session_ready():
        await update.message.reply_text("No hay sesión configurada. Primero /bindchat en el grupo.")
        return

    if not is_live_on(user.id):
        return  # liveoff no publica

    text = update.message.text or ""
    if not text.strip():
        return

    group_id = get_bound_group_id()
    if group_id is None:
        await update.message.reply_text("No hay sesión configurada.")
        return

    de = translate_text(text, source=LANG_MODEL, target=LANG_GROUP)
    model_name = get_model_name(user.id, username=user.username or "")

    out = f"🔥 {model_name}:\n{de}"
    try:
        await context.bot.send_message(chat_id=group_id, text=out)
    except Exception as e:
        logger.warning("No pude publicar en grupo: %s", e)

# ─────────────────────────────────────────────────────────────
# Cola de promos: Aurora manda foto/video en privado -> se guarda
# ─────────────────────────────────────────────────────────────
async def handle_private_media_from_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    if not user:
        return

    if not is_session_ready():
        await update.message.reply_text("No hay sesión configurada. Primero /bindchat en el grupo.")
        return

    # Solo aceptamos cuando ella está liveon (tu preferencia)
    if not is_live_on(user.id):
        await update.message.reply_text("Ahora estás en LIVE OFF. Usa /liveon para activar cola y publicaciones.")
        return

    msg = update.message
    item: Dict[str, Any] = {
        "ts": int(time.time()),
        "caption_pt": (msg.caption or "").strip(),
        "type": None,
        "file_id": None,
    }

    if msg.photo:
        item["type"] = "photo"
        item["file_id"] = msg.photo[-1].file_id
    elif msg.video:
        item["type"] = "video"
        item["file_id"] = msg.video.file_id
    elif msg.animation:
        item["type"] = "animation"
        item["file_id"] = msg.animation.file_id
    else:
        return

    q = get_queue()
    key = str_id(user.id)
    q.setdefault(key, [])
    q[key].append(item)
    set_queue(q)

    await update.message.reply_text(
        f"✅ Guardado en cola.\n"
        f"📦 En cola ahora: {len(q[key])}\n"
        f"⏱️ Se publicará automáticamente cada {AUTO_INTERVAL_MIN} min."
    )

# ─────────────────────────────────────────────────────────────
# Job: cada X min publica 1 item de la cola al grupo
# ─────────────────────────────────────────────────────────────
async def promo_job_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    model_user_id = int(data.get("model_user_id", 0))
    if model_user_id <= 0:
        return

    if not is_live_on(model_user_id):
        return

    group_id = get_bound_group_id()
    if group_id is None:
        return

    q = get_queue()
    key = str_id(model_user_id)
    items = q.get(key, [])
    if not items:
        return  # cola vacía: no hace nada

    item = items.pop(0)
    q[key] = items
    set_queue(q)

    caption_pt = (item.get("caption_pt") or "").strip()
    caption_de = translate_text(caption_pt, source=LANG_MODEL, target=LANG_GROUP) if caption_pt else ""
    model_name = get_model_name(model_user_id)

    final_caption = f"📣 {model_name}\n{caption_de}".strip()

    try:
        t = item.get("type")
        file_id = item.get("file_id")
        if t == "photo":
            await context.bot.send_photo(chat_id=group_id, photo=file_id, caption=final_caption if final_caption else None)
        elif t == "video":
            await context.bot.send_video(chat_id=group_id, video=file_id, caption=final_caption if final_caption else None)
        elif t == "animation":
            await context.bot.send_animation(chat_id=group_id, animation=file_id, caption=final_caption if final_caption else None)
    except Exception as e:
        logger.warning("Fallo publicando promo: %s", e)

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    # Flask thread
    import threading
    web_thread = threading.Thread(target=run_flask, daemon=True)
    web_thread.start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # comandos
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("whereami", cmd_whereami))
    application.add_handler(CommandHandler("bindchat", cmd_bindchat))
    application.add_handler(CommandHandler("setmodel", cmd_setmodel))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # grupo: mensajes de texto/caption -> a modelo en PT
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CaptionRegex(r".+")),
            handle_group_message,
        )
    )

    # privado: texto de Aurora -> a grupo en DE
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private_text_from_model)
    )

    # privado: medios de Aurora (foto/video/gif) -> cola
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.ANIMATION),
            handle_private_media_from_model,
        )
    )

    logger.info("Bot iniciado (polling). Intervalo promo: %s min", AUTO_INTERVAL_MIN)
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
