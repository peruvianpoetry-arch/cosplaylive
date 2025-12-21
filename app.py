# app.py
# CosplayLive Bot – PTB 13.15
# Traducción + Streamer + LIVE + Cola + Animaciones (ffmpeg)

import os, json, time, threading, subprocess, tempfile
from flask import Flask
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from PIL import Image, ImageDraw, ImageFont

# ================= CONFIG =================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
GROUP_LANGUAGE = "de"
MODEL_LANGUAGE = "pt"
PROMO_INTERVAL = 2 * 60 * 60  # 2 horas

os.makedirs(DATA_DIR, exist_ok=True)

FILES = {
    "rooms": f"{DATA_DIR}/rooms.json",
    "live": f"{DATA_DIR}/live.json",
    "streamers": f"{DATA_DIR}/streamers.json",
    "queue": f"{DATA_DIR}/queue.json",
}

def load(name):
    try:
        with open(FILES[name], "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save(name, data):
    with open(FILES[name], "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ================= UTIL =================
def sexy_fallback():
    return "🔥 Exklusiv für euch… Lust auf mehr? 😈"

def translate(txt, src, dst):
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source=src, target=dst).translate(txt)
    except:
        return txt

def informal_de(t):
    return (
        t.replace("Sie ", "du ")
         .replace("Möchten Sie", "Willst du")
         .replace("Ihnen", "dir")
    )

# ================= ANIMACIÓN =================
def create_animated_video(image_path, caption):
    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    img = Image.open(image_path).convert("RGB")

    draw = ImageDraw.Draw(img)
    w, h = img.size
    text = "🔥 EXKLUSIV 🔥"
    draw.rectangle((0, 0, w, 80), fill=(0, 0, 0))
    draw.text((w//2 - 120, 20), text, fill="red")

    frame_path = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    img.save(frame_path)

    subprocess.run([
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", frame_path,
        "-t", "5",
        "-vf", "scale=720:-2",
        "-pix_fmt", "yuv420p",
        tmp_video
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return tmp_video

# ================= COMMANDS =================
def start(update, ctx):
    update.message.reply_text("✅ Bot activo")

def teststreamer(update, ctx):
    if not update.message.reply_to_message:
        update.message.reply_text("Responde a un mensaje tuyo con /teststreamer")
        return
    uid = str(update.message.reply_to_message.from_user.id)
    gid = str(update.effective_chat.id)
    rooms = load("rooms")
    streamers = load("streamers")
    rooms[uid] = gid
    streamers[gid] = uid
    save("rooms", rooms)
    save("streamers", streamers)
    update.message.reply_text("✅ Test streamer asignado")

def liveon(update, ctx):
    live = load("live")
    live[str(update.effective_user.id)] = True
    save("live", live)
    update.message.reply_text("🟢 LIVE ON")

def liveoff(update, ctx):
    live = load("live")
    live[str(update.effective_user.id)] = False
    save("live", live)
    update.message.reply_text("🔴 LIVE OFF")

# ================= MEDIA HANDLER =================
def handle_private_media(update, ctx):
    uid = str(update.effective_user.id)
    live = load("live")
    if not live.get(uid):
        return

    rooms = load("rooms")
    gid = rooms.get(uid)
    if not gid:
        return

    caption = update.message.caption or ""
    caption = translate(caption or sexy_fallback(), MODEL_LANGUAGE, GROUP_LANGUAGE)
    caption = informal_de(caption)

    if update.message.photo:
        file = update.message.photo[-1].get_file()
    elif update.message.video:
        file = update.message.video.get_file()
    else:
        return

    tmp_img = tempfile.NamedTemporaryFile(delete=False).name
    file.download(tmp_img)

    video = create_animated_video(tmp_img, caption)
    ctx.bot.send_video(chat_id=int(gid), video=open(video, "rb"), caption=caption)

# ================= GROUP TEXT =================
def handle_group_text(update, ctx):
    txt = update.message.text
    streamers = load("streamers")
    gid = str(update.effective_chat.id)
    uid = streamers.get(gid)
    if not uid:
        return
    live = load("live")
    if not live.get(uid):
        return
    msg = translate(txt, GROUP_LANGUAGE, MODEL_LANGUAGE)
    ctx.bot.send_message(chat_id=int(uid), text=msg)

# ================= MAIN =================
def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("teststreamer", teststreamer))
    dp.add_handler(CommandHandler("liveon", liveon))
    dp.add_handler(CommandHandler("liveoff", liveoff))

    dp.add_handler(MessageHandler(Filters.private & (Filters.photo | Filters.video), handle_private_media))
    dp.add_handler(MessageHandler(Filters.group & Filters.text & ~Filters.command, handle_group_text))

    updater.start_polling(drop_pending_updates=True)
    updater.idle()

# Flask keepalive
app = Flask(__name__)
@app.route("/")
def home(): return "OK"

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000))), daemon=True).start()

if __name__ == "__main__":
    main()
