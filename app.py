# app.py
# CosplayLive Translate Bot (PTB v13.15) + streamer selection + LIVE toggle + promo queue every 2h
# + MULTI-ROOM ROUTING via DM reply (no confusión entre FREE/VIP/LIVECHAT)
# Compatible con: python-telegram-bot==13.15

import os
import json
import time
import threading
from flask import Flask
from telegram import Update, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# =========================
# Config (ENV)
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or os.getenv("TOKEN")
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
GROUP_LANGUAGE = os.getenv("GROUP_LANGUAGE", "de")   # idioma del grupo (Alemania)
MODEL_LANGUAGE = os.getenv("MODEL_LANGUAGE", "pt")   # idioma de la modelo (Brasil/Portugal)
PROMO_INTERVAL_SECONDS = int(os.getenv("PROMO_INTERVAL_SECONDS", str(2 * 60 * 60)))  # default 2h
WELCOME_ON_JOIN = os.getenv("WELCOME_ON_JOIN", "1") == "1"

os.makedirs(DATA_DIR, exist_ok=True)

# =========================
# Files
# =========================
ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")          # model_user_id -> group_chat_id (legacy/fallback)
MODELS_FILE = os.path.join(DATA_DIR, "models.json")        # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")            # model_user_id -> true/false
STREAMERS_FILE = os.path.join(DATA_DIR, "streamers.json")  # group_chat_id -> model_user_id
INTRO_FILE = os.path.join(DATA_DIR, "intro.json")          # group_chat_id -> intro_text
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")          # model_user_id -> {"items":[...], "last_sent": epoch}

# NUEVO: routing multi-room
BRIDGE_FILE = os.path.join(DATA_DIR, "bridge.json")        # model_user_id -> { dm_message_id : group_chat_id }
LAST_FILE = os.path.join(DATA_DIR, "last.json")            # model_user_id -> last_group_chat_id (fallback)

# =========================
# Safe JSON helpers
# =========================
_lock = threading.Lock()

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_all():
    with _lock:
        rooms = _read_json(ROOMS_FILE, {})
        models = _read_json(MODELS_FILE, {})
        live = _read_json(LIVE_FILE, {})
        streamers = _read_json(STREAMERS_FILE, {})
        intro = _read_json(INTRO_FILE, {})
        queue = _read_json(QUEUE_FILE, {})
        bridge = _read_json(BRIDGE_FILE, {})
        last = _read_json(LAST_FILE, {})
    return rooms, models, live, streamers, intro, queue, bridge, last

def save_all(rooms=None, models=None, live=None, streamers=None, intro=None, queue=None, bridge=None, last=None):
    with _lock:
        if rooms is not None: _write_json(ROOMS_FILE, rooms)
        if models is not None: _write_json(MODELS_FILE, models)
        if live is not None: _write_json(LIVE_FILE, live)
        if streamers is not None: _write_json(STREAMERS_FILE, streamers)
        if intro is not None: _write_json(INTRO_FILE, intro)
        if queue is not None: _write_json(QUEUE_FILE, queue)
        if bridge is not None: _write_json(BRIDGE_FILE, bridge)
        if last is not None: _write_json(LAST_FILE, last)

# =========================
# Translator (optional)
# =========================
def translate_text(text: str, src: str, dst: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if src == dst:
        return text
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source=src, target=dst).translate(text)
    except Exception:
        return text

# =========================
# Helpers
# =========================
def now_epoch() -> int:
    return int(time.time())

def is_group_chat(chat) -> bool:
    return chat.type in ("group", "supergroup")

def sexy_fallback_line(lang: str) -> str:
    if lang == "de":
        return "🔥 Na ihr… habt ihr Lust auf was ganz Privates? 😈"
    if lang == "pt":
        return "🔥 Oi… vocês querem algo bem privado? 😈"
    return "🔥 Hey… want something private? 😈"

def format_informal_hint_de(text: str) -> str:
    t = text
    t = t.replace("Möchten Sie", "Wollt ihr")
    t = t.replace("Möchtest du", "Willst du")
    t = t.replace("Sie ", "ihr ")
    t = t.replace("Ihnen", "euch")
    t = t.replace("Ihr", "euer")
    return t

def get_bound_model_for_group(group_chat_id: str, streamers: dict) -> str:
    return streamers.get(str(group_chat_id), "")

def is_live(model_user_id: str, live: dict) -> bool:
    return bool(live.get(str(model_user_id), False))

def find_group_for_streamer_fallback(model_user_id: str, rooms: dict, streamers: dict, last: dict) -> str:
    # 0) último grupo usado (si existe)
    g = last.get(str(model_user_id), "")
    if g:
        return g

    # 1) legacy rooms
    g = rooms.get(str(model_user_id), "")
    if g:
        return g

    # 2) reverse from streamers
    for group_id, streamer_id in (streamers or {}).items():
        if str(streamer_id) == str(model_user_id):
            return str(group_id)
    return ""

def group_label(group_chat_id: str, chat_title: str) -> str:
    # etiqueta simple: usa el título si existe
    title = (chat_title or "").strip()
    if not title:
        return f"CHAT {group_chat_id}"
    return title[:28]

# =========================
# Commands
# =========================
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text("✅ Bot funcionando correctamente")

def cmd_whoami(update: Update, context: CallbackContext):
    u = update.effective_user
    update.message.reply_text(f"👤 Tu user_id: {u.id}\nUsername: @{u.username}" if u else "No pude leer tu user_id")

def cmd_setmodel(update: Update, context: CallbackContext):
    if not update.message:
        return
    args = context.args
    name = " ".join(args).strip() if args else ""
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    if not name:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return
    models[str(user.id)] = name
    save_all(models=models)
    update.message.reply_text(f"✅ Modelo registrada: {name}\nuser_id: {user.id}")

def cmd_bindchat(update: Update, context: CallbackContext):
    # legacy (mantener)
    if not update.message or not is_group_chat(update.effective_chat):
        return
    args = context.args
    if not args:
        update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return
    model_user_id = args[0].strip()
    group_chat_id = str(update.effective_chat.id)
    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    rooms[model_user_id] = group_chat_id
    save_all(rooms=rooms)
    update.message.reply_text(f"✅ Grupo vinculado.\nmodel_user_id: {model_user_id}\nchat_id: {group_chat_id}")

def cmd_setstreamer(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return

    group_chat_id = str(update.effective_chat.id)
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        update.message.reply_text("Uso: responde (reply) a un mensaje del streamer y escribe:\n/setstreamer")
        return

    streamer_user = reply.from_user
    streamer_id = str(streamer_user.id)

    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    streamers[group_chat_id] = streamer_id

    # asegurar fallback legacy
    rooms[streamer_id] = group_chat_id

    if streamer_id not in models:
        models[streamer_id] = streamer_user.first_name or "Streamer"

    save_all(rooms=rooms, streamers=streamers, models=models)

    update.message.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {models.get(streamer_id, 'Streamer')}\n"
        f"user_id: {streamer_id}\n\n"
        "Ahora este grupo queda atendido por esa modelo.\n"
        "Con LIVE ON, todo se traduce en ambos sentidos.",
        parse_mode=ParseMode.HTML
    )

def cmd_liveon(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    live[str(user.id)] = True
    save_all(live=live)
    update.message.reply_text("🟢 LIVE ON ✅\nDesde ahora se traduce en ambos sentidos + cola habilitada.")

def cmd_liveoff(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    live[str(user.id)] = False
    save_all(live=live)
    update.message.reply_text("🔴 LIVE OFF ✅\nSe detiene traducción y cola.")

def cmd_intro(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = " ".join(context.args).strip()
    if not text:
        update.message.reply_text("Uso: /intro <texto>\nEj: /intro Soy Aurora 🔥 23 🇧🇷 ...")
        return
    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro[group_chat_id] = text
    save_all(intro=intro)
    msg = update.message.reply_text(f"📌 Presentación guardada:\n\n{text}")
    try:
        context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
    except Exception:
        pass

def cmd_queue(update: Update, context: CallbackContext):
    if not update.message:
        return
    update.message.reply_text(
        "📦 Cola de promos:\n"
        "En privado (streamer) manda foto/video con caption empezando con:\n"
        "  #cola  o  /cola\n"
        "✅ Eso lo encola.\n"
        "Si NO pones #cola → se publica inmediatamente.\n\n"
        "La cola se suelta cada X tiempo mientras LIVE esté ON."
    )

def cmd_cola_alias(update: Update, context: CallbackContext):
    return cmd_queue(update, context)

# =========================
# Message Handlers
# =========================
def handle_group_text(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    rooms, models, live, streamers, intro, queue, bridge, last = load_all()

    group_chat_id = str(update.effective_chat.id)
    model_user_id = get_bound_model_for_group(group_chat_id, streamers)
    if not model_user_id:
        return
    if not is_live(model_user_id, live):
        return

    translated = translate_text(text, GROUP_LANGUAGE, MODEL_LANGUAGE)

    # etiqueta para Aurora (para que vea de qué grupo viene)
    label = group_label(group_chat_id, update.effective_chat.title)
    sender = update.effective_user
    sender_name = ("@" + sender.username) if sender and sender.username else (sender.first_name if sender else "user")

    dm_text = f"💬 <b>[{label}]</b> {sender_name}:\n{translated}"

    try:
        sent = context.bot.send_message(
            chat_id=int(model_user_id),
            text=dm_text,
            parse_mode=ParseMode.HTML
        )

        # ✅ guardamos routing: si Aurora responde (reply) a este DM, volvemos al grupo correcto
        bridge.setdefault(str(model_user_id), {})
        bridge[str(model_user_id)][str(sent.message_id)] = group_chat_id

        # ✅ guardamos último grupo activo para fallback (media / mensajes sin reply)
        last[str(model_user_id)] = group_chat_id

        save_all(bridge=bridge, last=last)

    except Exception:
        pass

def handle_private_text(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    model_user_id = str(user.id)

    if not is_live(model_user_id, live):
        update.message.reply_text("⚠️ No estás en LIVE. Usa /liveon para habilitar envío al grupo.")
        return

    # ✅ SI AURORA RESPONDE (REPLY) -> se manda al grupo correcto
    target_group = ""
    if update.message.reply_to_message:
        replied_id = str(update.message.reply_to_message.message_id)
        target_group = (bridge.get(model_user_id) or {}).get(replied_id, "")

    # fallback: último grupo activo / legacy
    if not target_group:
        target_group = find_group_for_streamer_fallback(model_user_id, rooms, streamers, last)

    if not target_group:
        update.message.reply_text("⚠️ No encuentro el grupo destino. Ve al grupo y usa /setstreamer respondiendo a tu mensaje.")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    translated = translate_text(text, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated = format_informal_hint_de(translated)

    try:
        context.bot.send_message(chat_id=int(target_group), text=translated)
    except Exception:
        pass

def enqueue_media(model_user_id: str, item: dict):
    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    q = queue.get(model_user_id) or {"items": [], "last_sent": 0}
    q["items"].append(item)
    queue[model_user_id] = q
    save_all(queue=queue)

def handle_private_media(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    model_user_id = str(user.id)

    if not is_live(model_user_id, live):
        update.message.reply_text("⚠️ No estás en LIVE. Usa /liveon.")
        return

    # Para media: usamos último grupo activo (porque no hay reply fácil con media)
    group_chat_id = find_group_for_streamer_fallback(model_user_id, rooms, streamers, last)
    if not group_chat_id:
        update.message.reply_text("⚠️ No encuentro tu grupo vinculado. Ve al grupo y usa /setstreamer respondiendo a tu mensaje.")
        return

    caption = (update.message.caption or "").strip()
    cap_lower = caption.lower()
    should_queue = (
        cap_lower.startswith("#cola") or cap_lower.startswith("/cola") or
        cap_lower.startswith("#queue") or cap_lower.startswith("/queue")
    )

    clean_caption = caption
    if should_queue:
        parts = caption.split(maxsplit=1)
        clean_caption = parts[1].strip() if len(parts) > 1 else ""

    if not clean_caption:
        clean_caption = sexy_fallback_line(GROUP_LANGUAGE)

    translated_caption = translate_text(clean_caption, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated_caption = format_informal_hint_de(translated_caption)

    item = {"type": None, "file_id": None, "caption": translated_caption}

    if update.message.photo:
        item["type"] = "photo"
        item["file_id"] = update.message.photo[-1].file_id
    elif update.message.video:
        item["type"] = "video"
        item["file_id"] = update.message.video.file_id
    else:
        return

    if should_queue:
        enqueue_media(model_user_id, item)
        update.message.reply_text("✅ Guardado en cola. Se publicará cada X tiempo mientras LIVE esté ON.")
        return

    try:
        if item["type"] == "photo":
            context.bot.send_photo(chat_id=int(group_chat_id), photo=item["file_id"], caption=item["caption"])
        else:
            context.bot.send_video(chat_id=int(group_chat_id), video=item["file_id"], caption=item["caption"])
    except Exception:
        pass

def handle_new_members(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    if not WELCOME_ON_JOIN:
        return
    rooms, models, live, streamers, intro, queue, bridge, last = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro_text = intro.get(group_chat_id, "")
    if intro_text:
        try:
            context.bot.send_message(chat_id=update.effective_chat.id, text=intro_text)
        except Exception:
            pass

# =========================
# Promo scheduler thread
# =========================
def promo_loop(bot):
    while True:
        try:
            rooms, models, live, streamers, intro, queue, bridge, last = load_all()
            for model_user_id, live_on in list(live.items()):
                if not live_on:
                    continue
                group_chat_id = find_group_for_streamer_fallback(str(model_user_id), rooms, streamers, last)
                if not group_chat_id:
                    continue

                q = queue.get(str(model_user_id)) or {"items": [], "last_sent": 0}
                items = q.get("items", [])
                last_sent = int(q.get("last_sent", 0))

                if not items:
                    continue
                if now_epoch() - last_sent < PROMO_INTERVAL_SECONDS:
                    continue

                item = items.pop(0)
                q["items"] = items
                q["last_sent"] = now_epoch()
                queue[str(model_user_id)] = q
                save_all(queue=queue)

                try:
                    if item["type"] == "photo":
                        bot.send_photo(chat_id=int(group_chat_id), photo=item["file_id"], caption=item.get("caption", ""))
                    else:
                        bot.send_video(chat_id=int(group_chat_id), video=item["file_id"], caption=item.get("caption", ""))
                except Exception:
                    pass
        except Exception:
            pass

        time.sleep(60)

# =========================
# Flask keep-alive (Render)
# =========================
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# =========================
# Main
# =========================
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Environment Variables")

    updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Commands
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whoami", cmd_whoami))
    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("setstreamer", cmd_setstreamer))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))
    dp.add_handler(CommandHandler("intro", cmd_intro))
    dp.add_handler(CommandHandler("queue", cmd_queue))
    dp.add_handler(CommandHandler("cola", cmd_cola_alias))

    # Group text -> model private (3 grupos a la vez, sin confusión)
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_text))

    # Private text -> group (por reply routing)
    dp.add_handler(MessageHandler(Filters.private & Filters.text & ~Filters.command, handle_private_text))

    # Private media from model
    dp.add_handler(MessageHandler(Filters.private & (Filters.photo | Filters.video), handle_private_media))

    # New members in group
    dp.add_handler(MessageHandler(Filters.st
