import os
import json
import time
import queue
import threading
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List

from flask import Flask, Response, send_from_directory

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("cosplaylive")

# =========================
# ENV / CONFIG
# =========================
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN (o TELEGRAM_TOKEN) en Render Environment.")

GROUP_LANGUAGE = os.environ.get("GROUP_LANGUAGE", "de")  # idioma del grupo (alemán)
MODEL_LANGUAGE = os.environ.get("MODEL_LANGUAGE", "pt")  # idioma de Aurora (portugués)

TZ = os.environ.get("TZ", "Europe/Berlin")

AUTO_INTERVAL_MIN = int(os.environ.get("AUTO_INTERVAL_MIN", "120"))  # cada 2h por defecto

ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID")
ADMIN_USER_ID = int(ADMIN_USER_ID) if ADMIN_USER_ID and ADMIN_USER_ID.isdigit() else None

# Para Render/Flask
PORT = int(os.environ.get("PORT", "10000"))

# =========================
# FILES
# =========================
ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")   # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json") # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")     # model_user_id -> {"on": bool}
PROMOS_FILE = os.path.join(DATA_DIR, "promos.json") # model_user_id -> list[promo]

# promo item ejemplo:
# {"type":"photo"|"video", "file_id":"...", "caption_pt":"...", "ts": 1234567890}

# =========================
# SMALL PERSISTENCE HELPERS
# =========================
def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception(f"Error leyendo {path}")
    return default

def _save_json(path: str, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        logger.exception(f"Error guardando {path}")

rooms: Dict[str, str] = _load_json(ROOMS_FILE, {})
models: Dict[str, str] = _load_json(MODELS_FILE, {})
live_state: Dict[str, Dict[str, Any]] = _load_json(LIVE_FILE, {})
promos: Dict[str, List[Dict[str, Any]]] = _load_json(PROMOS_FILE, {})

def is_live(model_id: str) -> bool:
    return bool(live_state.get(model_id, {}).get("on", False))

def set_live(model_id: str, on: bool):
    live_state[model_id] = {"on": bool(on), "updated": int(time.time())}
    _save_json(LIVE_FILE, live_state)

def get_group_chat_id(model_id: str) -> Optional[int]:
    cid = rooms.get(model_id)
    if not cid:
        return None
    try:
        return int(cid)
    except Exception:
        return None

def get_model_name(model_id: str) -> str:
    return models.get(model_id, "Aurora")

# =========================
# TRANSLATION
# =========================
def translate_text(text: str, src: str, dest: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if src == dest:
        return text
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source=src, target=dest).translate(text)
    except Exception:
        logger.exception("Fallo traducción; devuelvo texto original")
        return text

# =========================
# OVERLAY SSE (optional)
# =========================
app = Flask(__name__, static_folder=".", static_url_path="")

_sse_clients: List[queue.Queue] = []
_sse_lock = threading.Lock()

def sse_broadcast(payload: Dict[str, Any]):
    data = json.dumps(payload, ensure_ascii=False)
    with _sse_lock:
        for q in list(_sse_clients):
            try:
                q.put_nowait(data)
            except Exception:
                pass

@app.get("/")
def home():
    return "OK"

@app.get("/overlay")
def overlay():
    # Si tienes overlay.html en el repo, lo sirve
    try:
        return send_from_directory(".", "overlay.html")
    except Exception:
        return "overlay.html not found", 404

@app.get("/events")
def events():
    q = queue.Queue(maxsize=100)
    with _sse_lock:
        _sse_clients.append(q)

    def gen():
        try:
            while True:
                msg = q.get()
                yield f"data: {msg}\n\n"
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(gen(), mimetype="text/event-stream")

# =========================
# TELEGRAM HANDLERS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = str(update.effective_user.id)
    name = update.effective_user.full_name or update.effective_user.first_name or "Usuario"
    await update.message.reply_text(
        f"Hola {name} ✅\n\n"
        f"Tu ID es: {uid}\n\n"
        f"Comandos:\n"
        f"/myid\n"
        f"/setmodel <nombre>\n"
        f"/liveon\n"
        f"/liveoff\n"
        f"/queue (ver cola)\n"
        f"/whereami (en grupos)\n"
    )

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    await update.message.reply_text(f"Tu user_id es: {update.effective_user.id}")

async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    await update.message.reply_text(f"Chat ID: {chat.id}\nTipo: {chat.type}")

async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Uso: /setmodel Aurora")
        return
    model_name = " ".join(context.args).strip()
    models[uid] = model_name
    _save_json(MODELS_FILE, models)
    await update.message.reply_text(f"✅ Nombre guardado: {model_name}")

async def cmd_bindchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Se ejecuta EN EL GRUPO.
    Uso:
      /bindchat <model_user_id>
    """
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("❌ /bindchat se usa dentro de un GRUPO.")
        return

    if ADMIN_USER_ID and user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Solo el admin puede usar /bindchat.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /bindchat 123456789 (el user_id de Aurora)")
        return

    model_id = context.args[0]
    rooms[model_id] = str(chat.id)
    _save_json(ROOMS_FILE, rooms)

    await update.message.reply_text(
        f"✅ Vinculado.\n"
        f"Grupo: {chat.id}\n"
        f"Modelo user_id: {model_id}\n"
        f"Nombre modelo: {get_model_name(model_id)}"
    )

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = str(update.effective_user.id)
    if not get_group_chat_id(uid):
        await update.message.reply_text("❌ No hay grupo vinculado. Primero haz /bindchat en el grupo con tu user_id.")
        return
    set_live(uid, True)
    await update.message.reply_text("✅ LIVE ON. (La cola de promos se publicará automáticamente.)")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = str(update.effective_user.id)
    set_live(uid, False)
    await update.message.reply_text("🛑 LIVE OFF.")

async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = str(update.effective_user.id)
    q = promos.get(uid, [])
    await update.message.reply_text(f"📦 Promos en cola: {len(q)}")

def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == ChatType.PRIVATE)

def _is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grupo (DE) -> Aurora (PT)"""
    if not update.effective_message or not update.effective_user or not update.effective_chat:
        return
    if not _is_group(update):
        return

    text = update.effective_message.text or ""
    if not text.strip():
        return

    # Encontrar a qué modelo corresponde este grupo
    group_id = str(update.effective_chat.id)
    model_id = None
    for mid, gid in rooms.items():
        if gid == group_id:
            model_id = mid
            break

    if not model_id:
        return  # grupo no vinculado

    if not is_live(model_id):
        return  # si no está live, no traducimos el chat para no molestar

    user_name = update.effective_user.full_name or update.effective_user.first_name or "Usuario"
    pt = translate_text(text, src=GROUP_LANGUAGE, dest=MODEL_LANGUAGE)

    msg = f"👤 {user_name}:\n{pt}"
    try:
        await context.bot.send_message(chat_id=int(model_id), text=msg)
        sse_broadcast({"user": user_name, "text": text})
    except Exception:
        logger.exception("No pude enviar al privado del modelo. ¿El modelo hizo /start al bot?")

async def handle_model_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aurora (PT) -> Grupo (DE)"""
    if not update.effective_message or not update.effective_user:
        return
    if not _is_private(update):
        return

    uid = str(update.effective_user.id)
    group_chat_id = get_group_chat_id(uid)
    if not group_chat_id:
        return

    text = update.effective_message.text or ""
    if not text.strip():
        return

    de = translate_text(text, src=MODEL_LANGUAGE, dest=GROUP_LANGUAGE)
    model_name = get_model_name(uid)

    try:
        await context.bot.send_message(chat_id=group_chat_id, text=f"💬 {model_name}: {de}")
        sse_broadcast({"user": model_name, "text": de})
    except Exception:
        logger.exception("Error enviando al grupo")

async def handle_model_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Aurora manda foto/video al bot en privado -> se guarda en cola.
    Caption lo toma como PT y (opcional) luego se usa como subtítulo traducido al DE al publicar.
    """
    if not update.effective_message or not update.effective_user:
        return
    if not _is_private(update):
        return

    uid = str(update.effective_user.id)
    if not get_group_chat_id(uid):
        await update.message.reply_text("❌ No hay grupo vinculado. Primero /bindchat en el grupo.")
        return

    caption_pt = (update.effective_message.caption or "").strip()

    item = None
    if update.effective_message.photo:
        file_id = update.effective_message.photo[-1].file_id
        item = {"type": "photo", "file_id": file_id, "caption_pt": caption_pt, "ts": int(time.time())}
    elif update.effective_message.video:
        file_id = update.effective_message.video.file_id
        item = {"type": "video", "file_id": file_id, "caption_pt": caption_pt, "ts": int(time.time())}

    if not item:
        return

    promos.setdefault(uid, []).append(item)
    _save_json(PROMOS_FILE, promos)

    await update.message.reply_text(f"✅ Guardado en cola. Total ahora: {len(promos.get(uid, []))}")

async def autopost_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Corre cada X minutos.
    Para cada modelo LIVE ON:
      si hay promo en cola -> postea 1 y la elimina (sin repetir)
    """
    for model_id, group_id_str in list(rooms.items()):
        try:
            if not is_live(model_id):
                continue

            group_chat_id = int(group_id_str)
            q = promos.get(model_id, [])
            if not q:
                continue

            item = q.pop(0)
            promos[model_id] = q
            _save_json(PROMOS_FILE, promos)

            caption_pt = (item.get("caption_pt") or "").strip()
            caption_de = translate_text(caption_pt, src=MODEL_LANGUAGE, dest=GROUP_LANGUAGE) if caption_pt else ""

            # Mensaje final
            model_name = get_model_name(model_id)
            final_caption = f"{model_name} 💋"
            if caption_de:
                final_caption += f"\n{caption_de}"

            if item["type"] == "photo":
                await context.bot.send_photo(chat_id=group_chat_id, photo=item["file_id"], caption=final_caption)
                sse_broadcast({"user": model_name, "text": "[PHOTO] " + (caption_de or "")})
            elif item["type"] == "video":
                await context.bot.send_video(chat_id=group_chat_id, video=item["file_id"], caption=final_caption)
                sse_broadcast({"user": model_name, "text": "[VIDEO] " + (caption_de or "")})

        except Exception:
            logger.exception("Error en autopost_job")

# =========================
# MAIN
# =========================
def run_flask():
    # Render necesita escuchar 0.0.0.0 y PORT
    app.run(host="0.0.0.0", port=PORT)

def main():
    # Flask thread (overlay SSE)
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("myid", cmd_myid))
    application.add_handler(CommandHandler("whereami", cmd_whereami))
    application.add_handler(CommandHandler("setmodel", cmd_setmodel))
    application.add_handler(CommandHandler("bindchat", cmd_bindchat))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))
    application.add_handler(CommandHandler("queue", cmd_queue))

    # Group -> model (text)
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_group_text))

    # Model -> group (private text)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND), handle_model_private_text))

    # Model -> queue (private media)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO), handle_model_private_media))

    # Auto post job (cada AUTO_INTERVAL_MIN)
    application.job_queue.run_repeating(autopost_job, interval=AUTO_INTERVAL_MIN * 60, first=30)

    logger.info("Bot arrancando con polling…")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
