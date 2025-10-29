# app.py — CosplayLive (24/7, Stripe + anuncios + traducción + edición por Telegram)
import os, sys, json, threading, logging, time, uuid
from datetime import datetime
from flask import Flask, request, jsonify, Response, redirect
import stripe
from deep_translator import GoogleTranslator

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ====== LOGGING (Render-friendly) ======
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL","INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)
log = logging.getLogger("cosplaylive")

# ====== ENV ======
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise SystemExit("❌ Falta TELEGRAM_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID","0"))
ADMINS = {int(x) for x in os.getenv("ADMINS","").split(",") if x.strip().isdigit()}
AUTO_TR = os.getenv("AUTO_TRANSLATE","true").lower() == "true"
MODEL_LANG = os.getenv("DONATION_LANG_MODEL","es")  # idioma de la modelo
CURRENCY = os.getenv("DONATION_CURRENCY","EUR").upper()
ANN_MIN = int(os.getenv("ANNOUNCE_EVERY_MIN","15"))
HOST = os.getenv("HOST","")
PORT = int(os.getenv("PORT","10000"))

# ====== Stripe ======
stripe.api_key = os.getenv("STRIPE_SECRET_KEY","")
WH_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET","")

# ====== Storage (persistente) ======
DATA_DIR = os.getenv("DATA_DIR","/data")
if not os.path.isdir(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

CATALOG_PATH = os.path.join(DATA_DIR, "catalog.json")
STATE_PATH   = os.path.join(DATA_DIR, "state.json")

DEFAULT_CATALOG = {
    "items":[
        {"key":"baile", "label":"💃 Baile", "amount":300, "desc":"Baile sensual (2 min)"},
        {"key":"topless", "label":"✨ Topless", "amount":500, "desc":"Topless breve"},
        {"key":"lenceria", "label":"🧷 Lencería", "amount":1000, "desc":"Probar lencería"},
        {"key":"meta", "label":"🎯 Meta Compartida", "amount":0, "desc":"Aporte libre a la meta"},
        {"key":"propina", "label":"💝 Propina libre", "amount":0, "desc":"Apoyo libre"}
    ],
    "goal":{"target":5000, "current":0, "title":"Show Especial al llegar a la meta"}
}

def load_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except:
        with open(path,"w",encoding="utf-8") as f: json.dump(default,f,ensure_ascii=False,indent=2)
        return default

def save_json(path, data):
    with open(path,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)

CATALOG = load_json(CATALOG_PATH, DEFAULT_CATALOG)
STATE   = load_json(STATE_PATH,   {"total":0,"donors":{}})

def money(amount_cents):
    return f"{amount_cents/100:.2f} {CURRENCY}"

# ====== Traducción ======
def tr(text, target):
    try:
        return GoogleTranslator(source="auto", target=target).translate(text)
    except Exception as e:
        log.warning(f"TR fail: {e}")
        return text

# ====== Flask (Stripe + health) ======
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot operativo"

def price_keyboard():
    rows=[]
    for it in CATALOG["items"]:
        if it.get("amount",0)>0:
            lbl = f'{it["label"]} – {money(it["amount"])}'
        else:
            lbl = f'{it["label"]} – libre'
        rows.append([InlineKeyboardButton(lbl, callback_data=f"buy:{it['key']}")])
    return InlineKeyboardMarkup(rows)

def catalog_text():
    lines=["🛒 *Donaciones y pedidos*"]
    for it in CATALOG["items"]:
        if it.get("amount",0)>0:
            lines.append(f'• {it["label"]}: *{money(it["amount"])}* – {it.get("desc","")}')
        else:
            lines.append(f'• {it["label"]}: *Libre* – {it.get("desc","")}')
    goal = CATALOG.get("goal",{})
    if goal.get("target",0)>0:
        lines.append(f'\n🎯 *Meta*: {money(goal["target"])} — Progreso: *{money(STATE.get("total",0))}*')
        lines.append(f'📝 {goal.get("title","")}')
    return "\n".join(lines)

def is_admin(user_id:int)->bool:
    return user_id in ADMINS

@web.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature","")
    try:
        event = stripe.Webhook.construct_event(payload, sig, WH_SECRET)
    except Exception as e:
        log.error(f"Webhook error: {e}")
        return ("",400)

    typ = event["type"]
    data = event["data"]["object"]
    log.info(f"✅ Evento Stripe: {typ}")

    if typ == "checkout.session.completed":
        amount = int(data.get("amount_total",0))
        uname  = (data.get("metadata") or {}).get("telegram_user","usuario")
        item   = (data.get("metadata") or {}).get("item_key","donacion")
        # acumular
        STATE["total"] = STATE.get("total",0) + amount
        donors = STATE.setdefault("donors",{})
        donors[uname] = donors.get(uname,0)+amount
        save_json(STATE_PATH, STATE)

        txt = f"🎉 ¡Nueva donación de *{uname}* → *{money(amount)}* por _{item}_!\n" \
              f"🎯 Progreso de meta: *{money(STATE['total'])}*"
        # Publicar al canal
        try:
            application.bot.send_message(chat_id=CHANNEL_ID, text=txt, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Error avisando en canal: {e}")

    return ("",200)

@web.get("/pay")
def create_checkout():
    """Crea sesión de Pago (Stripe Checkout) según item y monto libre opcional (cents)"""
    item = request.args.get("item","propina")
    amount_free = request.args.get("amount")     # en centavos si se usa libre
    user = request.args.get("u","anon")
    # buscar en catálogo
    sel = next((i for i in CATALOG["items"] if i["key"]==item), None)
    if not sel:
        return jsonify({"error":"item_not_found"}), 404
    amount = int(amount_free) if (amount_free and sel.get("amount",0)==0) else int(sel.get("amount",0))
    if amount <= 0:
        # sin monto, obligar amount en query
        return jsonify({"error":"amount_required"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data":{
                    "currency": CURRENCY.lower(),
                    "unit_amount": amount,
                    "product_data":{"name": f'{sel["label"]} ({item})'}
                },
                "quantity":1
            }],
            success_url=f"{HOST}/thanks?ok=1",
            cancel_url=f"{HOST}/thanks?ok=0",
            metadata={"telegram_user": user, "item_key": item}
        )
        return redirect(session.url, code=303)
    except Exception as e:
        log.error(f"Stripe create error: {e}")
        return jsonify({"error":"stripe_error"}), 500

@web.get("/thanks")
def thanks():
    return "✅ Pago recibido (modo test). Gracias."

def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ====== TELEGRAM BOT ======

async def send_menu(context: ContextTypes.DEFAULT_TYPE, intro:str=""):
    txt = (intro+"\n\n" if intro else "") + catalog_text()
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID, text=txt, reply_markup=price_keyboard(), parse_mode="Markdown"
        )
        log.info("📣 Anuncio automático enviado al canal")
    except Exception as e:
        log.error(f"Anuncio falló: {e}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("🤖 Bot activo. Usa /menu para ver donaciones.")
    await send_menu(context)

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(catalog_text(), reply_markup=price_keyboard(), parse_mode="Markdown")

async def buy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback de botones."""
    q = update.callback_query
    await q.answer()
    data = q.data  # buy:key
    _, key = data.split(":",1)
    sel = next((i for i in CATALOG["items"] if i["key"]==key), None)
    if not sel:
        return await q.edit_message_text("Item no disponible.")
    # Si es libre, mostramos ejemplos con links (5/10/20 €)
    if sel.get("amount",0)==0:
        uid = update.effective_user.username or f"id{update.effective_user.id}"
        base = f"{HOST}/pay?item={key}&u={uid}"
        kb = [
            [InlineKeyboardButton("💝 Donar 5",  url=f"{base}&amount=500")],
            [InlineKeyboardButton("💝 Donar 10", url=f"{base}&amount=1000")],
            [InlineKeyboardButton("💝 Donar 20", url=f"{base}&amount=2000")],
        ]
        await q.edit_message_text(
            "Elige un monto de donación libre:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        uid = update.effective_user.username or f"id{update.effective_user.id}"
        url = f"{HOST}/pay?item={key}&u={uid}"
        await q.edit_message_text(
            f"🔗 Abre el pago en Stripe para *{sel['label']}* → {money(sel['amount'])}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Pagar ahora", url=url)]]),
            parse_mode="Markdown"
        )

async def tick_announce(context: ContextTypes.DEFAULT_TYPE):
    await send_menu(context)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde en privado (DM) y en discusiones de grupos; traduce si procede."""
    msg = update.message or update.channel_post
    if not msg: return
    user = update.effective_user
    text = msg.text or msg.caption or ""
    if not text: return

    # Traducción automática: usuario -> idioma de la modelo
    reply_lines = []
    if AUTO_TR and text.strip():
        to_model = tr(text, MODEL_LANG)
        if to_model != text:
            reply_lines.append(f"🗣️ Traducción → {MODEL_LANG}: {to_model}")

    if reply_lines:
        await msg.reply_text("\n".join(reply_lines))

async def additem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return await update.effective_message.reply_text("Solo admins.")
    # formato: /additem key|Label|amount_cents|desc
    try:
        raw = " ".join(context.args)
        key, label, amount, desc = [t.strip() for t in raw.split("|",3)]
        amount = int(amount)
        CATALOG["items"] = [i for i in CATALOG["items"] if i["key"]!=key]
        CATALOG["items"].append({"key":key,"label":label,"amount":amount,"desc":desc})
        save_json(CATALOG_PATH, CATALOG)
        await update.effective_message.reply_text("✅ Item agregado/actualizado.")
    except Exception as e:
        await update.effective_message.reply_text("Uso: /additem key|Label|amount_cents|desc")

async def delitem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return await update.effective_message.reply_text("Solo admins.")
    try:
        key = " ".join(context.args).strip()
        before = len(CATALOG["items"])
        CATALOG["items"] = [i for i in CATALOG["items"] if i["key"]!=key]
        save_json(CATALOG_PATH, CATALOG)
        await update.effective_message.reply_text("✅ Item eliminado." if len(CATALOG["items"])<before else "No existía.")
    except:
        await update.effective_message.reply_text("Uso: /delitem key")

async def setgoal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return await update.effective_message.reply_text("Solo admins.")
    try:
        target = int(context.args[0])
        title  = " ".join(context.args[1:]) or DEFAULT_CATALOG["goal"]["title"]
        CATALOG["goal"]={"target":target,"title":title}
        save_json(CATALOG_PATH, CATALOG)
        await update.effective_message.reply_text("✅ Meta actualizada.")
    except:
        await update.effective_message.reply_text("Uso: /setgoal TARGET_CENTS Título opcional")

async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(context, "📣 Recordatorio de donaciones")

# ====== MAIN ======
def build_app():
    app = ApplicationBuilder().token(TOKEN).build()
    # comandos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu",  menu_cmd))
    app.add_handler(CommandHandler("additem", additem_cmd))
    app.add_handler(CommandHandler("delitem", delitem_cmd))
    app.add_handler(CommandHandler("setgoal", setgoal_cmd))
    app.add_handler(CommandHandler("announce", announce_cmd))
    # texto general
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    # callbacks de botones
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, lambda *_: None))  # no-op
    app.add_handler(MessageHandler(filters.ALL, lambda *_: None))  # catch-all ligero

    # job anuncios automáticos
    app.job_queue.run_repeating(tick_announce, interval=ANN_MIN*60, first=15)
    return app

# Servidor web Flask en hilo aparte
threading.Thread(target=run_web, daemon=True).start()

application = build_app()

if __name__ == "__main__":
    log.info("🚀 Iniciando bot (polling 24/7)…")
    application.run_polling(drop_pending_updates=True)
