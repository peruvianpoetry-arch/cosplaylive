import os, io, json, asyncio, time, logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from flask import Flask, request, Response
from threading import Thread

# --- Telegram ---
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatAdministratorRights
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackContext, filters
)

# --- Stripe ---
import stripe

# --- Imagen (Pillow) ---
from PIL import Image, ImageDraw, ImageFont

# -------------------------
# Configuración de entorno
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))               # ej. -1001234567890
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")         # ej. cosplayemmalive (SIN @)
BASE_URL = os.getenv("BASE_URL", "https://example.onrender.com").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "EUR")

STRIPE_SECRET = os.getenv("STRIPE_SECRET", "")
stripe.api_key = STRIPE_SECRET

PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("cosplaylive")

# -------------------------
# Persistencia en disco
# -------------------------
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

def load_state() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # estado por defecto
    return {
        "admins": [],                         # user_ids que pueden usar comandos admin
        "model_name": "Cosplay Emma",
        "langs": ["de","en","es","pl"],
        "marketing_on": False,
        "prices": [                           # (Nombre, precio)
            ["Besito", 1],
            ["Cariño", 3],
            ["Te amo", 5],
            ["Regalito", 7]
        ]
    }

def save_state(state: Dict[str, Any]) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

STATE = load_state()

# --------------
# Flask (Web)
# --------------
web = Flask(__name__)

# --------------
# SSE (overlay)
# --------------
class EventBus:
    def __init__(self):
        self._subs: List[asyncio.Queue] = []

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    async def push(self, payload: Dict[str, Any]):
        # limpiar subs muertos de forma perezosa
        survivors = []
        for q in self._subs:
            try:
                q.put_nowait(payload)
                survivors.append(q)
            except Exception:
                pass
        self._subs = survivors

EVENTS = EventBus()

# Singleton de la app de Telegram (para disparar tareas desde webhook)
_app_singleton: Optional[Application] = None
def telegram_app_singleton() -> Application:
    global _app_singleton
    if _app_singleton is None:
        _app_singleton = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .build()
        )
    return _app_singleton

# -------------------------
# Utilidades
# -------------------------
def is_admin(user_id: int) -> bool:
    return user_id in STATE.get("admins", [])

def ensure_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return is_admin(uid)

def price_menu() -> List[List[Any]]:
    return STATE.get("prices", [])

def set_marketing(on: bool):
    STATE["marketing_on"] = on
    save_state(STATE)

def _center(draw, txt, font, y, width=1200):
    # textbbox para Pillow moderno
    left, top, right, bottom = draw.textbbox((0,0), txt, font=font)
    w = right - left
    return ((width - w)//2, y)

def build_card(title: str, subtitle: str) -> bytes:
    W, H = 1200, 500
    img = Image.new("RGB", (W, H), (8, 12, 22))
    d = ImageDraw.Draw(img)
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 68)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 44)
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()
    d.rounded_rectangle([(20,20),(W-20,H-20)], radius=28, fill=(18,27,46))
    d.text(_center(d, title, font_big, 140), title, font=font_big, fill=(255,255,255))
    d.text(_center(d, subtitle, font_small, 260), subtitle, font=font_small, fill=(190,220,255))
    buf = io.BytesIO()
    img.save(buf, "PNG"); buf.seek(0)
    return buf.read()

async def push_event(event_type: str, data: Dict[str, Any]):
    await EVENTS.push({"type": event_type, "data": data, "ts": int(time.time())})

async def celebrate(bot, chat_id: int, payer_name: str, amount: str, memo: str):
    title = f"{payer_name} apoyó {amount}"
    subtitle = f"{memo}"
    png = build_card(title, subtitle)

    # Anuncio al canal
    msg = await bot.send_message(
        chat_id,
        f"💝 *{payer_name}* apoyó *{amount}*.\n_{memo}_",
        parse_mode=ParseMode.MARKDOWN
    )
    await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)

    # Imagen tarjeta
    await bot.send_photo(chat_id, png, caption=f"Gracias {payer_name} 🫶")

    # Overlay: sonar y mostrar
    await push_event("donation", {"payer": payer_name, "amount": amount, "memo": memo})

# -------------------------
# Teclados y menús
# -------------------------
def kb_donaciones(user=None) -> InlineKeyboardMarkup:
    rows = []
    uid = getattr(user, "id", "") if user else ""
    uname = (getattr(user, "username", "") or "") if user else ""

    base = f"{BASE_URL}/donar"

    def url_for(price):
        q = f"?amt={price}&c={CURRENCY}"
        if uid: q += f"&uid={uid}"
        if uname: q += f"&uname={uname}"
        return base + q

    for name, price in price_menu():
        rows.append([InlineKeyboardButton(f"{name} · {price} {CURRENCY}", url=url_for(price))])

    extra_q = f"?c={CURRENCY}"
    if uid:   extra_q += f"&uid={uid}"
    if uname: extra_q += f"&uname={uname}"
    rows.append([InlineKeyboardButton("💝 Donar libre", url=f"{base}{extra_q}")])
    return InlineKeyboardMarkup(rows)

# -------------------------
# Handlers Telegram
# -------------------------
async def cmd_start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        f"Hola {update.effective_user.first_name or ''} 👋\n"
        f"Soy el asistente de *{STATE.get('model_name')}*.\n"
        f"Elige una opción:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_donaciones(update.effective_user)
    )

async def cmd_menu(update: Update, context: CallbackContext):
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=kb_donaciones(update.effective_user))

async def cmd_iamadmin(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    if uid not in STATE["admins"]:
        STATE["admins"].append(uid)
        save_state(STATE)
    await update.message.reply_text("✅ Ya eres admin de este bot.")

async def cmd_addprice(update: Update, context: CallbackContext):
    if not ensure_admin(update): return await update.message.reply_text("Solo admin.")
    arg = update.message.text.split(" ",1)[-1].strip()
    if "·" not in arg:
        return await update.message.reply_text("Usa: /addprice Nombre · 7")
    name, price = [s.strip() for s in arg.split("·",1)]
    try:
        price = float(price)
    except:
        return await update.message.reply_text("Precio inválido.")
    STATE["prices"].append([name, price])
    save_state(STATE)
    await update.message.reply_text("✅ Precio agregado.")

async def cmd_delprice(update: Update, context: CallbackContext):
    if not ensure_admin(update): return await update.message.reply_text("Solo admin.")
    name = update.message.text.split(" ",1)[-1].strip()
    before = len(STATE["prices"])
    STATE["prices"] = [p for p in STATE["prices"] if p[0] != name]
    save_state(STATE)
    await update.message.reply_text("✅ Eliminado." if len(STATE["prices"])<before else "No encontrado.")

async def cmd_listprices(update: Update, context: CallbackContext):
    lines = [f"• {n} · {v} {CURRENCY}" for n,v in STATE.get("prices",[])]
    await update.message.reply_text("Precios actuales:\n" + "\n".join(lines))

async def cmd_setmodel(update: Update, context: CallbackContext):
    if not ensure_admin(update): return await update.message.reply_text("Solo admin.")
    model = update.message.text.split(" ",1)[-1].strip()
    if not model: return await update.message.reply_text("Usa: /setmodel Nombre")
    STATE["model_name"] = model; save_state(STATE)
    await update.message.reply_text("✅ Nombre actualizado.")

async def cmd_setlangs(update: Update, context: CallbackContext):
    if not ensure_admin(update): return await update.message.reply_text("Solo admin.")
    langs = update.message.text.split(" ",1)[-1].strip()
    arr = [s.strip().lower() for s in langs.split(",") if s.strip()]
    STATE["langs"] = arr or ["de","en","es","pl"]; save_state(STATE)
    await update.message.reply_text("✅ Idiomas actualizados.")

async def cmd_liveon(update: Update, context: CallbackContext):
    if not ensure_admin(update): return await update.message.reply_text("Solo admin.")
    set_marketing(True)
    await update.message.reply_text("✅ LIVE ON (marketing activo).")

async def cmd_liveoff(update: Update, context: CallbackContext):
    if not ensure_admin(update): return await update.message.reply_text("Solo admin.")
    set_marketing(False)
    await update.message.reply_text("✅ LIVE OFF (marketing detenido).")

async def on_group_message(update: Update, context: CallbackContext):
    # Marketing básico si está activo
    if STATE.get("marketing_on"):
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                f"💝 Apoya a *{STATE.get('model_name')}* y recibe un saludo en pantalla.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_donaciones()  # en grupo/canal: sin usuario
            )
        except Exception as e:
            log.warning(f"marketing msg error: {e}")

# Autodetección simple de video chat en grupos enlazados (status updates)
async def on_status_update(update: Update, context: CallbackContext):
    if update.message.video_chat_started:
        set_marketing(True)
        await context.bot.send_message(update.effective_chat.id, "🔴 LIVE detectado (grupo). Marketing activado.")
    elif update.message.video_chat_ended:
        set_marketing(False)
        await context.bot.send_message(update.effective_chat.id, "⚫️ LIVE finalizado. Marketing detenido.")

# -------------------------
# Rutas Web (donar/overlay)
# -------------------------
@web.get("/")
def index():
    return "CosplayLive bot OK"

@web.get("/overlay")
def overlay_page():
    return """
<!doctype html><meta charset="utf-8">
<title>Overlay</title>
<style>
 body{background:#0b1020;color:#fff;font-family:system-ui,Arial;margin:0}
 .event{padding:16px;margin:12px;border-radius:14px;background:#16213a}
 .big{font-size:22px;font-weight:700}
</style>
<div id="log"></div>
<script>
  const log = document.getElementById('log');
  const ev = new EventSource('/events');
  const ding = new Audio('https://actions.google.com/sounds/v1/cartoon/clang_and_wobble.ogg');
  ev.onmessage = (m)=>{
    const obj = JSON.parse(m.data);
    if(obj.type==='donation'){ ding.currentTime=0; ding.play(); }
    const div = document.createElement('div');
    div.className='event';
    let html = `<div class="big">${obj.type.toUpperCase()}</div>`;
    html += `<div>${JSON.stringify(obj.data)}</div>`;
    log.prepend(div);
    div.innerHTML = html;
  };
</script>
"""

@web.get("/events")
def sse_events():
    async def gen():
        q = await EVENTS.subscribe()
        try:
            while True:
                item = await q.get()
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await EVENTS.unsubscribe(q)

    loop = asyncio.get_event_loop()
    return Response(loop.run_in_executor(None, lambda: asyncio.run(gen())), mimetype="text/event-stream")

@web.get("/studio")
def studio_page():
    return f"""
<!doctype html><meta charset="utf-8">
<title>Studio</title>
<h2>Studio – {STATE.get('model_name')}</h2>
<p><a href="{BASE_URL}/overlay" target="_blank">Abrir Overlay</a></p>
<form method="post" action="{BASE_URL}/studio/ding">
  <button>🔔 Probar sonido overlay</button>
</form>
"""

@web.post("/studio/ding")
def studio_ding():
    app = telegram_app_singleton()
    app.create_task(push_event("donation", {"payer":"TestUser","amount":"0.00","memo":"Test"}))
    return "<p>OK. Abre Overlay para oír el sonido.</p><p><a href='/studio'>Volver</a></p>"

@web.get("/donar")
def donate_page():
    amt = request.args.get("amt", "")
    ccy = request.args.get("c", CURRENCY)
    uid = request.args.get("uid", "")
    uname = request.args.get("uname", "")

    title = f"Apoyo a {STATE.get('model_name')}"
    if amt and amt.replace(".","",1).isdigit():
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": ccy.lower(),
                    "product_data": {"name": title},
                    "unit_amount": int(float(amt)*100)
                },
                "quantity": 1
            }],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={
                "channel_id": str(CHANNEL_ID),
                "amount": f"{amt} {ccy}",
                "uid": uid,
                "uname": uname
            },
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">'
    else:
        # página simple para donación libre (en demo)
        return f"""
        <h2>Donación libre</h2>
        <form method="get" action="/donar">
          <input type="hidden" name="c" value="{ccy}">
          <input type="hidden" name="uid" value="{uid}">
          <input type="hidden" name="uname" value="{uname}">
          <input name="amt" placeholder="Cantidad" />
          <button>Pagar</button>
        </form>
        """

@web.get("/ok")
def ok_page():
    chan = CHANNEL_USERNAME.strip()
    tg_link = f"tg://resolve?domain={chan}" if chan else ""
    return f"""
    <h2>✅ Pago recibido (modo test)</h2>
    <p>Ya puedes volver a Telegram. En unos segundos verás el anuncio en el canal.</p>
    {'<p><a href="'+tg_link+'">Volver a Telegram</a></p>' if tg_link else ''}
    """

@web.get("/cancel")
def cancel_page():
    return "<h3>Pago cancelado</h3>"

@web.post("/webhook")
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, endpoint_secret) if endpoint_secret else json.loads(payload)
    except Exception as e:
        log.error(f"stripe webhook error: {e}")
        return ("", 400)

    et = event.get("type")
    if et == "checkout.session.completed":
        sess = event["data"]["object"]
        md = (sess.get("metadata") or {})
        uname = (md.get("uname") or "").strip()
        payer_name = f"@{uname}" if uname else "Supporter"
        amount = md.get("amount") or f"{(sess.get('amount_total') or 0)/100:.2f} {str(sess.get('currency') or '').upper()}"
        memo = "¡Gracias por tu apoyo!"

        app = telegram_app_singleton()
        app.create_task( celebrate(app.bot, int(CHANNEL_ID), payer_name, amount, memo) )
    return ("", 200)

# -------------------------
# Arranque Telegram + Flask
# -------------------------
def run_flask():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    app = telegram_app_singleton()

    # Comandos
    app.add_handler(CommandHandler(["start","help"], cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("iamadmin", cmd_iamadmin))
    app.add_handler(CommandHandler("addprice", cmd_addprice))
    app.add_handler(CommandHandler("delprice", cmd_delprice))
    app.add_handler(CommandHandler("listprices", cmd_listprices))
    app.add_handler(CommandHandler("setmodel", cmd_setmodel))
    app.add_handler(CommandHandler("setlangs", cmd_setlangs))
    app.add_handler(CommandHandler("liveon", cmd_liveon))
    app.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # Mensajes en grupos (marketing)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_message))

    # Status updates en grupos (video chat started/ended)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.UpdateType.MESSAGE & filters.StatusUpdate.ALL, on_status_update))

    # Flask en hilo aparte
    t = Thread(target=run_flask, daemon=True)
    t.start()

    log.info("Starting polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
