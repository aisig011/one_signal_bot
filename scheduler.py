"""
scheduler.py
Фоновая задача: каждые 30 минут проверяет всех настроенных пользователей
по их списку монет, ищет сигналы и присылает их в Telegram.
"""

import datetime
import logging
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import storage
import signals
import market

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 30 * 60  # 30 минут

# --- Рабочее окно ---
WORK_TIMEZONE = "Europe/Kyiv"
WORK_START = datetime.time(8, 30)
WORK_END = datetime.time(22, 0)


def _is_working_hours() -> bool:
    try:
        now = datetime.datetime.now(ZoneInfo(WORK_TIMEZONE))
    except Exception as e:
        logger.error(f"_is_working_hours: ошибка ({e}). Окно НЕ применяю.")
        return True
    return WORK_START <= now.time() <= WORK_END


# --- Лимиты экспозиции (личка бота) ---
MAX_ACTIVE_TRADES = 3
MAX_SIGNALS_PER_SCAN = 2
MAX_SAME_DIRECTION = 3

# --- Бесплатный канал (витрина) ---
CHANNEL_ID = -1004312355023          # приватный канал ONE SIGNAL (бот = админ)
CHANNEL_BOT_USERNAME = "one_s1gnal"  # для CTA в конце поста
CHANNEL_MIN_QUALITY_RATIO = 0.75     # только 🔥
CHANNEL_MIN_VOLUME = 1.0             # объём выше среднего (в личке хватает 0.7)
CHANNEL_MAX_PER_DAY = 2              # не больше 2 сигналов в день
CHANNEL_COIN_COOLDOWN_H = 6          # одна монета не чаще раза в 6 часов
CHANNEL_LEVERAGE = "x5–10"

CHANNEL_COINS = [
    # Только ликвидные монеты. Отобраны по реальной статистике 30 сделок:
    # мелкие альты (HYPE, INJ, TIA, ZEC, APT, OP, NEAR, SUI, ARB) дали
    # 11 сделок и НОЛЬ побед — у них шум больше, стопы выбивает чаще.
    # Ликвидные за тот же период: 19 сделок, винрейт 37%, +13.15 USDT.
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA",
    "AVAX", "LINK", "DOT", "LTC", "TRX", "ATOM",
]



# ============================================================
#  Проверка достижения уровней по СВЕЧАМ (а не по текущей цене)
# ============================================================

# Сколько последних 5-минутных свечей проверять.
# Задачи ходят раз в 2-5 минут, берём запас: 6 свечей = 30 минут истории.
# Так прокол уровня не потеряется, даже если бот на пару циклов задержался.
LEVEL_CHECK_CANDLES = 6


def _check_levels_by_candles(symbol: str, direction: str,
                             take_profit: float, stop_loss: float) -> str | None:
    """
    Проверяет, задела ли цена TP или SL, по МАКСИМУМАМ/МИНИМУМАМ свечей.

    Почему не по текущей цене: бот смотрит рынок раз в несколько минут.
    Если цена ткнула уровень между проверками и вернулась обратно —
    текущая цена этого уже не покажет, и сделка зависнет «открытой»
    навсегда. Именно так статистика теряла сработавшие стопы.

    Возвращает "TP", "SL" или None.
    Если задеты оба уровня в одном окне — возвращает SL (консервативно:
    считаем худший исход, чтобы не завышать винрейт).
    """
    try:
        df = market.get_klines(symbol, "5m", limit=LEVEL_CHECK_CANDLES)
    except Exception as e:
        logger.warning(f"_check_levels_by_candles: не удалось получить свечи {symbol}: {e}")
        return None

    if df is None or len(df) == 0:
        return None

    high = float(df["high"].max())
    low = float(df["low"].min())

    if direction == "LONG":
        hit_tp = high >= take_profit
        hit_sl = low <= stop_loss
    else:  # SHORT
        hit_tp = low <= take_profit
        hit_sl = high >= stop_loss

    if hit_sl:
        return "SL"   # худший исход в приоритете — не завышаем статистику
    if hit_tp:
        return "TP"
    return None


async def scan_market(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Скан рынка по всем пользователям каждые 30 минут."""
    if not _is_working_hours():
        now_str = datetime.datetime.now(ZoneInfo(WORK_TIMEZONE)).strftime("%H:%M")
        logger.info(
            f"scan_market: вне рабочего окна (сейчас {now_str} по Киеву, "
            f"окно {WORK_START.strftime('%H:%M')}–{WORK_END.strftime('%H:%M')}), скан пропущен"
        )
        return

    logger.info("scan_market: запуск сканирования")

    try:
        users = storage.get_all_configured_users()
    except Exception as e:
        logger.error(f"scan_market: ошибка получения пользователей: {e}", exc_info=True)
        return

    if not users:
        logger.info("scan_market: нет настроенных пользователей")
        return

    for user in users:
        logger.info(f"scan_market: пользователь {user['user_id']}, монеты: {user['coins']}")

        try:
            active = storage.get_active_trades_for_user(user["user_id"])
        except Exception as e:
            logger.warning(f"scan_market: не удалось получить активные сделки: {e}")
            active = []

        active_dirs = {"LONG": 0, "SHORT": 0}
        for t in active:
            d = t.get("direction")
            if d in active_dirs:
                active_dirs[d] += 1

        free_slots = MAX_ACTIVE_TRADES - len(active)
        if free_slots <= 0:
            logger.info(f"scan_market: {user['user_id']} — лимит сделок ({len(active)}/{MAX_ACTIVE_TRADES})")
            continue

        send_limit = min(free_slots, MAX_SIGNALS_PER_SCAN)

        # --- Шаг 1: собираем все кандидаты ---
        candidates = []
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
                logger.warning(f"scan_market: ошибка анализа {coin}: {e}", exc_info=True)
                continue

            if result is None:
                continue

            direction = result["trade"]["direction"]
            entry_price = float(result["trade"]["entry_price"])
            logger.info(f"scan_market: {coin} — сигнал {direction} @ {entry_price}")

            if storage.has_active_trade(user["user_id"], coin):
                logger.info(f"scan_market: {coin} — уже есть активная сделка")
                continue

            if storage.was_signal_sent_recently(user["user_id"], coin):
                logger.info(f"scan_market: {coin} — кулдаун")
                continue

            candidates.append(result)

        if not candidates:
            continue

        # --- Шаг 2: сортируем по качеству ---
        def _quality_ratio(r):
            q = r.get("quality") or {}
            mx = q.get("max") or 0
            return (q.get("score", 0) / mx) if mx > 0 else 0.0

        candidates.sort(key=_quality_ratio, reverse=True)
        logger.info(
            "scan_market: кандидаты — "
            + ", ".join(f"{r['coin']} {r['trade']['direction']} {_quality_ratio(r)*100:.0f}%" for r in candidates)
        )

        # --- Шаг 3: шлём лучшие ---
        sent = 0
        for result in candidates:
            if sent >= send_limit:
                break

            coin = result["coin"]
            trade = result["trade"]
            direction = trade["direction"]
            entry_price = float(trade["entry_price"])

            if active_dirs.get(direction, 0) >= MAX_SAME_DIRECTION:
                logger.info(f"scan_market: {coin} {direction} — лимит в одну сторону")
                continue

            # --- Теневой лог: пишем ДО отправки ---
            q = result.get("quality") or {}
            strategy = result.get("market_phase", "UNKNOWN")
            try:
                log_id = storage.log_signal(
                    user_id=user["user_id"], coin=coin, symbol=result["symbol"],
                    direction=direction, strategy=strategy,
                    entry_price=entry_price,
                    stop_loss=float(trade["stop_loss"]),
                    take_profit_1=float(trade["take_profit_1"]),
                    quality_score=float(q.get("score", 0)),
                    quality_max=float(q.get("max", 0)),
                )
            except Exception as e:
                logger.warning(f"scan_market: не удалось записать в signal_log: {e}")
                log_id = None

            # --- Отправляем ---
            text = format_signal_message(result, user)

            try:
                signal_id = storage.save_pending_signal(
                    user["user_id"], coin, result["symbol"], direction,
                    entry_price, float(trade["stop_loss"]), float(trade["take_profit_1"]),
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Вошёл в сделку", callback_data=f"entered_{signal_id}")
                ]])

                sent_msg = await context.bot.send_message(
                    chat_id=user["user_id"],
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )

                storage.mark_signal_sent(user["user_id"], coin, direction, entry_price)

                try:
                    storage.update_pending_signal_message_id(signal_id, sent_msg.message_id)
                except Exception as e:
                    logger.warning(f"scan_market: не удалось сохранить message_id: {e}")

                sent += 1
                active_dirs[direction] = active_dirs.get(direction, 0) + 1
                logger.info(f"scan_market: {coin} {direction} отправлен ({sent}/{send_limit}), log_id={log_id}")

            except Exception as e:
                logger.warning(f"scan_market: не удалось отправить {user['user_id']}: {e}", exc_info=True)

    logger.info("scan_market: сканирование завершено")


async def check_active_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет активные сделки (TP/SL) — круглосуточно."""
    try:
        trades = storage.get_all_active_trades()
    except Exception as e:
        logger.error(f"check_active_trades: ошибка: {e}", exc_info=True)
        return

    if not trades:
        return

    for t in trades:
        symbol = t["symbol"]
        direction = t["direction"]

        # Проверяем по свечам, а не по текущей цене: прокол уровня
        # между проверками иначе теряется.
        outcome = _check_levels_by_candles(
            symbol, direction, t["take_profit_1"], t["stop_loss"]
        )
        if outcome is None:
            continue

        hit_tp = outcome == "TP"
        hit_sl = outcome == "SL"

        if hit_tp:
            pct = abs(t["take_profit_1"] - t["entry_price"]) / t["entry_price"] * 100
            msg = (
                f"🎯 *ТЕЙК-ПРОФИТ взят!* {t['coin']}/USDT {direction}\n\n"
                f"Цена достигла {t['take_profit_1']:.4f} (+{pct:.2f}%)\n"
                f"Поздравляю с прибыльной сделкой! 🟢"
            )
        else:
            pct = abs(t["stop_loss"] - t["entry_price"]) / t["entry_price"] * 100
            msg = (
                f"🛑 *СТОП-ЛОСС сработал* {t['coin']}/USDT {direction}\n\n"
                f"Цена достигла {t['stop_loss']:.4f} (-{pct:.2f}%)\n"
                f"Это часть торговли — следующая сделка может быть прибыльной. 🔴"
            )

        msg_id = t.get("signal_message_id")
        sent_ok = False

        if msg_id:
            try:
                await context.bot.send_message(
                    chat_id=t["user_id"], text=msg, parse_mode="Markdown",
                    reply_to_message_id=msg_id,
                )
                sent_ok = True
            except Exception as e:
                logger.warning(f"check_active_trades: reply не удался ({e})")

        if not sent_ok:
            try:
                await context.bot.send_message(chat_id=t["user_id"], text=msg, parse_mode="Markdown")
                sent_ok = True
            except Exception as e:
                logger.warning(f"check_active_trades: ошибка отправки: {e}")

        if sent_ok:
            storage.remove_active_trade(t["id"])
            logger.info(f"check_active_trades: {t['coin']} {direction} — {'TP' if hit_tp else 'SL'}")


async def check_signal_log(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Фоновая проверка теневого лога: TP/SL достигнут?

    Работает независимо от активных сделок — отслеживает ВСЕ отправленные
    сигналы, даже те, в которые пользователь не входил. Именно это даёт
    честную статистику: «что было бы, если бы ты входил в каждый сигнал».

    Запускается каждые 5 минут. Не шлёт уведомлений — только пишет в базу.
    """
    try:
        open_logs = storage.get_all_open_signal_logs()
    except Exception as e:
        logger.error(f"check_signal_log: ошибка: {e}", exc_info=True)
        return

    if not open_logs:
        return

    for entry in open_logs:
        symbol = entry["symbol"]
        direction = entry["direction"]

        # По свечам, а не по текущей цене — иначе прокол уровня
        # между проверками теряется и сигнал висит «открытым» вечно.
        outcome = _check_levels_by_candles(
            symbol, direction, entry["take_profit_1"], entry["stop_loss"]
        )
        if outcome is None:
            continue

        level_price = entry["take_profit_1"] if outcome == "TP" else entry["stop_loss"]
        try:
            storage.resolve_signal_log(entry["id"], outcome, level_price)
            logger.info(f"check_signal_log: {entry['coin']} {direction} → {outcome} @ {level_price:.4f}")
        except Exception as e:
            logger.warning(f"check_signal_log: не удалось записать outcome: {e}")



# ============================================================
#  Канал: строгий отбор + украинский формат + трекинг результата
# ============================================================

def _channel_explanation(result: dict) -> str:
    """Живое объяснение на украинском из наших данных. Без ИИ — всегда правда."""
    direction = result["trade"]["direction"]
    phase = result.get("market_phase", "")
    vr = result.get("volume_ratio")
    trend_4h = result.get("trend_4h")
    is_range = phase == "RANGE"

    parts = []
    if direction == "SHORT":
        if is_range:
            parts.append("Ціна підійшла до верхньої межі діапазону й почала розворот вниз — заходимо в шорт від опору.")
        else:
            parts.append("Ринок у низхідному тренді. Ціна відкотилась угору й розвертається вниз — заходимо за трендом у шорт.")
    else:
        if is_range:
            parts.append("Ціна опустилась до нижньої межі діапазону й почала розворот угору — заходимо в лонг від підтримки.")
        else:
            parts.append("Ринок у висхідному тренді. Ціна відкотилась вниз і розвертається вгору — заходимо за трендом у лонг.")

    if (trend_4h == "bearish" and direction == "SHORT") or (trend_4h == "bullish" and direction == "LONG"):
        parts.append("Старший таймфрейм (4h) підтверджує напрямок.")
    if vr is not None and vr >= 1.0:
        parts.append(f"Обʼєм вищий за середній (x{vr:.1f}) — рух підкріплений.")
    if direction == "SHORT":
        parts.append("Ризик: якщо ринок різко піде вгору — стоп захистить від збитку.")
    else:
        parts.append("Ризик: якщо ринок різко піде вниз — стоп захистить від збитку.")
    return " ".join(parts)


def format_channel_signal(result: dict) -> str:
    """Украинский формат для канала. Без расчёта депозита."""
    trade = result["trade"]
    coin = result["coin"]
    direction = trade["direction"]
    entry = trade["entry_price"]

    header = f"📉 *{coin}/USDT — SHORT*" if direction == "SHORT" else f"📈 *{coin}/USDT — LONG*"
    tp1, tp2, sl = trade["take_profit_1"], trade["take_profit_2"], trade["stop_loss"]
    tp1_pct = abs(tp1 - entry) / entry * 100
    tp2_pct = abs(tp2 - entry) / entry * 100
    sl_pct = abs(sl - entry) / entry * 100

    return (
        f"{header}\n\n"
        f"💵 Вхід: {entry:.4f}\n"
        f"🎯 Ціль 1: {tp1:.4f} (+{tp1_pct:.1f}%)\n"
        f"🎯 Ціль 2: {tp2:.4f} (+{tp2_pct:.1f}%)\n"
        f"🛑 Стоп: {sl:.4f} (-{sl_pct:.1f}%)\n\n"
        f"⚙️ Кредитне плече: {CHANNEL_LEVERAGE}\n"
        f"📊 Ризик/прибуток: 1:{trade['risk_reward']:.1f}\n\n"
        f"📈 *Чому цей вхід:*\n{_channel_explanation(result)}\n\n"
        f"⚠️ Не заходь усім депозитом. Став стоп одразу.\n"
        f"Це не фінансова порада — торгуй з головою.\n\n"
        f"📲 Хочеш більше сигналів і персональний розрахунок під свій депозит?\n"
        f"Наш бот усе порахує сам 👉 @{CHANNEL_BOT_USERNAME}"
    )


async def scan_channel(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отдельный скан для канала: только лучший 🔥-сигнал, строгий фильтр."""
    if not _is_working_hours():
        return

    try:
        posted_today = storage.count_channel_signals_today()
    except Exception as e:
        logger.error(f"scan_channel: ошибка лимита: {e}", exc_info=True)
        return

    if posted_today >= CHANNEL_MAX_PER_DAY:
        logger.info(f"scan_channel: дневной лимит ({posted_today}/{CHANNEL_MAX_PER_DAY})")
        return

    DUMMY_DEPOSIT, DUMMY_RISK = 1000.0, 1.0
    candidates = []
    for coin in CHANNEL_COINS:
        coin = coin.strip().upper()
        if not coin:
            continue
        try:
            result = signals.find_signal(coin=coin, deposit=DUMMY_DEPOSIT,
                                         risk_percent=DUMMY_RISK, min_rr=2.0)
        except Exception as e:
            logger.warning(f"scan_channel: ошибка {coin}: {e}")
            continue
        if result is None:
            continue

        q = result.get("quality") or {}
        ratio = q.get("ratio", 0.0)
        vr = result.get("volume_ratio")
        trend_4h = result.get("trend_4h")
        direction = result["trade"]["direction"]

        if ratio < CHANNEL_MIN_QUALITY_RATIO:
            continue
        if vr is None or vr < CHANNEL_MIN_VOLUME:
            continue
        confirms = (trend_4h == "bearish" and direction == "SHORT") or \
                   (trend_4h == "bullish" and direction == "LONG")
        if not confirms:
            continue
        if storage.was_channel_signal_recent(coin, CHANNEL_COIN_COOLDOWN_H):
            continue
        candidates.append(result)

    if not candidates:
        logger.info("scan_channel: подходящих сигналов нет")
        return

    candidates.sort(key=lambda r: (r.get("quality") or {}).get("ratio", 0), reverse=True)
    best = candidates[0]
    trade = best["trade"]
    coin = best["coin"]

    text = format_channel_signal(best)
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        storage.log_channel_signal(
            coin=coin, symbol=best["symbol"], direction=trade["direction"],
            entry_price=trade["entry_price"], stop_loss=trade["stop_loss"],
            take_profit_1=trade["take_profit_1"], take_profit_2=trade["take_profit_2"],
        )
        logger.info(f"scan_channel: опубликован {coin} {trade['direction']} ({posted_today+1}/{CHANNEL_MAX_PER_DAY})")
    except Exception as e:
        logger.error(f"scan_channel: не удалось опубликовать в {CHANNEL_ID}: {e}", exc_info=True)


async def check_channel_signals(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Тихо фиксирует результат канальных сигналов по цене.
    Логика (по договорённости): дошла до Цели 1 → WIN, дошла до Стопа → LOSS.
    Что было после Цели 1 — не важно (стоп в безубыток). Не постит в канал.
    """
    try:
        open_signals = storage.get_open_channel_signals()
    except Exception as e:
        logger.error(f"check_channel_signals: ошибка: {e}", exc_info=True)
        return

    if not open_signals:
        return

    for sig in open_signals:
        symbol = sig["symbol"]
        direction = sig["direction"]

        # По свечам — прокол уровня между проверками не теряется.
        res = _check_levels_by_candles(
            symbol, direction, sig["take_profit_1"], sig["stop_loss"]
        )
        if res is None:
            continue

        outcome = "WIN" if res == "TP" else "LOSS"
        level_price = sig["take_profit_1"] if res == "TP" else sig["stop_loss"]
        try:
            storage.resolve_channel_signal(sig["id"], outcome, level_price)
            logger.info(f"check_channel_signals: {sig['coin']} {direction} → {outcome} @ {level_price:.4f}")
        except Exception as e:
            logger.warning(f"check_channel_signals: не записал outcome: {e}")


def format_signal_message(result: dict, user: dict) -> str:
    """Форматирует сигнал в текст для Telegram."""
    trade = result["trade"]
    direction_emoji = "🟢 LONG" if trade["direction"] == "LONG" else "🔴 SHORT"
    is_range = result.get("market_phase") == "RANGE" and bool(result.get("range_info"))

    leverage_note = ""
    if trade["leverage_reduced"]:
        leverage_note = (
            f"\n⚠️ Плечо снижено с x{trade['requested_leverage']} до x{trade['leverage']} "
            f"для безопасного запаса до ликвидации"
        )

    margin_note = ""
    if trade.get("margin_capped"):
        margin_note = (
            f"\nℹ️ Размер позиции уменьшен, чтобы маржа влезла в 40% депозита."
        )

    volume_line = f"📊 Объём: x{result['volume_ratio']:.1f} от среднего\n" if result.get("volume_ratio") else ""

    phase_names = {
        "TREND_UP": "восходящий тренд ↗️",
        "TREND_DOWN": "нисходящий тренд ↘️",
        "RANGE": "боковик ↔️",
    }
    phase_str = phase_names.get(result.get("market_phase", ""), result.get("market_phase", ""))

    if is_range:
        ri = result.get("range_info", {})
        context_lines = (
            f"📏 Диапазон: {ri.get('low', 0):.4f} — {ri.get('high', 0):.4f} "
            f"(ширина {ri.get('width_pct', 0):.1f}%)\n"
            f"🎯 Цель TP: {ri.get('tp_target', '')}\n"
        )
    else:
        context_lines = (
            f"📊 Тренд 1h: {result['trend_1h']}\n"
            f"📊 Тренд 4h: {result.get('trend_4h', '—')}\n"
        )

    quality = result.get("quality")
    quality_line = f"⭐ Качество: {quality['label']} ({quality['score']}/{quality['max']})\n" if quality else ""

    return (
        f"🚀 *НОВЫЙ СИГНАЛ: {result['coin']}/USDT {direction_emoji}*\n\n"
        f"{quality_line}"
        f"{context_lines}"
        f"📈 RSI 1h: {result['rsi_1h']:.1f}\n"
        f"📍 Причина входа: {result['entry_reason']}\n"
        f"🌐 Фаза рынка: {phase_str}\n"
        f"{volume_line}\n"
        f"💰 Цена входа: {trade['entry_price']:.4f}\n"
        f"🛑 Стоп-лосс: {trade['stop_loss']:.4f} (-{trade['sl_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 1: {trade['take_profit_1']:.4f} (+{trade['tp1_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 2: {trade['take_profit_2']:.4f}\n\n"
        f"📐 R/R: 1:{trade['risk_reward']:.2f}\n\n"
        f"💼 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск: {user['risk_percent']}% = {trade['risk_amount_usd']:.2f} USDT\n"
        f"📦 Размер позиции: {trade['position_size_usd']:.2f} USDT (плечо x{trade['leverage']})\n"
        f"💵 Маржа: {trade['margin_required']:.2f} USDT\n"
        f"💀 Цена ликвидации: {trade['liquidation_price']:.4f}"
        f"{leverage_note}{margin_note}"
    )# --- Лимиты экспозиции (личка бота) ---
MAX_ACTIVE_TRADES = 3
MAX_SIGNALS_PER_SCAN = 2
MAX_SAME_DIRECTION = 3

# --- Бесплатный канал (витрина) ---
CHANNEL_ID = -1004312355023          # приватный канал ONE SIGNAL (бот = админ)
CHANNEL_BOT_USERNAME = "one_s1gnal"  # для CTA в конце поста
CHANNEL_MIN_QUALITY_RATIO = 0.75     # только 🔥
CHANNEL_MIN_VOLUME = 1.0             # объём выше среднего (в личке хватает 0.7)
CHANNEL_MAX_PER_DAY = 2              # не больше 2 сигналов в день
CHANNEL_COIN_COOLDOWN_H = 6          # одна монета не чаще раза в 6 часов
CHANNEL_LEVERAGE = "x5–10"

CHANNEL_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "LTC", "TRX", "ATOM", "NEAR", "APT", "OP", "ARB", "INJ", "TIA", "SUI",
    "HYPE", "ZEC",
]



# ============================================================
#  Проверка достижения уровней по СВЕЧАМ (а не по текущей цене)
# ============================================================

# Сколько последних 5-минутных свечей проверять.
# Задачи ходят раз в 2-5 минут, берём запас: 6 свечей = 30 минут истории.
# Так прокол уровня не потеряется, даже если бот на пару циклов задержался.
LEVEL_CHECK_CANDLES = 6


def _check_levels_by_candles(symbol: str, direction: str,
                             take_profit: float, stop_loss: float) -> str | None:
    """
    Проверяет, задела ли цена TP или SL, по МАКСИМУМАМ/МИНИМУМАМ свечей.

    Почему не по текущей цене: бот смотрит рынок раз в несколько минут.
    Если цена ткнула уровень между проверками и вернулась обратно —
    текущая цена этого уже не покажет, и сделка зависнет «открытой»
    навсегда. Именно так статистика теряла сработавшие стопы.

    Возвращает "TP", "SL" или None.
    Если задеты оба уровня в одном окне — возвращает SL (консервативно:
    считаем худший исход, чтобы не завышать винрейт).
    """
    try:
        df = market.get_klines(symbol, "5m", limit=LEVEL_CHECK_CANDLES)
    except Exception as e:
        logger.warning(f"_check_levels_by_candles: не удалось получить свечи {symbol}: {e}")
        return None

    if df is None or len(df) == 0:
        return None

    high = float(df["high"].max())
    low = float(df["low"].min())

    if direction == "LONG":
        hit_tp = high >= take_profit
        hit_sl = low <= stop_loss
    else:  # SHORT
        hit_tp = low <= take_profit
        hit_sl = high >= stop_loss

    if hit_sl:
        return "SL"   # худший исход в приоритете — не завышаем статистику
    if hit_tp:
        return "TP"
    return None


async def scan_market(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Скан рынка по всем пользователям каждые 30 минут."""
    if not _is_working_hours():
        now_str = datetime.datetime.now(ZoneInfo(WORK_TIMEZONE)).strftime("%H:%M")
        logger.info(
            f"scan_market: вне рабочего окна (сейчас {now_str} по Киеву, "
            f"окно {WORK_START.strftime('%H:%M')}–{WORK_END.strftime('%H:%M')}), скан пропущен"
        )
        return

    logger.info("scan_market: запуск сканирования")

    try:
        users = storage.get_all_configured_users()
    except Exception as e:
        logger.error(f"scan_market: ошибка получения пользователей: {e}", exc_info=True)
        return

    if not users:
        logger.info("scan_market: нет настроенных пользователей")
        return

    for user in users:
        logger.info(f"scan_market: пользователь {user['user_id']}, монеты: {user['coins']}")

        try:
            active = storage.get_active_trades_for_user(user["user_id"])
        except Exception as e:
            logger.warning(f"scan_market: не удалось получить активные сделки: {e}")
            active = []

        active_dirs = {"LONG": 0, "SHORT": 0}
        for t in active:
            d = t.get("direction")
            if d in active_dirs:
                active_dirs[d] += 1

        free_slots = MAX_ACTIVE_TRADES - len(active)
        if free_slots <= 0:
            logger.info(f"scan_market: {user['user_id']} — лимит сделок ({len(active)}/{MAX_ACTIVE_TRADES})")
            continue

        send_limit = min(free_slots, MAX_SIGNALS_PER_SCAN)

        # --- Шаг 1: собираем все кандидаты ---
        candidates = []
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
                logger.warning(f"scan_market: ошибка анализа {coin}: {e}", exc_info=True)
                continue

            if result is None:
                continue

            direction = result["trade"]["direction"]
            entry_price = float(result["trade"]["entry_price"])
            logger.info(f"scan_market: {coin} — сигнал {direction} @ {entry_price}")

            if storage.has_active_trade(user["user_id"], coin):
                logger.info(f"scan_market: {coin} — уже есть активная сделка")
                continue

            if storage.was_signal_sent_recently(user["user_id"], coin):
                logger.info(f"scan_market: {coin} — кулдаун")
                continue

            candidates.append(result)

        if not candidates:
            continue

        # --- Шаг 2: сортируем по качеству ---
        def _quality_ratio(r):
            q = r.get("quality") or {}
            mx = q.get("max") or 0
            return (q.get("score", 0) / mx) if mx > 0 else 0.0

        candidates.sort(key=_quality_ratio, reverse=True)
        logger.info(
            "scan_market: кандидаты — "
            + ", ".join(f"{r['coin']} {r['trade']['direction']} {_quality_ratio(r)*100:.0f}%" for r in candidates)
        )

        # --- Шаг 3: шлём лучшие ---
        sent = 0
        for result in candidates:
            if sent >= send_limit:
                break

            coin = result["coin"]
            trade = result["trade"]
            direction = trade["direction"]
            entry_price = float(trade["entry_price"])

            if active_dirs.get(direction, 0) >= MAX_SAME_DIRECTION:
                logger.info(f"scan_market: {coin} {direction} — лимит в одну сторону")
                continue

            # --- Теневой лог: пишем ДО отправки ---
            q = result.get("quality") or {}
            strategy = result.get("market_phase", "UNKNOWN")
            try:
                log_id = storage.log_signal(
                    user_id=user["user_id"], coin=coin, symbol=result["symbol"],
                    direction=direction, strategy=strategy,
                    entry_price=entry_price,
                    stop_loss=float(trade["stop_loss"]),
                    take_profit_1=float(trade["take_profit_1"]),
                    quality_score=float(q.get("score", 0)),
                    quality_max=float(q.get("max", 0)),
                )
            except Exception as e:
                logger.warning(f"scan_market: не удалось записать в signal_log: {e}")
                log_id = None

            # --- Отправляем ---
            text = format_signal_message(result, user)

            try:
                signal_id = storage.save_pending_signal(
                    user["user_id"], coin, result["symbol"], direction,
                    entry_price, float(trade["stop_loss"]), float(trade["take_profit_1"]),
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Вошёл в сделку", callback_data=f"entered_{signal_id}")
                ]])

                sent_msg = await context.bot.send_message(
                    chat_id=user["user_id"],
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )

                storage.mark_signal_sent(user["user_id"], coin, direction, entry_price)

                try:
                    storage.update_pending_signal_message_id(signal_id, sent_msg.message_id)
                except Exception as e:
                    logger.warning(f"scan_market: не удалось сохранить message_id: {e}")

                sent += 1
                active_dirs[direction] = active_dirs.get(direction, 0) + 1
                logger.info(f"scan_market: {coin} {direction} отправлен ({sent}/{send_limit}), log_id={log_id}")

            except Exception as e:
                logger.warning(f"scan_market: не удалось отправить {user['user_id']}: {e}", exc_info=True)

    logger.info("scan_market: сканирование завершено")


async def check_active_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет активные сделки (TP/SL) — круглосуточно."""
    try:
        trades = storage.get_all_active_trades()
    except Exception as e:
        logger.error(f"check_active_trades: ошибка: {e}", exc_info=True)
        return

    if not trades:
        return

    for t in trades:
        symbol = t["symbol"]
        direction = t["direction"]

        # Проверяем по свечам, а не по текущей цене: прокол уровня
        # между проверками иначе теряется.
        outcome = _check_levels_by_candles(
            symbol, direction, t["take_profit_1"], t["stop_loss"]
        )
        if outcome is None:
            continue

        hit_tp = outcome == "TP"
        hit_sl = outcome == "SL"

        if hit_tp:
            pct = abs(t["take_profit_1"] - t["entry_price"]) / t["entry_price"] * 100
            msg = (
                f"🎯 *ТЕЙК-ПРОФИТ взят!* {t['coin']}/USDT {direction}\n\n"
                f"Цена достигла {t['take_profit_1']:.4f} (+{pct:.2f}%)\n"
                f"Поздравляю с прибыльной сделкой! 🟢"
            )
        else:
            pct = abs(t["stop_loss"] - t["entry_price"]) / t["entry_price"] * 100
            msg = (
                f"🛑 *СТОП-ЛОСС сработал* {t['coin']}/USDT {direction}\n\n"
                f"Цена достигла {t['stop_loss']:.4f} (-{pct:.2f}%)\n"
                f"Это часть торговли — следующая сделка может быть прибыльной. 🔴"
            )

        msg_id = t.get("signal_message_id")
        sent_ok = False

        if msg_id:
            try:
                await context.bot.send_message(
                    chat_id=t["user_id"], text=msg, parse_mode="Markdown",
                    reply_to_message_id=msg_id,
                )
                sent_ok = True
            except Exception as e:
                logger.warning(f"check_active_trades: reply не удался ({e})")

        if not sent_ok:
            try:
                await context.bot.send_message(chat_id=t["user_id"], text=msg, parse_mode="Markdown")
                sent_ok = True
            except Exception as e:
                logger.warning(f"check_active_trades: ошибка отправки: {e}")

        if sent_ok:
            storage.remove_active_trade(t["id"])
            logger.info(f"check_active_trades: {t['coin']} {direction} — {'TP' if hit_tp else 'SL'}")


async def check_signal_log(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Фоновая проверка теневого лога: TP/SL достигнут?

    Работает независимо от активных сделок — отслеживает ВСЕ отправленные
    сигналы, даже те, в которые пользователь не входил. Именно это даёт
    честную статистику: «что было бы, если бы ты входил в каждый сигнал».

    Запускается каждые 5 минут. Не шлёт уведомлений — только пишет в базу.
    """
    try:
        open_logs = storage.get_all_open_signal_logs()
    except Exception as e:
        logger.error(f"check_signal_log: ошибка: {e}", exc_info=True)
        return

    if not open_logs:
        return

    for entry in open_logs:
        symbol = entry["symbol"]
        direction = entry["direction"]

        # По свечам, а не по текущей цене — иначе прокол уровня
        # между проверками теряется и сигнал висит «открытым» вечно.
        outcome = _check_levels_by_candles(
            symbol, direction, entry["take_profit_1"], entry["stop_loss"]
        )
        if outcome is None:
            continue

        level_price = entry["take_profit_1"] if outcome == "TP" else entry["stop_loss"]
        try:
            storage.resolve_signal_log(entry["id"], outcome, level_price)
            logger.info(f"check_signal_log: {entry['coin']} {direction} → {outcome} @ {level_price:.4f}")
        except Exception as e:
            logger.warning(f"check_signal_log: не удалось записать outcome: {e}")



# ============================================================
#  Канал: строгий отбор + украинский формат + трекинг результата
# ============================================================

def _channel_explanation(result: dict) -> str:
    """Живое объяснение на украинском из наших данных. Без ИИ — всегда правда."""
    direction = result["trade"]["direction"]
    phase = result.get("market_phase", "")
    vr = result.get("volume_ratio")
    trend_4h = result.get("trend_4h")
    is_range = phase == "RANGE"

    parts = []
    if direction == "SHORT":
        if is_range:
            parts.append("Ціна підійшла до верхньої межі діапазону й почала розворот вниз — заходимо в шорт від опору.")
        else:
            parts.append("Ринок у низхідному тренді. Ціна відкотилась угору й розвертається вниз — заходимо за трендом у шорт.")
    else:
        if is_range:
            parts.append("Ціна опустилась до нижньої межі діапазону й почала розворот угору — заходимо в лонг від підтримки.")
        else:
            parts.append("Ринок у висхідному тренді. Ціна відкотилась вниз і розвертається вгору — заходимо за трендом у лонг.")

    if (trend_4h == "bearish" and direction == "SHORT") or (trend_4h == "bullish" and direction == "LONG"):
        parts.append("Старший таймфрейм (4h) підтверджує напрямок.")
    if vr is not None and vr >= 1.0:
        parts.append(f"Обʼєм вищий за середній (x{vr:.1f}) — рух підкріплений.")
    if direction == "SHORT":
        parts.append("Ризик: якщо ринок різко піде вгору — стоп захистить від збитку.")
    else:
        parts.append("Ризик: якщо ринок різко піде вниз — стоп захистить від збитку.")
    return " ".join(parts)


def format_channel_signal(result: dict) -> str:
    """Украинский формат для канала. Без расчёта депозита."""
    trade = result["trade"]
    coin = result["coin"]
    direction = trade["direction"]
    entry = trade["entry_price"]

    header = f"📉 *{coin}/USDT — SHORT*" if direction == "SHORT" else f"📈 *{coin}/USDT — LONG*"
    tp1, tp2, sl = trade["take_profit_1"], trade["take_profit_2"], trade["stop_loss"]
    tp1_pct = abs(tp1 - entry) / entry * 100
    tp2_pct = abs(tp2 - entry) / entry * 100
    sl_pct = abs(sl - entry) / entry * 100

    return (
        f"{header}\n\n"
        f"💵 Вхід: {entry:.4f}\n"
        f"🎯 Ціль 1: {tp1:.4f} (+{tp1_pct:.1f}%)\n"
        f"🎯 Ціль 2: {tp2:.4f} (+{tp2_pct:.1f}%)\n"
        f"🛑 Стоп: {sl:.4f} (-{sl_pct:.1f}%)\n\n"
        f"⚙️ Кредитне плече: {CHANNEL_LEVERAGE}\n"
        f"📊 Ризик/прибуток: 1:{trade['risk_reward']:.1f}\n\n"
        f"📈 *Чому цей вхід:*\n{_channel_explanation(result)}\n\n"
        f"⚠️ Не заходь усім депозитом. Став стоп одразу.\n"
        f"Це не фінансова порада — торгуй з головою.\n\n"
        f"📲 Хочеш більше сигналів і персональний розрахунок під свій депозит?\n"
        f"Наш бот усе порахує сам 👉 @{CHANNEL_BOT_USERNAME}"
    )


async def scan_channel(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отдельный скан для канала: только лучший 🔥-сигнал, строгий фильтр."""
    if not _is_working_hours():
        return

    try:
        posted_today = storage.count_channel_signals_today()
    except Exception as e:
        logger.error(f"scan_channel: ошибка лимита: {e}", exc_info=True)
        return

    if posted_today >= CHANNEL_MAX_PER_DAY:
        logger.info(f"scan_channel: дневной лимит ({posted_today}/{CHANNEL_MAX_PER_DAY})")
        return

    DUMMY_DEPOSIT, DUMMY_RISK = 1000.0, 1.0
    candidates = []
    for coin in CHANNEL_COINS:
        coin = coin.strip().upper()
        if not coin:
            continue
        try:
            result = signals.find_signal(coin=coin, deposit=DUMMY_DEPOSIT,
                                         risk_percent=DUMMY_RISK, min_rr=2.0)
        except Exception as e:
            logger.warning(f"scan_channel: ошибка {coin}: {e}")
            continue
        if result is None:
            continue

        q = result.get("quality") or {}
        ratio = q.get("ratio", 0.0)
        vr = result.get("volume_ratio")
        trend_4h = result.get("trend_4h")
        direction = result["trade"]["direction"]

        if ratio < CHANNEL_MIN_QUALITY_RATIO:
            continue
        if vr is None or vr < CHANNEL_MIN_VOLUME:
            continue
        confirms = (trend_4h == "bearish" and direction == "SHORT") or \
                   (trend_4h == "bullish" and direction == "LONG")
        if not confirms:
            continue
        if storage.was_channel_signal_recent(coin, CHANNEL_COIN_COOLDOWN_H):
            continue
        candidates.append(result)

    if not candidates:
        logger.info("scan_channel: подходящих сигналов нет")
        return

    candidates.sort(key=lambda r: (r.get("quality") or {}).get("ratio", 0), reverse=True)
    best = candidates[0]
    trade = best["trade"]
    coin = best["coin"]

    text = format_channel_signal(best)
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown")
        storage.log_channel_signal(
            coin=coin, symbol=best["symbol"], direction=trade["direction"],
            entry_price=trade["entry_price"], stop_loss=trade["stop_loss"],
            take_profit_1=trade["take_profit_1"], take_profit_2=trade["take_profit_2"],
        )
        logger.info(f"scan_channel: опубликован {coin} {trade['direction']} ({posted_today+1}/{CHANNEL_MAX_PER_DAY})")
    except Exception as e:
        logger.error(f"scan_channel: не удалось опубликовать в {CHANNEL_ID}: {e}", exc_info=True)


async def check_channel_signals(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Тихо фиксирует результат канальных сигналов по цене.
    Логика (по договорённости): дошла до Цели 1 → WIN, дошла до Стопа → LOSS.
    Что было после Цели 1 — не важно (стоп в безубыток). Не постит в канал.
    """
    try:
        open_signals = storage.get_open_channel_signals()
    except Exception as e:
        logger.error(f"check_channel_signals: ошибка: {e}", exc_info=True)
        return

    if not open_signals:
        return

    for sig in open_signals:
        symbol = sig["symbol"]
        direction = sig["direction"]

        # По свечам — прокол уровня между проверками не теряется.
        res = _check_levels_by_candles(
            symbol, direction, sig["take_profit_1"], sig["stop_loss"]
        )
        if res is None:
            continue

        outcome = "WIN" if res == "TP" else "LOSS"
        level_price = sig["take_profit_1"] if res == "TP" else sig["stop_loss"]
        try:
            storage.resolve_channel_signal(sig["id"], outcome, level_price)
            logger.info(f"check_channel_signals: {sig['coin']} {direction} → {outcome} @ {level_price:.4f}")
        except Exception as e:
            logger.warning(f"check_channel_signals: не записал outcome: {e}")


def format_signal_message(result: dict, user: dict) -> str:
    """Форматирует сигнал в текст для Telegram."""
    trade = result["trade"]
    direction_emoji = "🟢 LONG" if trade["direction"] == "LONG" else "🔴 SHORT"
    is_range = result.get("market_phase") == "RANGE" and bool(result.get("range_info"))

    leverage_note = ""
    if trade["leverage_reduced"]:
        leverage_note = (
            f"\n⚠️ Плечо снижено с x{trade['requested_leverage']} до x{trade['leverage']} "
            f"для безопасного запаса до ликвидации"
        )

    margin_note = ""
    if trade.get("margin_capped"):
        margin_note = (
            f"\nℹ️ Размер позиции уменьшен, чтобы маржа влезла в 40% депозита."
        )

    volume_line = f"📊 Объём: x{result['volume_ratio']:.1f} от среднего\n" if result.get("volume_ratio") else ""

    phase_names = {
        "TREND_UP": "восходящий тренд ↗️",
        "TREND_DOWN": "нисходящий тренд ↘️",
        "RANGE": "боковик ↔️",
    }
    phase_str = phase_names.get(result.get("market_phase", ""), result.get("market_phase", ""))

    if is_range:
        ri = result.get("range_info", {})
        context_lines = (
            f"📏 Диапазон: {ri.get('low', 0):.4f} — {ri.get('high', 0):.4f} "
            f"(ширина {ri.get('width_pct', 0):.1f}%)\n"
            f"🎯 Цель TP: {ri.get('tp_target', '')}\n"
        )
    else:
        context_lines = (
            f"📊 Тренд 1h: {result['trend_1h']}\n"
            f"📊 Тренд 4h: {result.get('trend_4h', '—')}\n"
        )

    quality = result.get("quality")
    quality_line = f"⭐ Качество: {quality['label']} ({quality['score']}/{quality['max']})\n" if quality else ""

    return (
        f"🚀 *НОВЫЙ СИГНАЛ: {result['coin']}/USDT {direction_emoji}*\n\n"
        f"{quality_line}"
        f"{context_lines}"
        f"📈 RSI 1h: {result['rsi_1h']:.1f}\n"
        f"📍 Причина входа: {result['entry_reason']}\n"
        f"🌐 Фаза рынка: {phase_str}\n"
        f"{volume_line}\n"
        f"💰 Цена входа: {trade['entry_price']:.4f}\n"
        f"🛑 Стоп-лосс: {trade['stop_loss']:.4f} (-{trade['sl_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 1: {trade['take_profit_1']:.4f} (+{trade['tp1_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 2: {trade['take_profit_2']:.4f}\n\n"
        f"📐 R/R: 1:{trade['risk_reward']:.2f}\n\n"
        f"💼 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск: {user['risk_percent']}% = {trade['risk_amount_usd']:.2f} USDT\n"
        f"📦 Размер позиции: {trade['position_size_usd']:.2f} USDT (плечо x{trade['leverage']})\n"
        f"💵 Маржа: {trade['margin_required']:.2f} USDT\n"
        f"💀 Цена ликвидации: {trade['liquidation_price']:.4f}"
        f"{leverage_note}{margin_note}"
    )
