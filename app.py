import os
import json
import threading
from urllib.parse import quote_plus
from datetime import datetime
from flask import Flask, request, jsonify, abort
import stripe

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from deep_translator import GoogleTranslator

# ========= CONFIG =========
DATA_FILE = os.environ.get("DATA_FILE", "/var/data/data.json")
CURRENCY = os.environ.get("CURRENCY", "EUR")
AUTO_INTERVAL_MIN = int(os.environ.get("AUTO_INTERVAL_MIN", "10"))
TRANSLATE_TO = os.environ.get("TRANSLATE_TO", "").strip().lower()  # ej. 'de', 'en', 'es'

PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")
BASE_URL = os.environ.get("BASE_URL", PUBLIC_BASE).rstrip("/")
if not BASE_URL:
    BASE_URL = "http://localhost:10000"

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)  # compat

ADMIN_IDS_ENV = os.environ.get("ADMIN_USER_IDS") or os.environ.get("ADMIN_USER_ID", "")
ADMIN_IDS = {int(x) for x in ADMIN_IDS_ENV.replace(";", ",").split(",") if x.strip().isdigit()}

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

ENABLE_TRANSLATION = os.environ.get("ENABLE_TRANSLATION", "True").lower() == "true"

# Stripe (si hay clave)
if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

# ========= STORAGE =========
_lock = threading.Lock()

def default_data():
    return {
        "prices": {},      # code -> {"name":"Etiqueta bonita", "amount": float}
        "live_chats": {},  # chat_id -> {"on": bool}
    }

def load_data():
    with _lock:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default_data()

def save_data(d):
    with _lock:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

def slugify_code(name: str) -> str:
    # Código ASCII seguro para URLs/Stripe. Reemplaza espacios por _
    # y deja solo [a-z0-9_]
    base = "".join(ch.lower() if ch.isalnum() else "_" for ch in name)
    while "__" in base:
        base = base.replace("__", "_")
    return base.strip("_") or "item"

# ========= TELEGRAM =========
application: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if is_admin(uid):
        await update.effective_message.reply_text(f"✅ Eres admin (ID: {uid})")
    else:
        await update.effective_message.reply_text("❌ Solo admin.")

def build_menu_keyboard(d):
    # Botones con label bonito, link a /pay con monto y código ASCII seguro
    rows = []
    for code, item in d["prices"].items():
        amount = float(item["amount"])
        label = f"{item['name']} · {amount:.2f} {CURRENCY}"
        pay_url = f"{BASE_URL}/pay?amt={amount:.2f}&item={quote_plus(code)}"
        rows.append([InlineKeyboardButton(label, url=pay_url)])
    return InlineKeyboardMarkup(rows)

def prices_text(d):
    if not d["prices"]:
        return "Aún no hay precios. Usa /addprice Nombre, 10"
    lines = ["🎬 *Menú del show*"]
    for code, item in d["prices"].items():
        lines.append(f"• {item['name']} — {float(item['amount']):.2f} {CURRENCY}")
    return "\n".join(lines)

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, pin=False):
    d = load_data()
    text = prices_text(d)
    kb = build_menu_keyboard(d)
    msg = await update.effective_message.reply_markdown(text, reply_markup=kb)
    if pin and update.effective_chat.type in ("supergroup", "group"):
        try:
            await context.bot.pin_chat_message(update.effective_chat.id, msg.message_id)
        except Exception:
            pass

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Hola 👋\nUsa /addprice Nombre, 5 para añadir opciones.\n"
        "/liveon o /liveoff para controlar el show."
    )

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await update.effective_message.reply_text("❌ Solo admin.")
        return

    # Texto esperado: "/addprice Nombre, 12.5"
    args = update.effective_message.text.partition(" ")[2].strip()
    if not args or "," not in args:
        await update.effective_message.reply_text("Formato incorrecto. Usa: /addprice 🍑 Nombre, 5€")
        return

    name_part, _, price_part = args.partition(",")
    name = name_part.strip()
    try:
        price = float(price_part.strip().replace("€", "").replace(",", "."))
    except Exception:
        await update.effective_message.reply_text("Precio inválido. Ej: /addprice Titten, 10")
        return

    d = load_data()
    code = slugify_code(name)  # código ASCII que viaja a Stripe/URL
    d["prices"][code] = {"name": name, "amount": price}
    save_data(d)
    await update.effective_message.reply_text("💰 Precio agregado correctamente.")
    # Refrescamos el menú si está live
    await show_menu(update, context)

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    await update.effective_message.reply_markdown(prices_text(d))

# ---- Live ON/OFF con anuncios automáticos ----
def job_name_for(chat_id): return f"auto_ads_{chat_id}"

async def send_auto_ad(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    d = load_data()
    if not d["live_chats"].get(str(chat_id), {}).get("on"):
        return
    try:
        # Mensaje de marketing sencillo + menú
        await context.bot.send_message(chat_id, "🔥 ¡El show sigue en vivo! Apoya con uno de los botones:")
        # Enviar menú
        from telegram.constants import ParseMode
        text = prices_text(d)
        kb = build_menu_keyboard(d)
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        pass

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await update.effective_message.reply_text("❌ Solo admin.")
        return

    chat_id = update.effective_chat.id
    d = load_data()
    d["live_chats"][str(chat_id)] = {"on": True}
    save_data(d)

    # Mostrar menú + fijarlo
    await show_menu(update, context, pin=True)

    # Programar anuncios
    jq = context.job_queue
    if jq is not None:
        # Cancela previos y programa nuevo
        for j in jq.get_jobs_by_name(job_name_for(chat_id)):
            j.schedule_removal()
        jq.run_repeating(send_auto_ad, interval=AUTO_INTERVAL_MIN * 60, first=60,
                         chat_id=chat_id, name=job_name_for(chat_id))
        await update.effective_message.reply_text("✅ Live ON. Anuncios automáticos activados.")
    else:
        await update.effective_message.reply_text("⚠️ Live ON. (Sin JobQueue: instala python-telegram-bot[job-queue])")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await update.effective_message.reply_text("❌ Solo admin.")
        return
    chat_id = update.effective_chat.id
    d = load_data()
    d["live_chats"][str(chat_id)] = {"on": False}
    save_data(d)

    jq = context.job_queue
    if jq is not None:
        for j in jq.get_jobs_by_name(job_name_for(chat_id)):
            j.schedule_removal()
    await update.effective_message.reply_text("🛑 Live OFF. Anuncios detenidos.")

# ---- Traducción en chat (cuando Live ON) ----
def should_translate(update: Update) -> bool:
    # Solo mensajes de texto de usuarios (no bots/canales), en grupos, y si live está ON
    if update.effective_chat is None or update.effective_message is None:
        return False
    if update.effective_message.from_user and update.effective_message.from_user.is_bot:
        return False
    if update.effective_chat.type not in ("supergroup", "group"):
        return False
    d = load_data()
    return d["live_chats"].get(str(update.effective_chat.id), {}).get("on", False)

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ENABLE_TRANSLATION or not TRANSLATE_TO:
        return
    if not should_translate(update):
        return
    txt = update.effective_message.text
    if not txt:
        return
    try:
        tr = GoogleTranslator(source="auto", target=TRANSLATE_TO).translate(txt)
        # Bandera/emoji simple por idioma destino
        prefix = "🇩🇪" if TRANSLATE_TO == "de" else ("🇬🇧" if TRANSLATE_TO == "en" else "🌐")
        await update.effective_message.reply_text(f"{prefix} {tr}")
    except Exception:
        pass

# ========= FLASK =========
app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

@app.get("/pay")
def pay():
    """
    Crea Checkout Session de Stripe si hay clave.
    /pay?amt=10.00&item=muschi
    """
    amt_str = (request.args.get("amt") or "").replace(",", ".")
    code = (request.args.get("item") or "").lower()
    try:
        amount = float(amt_str)
    except Exception:
        abort(400, "Invalid amount")

    d = load_data()
    item = d["prices"].get(code) if code else None
    label = item["name"] if item else f"Apoyo {amount:.2f} {CURRENCY}"

    if not STRIPE_SECRET:
        # Simulación local/simple si no hay Stripe
        return f"OK, simulación de donación recibida. Monto: {amount:.1f} {CURRENCY} | Item: {label}", 200

    try:
        # Stripe maneja bien UTF-8 en product_data.name
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": CURRENCY.lower(),
                    "product_data": {"name": label},
                    "unit_amount": int(round(amount * 100)),
                },
                "quantity": 1
            }],
            success_url=f"{BASE_URL}/ok?m={amount:.2f}",
            cancel_url=f"{BASE_URL}/cancel",
        )
        return "", 303, {"Location": session.url}
    except Exception as e:
        return f"Stripe error: {e}", 500

@app.get("/ok")
def ok():
    return "✅ Pago completado (modo test). Gracias por apoyar.", 200

@app.get("/cancel")
def cancel():
    return "❎ Pago cancelado.", 200

# ========= ARRANQUE =========
def start_bot_in_thread():
    # Handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("addprice", cmd_addprice))
    application.add_handler(CommandHandler("listprices", cmd_listprices))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # Traducción de texto
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_in_chat))

    # Ejecuta polling en hilo aparte
    def _run():
        application.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)

    t = threading.Thread(target=_run, name="tg-bot", daemon=True)
    t.start()

if __name__ == "__main__":
    print("🤖 Bot iniciando en Render… ✅ Iniciando servidor Flask y bot…", flush=True)
    start_bot_in_thread()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=False)
