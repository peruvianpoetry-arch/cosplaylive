import os
import json
import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional, List

from flask import Flask

from telegram import Update
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

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or ""
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN (o TELEGRAM_TOKEN) en Render → Environment.")

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")      # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")    # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")        # model_user_id -> true/false
STREAMER_FILE = os.path.join(DATA_DIR, "streamer.json")# owner_user_id -> model_user_id
PROMOS_FILE = os.path.join(DATA_DIR, "promos.json")    # model_user_id -> list of queued items

PROMO_INTERVAL_SECONDS = int(os.environ.get("PROMO_INTERVAL_SECONDS", str(2 * 60 * 60)))  # 2h default

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("cosplaylive")

# ─────────────────────────────────────────────────────────────
# UTILS: JSON STORE
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
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.exception("Error guardando %s: %s", path, e)

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

# ─────────────────────────────────────────────────────────────
# TRANSLATION
# ─────────────────────────────────────────────────────────────

def translate(text: str, source: str, target: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if GoogleTranslator is None:
        return text  # fallback
    try:
        return GoogleTranslator(source=source, target=target).translate(text)
    except Exception:
        return text

def hot_caption_fallback() -> str:
    # “Hot” pero sin explicitar porno duro (para evitar bloqueos y para que sea usable en anuncios)
    pool = [
        "🔥 Nur für dich… heute ganz nah.",
        "😈 Lust auf etwas Besonderes? Komm rein.",
        "💋 Ein kleiner Vorgeschmack…",
        "✨ Heute wird es heißer als normal.",
        "👀 Du willst mehr sehen? Bleib dran.",
    ]
    return pool[int(time.time()) % len(pool)]

# ─────────────────────────────────────────────────────────────
# CORE STATE
# ─────────────────────────────────────────────────────────────

def get_streamer_for_owner(owner_id: int) -> Optional[int]:
    m = _load_json(STREAMER_FILE, {})
    sid = m.get(str(owner_id))
    return int(sid) if sid else None

def set_streamer_for_owner(owner_id: int, streamer_id: int) -> None:
    m = _load_json(STREAMER_FILE, {})
    m[str(owner_id)] = int(streamer_id)
    _save_json(STREAMER_FILE, m)

def get_group_for_streamer(streamer_id: int) -> Optional[int]:
    rooms = _load_json(ROOMS_FILE, {})
    gid = rooms.get(str(streamer_id))
    return int(gid) if gid else None

def set_group_for_streamer(streamer_id: int, group_chat_id: int) -> None:
    rooms = _load_json(ROOMS_FILE, {})
    rooms[str(streamer_id)] = int(group_chat_id)
    _save_json(ROOMS_FILE, rooms)

def set_model_name(streamer_id: int, name: str) -> None:
    models = _load_json(MODELS_FILE, {})
    models[str(streamer_id)] = name.strip()[:40]
    _save_json(MODELS_FILE, models)

def get_model_name(streamer_id: int) -> str:
    models = _load_json(MODELS_FILE, {})
    return models.get(str(streamer_id), "Model")

def set_live(streamer_id: int, on: bool) -> None:
    live = _load_json(LIVE_FILE, {})
    live[str(streamer_id)] = bool(on)
    _save_json(LIVE_FILE, live)

def is_live(streamer_id: int) -> bool:
    live = _load_json(LIVE_FILE, {})
    return bool(live.get(str(streamer_id), False))

# ─────────────────────────────────────────────────────────────
# PROMOS QUEUE
# ─────────────────────────────────────────────────────────────

def promos_load() -> Dict[str, List[Dict[str, Any]]]:
    return _load_json(PROMOS_FILE, {})

def promos_save(d: Dict[str, List[Dict[str, Any]]]) -> None:
    _save_json(PROMOS_FILE, d)

def enqueue_promo(streamer_id: int, item: Dict[str, Any]) -> None:
    d = promos_load()
    key = str(streamer_id)
    d.setdefault(key, [])
    d[key].append(item)
    promos_save(d)

def pop_next_promo(streamer_id: int) -> Optional[Dict[str, Any]]:
    d = promos_load()
    key = str(streamer_id)
    q = d.get(key, [])
    if not q:
        return None
    item = q.pop(0)
    d[key] = q
    promos_save(d)
    return item

def queue_size(streamer_id: int) -> int:
    d = promos_load()
    return len(d.get(str(streamer_id), []))

# ─────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────

def cmd_start(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    update.message.reply_text("✅ Bot funcionando correctamente")

def cmd_whoami(update: Update, context: CallbackContext) -> None:
    u = update.effective_user
    update.message.reply_text(f"Tu user_id: {u.id}\nUsername: @{u.username if u.username else '(sin username)'}")

def cmd_bindchat(update: Update, context: CallbackContext) -> None:
    # Se usa EN EL GRUPO. Vincula ese grupo al streamer asignado a quien ejecuta el comando (owner).
    if update.effective_chat.type not in ("group", "supergroup"):
        update.message.reply_text("Usa /bindchat dentro del grupo.")
        return

    owner_id = update.effective_user.id
    streamer_id = get_streamer_for_owner(owner_id)
    if not streamer_id:
        update.message.reply_text(
            "❌ No hay streamer seleccionado.\n"
            "Primero haz esto en privado conmigo:\n"
            "1) Reenvíame (forward) un mensaje de Aurora\n"
            "o 2) /setstreamer @username"
        )
        return

    set_group_for_streamer(streamer_id, update.effective_chat.id)
    name = get_model_name(streamer_id)
    update.message.reply_text(f"✅ Grupo vinculado a streamer: {name} (id {streamer_id})")

def cmd_setstreamer(update: Update, context: CallbackContext) -> None:
    # Se usa EN PRIVADO por el owner/admin.
    # Opción A: /setstreamer @Aurorab23
    # Opción B: responder a un mensaje reenviado de Aurora
    if update.effective_chat.type != "private":
        update.message.reply_text("Usa /setstreamer en privado conmigo.")
        return

    owner_id = update.effective_user.id

    # Si es reply a un forward
    if update.message.reply_to_message and update.message.reply_to_message.forward_from:
        streamer_id = update.message.reply_to_message.forward_from.id
        set_streamer_for_owner(owner_id, streamer_id)
        update.message.reply_text(f"✅ Streamer seleccionado por forward: user_id {streamer_id}")
        return

    # Si viene con @username
    if context.args and len(context.args) >= 1:
        raw = context.args[0].strip()
        if raw.startswith("@"):
            try:
                chat = context.bot.get_chat(raw)
                streamer_id = chat.id
                set_streamer_for_owner(owner_id, streamer_id)
                update.message.reply_text(f"✅ Streamer seleccionado: {raw} (id {streamer_id})")
                return
            except Exception:
                update.message.reply_text("❌ No pude resolver ese @username. Usa la opción forward.")
                return

    update.message.reply_text(
        "✅ Para seleccionar streamer:\n"
        "• Opción fácil: Reenvíame (forward) cualquier mensaje de Aurora y listo.\n"
        "• Opción @: /setstreamer @Aurorab23\n"
        "Tip: también puedes hacer /setstreamer respondiendo a un mensaje reenviado."
    )

def cmd_setmodel(update: Update, context: CallbackContext) -> None:
    # Solo streamer puede poner su nombre.
    if update.effective_chat.type != "private":
        update.message.reply_text("Usa /setmodel en privado conmigo.")
        return
    uid = update.effective_user.id
    if not context.args:
        update.message.reply_text("Uso: /setmodel Aurora")
        return
    name = " ".join(context.args).strip()
    set_model_name(uid, name)
    update.message.reply_text(f"✅ Nombre de modelo guardado: {name}")

def cmd_liveon(update: Update, context: CallbackContext) -> None:
    if update.effective_chat.type != "private":
        update.message.reply_text("Usa /liveon en privado conmigo (solo streamer).")
        return
    streamer_id = update.effective_user.id
    set_live(streamer_id, True)
    update.message.reply_text("✅ LIVE ON")

def cmd_liveoff(update: Update, context: CallbackContext) -> None:
    if update.effective_chat.type != "private":
        update.message.reply_text("Usa /liveoff en privado conmigo (solo streamer).")
        return
    streamer_id = update.effective_user.id
    set_live(streamer_id, False)
    update.message.reply_text("⛔ LIVE OFF")

def cmd_queue(update: Update, context: CallbackContext) -> None:
    if update.effective_chat.type != "private":
        update.message.reply_text("Usa /queue en privado conmigo.")
        return
    streamer_id = update.effective_user.id
    update.message.reply_text(f"📦 Promos en cola: {queue_size(streamer_id)}")

# ─────────────────────────────────────────────────────────────
# MESSAGE HANDLERS (TRANSLATION + PROMOS)
# ─────────────────────────────────────────────────────────────

def handle_group_text(update: Update, context: CallbackContext) -> None:
    # Mensajes en grupo → se traducen DE->PT y se envían al streamer si está LIVE ON
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return

    msg = update.effective_message
    txt = (msg.text or "").strip()
    if not txt:
        return

    # Encontrar streamer por chat_id
    rooms = _load_json(ROOMS_FILE, {})
    streamer_id = None
    for k, v in rooms.items():
        if int(v) == int(chat.id):
            streamer_id = int(k)
            break
    if not streamer_id:
        return

    if not is_live(streamer_id):
        return

    user = update.effective_user
    sender = user.first_name or "User"
    de_to_pt = translate(txt, source="de", target="pt")
    model_name = get_model_name(streamer_id)

    payload = f"💬 <b>{sender}</b> (DE→PT):\n{de_to_pt}"
    try:
        context.bot.send_message(chat_id=streamer_id, text=payload, parse_mode="HTML")
    except Exception as e:
        logger.warning("No pude enviar al streamer: %s", e)

def handle_streamer_private(update: Update, context: CallbackContext) -> None:
    # Mensajes privados del streamer → se traducen PT->DE y se publican al grupo vinculado
    if update.effective_chat.type != "private":
        return

    streamer_id = update.effective_user.id
    msg = update.effective_message

    # 1) Si envía TEXTO normal
    if msg.text and not msg.text.startswith("/"):
        room = get_group_for_streamer(streamer_id)
        if not room:
            msg.reply_text("❌ No hay grupo vinculado. Pídele al admin que haga /bindchat en el grupo.")
            return

        out = translate(msg.text, source="pt", target="de")
        model_name = get_model_name(streamer_id)
        payload = f"🔥 <b>{model_name}</b>:\n{out}"
        try:
            context.bot.send_message(chat_id=room, text=payload, parse_mode="HTML")
        except Exception as e:
            logger.warning("No pude publicar al grupo: %s", e)
        return

    # 2) Si envía FOTO/VIDEO como promo → ENCOLAR
    if msg.photo or msg.video:
        caption = (msg.caption or "").strip()
        if not caption:
            caption_de = hot_caption_fallback()
        else:
            caption_de = translate(caption, source="pt", target="de")

        item = {
            "ts": _now(),
            "type": "photo" if msg.photo else "video",
            "file_id": (msg.photo[-1].file_id if msg.photo else msg.video.file_id),
            "caption_de": caption_de,
        }
        enqueue_promo(streamer_id, item)
        msg.reply_text(f"✅ Promo guardada en cola. Total ahora: {queue_size(streamer_id)}")
        return

def handle_forward_to_select_streamer(update: Update, context: CallbackContext) -> None:
    # Si el owner/admin reenvía un mensaje de Aurora al bot (en privado),
    # el bot la selecciona como streamer automáticamente.
    if update.effective_chat.type != "private":
        return
    msg = update.effective_message
    if msg.forward_from:
        owner_id = update.effective_user.id
        streamer_id = msg.forward_from.id
        set_streamer_for_owner(owner_id, streamer_id)
        update.effective_message.reply_text(f"✅ Streamer seleccionado por forward: user_id {streamer_id}")

# ─────────────────────────────────────────────────────────────
# PROMO JOB
# ─────────────────────────────────────────────────────────────

def promo_tick(context: CallbackContext) -> None:
    # Recorre streamers LIVE ON y si hay promo en cola, publica una
    try:
        live = _load_json(LIVE_FILE, {})
        rooms = _load_json(ROOMS_FILE, {})
        for sid_str, on in live.items():
            if not on:
                continue
            streamer_id = int(sid_str)
            room = rooms.get(sid_str)
            if not room:
                continue

            item = pop_next_promo(streamer_id)
            if not item:
                continue

            model_name = get_model_name(streamer_id)
            cap = item.get("caption_de", "") or hot_caption_fallback()
            caption = f"🔥 <b>{model_name}</b>\n{cap}"

            if item["type"] == "photo":
                context.bot.send_photo(chat_id=int(room), photo=item["file_id"], caption=caption, parse_mode="HTML")
            else:
                context.bot.send_video(chat_id=int(room), video=item["file_id"], caption=caption, parse_mode="HTML")

    except Exception as e:
        logger.exception("promo_tick error: %s", e)

# ─────────────────────────────────────────────────────────────
# FLASK keep-alive for Render
# ─────────────────────────────────────────────────────────────

flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK"

def main() -> None:
    updater = Updater(token=TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whoami", cmd_whoami))
    dp.add_handler(CommandHandler("setstreamer", cmd_setstreamer))
    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))
    dp.add_handler(CommandHandler("queue", cmd_queue))

    # Forward-based streamer selection (auto)
    dp.add_handler(MessageHandler(Filters.forwarded & Filters.chat_type.private, handle_forward_to_select_streamer))

    # Group -> Streamer translation
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_text))

    # Streamer private -> Group + Promo queue
    dp.add_handler(MessageHandler(Filters.chat_type.private & (Filters.text | Filters.photo | Filters.video), handle_streamer_private))

    # Job: promos
    updater.job_queue.run_repeating(promo_tick, interval=PROMO_INTERVAL_SECONDS, first=30)

    # Start bot
    updater.start_polling(drop_pending_updates=True)

    # Start Flask (Render expects a web listener)
    port = int(os.environ.get("PORT", "10000"))
    logger.info("Flask on port %s", port)
    flask_app.run(host="0.0.0.0", port=port)

    updater.idle()

if __name__ == "__main__":
    main()
