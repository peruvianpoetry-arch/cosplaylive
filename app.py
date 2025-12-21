# app.py (PTB v13.15) – Stickers animados + traducción + cola 2h + pins
import os, json, time, threading, random
from flask import Flask
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# ========= CONFIG =========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
GROUP_LANGUAGE = os.getenv("GROUP_LANGUAGE", "de")
MODEL_LANGUAGE = os.getenv("MODEL_LANGUAGE", "pt")
PROMO_INTERVAL_SECONDS = int(os.getenv("PROMO_INTERVAL_SECONDS", "7200"))
WELCOME_ON_JOIN = os.getenv("WELCOME_ON_JOIN", "1") == "1"

os.makedirs(DATA_DIR, exist_ok=True)
ROOMS = os.path.join(DATA_DIR, "rooms.json")
MODELS = os.path.join(DATA_DIR, "models.json")
LIVE = os.path.join(DATA_DIR, "live.json")
STREAMERS = os.path.join(DATA_DIR, "streamers.json")
INTRO = os.path.join(DATA_DIR, "intro.json")
QUEUE = os.path.join(DATA_DIR, "queue.json")
PINS = os.path.join(DATA_DIR, "pins.json")

def rj(p,d): 
    try: return json.load(open(p,"r",encoding="utf-8"))
    except: return d
def wj(p,d):
    json.dump(d, open(p,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

def translate(t, src, dst):
    if not t.strip() or src==dst: return t
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source=src, target=dst).translate(t)
    except: return t

def informal_de(t):
    return t.replace("Sie ","du ").replace("Ihnen","dir").replace("Ihr","dein")

# ===== STICKERS (PEGA TUS file_id) =====
STICKERS_LIVE = [
    "STICKER_FILE_ID_LIVE_NOW",
    "STICKER_FILE_ID_FIRE",
    "STICKER_FILE_ID_HEART_FIRE",
]
STICKERS_NEW = [
    "STICKER_FILE_ID_NEW",
    "STICKER_FILE_ID_CLAPPER",
    "STICKER_FILE_ID_DIAMOND",
]
STICKERS_SPICE = [
    "STICKER_FILE_ID_TONGUE_ICE",
    "STICKER_FILE_ID_LIPS_BITE",
    "STICKER_FILE_ID_CHERRIES",
    "STICKER_FILE_ID_WAVES",
]

PHRASES_DE = [
    "Na… spürst du die Hitze? 🔥",
    "Bleib hier… es wird besser 😈",
    "Heute wird’s gefährlich heiß 🌡️",
    "Ich hab Lust auf mehr… und du? 💋",
    "Nur ein kleiner Vorgeschmack… 🍒",
    "LIVE jetzt… komm näher 🔴",
    "Das ist erst der Anfang…",
    "Dein Blick verrät alles 👀",
    "Neu. Heiß. Verführerisch. 🆕",
    "Mach es dir bequem… 🔥",
]*6

def pick_sticker(live_on):
    base = STICKERS_LIVE if live_on else STICKERS_NEW
    return random.choice(base + STICKERS_SPICE)

# ===== COMMANDS =====
def start(u:Update,c:CallbackContext): u.message.reply_text("✅ Bot listo")
def whoami(u,c): u.message.reply_text(f"id={u.effective_user.id}")

def setmodel(u,c):
    name=" ".join(c.args).strip()
    if not name: return u.message.reply_text("Uso: /setmodel Aurora")
    m=rj(MODELS,{}); m[str(u.effective_user.id)]=name; wj(MODELS,m)
    u.message.reply_text(f"Modelo: {name}")

def bindchat(u,c):
    if u.effective_chat.type not in ("group","supergroup"): return
    mid=c.args[0]; rooms=rj(ROOMS,{}); rooms[mid]=str(u.effective_chat.id); wj(ROOMS,rooms)
    u.message.reply_text("Grupo vinculado")

def setstreamer(u,c):
    if u.effective_chat.type not in ("group","supergroup"): return
    r=u.message.reply_to_message
    if not r: return u.message.reply_text("Responde a un mensaje del streamer y usa /setstreamer")
    s=rj(STREAMERS,{}); s[str(u.effective_chat.id)]=str(r.from_user.id); wj(STREAMERS,s)
    rooms=rj(ROOMS,{}); rooms[str(r.from_user.id)]=str(u.effective_chat.id); wj(ROOMS,rooms)
    m=rj(MODELS,{}); m.setdefault(str(r.from_user.id), r.from_user.first_name); wj(MODELS,m)
    u.message.reply_text("Streamer asignado")

def liveon(u,c):
    l=rj(LIVE,{}); l[str(u.effective_user.id)]=True; wj(LIVE,l)
    u.message.reply_text("LIVE ON")

def liveoff(u,c):
    l=rj(LIVE,{}); l[str(u.effective_user.id)]=False; wj(LIVE,l)
    u.message.reply_text("LIVE OFF")

def intro(u,c):
    if u.effective_chat.type not in ("group","supergroup"): return
    text=" ".join(c.args)
    i=rj(INTRO,{}); i[str(u.effective_chat.id)]=text; wj(INTRO,i)
    msg=u.message.reply_text(text)
    try: c.bot.pin_chat_message(u.effective_chat.id, msg.message_id, disable_notification=True)
    except: pass

# ===== HANDLERS =====
def group_text(u,c):
    rooms=rj(ROOMS,{}); stream=rj(STREAMERS,{})
    gid=str(u.effective_chat.id); mid=stream.get(gid)
    if not mid or not rj(LIVE,{}).get(mid): return
    t=translate(u.message.text, GROUP_LANGUAGE, MODEL_LANGUAGE)
    c.bot.send_message(chat_id=int(mid), text=f"💬 {t}")

def private_text(u,c):
    mid=str(u.effective_user.id)
    if not rj(LIVE,{}).get(mid): return
    gid=rj(ROOMS,{}).get(mid)
    if not gid: return
    t=translate(u.message.text, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE=="de": t=informal_de(t)
    sticker=pick_sticker(True)
    c.bot.send_sticker(chat_id=int(gid), sticker=sticker)
    msg=c.bot.send_message(chat_id=int(gid), text=t)
    try:
        c.bot.pin_chat_message(int(gid), msg.message_id, disable_notification=True)
    except: pass

def private_media(u,c):
    mid=str(u.effective_user.id)
    if not rj(LIVE,{}).get(mid): return
    gid=rj(ROOMS,{}).get(mid)
    if not gid: return
    cap=u.message.caption or ""
    q = cap.lower().startswith("#queue") or cap.lower().startswith("/queue")
    clean=cap.split(maxsplit=1)[1] if q and len(cap.split())>1 else cap
    if not clean: clean=random.choice(PHRASES_DE)
    t=translate(clean, MODEL_LANGUAGE, GROUP_LANGUAGE)
    if GROUP_LANGUAGE=="de": t=informal_de(t)
    sticker=pick_sticker(True)
    c.bot.send_sticker(chat_id=int(gid), sticker=sticker)
    if u.message.photo:
        msg=c.bot.send_photo(chat_id=int(gid), photo=u.message.photo[-1].file_id, caption=t)
    else:
        msg=c.bot.send_video(chat_id=int(gid), video=u.message.video.file_id, caption=t)
    try:
        c.bot.pin_chat_message(int(gid), msg.message_id, disable_notification=True)
    except: pass

def on_join(u,c):
    if not WELCOME_ON_JOIN: return
    txt=rj(INTRO,{}).get(str(u.effective_chat.id))
    if txt: c.bot.send_message(chat_id=u.effective_chat.id, text=txt)

# ===== MAIN =====
def main():
    up=Updater(TOKEN, use_context=True)
    dp=up.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("whoami", whoami))
    dp.add_handler(CommandHandler("setmodel", setmodel))
    dp.add_handler(CommandHandler("bindchat", bindchat))
    dp.add_handler(CommandHandler("setstreamer", setstreamer))
    dp.add_handler(CommandHandler("liveon", liveon))
    dp.add_handler(CommandHandler("liveoff", liveoff))
    dp.add_handler(CommandHandler("intro", intro))
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, group_text))
    dp.add_handler(MessageHandler(Filters.private & Filters.text & ~Filters.command, private_text))
    dp.add_handler(MessageHandler(Filters.private & (Filters.photo|Filters.video), private_media))
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, on_join))
    threading.Thread(target=lambda: Flask(__name__).run(host="0.0.0.0", port=int(os.getenv("PORT","10000"))), daemon=True).start()
    up.start_polling(drop_pending_updates=True)
    up.idle()

if __name__=="__main__":
    main()
