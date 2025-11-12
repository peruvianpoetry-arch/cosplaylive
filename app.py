import os
import json
import re
import html
from urllib.parse import urlencode
from datetime import timedelta

from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters,
)

# ======================
# Config desde variables
# ======================
BOT_TOKEN        = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_USER_IDS   = os.environ.get("ADMIN_USER_IDS") or os.environ.get("ADMIN_USER_ID")
CHANNEL_ID       = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHANNEL_ID")
PUBLIC_BASE      = os.environ.get("PUBLIC_BASE") or os.environ.get("BASE_URL") or "https://cosplaylive.onrender.com"
CURRENCY         = os.environ.get("CURRENCY", "EUR")
TRANSLATE_TO     = os.environ.get("TRANSLATE_TO", "de")
AUTO_INTERVAL_MIN= int(os.environ.get("AUTO_INTERVAL_MIN", "10"))

# Stripe (modo test/real según lo que puse en Render)
import stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

DATA_FILE = "/var/data/data.json"
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"prices": {}, "live_on": False}, f)

def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def is_admin(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        return False
    ids = [x.strip() for x in re.split(r"[,\s]+", ADMIN_USER_IDS) if x.strip()]
    return str(user_id) in ids

# ======================
# Flask app (donar)
# ======================
app = Flask(__name__)

@app.get("/donar")
def donar():
    """
    Endpoint que recibe ?amt= y ?name= (ASCII/URL-safe) y opcionalmente ?label (con emojis) para mostrar
    """
    amount_eur = request.args.get("amt", "")
    name_ascii = request.args.get("name", "")   # limpio para Stripe URL
    label = request.args.get("label", "")       # solo para mostrar (puede traer emojis)

    # Normaliza monto
    try:
        amt = float(str(amount_eur).replace(",", "."))
    except:
        return "Monto inválido", 400

    # Si no hay Stripe, simula OK (útil en pruebas)
    if not stripe.api_key:
        return f"OK, simulación de donación recibida. Monto: {amt:.2f} {CURRENCY} | Item: {html.escape(label or name_ascii)}"

    # Crea Checkout
    try:
        cents = int(round(amt * 100))
        desc = f"Apoyo al show · {label or name_ascii}"
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": CURRENCY.lower(),
                    "product_data": {"name": name_ascii or "Support"},
                    "unit_amount": cents
                },
                "quantity": 1
            }],
            success_url=f"{PUBLIC_BASE}/ok",
            cancel_url=f"{PUBLIC_BASE}/cancel"
        )
        # Redirección simple
        return f'<meta http-equiv="refresh" content="0; url={session.url}"/>', 302
    except Exception as e:
        return f"Stripe error: {html.escape(str(e))}", 500

@app.get("/ok")
def ok():
    return "✅ Pago completado. ¡Gracias por el apoyo!"

@app.get("/cancel")
def cancel():
    return "❌ Pago cancelado."


# ======================
# Telegram BOT
# ======================
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    await update.effective_chat.send_message(f"✅ Eres admin (ID: {uid})" if is_admin(uid) else f"Tu ID: {uid}")

def _menu_text(prices: dict) -> str:
    if not prices:
        return "Aún no hay precios configurados. Usa /addprice Nombre, 10"
    lines = [f"🎬 *Menú del show*"]
    for k, v in prices.items():
        lines.append(f"• {k} — {float(v):.2f} {CURRENCY}")
    lines.append("\nPulsa un botón para apoyar al show 🔥")
    return "\n".join(lines)

def _menu_keyboard(prices: dict) -> InlineKeyboardMarkup:
    rows = []
    for k, v in prices.items():
        # Mantén ‘label’ con emojis para mostrar; ‘name’ ASCII-safe para Stripe
        safe_name = re.sub(r"[^\w\-\. ]", "", k).strip() or "Item"
        qs = urlencode({"amt": f"{float(v):.2f}", "name": safe_name, "label": k})
        url = f"{PUBLIC_BASE}/donar?{qs}"
        rows.append([InlineKeyboardButton(f"{k} · {float(v):.2f} {CURRENCY}", url=url)])
    return InlineKeyboardMarkup(rows)

async def live_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    d = load_data()
    prices = d.get("prices", {})
    await context.bot.send_message(
        chat_id=chat_id,
        text=_menu_text(prices),
        parse_mode="Markdown",
        reply_markup=_menu_keyboard(prices)
    )

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return
    d = load_data()
    d["live_on"] = True
    save_data(d)

    # 1) Publica menú ahora
    await live_menu(context, update.effective_chat.id)

    # 2) Programa anuncios si hay JobQueue
    jq = context.job_queue
    if jq is None:
        await update.effective_chat.send_message(
            "⚠️ Anuncios automáticos desactivados (JobQueue no disponible). Instala `python-telegram-bot[job-queue]` en requirements."
        )
        return

    # Evita duplicados por si ya había job
    if "auto_job" in context.chat_data and context.chat_data["auto_job"]:
        try:
            context.chat_data["auto_job"].schedule_removal()
        except Exception:
            pass

    job = jq.run_repeating(
        lambda c: live_menu(c, update.effective_chat.id),
        interval=timedelta(minutes=AUTO_INTERVAL_MIN),
        name=f"auto_ads_{update.effective_chat.id}"
    )
    context.chat_data["auto_job"] = job
    await update.effective_chat.send_message(f"🟢 Live ON. Anuncios cada {AUTO_INTERVAL_MIN} min.")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return
    d = load_data()
    d["live_on"] = False
    save_data(d)

    job = context.chat_data.get("auto_job")
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass
        context.chat_data["auto_job"] = None

    await update.effective_chat.send_message("🔴 Live OFF. Anuncios detenidos.")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return
    # acepta: /addprice Nombre, 10
    txt = (update.message.text or "").strip()
    m = re.match(r"^/addprice\s+(.+?),\s*([0-9]+(?:[.,][0-9]+)?)$", txt, re.I)
    if not m:
        await update.effective_chat.send_message("Formato incorrecto. Usa: /addprice 🍑 Nombre 5€  -> `/addprice Nombre, 5`")
        return
    name = m.group(1).strip()
    price = float(m.group(2).replace(",", "."))
    d = load_data()
    d.setdefault("prices", {})[name] = price
    save_data(d)
    await update.effective_chat.send_message("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    await update.effective_chat.send_message(_menu_text(d.get("prices", {})), parse_mode="Markdown")

# Traducción simple (evita fallar en mensajes de botones)
from deep_translator import GoogleTranslator
translator = GoogleTranslator(source="auto", target=TRANSLATE_TO)

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    d = load_data()
    if not d.get("live_on"):
        return
    try:
        t = translator.translate(msg.text)
        if t and t.strip() and t.strip().lower() != msg.text.strip().lower():
            await msg.reply_text(f"🌍 {t}")
    except Exception:
        # no rompas el bot por traducción
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("Hola 👋\nUsa /addprice Nombre, 10 para añadir opciones.\n/liveon o /liveoff para controlar anuncios.\n/whoami para verificar admin.")

def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("addprice", cmd_addprice))
    application.add_handler(CommandHandler("listprices", cmd_listprices))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_in_chat))
    return application

# ============
# Arranque
# ============
def run():
    application = build_application()
    # polling en hilo; Flask sirve HTTP
    import threading
    t = threading.Thread(target=lambda: application.run_polling(drop_pending_updates=True), daemon=True)
    t.start()

    port = int(os.environ.get("PORT", "10000"))
    print("Bot iniciando en Render… ✅ Iniciando servidor Flask y bot…")
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    run()
