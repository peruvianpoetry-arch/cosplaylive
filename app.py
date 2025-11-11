import os
import json
import threading
import logging
from flask import Flask, request, jsonify, render_template_string, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from deep_translator import GoogleTranslator

# ----------------------------
# CONFIGURACIÓN PRINCIPAL
# ----------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL = os.getenv("BASE_URL", "https://cosplaylive.onrender.com")
ADMIN_IDS = os.getenv("ADMIN_USER_ID", "").split(",")  # ✅ Corregido (sin la S)
TRANSLATE_TO = os.getenv("TRANSLATE_TO", "de")

# LOGGING
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# FLASK APP
# ----------------------------
app = Flask(__name__)
data_file = "/var/data/data.json"

if not os.path.exists("/var/data"):
    os.makedirs("/var/data", exist_ok=True)

if not os.path.exists(data_file):
    with open(data_file, "w") as f:
        json.dump({}, f)

def load_data():
    try:
        with open(data_file, "r") as f:
            return json.load(f)
    except:
        return {}

def save_data(data):
    with open(data_file, "w") as f:
        json.dump(data, f)

# ----------------------------
# TELEGRAM BOT
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bienvenido al bot Cosplay Live. Usa /menu para ver opciones.")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id in ADMIN_IDS:
        await update.message.reply_text(f"✅ Eres admin (ID: {user_id})")
    else:
        await update.message.reply_text(f"❌ Solo admin.\nTu ID es {user_id}")

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Solo admin.")
        return
    await update.message.reply_text("🔴 Transmisión en vivo activada.\nLos anuncios están corriendo.")

async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Solo admin.")
        return
    await update.message.reply_text("⚫ Transmisión finalizada.")

async def translate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    src_lang = "auto"
    try:
        translated = GoogleTranslator(source=src_lang, target=TRANSLATE_TO).translate(text)
        await update.message.reply_text(f"🇩🇪 {translated}")
    except Exception as e:
        logger.error(f"Error traduciendo: {e}")

# ----------------------------
# COMANDOS ADMIN EXTRA
# ----------------------------
async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Solo admin.")
        return
    await update.message.reply_text("💰 Precio agregado correctamente (modo demo).")

async def listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Solo admin.")
        return
    await update.message.reply_text("💵 Lista de precios (modo demo).")

# ----------------------------
# MANEJADOR DE FLASK
# ----------------------------
@app.route("/")
def index():
    return "Cosplay Live Bot funcionando correctamente."

@app.route("/donar")
def donar():
    return jsonify({"status": "ok", "message": "Sistema de donaciones activo"})

@app.route("/overlay")
def overlay():
    return render_template_string("""
    <html><body style="background:black;color:white;font-family:sans-serif;">
    <h2>💬 Overlay activo</h2>
    <p>Mensajes en vivo aparecerán aquí.</p>
    </body></html>
    """)

# ----------------------------
# INICIO DEL BOT
# ----------------------------
def run_bot():
    app_tg = ApplicationBuilder().token(TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(CommandHandler("whoami", whoami))
    app_tg.add_handler(CommandHandler("liveon", liveon))
    app_tg.add_handler(CommandHandler("liveoff", liveoff))
    app_tg.add_handler(CommandHandler("addprice", addprice))
    app_tg.add_handler(CommandHandler("listprices", listprices))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_message))
    app_tg.run_polling(drop_pending_updates=True)

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_flask)
    web_thread.start()
    run_bot()
