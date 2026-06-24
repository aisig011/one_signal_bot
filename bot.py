"""
bot.py
Главный файл бота: запуск, онбординг (депозит, риск), главное меню
с кнопками, фоновое сканирование рынка.
"""

import os
import logging

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
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
import market
import indicators
import signals
import scheduler

# --- Настройка логов (видно в Railway, удобно для отладки) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Токен бота берём из переменной окружения (настроим в Railway) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# --- Состояния диалогов ---
ASK_DEPOSIT, ASK_RISK = range(2)
ASK_DEPOSIT_ONLY, ASK_RISK_ONLY = range(2, 4)
ASK_COINS = 4

# --- Главное меню (постоянная клавиатура) ---
MAIN_MENU_BUTTONS = [
    ["🔍 Искать сигнал", "📊 Проверить монету"],
    ["⚙️ Настройки", "📋 Мои монеты"],
]
MAIN_MENU = ReplyKeyboardMarkup(MAIN_MENU_BUTTONS, resize_keyboard=True)

TIMEFRAMES = {"15m": "15m", "1h": "1h", "4h": "4h"}


def pd_isna(value) -> bool:
    """Небольшая обёртка, чтобы не импортировать pandas напрямую везде."""
    import pandas as pd
    return pd.isna(value)


# ============================================================
#  /start — приветствие и онбординг
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    if user and user["deposit"] and user["risk_percent"]:
        await update.message.reply_text(
            f"👋 Привет! Ты уже настроен:\n\n"
            f"💰 Депозит: {user['deposit']} USDT\n"
            f"⚠️ Риск на сделку: {user['risk_percent']}%\n"
            f"📊 Монеты: {', '.join(user['coins'])}\n\n"
            f"Используй меню ниже 👇",
            reply_markup=MAIN_MENU,
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
#  Онбординг: депозит -> риск
# ============================================================
async def ask_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")

    try:
        deposit = float(text)
        if deposit <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи положительное число, например: 1000")
        return ASK_DEPOSIT

    user_id = update.effective_user.id
    storage.set_deposit(user_id, deposit)

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


async def risk_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    if data == "risk_custom":
        await query.edit_message_text("✏️ Напиши своё значение риска в процентах (например: 1.5)")
        return ASK_RISK

    risk_value = float(data.split("_")[1])
    storage.set_risk(user_id, risk_value)

    await finish_onboarding(query, context, user_id, risk_value, is_callback=True)
    return ConversationHandler.END


async def risk_custom_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".").replace("%", "")

    try:
        risk_value = float(text)
        if not (0 < risk_value <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи число от 0 до 100, например: 1.5")
        return ASK_RISK

    user_id = update.effective_user.id
    storage.set_risk(user_id, risk_value)

    await finish_onboarding(update, context, user_id, risk_value, is_callback=False)
    return ConversationHandler.END


async def finish_onboarding(update_or_query, context, user_id, risk_value, is_callback: bool):
    user = storage.get_user(user_id)

    text = (
        f"✅ Готово! Твои настройки:\n\n"
        f"💰 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск на сделку: {risk_value}%\n"
        f"📊 Отслеживаемые монеты: {', '.join(user['coins'])}\n\n"
        f"🔍 Бот будет сам сканировать рынок каждые 30 минут и присылать "
        f"сигналы, когда они появятся.\n\n"
        f"Используй меню ниже 👇"
    )

    if is_callback:
        await update_or_query.edit_message_text(text)
        await update_or_query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    else:
        await update_or_query.message.reply_text(text, reply_markup=MAIN_MENU)


# ============================================================
#  Настройки (кнопка "⚙️ Настройки")
# ============================================================
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    if not user or not user["deposit"]:
        await update.message.reply_text("Ты ещё не настроен. Напиши /start чтобы начать.")
        return

    keyboard = [
        [InlineKeyboardButton("💰 Изменить депозит", callback_data="menu_deposit")],
        [InlineKeyboardButton("⚠️ Изменить риск", callback_data="menu_risk")],
        [InlineKeyboardButton("📋 Изменить список монет", callback_data="menu_coins")],
    ]

    await update.message.reply_text(
        f"⚙️ *Твои настройки:*\n\n"
        f"💰 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск на сделку: {user['risk_percent']}%\n"
        f"📊 Монеты: {', '.join(user['coins'])}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
#  Изменение депозита
# ============================================================
async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("💰 Введи новый размер депозита в USDT (например: 1500)")
    return ASK_DEPOSIT_ONLY


async def deposit_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("💰 Введи новый размер депозита в USDT (например: 1500)")
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

    await update.message.reply_text(f"✅ Депозит обновлён: {deposit} USDT", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ============================================================
#  Изменение риска
# ============================================================
async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_risk_keyboard(update.message)


async def risk_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton("0.5%", callback_data="setrisk_0.5"),
            InlineKeyboardButton("1%", callback_data="setrisk_1"),
            InlineKeyboardButton("2%", callback_data="setrisk_2"),
        ],
        [InlineKeyboardButton("Указать своё значение", callback_data="setrisk_custom")],
    ]
    await query.edit_message_text("⚠️ Выбери новый риск на сделку:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_RISK_ONLY


async def send_risk_keyboard(message):
    keyboard = [
        [
            InlineKeyboardButton("0.5%", callback_data="setrisk_0.5"),
            InlineKeyboardButton("1%", callback_data="setrisk_1"),
            InlineKeyboardButton("2%", callback_data="setrisk_2"),
        ],
        [InlineKeyboardButton("Указать своё значение", callback_data="setrisk_custom")],
    ]
    await message.reply_text("⚠️ Выбери новый риск на сделку:", reply_markup=InlineKeyboardMarkup(keyboard))


async def risk_command_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    if data == "setrisk_custom":
        await query.edit_message_text("✏️ Напиши своё значение риска в процентах (например: 1.5)")
        return ASK_RISK_ONLY

    risk_value = float(data.split("_")[1])
    storage.set_risk(user_id, risk_value)

    await query.edit_message_text(f"✅ Риск обновлён: {risk_value}%")
    await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
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

    await update.message.reply_text(f"✅ Риск обновлён: {risk_value}%", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ============================================================
#  Изменение списка монет
# ============================================================
async def coins_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    await query.edit_message_text(
        f"📋 Текущий список монет: {', '.join(user['coins'])}\n\n"
        f"Напиши новый список через запятую, например:\n"
        f"BTC, ETH, SOL, BNB, XRP, DOGE"
    )
    return ASK_COINS


async def coins_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    coins = [c.strip().upper() for c in text.split(",") if c.strip()]

    if not coins:
        await update.message.reply_text("⚠️ Список не может быть пустым. Напиши хотя бы одну монету.")
        return ASK_COINS

    user_id = update.effective_user.id
    storage.set_coins(user_id, coins)

    await update.message.reply_text(
        f"✅ Список монет обновлён: {', '.join(coins)}",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


# ============================================================
#  📋 Мои монеты (быстрый просмотр)
# ============================================================
async def my_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    if not user or not user["deposit"]:
        await update.message.reply_text("Ты ещё не настроен. Напиши /start чтобы начать.")
        return

    await update.message.reply_text(
        f"📋 Отслеживаемые монеты:\n{', '.join(user['coins'])}\n\n"
        f"Изменить список — кнопка ⚙️ Настройки → 📋 Изменить список монет"
    )


# ============================================================
#  📊 Проверить монету
# ============================================================
async def check_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает на кнопку '📊 Проверить монету' — просит написать тикер."""
    await update.message.reply_text(
        "Напиши название монеты, например: BTC, ETH, SOL\n\n"
        "Или используй команду: /check BTC"
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args

    if not args:
        await update.message.reply_text("Напиши монету после команды, например:\n/check BTC")
        return

    await do_check(update, args[0])


async def do_check(update: Update, coin: str) -> None:
    coin = coin.upper()
    symbol = market.get_symbol(coin)

    await update.message.reply_text(f"⏳ Загружаю данные по {coin}...")

    try:
        current_price = market.get_current_price(symbol)
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Не удалось получить данные по {coin}.\n"
            f"Проверь правильность названия монеты (например BTC, ETH, SOL).\n\n"
            f"Ошибка: {e}"
        )
        return

    text = f"📊 *{coin}/USDT*\n💰 Текущая цена: {current_price}\n\n"

    for label, interval in TIMEFRAMES.items():
        try:
            df = market.get_klines(symbol, interval, limit=250)
            summary = indicators.summarize(df)

            trend_emoji = {
                "bullish": "🟢 восходящий",
                "bearish": "🔴 нисходящий",
                "flat": "⚪ флэт",
            }[summary["trend"]]

            macd_emoji = {
                "bullish": "🟢 бычий",
                "bearish": "🔴 медвежий",
                "neutral": "⚪ нейтральный",
            }[summary["macd_signal"]]

            rsi_value = summary["rsi"]
            rsi_str = f"{rsi_value:.1f}" if not pd_isna(rsi_value) else "н/д"

            text += (
                f"⏱ *{label}*\n"
                f"  Тренд: {trend_emoji}\n"
                f"  RSI: {rsi_str} ({summary['rsi_state']})\n"
                f"  MACD: {macd_emoji}\n\n"
            )
        except Exception as e:
            text += f"⏱ *{label}*: ошибка загрузки ({e})\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
#  🔍 Искать сигнал — проверка по всем монетам пользователя
# ============================================================
async def search_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    if not user or not user["deposit"] or not user["risk_percent"]:
        await update.message.reply_text("⚠️ Сначала настрой депозит и риск через /start")
        return

    await update.message.reply_text(
        f"⏳ Анализирую монеты: {', '.join(user['coins'])}...\n"
        f"Это может занять немного времени."
    )

    found_any = False

    for coin in user["coins"]:
        coin = coin.strip().upper()
        if not coin:
            continue

        try:
            result = signals.find_signal(
                coin=coin,
                deposit=user["deposit"],
                risk_percent=user["risk_percent"],
                min_rr=2.0,
            )
        except Exception as e:
            logger.warning(f"Ошибка анализа {coin}: {e}")
            continue

        if result is None:
            continue

        found_any = True
        text = scheduler.format_signal_message(result, user)
        await update.message.reply_text(text, parse_mode="Markdown")

    if not found_any:
        await update.message.reply_text(
            "📭 Сейчас сигналов нет ни по одной из твоих монет.\n\n"
            "Это нормально — бот фильтрует слабые сетапы (нет тренда, "
            "нет точки входа, или R/R хуже 1:2).\n\n"
            "Бот продолжит сканировать рынок в фоне и пришлёт сигнал, "
            "как только он появится."
        )


async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /signal BTC — проверка одной конкретной монеты."""
    args = context.args

    if not args:
        # Без аргумента — ищем по всем монетам пользователя
        await search_signal(update, context)
        return

    user_id = update.effective_user.id
    user = storage.get_user(user_id)

    if not user or not user["deposit"] or not user["risk_percent"]:
        await update.message.reply_text("⚠️ Сначала настрой депозит и риск через /start")
        return

    coin = args[0].upper()

    await update.message.reply_text(f"⏳ Анализирую {coin}...")

    try:
        result = signals.find_signal(
            coin=coin,
            deposit=user["deposit"],
            risk_percent=user["risk_percent"],
            min_rr=2.0,
        )
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Не удалось проанализировать {coin}.\n"
            f"Проверь правильность названия монеты.\n\nОшибка: {e}"
        )
        return

    if result is None:
        await update.message.reply_text(
            f"📭 По {coin} сейчас нет сигнала.\n\n"
            f"Либо нет чёткого тренда на 4h, либо нет точки входа на 1h, "
            f"либо R/R хуже 1:2."
        )
        return

    text = scheduler.format_signal_message(result, user)
    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
#  Обработчик обычных текстовых сообщений (кнопки главного меню
#  + ручной ввод тикера для проверки монеты)
# ============================================================
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text == "🔍 Искать сигнал":
        await search_signal(update, context)
        return

    if text == "📊 Проверить монету":
        await check_prompt(update, context)
        return

    if text == "⚙️ Настройки":
        await settings(update, context)
        return

    if text == "📋 Мои монеты":
        await my_coins(update, context)
        return

    # Если это похоже на тикер монеты (короткое слово без пробелов,
    # буквы/цифры) — пробуем как /check
    if text.isalpha() and 2 <= len(text) <= 10:
        await do_check(update, text)
        return

    await update.message.reply_text(
        "Не понял команду. Используй меню ниже 👇",
        reply_markup=MAIN_MENU,
    )


# ============================================================
#  /cancel — выйти из диалога
# ============================================================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ============================================================
#  Обработчик кнопки "Вошёл в сделку"
# ============================================================
async def entered_trade_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # callback_data вида "entered_123"
    try:
        signal_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        return

    sig = storage.get_pending_signal(signal_id)
    if sig is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠️ Не удалось найти данные сигнала (возможно, он устарел).")
        return

    # Сохраняем как активную сделку для отслеживания
    storage.add_active_trade(
        sig["user_id"], sig["coin"], sig["symbol"], sig["direction"],
        sig["entry_price"], sig["stop_loss"], sig["take_profit_1"],
    )

    # Убираем кнопку и подтверждаем
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"✅ Сделка {sig['coin']}/USDT {sig['direction']} взята в отслеживание.\n"
        f"Я пришлю уведомление, когда цена достигнет 🎯 тейк-профита или 🛑 стоп-лосса."
    )


# ============================================================
#  Глобальный обработчик ошибок — чтобы ошибки в job_queue
#  (фоновом сканировании) не "проглатывались" молча
# ============================================================
async def error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)


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

    # Изменение депозита через /deposit ИЛИ кнопку настроек
    deposit_handler = ConversationHandler(
        entry_points=[
            CommandHandler("deposit", deposit_command),
            CallbackQueryHandler(deposit_menu_button, pattern="^menu_deposit$"),
        ],
        states={
            ASK_DEPOSIT_ONLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_only_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Изменение риска через /risk ИЛИ кнопку настроек
    risk_handler = ConversationHandler(
        entry_points=[
            CommandHandler("risk", risk_command),
            CallbackQueryHandler(risk_menu_button, pattern="^menu_risk$"),
        ],
        states={
            ASK_RISK_ONLY: [
                CallbackQueryHandler(risk_command_button, pattern="^setrisk_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, risk_only_custom_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Изменение списка монет через кнопку настроек
    coins_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(coins_menu_button, pattern="^menu_coins$")],
        states={
            ASK_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, coins_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(onboarding_handler)
    app.add_handler(deposit_handler)
    app.add_handler(risk_handler)
    app.add_handler(coins_handler)

    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("signal", signal_command))

    # Кнопка "Вошёл в сделку" под сигналом
    app.add_handler(CallbackQueryHandler(entered_trade_button, pattern="^entered_"))

    # Обработчик кнопок главного меню и текстовых сообщений — последним,
    # чтобы не перехватывать диалоги выше
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)

    # --- Фоновое сканирование рынка ---
    job_queue = app.job_queue
    job_queue.run_repeating(
        scheduler.scan_market,
        interval=scheduler.SCAN_INTERVAL_SECONDS,
        first=10,  # первая проверка через 10 секунд после запуска
    )
    # --- Отслеживание активных сделок (TP/SL) каждые 2 минуты ---
    job_queue.run_repeating(
        scheduler.check_active_trades,
        interval=120,
        first=30,
    )

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
