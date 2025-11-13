import os, json, threading, logging, urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional

from flask import Flask, redirect, request, abort
import stripe

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from deep_translator import GoogleTranslator

# ---------- Config ----------
PORT               = int(os.getenv("PORT", "10000"))
PUBLIC_BASE        = os.getenv("PUBLIC_BASE", "").rstrip("/")
BASE_URL           = os.getenv("BASE_URL", PUBLIC_BASE).rstrip("/")
DATA_DIR           = Path(os.getenv("DATA_DIR", "/var/data")); DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE          = DATA_DIR / "data.json"

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")  # chat del LIVE
ADMIN_USER_ID      = os.getenv("ADMIN_USER_ID", "").strip()
AUTO_INTERVAL_MIN  = int(os.getenv("AUTO_INTERVAL_MIN", "5"))   # ← por defecto 5 min
TRANSLATE_TO       = os.getenv("TRANSLATE_TO", "de")
CURRENCY           = os.getenv("CURRENCY", "EUR")

stripe.api_key     = os.getenv("STRIPE_SECRET_KEY", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("cosplaylive")

# ---------- Estado persistente ----------
def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"live": False, "prices": {}, "welcome": "Hola 👋 ¡Bienvenidos al show!"}

def save_data(d: Dict[str, Any]): DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
data = load_data()

# ---------- Utils ----------
def is_admin(update: Update) -> bool:
    return ADMIN_USER_ID and update.effective_user and str(update.effective_user.id) == ADMIN_USER_ID

def safe_stripe_name(label: str) -> str:
    return label.encode("ascii", "ignore").decode().strip() or "Apoyo"

def prices_menu_text() -> str:
    if not data["prices"]: return "Aún no hay opciones. Usa /addprice Nombre, Precio"
    lines = ["🎬 *Menú del show*"]
    for it in data["prices"].values():
        lines.append(f"• {it['label']} — *{float(it['amount']):.2f} {CURRENCY}*")
    lines.append("\nPulsa un botón para apoyar al show 🔥")
    return "\n".join(lines)

def build_price_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for pid, it in data["prices"].items():
        label = f"{it['label']} · {float(it['amount']):.2f} {CURRENCY}"
        url = f"{BASE_URL}/donar?id={urllib.parse.quote(pid)}"
        rows.append([InlineKeyboardButton(text=label, url=url)])
    return InlineKeyboardMarkup(rows)

def translator_safe(text: str, to: str) -> Optional[str]:
    try:
        out = GoogleTranslator(source="auto", target=to).translate(text)
        return out.strip() if isinstance(out, str) else None
    except Exception as e:
        log.warning("Translator error: %s", e); return None

# ---------- Telegram ----------
application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_chat.send_message("Usa /liveon para ver el menú (solo admin)."); return
    await update.effective_chat.send_message(
        "Hola 👋\n/addprice Nombre, Precio\n/liveon para mostrar el menú\n/liveoff para parar anuncios.",
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("✅ Eres admin (ID: %s)" % ADMIN_USER_ID if is_admin(update) else "No eres admin.")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_chat.send_message("Solo admin."); return
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or "," not in parts[1]:
        await update.effective_chat.send_message("Formato incorrecto. Usa: /addprice 🍑 Nombre, 5"); return
    name_part, price_part = [p.strip() for p in parts[1].split(",", 1)]
    amount_str = "".join(ch for ch in price_part if (ch.isdigit() or ch in ".,"))
    amount = float(amount_str.replace(",", ".")) if amount_str else 0.0
    if amount <= 0: await update.effective_chat.send_message("Precio inválido."); return
    pid = name_part.lower().strip().replace(" ", "-")
    data["prices"][pid] = {"label": name_part, "amount": amount}; save_data(data)
    await update.effective_chat.send_message("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await update.effective_chat.send_message("Solo admin."); return
    await update.effective_chat.send_message(prices_menu_text(), parse_mode="Markdown")

async def auto_ads(context: ContextTypes.DEFAULT_TYPE):
    if not data.get("live"): return
    if not TELEGRAM_CHAT_ID: return
    try:
        await context.bot.send_message(
            int(TELEGRAM_CHAT_ID),
            text=prices_menu_text(),
            parse_mode="Markdown",
            reply_markup=build_price_keyboard(),
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error("auto_ads error: %s", e)

def cancel_ads(context: ContextTypes.DEFAULT_TYPE):
    for job in context.job_queue.get_jobs_by_name(f"auto_ads_{TELEGRAM_CHAT_ID or 'chat'}"):
        job.schedule_removal()

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await update.effective_chat.send_message("Solo admin."); return
    data["live"] = True; save_data(data)
    await update.effective_chat.send_message(
        data.get("welcome") or "¡Bienvenidos al show! 🥳", parse_mode="Markdown"
    )
    await update.effective_chat.send_message(
        prices_menu_text(), parse_mode="Markdown", reply_markup=build_price_keyboard()
    )
    cancel_ads(context)
    context.job_queue.run_repeating(
        auto_ads,
        interval=AUTO_INTERVAL_MIN * 60,
        name=f"auto_ads_{TELEGRAM_CHAT_ID or 'chat'}",
        first=AUTO_INTERVAL_MIN * 60,
    )
    await update.effective_chat.send_message("🔔 Anuncios automáticos activados.")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await update.effective_chat.send_message("Solo admin."); return
    data["live"] = False; save_data(data)
    cancel_ads(context)
    await update.effective_chat.send_message("🛑 Live OFF. Anuncios detenidos.")

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data.get("live"): return
    if not update.message or not update.message.text: return
    if not TELEGRAM_CHAT_ID or str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    txt = update.message.text.strip()
    if not txt or txt.startswith("/"): return
    out = translator_safe(txt, TRANSLATE_TO)
    if out and out.lower() != txt.lower():
        await update.effective_chat.send_message(f"🌐 {out}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Handler error: %s", context.error)

application.add_handler(CommandHandler("start",     cmd_start))
application.add_handler(CommandHandler("whoami",    cmd_whoami))
application.add_handler(CommandHandler("addprice",  cmd_addprice))
application.add_handler(CommandHandler("listprices",cmd_listprices))
application.add_handler(CommandHandler("liveon",    cmd_liveon))
application.add_handler(CommandHandler("liveoff",   cmd_liveoff))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate_in_chat))
application.add_error_handler(on_error)

# ---------- Flask (Stripe / health) ----------
app = Flask(__name__)

@app.get("/")
def root(): return "OK"

@app.get("/healthz")
def healthz(): return "ok"

@app.get("/donar")
def donar():
    pid = request.args.get("id","").strip()
    if not pid or pid not in data["prices"]: abort(400, "Opción inválida.")
    item   = data["prices"][pid]
    amount = int(round(float(item["amount"]) * 100))
    label  = item["label"]
    if not stripe.api_key:
        return f"OK, simulación de donación recibida. Monto: {item['amount']} {CURRENCY} | Item: {label}"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": CURRENCY.lower(),
                    "product_data": {"name": safe_stripe_name(label)},
                    "unit_amount": amount
                },
                "quantity": 1
            }],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"display_label": label}
        )
        return redirect(session.url, code=303)
    except Exception as e:
        log.exception("Stripe error"); abort(500, f"Stripe error: {e}")

@app.get("/ok")
def ok(): return "Gracias por tu apoyo ✅"

@app.get("/cancel")
def cancel(): return "Pago cancelado."

# ---------- Arranque ----------
def start_flask():
    log.info("Iniciando Flask…")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    # 1) Servidor Flask en hilo secundario
    threading.Thread(target=start_flask, daemon=True).start()
    # 2) Bot en hilo principal (evita el error del event loop)
    log.info("Iniciando bot (polling)…")
    application.run_polling(stop_signals=None, allowed_updates=Update.ALL_TYPES)
