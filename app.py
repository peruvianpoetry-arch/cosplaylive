import os
import json
import threading
from typing import List, Dict, Optional
from uuid import uuid4

from flask import Flask, request
import stripe

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config / Helpers
# =========================
DATA_FILE = os.getenv("DATA_FILE", "/var/data/data.json")
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

def load_data() -> Dict:
    if not os.path.exists(DATA_FILE):
        return {"prices": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"prices": []}

def save_data(data: Dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def add_price(name: str, price: float, emoji: str = "💝"):
    data = load_data()
    data["prices"].append({
        "id": str(uuid4()),
        "name": name.strip(),
        "price": float(price),
        "emoji": emoji
    })
    save_data(data)

def reset_prices():
    save_data({"prices": []})

def list_prices() -> List[Dict]:
    return load_data().get("prices", [])

def admins() -> List[int]:
    raw = (os.getenv("ADMIN_USER_IDS") or os.getenv("ADMIN_USER_ID") or "").strip()
    ids = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except:
            pass
    return ids

def is_admin(uid: Optional[int]) -> bool:
    return uid is not None and uid in admins()

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID") or ""
PUBLIC_BASE = (os.getenv("PUBLIC_BASE") or os.getenv("BASE_URL") or "").rstrip("/")
CURRENCY = (os.getenv("CURRENCY") or "EUR").lower()
PORT = int(os.getenv("PORT", "10000"))

# Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

# =========================
# Flask part (payments)
# =========================
flask_app = Flask(__name__)

@flask_app.get("/")
def root():
    return "OK", 200

@flask_app.get("/donar")
def donate_redirect():
    """
    Crea sesión de Stripe Checkout. El texto explícito NO va a Stripe.
    En Checkout el nombre es neutro 'Tip X EUR'. Guardamos lo real en metadata.
    """
    amt_str = request.args.get("amt", "0")
    item_id = request.args.get("id", "")  # id del price en nuestra lista

    try:
        amount_eur = float(str(amt_str).replace(",", "."))
    except Exception:
        amount_eur = 0.0

    if amount_eur <= 0:
        # mini formulario para elegir monto libre
        return """
        <html><body>
        <h3>Elige un monto</h3>
        <form action="/donar" method="get">
          <input type="number" name="amt" min="1" step="1" value="5"/>
          <button type="submit">Continuar</button>
        </form>
        </body></html>
        """

    if not stripe.api_key:
        return "Stripe no configurado (falta STRIPE_SECRET_KEY).", 500

    product_name = f"Tip {amount_eur:.2f} {CURRENCY.upper()}"

    success_url = (PUBLIC_BASE + "/ok") if PUBLIC_BASE else "https://google.com"
    cancel_url = (PUBLIC_BASE + "/cancel") if PUBLIC_BASE else "https://google.com"

    # Recuperamos info visible (solo para metadata) si vino el id
    original_name = ""
    for p in list_prices():
        if p.get("id") == item_id:
            original_name = p.get("name", "")
            break

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": CURRENCY,
                    "product_data": {
                        "name": product_name,   # Seguro para recibo
                    },
                    "unit_amount": int(round(amount_eur * 100)),
                },
                "quantity": 1,
            }],
            success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
            metadata={
                "menu_item_name": original_name,     # PRIVADO (no se muestra en recibo)
                "amount_eur": str(amount_eur),
                "item_id": item_id or "",
            }
        )
        # redirigir al checkout
        return f'<meta http-equiv="refresh" content="0; url={session.url}"/>Redirigiendo a Stripe…'
    except Exception as e:
        return f"Error creando pago: {e}", 500

@flask_app.get("/ok")
def ok():
    return "✅ ¡Pago realizado! Gracias por el apoyo.", 200

@flask_app.get("/cancel")
def cancel():
    return "❎ Pago cancelado.", 200

def run_flask():
    # Flask en hilo aparte
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

# =========================
# Telegram bot
# =========================

def render_menu_header(prices: List[Dict]) -> str:
    """
    Texto visible arriba de los botones, con las PALABRAS reales.
    Ejemplo:
      • Titten – 5€
      • Muschi – 10€
    """
    if not prices:
        return "No hay precios. Usa /addprice Nombre · Precio para añadir."
    lines = ["📝 **Lista de opciones**"]
    for p in prices:
        lines.append(f"• {p['name']} – {int(p['price']) if p['price'].is_integer() else p['price']}€")
    return "\n".join(lines)

def build_menu_buttons(prices: List[Dict]) -> InlineKeyboardMarkup:
    """
    Botones: SOLO emoji + precio (no texto explícito).
    El URL incluye el id para guardar nombre real en metadata de Stripe.
    """
    rows = []
    base = PUBLIC_BASE
    for p in prices[:12]:
        label = f"{p.get('emoji','💝')} {int(p['price']) if float(p['price']).is_integer() else p['price']}€"
        if base:
            url = f"{base}/donar?amt={p['price']}&id={p['id']}"
            rows.append([InlineKeyboardButton(text=label, url=url)])
        else:
            rows.append([InlineKeyboardButton(text=label, callback_data="noop")])
    if base:
        rows.append([InlineKeyboardButton(text="✨ Donar libre", url=f"{base}/donar?amt=0")])
    else:
        rows.append([InlineKeyboardButton(text="✨ Donar libre", callback_data="noop")])
    return InlineKeyboardMarkup(rows)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola 👋\nUsa /addprice Nombre · Precio para añadir opciones.\n/livezona o /liveon para mostrar el menú."
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if is_admin(uid):
        await update.message.reply_text(f"✅ Eres admin (ID: {uid})")
    else:
        await update.message.reply_text("⛔ No eres admin.")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_admin(uid):
        await update.message.reply_text("Solo admin.")
        return
    args = update.message.text.split(" ", 1)
    if len(args) < 2:
        await update.message.reply_text("Usa: /addprice Nombre · Precio\nEj: /addprice Titten · 5")
        return
    body = args[1]
    if "·" in body:
        name, price = [x.strip() for x in body.split("·", 1)]
    elif "," in body:
        name, price = [x.strip() for x in body.split(",", 1)]
    else:
        await update.message.reply_text("Formato: Nombre · Precio")
        return
    try:
        price_val = float(price.replace(",", "."))
    except:
        await update.message.reply_text("Precio inválido.")
        return
    # emoji automático si el nombre incluye alguno, si no ponemos 💝
    emoji = "💝"
    add_price(name=name, price=price_val, emoji=emoji)
    await update.message.reply_text("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ps = list_prices()
    txt = render_menu_header(ps)
    await update.message.reply_markdown(txt)

async def cmd_resetprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_admin(uid):
        await update.message.reply_text("Solo admin.")
        return
    reset_prices()
    await update.message.reply_text("🧹 Precios borrados.")

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra el mensaje con:
      1) Palabras reales arriba (markdown)
      2) Botones con emoji+precio abajo
    Si TELEGRAM_CHAT_ID está definido, también lo envía al canal.
    """
    ps = list_prices()
    header = render_menu_header(ps)
    markup = build_menu_buttons(ps)

    # en privado
    await update.message.reply_markdown(header, reply_markup=markup)

    # opcional al canal
    if CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=int(CHANNEL_ID),
                text=header,
                reply_markup=markup,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except Exception as e:
            await update.message.reply_text(f"No pude publicar en el canal: {e}")

def run_bot():
    if not BOT_TOKEN:
        print("Falta TELEGRAM_TOKEN.")
        return
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("iamadmin", cmd_whoami))
    app.add_handler(CommandHandler("addprice", cmd_addprice))
    app.add_handler(CommandHandler("listprices", cmd_listprices))
    app.add_handler(CommandHandler("resetprices", cmd_resetprices))
    app.add_handler(CommandHandler("liveon", cmd_liveon))
    app.add_handler(CommandHandler("live", cmd_liveon))
    app.add_handler(CommandHandler("livezona", cmd_liveon))

    print("🤖 Bot iniciando en Render…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

# =========================
# Entrypoint
# =========================
if __name__ == "__main__":
    # levantar Flask en hilo secundario; bot en el hilo principal
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    run_bot()
