import os, json, threading, asyncio
from pathlib import Path
from typing import Dict, List, Tuple

import telegram  # para CallbackQueryHandler
from flask import Flask, jsonify, request
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

# =======================
# ENV & CONSTANTES
# =======================
BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID       = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHANNEL_ID")
ADMIN_USER_ID    = int(os.getenv("ADMIN_USER_ID", "0"))
TRANSLATE_TO     = os.getenv("TRANSLATE_TO", "de")
DATA_DIR         = Path(os.getenv("DATA_DIR", "/var/data"))
PUBLIC_BASE      = os.getenv("PUBLIC_BASE") or os.getenv("BASE_URL") or ""
PORT             = int(os.getenv("PORT", "10000"))

assert BOT_TOKEN, "Falta TELEGRAM_BOT_TOKEN"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PRICES_PATH = DATA_DIR / "prices.json"

# =======================
# UTIL PRECIOS
# =======================
def load_prices() -> List[Dict]:
    if PRICES_PATH.exists():
        try:
            return json.loads(PRICES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_prices(prices: List[Dict]) -> None:
    PRICES_PATH.write_text(json.dumps(prices, ensure_ascii=False, indent=2), encoding="utf-8")

def parse_price_args(args: List[str]) -> Tuple[str, float]:
    if not args or len(args) < 2:
        raise ValueError("Usa: /addprice Nombre 12.5")
    *name_parts, last = args
    name = " ".join(name_parts).strip()
    price = float(last.replace("€", "").replace(",", "."))
    return name, price

def build_menu_buttons(prices: List[Dict]) -> InlineKeyboardMarkup:
    rows = []
    base = PUBLIC_BASE.rstrip("/")
    for p in prices[:12]:
        label = f"{p['name']} · {p['price']} EUR"
        if base:
            url = f"{base}/donar?amt={p['price']}&item={p['name']}"
            rows.append([InlineKeyboardButton(text=label, url=url)])
        else:
            rows.append([InlineKeyboardButton(text=label, callback_data=f"noop:{p['name']}")])
    if base:
        rows.append([InlineKeyboardButton(text="💝 Donar libre", url=f"{base}/donar?amt=0")])
    else:
        rows.append([InlineKeyboardButton(text="💝 Donar libre", callback_data="noop:free")])
    return InlineKeyboardMarkup(rows)

# =======================
# HANDLERS
# =======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hola, soy CosplayLive Bot. Usa /whoami, /addprice, /listprices, /liveon.")

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_USER_ID:
        await update.message.reply_text(f"✅ Eres admin (ID: {uid})")
    else:
        await update.message.reply_text(f"🚫 No eres admin (tu ID: {uid})")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Solo admin.")
        return
    try:
        name, price = parse_price_args(context.args)
    except Exception as e:
        await update.message.reply_text(f"Usa: /addprice Nombre 12.5\nDetalle: {e}")
        return
    prices = load_prices()
    prices.append({"name": name, "price": price})
    save_prices(prices)
    await update.message.reply_text("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prices = load_prices()
    if not prices:
        await update.message.reply_text("Aún no hay precios.")
        return
    text = "💶 *Lista de precios:*\n" + "\n".join(
        [f"• {p['name']} — {p['price']} EUR" for p in prices]
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_resetprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Solo admin.")
        return
    save_prices([])
    await update.message.reply_text("🧹 Precios borrados.")

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Solo admin.")
        return
    prices = load_prices()
    kb = build_menu_buttons(prices)
    if not CHANNEL_ID:
        await update.message.reply_text("📣 LIVE activado.", reply_markup=kb)
        return
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text="📣 *Cosplay Emma LIVE* — Apoya y aparece en pantalla.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
        await update.message.reply_text("🟢 LIVE activado (mensaje enviado al canal).")
    except Exception as e:
        await update.message.reply_text(f"No pude publicar en el canal: {e}")

async def noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer("Usa los botones cuando haya enlace activo.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        txt = update.message.text.strip().lower()
        if txt in ("hola", "hello"):
            await update.message.reply_text("👋 Hola")

# =======================
# BOOT BOT (HILO)
# =======================
application = ApplicationBuilder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("whoami", cmd_whoami))
application.add_handler(CommandHandler("addprice", cmd_addprice))
application.add_handler(CommandHandler("listprices", cmd_listprices))
application.add_handler(CommandHandler("resetprices", cmd_resetprices))
application.add_handler(CommandHandler("liveon", cmd_liveon))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
application.add_handler(MessageHandler(filters.StatusUpdate.ALL, on_text))
application.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop:"))

def start_bot_in_thread():
    print("🤖 Bot iniciando en Render...")
    loop = asyncio.new_event_loop()          # ✅ crea loop nuevo
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.create_task(application.start())
    loop.create_task(application.updater.start_polling())
    loop.run_forever()

# =======================
# FLASK
# =======================
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify(ok=True, live=True)

@app.get("/donar")
def donate_redirect():
    amt = request.args.get("amt", "0")
    item = request.args.get("item", "")
    return f"OK, simulación de donación recibida. Monto: {amt} EUR | Item: {item}"

# =======================
# MAIN
# =======================
if __name__ == "__main__":
    t = threading.Thread(target=start_bot_in_thread, name="start_polling", daemon=True)
    t.start()
    print("✅ Iniciando servidor Flask y bot...")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
