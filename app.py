# ============================================================
#  BLOQUE DE ANUNCIOS AUTOMÁTICOS (LIVEON / LIVEOFF)
# ============================================================
from telegram.ext import ContextTypes  # por si no estaba importado

# Mensaje que el bot enviará cada X minutos cuando el show está en vivo
AUTO_AD_TEXT = "🔥 Unterstütze die Show mit einem Klick!\nDas Model bedankt sich live."

async def announce_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job que envía el anuncio automático al chat del show.
    Se usa el chat_id que está guardado en el propio job.
    """
    chat_id = context.job.chat_id
    try:
        await context.bot.send_message(chat_id=chat_id, text=AUTO_AD_TEXT)
    except Exception as e:
        print(f"[announce_job] Error enviando anuncio: {e}")


async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Activa anuncios automáticos en ESTE chat.
    No hace falta /bindhere, solo escribir /liveon en la sala de chat de la modelo.
    """
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    job_name = f"auto_ads_{chat_id}"

    # Cancelar jobs antiguos de este mismo chat (por si se llama dos veces)
    try:
        existing = context.job_queue.get_jobs_by_name(job_name)
    except Exception as e:
        print(f"[cmd_liveon] Error obteniendo jobs: {e}")
        existing = []

    for job in existing:
        job.schedule_removal()

    # Crear nuevo job que manda el anuncio cada 5 minutos
    try:
        context.job_queue.run_repeating(
            announce_job,
            interval=300,      # 300 segundos = 5 minutos
            first=0,           # primer anuncio inmediatamente
            chat_id=chat_id,
            name=job_name,
        )
    except Exception as e:
        print(f"[cmd_liveon] Error creando job: {e}")
        await update.message.reply_text("⚠️ No pude activar los anuncios automáticos.")
        return

    await update.message.reply_text(
        "✅ Ankündigungen aktiviert.\n"
        "Der Bot postet jetzt alle 5 Minuten eine Nachricht in diesem Chat."
    )


async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Desactiva anuncios automáticos en ESTE chat.
    """
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    job_name = f"auto_ads_{chat_id}"

    try:
        jobs = context.job_queue.get_jobs_by_name(job_name)
    except Exception as e:
        print(f"[cmd_liveoff] Error obteniendo jobs: {e}")
        jobs = []

    if not jobs:
        await update.message.reply_text("ℹ️ Es sind keine automatischen Ankündigungen aktiv.")
        return

    for job in jobs:
        job.schedule_removal()

    await update.message.reply_text("🛑 Ankündigungen wurden für diesen Chat deaktiviert.")
# ============================================================
#  FIN DEL BLOQUE DE ANUNCIOS AUTOMÁTICOS
# ============================================================
