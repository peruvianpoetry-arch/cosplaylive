import os, json, threading, asyncio, logging
from flask import Flask, request, redirect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- ENV ----------
TOKEN   = os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID","0"))
ADMIN_ID= int(os.getenv("ADMIN_USER_ID","0"))
BASE_URL= os.getenv("BASE_URL","https://cosplaylive.onrender.com").rstrip("/")
CURRENCY= os.getenv("CURRENCY","EUR")
PORT    = int(os.getenv("PORT","10000"))
AUTO_MIN= int(os.getenv("AUTO_INTERVAL_MIN","10"))
LANG_TO = os.getenv("TRANSLATE_TO","de").strip() or "de"
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY","").strip()

DATA_FILE = "/var/data/data.json"

# ---------- LOG ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cosplaylive")

# ---------- PERSISTENCIA ----------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return {"prices":{}, "live": False}

def save_data(d):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE,"w",encoding="utf-8") as f: json.dump(d,f,ensure_ascii=False,indent=2)

data = load_data()
if "prices" not in data or not isinstance(data["prices"], dict):
    data["prices"] = {}
if "live" not in data: data["live"] = False

# ---------- TRADUCTOR ----------
def tr(txt, to=LANG_TO):
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target=to).translate(txt)
    except Exception as e:
        log.warning(f"translate fail: {e}")
        return txt

# ---------- FLASK ----------
app = Flask(__name__)

@app.get("/")
def health():
    return "✅ CosplayLive online"

@app.get("/donar")
def donar():
    amt = request.args.get("amt","0").replace(",",".")
    item = request.args.get("item","")
    try:
        val = float(amt)
    except:
        return "Monto inválido", 400

    # Stripe si hay clave, si no simulación
    if STRIPE_KEY:
        import stripe
        stripe.api_key = STRIPE_KEY
        price_cents = int(round(val*100))
        success = f"{BASE_URL}/ok?amt={price_cents/100:.2f}&item={item}"
        cancel  = f"{BASE_URL}/cancel"
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data":{
                    "currency": (CURRENCY or "eur").lower(),
                    "product_data":{"name": item or "Apoyo"},
                    "unit_amount": price_cents
                },
                "quantity":1
            }],
            success_url=success,
            cancel_url=cancel
        )
        return redirect(session.url, code=303)
    else:
        return f"OK simulación: {val:.2f} {CURRENCY} | Item: {item}"

@app.get("/ok")
def ok():
    amt = request.args.get("amt","0")
    item= request.args.get("item","")
    return f"✅ Pago recibido: {amt} {CURRENCY} | {item}"

@app.get("/cancel")
def cancel():
    return "❎ Pago cancelado"

# ---------- UTIL ----------
def only_emojis(s:str)->str:
    e = "".join(ch for ch in s if ord(ch)>1000)
    return e if e else "🔥"

def prices_keyboard():
    kb=[]
    for name, price in data["prices"].items():
        emo = only_emojis(name)
        url = f"{BASE_URL}/donar?amt={price}&item={emo}"
        kb.append([InlineKeyboardButton(f"{name} — {price}€", url=url)])
    return InlineKeyboardMarkup(kb) if kb else None

async def announce(appctx: ContextTypes.DEFAULT_TYPE):
    """Anunciador periódico mientras live=True"""
    if CHAT_ID == 0: return
    while True:
        try:
            if data.get("live") and data["prices"]:
                text = (
                    "🎥 *EN VIVO*\n"
                    "Apoya y elige una acción:\n"
                    + "\n".join([f"• {k} — *{v}€*" for k,v in data['prices'].items()])
                )
                await appctx.bot.send_message(
                    chat_id=CHAT_ID, text=text, parse_mode="Markdown",
                    reply_markup=prices_keyboard()
                )
        except Exception as e:
            log.error(f"announce error: {e}")
        await asyncio.sleep(max(60, AUTO_MIN*60))

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola\n"
        "• /addprice 🍑 Nombre 5  → agrega opción\n"
        "• /listprices            → lista\n"
        "• /liveon                → activa modo en vivo + anuncios\n"
        "• /liveoff               → desactiva modo en vivo\n"
        "• /whoami                → ver si eres admin\n"
        "• /setlang de|en|es      → idioma de traducción\n"
        "• /setinterval 10        → minutos entre anuncios"
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    await update.message.reply_text("✅ Eres admin" if uid==ADMIN_ID else f"❌ No admin (ID: {uid})")

async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Solo admin.")
        return
    raw = update.message.text.split(" ",1)[1].replace(",", " ").replace("€"," ").strip()
    parts = raw.rsplit(" ",1)
    if len(parts)<2:
        await update.message.reply_text("Formato: /addprice 🍑 Nombre 5")
        return
    name = parts[0].strip()
    try:
        price = float(parts[1])
    except:
        await update.message.reply_text("Precio inválido")
        return
    data["prices"][name]=price
    save_data(data)
    await update.message.reply_text(f"💰 Agregado: {name} → {price}€")

async def listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data["prices"]:
        await update.message.reply_text("No hay precios.")
        return
    msg = "💸 *Lista de precios*\n" + "\n".join([f"• {k} — *{v}€*" for k,v in data["prices"].items()])
    await update.message.reply_text(msg, parse_mode="Markdown")

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # activa live y muestra menú
    data["live"]=True
    save_data(data)
    if not data["prices"]:
        await update.message.reply_text("Agrega precios con /addprice.")
        return
    await update.message.reply_text("🎬 *Show en vivo* — elige tu opción:",
        parse_mode="Markdown", reply_markup=prices_keyboard())

async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data["live"]=False
    save_data(data)
    await update.message.reply_text("🛑 Live desactivado.")

async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Solo admin.")
        return
    global LANG_TO
    try:
        LANG_TO = update.message.text.split(" ",1)[1].strip().lower()
    except:
        pass
    await update.message.reply_text(f"🌐 Idioma de traducción: {LANG_TO}")

async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Solo admin.")
        return
    global AUTO_MIN
    try:
        AUTO_MIN = int(update.message.text.split(" ",1)[1].strip())
    except:
        await update.message.reply_text("Ej: /setinterval 10")
        return
    await update.message.reply_text(f"⏱️ Intervalo anuncios: {AUTO_MIN} min")

async def translate_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # solo en el chat configurado
    if update.effective_chat.id != CHAT_ID: return
    txt = update.message.text or ""
    if not txt.strip(): return
    translated = tr(txt, LANG_TO)
    flags = "🇩🇪" if LANG_TO=="de" else "🌐"
    try:
        await update.message.reply_text(f"{flags} {translated}")
    except Exception as e:
        log.warning(f"reply fail: {e}")

# ---------- TG APP ----------
if not TOKEN:
    raise SystemExit("Falta TELEGRAM_TOKEN")
application = ApplicationBuilder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("whoami", whoami))
application.add_handler(CommandHandler("addprice", addprice))
application.add_handler(CommandHandler("listprices", listprices))
application.add_handler(CommandHandler("liveon", liveon))
application.add_handler(CommandHandler("liveoff", liveoff))
application.add_handler(CommandHandler("setlang", setlang))
application.add_handler(CommandHandler("setinterval", setinterval))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_chat))
application.add_handler(CallbackQueryHandler(lambda *_: None, pattern=r"^noop$"))

# ---------- ARRANQUE ----------
def bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # lanza anunciador
    loop.create_task(announce(application))
    loop.run_until_complete(application.run_polling(stop_signals=None))

@app.before_first_request
def _spawn():
    t = threading.Thread(target=bot_thread, daemon=True, name="tg-bot")
    t.start()

if __name__ == "__main__":
    log.info("Bot+Flask iniciando…")
    app.run(host="0.0.0.0", port=PORT, debug=False)
