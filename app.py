import os
import json
import threading
import logging
import asyncio
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# =============================
# VARIABLES DE ENTORNO
# =============================
TOKEN     = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_ID  = int(os.getenv("ADMIN_USER_ID", "0"))
BASE_URL  = os.getenv("BASE_URL", "https://cosplaylive.onrender.com").rstrip("/")
CURRENCY  = os.getenv("CURRENCY", "EUR")
PORT      = int(os.getenv("PORT", "10000"))

DATA_FILE = "/var/data/data.json"

# =============================
# LOGGING
# =============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("cosplaylive")

# =============================
# FLASK
# =============================
app = Flask(__name__)

@app.route("/")
def health():
    return "✅ CosplayLive: Flask OK & Bot OK"

@app.route("/donar")
def simular_donacion():
    amount = request.args.get("amt", "0")
    item   = request.args.get("item", "Sin descripción")
    return f"OK, simulación de donación recibida. Monto: {amount} {CURRENCY} | Item: {item}"

# =============================
# PERSISTENCIA
# =============================
def _load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"prices": {}}

def _save_data(d):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

data = _load_data()
if "prices" not in data or not isinstance(data["prices"], dict):
    data["prices"] = {}

# =============================
# BOT HANDLERS
# =============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola! Usa:\n"
        "• /addprice Nombre Precio → agrega opción (ej. `/addprice 🍑 Titten 5` o `/addprice 🍑 Titten, 5€`)\n"
        "• /listprices → ver lista\n"
        "• /liveon → mostrar menú\n"
        "• /whoami → comprobar admin"
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        await update.message.reply_text(f"✅ Eres admin (ID: {uid})")
    else:
        await update.message.reply_text(f"❌ No eres admin. Tu ID: {uid}")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Solo admin puede usar este comando.")
        return

    raw = update.message.text.split(" ", 1)[1].strip()
    # admite formato con coma o espacio
    raw = raw.replace(",", " ").replace("€", "").strip()
    parts = raw.rsplit(" ", 1)

    if len(parts) < 2:
        await update.message.reply_text("Formato incorrecto. Usa: /addprice 🍑 Nombre 5€")
        return

    name = parts[0].strip()
    try:
        price = float(parts[1])
    except ValueError:
        await update.message.reply_text("Precio inválido. Ejemplo: /addprice 🍑 Titten 5")
        return

    data["prices"][name] = price
    _save_data(data)
    await update.message.reply_text(f"💰 Agregado: {name} → {price}€")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data["prices"]:
        await update.message.reply_text("🪙 No hay precios configurados.")
        return
    msg = "💸 *Lista de precios:*\n" + "\n".join(f"• {k} → {v}€" for k, v in data["prices"].items())
    await update.message.reply_text(msg, parse_mode="Markdown")

def _only_emojis(s: str) -> str:
    e = "".join(ch for ch in s if ord(ch) > 1000)
    return e if e else "🔥"

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data["prices"]:
        await update.message.reply_text("Primero agrega precios con /addprice.")
        return

    keyboard = []
    for item, price in data["prices"].items():
        emoji = _only_emojis(item)
        url = f"{BASE_URL}/donar?amt={price}&item={emoji}"
        keyboard.append([InlineKeyboardButton(f"{item} - {price}€", url=url)])

    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎬 Opciones disponibles:", reply_markup=markup)

# =============================
# APLICACIÓN TELEGRAM
# =============================
if not TOKEN:
    raise SystemExit("Falta TELEGRAM_TOKEN en variables de entorno.")

application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler("start",     cmd_start))
application.add_handler(CommandHandler("whoami",    cmd_whoami))
application.add_handler(CommandHandler("addprice",  cmd_addprice))
application.add_handler(CommandHandler("listprices",cmd_listprices))
application.add_handler(CommandHandler("liveon",    cmd_liveon))
application.add_handler(CallbackQueryHandler(lambda *_: None, pattern=r"^noop$"))

# =============================
# ARRANQUE CORREGIDO
# =============================
def start_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        application.run_polling(stop_signals=None, allowed_updates=Update.ALL_TYPES)
    )

if __name__ == "__main__":
    t = threading.Thread(target=start_bot_thread, daemon=True, name="tg-bot")
    t.start()
    print("🤖 Bot iniciando en Render… ✅ Iniciando servidor Flask y bot…")
    app.run(host="0.0.0.0", port=PORT, debug=False)
