import os, json, time, re, io, asyncio, logging
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, Response

from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode,
    ChatPermissions
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackContext
)

# ========= Config & storage =========
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # -100xxxxxxxx
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")  # sin @
BASE_URL = os.getenv("BASE_URL", "https://cosplaylive.onrender.com")
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION", "0") == "1"

os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

def load():
    if not os.path.exists(DATA_FILE):
        return {"admins": [], "prices": [], "model_id": None,
                "langs": ["de","en","es"], "cooldown": 0, "live": False}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

state = load()

# ========= Logging =========
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# ========= Minimal "translation" helper =========
def translate(txt: str, target: str) -> str:
    if not ENABLE_TRANSLATION:
        return txt
    try:
        # sin dependencias externas: pequeño truco de demo
        # (si usas deep-translator, reemplaza aquí)
        return txt  # deja igual si no tienes traductor local
    except Exception:
        return txt

# ========= Overlay events (very simple) =========
overlay_queue: asyncio.Queue = asyncio.Queue()

async def push_event(ev: dict):
    await overlay_queue.put(json.dumps(ev))

web = Flask(__name__)

@web.get("/")
def root():
    return "OK"

@web.get("/overlay")
def overlay_page():
    # Página vacía + escucha de SSE
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
    <style>body{background:#0f172a;color:#e2e8f0;font-family:sans-serif}</style>
    <h3>Overlay</h3><div id="log"></div>
    <script>
      var es=new EventSource('/events');
      es.onmessage=(e)=>{ let o=JSON.parse(e.data);
        if(o.type==='ding'){ new Audio('https://actions.google.com/sounds/v1/alarms/beep_short.ogg').play(); }
        if(o.type==='celebrate'){ let d=document.getElementById('log');
          let p=document.createElement('p'); p.textContent='Gracias '+o.name+' ('+o.amount+')';
          d.prepend(p);
        }
      };
    </script>"""

@web.get("/events")
def sse():
    async def gen():
        while True:
            data = await overlay_queue.get()
            yield f"data: {data}\n\n"
    return Response(gen(), mimetype="text/event-stream")

@web.get("/studio")
def studio_page():
    return f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
    <h2>Studio – Cosplay Emma</h2>
    <p><a href="/overlay" target="_blank">Abrir Overlay</a></p>
    <form method="post" action="/studio/ding"><button>🔔 Probar sonido</button></form>
    <p><a href="/studio/ding">Probar sonido (GET)</a></p>"""

@web.route("/studio/ding", methods=["GET","POST"])
def studio_ding():
    loop = asyncio.get_event_loop()
    loop.create_task(push_event({"type":"ding","ts":time.time()}))
    return "ding!"

# ========= Stripe (opcional: solo crea URL de pago) =========
try:
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
except Exception:
    stripe = None

def amount_from_query(v: str):
    if not v: return None
    m = re.search(r"(\d+([.,]\d{1,2})?)", v.replace("€","").replace("EUR",""))
    if not m: return None
    return float(m.group(1).replace(",", "."))

@web.get("/donar")
def donate_page():
    amt_q = request.args.get("amt", "")
    ccy = request.args.get("c", "EUR").upper()
    uid = request.args.get("uid",""); uname = request.args.get("uname","")
    amt = amount_from_query(amt_q)
    if not amt:
        return "Monto inválido"
    title = f"Support {amt:.2f} {ccy}"
    if stripe and stripe.api_key:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": ccy.lower(),
                    "product_data": {"name": title},
                    "unit_amount": int(round(amt*100))
                },
                "quantity": 1
            }],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"channel_id": str(CHANNEL_ID), "amount": f"{amt:.2f} {ccy}",
                      "uid": uid, "uname": uname}
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">'
    # sin stripe -> solo simula ok
    loop = asyncio.get_event_loop()
    payer = f"@{uname}" if uname else "Supporter"
    loop.create_task(celebrate(None, CHANNEL_ID, payer, f"{amt:.2f} {ccy}", "¡Gracias!"))
    return "<h3>Pago simulado OK (modo sin Stripe)</h3>"

@web.get("/ok")
def ok_page():
    chan = CHANNEL_USERNAME
    tg_link = f"tg://resolve?domain={chan}" if chan else ""
    return f'✅ Pago recibido. <a href="{tg_link}">Volver a Telegram</a>'

# ========= Telegram bot =========
def is_admin(user_id:int)->bool:
    return user_id in state.get("admins",[])

def admin_only(f):
    @wraps(f)
    async def wrap(update:Update, context:ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not is_admin(uid):
            await update.effective_message.reply_text("Solo admin.")
            return
        return await f(update, context)
    return wrap

def kb_donaciones(user=None) -> InlineKeyboardMarkup:
    rows=[]; uid=""; uname=""
    if user:
        uid = getattr(user,"id","")
        uname = getattr(user,"username","") or ""
    base = f"{BASE_URL}/donar"
    def url_for(price):
        q=f"?amt={price}&c=EUR"
        if uid: q+=f"&uid={uid}"
        if uname: q+=f"&uname={uname}"
        return base+q
    prices = state.get("prices",[])
    if not prices:
        # placeholders
        for _ in range(4):
            rows.append([InlineKeyboardButton("name · price EUR", url=base)])
    else:
        for name,price in prices:
            rows.append([InlineKeyboardButton(f"{name} · {price} EUR", url=url_for(price))])
    rows.append([InlineKeyboardButton("💝 Donar libre", url=base + (f"?uid={uid}&uname={uname}" if uid else ""))])
    return InlineKeyboardMarkup(rows)

async def welcome_or_menu(context:CallbackContext, chat_id:int):
    now=time.time()
    if now < state.get("cooldown",0):
        return
    state["cooldown"]=now+600  # 10 min
    save(state)
    text = f"💝 Apoya a *Cosplay Emma* y aparece en pantalla."
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones())

async def celebrate(bot, chat_id:int, name:str, amount:str, memo:str):
    text = f"🎉 Gracias {name} — *{amount}*"
    app = telegram_app  # global
    await push_event({"type":"celebrate","name":name,"amount":amount,"memo":memo})
    await app.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)

# ---- Handlers ----
async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Asistente de Cosplay Emma.", reply_markup=kb_donaciones(update.effective_user))

async def cmd_menu(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=kb_donaciones(update.effective_user))

async def cmd_iamadmin(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in state["admins"]:
        state["admins"].append(uid); save(state)
    await update.message.reply_text("✅ Ya eres admin de este bot.")

@admin_only
async def cmd_liveon(update:Update, context:ContextTypes.DEFAULT_TYPE):
    state["live"]=True; save(state)
    await update.message.reply_text("🟢 LIVE activado.")

@admin_only
async def cmd_liveoff(update:Update, context:ContextTypes.DEFAULT_TYPE):
    state["live"]=False; save(state)
    await update.message.reply_text("🔴 LIVE desactivado.")

@admin_only
async def cmd_setlangs(update:Update, context:ContextTypes.DEFAULT_TYPE):
    langs = " ".join(context.args).replace(","," ").split()
    if not langs:
        await update.message.reply_text("Usa: /setlangs de,en,es")
        return
    state["langs"]=langs; save(state)
    await update.message.reply_text(f"Idiomas: {', '.join(langs)}")

@admin_only
async def cmd_setmodelid(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usa: /setmodelid 123456 o /setmodelid me")
        return
    if context.args[0].lower()=="me":
        state["model_id"]=update.effective_user.id
    else:
        try:
            state["model_id"]=int(context.args[0])
        except:
            return await update.message.reply_text("ID inválido.")
    save(state); await update.message.reply_text(f"Modelo ID: {state['model_id']}")

@admin_only
async def cmd_listprices(update:Update, context:ContextTypes.DEFAULT_TYPE):
    ps = state.get("prices",[])
    if not ps:
        await update.message.reply_text("Sin precios. Usa /addprice Nombre · 7")
        return
    s="\n".join([f"• {n} — {p} EUR" for n,p in ps])
    await update.message.reply_text(s)

@admin_only
async def cmd_resetprices(update:Update, context:ContextTypes.DEFAULT_TYPE):
    state["prices"]=[]; save(state)
    await update.message.reply_text("✔️ Precios reiniciados.")

def parse_addprice(text:str):
    # admite: /addprice Name 7 | Name · 7 | Name : 7 | Name - 7 | Name 7€ | Name 7 EUR
    m = re.match(r"^/addprice(?:@[\w_]+)?\s+(.+)$", text, re.I)
    if not m: return None
    rest = m.group(1).strip()
    # separa por '·' ':' '-' o espacio antes del número
    m = re.match(r"(.+?)\s*(?:[·:\-]\s*|\s+)(\d+[.,]?\d*)\s*(?:€|eur)?$", rest, re.I)
    if not m:
        return None
    name = m.group(1).strip()
    price = float(m.group(2).replace(",", "."))
    return name, int(price) if price.is_integer() else price

@admin_only
async def cmd_addprice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    parsed = parse_addprice(update.message.text or "")
    if not parsed:
        return await update.message.reply_text("Usa: /addprice Nombre · 7")
    name, price = parsed
    ps = state.get("prices",[])
    # reemplaza si existe
    ps = [p for p in ps if p[0].lower()!=name.lower()]
    ps.append([name, price]); state["prices"]=ps; save(state)
    await update.message.reply_text(f"Agregado: {name} — {price} EUR")

@admin_only
async def cmd_delprice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usa: /delprice Nombre")
    name=" ".join(context.args).strip().lower()
    ps=[p for p in state.get("prices",[]) if p[0].lower()!=name]
    state["prices"]=ps; save(state)
    await update.message.reply_text("Eliminado (si existía).")

async def on_group_message(update:Update, context:ContextTypes.DEFAULT_TYPE):
    # bienvenida + marketing
    await welcome_or_menu(context, update.effective_chat.id)

async def on_channel_message(update:Update, context:ContextTypes.DEFAULT_TYPE):
    # cada post en el canal: mostrar menú (con cooldown)
    await welcome_or_menu(context, update.effective_chat.id)

# Autodetección LIVE (grupo)
async def on_group_live_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    state["live"]=True; save(state)
    await context.bot.send_message(update.effective_chat.id, "🔴 LIVE detectado (grupo).")
async def on_group_live_end(update:Update, context:ContextTypes.DEFAULT_TYPE):
    state["live"]=False; save(state)
    await context.bot.send_message(update.effective_chat.id, "⚫ LIVE finalizado (grupo).")

# Autodetección LIVE (canal)
async def on_channel_live_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    state["live"]=True; save(state)
    await context.bot.send_message(CHANNEL_ID, "🔴 LIVE detectado (canal).")
async def on_channel_live_end(update:Update, context:ContextTypes.DEFAULT_TYPE):
    state["live"]=False; save(state)
    await context.bot.send_message(CHANNEL_ID, "⚫ LIVE finalizado (canal).")

# ========= Build application =========
telegram_app: Application = ApplicationBuilder().token(TOKEN).build()

telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CommandHandler("menu", cmd_menu))
telegram_app.add_handler(CommandHandler("iamadmin", cmd_iamadmin))
telegram_app.add_handler(CommandHandler("liveon", cmd_liveon))
telegram_app.add_handler(CommandHandler("liveoff", cmd_liveoff))
telegram_app.add_handler(CommandHandler("setlangs", cmd_setlangs))
telegram_app.add_handler(CommandHandler("setmodelid", cmd_setmodelid))
telegram_app.add_handler(CommandHandler("listprices", cmd_listprices))
telegram_app.add_handler(CommandHandler("resetprices", cmd_resetprices))
telegram_app.add_handler(CommandHandler("addprice", cmd_addprice))
telegram_app.add_handler(CommandHandler("delprice", cmd_delprice))

# grupo: mensajes normales
telegram_app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, on_group_message))
# canal: cada post
telegram_app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_message))

# video chat started/ended en grupos
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_STARTED & filters.ChatType.GROUPS, on_group_live_start))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_ENDED & filters.ChatType.GROUPS, on_group_live_end))
# y en canales
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_STARTED & filters.ChatType.CHANNEL, on_channel_live_start))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_ENDED & filters.ChatType.CHANNEL, on_channel_live_end))

# ========= Runner =========
def run():
    # arranca bot en segundo plano
    loop = asyncio.get_event_loop()
    loop.create_task(telegram_app.initialize())
    loop.create_task(telegram_app.start())
    # Flask
    web.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))

if __name__ == "__main__":
    run()
