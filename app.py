import os
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRANSLATE_TO = os.getenv("TRANSLATE_TO", "de")

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Cosplay Live Bot is running!"

# --- Telegram bot setup ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot activo y escuchando tus mensajes!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await update.message.reply_text(f"📨 Recibido: {text}")

def start_bot():
    if not TOKEN:
        print("⚠️ No hay TOKEN configurado")
        return

    app_tg = ApplicationBuilder().token(TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    print("🤖 Telegram bot iniciado (polling)...")
    app_tg.run_polling()

if __name__ == '__main__':
    import threading
    threading.Thread(target=start_bot).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

