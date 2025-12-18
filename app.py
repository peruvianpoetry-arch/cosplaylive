import json
import logging
import os
from threading import Thread
from typing import Dict, Optional

from flask import Flask
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

from deep_translator import GoogleTranslator

# ───────────────── CONFIG ─────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cosplaylive_translate")

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_ANNOUNCE")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN (o TELEGRAM_ANNOUNCE) en Render")

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

# Admin (TU ID). Puedes poner varios separados por coma: "123,456"
ADMIN_IDS = set()
_admin_raw = os.environ.get("ADMIN_IDS", "").strip()
if _admin_raw:
    try:
        ADMIN_IDS = {int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()}
    except Exception:
        ADMIN_IDS = set()

ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")        # streamer_user_id -> group_chat_id
LIVE_FILE = os.path.join(DATA_DIR, "live.json")          # streamer_user_id -> true/false
STREAMER_FILE = os.path.join(DATA_DIR, "streamer.json")  # {"streamer_user_id": 123, "streamer_username": "@xxx"}
USERS_FILE = os.path.join(DATA_DIR, "users.json")        # "@username" -> user_id (cache)

# Traducción
translator_de_to_pt = GoogleTranslator(source="de", target="pt")
translator_pt_to_de = GoogleTranslator(source="pt", target="de")

# ───────────────── JSON helpers ─────────────────

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error(f"Error cargando {path}: {e}")
        return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error guardando {path}: {e}")

def load_rooms() -> Dict[str, int]:
    return load_json(ROOMS_FILE, {})

def save_rooms(d: Dict[str, int]):
    save_json(ROOMS_FILE, d)

def load_live() -> Dict[str, bool]:
    return load_json(LIVE_FILE, {})

def save_live(d: Dict[str, bool]):
    save_json(LIVE_FILE, d)

def load_streamer():
    return load_json(STREAMER_FILE, {"streamer_user_id": None, "streamer_username": None})

def save_streamer(streamer_user_id: int, streamer_username: Optional[str]):
    save_json(STREAMER_FILE, {"streamer_user_id": streamer_user_id, "streamer_username": streamer_username})

def load_users() -> Dict[str, int]:
    return load_json(USERS_FILE, {})

def save_users(d: Dict[str, int]):
    save_json(USERS_FILE, d)

def is_admin(user_id: int) -> bool:
    # Si ADMIN_IDS está vacío, permitimos que el primer admin se “auto-reclame” con /claimadmin
    return (user_id in ADMIN_IDS)

def get_streamer_id() -> Optional[int]:
    s = load_streamer()
    sid = s.get("streamer_user_id")
    return int(sid) if sid else None

def is_live(streamer_user_id: int) -> bool:
    live = load_live()
    return bool(live.get(str(streamer_user_id), False))

def set_live(streamer_user_id: int, on: bool):
    live = load_live()
    live[str(streamer_user_id)] = on
    save_live(live)

def get_room_for_streamer(streamer_user_id: int) -> Optional[int]:
    rooms = load_rooms()
    cid = rooms.get(str(streamer_user_id))
    return int(cid) if cid else None

# ───────────────── Util: hacer alemán menos formal ─────────────────

def make_german_more_casual(text: str) -> str:
    t = text
    # ajustes muy suaves (sin romper frases)
    t = t.replace("Sie ", "du ").replace(" Ihnen", " dir").replace(" Ihr ", " dein ")
    return t

# ───────────────── Registro de usuarios (para /setstreamer @user) ─────────────────

def cache_user(update: Update):
    user = update.effective_user
    if not user:
        return
    if user.username:
        users = load_users()
        users[f"@{user.username.lower()}"] = int(user.id)
        save_users(users)

# ───────────────── COMANDOS ─────────────────

def cmd_start(update: Update, context: CallbackContext):
    cache_user(update)
    update.message.reply_text(
        "✅ CosplayLive Translate listo.\n\n"
        "ADMIN:\n"
        "• /setstreamer @username  (elige streamer)\n"
        "• /whoami  (ver tu ID)\n\n"
        "STREAMER (Aurora):\n"
        "• En el grupo: /bindchat (una vez)\n"
        "• En privado: /liveon /liveoff\n\n"
        "Traducción:\n"
        "• Grupo (DE) -> Streamer (PT) en privado\n"
        "• Streamer (PT) -> Grupo (DE)\n"
    )

def cmd_whoami(update: Update, context: CallbackContext):
    cache_user(update)
    u = update.effective_user
    update.message.reply_text(f"ID: {u.id}\nUser: @{u.username}" if u.username else f"ID: {u.id}")

def cmd_claimadmin(update: Update, context: CallbackContext):
    # Solo si no hay ADMIN_IDS configurado (modo emergencia)
    global ADMIN_IDS
    if ADMIN_IDS:
        update.message.reply_text("ADMIN_IDS ya está configurado en Render. No necesitas /claimadmin.")
        return
    ADMIN_IDS.add(update.effective_user.id)
    update.message.reply_text("✅ Listo. Tu cuenta quedó como admin temporal (solo en memoria). "
                              "RECOMENDADO: pon tu ID en ADMIN_IDS en Render para que sea permanente.")

def cmd_setstreamer(update: Update, context: CallbackContext):
    """
    ADMIN: /setstreamer @aurorab23
    ADMIN: /setstreamer 123456789
    ADMIN: /setstreamer (respondiendo a un mensaje reenviado del streamer si trae forward info)
    """
    cache_user(update)
    uid = update.effective_user.id
    chat = update.effective_chat
    msg = update.effective_message

    if chat.type != "private":
        msg.reply_text("❌ /setstreamer se usa en PRIVADO conmigo.")
        return
    if not is_admin(uid):
        msg.reply_text("❌ No autorizado (admin).")
        return

    # Caso 1: por argumento @username o ID
    if context.args:
        arg = context.args[0].strip()
        # @username
        if arg.startswith("@"):
            users = load_users()
            key = arg.lower()
            sid = users.get(key)
            if not sid:
                msg.reply_text(
                    "⚠️ No tengo registrado ese @username aún.\n\n"
                    "Solución rápida:\n"
                    "1) Pídele a Aurora que le escriba 'hola' al bot (en privado).\n"
                    "2) Luego intenta otra vez: /setstreamer @Aurorab23"
                )
                return
            save_streamer(int(sid), arg)
            msg.reply_text(f"✅ Streamer configurada: {arg} (ID {sid})")
            return

        # ID numérico
        if arg.isdigit():
            save_streamer(int(arg), None)
            msg.reply_text(f"✅ Streamer configurada por ID: {arg}")
            return

    # Caso 2: por reply a un forward (si viene)
    if msg.reply_to_message:
        # forward_from (usuario)
        fwd_user = msg.reply_to_message.forward_from
        if fwd_user and fwd_user.id:
            sname = f"@{fwd_user.username}" if fwd_user.username else None
            save_streamer(int(fwd_user.id), sname)
            msg.reply_text(f"✅ Streamer configurada desde forward: {sname or fwd_user.id}")
            return

    msg.reply_text(
        "Uso:\n"
        "• /setstreamer @Aurorab23\n"
        "• /setstreamer 123456789\n\n"
        "Si el @ no funciona:\n"
        "1) Aurora debe escribirle al bot en privado (un 'hola').\n"
        "2) Luego repites /setstreamer @Aurorab23"
    )

def cmd_bindchat(update: Update, context: CallbackContext):
    """
    STREAMER: se usa EN EL GRUPO del live.
    Vincula ese grupo al streamer configurado.
    """
    cache_user(update)
    chat = update.effective_chat
    msg = update.effective_message

    if chat.type not in ("group", "supergroup"):
        msg.reply_text("❌ /bindchat se usa en el GRUPO (chat), no en privado.")
        return

    streamer_id = get_streamer_id()
    if not streamer_id:
        msg.reply_text("⚠️ Aún no hay streamer configurada. El admin debe hacer /setstreamer primero.")
        return

    # Solo streamer o admin puede bindear
    caller_id = update.effective_user.id
    if caller_id != streamer_id and not is_admin(caller_id):
        msg.reply_text("❌ Solo la streamer o el admin puede usar /bindchat aquí.")
        return

    rooms = load_rooms()
    rooms[str(streamer_id)] = int(chat.id)
    save_rooms(rooms)

    msg.reply_text(
        f"✅ Grupo vinculado al streamer.\n"
        f"Chat ID: {chat.id}\n\n"
        "Ahora la streamer puede activar traducción con /liveon en privado."
    )

def cmd_liveon(update: Update, context: CallbackContext):
    cache_user(update)
    chat = update.effective_chat
    msg = update.effective_message

    if chat.type != "private":
        msg.reply_text("❌ /liveon se usa en PRIVADO conmigo.")
        return

    streamer_id = get_streamer_id()
    if not streamer_id:
        msg.reply_text("⚠️ No hay streamer configurada. Admin debe hacer /setstreamer primero.")
        return

    if update.effective_user.id != streamer_id:
        msg.reply_text("❌ Solo la streamer configurada puede usar /liveon.")
        return

    room = get_room_for_streamer(streamer_id)
    if not room:
        msg.reply_text("⚠️ Falta vincular el grupo: entra al grupo y usa /bindchat.")
        return

    set_live(streamer_id, True)
    msg.reply_text(
        "🔥 LIVE ON.\n"
        "✅ Traducción activada.\n\n"
        "• Tú (PT) -> Grupo (DE)\n"
        "• Grupo (DE) -> Tú (PT) en privado"
    )

def cmd_liveoff(update: Update, context: CallbackContext):
    cache_user(update)
    chat = update.effective_chat
    msg = update.effective_message

    if chat.type != "private":
        msg.reply_text("❌ /liveoff se usa en PRIVADO conmigo.")
        return

    streamer_id = get_streamer_id()
    if not streamer_id:
        msg.reply_text("⚠️ No hay streamer configurada.")
        return

    if update.effective_user.id != streamer_id and not is_admin(update.effective_user.id):
        msg.reply_text("❌ Solo streamer o admin.")
        return

    set_live(streamer_id, False)
    msg.reply_text("⛔ LIVE OFF. Traducción desactivada.")

# ───────────────── TRADUCCIÓN ─────────────────

def handle_group_messages(update: Update, context: CallbackContext):
    cache_user(update)
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return
    if user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        return

    streamer_id = get_streamer_id()
    if not streamer_id:
        return
    if not is_live(streamer_id):
        return

    room = get_room_for_streamer(streamer_id)
    if not room or int(room) != int(chat.id):
        return

    text = msg.text or ""
    if not text.strip():
        return

    try:
        pt = translator_de_to_pt.translate(text)
    except Exception as e:
        logger.error(f"DE->PT error: {e}")
        return

    username = f"@{user.username}" if user.username else (user.first_name or "User")
    payload = f"💬 <b>{username}</b>\n🇩🇪 {text}\n🇵🇹 {pt}"

    try:
        context.bot.send_message(chat_id=streamer_id, text=payload, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Send to streamer error: {e}")

def handle_streamer_private(update: Update, context: CallbackContext):
    cache_user(update)
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return
    if chat.type != "private":
        return
    if user.is_bot:
        return

    streamer_id = get_streamer_id()
    if not streamer_id:
        return
    if user.id != streamer_id:
        return
    if not is_live(streamer_id):
        return

    room = get_room_for_streamer(streamer_id)
    if not room:
        return

    text = msg.text or ""
    if not text.strip():
        return

    try:
        de = translator_pt_to_de.translate(text)
        de = make_german_more_casual(de)
    except Exception as e:
        logger.error(f"PT->DE error: {e}")
        return

    out = f"🎙️ <b>Aurora</b>: {de}"
    try:
        context.bot.send_message(chat_id=room, text=out, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Publish to group error: {e}")

# ───────────────── FLASK KEEP ALIVE ─────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "OK - CosplayLive Translate"

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# ───────────────── MAIN ─────────────────

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # comandos
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whoami", cmd_whoami))
    dp.add_handler(CommandHandler("claimadmin", cmd_claimadmin))
    dp.add_handler(CommandHandler("setstreamer", cmd_setstreamer))
    dp.add_handler(CommandHandler("bindchat", cmd_bindchat))
    dp.add_handler(CommandHandler("liveon", cmd_liveon))
    dp.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # mensajes
    dp.add_handler(MessageHandler(Filters.chat_type.groups & Filters.text & ~Filters.command, handle_group_messages))
    dp.add_handler(MessageHandler(Filters.chat_type.private & Filters.text & ~Filters.command, handle_streamer_private))

    # Flask en hilo
    Thread(target=run_flask, daemon=True).start()

    updater.start_polling(drop_pending_updates=True)
    logger.info("CosplayLive Translate running...")
    updater.idle()

if __name__ == "__main__":
    main()
