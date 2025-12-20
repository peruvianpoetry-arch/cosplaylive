# cosplaylive_bot.py
import os
import json
import time
import random
import threading
from typing import Dict, List, Optional
from flask import Flask
from telegram import Update, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)
from deep_translator import GoogleTranslator

# ───────────────── CONFIG ─────────────────

TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID"))  # TU user_id
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

GROUP_FILE = f"{DATA_DIR}/group.json"
STREAMER_FILE = f"{DATA_DIR}/streamer.json"
QUEUE_FILE = f"{DATA_DIR}/queue.json"
LIVE_FILE = f"{DATA_DIR}/live.json"

# Traducción
pt_to_de = GoogleTranslator(source="pt", target="de")
de_to_pt = GoogleTranslator(source="de", target="pt")

# ───────────────── PLANTILLAS ─────────────────

TEMPLATES = [
    "🔥 Heute wird’s heiß… nur für euch 😈",
    "💋 Ich bin gerade ganz bei euch…",
    "🔥 Neue Aufnahme… schaut genau hin",
    "💎 Exklusiv für meine Süßen",
    "😈 Ich hoffe, ihr mögt das…",
    "🔥 Für euch gemacht…",
    "💋 Heute ganz nah bei euch",
    "🔥 Spürt ihr die Vibes?",
    "😈 Nur ein kleiner Vorgeschmack",
    "💎 Mehr kommt bald…"
]

INTRO_TEXT = (
    "💋 <b>Wer ist Aurora?</b>\n\n"
    "Ich bin Aurora, leidenschaftlich, verspielt und voller Fantasie.\n"
    "Ich liebe Nähe, ehrliche Vibes und heiße Gespräche.\n\n"
    "Bleibt hier, schreibt mit mir und lernt mich kennen… 😈🔥"
)

# ───────────────── UTILIDADES ─────────────────

def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def casual_de(text: str) -> str:
    return (
        text.replace("Sie", "ihr")
            .replace("Ihnen", "euch")
            .replace("Ihr", "euer")
    )

# ───────────────── ESTADO ─────────────────

def set_group(chat_id):
    save(GROUP_FILE, {"id": chat_id})

def get_group():
    d = load(GROUP_FILE, {})
    return d.get("id")

def set_streamer(user_id):
    save(STREAMER_FILE, {"id": user_id})

def get_streamer():
    d = load(STREAMER_FILE, {})
    return d.get("id")

def set_live(on: bool):
    save(LIVE_FILE, {"on": on})

def is_live():
    return load(LIVE_FILE, {}).get("on", False)

def load_queue():
    return load(QUEUE_FILE, [])

def save_queue(q):
    save(QUEUE_FILE, q)

# ───────────────── COMANDOS OWNER ─────────────────

def cmd_setgroup(update: Update, ctx: CallbackContext):
    if update.effective_user.id != OWNER_ID:
        return
    set_group(update.effective_chat.id)
    update.message.reply_text("✅ Grupo configurado")

def cmd_setstreamer(update: Update, ctx: CallbackContext):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        update.message.reply_text("Uso: /setstreamer @username")
        return
    username = ctx.args[0].replace("@", "")
    try:
        user = ctx.bot.get_chat_member(update.effective_chat.id, username).user
        set_streamer(user.id)
        update.message.reply_text(f"✅ Streamer configurada: {user.first_name}")
    except:
        update.message.reply_text("❌ No pude encontrar a la usuaria")

def cmd_pin_intro(update: Update, ctx: CallbackContext):
    if update.effective_user.id != OWNER_ID:
        return
    gid = get_group()
    msg = ctx.bot.send_message(gid, INTRO_TEXT, parse_mode=ParseMode.HTML)
    ctx.bot.pin_chat_message(gid, msg.message_id, disable_notification=True)

# ───────────────── STREAMER ─────────────────

def cmd_liveon(update: Update, ctx: CallbackContext):
    if update.effective_user.id != get_streamer():
        return
    set_live(True)
    update.message.reply_text("🔥 LIVE ON")

def cmd_liveoff(update: Update, ctx: CallbackContext):
    if update.effective_user.id != get_streamer():
        return
    set_live(False)
    update.message.reply_text("⛔ LIVE OFF")

def cmd_queue(update: Update, ctx: CallbackContext):
    if update.effective_user.id != get_streamer():
        return
    if not update.message.reply_to_message:
        update.message.reply_text("Responde a una foto o video")
        return
    q = load_queue()
    q.append({
        "msg_id": update.message.reply_to_message.message_id,
        "type": "media"
    })
    save_queue(q)
    update.message.reply_text("⏳ Añadido a la cola (120 min)")

# ───────────────── MENSAJES ─────────────────

def handle_group(update: Update, ctx: CallbackContext):
    if not is_live():
        return
    streamer = get_streamer()
    if not streamer:
        return
    text = update.message.text
    pt = de_to_pt.translate(text)
    ctx.bot.send_message(
        streamer,
        f"💬 {update.effective_user.first_name}\n🇩🇪 {text}\n🇵🇹 {pt}"
    )

def handle_private(update: Update, ctx: CallbackContext):
    if update.effective_user.id != get_streamer():
        return
    if not is_live():
        return
    de = casual_de(pt_to_de.translate(update.message.text))
    gid = get_group()
    ctx.bot.send_message(
        gid,
        f"🎙️ <b>Aurora</b>: {de}",
        parse_mode=ParseMode.HTML
    )

# ───────────────── COLA AUTOMÁTICA ─────────────────

def queue_worker(bot):
    while True:
        time.sleep(120 * 60)
        q = load_queue()
        if not q:
            continue
        item = q.pop(0)
        save_queue(q)
        gid = get_group()
        bot.forward_message(
            chat_id=gid,
            from_chat_id=gid,
            message_id=item["msg_id"]
        )
        bot.send_message(
            gid,
            random.choice(TEMPLATES)
        )

# ───────────────── MAIN ─────────────────

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("setgroup", cmd_setgroup))
    dp.add_handler(CommandHandler("setstreamer", cmd_setstreamer))
    dp.add_handler(CommandHandler("pin_intro", cmd_pin_intro))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))
    dp.add_handler(CommandHandler("queue", cmd_queue))

    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text, handle_group))
    dp.add_handler(MessageHandler(Filters.chat_type.private & Filters.text, handle_private))

    threading.Thread(target=queue_worker, args=(updater.bot,), daemon=True).start()
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
