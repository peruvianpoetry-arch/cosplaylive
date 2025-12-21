# app.py
# CosplayLive Translate Bot (PTB v13.15) + streamer selection + LIVE toggle
# + promo queue every 2h + animated GIF templates (Pillow-only, no ffmpeg)
#
# Compatible con: python-telegram-bot==13.15

import os
import json
import time
import threading
from io import BytesIO
from flask import Flask
from telegram import Update
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

# Si pones DEBUG=1 verás más logs en Render
DEBUG = os.getenv("DEBUG", "1") == "1"

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
TEMPLATES_FILE = os.path.join(DATA_DIR, "templates.json") # group_chat_id -> template_id (1..10)

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
        templates = _read_json(TEMPLATES_FILE, {})
    return rooms, models, live, streamers, intro, queue, templates

def save_all(rooms=None, models=None, live=None, streamers=None, intro=None, queue=None, templates=None):
    with _lock:
        if rooms is not None: _write_json(ROOMS_FILE, rooms)
        if models is not None: _write_json(MODELS_FILE, models)
        if live is not None: _write_json(LIVE_FILE, live)
        if streamers is not None: _write_json(STREAMERS_FILE, streamers)
        if intro is not None: _write_json(INTRO_FILE, intro)
        if queue is not None: _write_json(QUEUE_FILE, queue)
        if templates is not None: _write_json(TEMPLATES_FILE, templates)

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

def sexy_fallback_line(lang: str) -> str:
    if lang == "de":
        return "🔥 Hey ihr… habt ihr Lust auf was ganz Privates? 😈"
    if lang == "pt":
        return "🔥 Oi… vocês estão a fim de algo bem privado? 😈"
    return "🔥 Hey… want something private? 😈"

def format_informal_hint_de(text: str) -> str:
    # heurística suave para evitar “Sie/dich” mezclado
    t = text
    t = t.replace("Möchten Sie", "Wollt ihr")
    t = t.replace("Wollen Sie", "Wollt ihr")
    t = t.replace("Sie ", "ihr ")
    t = t.replace("Ihnen", "euch")
    t = t.replace("Ihr", "euer")
    t = t.replace("dich", "euch")
    return t

def get_bound_model_for_group(group_chat_id: str, streamers: dict) -> str:
    return streamers.get(str(group_chat_id), "")

def get_group_for_model(model_user_id: str, rooms: dict) -> str:
    return rooms.get(str(model_user_id), "")

def is_live(model_user_id: str, live: dict) -> bool:
    return bool(live.get(str(model_user_id), False))

def get_template_for_group(group_chat_id: str, templates: dict) -> int:
    try:
        t = int(templates.get(str(group_chat_id), 5))
        if t < 1: t = 1
        if t > 10: t = 10
        return t
    except Exception:
        return 5

# =========================
# GIF Templates (Pillow-only)
# =========================
def make_animated_gif_from_photo_bytes(photo_bytes: bytes, template_id: int) -> BytesIO:
    """
    Crea un GIF animado (glow/pulse) SIN ffmpeg.
    Telegram lo envía como animation (GIF).
    """
    from PIL import Image, ImageDraw, ImageFont, ImageEnhance

    base = Image.open(BytesIO(photo_bytes)).convert("RGB")

    # Ajuste simple para que se vea más “pro”
    enhancer = ImageEnhance.Contrast(base)
    base = enhancer.enhance(1.08)

    # Tamaño final
    W = 720
    ratio = W / float(base.size[0])
    H = int(base.size[1] * ratio)
    base = base.resize((W, H), Image.LANCZOS)

    pad = 40
    header_h = 80
    footer_h = 90

    canvas_w = W + pad * 2
    canvas_h = H + pad * 2 + header_h + footer_h

    # 10 estilos base (colores cambian por plantilla)
    style = {
        1: ("EXKLUSIV", "NUR FÜR EUCH", (255, 60, 180)),
        2: ("PREMIERE", "NUR HIER", (120, 255, 120)),
        3: ("LIVE", "JETZT", (255, 120, 0)),
        4: ("EXKLUSIV", "🔥", (120, 200, 255)),
        5: ("EXKLUSIV", "NUR FÜR EUCH", (255, 90, 90)),
        6: ("PREMIERE", "HEUTE", (190, 120, 255)),
        7: ("VIP", "ONLY", (255, 255, 120)),
        8: ("HOT", "EXKLUSIV", (255, 80, 0)),
        9: ("NEU", "PREMIERE", (80, 255, 200)),
        10: ("EXKLUSIV", "PRIVATE", (255, 120, 200)),
    }.get(template_id, ("EXKLUSIV", "NUR FÜR EUCH", (255, 60, 180)))

    title, subtitle, base_color = style

    frames = []
    durations = []
    steps = 18  # cuantos frames

    # Fuentes (si no hay, usa default)
    try:
        font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 44)
        font_sub = ImageFont.truetype("DejaVuSans.ttf", 26)
    except Exception:
        font_title = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    for i in range(steps):
        # pulso
        pulse = (i if i <= steps // 2 else steps - i) / (steps // 2)
        glow = int(8 + pulse * 18)          # grosor glow
        alpha = int(80 + pulse * 120)       # intensidad

        frame = Image.new("RGB", (canvas_w, canvas_h), (10, 10, 10))
        draw = ImageDraw.Draw(frame)

        # header/footer
        draw.rectangle([0, 0, canvas_w, header_h], fill=(0, 0, 0))
        draw.rectangle([0, canvas_h - footer_h, canvas_w, canvas_h], fill=(0, 0, 0))

        # texto
        draw.text((pad, 18), title, font=font_title, fill=(255, 255, 255))
        draw.text((pad, 56), subtitle, font=font_sub, fill=(220, 220, 220))

        # imagen pegada
        x0 = pad
        y0 = pad + header_h
        frame.paste(base, (x0, y0))

        # borde glow animado
        r, g, b = base_color
        c1 = (min(255, r + int(pulse * 30)), min(255, g + int(pulse * 30)), min(255, b + int(pulse * 30)))
        c2 = (max(0, r - 40), max(0, g - 40), max(0, b - 40))

        # “doble borde”
        for t in range(glow, 0, -1):
            mix = (int(c2[0] + (c1[0] - c2[0]) * (t / glow)),
                   int(c2[1] + (c1[1] - c2[1]) * (t / glow)),
                   int(c2[2] + (c1[2] - c2[2]) * (t / glow)))
            # rectángulo alrededor de la foto
            draw.rectangle([x0 - t, y0 - t, x0 + W + t, y0 + H + t], outline=mix, width=2)

        # etiqueta tipo “sticker” esquina
        tag = "★"
        tx = canvas_w - pad - 40
        ty = 24
        draw.text((tx, ty), tag, font=font_title, fill=(255, 255, 255))

        frames.append(frame)
        durations.append(70)  # ms

    out = BytesIO()
    frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=False,
    )
    out.seek(0)
    return out

def download_photo_bytes(bot, file_id: str) -> bytes:
    f = bot.get_file(file_id)
    bio = BytesIO()
    f.download(out=bio)
    return bio.getvalue()

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

    rooms, models, live, streamers, intro, queue, templates = load_all()
    if not name:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return
    models[str(user.id)] = name
    save_all(models=models)
    update.message.reply_text(f"✅ Modelo registrada: {name}\nuser_id: {user.id}")

def cmd_bindchat(update: Update, context: CallbackContext):
    # En el grupo: /bindchat <model_user_id>
    if not update.message or not is_group_chat(update.effective_chat):
        return
    args = context.args
    if not args:
        update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return
    model_user_id = args[0].strip()
    group_chat_id = str(update.effective_chat.id)
    rooms, models, live, streamers, intro, queue, templates = load_all()
    rooms[model_user_id] = group_chat_id
    save_all(rooms=rooms)
    update.message.reply_text(f"✅ Grupo vinculado.\nmodel_user_id: {model_user_id}\nchat_id: {group_chat_id}")

def cmd_setstreamer(update: Update, context: CallbackContext):
    """
    En el grupo:
    - Responde (reply) a un mensaje de Aurora (o de quien será streamer) y escribe: /setstreamer
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return

    group_chat_id = str(update.effective_chat.id)
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        update.message.reply_text("Uso: responde (reply) a un mensaje del streamer y escribe:\n/setstreamer")
        return

    streamer_user = reply.from_user
    streamer_id = str(streamer_user.id)

    rooms, models, live, streamers, intro, queue, templates = load_all()

    streamers[group_chat_id] = streamer_id
    rooms[streamer_id] = group_chat_id

    if streamer_id not in models:
        models[streamer_id] = streamer_user.first_name or "Streamer"

    # si no hay plantilla guardada para este grupo, default 5
    if group_chat_id not in templates:
        templates[group_chat_id] = 5

    save_all(rooms=rooms, streamers=streamers, models=models, templates=templates)

    t_id = get_template_for_group(group_chat_id, templates)

    update.message.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {models.get(streamer_id, 'Streamer')}\n"
        f"user_id: {streamer_id}\n\n"
        "Prueba ahora:\n"
        "- En el grupo escribe algo en alemán → llega traducido al privado del streamer.\n"
        "- En privado, el streamer escribe algo → se publica traducido aquí.\n\n"
        f"🎬 Plantilla activa: {t_id}\nCámbiala con: /plantilla 1..10"
    )

def cmd_liveon(update: Update, context: CallbackContext):
    # Privado
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, templates = load_all()
    live[str(user.id)] = True
    save_all(live=live)
    update.message.reply_text("🟢 LIVE ON ✅\nSe activa traducción + cola de promos (si hay).")

def cmd_liveoff(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, templates = load_all()
    live[str(user.id)] = False
    save_all(live=live)
    update.message.reply_text("🔴 LIVE OFF ✅\nSe detiene traducción + cola.")

def cmd_intro(update: Update, context: CallbackContext):
    # En grupo: /intro texto...
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = " ".join(context.args).strip()
    if not text:
        update.message.reply_text("Uso: /intro <texto>\nEj: /intro Soy Aurora 🔥 23 🇧🇷 ...")
        return

    rooms, models, live, streamers, intro, queue, templates = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro[group_chat_id] = text
    save_all(intro=intro)

    msg = update.message.reply_text(f"📌 Presentación guardada:\n\n{text}")

    # pin si se puede
    try:
        context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True)
    except Exception as e:
        log(f"[pin] no se pudo pinear: {e}")

def cmd_queue(update: Update, context: CallbackContext):
    if not update.message:
        return
    update.message.reply_text(
        "📦 Cola de promos (solo en privado del streamer):\n"
        "- Manda foto/video con caption empezando con: #queue\n"
        "  Ej: #queue hoy me pongo este conjunto 😈\n"
        "✅ Eso lo encola.\n"
        "Si NO pones #queue → se publica inmediatamente.\n\n"
        "La cola se suelta cada 2 horas mientras LIVE esté ON."
    )

def cmd_plantilla(update: Update, context: CallbackContext):
    """
    En el grupo: /plantilla 1..10
    Guarda plantilla activa para ese grupo.
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return
    args = context.args
    if not args:
        update.message.reply_text("Uso: /plantilla 1..10")
        return
    try:
        tid = int(args[0])
        if tid < 1: tid = 1
        if tid > 10: tid = 10
    except Exception:
        update.message.reply_text("Uso: /plantilla 1..10")
        return

    rooms, models, live, streamers, intro, queue, templates = load_all()
    group_chat_id = str(update.effective_chat.id)
    templates[group_chat_id] = tid
    save_all(templates=templates)

    update.message.reply_text(f"🎬 Plantilla activa: {tid}")

# =========================
# Message Handlers
# =========================
def handle_group_text(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    rooms, models, live, streamers, intro, queue, templates = load_all()
    group_chat_id = str(update.effective_chat.id)
    model_user_id = get_bound_model_for_group(group_chat_id, streamers)
    if not model_user_id:
        return
    if not is_live(model_user_id, live):
        return

    translated = translate_text(text, GROUP_LANGUAGE, MODEL_LANGUAGE)

    try:
        context.bot.send_message(
            chat_id=int(model_user_id),
            text=f"💬 (del grupo) {translated}"
        )
    except Exception as e:
        log(f"[group->private] fail: {e}")

def handle_private_text(update: Update, context: CallbackContext):
    """
    Privado del streamer -> grupo (traducido PT->DE).
    """
    if not update.message or is_group_chat(update.effective_chat):
        return

    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, templates = load_all()
    model_user_id = str(user.id)

    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        log(f"[private->group] no group bound for model_user_id={model_user_id}")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    translated = translate_text(text, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated = format_informal_hint_de(translated)

    try:
        context.bot.send_message(chat_id=int(group_chat_id), text=translated)
    except Exception as e:
        log(f"[private->group] fail: {e}")

def enqueue_media(model_user_id: str, item: dict):
    rooms, models, live, streamers, intro, queue, templates = load_all()
    q = queue.get(model_user_id) or {"items": [], "last_sent": 0}
    q["items"].append(item)
    queue[model_user_id] = q
    save_all(queue=queue)

def send_media_with_template(bot, group_chat_id: str, item: dict):
    """
    - Si es photo: crea GIF animado con glow/marco (Pillow) y lo manda como animation.
    - Si es video: manda video normal + GIF animado “preview overlay” separado (sin ffmpeg no se puede fusionar al video).
    """
    t_id = int(item.get("template_id", 5))
    caption = item.get("caption", "") or ""
    mtype = item.get("type")
    file_id = item.get("file_id")

    try:
        if mtype == "photo":
            photo_bytes = download_photo_bytes(bot, file_id)
            gif_io = make_animated_gif_from_photo_bytes(photo_bytes, t_id)
            bot.send_animation(chat_id=int(group_chat_id), animation=gif_io, caption=caption)
            return

        if mtype == "video":
            # 1) video normal
            bot.send_video(chat_id=int(group_chat_id), video=file_id, caption=caption)
            # 2) “preview overlay” (si nos mandan thumbnail/si no, mandamos un gif “banner” simple)
            # Sin frame exacto del video (sin ffmpeg), hacemos un GIF “banner” usando solo texto:
            try:
                from PIL import Image, ImageDraw, ImageFont
                W, H = 720, 420
                im = Image.new("RGB", (W, H), (10, 10, 10))
                d = ImageDraw.Draw(im)
                try:
                    ft = ImageFont.truetype("DejaVuSans-Bold.ttf", 44)
                    fs = ImageFont.truetype("DejaVuSans.ttf", 28)
                except Exception:
                    ft = ImageFont.load_default()
                    fs = ImageFont.load_default()

                title = "🎬 PREMIERE" if t_id % 2 == 0 else "🔥 EXKLUSIV"
                sub = "NUR FÜR EUCH" if t_id % 3 else "JETZT"

                frames = []
                for i in range(16):
                    frame = im.copy()
                    dd = ImageDraw.Draw(frame)
                    pulse = (i if i <= 8 else 16 - i) / 8.0
                    col = (255, int(60 + pulse * 160), int(120 + pulse * 120))
                    dd.rectangle([20, 20, W - 20, H - 20], outline=col, width=8)
                    dd.text((40, 120), title, font=ft, fill=(255, 255, 255))
                    dd.text((40, 190), sub, font=fs, fill=(220, 220, 220))
                    dd.text((40, 260), "▶️ Video oben (Preview)", font=fs, fill=(200, 200, 200))
                    frames.append(frame)

                out = BytesIO()
                frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
                out.seek(0)
                bot.send_animation(chat_id=int(group_chat_id), animation=out, caption="✨ Preview / Overlay")
            except Exception as e2:
                log(f"[video preview gif] fail: {e2}")
            return

    except Exception as e:
        log(f"[send_media_with_template] fail: {e}")
        # fallback simple
        if mtype == "photo":
            bot.send_photo(chat_id=int(group_chat_id), photo=file_id, caption=caption)
        elif mtype == "video":
            bot.send_video(chat_id=int(group_chat_id), video=file_id, caption=caption)

def handle_private_media(update: Update, context: CallbackContext):
    """
    Privado del streamer:
    - caption con #queue => encola
    - sin #queue => publica inmediato
    Siempre requiere LIVE ON (tu regla).
    """
    if not update.message or is_group_chat(update.effective_chat):
        return

    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, templates = load_all()
    model_user_id = str(user.id)

    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        return

    caption = (update.message.caption or "").strip()
    cap_lower = caption.lower()
    should_queue = cap_lower.startswith("#queue")
    clean_caption = caption

    if should_queue:
        parts = caption.split(maxsplit=1)
        clean_caption = parts[1].strip() if len(parts) > 1 else ""

    if not clean_caption:
        clean_caption = sexy_fallback_line(GROUP_LANGUAGE)

    translated_caption = translate_text(clean_caption, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated_caption = format_informal_hint_de(translated_caption)

    # plantilla para este grupo
    t_id = get_template_for_group(str(group_chat_id), templates)

    item = {"type": None, "file_id": None, "caption": translated_caption, "template_id": t_id}

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

    # inmediato
    send_media_with_template(context.bot, group_chat_id, item)

def handle_new_members(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    if not WELCOME_ON_JOIN:
        return

    rooms, models, live, streamers, intro, queue, templates = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro_text = intro.get(group_chat_id, "")
    if intro_text:
        try:
            context.bot.send_message(chat_id=update.effective_chat.id, text=intro_text)
        except Exception as e:
            log(f"[welcome intro] fail: {e}")

# =========================
# Promo scheduler thread
# =========================
def promo_loop(bot):
    while True:
        try:
            rooms, models, live, streamers, intro, queue, templates = load_all()
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

                send_media_with_template(bot, group_chat_id, item)

        except Exception as e:
            log(f"[promo_loop] fail: {e}")

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
    dp.add_handler(CommandHandler("plantilla", cmd_plantilla))

    # ✅ PTB v13: Filters.group ya incluye group + supergroup
    dp.add_handler(MessageHandler(Filters.group & Filters.text & ~Filters.command, handle_group_text))
    dp.add_handler(MessageHandler(Filters.private & Filters.text & ~Filters.command, handle_private_text))
    dp.add_handler(MessageHandler(Filters.private & (Filters.photo | Filters.video), handle_private_media))
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, handle_new_members))

    # Start Flask
    t_web = threading.Thread(target=run_flask, daemon=True)
    t_web.start()

    # Promo scheduler
    t_promo = threading.Thread(target=promo_loop, args=(updater.bot,), daemon=True)
    t_promo.start()

    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
