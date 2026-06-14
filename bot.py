"""
bot.py
Главный файл бота: запуск, онбординг пользователя (депозит, риск)
через кнопки и пошаговый диалог.
"""

import os
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import storage

# --- Настройка логов (видно в Railway, удобно для отладки) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Токен бота берём из переменной окружения (настроим в Railway) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# --- Состояния для пошагового диалога (ConversationHandler) ---
ASK_DEPOSIT, ASK_RISK = range(2)


# ============================================================
#  /start — приветствие и начало онбординга
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    if user and user["deposit"] and user["risk_percent"]:
        # Пользователь уже настроен — показываем текущие настройки
        await update.message.reply_text(
            f"👋 Привет! Ты уже настроен:\n\n"
            f"💰 Депозит: {user['deposit']} USDT\n"
            f"⚠️ Риск на сделку: {user['risk_percent']}%\n"
            f"📊 Монеты: {', '.join(user['coins'])}\n\n"
            f"Изменить настройки — команда /settings"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Я бот сигналов по фьючерсам Binance.\n\n"
        "Сначала настроим основные параметры.\n\n"
        "💰 Какой у тебя депозит на Binance Futures? "
        "Напиши число в USDT (например: 1000)"
    )
    return ASK_DEPOSIT


# ============================================================
#  Шаг 1: получаем депозит
# ============================================================
async def ask_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")

    try:
        deposit = float(text)
        if deposit <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введи положительное число, например: 1000"
        )
        return ASK_DEPOSIT

    user_id = update.effective_user.id
    storage.set_deposit(user_id, deposit)

    # Показываем кнопки для выбора риска
    keyboard = [
        [
            InlineKeyboardButton("0.5%", callback_data="risk_0.5"),
            InlineKeyboardButton("1%", callback_data="risk_1"),
            InlineKeyboardButton("2%", callback_data="risk_2"),
        ],
        [InlineKeyboardButton("Указать своё значение", callback_data="risk_custom")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"✅ Депозит сохранён: {deposit} USDT\n\n"
        f"⚠️ Теперь выбери риск на одну сделку "
        f"(% от депозита, который ты готов потерять, если сделка пойдёт в минус):",
        reply_markup=reply_markup,
    )
    return ASK_RISK


# ============================================================
#  Шаг 2: получаем риск через кнопки
# ============================================================
async def risk_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data  # например "risk_1" или "risk_custom"

    if data == "risk_custom":
        await query.edit_message_text(
            "✏️ Напиши своё значение риска в процентах (например: 1.5)"
        )
        return ASK_RISK

    # data вида "risk_1" -> берём число после "risk_"
    risk_value = float(data.split("_")[1])
    storage.set_risk(user_id, risk_value)

    await finish_onboarding(query, context, user_id, risk_value, is_callback=True)
    return ConversationHandler.END


async def risk_custom_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка случая, когда пользователь вводит риск текстом."""
    text = update.message.text.strip().replace(",", ".").replace("%", "")

    try:
        risk_value = float(text)
        if not (0 < risk_value <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введи число от 0 до 100, например: 1.5"
        )
        return ASK_RISK

    user_id = update.effective_user.id
    storage.set_risk(user_id, risk_value)

    await finish_onboarding(update, context, user_id, risk_value, is_callback=False)
    return ConversationHandler.END


async def finish_onboarding(update_or_query, context, user_id, risk_value, is_callback: bool):
    """Финальное сообщение после завершения онбординга."""
    user = storage.get_user(user_id)

    text = (
        f"✅ Готово! Твои настройки:\n\n"
        f"💰 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск на сделку: {risk_value}%\n"
        f"📊 Отслеживаемые монеты: {', '.join(user['coins'])}\n\n"
        f"Команды:\n"
        f"/settings — посмотреть/изменить настройки\n"
        f"/deposit — изменить депозит\n"
        f"/risk — изменить риск\n\n"
        f"🔍 Анализ рынка пока не подключён — это следующий шаг."
    )

    if is_callback:
        await update_or_query.edit_message_text(text)
    else:
        await update_or_query.message.reply_text(text)


# ============================================================
#  /settings — посмотреть текущие настройки
# ============================================================
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    if not user or not user["deposit"]:
        await update.message.reply_text(
            "Ты ещё не настроен. Напиши /start чтобы начать."
        )
        return

    await update.message.reply_text(
        f"⚙️ Твои настройки:\n\n"
        f"💰 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск на сделку: {user['risk_percent']}%\n"
        f"📊 Монеты: {', '.join(user['coins'])}\n\n"
        f"/deposit — изменить депозит\n"
        f"/risk — изменить риск"
    )


# ============================================================
#  /deposit — изменить депозит (без полного онбординга)
# ============================================================
async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "💰 Введи новый размер депозита в USDT (например: 1500)"
    )
    return ASK_DEPOSIT_ONLY


async def deposit_only_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")

    try:
        deposit = float(text)
        if deposit <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи положительное число, например: 1000")
        return ASK_DEPOSIT_ONLY

    user_id = update.effective_user.id
    storage.set_deposit(user_id, deposit)

    await update.message.reply_text(f"✅ Депозит обновлён: {deposit} USDT")
    return ConversationHandler.END


# ============================================================
#  /risk — изменить риск (без полного онбординга)
# ============================================================
async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton("0.5%", callback_data="setrisk_0.5"),
            InlineKeyboardButton("1%", callback_data="setrisk_1"),
            InlineKeyboardButton("2%", callback_data="setrisk_2"),
        ],
        [InlineKeyboardButton("Указать своё значение", callback_data="setrisk_custom")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚠️ Выбери новый риск на сделку:",
        reply_markup=reply_markup,
    )


async def risk_command_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data  # "setrisk_1" или "setrisk_custom"

    if data == "setrisk_custom":
        await query.edit_message_text("✏️ Напиши своё значение риска в процентах (например: 1.5)")
        return ASK_RISK_ONLY

    risk_value = float(data.split("_")[1])
    storage.set_risk(user_id, risk_value)

    await query.edit_message_text(f"✅ Риск обновлён: {risk_value}%")
    return ConversationHandler.END


async def risk_only_custom_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".").replace("%", "")

    try:
        risk_value = float(text)
        if not (0 < risk_value <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи число от 0 до 100, например: 1.5")
        return ASK_RISK_ONLY

    user_id = update.effective_user.id
    storage.set_risk(user_id, risk_value)

    await update.message.reply_text(f"✅ Риск обновлён: {risk_value}%")
    return ConversationHandler.END


# Дополнительные состояния для /deposit и /risk команд
ASK_DEPOSIT_ONLY, ASK_RISK_ONLY = range(2, 4)


# ============================================================
#  /cancel — выйти из диалога
# ============================================================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. Напиши /start чтобы начать заново.")
    return ConversationHandler.END


# ============================================================
#  Запуск бота
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Переменная окружения BOT_TOKEN не задана! "
            "Добавь её в настройках Railway."
        )

    storage.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Онбординг при /start
    onboarding_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_DEPOSIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_deposit)],
            ASK_RISK: [
                CallbackQueryHandler(risk_button, pattern="^risk_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, risk_custom_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Изменение депозита через /deposit
    deposit_handler = ConversationHandler(
        entry_points=[CommandHandler("deposit", deposit_command)],
        states={
            ASK_DEPOSIT_ONLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_only_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Изменение риска через /risk
    risk_handler = ConversationHandler(
        entry_points=[CommandHandler("risk", risk_command)],
        states={
            ASK_RISK_ONLY: [
                CallbackQueryHandler(risk_command_button, pattern="^setrisk_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, risk_only_custom_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    # Кнопки /risk обрабатываются сразу при первом вызове (без MessageHandler входа)
    app.add_handler(CallbackQueryHandler(risk_command_button, pattern="^setrisk_"))

    app.add_handler(onboarding_handler)
    app.add_handler(deposit_handler)
    app.add_handler(risk_handler)
    app.add_handler(CommandHandler("settings", settings))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
