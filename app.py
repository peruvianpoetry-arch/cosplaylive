import os, json, threading, time, asyncio
from pathlib import Path
from flask import Flask, request, Response

# ================== CONFIG ==================
TOKEN              = os.environ.get("TELEGRAM_TOKEN", "").strip()
ADMIN_USER_ID      = int(os.environ.get("ADMIN_USER_ID", "0") or "0")
DATA_DIR           = Path(os.environ.get("DATA_DIR", "/var/data"))
ENABLE_TRANSLATION = os.environ.get("ENABLE_TRANSLATION", "1").lower() in ("1","true","yes","on")
ANNOUNCE_EVERY_MIN = int(os.environ.get("ANNOUNCE_EVERY_MIN", "5"))  # repetición de anuncios

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "data.json"

# ================== STORAGE ==================
def load_db():
    if DB_FILE.exists():
        try: return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {
        "prices":[
            {"name":"name","price":0},
            {"name":"name","price":0},
            {"name":"name","price":0},
            {"name":"name","price":0},
        ],
        "live": False,
        "discussion_chat_id": None,
        "last_announce_ts": 0
    }

def save_db(d): DB_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

db = load_db()

# ================== TELEGRAM ==================
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters

BOT_APP = None  # se asigna al arrancar

def is_admin(uid:int)->bool:
    return ADMIN_USER_ID and uid == ADMIN_USER_ID

def public_base():
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") if request else None
    if host: return f"https://{host}"
    return f"http://localhost:{os.environ.get('PORT','10000')}"

def prices_kb():
    rows=[]
    for item in db.get("prices", []):
        name=item.get("name","name")
        price=item.get("price",0)
        label = f"{name} · {price} EUR" if price else f"{name} · price EUR"
        rows.append([InlineKeyboardButton(text=label, url=f"{public_base()}/donar?amt={price or ''}")])
    rows.append([InlineKeyboardButton(text="💝 Donar libre", url=f"{public_base()}/donar")])
    return InlineKeyboardMarkup(rows)

# --------- comandos ----------
async def cmd_start(update: Update, ctx: CallbackContext):
    await update.message.reply_text(
        "Asistente de Cosplay Emma.\nComandos: /studio /bindhere /liveon /liveoff /announce /addprice /listprices /resetprices",
        reply_markup=prices_kb()
    )

async def cmd_studio(update: Update, ctx: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return
    await update.message.reply_text(f"Studio: {public_base()}/studio")

async def cmd_bindhere(update: Update, ctx: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return
    db["discussion_chat_id"] = update.effective_chat.id
    save_db(db)
    await update.message.reply_text("✅ Chat de discusión vinculado para anuncios.", reply_markup=prices_kb())

async def cmd_liveon(update: Update, ctx: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return
    db["live"] = True
    db["last_announce_ts"] = 0
    save_db(db)
    await update.message.reply_text("🟢 LIVE activado.")
    await do_announce_once(ctx.application)

async def cmd_liveoff(update: Update, ctx: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return
    db["live"] = False; save_db(db)
    await update.message.reply_text("🔴 LIVE desactivado.")

async def cmd_announce(update: Update, ctx: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return
    ok = await do_announce_once(ctx.application)
    await update.message.reply_text("✅ Anuncio enviado." if ok else "⚠️ Vincula un chat con /bindhere.")

def parse_price_args(args:list[str]):
    if not args: return None
    txt=" ".join(args).strip()
    for sep in ["·","-","|",":"]:
        txt = txt.replace(f" {sep} ", " ")
        txt = txt.replace(sep, " ")
    parts = txt.split()
    if len(parts)<2: return None
    try: price = float(parts[-1].replace(",", "."))
    except: return None
    name  = " ".join(parts[:-1]).strip()
    if not name: return None
    return {"name":name, "price": price}

async def cmd_addprice(update: Update, ctx: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return
    parsed = parse_price_args(ctx.args)
    if not parsed:
        await update.message.reply_text("Usa: /addprice Nombre · 7"); return
    name_lower = parsed["name"].lower(); replaced=False
    for it in db["prices"]:
        if it["name"].lower()==name_lower:
            it["price"]=parsed["price"]; replaced=True; break
    if not replaced: db["prices"].append(parsed)
    save_db(db)
    await update.message.reply_text("✅ Precio guardado.", reply_markup=prices_kb())

async def cmd_listprices(update: Update, ctx: CallbackContext):
    text = "\n".join([f"- {p['name']} · {p['price']} EUR" for p in db.get("prices",[])]) or "(vacío)"
    await update.message.reply_text(text, reply_markup=prices_kb())

async def cmd_resetprices(update: Update, ctx: CallbackContext):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return
    db["prices"]=[]; save_db(db)
    await update.message.reply_text("✅ Lista de precios reiniciada.", reply_markup=prices_kb())

# --------- traducción automática ----------
if ENABLE_TRANSLATION:
    try:
        from deep_translator import GoogleTranslator
    except Exception:
        GoogleTranslator=None
else:
    GoogleTranslator=None

def tr(text:str, src='auto', to='es'):
    if not GoogleTranslator: return text
    try: return GoogleTranslator(source=src, target=to).translate(text)
    except Exception: return text

async def on_group_message(update: Update, ctx: CallbackContext):
    msg = update.effective_message
    if not msg or not msg.text: return
    original = msg.text.strip()
    es = tr(original, 'auto', 'es')
    de = tr(original, 'auto', 'de')
    if es != original: await msg.reply_text(f"🇪🇸 {es}")
    if de != original: await msg.reply_text(f"🇩🇪 {de}")

# --------- anunciador ----------
ANNOUNCE_TEXT = (
    "💖 Apoya a Cosplay Emma y aparece en pantalla.\n"
    "Selecciona una opción o dona libre:"
)

async def do_announce_once(app: Application) -> bool:
    chat_id = db.get("discussion_chat_id")
    if not chat_id: return False
    try:
        await app.bot.send_message(chat_id=chat_id, text=ANNOUNCE_TEXT, reply_markup=prices_kb())
        db["last_announce_ts"] = int(time.time()); save_db(db)
        return True
    except Exception:
        return False

def announcer_loop():
    # corre en hilo aparte, usa el bot de BOT_APP
    while True:
        try:
            if db.get("live") and db.get("discussion_chat_id") and BOT_APP:
                now = int(time.time())
                due = db.get("last_announce_ts", 0) + ANNOUNCE_EVERY_MIN*60
                if now >= due:
                    asyncio.run(do_announce_once(BOT_APP))
        except Exception:
            pass
        time.sleep(15)

# ================== FLASK (overlay & pagos demo) ==================
flask_app = Flask(__name__)

@flask_app.get("/")
def index(): return "CosplayLive bot OK"

@flask_app.get("/studio")
def studio():
    return f"""<html><head><meta charset="utf-8"><title>Studio – Cosplay Emma</title></head>
<body>
<h3>Studio – Cosplay Emma</h3>
<p><a href="{public_base()}/overlay" target="_blank">Abrir Overlay</a></p>
<p><a href="{public_base()}/overlay?sound=1" target="_blank">Probar sonido</a></p>
</body></html>"""

@flask_app.get("/overlay")
def overlay():
    return """<html><head><meta charset="utf-8"><title>Overlay</title>
<style>html,body{margin:0;height:100%;background:#0e1320;color:#fff;font-family:system-ui,sans-serif}</style>
</head><body></body></html>"""

@flask_app.get("/donar")
def donar():
    amt = (request.args.get("amt") or "").replace(",", ".")
    try: amount = float(amt) if amt else None
    except: amount = None
    if amt and amount is None: return Response("Monto inválido", mimetype="text/plain")
    return f"""<html><head><meta charset="utf-8"><title>Donar</title></head>
<body><h3>Gracias por apoyar ✨</h3>
<p>Importe: {amount if amount is not None else 'libre'}</p>
<p>(Demo) Cierra esta ventana y vuelve al chat.</p></body></html>"""

# ================== BOOTSTRAP ==================
def start_bot_in_thread():
    def runner():
        asyncio.set_event_loop(asyncio.new_event_loop())
        application = Application.builder().token(TOKEN).build()

        application.add_handler(CommandHandler("start",       cmd_start))
        application.add_handler(CommandHandler("studio",      cmd_studio))
        application.add_handler(CommandHandler("bindhere",    cmd_bindhere))
        application.add_handler(CommandHandler("liveon",      cmd_liveon))
        application.add_handler(CommandHandler("liveoff",     cmd_liveoff))
        application.add_handler(CommandHandler("announce",    cmd_announce))
        application.add_handler(CommandHandler("addprice",    cmd_addprice))
        application.add_handler(CommandHandler("listprices",  cmd_listprices))
        application.add_handler(CommandHandler("resetprices", cmd_resetprices))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_group_message))

        global BOT_APP
        BOT_APP = application
        application.run_polling(drop_pending_updates=True, stop_signals=None)

    t = threading.Thread(target=runner, name="bot_thread", daemon=True)
    t.start()

if __name__ == "__main__":
    assert TOKEN, "Falta TELEGRAM_TOKEN"
    start_bot_in_thread()
    # lanzar anunciador
    threading.Thread(target=announcer_loop, name="announcer", daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)
