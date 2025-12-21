# app.py
# CosplayLive Bot (PTB v13.15)
# - streamer selection
# - LIVE toggle
# - translate group<->model
# - promo queue every 2h
# - ORIGINAL media (no marco)
# - Neon GIF text banner (LIVE NOW / NEW) as reply to media
# - Pin intro + pin last promo (multi-pin if chat supports it)

import os
import json
import time
import threading
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# =========================
# ENV
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or os.getenv("TOKEN")
DATA_DIR = os.getenv("DATA_DIR", "/var/data")

GROUP_LANGUAGE = os.getenv("GROUP_LANGUAGE", "de")   # idioma del grupo
MODEL_LANGUAGE = os.getenv("MODEL_LANGUAGE", "pt")   # idioma del streamer/modelo

PROMO_INTERVAL_SECONDS = int(os.getenv("PROMO_INTERVAL_SECONDS", str(2 * 60 * 60)))  # 2h
WELCOME_ON_JOIN = os.getenv("WELCOME_ON_JOIN", "1") == "1"
DEBUG = os.getenv("DEBUG", "1") == "1"

# Si tu grupo NO soporta multi-pin, esto ayuda:
# 1 = re-publicar intro al entrar alguien nuevo (además del pin)
REPOST_INTRO_ON_JOIN = os.getenv("REPOST_INTRO_ON_JOIN", "1") == "1"

# =========================
# Files
# =========================
os.makedirs(DATA_DIR, exist_ok=True)
ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")          # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")        # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")            # model_user_id -> true/false
STREAMERS_FILE = os.path.join(DATA_DIR, "streamers.json")  # group_chat_id -> model_user_id
INTRO_FILE = os.path.join(DATA_DIR, "intro.json")          # group_chat_id -> intro_text
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")          # model_user_id -> {"items":[...], "last_sent": epoch}
PINS_FILE = os.path.join(DATA_DIR, "pins.json")            # group_chat_id -> {"intro_msg_id":..., "last_promo_msg_id":...}

_lock = threading.Lock()

def log(msg: str):
    if DEBUG:
        print(msg, flush=True)

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
        pins = _read_json(PINS_FILE, {})
    return rooms, models, live, streamers, intro, queue, pins

def save_all(rooms=None, models=None, live=None, streamers=None, intro=None, queue=None, pins=None):
    with _lock:
        if rooms is not None: _write_json(ROOMS_FILE, rooms)
        if models is not None: _write_json(MODELS_FILE, models)
        if live is not None: _write_json(LIVE_FILE, live)
        if streamers is not None: _write_json(STREAMERS_FILE, streamers)
        if intro is not None: _write_json(INTRO_FILE, intro)
        if queue is not None: _write_json(QUEUE_FILE, queue)
        if pins is not None: _write_json(PINS_FILE, pins)

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
    except Exception as e:
        log(f"[translate] fallback (error={e}) text='{text[:80]}'")
        return text

# =========================
# Helpers
# =========================
def now_epoch() -> int:
    return int(time.time())

def is_group_chat(chat) -> bool:
    return chat.type in ("group", "supergroup")

def format_informal_hint_de(text: str) -> str:
    t = text
    # evitar Sie/dich mezclado - preferimos "ihr/euch"
    t = t.replace("Möchten Sie", "Wollt ihr")
    t = t.replace("Wollen Sie", "Wollt ihr")
    t = t.replace("Sie ", "ihr ")
    t = t.replace("Ihnen", "euch")
    t = t.replace("Ihr", "euer")
    t = t.replace("dich", "euch")
    return t

def sexy_fallback_line_de():
    return "🔥 Hey ihr… kommt näher 😈"

def get_bound_model_for_group(group_chat_id: str, streamers: dict) -> str:
    return streamers.get(str(group_chat_id), "")

def get_group_for_model(model_user_id: str, rooms: dict) -> str:
    return rooms.get(str(model_user_id), "")

def is_live(model_user_id: str, live: dict) -> bool:
    return bool(live.get(str(model_user_id), False))

# =========================
# Neon GIF Banner (Pillow)
# =========================
def make_neon_banner_gif(kind: str, text: str) -> BytesIO:
    """
    kind: "LIVE NOW" o "NEW"
    animación: sube desde abajo + glow + flicker + flechas + fuego (emoji)
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    W, H = 720, 220
    bg = (8, 8, 10)

    # colores por tipo
    if kind == "LIVE NOW":
        neon = (80, 255, 140)
        accent = (255, 80, 180)
        tag = "LIVE NOW"
        flames = "🔥🔥"
    else:
        neon = (255, 90, 90)
        accent = (120, 200, 255)
        tag = "NEW"
        flames = "🔥"

    # fuentes (Render suele tener DejaVu)
    try:
        ft_tag = ImageFont.truetype("DejaVuSans-Bold.ttf", 46)
        ft_txt = ImageFont.truetype("DejaVuSans-Bold.ttf", 38)
        ft_small = ImageFont.truetype("DejaVuSans.ttf", 26)
    except Exception:
        ft_tag = ImageFont.load_default()
        ft_txt = ImageFont.load_default()
        ft_small = ImageFont.load_default()

    # recortar texto
    text = (text or "").strip()
    if not text:
        text = "Hallo meine Süßen… 😈"
    if len(text) > 80:
        text = text[:77] + "…"

    frames = []
    steps = 18
    for i in range(steps):
        # progresión: aparece desde abajo
        p = i / (steps - 1)
        y_shift = int((1.0 - p) * 30)

        # flicker glow
        flick = 0.65 + 0.35 * (1 if i % 3 else 0.6)

        base = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(base)

        # panel
        d.rounded_rectangle([18, 18, W - 18, H - 18], radius=26, outline=(40, 40, 50), width=2)
        d.rounded_rectangle([24, 24, W - 24, H - 24], radius=22, outline=(20, 20, 30), width=2)

        # flechas laterales
        arrows = "➤➤➤"
        d.text((30, 90), arrows, font=ft_small, fill=accent)
        d.text((W - 130, 90), arrows, font=ft_small, fill=accent)

        # fuego arriba
        d.text((W - 150, 30), flames, font=ft_small, fill=(255, 150, 60))

        # tag neón (glow)
        tag_x, tag_y = 44, 42 + y_shift
        txt_x, txt_y = 44, 110 + y_shift

        # capa glow
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.text((tag_x, tag_y), tag, font=ft_tag, fill=(*neon, int(220 * flick)))
        gd.text((txt_x, txt_y), text, font=ft_txt, fill=(255, 255, 255, int(230 * flick)))

        glow = glow.filter(ImageFilter.GaussianBlur(radius=6))
        base_rgba = base.convert("RGBA")
        base_rgba = Image.alpha_composite(base_rgba, glow)

        # texto normal encima
        d2 = ImageDraw.Draw(base_rgba)
        d2.text((tag_x, tag_y), tag, font=ft_tag, fill=(255, 255, 255))
        d2.text((txt_x, txt_y), text, font=ft_txt, fill=(255, 255, 255))

        # línea inferior
        d2.text((44, 178), "💬 Schreib mir…", font=ft_small, fill=(200, 200, 210))

        frames.append(base_rgba.convert("P"))

    out = BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=70, loop=0, optimize=False)
    out.seek(0)
    return out

# =========================
# Pin helpers
# =========================
def try_pin(context: CallbackContext, chat_id: int, message_id: int):
    """Intenta fijar sin romper nada (si no puede, no pasa nada)."""
    try:
        context.bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
        return True
    except Exception as e:
        log(f"[pin] fail: {e}")
        return False

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
    name = " ".join(context.args).strip()
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, pins = load_all()
    if not name:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return
    models[str(user.id)] = name
    save_all(models=models)
    update.message.reply_text(f"✅ Modelo registrada: {name}\nuser_id: {user.id}")

def cmd_bindchat(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    args = context.args
    if not args:
        update.message.reply_text("Uso: /bindchat <model_user_id>")
        return
    model_user_id = args[0].strip()
    group_chat_id = str(update.effective_chat.id)
    rooms, models, live, streamers, intro, queue, pins = load_all()
    rooms[model_user_id] = group_chat_id
    save_all(rooms=rooms)
    update.message.reply_text(f"✅ Grupo vinculado.\nmodel_user_id: {model_user_id}\nchat_id: {group_chat_id}")

def cmd_setstreamer(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    group_chat_id = str(update.effective_chat.id)
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        update.message.reply_text("Uso: responde a un mensaje del streamer y escribe:\n/setstreamer")
        return

    streamer_user = reply.from_user
    streamer_id = str(streamer_user.id)

    rooms, models, live, streamers, intro, queue, pins = load_all()
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
        "- En el grupo escribe alemán → llega al privado del streamer.\n"
        "- En privado el streamer escribe PT → sale en el grupo traducido."
    )

def cmd_liveon(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, pins = load_all()
    live[str(user.id)] = True
    save_all(live=live)
    update.message.reply_text("🟢 LIVE ON ✅")

def cmd_liveoff(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, pins = load_all()
    live[str(user.id)] = False
    save_all(live=live)
    update.message.reply_text("🔴 LIVE OFF ✅")

def cmd_intro(update: Update, context: CallbackContext):
    """
    /intro texto...
    - Guarda presentación
    - La publica y la PINNEA (Pin 1)
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return

    text = " ".join(context.args).strip()
    if not text:
        update.message.reply_text("Uso: /intro <texto>")
        return

    rooms, models, live, streamers, intro, queue, pins = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro[group_chat_id] = text
    save_all(intro=intro)

    msg = update.message.reply_text(f"📌 {text}")

    # Guardar + pin
    pins.setdefault(group_chat_id, {})
    pins[group_chat_id]["intro_msg_id"] = msg.message_id
    save_all(pins=pins)

    try_pin(context, update.effective_chat.id, msg.message_id)

def cmd_queue(update: Update, context: CallbackContext):
    if not update.message:
        return
    update.message.reply_text(
        "📦 Cola:\n"
        "- En privado manda foto/video con caption empezando con #queue\n"
        "  Ej: #queue hallo meine Süßen… 😈\n"
        "✅ queda en cola\n"
        "Si NO pones #queue → sale instantáneo.\n\n"
        "La cola suelta 1 promo cada 2 horas mientras LIVE esté ON."
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

    rooms, models, live, streamers, intro, queue, pins = load_all()
    group_chat_id = str(update.effective_chat.id)
    model_user_id = get_bound_model_for_group(group_chat_id, streamers)
    if not model_user_id:
        return
    if not is_live(model_user_id, live):
        return

    translated = translate_text(text, GROUP_LANGUAGE, MODEL_LANGUAGE)
    try:
        context.bot.send_message(chat_id=int(model_user_id), text=f"💬 (del grupo) {translated}")
    except Exception as e:
        log(f"[group->private] fail: {e}")

def handle_private_text(update: Update, context: CallbackContext):
    """
    Privado del streamer -> grupo
    Aquí ponemos banner LIVE NOW (texto), pero como mensaje normal (no GIF),
    para no spamear demasiado. Si quieres, también lo hago GIF.
    """
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, pins = load_all()
    model_user_id = str(user.id)
    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    translated = translate_text(text, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated = format_informal_hint_de(translated)

    # Prefijo sutil “LIVE NOW” (sin hacer media)
    final_text = f"🔴 LIVE NOW · {translated}"

    try:
        context.bot.send_message(chat_id=int(group_chat_id), text=final_text)
    except Exception as e:
        log(f"[private->group] fail: {e}")

def enqueue_media(model_user_id: str, item: dict):
    rooms, models, live, streamers, intro, queue, pins = load_all()
    q = queue.get(model_user_id) or {"items": [], "last_sent": 0}
    q["items"].append(item)
    queue[model_user_id] = q
    save_all(queue=queue)

def pin_last_promo(context: CallbackContext, group_chat_id: str, msg_id: int):
    rooms, models, live, streamers, intro, queue, pins = load_all()
    pins.setdefault(group_chat_id, {})
    pins[group_chat_id]["last_promo_msg_id"] = msg_id
    save_all(pins=pins)
    try_pin(context, int(group_chat_id), msg_id)

def send_media_original_plus_neon(context: CallbackContext, group_chat_id: str, kind: str, neon_text: str, media_type: str, file_id: str, caption: str):
    """
    - manda media ORIGINAL (foto/video)
    - manda GIF neon como REPLY al media
    - pinnea el media como "Pin 2: última promo"
    """
    sent = None
    try:
        if media_type == "photo":
            sent = context.bot.send_photo(chat_id=int(group_chat_id), photo=file_id, caption=caption)
        else:
            sent = context.bot.send_video(chat_id=int(group_chat_id), video=file_id, caption=caption)
    except Exception as e:
        log(f"[send media] fail: {e}")
        return

    # Pin 2 = última promo
    try:
        pin_last_promo(context, str(group_chat_id), sent.message_id)
    except Exception as e:
        log(f"[pin last promo] fail: {e}")

    # GIF neon reply
    try:
        gif_io = make_neon_banner_gif(kind, neon_text)
        context.bot.send_animation(
            chat_id=int(group_chat_id),
            animation=gif_io,
            caption="",
            reply_to_message_id=sent.message_id
        )
    except Exception as e:
        log(f"[neon gif] fail: {e}")

def handle_private_media(update: Update, context: CallbackContext):
    """
    Privado del streamer:
    - #queue => cola (NEW)
    - sin #queue => instantáneo (NEW)
    - Si LIVE ON => ok
    """
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, pins = load_all()
    model_user_id = str(user.id)
    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        return

    caption = (update.message.caption or "").strip()
    cap_lower = caption.lower()
    should_queue = cap_lower.startswith("#queue")

    neon_text = caption
    if should_queue:
        parts = caption.split(maxsplit=1)
        neon_text = parts[1].strip() if len(parts) > 1 else ""

    if not neon_text:
        neon_text = "Hallo meine Süßen… 😈"

    # traducimos el texto para el banner (PT->DE)
    neon_de = translate_text(neon_text, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        neon_de = format_informal_hint_de(neon_de)

    # caption debajo del media: puede ser vacío o algo corto
    # (si quieres, aquí puedes poner algo fijo)
    media_caption = ""  # dejamos limpio; el efecto va en el GIF
    # Si quieres algo mínimo:
    # media_caption = " "

    item = {"type": None, "file_id": None, "neon_text": neon_de}

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
        update.message.reply_text("✅ Guardado en cola (NEW). Se soltará cada 2 horas mientras LIVE esté ON.")
        return

    # instantáneo = NEW
    send_media_original_plus_neon(
        context=context,
        group_chat_id=str(group_chat_id),
        kind="NEW",
        neon_text=item["neon_text"],
        media_type=item["type"],
        file_id=item["file_id"],
        caption=media_caption
    )

def handle_new_members(update: Update, context: CallbackContext):
    """
    Cuando entra alguien:
    - intenta asegurar Pin 1 (intro)
    - y si REPOST_INTRO_ON_JOIN=1, re-publica intro para que el nuevo lo vea sin scrollear
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return
    if not WELCOME_ON_JOIN:
        return

    rooms, models, live, streamers, intro, queue, pins = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro_text = intro.get(group_chat_id, "")

    if not intro_text:
        return

    # Repost intro (para que lo vea el nuevo)
    if REPOST_INTRO_ON_JOIN:
        try:
            msg = context.bot.send_message(chat_id=update.effective_chat.id, text=f"📌 {intro_text}")
            # pin intro si no está guardado
            pins.setdefault(group_chat_id, {})
            if not pins[group_chat_id].get("intro_msg_id"):
                pins[group_chat_id]["intro_msg_id"] = msg.message_id
                save_all(pins=pins)
                try_pin(context, update.effective_chat.id, msg.message_id)
        except Exception as e:
            log(f"[join intro] fail: {e}")

# =========================
# Promo loop
# =========================
def promo_loop(bot):
    while True:
        try:
            rooms, models, live, streamers, intro, queue, pins = load_all()
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

                item = items.pop(0)
                q["items"] = items
                q["last_sent"] = now_epoch()
                queue[str(model_user_id)] = q
                save_all(queue=queue)

                # cola = NEW
                try:
                    # media_caption vacío
                    # mandamos ORIGINAL + neon GIF reply
                    # aquí NO tenemos context, solo bot; hacemos pin por bot directamente (sin context)
                    sent = None
                    if item["type"] == "photo":
                        sent = bot.send_photo(chat_id=int(group_chat_id), photo=item["file_id"], caption="")
                    else:
                        sent = bot.send_video(chat_id=int(group_chat_id), video=item["file_id"], caption="")

                    # pin last promo
                    try:
                        pins.setdefault(str(group_chat_id), {})
                        pins[str(group_chat_id)]["last_promo_msg_id"] = sent.message_id
                        save_all(pins=pins)
                        bot.pin_chat_message(chat_id=int(group_chat_id), message_id=sent.message_id, disable_notification=True)
                    except Exception as e2:
                        log(f"[promo pin] fail: {e2}")

                    # neon gif reply
                    try:
                        gif_io = make_neon_banner_gif("NEW", item.get("neon_text", "Hallo… 😈"))
                        bot.send_animation(chat_id=int(group_chat_id), animation=gif_io, reply_to_message_id=sent.message_id)
                    except Exception as e3:
                        log(f"[promo neon] fail: {e3}")

                except Exception as e:
                    log(f"[promo send] fail: {e}")

        except Exception as e:
            log(f"[promo_loop] fail outer: {e}")

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

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whoami", cmd_whoami))
    dp.add_handler(CommandHandler("setmodel", cmd_setmodel))
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("setstreamer", cmd_setstreamer))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))
    dp.add_handler(CommandHandler("intro", cmd_intro))
    dp.add_handler(CommandHandler("queue", cmd_queue))

    # PTB v13: Filters.group cubre group + supergroup
    dp.add_handler(MessageHandler(Filters.group & Filters.text & ~Filters.command, handle_group_text))
    dp.add_handler(MessageHandler(Filters.private & Filters.text & ~Filters.command, handle_private_text))
    dp.add_handler(MessageHandler(Filters.private & (Filters.photo | Filters.video), handle_private_media))
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, handle_new_members))

    # Flask thread
    t_web = threading.Thread(target=run_flask, daemon=True)
    t_web.start()

    # promo thread
    t_promo = threading.Thread(target=promo_loop, args=(updater.bot,), daemon=True)
    t_promo.start()

    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
