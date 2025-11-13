import os, json, threading, html, urllib.parse, logging
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional

from flask import Flask, redirect, request, abort
import stripe

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters, CallbackQueryHandler
)
from deep_translator import GoogleTranslator

# -------------------------
# Config
# -------------------------
PORT               = int(os.getenv("PORT", "10000"))
PUBLIC_BASE        = os.getenv("PUBLIC_BASE", "").rstrip("/")
BASE_URL           = os.getenv("BASE_URL", PUBLIC_BASE).rstrip("/")
DATA_DIR           = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE          = DATA_DIR / "data.json"

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")  # chat del LIVE
ADMIN_USER_ID      = os.getenv("ADMIN_USER_ID", os.getenv("ADMIN_USER_IDS","")).strip()
AUTO_INTERVAL_MIN  = int(os.getenv("AUTO_INTERVAL_MIN", "10"))
TRANSLATE_TO       = os.getenv("TRANSLATE_TO", "de")
CURRENCY           = os.getenv("CURRENCY", "EUR")

stripe.api_key     = os.getenv("STRIPE_SECRET_KEY","").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s"
)
log = logging.getLogger("cosplaylive")

# -------------------------
# Persistencia sencilla
# -------------------------
def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"live": False, "prices": {}, "welcome": "Hola 👋 ¡Bienvenidos al show!"}

def save_data(d: Dict[str, Any]):
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

data = load_data()

# -------------------------
# Utilidades
# -------------------------
def is_admin(update: Update) -> bool:
    uid = str(update.effective_user.id) if update.effective_user else ""
    return ADMIN_USER_ID and uid == ADMIN_USER_ID

def require_admin() -> str:
    return "Solo admin."

def safe_stripe_name(label: str) -> str:
    """
    Nombre que va a Stripe (sin emojis, ASCII).
    """
    base = label.encode("ascii", errors="ignore").decode().strip()
    return base or "Apoyo"

def prices_menu_text() -> str:
    if not data["prices"]:
        return "Aún no hay opciones. Usa /addprice Nombre, Precio"
    lines = ["🎬 *Menú del show*"]
    for _, item in data["prices"].items():
        lines.append(f"• {item['label']} — *{float(item['amount']):.2f} {CURRENCY}*")
    lines.append("\nPulsa un botón para apoyar al show 🔥")
    return "\n".join(lines)

def build_price_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for pid, item in data["prices"].items():
        label = f"{item['label']} · {float(item['amount']):.2f} {CURRENCY}"
        url = f"{BASE_URL}/donar?id={urllib.parse.quote(pid)}"
        rows.append([InlineKeyboardButton(text=label, url=url)])
    return InlineKeyboardMarkup(rows)

def translator_safe(text: str, to: str) -> Optional[str]:
    try:
        t = GoogleTranslator(source="auto", target=to)
        out = t.translate(text)
        if isinstance(out, str) and out.strip():
            return out.strip()
    except Exception as e:
        log.warning("Translator error: %s", e)
    return None

# -------------------------
# Telegram bot
# -------------------------
application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_chat.send_message("Usa /liveon para ver el menú (solo admin).")
        return
    await update.effective_chat.send_message(
        "Hola 👋\nUsa /addprice _Nombre_, _Precio_ para añadir opciones.\n`/liveon` para mostrar el menú.\n`/liveoff` para parar anuncios.",
        parse_mode="Markdown"
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(f"✅ Eres admin (ID: {ADMIN_USER_ID})" if is_admin(update) else "No eres admin.")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_chat.send_message(require_admin()); return
    # Espera: /addprice Nombre, Precio
    text = (update.message.text or "").split(" ", 1)
    if len(text) < 2 or "," not in text[1]:
        await update.effective_chat.send_message("Formato incorrecto. Usa: /addprice 🍑 Nombre, 5€")
        return
    name_part, price_part = [p.strip() for p in text[1].split(",", 1)]
    # limpiar moneda y comas
    amount_str = "".join(ch for ch in price_part if (ch.isdigit() or ch=="." or ch=="," ))
    amount = float(amount_str.replace(",", ".")) if amount_str else 0.0
    if amount <= 0:
        await update.effective_chat.send_message("Precio inválido.")
        return
    pid = name_part.lower().strip().replace(" ", "-")
    data["prices"][pid] = {"label": name_part, "amount": amount}
    save_data(data)
    await update.effective_chat.send_message("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_chat.send_message(require_admin()); return
    await update.effective_chat.send_message(prices_menu_text(), parse_mode="Markdown")

async def auto_ads(context: ContextTypes.DEFAULT_TYPE):
    if not data.get("live"): 
        return
    chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else None
    if not chat_id:
        return
    try:
        await context.bot.send_message(
            chat_id,
            text=prices_menu_text(),
            parse_mode="Markdown",
            reply_markup=build_price_keyboard()
        )
    except Exception as e:
        log.error("auto_ads error: %s", e)

def cancel_ads(context: ContextTypes.DEFAULT_TYPE):
    for job in context.job_queue.get_jobs_by_name(f"auto_ads_{TELEGRAM_CHAT_ID or 'chat'}"):
        job.schedule_removal()

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_chat.send_message(require_admin()); return
    data["live"] = True; save_data(data)

    # Mensaje de bienvenida + menú
    await update.effective_chat.send_message(
        data.get("welcome") or "¡Bienvenidos al show! 🥳",
        parse_mode="Markdown"
    )
    await update.effective_chat.send_message(
        prices_menu_text(), parse_mode="Markdown", reply_markup=build_price_keyboard()
    )

    # Programar anuncios
    cancel_ads(context)
    context.job_queue.run_repeating(
        auto_ads,
        interval=AUTO_INTERVAL_MIN * 60,
        name=f"auto_ads_{TELEGRAM_CHAT_ID or 'chat'}",
        first=AUTO_INTERVAL_MIN * 60,
    )
    await update.effective_chat.send_message("🔔 Anuncios automáticos activados.")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_chat.send_message(require_admin()); return
    data["live"] = False; save_data(data)
    cancel_ads(context)
    await update.effective_chat.send_message("🛑 Live OFF. Anuncios detenidos.")

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sólo cuando live está ON y en el chat configurado
    if not data.get("live"):
        return
    if not TELEGRAM_CHAT_ID or str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    txt = (update.message.text or "").strip()
    if not txt:
        return
    # Evitar traducir comandos
    if txt.startswith("/"):
        return
    translated = translator_safe(txt, TRANSLATE_TO)
    if translated and translated.lower() != txt.lower():
        await update.effective_chat.send_message(f"🌐 {translated}")

# evitar excepción roja sin handler
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Handler error: %s", context.error)

# Handlers
application.add_handler(CommandHandler("start",     cmd_start))
application.add_handler(CommandHandler("whoami",    cmd_whoami))
application.add_handler(CommandHandler("addprice",  cmd_addprice))
application.add_handler(CommandHandler("listprices",cmd_listprices))
application.add_handler(CommandHandler("liveon",    cmd_liveon))
application.add_handler(CommandHandler("liveoff",   cmd_liveoff))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate_in_chat))
application.add_error_handler(on_error)

# -------------------------
# Flask (Stripe + salud)
# -------------------------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/healthz")
def healthz():
    return "ok"

@app.get("/donar")
def donar():
    """
    Recibe ?id=<price_id> y crea Checkout Session.
    Redirige 302 al URL de Stripe.
    """
    pid = request.args.get("id", "").strip()
    if not pid or pid not in data["prices"]:
        abort(400, "Opción inválida.")
    item = data["prices"][pid]
    amount = int(round(float(item["amount"]) * 100))
    label  = item["label"]
    stripe_name = safe_stripe_name(label)

    if not stripe.api_key:
        # modo demo: simula OK
        return f"OK, simulación de donación recibida. Monto: {item['amount']} {CURRENCY} | Item: {label}"

    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": CURRENCY.lower(),
                    "product_data": {"name": stripe_name},
                    "unit_amount": amount,
                },
                "quantity": 1,
            }],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"display_label": label}
        )
        # redirección directa a Stripe
        return redirect(checkout.url, code=303)
    except Exception as e:
        log.exception("Stripe error")
        abort(500, f"Stripe error: {e}")

@app.get("/ok")
def ok(): return "Gracias por tu apoyo ✅"
@app.get("/cancel")
def cancel(): return "Pago cancelado."

# -------------------------
# Arranque combinado
# -------------------------
def start_bot():
    # run_polling dentro de hilo aparte
    log.info("Iniciando bot en Render… ✅")
    application.run_polling(stop_signals=None, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    t = threading.Thread(target=start_bot, name="tg-bot", daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
