# app.py
# CosplayLive Translate Bot (PTB v13.15) + streamer selection + LIVE toggle + promo queue every 2h
# Compatible con: python-telegram-bot==13.15

import os
import json
import time
import threading
from datetime import datetime, timezone

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

# Cola: cada cuántos segundos se suelta 1 promo cuando LIVE está ON
PROMO_INTERVAL_SECONDS = int(os.getenv("PROMO_INTERVAL_SECONDS", str(2 * 60 * 60)))  # default 2h

# Si quieres que el bot postee algo cuando alguien nuevo entra
WELCOME_ON_JOIN = os.getenv("WELCOME_ON_JOIN", "1") == "1"

# =========================
# Files
# =========================
os.makedirs(DATA_DIR, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")       # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")     # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")         # model_user_id -> true/false
STREAMERS_FILE = os.path.join(DATA_DIR, "streamers.json")  # group_chat_id -> model_user_id
INTRO_FILE = os.path.join(DATA_DIR, "intro.json")       # group_chat_id -> intro_text
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")       # model_user_id -> {"items":[...], "last_sent": epoch}

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
    return rooms, models, live, streamers, intro, queue

def save_all(rooms=None, models=None, live=None, streamers=None, intro=None, queue=None):
    with _lock:
        if rooms is not None: _write_json(ROOMS_FILE, rooms)
        if models is not None: _write_json(MODELS_FILE, models)
        if live is not None: _write_json(LIVE_FILE, live)
        if streamers is not None: _write_json(STREAMERS_FILE, streamers)
        if intro is not None: _write_json(INTRO_FILE, intro)
        if queue is not None: _write_json(QUEUE_FILE, queue)

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
        # Si falla el traductor, devolvemos el texto original
        return text

# =========================
# Helpers
# =========================
def now_epoch() -> int:
    return int(time.time())

def is_group_chat(chat) -> bool:
    return chat.type in ("group", "supergroup")

def sexy_fallback_line(lang: str) -> str:
    # Mantenerlo sexy / sugerente sin volverse extremadamente gráfico
    if lang == "de":
        return "🔥 Na, Lust auf etwas ganz Privates? Schreib mir… 😈"
    if lang == "pt":
        return "🔥 Oi… tá a fim de algo bem privado? Me chama… 😈"
    return "🔥 Hey… want something private? 😈"

def format_informal_hint_de(text: str) -> str:
    # No fuerza nada, solo evita “Sie/Dich” mezclado si el traductor se equivoca.
    # Se usa como fallback: si detecta "Sie" en contexto romántico, lo cambia a "du" simple.
    # (Es heurística, no perfecta.)
    t = text
    t = t.replace("Möchten Sie", "Willst du")
    t = t.replace("Sie ", "du ")
    t = t.replace("Ihnen", "dir")
    t = t.replace("Ihr", "dein")
    return t

def get_bound_model_for_group(group_chat_id: str, streamers: dict) -> str:
    return streamers.get(str(group_chat_id), "")

def get_group_for_model(model_user_id: str, rooms: dict) -> str:
    return rooms.get(str(model_user_id), "")

def is_live(model_user_id: str, live: dict) -> bool:
    return bool(live.get(str(model_user_id), False))

# =========================
# Commands
# =========================
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text("✅ Bot funcionando correctamente")

def cmd_whoami(update: Update, context: CallbackContext):
    u = update.effective_user
    update.message.reply_text(f"👤 Tu user_id: {u.id}\nUsername: @{u.username}" if u else "No pude leer tu user_id")

def cmd_setmodel(update: Update, context: CallbackContext):
    # Se usa en privado por la modelo (o el dueño si quiere registrar un nombre)
    # /setmodel Aurora
    if not update.message:
        return
    args = context.args
    name = " ".join(args).strip() if args else ""
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue = load_all()
    if not name:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return
    models[str(user.id)] = name
    save_all(models=models)
    update.message.reply_text(f"✅ Modelo registrada: {name}\nuser_id: {user.id}")

def cmd_bindchat(update: Update, context: CallbackContext):
    # Se ejecuta en el GRUPO por admin:
    # /bindchat <model_user_id>
    if not update.message or not is_group_chat(update.effective_chat):
        return
    args = context.args
    if not args:
        update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return
    model_user_id = args[0].strip()
    group_chat_id = str(update.effective_chat.id)

    rooms, models, live, streamers, intro, queue = load_all()
    rooms[model_user_id] = group_chat_id
    save_all(rooms=rooms)
    update.message.reply_text(f"✅ Grupo vinculado.\nmodel_user_id: {model_user_id}\nchat_id: {group_chat_id}")

def cmd_setstreamer(update: Update, context: CallbackContext):
    """
    Se ejecuta en el GRUPO.
    Forma fácil para ti:
    - Responde (reply) a un mensaje de Aurora y escribe: /setstreamer
    Así el bot toma el user_id del mensaje respondido.
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return

    group_chat_id = str(update.effective_chat.id)
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        update.message.reply_text("Uso: responde (reply) a un mensaje de Aurora y escribe:\n/setstreamer")
        return

    streamer_user = reply.from_user
    streamer_id = str(streamer_user.id)

    rooms, models, live, streamers, intro, queue = load_all()

    # Guardar streamer para este grupo
    streamers[group_chat_id] = streamer_id

    # Asegurar rooms vinculado también (model->grupo)
    rooms[streamer_id] = group_chat_id

    # Si no existe nombre en models, guardar uno básico
    if streamer_id not in models:
        models[streamer_id] = streamer_user.first_name or "Streamer"

    save_all(rooms=rooms, streamers=streamers, models=models)

    update.message.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {models.get(streamer_id, 'Streamer')}\n"
        f"user_id: {streamer_id}\n\n"
        "Prueba ahora:\n"
        "- En el grupo escribe algo en alemán → se enviará traducido al privado del streamer.\n"
        "- En privado, el streamer escribe algo → se publicará traducido aquí.",
        parse_mode=ParseMode.HTML
    )

def cmd_liveon(update: Update, context: CallbackContext):
    # Se ejecuta en PRIVADO por la modelo/streamer
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue = load_all()
    live[str(user.id)] = True
    save_all(live=live)
    update.message.reply_text("🟢 LIVE ON ✅\nDesde ahora se traducen mensajes + se habilita la cola (si hay).")

def cmd_liveoff(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue = load_all()
    live[str(user.id)] = False
    save_all(live=live)
    update.message.reply_text("🔴 LIVE OFF ✅\nSe detiene traducción y cola.")

def cmd_intro(update: Update, context: CallbackContext):
    """
    /intro <texto...>
    - Se ejecuta en el grupo por admin o por el dueño.
    - Guarda el texto y el bot intenta pinearlo (si es admin y tiene permiso).
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = " ".join(context.args).strip()
    if not text:
        update.message.reply_text("Uso: /intro <texto>\nEj: /intro Soy Aurora 🔥 23 🇧🇷 ...")
        return

    rooms, models, live, streamers, intro, queue = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro[group_chat_id] = text
    save_all(intro=intro)

    msg = update.message.reply_text(f"📌 Presentación guardada:\n\n{text}")
    try:
        context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
    except Exception:
        # Si no puede pin, igual queda guardado y se reenvía cuando entra alguien nuevo
        pass

def cmd_queue(update: Update, context: CallbackContext):
    """
    /queue  -> explica cómo encolar promos
    """
    if not update.message:
        return
    update.message.reply_text(
        "📦 Cola de promos:\n"
        "En privado (streamer) manda foto/video con caption empezando con:\n"
        "  #queue  o  /queue\n"
        "✅ Eso lo encola.\n"
        "Si NO pones #queue → se publica inmediatamente.\n\n"
        "La cola se suelta cada 2 horas mientras LIVE esté ON.",
    )

# =========================
# Message Handlers
# =========================
def handle_group_text(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    rooms, models, live, streamers, intro, queue = load_all()
    group_chat_id = str(update.effective_chat.id)
    model_user_id = get_bound_model_for_group(group_chat_id, streamers)
    if not model_user_id:
        # No hay streamer seleccionado todavía
        return
    if not is_live(model_user_id, live):
        return

    # Traduce DE -> PT y envía al privado del streamer
    translated = translate_text(text, GROUP_LANGUAGE, MODEL_LANGUAGE)
    try:
        context.bot.send_message(
            chat_id=int(model_user_id),
            text=f"💬 (del grupo) {translated}"
        )
    except Exception:
        pass

def handle_private_text(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return

    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue = load_all()
    model_user_id = str(user.id)

    # Solo el streamer/modelo (cuando LIVE ON) puede publicar al grupo
    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    translated = translate_text(text, MODEL_LANGUAGE, GROUP_LANGUAGE)

    # Forzar tono informal "du" de forma suave (evita Sie/dich mezclado)
    if GROUP_LANGUAGE == "de":
        translated = format_informal_hint_de(translated)

    try:
        context.bot.send_message(
            chat_id=int(group_chat_id),
            text=translated
        )
    except Exception:
        pass

def enqueue_media(model_user_id: str, item: dict):
    rooms, models, live, streamers, intro, queue = load_all()
    q = queue.get(model_user_id) or {"items": [], "last_sent": 0}
    q["items"].append(item)
    queue[model_user_id] = q
    save_all(queue=queue)

def handle_private_media(update: Update, context: CallbackContext):
    """
    En privado:
    - Si caption empieza con #queue o /queue => encola para publicar cada 2 horas
    - Si no => publica inmediato al grupo (si LIVE ON y hay grupo)
    """
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue = load_all()
    model_user_id = str(user.id)

    # Si no está LIVE, ignoramos (según tu regla: "Siempre que esté en liveon manda todo")
    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        return

    caption = (update.message.caption or "").strip()
    cap_lower = caption.lower()

    should_queue = cap_lower.startswith("#queue") or cap_lower.startswith("/queue")
    clean_caption = caption
    if should_queue:
        # quitar el prefijo "#queue" o "/queue"
        parts = caption.split(maxsplit=1)
        clean_caption = parts[1].strip() if len(parts) > 1 else ""

    # Si caption vacío, poner una línea sexy por defecto (en alemán porque se publica al grupo)
    if not clean_caption:
        clean_caption = sexy_fallback_line(GROUP_LANGUAGE)

    # Si viene en portugués, traducir a alemán (y forzar informal)
    translated_caption = translate_text(clean_caption, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated_caption = format_informal_hint_de(translated_caption)

    # Detecta tipo de media
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
        update.message.reply_text("✅ Guardado en cola. Se publicará cada 2 horas mientras LIVE esté ON.")
        return

    # Publicación inmediata
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

    rooms, models, live, streamers, intro, queue = load_all()
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
    """
    Cada minuto:
    - Para cada modelo con LIVE ON
    - Si tiene cola y ya pasó el intervalo
    - Publica 1 item al grupo y actualiza last_sent
    """
    while True:
        try:
            rooms, models, live, streamers, intro, queue = load_all()

            for model_user_id, live_on in list(live.items()):
                if not live_on:
                    continue

                group_chat_id = rooms.get(str(model_user_id))
                if not group_chat_id:
                    continue

                q = queue.get(str(model_user_id)) or {"items": [], "last_sent": 0}
                items = q.get("items", [])
                last_sent = int(q.get("last_sent", 0))
                if not items:
                    continue

                if now_epoch() - last_sent < PROMO_INTERVAL_SECONDS:
                    continue

                # Pop 1
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

    # Group text -> model private
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_text))

    # Private text -> group
    dp.add_handler(MessageHandler(Filters.private & Filters.text & ~Filters.command, handle_private_text))

    # Private media from model
    dp.add_handler(MessageHandler(Filters.private & (Filters.photo | Filters.video), handle_private_media))

    # New members in group
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, handle_new_members))

    # Start Flask in background
    t_web = threading.Thread(target=run_flask, daemon=True)
    t_web.start()

    # Start promo scheduler in background
    t_promo = threading.Thread(target=promo_loop, args=(updater.bot,), daemon=True)
    t_promo.start()

    # Polling
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
