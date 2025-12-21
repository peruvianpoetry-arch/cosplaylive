# app.py
# CosplayLive Translate Bot (PTB v13.15)
# - streamer selection (/setstreamer)
# - LIVE toggle (/liveon /liveoff)
# - Cola de promos cada 2h: usar "cola" al inicio del caption (ej: "cola ...")
# - Pin automático: /intro y último post (best-effort)

import os
import json
import time
import threading
import random
from flask import Flask
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# =========================
# Config (ENV)
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", "/var/data")

GROUP_LANGUAGE = os.getenv("GROUP_LANGUAGE", "de")  # idioma del grupo
MODEL_LANGUAGE = os.getenv("MODEL_LANGUAGE", "pt")  # idioma de la modelo

PROMO_INTERVAL_SECONDS = int(os.getenv("PROMO_INTERVAL_SECONDS", str(2 * 60 * 60)))  # 2h
WELCOME_ON_JOIN = os.getenv("WELCOME_ON_JOIN", "1") == "1"

# =========================
# Files
# =========================
os.makedirs(DATA_DIR, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")         # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")       # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")           # model_user_id -> true/false
STREAMERS_FILE = os.path.join(DATA_DIR, "streamers.json") # group_chat_id -> model_user_id
INTRO_FILE = os.path.join(DATA_DIR, "intro.json")         # group_chat_id -> intro_text
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")         # model_user_id -> {"items":[...], "last_sent": epoch}
LASTPOST_FILE = os.path.join(DATA_DIR, "lastpost.json")   # group_chat_id -> last_message_id (pin)

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
        lastpost = _read_json(LASTPOST_FILE, {})
    return rooms, models, live, streamers, intro, queue, lastpost

def save_all(rooms=None, models=None, live=None, streamers=None, intro=None, queue=None, lastpost=None):
    with _lock:
        if rooms is not None: _write_json(ROOMS_FILE, rooms)
        if models is not None: _write_json(MODELS_FILE, models)
        if live is not None: _write_json(LIVE_FILE, live)
        if streamers is not None: _write_json(STREAMERS_FILE, streamers)
        if intro is not None: _write_json(INTRO_FILE, intro)
        if queue is not None: _write_json(QUEUE_FILE, queue)
        if lastpost is not None: _write_json(LASTPOST_FILE, lastpost)

# =========================
# Translator
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

def is_group_chat(update: Update) -> bool:
    try:
        return update.effective_chat and update.effective_chat.type in ("group", "supergroup")
    except Exception:
        return False

def is_private_chat(update: Update) -> bool:
    try:
        return update.effective_chat and update.effective_chat.type == "private"
    except Exception:
        return False

def get_bound_model_for_group(group_chat_id: str, streamers: dict) -> str:
    return streamers.get(str(group_chat_id), "")

def get_group_for_model(model_user_id: str, rooms: dict) -> str:
    return rooms.get(str(model_user_id), "")

def is_live(model_user_id: str, live: dict) -> bool:
    return bool(live.get(str(model_user_id), False))

def format_informal_hint_de(text: str) -> str:
    # Heurística suave para evitar Sie/Dich mezclado.
    t = text
    t = t.replace("Möchten Sie", "Willst du")
    t = t.replace("Wollen Sie", "Willst du")
    t = t.replace("Sie ", "du ")
    t = t.replace("Ihnen", "dir")
    t = t.replace("Ihr", "dein")
    t = t.replace("Ihre", "deine")
    return t

# =========================
# Frases "hot" (no explícitas) + emojis
# =========================
HOT_LINES_DE = [
    "🔥 Hey du… Lust auf was Freches? 😈",
    "💋 Na, vermisst du Nähe gerade auch? 😉",
    "✨ Ich bin heute besonders verspielt… komm näher 😏",
    "🔥 Schreib mir… ich antworte dir süß & frech 😘",
    "😇 Engelchen oder Teufelchen… was willst du heute? 😈",
    "💞 Ich hab gerade richtig Lust auf Aufmerksamkeit… 🫦",
    "🌶️ Heute wird’s heiß… bleib dran 😏",
    "💋 Du machst mich neugierig… was stellst du dir vor? 😉",
    "🔥 Ich hab Bock auf ein bisschen Knistern… 😈",
    "💖 Komm, sag mir wie dein Tag war… ich mach ihn besser 😘",
    "😏 Wenn du wüsstest, was ich gerade denke…",
    "🔥 Ich bin online… und ziemlich in Stimmung 😈",
    "💋 Nur du & ich… klingt gut, oder? 😉",
    "✨ Ich mag Männer mit Fantasie… hast du eine? 😏",
    "🌶️ Heute wird nicht langweilig… versprochen 😘",
    "🔥 Ich liebe es, wenn ihr mich heiß macht… 😈",
    "💞 Vielleicht kriegst du heute ein kleines Extra… 😉",
    "🫦 Ich bin süß… aber auch gefährlich 😏",
    "🔥 Schreib mir dein Geheimnis… ich behalte es 😈",
    "💋 Wenn du nett bist… bin ich es auch 😘",
    "✨ Neue kleine Preview für euch… 😏",
    "🔥 Nur ein Vorgeschmack… mehr gibt’s privat 😈",
    "💖 Ihr wollt mehr? Dann zeigt mir’s 😉",
    "🌶️ Ich bin heute sehr… „wach“ 😏",
    "😈 Du weißt genau, warum du hier bist…",
    "💋 Ich kann sehr brav sein… oder gar nicht 😘",
    "🔥 Bleib hier… es lohnt sich 😈",
    "✨ Ich liebe es, wenn ihr mich verwöhnt… 😉",
    "🫦 Ich hab richtig Lust auf Komplimente… 😏",
    "💞 Nur ein kleiner Teaser… 😈",
]

def sexy_fallback_line_de(live_on: bool) -> str:
    # Si está LIVE: "LIVE NOW", si no: "NEW"
    tag = "🔴 LIVE NOW" if live_on else "🆕 NEW"
    line = random.choice(HOT_LINES_DE)
    # Coherencia con plural: usar "ihr" a veces
    if random.random() < 0.35:
        line = line.replace("du", "ihr").replace("dir", "euch")
    return f"{tag} {line}"

# =========================
# Pin helpers (best effort)
# =========================
def try_pin(context: CallbackContext, chat_id: int, message_id: int):
    try:
        context.bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception:
        pass

def remember_and_pin_last(context: CallbackContext, group_chat_id: int, message_id: int):
    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    lastpost[str(group_chat_id)] = int(message_id)
    save_all(lastpost=lastpost)
    try_pin(context, group_chat_id, message_id)

# =========================
# Commands
# =========================
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text("✅ Bot funcionando correctamente")

def cmd_whoami(update: Update, context: CallbackContext):
    u = update.effective_user
    if not u:
        update.message.reply_text("No pude leer tu user_id")
        return
    update.message.reply_text(f"👤 Tu user_id: {u.id}\nUsername: @{u.username}")

def cmd_setmodel(update: Update, context: CallbackContext):
    # /setmodel Aurora (ideal en privado)
    if not update.message:
        return
    user = update.effective_user
    if not user:
        return
    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return
    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    models[str(user.id)] = name
    save_all(models=models)
    update.message.reply_text(f"✅ Modelo registrada: {name}\nuser_id: {user.id}")

def cmd_bindchat(update: Update, context: CallbackContext):
    # /bindchat <model_user_id> en el grupo
    if not update.message or not is_group_chat(update):
        return
    if not context.args:
        update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return
    model_user_id = context.args[0].strip()
    group_chat_id = str(update.effective_chat.id)
    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    rooms[model_user_id] = group_chat_id
    save_all(rooms=rooms)
    update.message.reply_text(f"✅ Grupo vinculado.\nmodel_user_id: {model_user_id}\nchat_id: {group_chat_id}")

def cmd_setstreamer(update: Update, context: CallbackContext):
    # En el grupo: responder a un mensaje de Aurora y poner /setstreamer
    if not update.message or not is_group_chat(update):
        return
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        update.message.reply_text("Uso: responde (reply) a un mensaje de Aurora y escribe:\n/setstreamer")
        return

    group_chat_id = str(update.effective_chat.id)
    streamer_user = reply.from_user
    streamer_id = str(streamer_user.id)

    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    streamers[group_chat_id] = streamer_id
    rooms[streamer_id] = group_chat_id
    if streamer_id not in models:
        models[streamer_id] = streamer_user.first_name or "Streamer"

    save_all(rooms=rooms, streamers=streamers, models=models)

    update.message.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {models.get(streamer_id, 'Streamer')}\n"
        f"user_id: {streamer_id}\n\n"
        "Prueba:\n"
        "- En el grupo escribe en alemán → llega traducido al privado del streamer.\n"
        "- En privado, el streamer escribe en portugués → se publica en el grupo en alemán.\n"
        "⚠️ Recuerda: el streamer debe usar /liveon para activar todo."
    )

def cmd_liveon(update: Update, context: CallbackContext):
    if not update.message or not is_private_chat(update):
        return
    user = update.effective_user
    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    live[str(user.id)] = True
    save_all(live=live)
    update.message.reply_text("🟢 LIVE ON ✅\nTraducción + envíos + cola activados.")

def cmd_liveoff(update: Update, context: CallbackContext):
    if not update.message or not is_private_chat(update):
        return
    user = update.effective_user
    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    live[str(user.id)] = False
    save_all(live=live)
    update.message.reply_text("🔴 LIVE OFF ✅\nSe detiene traducción y cola.")

def cmd_intro(update: Update, context: CallbackContext):
    # /intro <texto...> en el grupo
    if not update.message or not is_group_chat(update):
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        update.message.reply_text("Uso: /intro <texto>\nEj: /intro Soy Aurora 🔥 23 🇧🇷 ...")
        return

    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    gid = str(update.effective_chat.id)
    intro[gid] = text
    save_all(intro=intro)

    msg = update.message.reply_text("📌 " + text)
    remember_and_pin_last(context, update.effective_chat.id, msg.message_id)

def cmd_cola(update: Update, context: CallbackContext):
    # Explicación corta para Aurora
    update.message.reply_text(
        "📦 COLA (fácil):\n"
        "En privado, cuando mandes una foto/video:\n"
        "- Si quieres que salga en 2 horas (cola): escribe en el caption empezando con:  cola\n"
        "  Ej:  cola hola mis Süßen… 😈\n"
        "- Si NO pones 'cola' → sale inmediato.\n\n"
        f"⏱ Intervalo actual: {PROMO_INTERVAL_SECONDS//3600} horas."
    )

# =========================
# Message Handlers
# =========================
def handle_group_text(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    gid = str(update.effective_chat.id)
    model_user_id = get_bound_model_for_group(gid, streamers)
    if not model_user_id:
        return
    if not is_live(model_user_id, live):
        return

    translated = translate_text(text, GROUP_LANGUAGE, MODEL_LANGUAGE)
    try:
        context.bot.send_message(chat_id=int(model_user_id), text=f"💬 (grupo) {translated}")
    except Exception:
        pass

def handle_private_text(update: Update, context: CallbackContext):
    # STREAMER/Modelo en privado -> Grupo (PT->DE)
    if not update.message or not is_private_chat(update):
        return

    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    uid = str(user.id)

    if not is_live(uid, live):
        return

    gid = get_group_for_model(uid, rooms)
    if not gid:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    translated = translate_text(text, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated = format_informal_hint_de(translated)

    try:
        sent = context.bot.send_message(chat_id=int(gid), text=translated)
        # pin último texto (opcional)
        remember_and_pin_last(context, int(gid), sent.message_id)
    except Exception:
        pass

def enqueue_media(model_user_id: str, item: dict):
    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    q = queue.get(model_user_id) or {"items": [], "last_sent": 0}
    q["items"].append(item)
    queue[model_user_id] = q
    save_all(queue=queue)

def publish_media_to_group(context: CallbackContext, gid: int, item: dict, live_on: bool):
    # Publica foto/video con caption ya listo + pin último post
    try:
        caption = item.get("caption") or sexy_fallback_line_de(live_on)
        if item["type"] == "photo":
            sent = context.bot.send_photo(chat_id=gid, photo=item["file_id"], caption=caption)
        else:
            sent = context.bot.send_video(chat_id=gid, video=item["file_id"], caption=caption)
        try:
            remember_and_pin_last(context, gid, sent.message_id)
        except Exception:
            pass
    except Exception:
        pass

def handle_private_media(update: Update, context: CallbackContext):
    # En privado: si caption empieza con "cola" => encola, si no => publica inmediato
    if not update.message or not is_private_chat(update):
        return

    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    uid = str(user.id)

    if not is_live(uid, live):
        return

    gid = get_group_for_model(uid, rooms)
    if not gid:
        return

    caption = (update.message.caption or "").strip()
    cap_lower = caption.lower().strip()

    # Trigger cola: "cola ..." (simple)
    should_cola = cap_lower.startswith("cola")

    # Limpia "cola"
    clean_caption = caption
    if should_cola:
        parts = caption.split(maxsplit=1)
        clean_caption = parts[1].strip() if len(parts) > 1 else ""

    live_on = True  # aquí siempre live, pero lo dejamos por claridad

    # Si no hay caption, usar frase aleatoria en DE
    if not clean_caption:
        final_caption_de = sexy_fallback_line_de(live_on)
    else:
        # Traducimos caption PT->DE y lo hacemos informal
        final_caption_de = translate_text(clean_caption, MODEL_LANGUAGE, GROUP_LANGUAGE)
        if GROUP_LANGUAGE == "de":
            final_caption_de = format_informal_hint_de(final_caption_de)
        # Si quedó vacío, fallback
        if not final_caption_de.strip():
            final_caption_de = sexy_fallback_line_de(live_on)

    item = {"type": None, "file_id": None, "caption": final_caption_de}

    if update.message.photo:
        item["type"] = "photo"
        item["file_id"] = update.message.photo[-1].file_id
    elif update.message.video:
        item["type"] = "video"
        item["file_id"] = update.message.video.file_id
    else:
        return

    if should_cola:
        enqueue_media(uid, item)
        update.message.reply_text("✅ Listo. Guardado en COLA. (sale automático cada 2 horas mientras LIVE esté ON)")
        return

    # Publicación inmediata
    publish_media_to_group(context, int(gid), item, live_on)

def handle_new_members(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update):
        return
    if not WELCOME_ON_JOIN:
        return

    rooms, models, live, streamers, intro, queue, lastpost = load_all()
    gid = str(update.effective_chat.id)
    intro_text = intro.get(gid, "").strip()
    if intro_text:
        try:
            context.bot.send_message(chat_id=update.effective_chat.id, text="📌 " + intro_text)
        except Exception:
            pass

# =========================
# Promo scheduler thread
# =========================
def promo_loop(updater: Updater):
    while True:
        try:
            rooms, models, live, streamers, intro, queue, lastpost = load_all()
            for model_user_id, live_on in list(live.items()):
                if not live_on:
                    continue

                gid = rooms.get(str(model_user_id))
                if not gid:
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

                publish_media_to_group(updater.dispatcher.bot, int(gid), item, True)

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
    dp.add_handler(CommandHandler("cola", cmd_cola))

    # Handlers (robustos)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_group_text))
    dp.add_handler(MessageHandler(Filters.private & Filters.text & ~Filters.command, handle_private_text))
    dp.add_handler(MessageHandler(Filters.private & (Filters.photo | Filters.video), handle_private_media))
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, handle_new_members))

    # Flask thread
    t_web = threading.Thread(target=run_flask, daemon=True)
    t_web.start()

    # Promo scheduler
    t_promo = threading.Thread(target=promo_loop, args=(updater,), daemon=True)
    t_promo.start()

    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
