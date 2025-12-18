import os
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from flask import Flask

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG / LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("cosplaylive")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN (o TELEGRAM_TOKEN) en Environment Variables")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "state.json"

DEFAULT_GROUP_LANG = os.environ.get("GROUP_LANGUAGE", "de")   # idioma del grupo (lo que escriben usuarios)
DEFAULT_MODEL_LANG = os.environ.get("MODEL_LANGUAGE", "pt")   # idioma de Aurora

# =========================
# PERSISTENCIA
# =========================
def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("No se pudo leer state.json, creando uno nuevo.")
    return {
        "bound_chat_id": None,          # chat donde se publica (grupo/canal)
        "owner_user_id": None,          # el primero que haga /bindhere será el owner
        "streamer_user_id": None,       # user_id de Aurora
        "streamer_name": None,          # nombre opcional
        "group_lang": DEFAULT_GROUP_LANG,
        "model_lang": DEFAULT_MODEL_LANG,
    }

def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

STATE = load_state()

# =========================
# TRADUCCIÓN (Deep Translator)
# =========================
def translate_text(text: str, source: str, target: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if source == target:
        return text
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source=source, target=target).translate(text)
    except Exception:
        logger.exception("Fallo traducción. Devuelvo texto original.")
        return text

# =========================
# HELPERS
# =========================
def is_owner(user_id: int) -> bool:
    return STATE.get("owner_user_id") == user_id

def require_owner(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
        uid = update.effective_user.id
        if STATE.get("owner_user_id") is None:
            # si aún no hay owner, el primero que haga /bindhere será owner (controlado en bindhere)
            await update.effective_message.reply_text("⚠️ Primero usa /bindhere en el grupo para establecer el owner y el chat.")
            return
        if not is_owner(uid):
            await update.effective_message.reply_text("⛔ Solo el owner/admin configurado puede usar este comando.")
            return
        return await func(update, context)
    return wrapper

# =========================
# COMANDOS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "✅ Bot funcionando correctamente.\n"
        "Si eres streamer/modelo: solo escribe normal aquí.\n"
        "Si eres owner: usa /bindhere en el grupo y luego /setstreamer respondiendo a un mensaje de Aurora."
    )

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"👤 user_id: {u.id}\n"
        f"🗣 username: @{u.username}" if u.username else f"👤 user_id: {u.id}\n🗣 username: (sin username)\n"
        f"💬 chat_id: {chat.id}\n"
        f"💬 chat_type: {chat.type}"
    )

@require_owner
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📌 Estado actual:\n"
        f"- bound_chat_id: {STATE.get('bound_chat_id')}\n"
        f"- owner_user_id: {STATE.get('owner_user_id')}\n"
        f"- streamer_user_id: {STATE.get('streamer_user_id')}\n"
        f"- streamer_name: {STATE.get('streamer_name')}\n"
        f"- group_lang: {STATE.get('group_lang')}\n"
        f"- model_lang: {STATE.get('model_lang')}"
    )

async def bindhere_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Se ejecuta EN EL GRUPO donde quieres que el bot publique.
    El primero que ejecute esto se vuelve owner.
    """
    if not update.effective_user or not update.effective_chat:
        return

    chat = update.effective_chat
    user = update.effective_user

    # Solo tiene sentido en grupos/supergrupos/canales vinculados (canales no tienen mensajes normales del bot)
    # Pero lo permitimos igualmente si el bot recibe el comando allí.
    STATE["bound_chat_id"] = chat.id

    if STATE.get("owner_user_id") is None:
        STATE["owner_user_id"] = user.id

    save_state(STATE)

    await update.effective_message.reply_text(
        "✅ Chat vinculado.\n"
        f"bound_chat_id = {chat.id}\n"
        f"owner_user_id = {STATE.get('owner_user_id')}\n\n"
        "Siguiente paso:\n"
        "1) Aurora manda un mensaje aquí (solo 1 vez).\n"
        "2) Tú respondes a su mensaje y ejecutas /setstreamer"
    )

@require_owner
async def setstreamer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Debe ejecutarse COMO REPLY a un mensaje de Aurora.
    Así no necesitas @ ni IDs.
    """
    msg = update.effective_message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("⚠️ Usa /setstreamer RESPONDIENDO (reply) a un mensaje de Aurora en este chat.")
        return

    target_user = msg.reply_to_message.from_user
    STATE["streamer_user_id"] = target_user.id
    STATE["streamer_name"] = target_user.full_name
    save_state(STATE)

    await msg.reply_text(
        "✅ Streamer seleccionado.\n"
        f"Streamer: {target_user.full_name}\n"
        f"user_id: {target_user.id}\n\n"
        "Prueba ahora:\n"
        "- En el grupo escribe algo en alemán → se enviará traducido al privado del streamer.\n"
        "- En privado, el streamer escribe algo → se publicará traducido aquí."
    )

@require_owner
async def setlangs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setlangs de pt
    group_lang model_lang
    """
    msg = update.effective_message
    if len(context.args) != 2:
        await msg.reply_text("Uso: /setlangs <group_lang> <model_lang>\nEj: /setlangs de pt")
        return
    STATE["group_lang"] = context.args[0].strip().lower()
    STATE["model_lang"] = context.args[1].strip().lower()
    save_state(STATE)
    await msg.reply_text(f"✅ Idiomas guardados: group={STATE['group_lang']} model={STATE['model_lang']}")

# =========================
# FLUJOS DE MENSAJES
# =========================
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mensajes escritos en el grupo:
    -> se traducen y se mandan por privado al streamer (Aurora)
    """
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if STATE.get("bound_chat_id") != chat_id:
        return  # ignorar otros chats

    streamer_id = STATE.get("streamer_user_id")
    if not streamer_id:
        return  # aún no hay streamer

    # Ignorar mensajes del propio streamer en el grupo (para evitar bucles)
    if update.effective_user.id == streamer_id:
        return

    text = (update.effective_message.text or "").strip()
    if not text:
        return

    # Traduce del idioma del grupo al de la modelo
    src = STATE.get("group_lang", DEFAULT_GROUP_LANG)
    tgt = STATE.get("model_lang", DEFAULT_MODEL_LANG)
    translated = translate_text(text, source=src, target=tgt)

    sender = update.effective_user.full_name
    out = f"💬 {sender}:\n{translated}"

    try:
        await context.bot.send_message(chat_id=streamer_id, text=out)
    except Exception:
        # Si Aurora no abrió el bot, Telegram no deja escribirle
        logger.exception("No pude enviar DM al streamer. Probable: streamer no inició chat con el bot.")
        # Aviso suave al grupo (solo una vez por minuto para no spamear)
        now = int(time.time())
        last = context.chat_data.get("last_dm_warn", 0)
        if now - last > 60:
            context.chat_data["last_dm_warn"] = now
            await update.effective_message.reply_text(
                "⚠️ No pude enviar el DM al streamer.\n"
                "Aurora debe abrir el chat con el bot y pulsar Start una vez (requisito de Telegram)."
            )

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mensajes privados:
    - Si los manda el streamer: traducir y publicar en el grupo
    - Si los manda el owner: responder status rápido
    """
    if not update.effective_chat or not update.effective_user:
        return
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    bound_chat = STATE.get("bound_chat_id")
    if not bound_chat:
        await update.effective_message.reply_text("⚠️ Aún no hay chat vinculado. Owner debe usar /bindhere en el grupo.")
        return

    streamer_id = STATE.get("streamer_user_id")

    # Si es streamer, publica al grupo
    if streamer_id and uid == streamer_id:
        src = STATE.get("model_lang", DEFAULT_MODEL_LANG)
        tgt = STATE.get("group_lang", DEFAULT_GROUP_LANG)
        translated = translate_text(text, source=src, target=tgt)
        try:
            await context.bot.send_message(chat_id=bound_chat, text=f"🔥 {translated}")
        except Exception:
            logger.exception("No pude publicar en el grupo. ¿Bot tiene permisos?")
            await update.effective_message.reply_text("⚠️ No pude publicar en el grupo. Revisa permisos del bot en el grupo.")
        return

    # Si es owner, feedback rápido
    if is_owner(uid):
        await update.effective_message.reply_text(
            "✅ Owner DM.\n"
            "Recuerda: /bindhere en el grupo, luego /setstreamer respondiendo al mensaje de Aurora."
        )
        return

    # Otros privados: ignorar o mensaje neutro
    await update.effective_message.reply_text("✅ Bot activo. Escribe en el grupo para comunicarte.")

# =========================
# FLASK KEEP ALIVE
# =========================
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# =========================
# MAIN
# =========================
def main():
    # levanta Flask en thread para Render
    import threading
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    app = Application.builder().token(TOKEN).build()

    # comandos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(CommandHandler("bindhere", bindhere_cmd))
    app.add_handler(CommandHandler("setstreamer", setstreamer_cmd))
    app.add_handler(CommandHandler("setlangs", setlangs_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # mensajes
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, handle_group_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))

    logger.info("Bot iniciado. Polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
