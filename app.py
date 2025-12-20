import os
import json
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional

from flask import Flask

from telegram import Update, Message
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Optional translator (deep-translator)
try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None


# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en Environment Variables")

DATA_DIR = os.getenv("DATA_DIR", "/var/data")
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

GROUP_LANGUAGE = (os.getenv("GROUP_LANGUAGE", "de") or "de").lower()  # group default: German
MODEL_LANGUAGE = (os.getenv("MODEL_LANGUAGE", "pt") or "pt").lower()  # model default: Portuguese
BOT_NAME = os.getenv("BOT_NAME", "Aurora 🔥 Live")

STATE_FILE = os.path.join(DATA_DIR, "state.json")

# State format:
# {
#   "groups": {
#       "<group_chat_id>": {
#           "streamer_user_id": 123,
#           "streamer_name": "Aurora",
#           "live": true
#       }
#   }
# }
DEFAULT_STATE = {"groups": {}}


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


STATE = load_state()


def get_group_cfg(chat_id: int) -> Dict[str, Any]:
    gid = str(chat_id)
    if "groups" not in STATE:
        STATE["groups"] = {}
    if gid not in STATE["groups"]:
        STATE["groups"][gid] = {
            "streamer_user_id": None,
            "streamer_name": None,
            "live": False,
        }
        save_state(STATE)
    return STATE["groups"][gid]


def is_admin_user(user_id: int) -> bool:
    # Si quieres, puedes fijar un ADMIN_USER_ID en env para bloquear comandos.
    admin_env = os.getenv("ADMIN_USER_ID", "").strip()
    if not admin_env:
        return True  # si no hay ADMIN_USER_ID, no bloqueamos (modo simple)
    try:
        return int(admin_env) == int(user_id)
    except Exception:
        return False


# -----------------------------
# TRANSLATION + STYLE
# -----------------------------
def translate_text(text: str, source: str, target: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if GoogleTranslator is None:
        # Sin deep-translator instalado, devolvemos original
        return text
    try:
        return GoogleTranslator(source=source, target=target).translate(text)
    except Exception:
        return text


def spice_german(text: str) -> str:
    """Make German informal + sexy-ish and remove formal Sie."""
    t = (text or "").strip()
    if not t:
        return ""

    # Quitar formalidades típicas y unificar informal
    replacements = [
        ("Sie ", "du "),
        ("Sie?", "du?"),
        ("Sie!", "du!"),
        ("Ihnen", "dir"),
        ("Ihr ", "dein "),
        ("Ihre ", "deine "),
        ("Ihrem", "deinem"),
        ("Ihren", "deinen"),
        ("Ihres", "deines"),
        ("Sehr geehrte", "Hey"),
        ("Guten Tag", "Hey"),
        ("Hallo", "Hey du 😘"),
        ("Möchten Sie", "Hast du Lust"),
        ("Wollen Sie", "Hast du Lust"),
        ("Möchtest du", "Hast du Lust"),
        ("Ich bin bereit", "Ich bin ganz bereit für dich"),
        ("Ich warte auf dich", "Ich warte heiß auf dich"),
        ("Ich freue mich", "Ich freue mich richtig auf dich"),
        ("mein Lieber", "mein Süßer"),
        ("meine Liebe", "meine Schöne"),
    ]

    for a, b in replacements:
        t = t.replace(a, b)

    # Ajustes suaves para hacerlo más “streamer real”
    # Evitar mezclas raras Sie/dich: si aparece "Sie" lo bajamos
    t = t.replace(" Sie", " du")
    t = t.replace(" Ihnen", " dir")

    # Pequeños toques sin ser porno explícito
    # (puedes editar estas líneas si quieres aún más caliente)
    if "Lust" in t and "😈" not in t:
        t = "🔥 " + t
    return t


def force_plural_group(text: str) -> str:
    """Force plural (ihr/euch) for group style. Keeps it consistent."""
    t = (text or "").strip()
    if not t:
        return ""

    plural_replacements = [
        ("du ", "ihr "),
        (" dich", " euch"),
        (" dir", " euch"),
        (" dein ", " euer "),
        (" deine ", " eure "),
        (" deinem ", " eurem "),
        (" deinen ", " euren "),
        ("deinem", "eurem"),
        ("deinen", "euren"),
        ("deine", "eure"),
        ("dein", "euer"),
        ("mein Süßer", "meine Süßen"),
        ("meine Schöne", "meine Süßen"),
        ("mein Schatz", "meine Schätze"),
    ]

    for a, b in plural_replacements:
        t = t.replace(a, b)

    # Si queda "ihr" y luego "dir", arreglar
    t = t.replace(" dir", " euch")
    t = t.replace(" dich", " euch")
    return t


def hot_caption_de_plural() -> str:
    # Caption default (alemán informal, plural, sexy)
    options = [
        "🔥 Hey meine Süßen 😘 Ich bin jetzt live… wer von euch hat Lust auf mehr?",
        "😈 Na ihr… seid ihr bereit? Kommt näher…",
        "🔥 Ich vermisse euch… schreibt mir, was ihr gerade wollt 😘",
        "💋 Hey ihr Schätze… ich bin heiß drauf, euch heute zu verwöhnen…",
        "🔥 Ihr wollt mich sehen? Dann macht euch bereit 😘",
    ]
    return options[int(time.time()) % len(options)]


def format_as_aurora(text: str, aurora_name: str) -> str:
    # Para que se vea que es “Aurora” hablando, aunque el mensaje lo envíe el bot
    # (Telegram no permite que el bot “se haga pasar” por su cuenta)
    return f"🔥 {aurora_name}:\n{text}".strip()


# -----------------------------
# COMMANDS
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"✅ {BOT_NAME} listo.\n\n"
        "Comandos:\n"
        "- /whoami (ver tu user_id)\n"
        "- En el grupo: responde a Aurora y escribe /setstreamer\n"
        "- Aurora (privado): /liveon y /liveoff\n"
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"👤 Tu user_id: {u.id}\n"
        f"Nombre: {u.full_name}\n"
        f"Username: @{u.username}" if u.username else f"Username: (sin @)"
    )


async def cmd_setstreamer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("❌ Usa /setstreamer dentro del grupo y respondiendo a un mensaje de Aurora.")
        return

    if not is_admin_user(update.effective_user.id):
        await msg.reply_text("❌ No autorizado.")
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("❌ Responde al mensaje de Aurora y luego escribe /setstreamer.")
        return

    target_user = msg.reply_to_message.from_user
    cfg = get_group_cfg(msg.chat_id)
    cfg["streamer_user_id"] = target_user.id
    cfg["streamer_name"] = target_user.first_name or "Aurora"
    cfg["live"] = False  # empieza apagado

    save_state(STATE)

    await msg.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {cfg['streamer_name']}\n"
        f"user_id: {cfg['streamer_user_id']}\n\n"
        "Prueba ahora:\n"
        "- Aurora abre el bot en privado y escribe /start\n"
        "- Luego Aurora usa /liveon para activar traducción sexy\n"
    )


async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != ChatType.PRIVATE:
        await msg.reply_text("❌ /liveon se usa en privado.")
        return

    user_id = update.effective_user.id

    # Buscar qué grupo tiene a este usuario como streamer
    found = False
    for gid, cfg in STATE.get("groups", {}).items():
        if cfg.get("streamer_user_id") == user_id:
            cfg["live"] = True
            save_state(STATE)
            found = True
            await msg.reply_text(
                "✅ LIVE ON.\n"
                "Ahora:\n"
                "- Lo que tú escribas (PT) → se publica en el grupo (DE) sexy + informal + plural.\n"
                "- Lo que escriban en el grupo (DE) → te llega (PT) en privado.\n"
            )
    if not found:
        await msg.reply_text("❌ No estás registrada como streamer en ningún grupo. (Usa /setstreamer en el grupo).")


async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != ChatType.PRIVATE:
        await msg.reply_text("❌ /liveoff se usa en privado.")
        return

    user_id = update.effective_user.id
    found = False
    for gid, cfg in STATE.get("groups", {}).items():
        if cfg.get("streamer_user_id") == user_id:
            cfg["live"] = False
            save_state(STATE)
            found = True
            await msg.reply_text("⛔ LIVE OFF. Traducción apagada.")
    if not found:
        await msg.reply_text("❌ No estás registrada como streamer en ningún grupo.")


# -----------------------------
# MESSAGE ROUTING
# -----------------------------
def get_bound_group_for_streamer(streamer_user_id: int) -> Optional[int]:
    for gid, cfg in STATE.get("groups", {}).items():
        if cfg.get("streamer_user_id") == streamer_user_id:
            return int(gid)
    return None


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    cfg = get_group_cfg(msg.chat_id)
    if not cfg.get("live"):
        return  # SOLO cuando LIVE ON

    streamer_id = cfg.get("streamer_user_id")
    if not streamer_id:
        return

    # Ignorar mensajes del bot
    if msg.from_user and msg.from_user.is_bot:
        return

    # Solo texto (por ahora); si mandan stickers/fotos, puedes extender luego
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    sender = msg.from_user.full_name if msg.from_user else "Alguien"
    # DE -> PT
    pt = translate_text(text, source=GROUP_LANGUAGE, target=MODEL_LANGUAGE)
    await context.bot.send_message(
        chat_id=streamer_id,
        text=f"📩 {sender} (grupo):\n{pt}",
    )


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != ChatType.PRIVATE:
        return

    user_id = update.effective_user.id

    # ¿Es streamer de algún grupo?
    group_id = get_bound_group_for_streamer(user_id)
    if not group_id:
        return

    cfg = get_group_cfg(group_id)
    if not cfg.get("live"):
        return  # SOLO cuando LIVE ON

    aurora_name = cfg.get("streamer_name") or "Aurora"

    # Si es texto normal:
    if msg.text:
        pt_text = msg.text.strip()
        de = translate_text(pt_text, source=MODEL_LANGUAGE, target=GROUP_LANGUAGE)
        de = spice_german(de)
        de = force_plural_group(de)
        out = format_as_aurora(de, aurora_name)
        await context.bot.send_message(chat_id=group_id, text=out)
        return

    # Si es foto/video con caption opcional:
    caption = (msg.caption or "").strip()

    # Determinar caption en alemán
    if caption:
        de = translate_text(caption, source=MODEL_LANGUAGE, target=GROUP_LANGUAGE)
        de = spice_german(de)
        de = force_plural_group(de)
    else:
        de = hot_caption_de_plural()

    out_caption = format_as_aurora(de, aurora_name)

    # Reenviar media al grupo con caption
    try:
        if msg.photo:
            # mayor tamaño
            photo = msg.photo[-1]
            await context.bot.send_photo(chat_id=group_id, photo=photo.file_id, caption=out_caption)
            return
        if msg.video:
            await context.bot.send_video(chat_id=group_id, video=msg.video.file_id, caption=out_caption)
            return
    except Exception:
        # Si falla el reenvío, al menos manda el texto
        await context.bot.send_message(chat_id=group_id, text=out_caption)


# -----------------------------
# FLASK KEEP-ALIVE (Render)
# -----------------------------
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK", 200


def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)


# -----------------------------
# MAIN
# -----------------------------
def main():
    # Start Flask on a background thread
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))

    # Group: select streamer by replying
    app.add_handler(CommandHandler("setstreamer", cmd_setstreamer))

    # Private: live control
    app.add_handler(CommandHandler("liveon", cmd_liveon))
    app.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # Messages
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, handle_group_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, handle_private_message))

    # Polling (sync)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
