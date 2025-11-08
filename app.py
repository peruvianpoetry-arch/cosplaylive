import os, json, threading, io, time
from pathlib import Path
from flask import Flask, request, Response

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# --- Opcional pagos (dejado listo; no usado en esta versión mínima) ----
import stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_placeholder")

# -------------------- Persistencia --------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "data.json"

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "admins": [],              # user_ids admins del bot
        "prices": [                # ejemplo inicial vacío (el admin los llenará)
        ],
        "model_name": "Cosplay Emma",
        "live": False,
        "group_id": None           # te lo guardo al primer uso
    }

def save_data(d): DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

DB = load_data()

# -------------------- Traducción --------------------
ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION", "true").lower() in ("1","true","yes")
MODEL_ID = int(os.getenv("MODEL_ID", "0") or 0)

if ENABLE_TRANSLATION:
    from deep_translator import GoogleTranslator
    def tr_to_es(txt: str) -> str:
        try:
            return GoogleTranslator(source='auto', target='es').translate(txt)
        except Exception:
            return txt
    def tr_to_de(txt: str) -> str:
        try:
            return GoogleTranslator(source='auto', target='de').translate(txt)
        except Exception:
            return txt
else:
    tr_to_es = tr_to_de = lambda s: s

# -------------------- Bot --------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

def app_singleton() -> Application:
    global _APP
    try:
        return _APP
    except NameError:
        _APP = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        return _APP

# ---- Helpers ----
def is_admin(user_id: int) -> bool:
    return user_id in DB["admins"]

def need_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user and update.effective_user.id
        if not uid or not is_admin(uid):
            await update.effective_chat.send_message("Solo admin.")
            return
        return await func(update, context)
    return wrapper

def kb_donaciones(user=None) -> InlineKeyboardMarkup:
    rows = []
    currency = os.getenv("CURRENCY", "EUR")
    base = os.getenv("BASE_URL", "").rstrip("/") + "/donar"
    uid = getattr(user, "id", "") if user else ""
    uname = getattr(user, "username", "") if user else ""
    def url_for(price):
        q = f"?amt={price}&c={currency}"
        if uid: q += f"&uid={uid}"
        if uname: q += f"&uname={uname}"
        return base + q
    # Botones a partir de DB
    for item in DB["prices"]:
        name = item["name"]; price = item["price"]
        rows.append([InlineKeyboardButton(f"{name} · {price} {currency}", url=url_for(price))])
    rows.append([InlineKeyboardButton("💝 Donar libre", url=(base + f"?c={currency}" + (f"&uid={uid}&uname={uname}" if uid else "")))])
    return InlineKeyboardMarkup(rows)

async def post_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    currency = os.getenv("CURRENCY", "EUR")
    title = DB.get("model_name", "Cosplay Emma")
    text = f"💖 Apoya a *{title}* y aparece en pantalla."
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones())

# -------- Anuncios al detectar LIVE ----------
async def on_group_live_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # VIDEO_CHAT_STARTED llega en grupos/supergrupos
    DB["live"] = True
    gid = update.effective_chat.id
    DB["group_id"] = gid
    save_data(DB)
    await post_menu(context, gid)

async def on_group_live_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    DB["live"] = False
    save_data(DB)
    await update.effective_chat.send_message("🔴 LIVE desactivado.")

# ------------- Comandos --------------
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"Tu user_id: {u.id}\nUsername: @{u.username or '—'}")

async def iamadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in DB["admins"]:
        DB["admins"].append(uid); save_data(DB)
    await update.message.reply_text("✅ Ya eres admin de este bot.")

@need_admin
async def setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usa: /setmodel Nombre del modelo")
        return
    DB["model_name"] = name; save_data(DB)
    await update.message.reply_text(f"OK. Modelo: {name}")

@need_admin
async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # formato: /addprice Nombre · 7  (el separador puede ser · o - o espacio)
    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Usa: /addprice Nombre · 7")
        return
    # Normalizar separadores
    for sep in ["·", "-", "|"]:
        raw = raw.replace(sep, " ")
    parts = [p for p in raw.split() if p]
    # último token debe ser precio
    try:
        price = float(parts[-1].replace(",", "."))
        name = " ".join(parts[:-1]).strip()
        if not name:
            raise ValueError
    except Exception:
        await update.message.reply_text("Usa: /addprice Nombre · 7")
        return
    DB["prices"] = [p for p in DB["prices"] if p["name"].lower()!=name.lower()]
    DB["prices"].append({"name": name, "price": price})
    save_data(DB)
    await update.message.reply_text(f"✅ Añadido: {name} · {price}")

@need_admin
async def delprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usa: /delprice Nombre")
        return
    before = len(DB["prices"])
    DB["prices"] = [p for p in DB["prices"] if p["name"].lower()!=name.lower()]
    save_data(DB)
    await update.message.reply_text("✅ Eliminado" if len(DB["prices"])<before else "No existía.")

@need_admin
async def listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DB["prices"]:
        await update.message.reply_text("Sin precios. Usa /addprice Nombre · 7")
        return
    lines = [f"- {p['name']} · {p['price']}" for p in sorted(DB["prices"], key=lambda x: x["name"].lower())]
    await update.message.reply_text("Precios:\n" + "\n".join(lines))

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=kb_donaciones(update.effective_user))

@need_admin
async def live_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    DB["live"] = True; save_data(DB)
    DB["group_id"] = update.effective_chat.id
    await update.message.reply_text("🟢 LIVE activado.")
    await post_menu(context, update.effective_chat.id)

@need_admin
async def live_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    DB["live"] = False; save_data(DB)
    await update.message.reply_text("🔴 LIVE desactivado.")

# ------------- Traducción de mensajes del grupo -------------
async def on_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Solo actuamos en el grupo, y si LIVE está activo (o lo prefieres SIEMPRE, cambia esta condición)
    if update.effective_chat.type not in ("group","supergroup"):
        return
    if not DB.get("group_id"):
        DB["group_id"] = update.effective_chat.id; save_data(DB)

    txt = update.effective_message.text or ""
    if not txt or txt.startswith("/"):
        return

    # Mostrar menú con baja frecuencia cuando hay actividad
    if DB.get("live"):
        # Al primer mensaje tras activarse, soltamos menú (ya se mandó en live_on)
        pass

    if not ENABLE_TRANSLATION:
        return

    sender = update.effective_user.id if update.effective_user else 0
    if sender == MODEL_ID and MODEL_ID != 0:
        # Mensaje de la modelo -> traducimos a alemán para la audiencia
        t = tr_to_de(txt)
        if t and t.strip() and t.strip() != txt.strip():
            await update.effective_chat.send_message(f"🗣️ (DE) {t}")
    else:
        # Mensaje de audiencia -> traducimos a ES para la modelo
        t = tr_to_es(txt)
        if t and t.strip() and t.strip() != txt.strip():
            await update.effective_chat.send_message(f"👀 (ES) {t}")

# -------------------- Flask (Studio mínimo) --------------------
web = Flask(__name__)

@web.get("/")
def home():
    return "CosplayLive bot OK"

@web.get("/studio")
def studio():
    return """<h3>Studio – Cosplay Emma</h3>
<p><a href="/overlay" target="_blank">Abrir Overlay</a></p>
<p><a href="/studio/ding">🔔 Probar sonido</a> (dummy OK)</p>"""

@web.get("/overlay")
def overlay():
    # Overlay simple (negro), sin SSE para no fallar
    return Response(
        "<html><body style='margin:0;background:#0b1220;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif'>Overlay listo</body></html>",
        mimetype="text/html"
    )

@web.get("/studio/ding")
def studio_ding():
    # No hacemos nada complejo: solo OK (antes te daba 500)
    return "ding ok"

# (Ruta /donar existe pero no creamos sesión Stripe si no hay 'amt' válido; evita 500)
@web.get("/donar")
def donate_page():
    amt = (request.args.get("amt") or "").strip()
    ccy = (request.args.get("c") or "EUR").strip().upper()
    if not (amt.replace(".","",1).isdigit() or amt.replace(",","",1).isdigit()):
        return "Monto inválido"
    return f"OK, {amt} {ccy} (demo)."

# -------------------- Lanzar polling --------------------
def start_polling():
    app = app_singleton()
    # Handlers
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("iamadmin", iamadmin))
    app.add_handler(CommandHandler("setmodel", setmodel))
    app.add_handler(CommandHandler("addprice", addprice))
    app.add_handler(CommandHandler("delprice", delprice))
    app.add_handler(CommandHandler("listprices", listprices))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("liveon", live_on))
    app.add_handler(CommandHandler("liveoff", live_off))

    # Videochat start/end en grupos -> auto LIVE
    app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_STARTED, on_group_live_start))
    app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_ENDED, on_group_live_end))

    # Mensajes normales del grupo -> traducción
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_group_text))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

def run():
    t = threading.Thread(target=start_polling, daemon=True)
    t.start()
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    run()
