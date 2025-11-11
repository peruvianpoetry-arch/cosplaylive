import os
import json
import threading
import logging
import asyncio
from flask import Flask, request
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# =============================
# CONFIGURACIÓN BÁSICA
# =============================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_USER_ID", "0"))
BASE_URL = os.getenv("BASE_URL", "https://cosplaylive.onrender.com")
CURRENCY = os.getenv("CURRENCY", "EUR")

DATA_FILE = "/var/data/data.json"

# =============================
# LOGGING
# =============================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============================
# FLASK APP
# =============================
app = Flask(__name__)

@app.route('/')
def index():
    return "CosplayLive Bot corriendo correctamente ✅"

@app.route('/donar')
def simular_donacion():
    amount = request.args.get("amt", "0")
    item = request.args.get("item", "Sin descripción")
    return f"OK, simulación de donación recibida. Monto: {amount} {CURRENCY} | Item: {item}"

# =============================
# MANEJO DE DATOS
# =============================
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"prices": {}}

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

data = load_data()

# =============================
# FUNCIONES DEL BOT
# =============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola! Usa /addprice Nombre Precio para añadir opciones.\n"
        "Ejemplo: /addprice 🍑 Titten 5\n"
        "Luego usa /liveon para mostrar el menú."
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID:
        await update.message.reply_text(f"✅ Eres admin (ID: {user_id})")
    else:
        await update.message.reply_text(f"❌ No eres admin. Tu ID: {user_id}")

async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Solo admin puede usar este comando.")
        return

    try:
        text = update.message.text.split(" ", 1)[1]
        parts = text.split()
        name = " ".join(parts[:-1])
        price = float(parts[-1].replace("€", "").replace(",", "."))
    except Exception:
        await update.message.reply_text("Formato incorrecto. Usa: /addprice 🍑 Nombre 5€")
        return

    data["prices"][name] = price
    save_data(data)
    await update.message.reply_text(f"💰 Precio agregado correctamente: {name} = {price}€")

async def listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data["prices"]:
        await update.message.reply_text("🪙 No hay precios configurados.")
        return

    msg = "💸 *Lista de precios:*\n"
    for k, v in data["prices"].items():
        msg += f"• {k} → {v}€\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data["prices"]:
        await update.message.reply_text("Primero agrega precios con /addprice.")
        return

    keyboard = []
    for item, price in data["prices"].items():
        # Se muestra el nombre real en el botón, pero Stripe verá solo el emoji y el precio
        emoji_part = ''.join([c for c in item if ord(c) > 1000]) or "🔥"
        button = InlineKeyboardButton(
            f"{item} - {price}€",
            url=f"{BASE_URL}/donar?amt={price}&item={emoji_part}"
        )
        keyboard.append([button])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎬 Opciones disponibles:", reply_markup=reply_markup)

# =============================
# HANDLERS
# =============================
application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("whoami", whoami))
application.add_handler(CommandHandler("addprice", addprice))
application.add_handler(CommandHandler("listprices", listprices))
application.add_handler(CommandHandler("liveon", liveon))
application.add_handler(CallbackQueryHandler(lambda u, c: None, pattern=r"^noop$"))

# =============================
# ARRANQUE CORREGIDO
# =============================
def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        application.run_polling(stop_signals=None, allowed_updates=Update.ALL_TYPES)
    )

if __name__ == "__main__":
    # Hilo del bot
    t = threading.Thread(target=start_bot, daemon=True, name="tg-bot")
    t.start()

    port = int(os.environ.get("PORT", "10000"))
    print("🤖 Bot iniciando en Render… ✅ Iniciando servidor Flask y bot…")
    app.run(host="0.0.0.0", port=port, debug=False)
