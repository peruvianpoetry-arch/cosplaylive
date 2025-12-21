# app.py
# CosplayLive Translate Bot (PTB v13.15) + streamer selection + LIVE toggle
# + promo queue every 2h + templates (Pillow + ffmpeg) + test mode + better debug
# Compatible con: python-telegram-bot==13.15

import os
import json
import time
import threading
import tempfile
import subprocess
from typing import Dict, Any, Optional, Tuple

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

# Dueño (para modo test / admin): si no lo pones, el modo test se permite al primer admin que use /owner
OWNER_ID = os.getenv("OWNER_ID")  # opcional

# =========================
# Files
# =========================
os.makedirs(DATA_DIR, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")           # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")         # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")             # model_user_id -> true/false
STREAMERS_FILE = os.path.join(DATA_DIR, "streamers.json")   # group_chat_id -> model_user_id
INTRO_FILE = os.path.join(DATA_DIR, "intro.json")           # group_chat_id -> intro_text
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")           # model_user_id -> {"items":[...], "last_sent": epoch}
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")     # group_chat_id -> {"template_id":int, "test_mode":bool}
OWNER_FILE = os.path.join(DATA_DIR, "owner.json")           # {"owner_id": int}

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
        settings = _read_json(SETTINGS_FILE, {})
        owner = _read_json(OWNER_FILE, {})
    return rooms, models, live, streamers, intro, queue, settings, owner

def save_all(rooms=None, models=None, live=None, streamers=None, intro=None, queue=None, settings=None, owner=None):
    with _lock:
        if rooms is not None: _write_json(ROOMS_FILE, rooms)
        if models is not None: _write_json(MODELS_FILE, models)
        if live is not None: _write_json(LIVE_FILE, live)
        if streamers is not None: _write_json(STREAMERS_FILE, streamers)
        if intro is not None: _write_json(INTRO_FILE, intro)
        if queue is not None: _write_json(QUEUE_FILE, queue)
        if settings is not None: _write_json(SETTINGS_FILE, settings)
        if owner is not None: _write_json(OWNER_FILE, owner)

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
    # Sexy, informal, en plural ("ihr")
    if lang == "de":
        return "🔥 Hey ihr… habt ihr Lust auf was richtig Privates? 😈 Schreib mir…"
    if lang == "pt":
        return "🔥 Oi… vocês tão a fim de algo bem privado? 😈 Me chama…"
    return "🔥 Hey… want something private? 😈"

def format_informal_hint_de(text: str) -> str:
    # Heurística suave para evitar Sie/dich mezclado
    t = text or ""
    # Cambios típicos
    t = t.replace("Möchten Sie", "Wollt ihr")
    t = t.replace("Wollen Sie", "Wollt ihr")
    t = t.replace("Sie ", "ihr ")
    t = t.replace("Ihnen", "euch")
    t = t.replace("Ihr", "euer")
    t = t.replace("dein", "euer")
    t = t.replace("dir", "euch")
    return t

def get_bound_model_for_group(group_chat_id: str, streamers: dict) -> str:
    return streamers.get(str(group_chat_id), "")

def get_group_for_model(model_user_id: str, rooms: dict) -> str:
    return rooms.get(str(model_user_id), "")

def is_live(model_user_id: str, live: dict) -> bool:
    return bool(live.get(str(model_user_id), False))

def get_owner_id(owner: dict) -> Optional[int]:
    # prioridad: ENV > owner.json
    if OWNER_ID:
        try:
            return int(OWNER_ID)
        except Exception:
            pass
    try:
        if "owner_id" in owner:
            return int(owner["owner_id"])
    except Exception:
        pass
    return None

def set_owner_if_missing(update: Update):
    # si no hay owner y el user es admin del grupo, lo guardamos
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    if get_owner_id(owner) is not None:
        return
    u = update.effective_user
    if not u:
        return
    owner = {"owner_id": int(u.id)}
    save_all(owner=owner)

def is_owner(update: Update) -> bool:
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    oid = get_owner_id(owner)
    u = update.effective_user
    return bool(oid and u and int(u.id) == int(oid))

def ensure_group_settings(group_chat_id: str, settings: dict) -> dict:
    if group_chat_id not in settings:
        settings[group_chat_id] = {"template_id": 5, "test_mode": False}
    else:
        if "template_id" not in settings[group_chat_id]:
            settings[group_chat_id]["template_id"] = 5
        if "test_mode" not in settings[group_chat_id]:
            settings[group_chat_id]["test_mode"] = False
    return settings

# =========================
# Templates (10)
# =========================
TEMPLATES = {
    1: {"title": "EXKLUSIV",  "sub": "NUR FÜR EUCH",    "badge": "🔥"},
    2: {"title": "PREMIERE",  "sub": "HEUTE NACHT",    "badge": "💋"},
    3: {"title": "LIVE",      "sub": "JETZT ONLINE",   "badge": "🔴"},
    4: {"title": "HOT",       "sub": "PRIVATE SHOW",   "badge": "😈"},
    5: {"title": "NEU",       "sub": "FRISCHES UPDATE","badge": "✨"},
    6: {"title": "VIP",       "sub": "NUR FÜR FANS",   "badge": "👑"},
    7: {"title": "NACHT",     "sub": "SPÄT & HEISS",   "badge": "🌙"},
    8: {"title": "SPECIAL",   "sub": "LIMITIERT",      "badge": "⚡"},
    9: {"title": "SECRET",    "sub": "KOMM REIN…",     "badge": "🫦"},
    10:{"title": "PREVIEW",   "sub": "KLEINER TEASER", "badge": "🎬"},
}

def _find_font():
    # Render linux suele tener DejaVu
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def apply_frame_image(input_path: str, output_path: str, template_id: int) -> bool:
    """Aplica marco glow + textos sobre una imagen. Retorna True si ok."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        tpl = TEMPLATES.get(template_id, TEMPLATES[5])
        im = Image.open(input_path).convert("RGB")
        w, h = im.size

        # Marco base
        border = max(18, int(min(w, h) * 0.02))
        canvas = Image.new("RGB", (w + border*2, h + border*2), (10, 10, 10))
        canvas.paste(im, (border, border))

        draw = ImageDraw.Draw(canvas)

        # Glow: dibujar rectángulos repetidos semi-transparent (simulación glow)
        glow_layers = 10
        for i in range(glow_layers):
            alpha = int(180 * (1 - i/glow_layers))
            # color violeta/rosa
            color = (220, 80 + i*5, 200)
            inset = i*2
            draw.rectangle(
                [inset, inset, canvas.size[0]-inset-1, canvas.size[1]-inset-1],
                outline=color, width=3
            )
        canvas = canvas.filter(ImageFilter.SMOOTH_MORE)

        # Tipografía
        font_path = _find_font()
        if font_path:
            title_font = ImageFont.truetype(font_path, size=max(28, int(canvas.size[0]*0.05)))
            sub_font   = ImageFont.truetype(font_path, size=max(18, int(canvas.size[0]*0.03)))
        else:
            title_font = sub_font = None

        # Texto superior izq
        pad = 18
        title = f"{tpl['badge']} {tpl['title']}"
        sub = tpl["sub"]

        # Fondo semitransparente (simulado con rectángulo sólido oscuro)
        box_h = int(canvas.size[1]*0.12)
        draw.rectangle([0, 0, canvas.size[0], box_h], fill=(0, 0, 0))

        draw.text((pad, int(box_h*0.18)), title, fill=(255, 255, 255), font=title_font)
        draw.text((pad, int(box_h*0.62)), sub, fill=(255, 170, 255), font=sub_font)

        canvas.save(output_path, "JPEG", quality=92)
        return True
    except Exception:
        return False

def run_ffmpeg_make_animated_from_image(img_path: str, out_mp4: str, template_id: int) -> bool:
    """
    Convierte imagen ya enmarcada a mp4 con animación visible:
    - zoom suave
    - ligero brillo/pulse
    """
    try:
        tpl = TEMPLATES.get(template_id, TEMPLATES[5])
        # Duración 4.5s para que se note
        dur = 4.5
        # Filtro: zoompan + eq + drawtext extra (pequeño) opcional
        # Nota: drawtext requiere fonts; si falla, igual sirve sin drawtext.
        vf = (
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            "zoompan=z='min(zoom+0.0015,1.08)':d=125:s=trunc(iw/2)*2xtrunc(ih/2)*2,"
            "eq=brightness=0.02:saturation=1.15"
        )
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", img_path,
            "-t", str(dur),
            "-vf", vf,
            "-r", "25",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            out_mp4
        ]
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False

def process_photo_to_template_mp4(bot, file_id: str, template_id: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Descarga foto, aplica marco, genera mp4 animado.
    Retorna (mp4_path, err)
    """
    try:
        tf = tempfile.mkdtemp(prefix="cosplay_")
        raw_path = os.path.join(tf, "in.jpg")
        framed_path = os.path.join(tf, "framed.jpg")
        out_mp4 = os.path.join(tf, "out.mp4")

        telegram_file = bot.get_file(file_id)
        telegram_file.download(custom_path=raw_path)

        ok = apply_frame_image(raw_path, framed_path, template_id)
        if not ok:
            return None, "Pillow no pudo aplicar marco"
        ok2 = run_ffmpeg_make_animated_from_image(framed_path, out_mp4, template_id)
        if not ok2:
            # fallback: devolver framed jpg (como "error" mp4)
            return framed_path, "ffmpeg no generó animación, uso JPG"
        return out_mp4, None
    except Exception as e:
        return None, f"process_photo error: {e}"

# =========================
# Commands
# =========================
def cmd_start(update: Update, context: CallbackContext):
    # Si alguien hace /start, lo tomamos como posible dueño si no está set
    set_owner_if_missing(update)
    update.message.reply_text("✅ Bot funcionando correctamente")

def cmd_whoami(update: Update, context: CallbackContext):
    u = update.effective_user
    update.message.reply_text(f"👤 Tu user_id: {u.id}\nUsername: @{u.username}" if u else "No pude leer tu user_id")

def cmd_setmodel(update: Update, context: CallbackContext):
    # /setmodel Aurora (en privado o donde sea)
    if not update.message:
        return
    args = context.args
    name = " ".join(args).strip() if args else ""
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    if not name:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return
    models[str(user.id)] = name
    save_all(models=models)
    update.message.reply_text(f"✅ Modelo registrada: {name}\nuser_id: {user.id}")

def cmd_bindchat(update: Update, context: CallbackContext):
    # /bindchat <model_user_id> en grupo
    if not update.message or not is_group_chat(update.effective_chat):
        return
    set_owner_if_missing(update)
    args = context.args
    if not args:
        update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return
    model_user_id = args[0].strip()
    group_chat_id = str(update.effective_chat.id)
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    rooms[model_user_id] = group_chat_id
    save_all(rooms=rooms)
    update.message.reply_text(f"✅ Grupo vinculado.\nmodel_user_id: {model_user_id}\nchat_id: {group_chat_id}")

def cmd_setstreamer(update: Update, context: CallbackContext):
    """
    En el grupo: responde a un mensaje de Aurora y escribe /setstreamer
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return
    set_owner_if_missing(update)
    group_chat_id = str(update.effective_chat.id)
    reply = update.message.reply_to_message
    if not reply or not reply.from_user:
        update.message.reply_text("Uso: responde (reply) a un mensaje de Aurora y escribe:\n/setstreamer")
        return

    streamer_user = reply.from_user
    streamer_id = str(streamer_user.id)

    rooms, models, live, streamers, intro, queue, settings, owner = load_all()

    streamers[group_chat_id] = streamer_id
    rooms[streamer_id] = group_chat_id

    if streamer_id not in models:
        models[streamer_id] = streamer_user.first_name or "Streamer"

    # settings por grupo
    settings = ensure_group_settings(group_chat_id, settings)

    save_all(rooms=rooms, streamers=streamers, models=models, settings=settings)

    update.message.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {models.get(streamer_id, 'Streamer')}\n"
        f"user_id: {streamer_id}\n\n"
        "Prueba ahora:\n"
        "- En el grupo escribe algo en alemán → se enviará traducido al privado del streamer.\n"
        "- En privado, el streamer escribe algo → se publicará traducido aquí.\n\n"
        f"🎬 Plantilla activa: {settings[group_chat_id]['template_id']}\n"
        "Cámbiala con: /plantilla 1..10",
        parse_mode=ParseMode.HTML
    )

def cmd_liveon(update: Update, context: CallbackContext):
    # PRIVADO por streamer/modelo
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    live[str(user.id)] = True
    save_all(live=live)
    update.message.reply_text("🟢 LIVE ON ✅\nAhora: traducción + cola (si hay) + publicación de media.")

def cmd_liveoff(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    live[str(user.id)] = False
    save_all(live=live)
    update.message.reply_text("🔴 LIVE OFF ✅\nSe detiene traducción y cola.")

def cmd_intro(update: Update, context: CallbackContext):
    """
    /intro <texto...> en grupo
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return
    set_owner_if_missing(update)
    text = " ".join(context.args).strip()
    if not text:
        update.message.reply_text("Uso: /intro <texto>\nEj: /intro Ich bin Aurora 🔥 23 🇧🇷 ...")
        return
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro[group_chat_id] = text
    save_all(intro=intro)
    msg = update.message.reply_text(f"📌 Vorstellung (Pinned):\n\n{text}")
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
        "  #queue  o  /queue\n"
        "✅ Eso lo encola.\n"
        "Si NO pones #queue → se publica inmediatamente.\n\n"
        f"La cola se suelta cada {int(PROMO_INTERVAL_SECONDS/3600)} horas mientras LIVE esté ON."
    )

def cmd_template(update: Update, context: CallbackContext):
    """
    /plantilla 5   (en grupo)
    Aliases: /plantila, /template, /plantilla5, /template5, etc.
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return
    set_owner_if_missing(update)
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    group_chat_id = str(update.effective_chat.id)
    settings = ensure_group_settings(group_chat_id, settings)

    # parse args si existe
    tid = None
    if context.args and context.args[0].isdigit():
        tid = int(context.args[0])
    else:
        # puede venir como /plantilla5
        txt = (update.message.text or "").strip().lower()
        digits = "".join([c for c in txt if c.isdigit()])
        if digits.isdigit():
            tid = int(digits)

    if not tid or tid < 1 or tid > 10:
        update.message.reply_text("Uso: /plantilla 1..10  (o /template 1..10)")
        return

    settings[group_chat_id]["template_id"] = tid
    save_all(settings=settings)
    tpl = TEMPLATES[tid]
    update.message.reply_text(f"🎬 Plantilla {tid} activada: {tpl['badge']} {tpl['title']} — {tpl['sub']}")

def cmd_teston(update: Update, context: CallbackContext):
    # solo dueño, en grupo
    if not update.message or not is_group_chat(update.effective_chat):
        return
    set_owner_if_missing(update)
    if not is_owner(update):
        update.message.reply_text("⛔ Solo el dueño puede activar TEST.")
        return
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    group_chat_id = str(update.effective_chat.id)
    settings = ensure_group_settings(group_chat_id, settings)
    settings[group_chat_id]["test_mode"] = True
    save_all(settings=settings)
    update.message.reply_text("🧪 TEST MODE ON ✅\nAhora el bot simula respuesta aunque Aurora no esté.")

def cmd_testoff(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    set_owner_if_missing(update)
    if not is_owner(update):
        update.message.reply_text("⛔ Solo el dueño puede desactivar TEST.")
        return
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    group_chat_id = str(update.effective_chat.id)
    settings = ensure_group_settings(group_chat_id, settings)
    settings[group_chat_id]["test_mode"] = False
    save_all(settings=settings)
    update.message.reply_text("🧪 TEST MODE OFF ✅")

# =========================
# Message Handlers
# =========================
def handle_group_text(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    group_chat_id = str(update.effective_chat.id)
    settings = ensure_group_settings(group_chat_id, settings)

    model_user_id = get_bound_model_for_group(group_chat_id, streamers)
    if not model_user_id:
        return

    # Si LIVE está ON, manda al privado traducido
    if is_live(model_user_id, live):
        translated = translate_text(text, GROUP_LANGUAGE, MODEL_LANGUAGE)
        try:
            context.bot.send_message(chat_id=int(model_user_id), text=f"💬 (grupo) {translated}")
        except Exception:
            pass

    # Si TEST MODE, responde en el grupo como si fuera Aurora (para que pruebes sin ella)
    if settings[group_chat_id].get("test_mode"):
        fake_pt = "Oi amor… tô aqui 😈"
        fake_de = translate_text(fake_pt, MODEL_LANGUAGE, GROUP_LANGUAGE)
        if GROUP_LANGUAGE == "de":
            fake_de = format_informal_hint_de(fake_de)
        try:
            context.bot.send_message(chat_id=update.effective_chat.id, text=f"🔥 {fake_de}")
        except Exception:
            pass

def _debug_to_streamer(context: CallbackContext, model_user_id: str, msg: str):
    try:
        context.bot.send_message(chat_id=int(model_user_id), text=f"⚠️ DEBUG: {msg}")
    except Exception:
        pass

def handle_private_text(update: Update, context: CallbackContext):
    # Privado streamer -> grupo
    if not update.message or is_group_chat(update.effective_chat):
        return

    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    model_user_id = str(user.id)

    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        _debug_to_streamer(context, model_user_id, "No hay grupo vinculado (rooms.json vacío para tu user_id). Usa /setstreamer en el grupo.")
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
        # Avisa SIEMPRE en privado por qué no llegó
        _debug_to_streamer(context, model_user_id, f"No pude publicar al grupo. chat_id={group_chat_id} error={e}")

def enqueue_media(model_user_id: str, item: dict):
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    q = queue.get(model_user_id) or {"items": [], "last_sent": 0}
    q["items"].append(item)
    queue[model_user_id] = q
    save_all(queue=queue)

def _get_active_template_for_group(group_chat_id: str) -> int:
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    settings = ensure_group_settings(group_chat_id, settings)
    return int(settings[group_chat_id].get("template_id", 5))

def handle_private_media(update: Update, context: CallbackContext):
    """
    En privado:
    - caption empieza con #queue o /queue => encola
    - si no => publica inmediato
    Además:
    - aplica plantilla (marco + animación) a fotos cuando posible
    """
    if not update.message or is_group_chat(update.effective_chat):
        return

    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
    model_user_id = str(user.id)

    if not is_live(model_user_id, live):
        return

    group_chat_id = get_group_for_model(model_user_id, rooms)
    if not group_chat_id:
        _debug_to_streamer(context, model_user_id, "No hay grupo vinculado. Selecciona streamer en el grupo con /setstreamer.")
        return

    caption = (update.message.caption or "").strip()
    cap_lower = caption.lower()

    should_queue = cap_lower.startswith("#queue") or cap_lower.startswith("/queue")
    clean_caption = caption
    if should_queue:
        parts = caption.split(maxsplit=1)
        clean_caption = parts[1].strip() if len(parts) > 1 else ""

    if not clean_caption:
        clean_caption = sexy_fallback_line(GROUP_LANGUAGE)

    translated_caption = translate_text(clean_caption, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated_caption = format_informal_hint_de(translated_caption)

    item = {"type": None, "file_id": None, "caption": translated_caption, "template": None}

    # Capturar template del grupo
    tpl_id = _get_active_template_for_group(str(group_chat_id))
    item["template"] = tpl_id

    # Detectar media
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
        update.message.reply_text("✅ Guardado en cola. Se publicará según intervalo mientras LIVE esté ON.")
        return

    # Publicación inmediata
    try:
        # Foto: intentamos plantilla (marco + animación mp4)
        if item["type"] == "photo":
            out_path, err = process_photo_to_template_mp4(context.bot, item["file_id"], tpl_id)
            if out_path and out_path.endswith(".mp4"):
                with open(out_path, "rb") as f:
                    context.bot.send_video(chat_id=int(group_chat_id), video=f, caption=item["caption"])
            elif out_path and out_path.endswith(".jpg"):
                # fallback: imagen con marco
                with open(out_path, "rb") as f:
                    context.bot.send_photo(chat_id=int(group_chat_id), photo=f, caption=item["caption"])
                if err:
                    _debug_to_streamer(context, model_user_id, f"Fallback JPG: {err}")
            else:
                # fallback absoluto: sin marco
                context.bot.send_photo(chat_id=int(group_chat_id), photo=item["file_id"], caption=item["caption"])
                if err:
                    _debug_to_streamer(context, model_user_id, f"Sin marco (error): {err}")
        else:
            # Video real: por ahora lo mandamos tal cual (no lo re-encodeamos para no romper y por costo)
            context.bot.send_video(chat_id=int(group_chat_id), video=item["file_id"], caption=item["caption"])
    except Exception as e:
        _debug_to_streamer(context, model_user_id, f"Error publicando media al grupo: {e}")

def handle_new_members(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    if not WELCOME_ON_JOIN:
        return
    rooms, models, live, streamers, intro, queue, settings, owner = load_all()
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
    Cada 60s:
    - Para cada modelo con LIVE ON
    - Si tiene cola y ya pasó el intervalo
    - Publica 1 item al grupo y actualiza last_sent
    """
    while True:
        try:
            rooms, models, live, streamers, intro, queue, settings, owner = load_all()
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

                tpl_id = int(item.get("template") or _get_active_template_for_group(str(group_chat_id)))

                try:
                    if item["type"] == "photo":
                        out_path, err = process_photo_to_template_mp4(bot, item["file_id"], tpl_id)
                        if out_path and out_path.endswith(".mp4"):
                            with open(out_path, "rb") as f:
                                bot.send_video(chat_id=int(group_chat_id), video=f, caption=item.get("caption", ""))
                        elif out_path and out_path.endswith(".jpg"):
                            with open(out_path, "rb") as f:
                                bot.send_photo(chat_id=int(group_chat_id), photo=f, caption=item.get("caption", ""))
                        else:
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

    # Plantillas (aliases)
    dp.add_handler(CommandHandler(["plantilla", "plantila", "template"], cmd_template))

    # Test mode
    dp.add_handler(CommandHandler("teston", cmd_teston))
    dp.add_handler(CommandHandler("testoff", cmd_testoff))

    # Group text -> model private + optional TEST response
    dp.add_handler(MessageHandler(Filters.group & Filters.text & ~Filters.command, handle_group_text))
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
