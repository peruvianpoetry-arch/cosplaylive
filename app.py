import os
import json
import threading
from pathlib import Path
from typing import List, Tuple, Optional

from flask import Flask, request, make_response
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ========== CONFIG ==========
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno.")

# Admins: comas o espacios. Ej: "2103408030,12345678"
ADMIN_USER_IDS = {x.strip() for x in (os.environ.get("ADMIN_USER_IDS") or "").replace(" ", ",").split(",") if x.strip()}

# Canal opcional (para publicar menú/donaciones)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
try:
    TELEGRAM_CHAT_ID = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else None
except Exception:
    TELEGRAM_CHAT_ID = None

# Moneda y base pública para botones
CURRENCY = os.environ.get("CURRENCY", "EUR")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE") or os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
if PUBLIC_BASE:
    PUBLIC_BASE = PUBLIC_BASE.rstrip("/")
else:
    PUBLIC_BASE = "https://example.com"  # fallback (evitar None en botones)

# Carpeta para datos persistentes (Render Starter con Disk montado)
DATA_DIR = Path(os.environ.get("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRICES_FILE = DATA_DIR / "prices.json"

# ========== PERSISTENCIA ==========
def load_prices() -> List[Tuple[str, float]]:
    if not PRICES_FILE.exists():
        return []
    try:
        raw = json.loads(PRICES_FILE.read_text("utf-8"))
        out: List[Tuple[str, float]] = []
        for item in raw:
            name = str(item.get("name", "")).strip()
            price = float(item.get("price", 0))
            if name and price > 0:
                out.append((name, price))
        return out
    except Exception:
        return []

def save_prices(items: List[Tuple[str, float]]) -> None:
    data = [{"name": n, "price": p} for (n, p) in items]
    PRICES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

# ========== UTILS ==========
def is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    return str(user_id) in ADMIN_USER_IDS

def parse_addprice_arg(text: str) -> Tuple[str, float]:
    """
    Acepta:
      - 'Nombre · 10'
      - 'Nombre, 10'
      - 'Nombre 10'
    Devuelve (name, price)
    """
    t = text.strip()
    # separadores comunes
    for sep in ["·", ","]:
        if sep in t:
            left, right = t.split(sep, 1)
            return left.strip(), float(right.strip().replace(",", "."))
    # ultimo token como número
    parts = t.split()
    price = float(parts[-1].replace(",", "."))
    name = " ".join(parts[:-1]).strip()
    return name, price

def prices_text_and_markup(prices: List[Tuple[str, float]], currency: str):
    """
    Devuelve:
      - texto tipo lista "• Nombre – 10€"
      - InlineKeyboardMarkup con botones de solo monto (💝 10 €)
    """
    if not prices:
        text = "⚠️ No hay precios cargados. Usa /addprice Nombre · Precio"
        return text, None

    lines = ["📝 Lista de opciones"]
    buttons = []
    for name, price in prices:
        lines.append(f"• {name} – {int(price)}{currency}")
        buttons.append(
            [InlineKeyboardButton(
                text=f"💝 {int(price)} {currency}",
                url=f"{PUBLIC_BASE}/donar?amt={int(price)}&item={name}"
            )]
        )
    buttons.append([InlineKeyboardButton("✨ Donar libre", url=f"{PUBLIC_BASE}/donar")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)

# ========== TELEGRAM BOT ==========
application = Application.builder().token(TOKEN).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Hola 👋\n"
        "Comandos:\n"
        "• /whoami – verificar admin\n"
        "• /addprice Nombre · Precio – agregar opción\n"
        "• /listprices – ver opciones\n"
        "• /menu (alias /liveon, /livezona) – publicar menú con botones\n"
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if is_admin(uid):
        await update.effective_message.reply_text(f"✅ Eres admin (ID: {uid})")
    else:
        await update.effective_message.reply_text("Solo admin.")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_admin(uid):
        await update.effective_message.reply_text("Solo admin.")
        return

    arg = (update.effective_message.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await update.effective_message.reply_text("Usa: /addprice Nombre · Precio")
        return
    try:
        name, price = parse_addprice_arg(arg[1])
        if not name or price <= 0:
            raise ValueError("Valores inválidos")
        items = load_prices()
        items.append((name, price))
        save_prices(items)
        await update.effective_message.reply_text("💰 Precio agregado correctamente.")
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Error: {e}\nFormato: /addprice Nombre · 10")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = load_prices()
    if not items:
        await update.effective_message.reply_text("No hay precios. Usa /addprice")
        return
    lines = ["💵 Opciones actuales:"]
    for n, p in items:
        lines.append(f"• {n} – {int(p)}{CURRENCY}")
    await update.effective_message.reply_text("\n".join(lines))

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_admin(uid):
        await update.effective_message.reply_text("Solo admin.")
        return
    try:
        prices = load_prices()
        text, markup = prices_text_and_markup(prices, CURRENCY)
        # Siempre responde en el chat actual
        await update.effective_message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
        # Si hay canal, intenta publicar también allí (no romper si falla)
        if TELEGRAM_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(TELEGRAM_CHAT_ID),
                    text=text,
                    reply_markup=markup,
                    disable_web_page_preview=True
                )
            except Exception as e:
                print(f"[WARN] No se pudo publicar en canal: {e}")
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Error al generar menú: {e}")

# Handlers
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("whoami", cmd_whoami))
application.add_handler(CommandHandler("addprice", cmd_addprice))
application.add_handler(CommandHandler("listprices", cmd_listprices))
application.add_handler(CommandHandler(["menu", "liveon", "livezona"], cmd_menu))

# ========== FLASK APP ==========
app = Flask(__name__)

@app.get("/")
def root():
    return "OK", 200

@app.get("/donar")
def donar():
    """
    Simulación de pago.
    ?amt=10&item=Nombre
    Manda un aviso al canal/chat y muestra confirmación simple en HTML.
    """
    amt = request.args.get("amt")
    item = request.args.get("item", "Donación")
    try:
        amt_int = int(float(amt)) if amt else None
    except Exception:
        amt_int = None

    # Aviso en Telegram (no bloquear si falla)
    try:
        msg = f"✅ Donación simulada: {amt_int} {CURRENCY}"
        if item:
            msg += f" | Item: {item}"
        # Preferir canal si está configurado; si no, al primer admin por privado
        target = TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID else None
        if target:
            application.bot.send_message(chat_id=target, text=msg)
        else:
            # Enviar al primer admin disponible
            if ADMIN_USER_IDS:
                any_admin = next(iter(ADMIN_USER_IDS))
                application.bot.send_message(chat_id=int(any_admin), text=msg)
    except Exception as e:
        print(f"[WARN] No se pudo avisar en Telegram: {e}")

    html = f"OK, simulación de donación recibida. Monto: {amt_int} {CURRENCY} | Item: {item}"
    resp = make_response(html, 200)
    resp.mimetype = "text/html; charset=utf-8"
    return resp

# ========== ARRANQUE ==========
def start_bot():
    # Evita registrar signal-handlers en thread (error set_wakeup_fd).
    application.run_polling(stop_signals=None, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Iniciar bot en hilo aparte
    t = threading.Thread(target=start_bot, daemon=True, name="tg-bot")
    t.start()

    port = int(os.environ.get("PORT", "10000"))
    print("🤖 Bot iniciando en Render… ✅ Iniciando servidor Flask y bot…")
    # Flask en hilo principal
    app.run(host="0.0.0.0", port=port, debug=False)
