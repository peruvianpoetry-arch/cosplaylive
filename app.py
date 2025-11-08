import os, json, threading, asyncio
from pathlib import Path
from flask import Flask, request, Response

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ----------------- Persistencia -----------------
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data")); DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "data.json"

def load_data():
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"admins": [], "prices": [], "model_name": "Cosplay Emma", "live": False, "group_id": None}

def save_data(d): DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
DB = load_data()

# ----------------- Traducción -----------------
ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION", "true").lower() in ("1","true","yes")
MODEL_ID = int(os.getenv("MODEL_ID", "0") or 0)
if ENABLE_TRANSLATION:
    from deep_translator import GoogleTranslator
    tr_es = lambda s: GoogleTranslator(source='auto', target='es').translate(s)
    tr_de = lambda s: GoogleTranslator(source='auto', target='de').translate(s)
else:
    tr_es = tr_de = lambda s: s

# ----------------- Telegram -----------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

def app_singleton() -> Application:
    global _APP
    try: return _APP
    except NameError:
        _APP = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        return _APP

def is_admin(uid:int)->bool: return uid in DB["admins"]

def need_admin(fn):
    async def wrap(update:Update, context:ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user and update.effective_user.id
        if not uid or not is_admin(uid):
            await update.effective_chat.send_message("Solo admin.")
            return
        return await fn(update, context)
    return wrap

# ----------------- Menú de donaciones -----------------
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "EUR")

def menu_markup(user=None):
    rows=[]
    uid=getattr(user,"id",""); uname=getattr(user,"username","")
    def mkurl(amount=None):
        u = f"{BASE_URL}/donar" if BASE_URL else "/donar"
        q=[]
        if amount is not None: q+= [f"amt={amount}"]
        q+= [f"c={CURRENCY}"]
        if uid: q+= [f"uid={uid}"]
        if uname: q+= [f"uname={uname}"]
        return u + "?" + "&".join(q)
    for p in DB["prices"]:
        rows.append([InlineKeyboardButton(f"{p['name']} · {p['price']} {CURRENCY}", url=mkurl(p["price"]))])
    rows.append([InlineKeyboardButton("💝 Donar libre", url=mkurl(None))])
    return InlineKeyboardMarkup(rows)

async def post_menu(context, chat_id:int):
    title = DB.get("model_name","Cosplay Emma")
    await context.bot.send_message(chat_id, f"💖 Apoya a *{title}* y aparece en pantalla.", parse_mode=ParseMode.MARKDOWN, reply_markup=menu_markup())

# --------- Eventos LIVE (grupo de discusión) ---------
async def on_group_live_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    DB["live"]=True; DB["group_id"]=update.effective_chat.id; save_data(DB)
    await post_menu(context, update.effective_chat.id)

async def on_group_live_end(update:Update, context:ContextTypes.DEFAULT_TYPE):
    DB["live"]=False; save_data(DB)
    await update.effective_chat.send_message("🔴 LIVE desactivado.")

# ----------------- Comandos -----------------
async def start_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    title = DB.get("model_name","Cosplay Emma")
    text = (f"Hola, soy el asistente de *{title}*.\n\n"
            "Comandos:\n"
            "• /start, /menu, /whoami\n"
            "• /iamadmin – hacerte admin\n"
            "• /addprice Nombre · 7, /delprice Nombre, /listprices\n"
            "• /setmodel Nombre\n"
            "• /liveon /liveoff\n"
            "• /studio – abrir panel/overlay")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def studio_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    base = BASE_URL or f"https://{request.host}" if request else BASE_URL
    base = base or "http://localhost:10000"
    await update.message.reply_text(f"Panel: {base}/studio\nOverlay: {base}/overlay")

async def whoami(update:Update, context:ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    await update.message.reply_text(f"Tu user_id: {u.id}\nUsername: @{u.username or '—'}")

async def iamadmin(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if uid not in DB["admins"]:
        DB["admins"].append(uid); save_data(DB)
    await update.message.reply_text("✅ Ya eres admin de este bot.")

@need_admin
async def setmodel(update:Update, context:ContextTypes.DEFAULT_TYPE):
    name=" ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usa: /setmodel Nombre del modelo"); return
    DB["model_name"]=name; save_data(DB)
    await update.message.reply_text(f"OK. Modelo: {name}")

@need_admin
async def addprice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    raw=" ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Usa: /addprice Nombre · 7"); return
    for sep in ["·","-","|"]: raw=raw.replace(sep," ")
    parts=[p for p in raw.split() if p]
    try:
        price=float(parts[-1].replace(",","."))
        name=" ".join(parts[:-1]).strip(); assert name
    except Exception:
        await update.message.reply_text("Usa: /addprice Nombre · 7"); return
    DB["prices"]=[p for p in DB["prices"] if p["name"].lower()!=name.lower()]
    DB["prices"].append({"name":name,"price":price}); save_data(DB)
    await update.message.reply_text(f"✅ Añadido: {name} · {price}")

@need_admin
async def delprice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    name=" ".join(context.args).strip()
    if not name: await update.message.reply_text("Usa: /delprice Nombre"); return
    before=len(DB["prices"])
    DB["prices"]=[p for p in DB["prices"] if p["name"].lower()!=name.lower()]
    save_data(DB)
    await update.message.reply_text("✅ Eliminado" if len(DB["prices"])<before else "No existía.")

@need_admin
async def listprices(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not DB["prices"]:
        await update.message.reply_text("Sin precios. Usa /addprice Nombre · 7"); return
    lines=[f"- {p['name']} · {p['price']}" for p in sorted(DB["prices"], key=lambda x:x["name"].lower())]
    await update.message.reply_text("Precios:\n"+"\n".join(lines))

async def menu_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=menu_markup(update.effective_user))

@need_admin
async def live_on(update:Update, context:ContextTypes.DEFAULT_TYPE):
    DB["live"]=True; DB["group_id"]=update.effective_chat.id; save_data(DB)
    await update.message.reply_text("🟢 LIVE activado."); await post_menu(context, update.effective_chat.id)

@need_admin
async def live_off(update:Update, context:ContextTypes.DEFAULT_TYPE):
    DB["live"]=False; save_data(DB); await update.message.reply_text("🔴 LIVE desactivado.")

# ---- Traducción en grupos ----
async def on_group_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group","supergroup"): return
    if not DB.get("group_id"): DB["group_id"]=update.effective_chat.id; save_data(DB)
    txt=update.effective_message.text or ""
    if not txt or txt.startswith("/"): return
    if not ENABLE_TRANSLATION: return
    uid=update.effective_user.id if update.effective_user else 0
    if uid==MODEL_ID and MODEL_ID!=0:
        t=tr_de(txt)
        if t.strip()!=txt.strip(): await update.effective_chat.send_message(f"🗣️ (DE) {t}")
    else:
        t=tr_es(txt)
        if t.strip()!=txt.strip(): await update.effective_chat.send_message(f"👀 (ES) {t}")

# ----------------- Flask (Studio/Overlay/Donar) -----------------
web = Flask(__name__)

@web.get("/")
def home(): return "CosplayLive bot OK"

@web.get("/studio")
def studio():
    return """<h3>Studio – Cosplay Emma</h3>
<p><a href="/overlay" target="_blank">Abrir Overlay</a></p>
<p><a href="/studio/ding">🔔 Probar sonido</a> – OK</p>"""

@web.get("/overlay")
def overlay():
    return Response("<html><body style='margin:0;background:#0b1220;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif'>Overlay listo</body></html>", mimetype="text/html")

@web.get("/studio/ding")
def studio_ding(): return "ding ok"

@web.get("/donar")
def donate_page():
    amt=(request.args.get("amt") or "").strip()
    ccy=(request.args.get("c") or CURRENCY).upper()
    if not (amt.replace(".","",1).isdigit() or amt.replace(",","",1).isdigit()):
        return "Monto inválido"
    return f"OK, {amt} {ccy} (demo)."

# ----------------- Lanzadores -----------------
def start_bot_in_thread():
    # Crear y registrar event loop en ESTE hilo
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = app_singleton()
    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("studio", studio_cmd))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("iamadmin", iamadmin))
    app.add_handler(CommandHandler("setmodel", setmodel))
    app.add_handler(CommandHandler("addprice", addprice))
    app.add_handler(CommandHandler("delprice", delprice))
    app.add_handler(CommandHandler("listprices", listprices))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("liveon", live_on))
    app.add_handler(CommandHandler("liveoff", live_off))
    app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_STARTED, on_group_live_start))
    app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_ENDED, on_group_live_end))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_group_text))

    # Ejecutar bot en este hilo con su loop
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

def run():
    # Arrancar BOT en hilo con event loop propio
    threading.Thread(target=start_bot_in_thread, name="bot-thread", daemon=True).start()
    # Servidor web en el hilo principal (Render necesita el puerto)
    port=int(os.getenv("PORT","10000"))
    web.run(host="0.0.0.0", port=port)

if __name__=="__main__":
    run()
