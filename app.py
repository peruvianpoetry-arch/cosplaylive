# app.py
# CosplayLive Translate Bot (PTB v13.15) + streamer selection + LIVE toggle
# + promo queue (cada 2h) + intro on join
# + TEST MODE (sin Aurora) + efectos opcionales (Pillow + ffmpeg best-effort)

import os
import json
import time
import threading
import tempfile
import shutil
import subprocess
from typing import Dict, Any, Tuple

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

# Si quieres que el bot postee intro cuando alguien entra
WELCOME_ON_JOIN = os.getenv("WELCOME_ON_JOIN", "1") == "1"

# Efectos opcionales:
ENABLE_EFFECTS = os.getenv("ENABLE_EFFECTS", "1") == "1"   # 1=ON, 0=OFF
# Si quieres modo test rápido, puedes setear PROMO_INTERVAL_SECONDS=120 (2 minutos)

# =========================
# Files
# =========================
os.makedirs(DATA_DIR, exist_ok=True)

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")            # model_user_id -> group_chat_id
MODELS_FILE = os.path.join(DATA_DIR, "models.json")          # model_user_id -> model_name
LIVE_FILE = os.path.join(DATA_DIR, "live.json")              # model_user_id -> true/false
STREAMERS_FILE = os.path.join(DATA_DIR, "streamers.json")    # group_chat_id -> model_user_id
INTRO_FILE = os.path.join(DATA_DIR, "intro.json")            # group_chat_id -> intro_text
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")            # model_user_id -> {"items":[...], "last_sent": epoch}
FX_FILE = os.path.join(DATA_DIR, "fx.json")                  # model_user_id -> {"template": int}

TMP_DIR = os.path.join(DATA_DIR, "tmp")
os.makedirs(TMP_DIR, exist_ok=True)

_lock = threading.Lock()

# =========================
# Safe JSON helpers
# =========================
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
        fx = _read_json(FX_FILE, {})
    return rooms, models, live, streamers, intro, queue, fx

def save_all(rooms=None, models=None, live=None, streamers=None, intro=None, queue=None, fx=None):
    with _lock:
        if rooms is not None: _write_json(ROOMS_FILE, rooms)
        if models is not None: _write_json(MODELS_FILE, models)
        if live is not None: _write_json(LIVE_FILE, live)
        if streamers is not None: _write_json(STREAMERS_FILE, streamers)
        if intro is not None: _write_json(INTRO_FILE, intro)
        if queue is not None: _write_json(QUEUE_FILE, queue)
        if fx is not None: _write_json(FX_FILE, fx)

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
    # sugerente / sexy sin volverse excesivamente gráfico
    if lang == "de":
        return "🔥 Hey ihr… habt ihr Lust auf was ganz Privates? 😈"
    if lang == "pt":
        return "🔥 Oi… vocês tão a fim de algo bem privado? 😈"
    return "🔥 Hey… want something private? 😈"

def format_informal_hint_de(text: str) -> str:
    # Heurística suave: evita mezclas Sie/dich.
    t = text or ""
    t = t.replace("Möchten Sie", "Willst du")
    t = t.replace("Wollen Sie", "Willst du")
    t = t.replace("Sie ", "du ")
    t = t.replace("Ihnen", "dir")
    t = t.replace("Ihr ", "dein ")
    return t

def get_bound_model_for_group(group_chat_id: str, streamers: dict) -> str:
    return streamers.get(str(group_chat_id), "")

def get_group_for_model(model_user_id: str, rooms: dict) -> str:
    return rooms.get(str(model_user_id), "")

def is_live(model_user_id: str, live: dict) -> bool:
    return bool(live.get(str(model_user_id), False))

def ensure_model_name(models: dict, user_id: str, fallback: str):
    if user_id not in models or not models[user_id]:
        models[user_id] = fallback

# =========================
# Effects (Pillow + ffmpeg best-effort)
# =========================
def _pick_template_for_model(model_user_id: str, fx: dict) -> int:
    try:
        t = int((fx.get(model_user_id) or {}).get("template", 1))
        if 1 <= t <= 10:
            return t
    except Exception:
        pass
    return 1

def _parse_caption_fx(caption: str) -> Tuple[str, int]:
    """
    Permite que Aurora ponga:
      #fx3 texto...
    o  /fx3 texto...
    Devuelve (caption_sin_fx, template_id_o_0)
    """
    cap = (caption or "").strip()
    if not cap:
        return cap, 0
    low = cap.lower()
    if low.startswith("#fx") or low.startswith("/fx"):
        # Ej: #fx3 hola
        token = cap.split(maxsplit=1)[0]
        rest = cap[len(token):].strip()
        digits = "".join([c for c in token if c.isdigit()])
        if digits.isdigit():
            tid = int(digits)
            if 1 <= tid <= 10:
                return rest, tid
    return cap, 0

def apply_frame_to_photo(local_path: str, template_id: int) -> str:
    """
    Genera una nueva imagen con un marco llamativo.
    No depende de assets externos: todo se dibuja.
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    img = Image.open(local_path).convert("RGBA")
    w, h = img.size

    # Crear un canvas un poco más grande (borde)
    pad = int(min(w, h) * 0.06)
    out_w, out_h = w + pad * 2, h + pad * 2
    canvas = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 255))

    # Pegar foto centrada
    canvas.paste(img, (pad, pad))

    # Overlay transparente
    overlay = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # Estilos (10)
    # Cambiamos texto/emoji y detalles de borde
    titles = {
        1: ("🔥 PREMIERE", "JETZT"),
        2: ("💋 HEUTE NACHT", "LIVE"),
        3: ("✨ EXKLUSIV", "NUR FÜR EUCH"),
        4: ("🔥 HOT DROP", "NEU"),
        5: ("💎 VIP", "PRIVATE"),
        6: ("🌙 NACHTSHOW", "JETZT"),
        7: ("⚡ SPECIAL", "LIVE"),
        8: ("🔥 BRANDNEU", "PREMIERE"),
        9: ("💋 HEISS", "JETZT LIVE"),
        10: ("✨ SHOWTIME", "NEU"),
    }
    t1, t2 = titles.get(template_id, titles[1])

    # Borde neon
    border = pad
    # rectángulo externo
    d.rectangle([2, 2, out_w - 3, out_h - 3], outline=(255, 80, 140, 200), width=max(4, pad // 4))
    # rectángulo interno
    d.rectangle([border // 2, border // 2, out_w - border // 2 - 1, out_h - border // 2 - 1],
                outline=(255, 230, 250, 160), width=max(2, pad // 6))

    # Cinta superior e inferior
    top_h = max(48, pad)
    bot_h = max(44, pad)
    d.rectangle([0, 0, out_w, top_h], fill=(0, 0, 0, 140))
    d.rectangle([0, out_h - bot_h, out_w, out_h], fill=(0, 0, 0, 140))

    # Fuente (fallback)
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", max(28, pad // 2))
        font_small = ImageFont.truetype("DejaVuSans.ttf", max(20, pad // 3))
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # Efecto glow: dibujar texto varias veces borroso
    def glow_text(x, y, text, font, fill, glow_fill):
        # Glow layer
        glow = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.text((x, y), text, font=font, fill=glow_fill)
        glow = glow.filter(ImageFilter.GaussianBlur(radius=6))
        overlay.alpha_composite(glow)
        d.text((x, y), text, font=font, fill=fill)

    # Texto top
    glow_text(16, 10, t1, font_big, (255, 255, 255, 240), (255, 80, 140, 180))
    # Texto bottom
    glow_text(16, out_h - bot_h + 8, t2, font_small, (255, 255, 255, 220), (120, 220, 255, 160))

    # Componer
    out = Image.alpha_composite(canvas, overlay).convert("RGB")

    out_path = os.path.join(TMP_DIR, f"fx_photo_{int(time.time())}_{template_id}.jpg")
    out.save(out_path, quality=92)
    return out_path

def ffmpeg_overlay_video(local_path: str, template_id: int) -> str:
    """
    Best-effort: aplica un watermark simple con drawtext.
    Si no hay ffmpeg, fallará y devolvemos excepción arriba (capturada).
    """
    # textos por template
    labels = {
        1: "PREMIERE",
        2: "HEUTE NACHT",
        3: "EXKLUSIV",
        4: "HOT DROP",
        5: "VIP",
        6: "NACHTSHOW",
        7: "SPECIAL",
        8: "BRANDNEU",
        9: "HEISS",
        10: "SHOWTIME",
    }
    label = labels.get(template_id, "PREMIERE")

    out_path = os.path.join(TMP_DIR, f"fx_video_{int(time.time())}_{template_id}.mp4")

    # drawtext requiere fontfile o usará default. Intentamos DejaVu.
    fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not os.path.exists(fontfile):
        fontfile = ""

    draw = (
        f"drawtext=text='{label}':"
        f"x=20:y=20:fontsize=34:fontcolor=white@0.95:"
        f"box=1:boxcolor=black@0.35:boxborderw=10"
    )
    if fontfile:
        draw = draw.replace("drawtext=", f"drawtext=fontfile={fontfile}:")

    cmd = [
        "ffmpeg", "-y",
        "-i", local_path,
        "-vf", draw,
        "-c:a", "copy",
        out_path
    ]
    # Ejecutar
    subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=45)
    return out_path

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
    update.message.reply_text(f"👤 Tu user_id: {u.id}\nUsername: @{u.username}" if u.username else f"👤 Tu user_id: {u.id}")

def cmd_setmodel(update: Update, context: CallbackContext):
    # /setmodel Aurora  (privado)
    if not update.message:
        return
    if is_group_chat(update.effective_chat):
        update.message.reply_text("Usa /setmodel en privado con el bot.")
        return

    args = context.args
    name = " ".join(args).strip() if args else ""
    user = update.effective_user
    if not user:
        return

    if not name:
        update.message.reply_text("Uso: /setmodel <Nombre>\nEj: /setmodel Aurora")
        return

    rooms, models, live, streamers, intro, queue, fx = load_all()
    models[str(user.id)] = name
    save_all(models=models)
    update.message.reply_text(f"✅ Modelo registrada: {name}\nuser_id: {user.id}")

def cmd_bindchat(update: Update, context: CallbackContext):
    # /bindchat <model_user_id> en grupo
    if not update.message or not is_group_chat(update.effective_chat):
        return
    args = context.args
    if not args:
        update.message.reply_text("Uso: /bindchat <model_user_id>\nEj: /bindchat 123456789")
        return

    model_user_id = args[0].strip()
    group_chat_id = str(update.effective_chat.id)

    rooms, models, live, streamers, intro, queue, fx = load_all()
    rooms[model_user_id] = group_chat_id
    save_all(rooms=rooms)

    update.message.reply_text(f"✅ Grupo vinculado.\nmodel_user_id: {model_user_id}\nchat_id: {group_chat_id}")

def cmd_setstreamer(update: Update, context: CallbackContext):
    """
    En el GRUPO:
    Responde a un mensaje de Aurora y escribe /setstreamer
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

    rooms, models, live, streamers, intro, queue, fx = load_all()

    streamers[group_chat_id] = streamer_id
    rooms[streamer_id] = group_chat_id
    ensure_model_name(models, streamer_id, streamer_user.first_name or "Streamer")

    save_all(rooms=rooms, streamers=streamers, models=models)

    update.message.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {models.get(streamer_id, 'Streamer')}\n"
        f"user_id: {streamer_id}\n\n"
        "Prueba:\n"
        "- En el grupo escribe algo en alemán → se envía traducido al privado del streamer.\n"
        "- En privado, el streamer escribe algo → se publica traducido aquí."
    )

def cmd_teststreamer(update: Update, context: CallbackContext):
    """
    MODO TEST sin Aurora:
    En el grupo escribe /teststreamer y el bot te pone a TI como streamer temporal.
    """
    if not update.message or not is_group_chat(update.effective_chat):
        return
    u = update.effective_user
    if not u:
        return

    group_chat_id = str(update.effective_chat.id)
    streamer_id = str(u.id)

    rooms, models, live, streamers, intro, queue, fx = load_all()

    streamers[group_chat_id] = streamer_id
    rooms[streamer_id] = group_chat_id
    ensure_model_name(models, streamer_id, u.first_name or "TestStreamer")

    save_all(rooms=rooms, streamers=streamers, models=models)

    update.message.reply_text(
        "🧪 TEST MODE: Streamer = tú.\n"
        f"user_id: {streamer_id}\n"
        "Ahora haz /liveon en privado y prueba traducción sin Aurora."
    )

def cmd_liveon(update: Update, context: CallbackContext):
    # privado
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, fx = load_all()
    live[str(user.id)] = True
    save_all(live=live)
    update.message.reply_text("🟢 LIVE ON ✅\nTraducción + cola habilitadas.")

def cmd_liveoff(update: Update, context: CallbackContext):
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return
    rooms, models, live, streamers, intro, queue, fx = load_all()
    live[str(user.id)] = False
    save_all(live=live)
    update.message.reply_text("🔴 LIVE OFF ✅\nTraducción + cola detenidas.")

def cmd_intro(update: Update, context: CallbackContext):
    # /intro <texto> en grupo
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = " ".join(context.args).strip()
    if not text:
        update.message.reply_text("Uso: /intro <texto>\nEj: /intro Soy Aurora 🔥 23 🇧🇷 ...")
        return

    rooms, models, live, streamers, intro, queue, fx = load_all()
    group_chat_id = str(update.effective_chat.id)
    intro[group_chat_id] = text
    save_all(intro=intro)

    msg = update.message.reply_text(f"📌 Presentación:\n\n{text}")
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
        "✅ Eso lo ENCOLA.\n"
        "Si NO pones #queue → se publica inmediatamente.\n\n"
        "La cola se suelta cada 2 horas mientras LIVE esté ON.\n"
        "Tip: para efectos por post usa: #fx3 (1..10) al inicio del caption."
    )

def cmd_setfx(update: Update, context: CallbackContext):
    """
    En privado (streamer):
      /setfx 3
    Guarda template por defecto 1..10 para esa modelo.
    """
    if not update.message or is_group_chat(update.effective_chat):
        return
    u = update.effective_user
    if not u:
        return
    args = context.args
    if not args:
        update.message.reply_text("Uso: /setfx <1..10>\nEj: /setfx 3")
        return
    try:
        tid = int(args[0])
        if tid < 1 or tid > 10:
            raise ValueError()
    except Exception:
        update.message.reply_text("El template debe ser un número 1..10")
        return

    rooms, models, live, streamers, intro, queue, fx = load_all()
    fx[str(u.id)] = {"template": tid}
    save_all(fx=fx)
    update.message.reply_text(f"✅ Template por defecto guardado: {tid}")

# =========================
# Message Handlers
# =========================
def handle_group_text(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    rooms, models, live, streamers, intro, queue, fx = load_all()
    group_chat_id = str(update.effective_chat.id)
    model_user_id = get_bound_model_for_group(group_chat_id, streamers)
    if not model_user_id:
        return
    if not is_live(model_user_id, live):
        return

    translated = translate_text(text, GROUP_LANGUAGE, MODEL_LANGUAGE)
    try:
        context.bot.send_message(chat_id=int(model_user_id), text=f"💬 (del grupo) {translated}")
    except Exception:
        pass

def handle_private_text(update: Update, context: CallbackContext):
    # Privado del streamer -> grupo
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, fx = load_all()
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

    try:
        context.bot.send_message(chat_id=int(group_chat_id), text=translated)
    except Exception:
        pass

def enqueue_media(model_user_id: str, item: dict):
    rooms, models, live, streamers, intro, queue, fx = load_all()
    q = queue.get(model_user_id) or {"items": [], "last_sent": 0}
    q["items"].append(item)
    queue[model_user_id] = q
    save_all(queue=queue)

def _download_file(context: CallbackContext, file_id: str, ext: str) -> str:
    f = context.bot.get_file(file_id)
    local_path = os.path.join(TMP_DIR, f"in_{int(time.time())}_{file_id}.{ext}")
    f.download(custom_path=local_path)
    return local_path

def handle_private_media(update: Update, context: CallbackContext):
    """
    En privado:
    - caption empieza con #queue o /queue => encola (cada 2h)
    - si no => publica inmediato
    - efectos: por post #fx3 ... o /fx3 ...   (1..10)
      o template por defecto con /setfx
    """
    if not update.message or is_group_chat(update.effective_chat):
        return
    user = update.effective_user
    if not user:
        return

    rooms, models, live, streamers, intro, queue, fx = load_all()
    model_user_id = str(user.id)

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
        parts = caption.split(maxsplit=1)
        clean_caption = parts[1].strip() if len(parts) > 1 else ""

    # FX per post
    clean_caption, fx_tid = _parse_caption_fx(clean_caption)

    # caption vacío -> fallback
    if not clean_caption:
        clean_caption = sexy_fallback_line(GROUP_LANGUAGE)

    # traducir caption al alemán
    translated_caption = translate_text(clean_caption, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE == "de":
        translated_caption = format_informal_hint_de(translated_caption)

    # Determinar template final
    template_id = fx_tid if (1 <= fx_tid <= 10) else _pick_template_for_model(model_user_id, fx)

    item = {"type": None, "file_id": None, "caption": translated_caption, "template": template_id, "effects": ENABLE_EFFECTS}

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
        update.message.reply_text("✅ Guardado en cola. Se publicará según el intervalo mientras LIVE esté ON.")
        return

    # Publicación inmediata
    _send_media_item(context, int(group_chat_id), item)

def _send_media_item(context: CallbackContext, group_chat_id: int, item: Dict[str, Any]):
    """
    Envía item al grupo.
    Si efectos habilitados:
      - foto: genera frame con Pillow y envía la imagen resultante
      - video: intenta overlay con ffmpeg (si falla, envía original)
    """
    media_type = item.get("type")
    file_id = item.get("file_id")
    caption = item.get("caption", "")
    template_id = int(item.get("template") or 1)
    use_fx = bool(item.get("effects", False)) and ENABLE_EFFECTS

    if not use_fx:
        try:
            if media_type == "photo":
                context.bot.send_photo(chat_id=group_chat_id, photo=file_id, caption=caption)
            else:
                context.bot.send_video(chat_id=group_chat_id, video=file_id, caption=caption)
        except Exception:
            pass
        return

    # FX ON
    if media_type == "photo":
        local_in = None
        local_out = None
        try:
            local_in = _download_file(context, file_id, "jpg")
            local_out = apply_frame_to_photo(local_in, template_id)
            with open(local_out, "rb") as f:
                context.bot.send_photo(chat_id=group_chat_id, photo=f, caption=caption)
        except Exception:
            # fallback original
            try:
                context.bot.send_photo(chat_id=group_chat_id, photo=file_id, caption=caption)
            except Exception:
                pass
        finally:
            for p in [local_in, local_out]:
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass

    elif media_type == "video":
        local_in = None
        local_out = None
        try:
            local_in = _download_file(context, file_id, "mp4")
            local_out = ffmpeg_overlay_video(local_in, template_id)
            with open(local_out, "rb") as f:
                context.bot.send_video(chat_id=group_chat_id, video=f, caption=caption)
        except Exception:
            # fallback original
            try:
                context.bot.send_video(chat_id=group_chat_id, video=file_id, caption=caption)
            except Exception:
                pass
        finally:
            for p in [local_in, local_out]:
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass

def handle_new_members(update: Update, context: CallbackContext):
    if not update.message or not is_group_chat(update.effective_chat):
        return
    if not WELCOME_ON_JOIN:
        return

    rooms, models, live, streamers, intro, queue, fx = load_all()
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
    - Publica 1 item al grupo
    """
    while True:
        try:
            rooms, models, live, streamers, intro, queue, fx = load_all()
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

                # enviar
                try:
                    # Para usar la misma lógica de efectos, necesitamos un "context" pero aquí no hay.
                    # Enviamos versión simple con file_id (sin descargar) para no romper.
                    # Si quieres FX también en cola, dímelo y lo hacemos con un BotContext ligero.
                    if item.get("type") == "photo":
                        bot.send_photo(chat_id=int(group_chat_id), photo=item.get("file_id"), caption=item.get("caption", ""))
                    else:
                        bot.send_video(chat_id=int(group_chat_id), video=item.get("file_id"), caption=item.get("caption", ""))
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
    dp.add_handler(CommandHandler("teststreamer", cmd_teststreamer))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))
    dp.add_handler(CommandHandler("intro", cmd_intro))
    dp.add_handler(CommandHandler("queue", cmd_queue))
    dp.add_handler(CommandHandler("setfx", cmd_setfx))

    # ✅ PTB v13 filters correctos (NO usar Filters.chat_type.groups)
    group_filter = (Filters.group | Filters.supergroup)

    # Group text -> model private
    dp.add_handler(MessageHandler(group_filter & Filters.text & ~Filters.command, handle_group_text))

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
