import os, json, threading, logging
from urllib.parse import quote
from time import monotonic

from flask import Flask, request, jsonify
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters,
)

# -------------------- Config --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
log = logging.getLogger("cosplaylive")

BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
ADMIN_IDS_ENV    = os.environ.get("ADMIN_USER_IDS", "")  # coma-sep
PUBLIC_BASE      = (os.environ.get("PUBLIC_BASE") or os.environ.get("BASE_URL") or "").rstrip("/")
CURRENCY         = os.environ.get("CURRENCY", "EUR")
TRANSLATE_TO     = os.environ.get("TRANSLATE_TO", "").strip().lower()
AUTO_MIN         = int(os.environ.get("AUTO_INTERVAL_MIN", "10"))
DATA_DIR         = os.environ.get("DATA_DIR", "/var/data")
STRIPE_KEY       = os.environ.get("STRIPE_SECRET_KEY", "").strip()

os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

def _load():
    if not os.path.exists(DATA_FILE):
        return {"prices": {}, "live_chats": {}, "created": monotonic()}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(d):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

DATA = _load()

def is_admin(user_id:int) -> bool:
    if not ADMIN_IDS_ENV:
        return True  # si no hay lista, no bloqueamos
    allowed = {int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip().isdigit()}
    return user_id in allowed

# -------------------- Flask (pagos) --------------------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK cosplaylive"

def _stripe_checkout(amount_cents:int, name:str):
    if not STRIPE_KEY:
        # Simulación
        return {"mode": "sim", "ok": True, "amount": amount_cents, "name": name}
    import stripe
    stripe.api_key = STRIPE_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": CURRENCY.lower(),
                "product_data": {"name": name[:60]},
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        success_url=f"{PUBLIC_BASE}/pay/success",
        cancel_url=f"{PUBLIC_BASE}/pay/cancel",
    )
    return {"mode": "stripe", "id": session.id, "url": session.url}

@app.get("/pay")
def pay():
    # /pay?amt=500&name=Titten
    try:
        amount = int(request.args.get("amt", "0"))
        name = request.args.get("name", "Support")
        res = _stripe_checkout(amount, name)
        if res["mode"] == "sim":
            return f"OK, simulación de pago: {amount/100:.2f} {CURRENCY} | Item: {name}"
        return f'<a href="{res["url"]}">Ir a Stripe</a>', 200
    except Exception as e:
        log.exception("Error en /pay")
        return f"Error: {e}", 500

# -------------------- Bot --------------------
application: Application = Application.builder().token(BOT_TOKEN).build()

def _menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    # Orden estable por nombre
    for name, price in sorted(DATA["prices"].items()):
        # botones legibles para Telegram; URL sanitizada
        amt_cents = int(round(float(price) * 100))
        safe_name = quote(name, safe="")
        url = f"{PUBLIC_BASE}/pay?amt={amt_cents}&name={safe_name}"
        label = f"{name} · {float(price):.2f} {CURRENCY}"
        buttons.append([InlineKeyboardButton(text=label, url=url)])
    return InlineKeyboardMarkup(buttons) if buttons else InlineKeyboardMarkup([])

def _prices_text() -> str:
    if not DATA["prices"]:
        return "Aún no hay precios. Usa /addprice Nombre, 10"
    lines = [f"• {n} — {float(p):.2f} {CURRENCY}" for n, p in sorted(DATA["prices"].items())]
    return "🎬 Menú del show\n" + "\n".join(lines) + "\n\nPulsa un botón para apoyar al show 🔥"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    uid = update.effective_user.id if update.effective_user else 0
    if not m:
        return
    await m.reply_text("Hola 👋\nUsa /addprice Nombre, 5 para añadir opciones.\n/liveon o /liveoff para controlar el menú.")
    if is_admin(uid):
        await m.reply_text(f"✅ Eres admin (ID: {uid})")

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m: return
    uid = update.effective_user.id if update.effective_user else 0
    await m.reply_text(f"{'✅' if is_admin(uid) else '❌'} Eres admin (ID: {uid})")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m: return
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await m.reply_text("Solo admin.")
        return
    # Formato: /addprice Nombre, 10
    raw = (m.text or "").split(" ", 1)
    if len(raw) < 2 or "," not in raw[1]:
        await m.reply_text("Formato incorrecto. Usa: /addprice 🍑 Nombre, 5€")
        return
    name_part, price_part = [x.strip() for x in raw[1].split(",", 1)]
    try:
        price = float(price_part.replace("€", "").replace(",", ".").strip())
    except Exception:
        await m.reply_text("Precio inválido. Ej: /addprice Titten, 5")
        return
    DATA["prices"][name_part] = price
    _save(DATA)
    await m.reply_text("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m: return
    await m.reply_text(_prices_text(), reply_markup=_menu_keyboard())

# ---- Anuncios automáticos ----
def _job_name(chat_id:int) -> str:
    return f"auto_ads_{chat_id}"

async def _auto_ad(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id if context.job else None
    if not chat_id: return
    try:
        await context.bot.send_message(chat_id, "✨ ¡Disfruta el show! Si te gusta, apóyalo con un botón del menú. ✨",
                                       reply_markup=_menu_keyboard())
    except Exception as e:
        log.warning("Auto ad error: %s", e)

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    chat = update.effective_chat
    uid = update.effective_user.id if update.effective_user else 0
    if not (m and chat): return
    if not is_admin(uid):
        await m.reply_text("Solo admin.")
        return
    DATA["live_chats"][str(chat.id)] = True
    _save(DATA)
    await m.reply_text(_prices_text(), reply_markup=_menu_keyboard())

    # programa anuncios
    name = _job_name(chat.id)
    # Cancela si existe
    for j in context.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    context.job_queue.run_repeating(
        _auto_ad, interval=AUTO_MIN * 60, first=AUTO_MIN * 60,
        chat_id=chat.id, name=name
    )
    log.info("Live ON en %s; anuncios cada %s min", chat.id, AUTO_MIN)

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    chat = update.effective_chat
    uid = update.effective_user.id if update.effective_user else 0
    if not (m and chat): return
    if not is_admin(uid):
        await m.reply_text("Solo admin.")
        return
    DATA["live_chats"][str(chat.id)] = False
    _save(DATA)
    name = _job_name(chat.id)
    for j in context.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    await m.reply_text("🛑 Live OFF. Anuncios detenidos.")

# ---- Traducción ligera ----
try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if not TRANSLATE_TO or not GoogleTranslator:
        return
    # Evita bucles: no traduzcas mensajes del propio bot
    if update.effective_user and update.effective_user.is_bot:
        return
    try:
        tr = GoogleTranslator(source="auto", target=TRANSLATE_TO).translate(msg.text)
        if tr and tr.strip().lower() != msg.text.strip().lower():
            await msg.reply_text(f"🌍 {tr}")
    except Exception as e:
        log.debug("Translate error: %s", e)

# Handlers
application.add_handler(CommandHandler("start",   cmd_start))
application.add_handler(CommandHandler("whoami",  cmd_whoami))
application.add_handler(CommandHandler("addprice", cmd_addprice))
application.add_handler(CommandHandler("listprices", cmd_listprices))
application.add_handler(CommandHandler("liveon",  cmd_liveon))
application.add_handler(CommandHandler("liveoff", cmd_liveoff))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate_in_chat))

# -------------------- Run (Flask + Bot) --------------------
def _run_bot():
    # run_polling() es bloqueante; lo lanzamos en hilo aparte
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    log.info("Bot iniciando en Render…  ✅ Iniciando servidor Flask y bot…")
    t = threading.Thread(target=_run_bot, name="tg-bot", daemon=True)
    t.start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
