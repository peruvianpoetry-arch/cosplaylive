import os, io, json, time, asyncio, threading
from pathlib import Path
from typing import Dict, List, Tuple

from flask import Flask, request, Response
import stripe

# -------------------- Telegram (PTB 20.x) --------------------
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

# -------------------- Traducción (opcional) --------------------
ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION", "false").lower() == "true"
if ENABLE_TRANSLATION:
    try:
        from deep_translator import GoogleTranslator
    except Exception:
        ENABLE_TRANSLATION = False

# -------------------- Config --------------------
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "data.json"

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

DEFAULT_MODEL_NAME = "Cosplay Emma"
DEFAULT_CC = "EUR"

# -------------------- Estado persistente --------------------
def load_state() -> Dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {
        "admins": [],             # lista de user_id
        "model_name": DEFAULT_MODEL_NAME,
        "currency": DEFAULT_CC,
        "prices": [                # demo inicial (puedes vaciarla con /resetprices)
            ["Group goal", 50],
            ["Freier Betrag", 0],
        ],
        "live": False,
    }

def save_state(d: Dict):
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")

state = load_state()

# -------------------- Utilidades --------------------
def is_admin(user_id: int) -> bool:
    return user_id in state["admins"]

def ensure_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not is_admin(uid):
            await update.effective_chat.send_message("Solo admin.", quote=False)
            return
        return await func(update, context)
    return wrapper

def kb_menu(user=None) -> InlineKeyboardMarkup:
    rows = []
    uid = getattr(user, "id", "")
    uname = getattr(user, "username", "") or ""
    base = f"{BASE_URL}/donar"

    def url_for(name: str, price: float):
        q = f"?c={state['currency']}"
        if price and price > 0:
            q += f"&amt={price}"
        if uid:   q += f"&uid={uid}"
        if uname: q += f"&uname={uname}"
        return base + q

    for name, price in state["prices"]:
        label = f"{name} · {state['currency']}" if not price else f"{name} · {price} {state['currency']}"
        rows.append([InlineKeyboardButton(label, url=url_for(name, price or 0))])

    return InlineKeyboardMarkup(rows)

def app_title():
    return f"Asistente de <b>{state['model_name']}</b>."

# -------------------- Overlay / Server-Sent Events --------------------
overlay_clients: List[asyncio.Queue] = []

async def push_event(payload: Dict):
    # notificar a todos los clientes conectados al overlay
    dead = []
    for q in overlay_clients:
        try:
            await q.put(json.dumps(payload))
        except Exception:
            dead.append(q)
    for q in dead:
        try: overlay_clients.remove(q)
        except: pass

# -------------------- Telegram App Singleton --------------------
_telegram_app: Application = None
def telegram_app_singleton() -> Application:
    global _telegram_app
    if _telegram_app:
        return _telegram_app
    _telegram_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )
    # --- Handlers ---
    _telegram_app.add_handler(CommandHandler("start", cmd_start))
    _telegram_app.add_handler(CommandHandler("menu", cmd_menu))
    _telegram_app.add_handler(CommandHandler("studio", cmd_studio))
    _telegram_app.add_handler(CommandHandler("iamadmin", cmd_iamadmin))
    _telegram_app.add_handler(CommandHandler("whoami", cmd_whoami))
    _telegram_app.add_handler(CommandHandler("listprices", cmd_listprices))
    _telegram_app.add_handler(CommandHandler("resetprices", cmd_resetprices))
    _telegram_app.add_handler(CommandHandler("addprice", cmd_addprice))
    _telegram_app.add_handler(CommandHandler("delprice", cmd_delprice))
    _telegram_app.add_handler(CommandHandler("setmodel", cmd_setmodel))
    _telegram_app.add_handler(CommandHandler("setccy", cmd_setccy))
    _telegram_app.add_handler(CommandHandler("liveon", cmd_liveon))
    _telegram_app.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # Responder actividad en grupo de discusión (bienvenida + menú + traducción)
    _telegram_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_any))

    # Lanza el loop del bot en thread aparte al arrancar Flask
    threading.Thread(target=_telegram_app.run_polling, kwargs={"allowed_updates": Update.ALL_TYPES}, daemon=True).start()
    return _telegram_app

# -------------------- Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(app_title())
    await cmd_menu(update, context)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.effective_chat.send_message("💝 Opciones de apoyo:", reply_markup=kb_menu(user))

async def cmd_studio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Solo admin.")
        return
    await update.effective_chat.send_message(f"🔗 Abre tu panel: {BASE_URL}/studio")

async def cmd_iamadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in state["admins"]:
        state["admins"].append(uid)
        save_state(state)
    await update.effective_chat.send_message("✅ Ya eres admin de este bot.")

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_chat.send_message(f"Tu user_id: <code>{u.id}</code>\nUsername: @{u.username}", parse_mode=ParseMode.HTML)

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["prices"]:
        await update.effective_chat.send_message("No hay precios.")
        return
    lines = [f"• {n} — {p} {state['currency']}" if p else f"• {n} — libre" for n, p in state["prices"]]
    await update.effective_chat.send_message("\n".join(lines))

@ensure_admin
async def cmd_resetprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["prices"] = []
    save_state(state)
    await update.effective_chat.send_message("✅ Lista de precios vaciada.")

@ensure_admin
async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Formatos aceptados:
    # /addprice Nombre · 7
    # /addprice Nombre 7
    # /addprice Nombre - 7
    txt = (update.message.text or "").strip()
    parts = txt.split(" ", 1)
    if len(parts) < 2:
        await update.effective_chat.send_message("Usa: /addprice Nombre · 7")
        return
    rest = parts[1].strip()
    # Normaliza separadores
    for sep in [" · ", " - ", " — ", " – "]:
        rest = rest.replace(sep, " ")
    toks = rest.split()
    if not toks:
        await update.effective_chat.send_message("Usa: /addprice Nombre · 7")
        return
    try:
        price = float(toks[-1].replace(",", "."))
        name = " ".join(toks[:-1]).strip()
    except ValueError:
        # sin precio => libre
        name = rest
        price = 0.0
    if not name:
        await update.effective_chat.send_message("Usa: /addprice Nombre · 7")
        return
    state["prices"].append([name, price])
    save_state(state)
    await update.effective_chat.send_message(f"✅ Añadido: {name} ({'libre' if price==0 else f'{price} {state['currency']}'})")

@ensure_admin
async def cmd_delprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").split(" ", 1)
    if len(name) < 2:
        await update.effective_chat.send_message("Usa: /delprice Nombre")
        return
    key = name[1].strip().lower()
    before = len(state["prices"])
    state["prices"] = [x for x in state["prices"] if x[0].lower() != key]
    save_state(state)
    removed = before - len(state["prices"])
    await update.effective_chat.send_message("Eliminados: %d" % removed)

@ensure_admin
async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").split(" ", 1)
    if len(name) < 2:
        await update.effective_chat.send_message("Usa: /setmodel Nombre del modelo")
        return
    state["model_name"] = name[1].strip()
    save_state(state)
    await update.effective_chat.send_message(f"Modelo ahora: {state['model_name']}")

@ensure_admin
async def cmd_setccy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").split(" ", 1)
    if len(t) < 2:
        await update.effective_chat.send_message("Usa: /setccy EUR")
        return
    state["currency"] = t[1].strip().upper()
    save_state(state)
    await update.effective_chat.send_message(f"Moneda ahora: {state['currency']}")

@ensure_admin
async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["live"] = True
    save_state(state)
    await update.effective_chat.send_message("🟢 LIVE activado.")

@ensure_admin
async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["live"] = False
    save_state(state)
    await update.effective_chat.send_message("🔴 LIVE desactivado.")

# Bienvenida / traducción / marketing básico en grupo
async def on_text_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    # 1) Bienvenida simplificada
    if update.message and update.message.new_chat_members:
        for m in update.message.new_chat_members:
            await chat.send_message(f"👋 Bienvenido, @{m.username or m.first_name}!")
        return

    # 2) Traducción (opcional) reemite como INFO para la modelo
    if ENABLE_TRANSLATION and update.message and update.message.text:
        user_lang = "de"  # heurística simple; cámbiala si quieres por detección
        try:
            translated = GoogleTranslator(source='auto', target='es').translate(update.message.text)
            await chat.send_message(f"🈯️ Traducción para la modelo:\n<code>{translated}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass

    # 3) Marketing básico si hay LIVE
    if state["live"]:
        try:
            await chat.send_message(
                f"💖 Apoya a <b>{state['model_name']}</b> y aparece en pantalla.",
                reply_markup=kb_menu(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass

# -------------------- Eventos de celebración --------------------
async def celebrate(bot, chat_id: int, payer_name: str, amount: str, memo: str):
    text = f"🎉 Gracias {payer_name} por {amount}!\n{memo}"
    await bot.send_message(chat_id, text)
    # Empuja al overlay 1 evento con sonido
    await push_event({"type": "ding", "text": text})

# -------------------- Flask --------------------
web = Flask(__name__)

@web.get("/")
def root():
    return "OK"

@web.get("/overlay")
def overlay():
    async def event_stream(q: asyncio.Queue):
        try:
            while True:
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            return

    q = asyncio.Queue()
    overlay_clients.append(q)
    loop = asyncio.get_event_loop()

    def generate():
        # primera conexión: limpio
        yield "data: {}\n\n"
        while True:
            data = loop.run_until_complete(q.get())
            yield f"data: {data}\n\n"

    return Response(generate(), mimetype="text/event-stream")

@web.get("/studio")
def studio():
    return """
    <h2>Studio – {model}</h2>
    <p><a href="/overlay" target="_blank">Abrir Overlay</a></p>
    <form action="/studio/ding" method="post"><button>🔔 Probar sonido</button></form>
    """.format(model=state["model_name"])

@web.post("/studio/ding")
def studio_ding():
    app = telegram_app_singleton()
    # Ejecuta la corrutina en el loop del bot
    asyncio.get_event_loop().create_task(push_event({"type":"ding","text":"Test"}))
    return "OK"

# Página OK con volver a Telegram
@web.get("/ok")
def ok_page():
    tg_link = f"tg://resolve?domain={CHANNEL_USERNAME}" if CHANNEL_USERNAME else ""
    btn = f'<p><a href="{tg_link}">Volver a Telegram</a></p>' if tg_link else ""
    return f"<h2>✅ Pago recibido (modo test)</h2>{btn}"

# Stripe: página de donación
@web.get("/donar")
def donate_page():
    amt = request.args.get("amt", "").strip()
    ccy = request.args.get("c", state["currency"]).upper()
    uid = request.args.get("uid", "")
    uname = request.args.get("uname", "")
    title = f"Support {state['model_name']}"

    if amt:
        try:
            v = float(amt.replace(",", "."))
            assert v > 0
        except Exception:
            return "Monto inválido"
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": ccy.lower(),
                    "product_data": {"name": title},
                    "unit_amount": int(round(v * 100)),
                },
                "quantity": 1
            }],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/ok",
            metadata={
                "channel_id": str(CHANNEL_ID),
                "amount": f"{v:.2f} {ccy}",
                "uid": uid,
                "uname": uname
            },
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">'
    else:
        # libre: pedir cantidad
        return """
        <h3>Donación libre</h3>
        <form method="get" action="/donar">
          <input name="amt" placeholder="Cantidad" />
          <input type="hidden" name="c" value="{ccy}">
          <button type="submit">Pagar</button>
        </form>
        """.format(ccy=ccy)

# Webhook Stripe (modo test: no es obligatorio, pero lo dejamos)
@web.post("/stripe_webhook")
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig = request.headers.get("Stripe-Signature", "")
    # Si no tienes endpoint secret, aceptamos sin verificar en test:
    try:
        event = json.loads(payload)
    except Exception:
        return "bad", 400

    et = event.get("type")
    if et == "checkout.session.completed":
        sess = event["data"]["object"]
        md = (sess.get("metadata") or {})
        uname = (md.get("uname") or "").strip()
        payer = f"@{uname}" if uname else "Supporter"
        amount = md.get("amount") or f"{(sess.get('amount_total') or 0)/100:.2f} {(sess.get('currency') or '').upper()}"
        memo = "¡Gracias por tu apoyo!"
        app = telegram_app_singleton()
        app.create_task(celebrate(app.bot, CHANNEL_ID, payer, amount, memo))
    return "ok", 200

# -------------------- Lanzar todo --------------------
if __name__ == "__main__":
    telegram_app_singleton()  # arranca el bot (polling) en un thread
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port)
