"""
scheduler.py
Фоновая задача: каждые 30 минут проверяет всех настроенных пользователей
по их списку монет, ищет сигналы и присылает их в Telegram.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import storage
import signals
import market

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 30 * 60  # 30 минут


async def scan_market(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Запускается планировщиком каждые SCAN_INTERVAL_SECONDS.
    Проходит по всем пользователям и их монетам, ищет сигналы.
    """
    logger.info("scan_market: запуск сканирования")

    try:
        users = storage.get_all_configured_users()
    except Exception as e:
        logger.error(f"scan_market: ошибка получения пользователей: {e}", exc_info=True)
        return

    logger.info(f"scan_market: найдено пользователей с настройками: {len(users)}")

    if not users:
        logger.info("scan_market: нет настроенных пользователей, завершаю")
        return

    # Собираем уникальный набор монет, чтобы не запрашивать одно и то же
    # с Binance несколько раз для разных пользователей с похожими списками.
    # (Для простоты MVP считаем сигнал отдельно на каждого юзера+монету —
    # запросов всё равно немного: до ~5 монет x число пользователей)

    for user in users:
        logger.info(f"scan_market: пользователь {user['user_id']}, монеты: {user['coins']}")

        for coin in user["coins"]:
            coin = coin.strip().upper()
            if not coin:
                continue

            logger.info(f"scan_market: анализирую {coin} для {user['user_id']}")

            try:
                result = signals.find_signal(
                    coin=coin,
                    deposit=user["deposit"],
                    risk_percent=user["risk_percent"],
                    min_rr=2.0,
                )
            except Exception as e:
                logger.warning(f"scan_market: ошибка анализа {coin} для пользователя {user['user_id']}: {e}", exc_info=True)
                continue

            if result is None:
                logger.info(f"scan_market: {coin} — сигнала нет")
                continue

            trade = result["trade"]
            direction = trade["direction"]
            entry_price = trade["entry_price"]

            logger.info(f"scan_market: {coin} — НАЙДЕН сигнал {direction} @ {entry_price}")

            # Проверяем, не отправляли ли уже похожий сигнал недавно
            if storage.was_signal_sent_recently(user["user_id"], coin, direction, entry_price):
                logger.info(f"scan_market: {coin} {direction} — уже отправлялся недавно, пропускаю")
                continue

            text = format_signal_message(result, user)

            try:
                # Сохраняем сигнал и создаём кнопку "Вошёл" с его id
                signal_id = storage.save_pending_signal(
                    user["user_id"], coin, result["symbol"], direction,
                    float(entry_price), float(trade["stop_loss"]), float(trade["take_profit_1"]),
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Вошёл в сделку", callback_data=f"entered_{signal_id}")
                ]])

                await context.bot.send_message(
                    chat_id=user["user_id"],
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                storage.mark_signal_sent(user["user_id"], coin, direction, entry_price)
                logger.info(f"scan_market: сигнал {coin} {direction} отправлен пользователю {user['user_id']}")
            except Exception as e:
                logger.warning(f"scan_market: не удалось отправить сообщение пользователю {user['user_id']}: {e}", exc_info=True)

    logger.info("scan_market: сканирование завершено")


async def check_active_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Проверяет активные сделки (которые пользователь подтвердил кнопкой
    'Вошёл') — достигла ли цена TP или SL, и присылает уведомление.
    """
    try:
        trades = storage.get_all_active_trades()
    except Exception as e:
        logger.error(f"check_active_trades: ошибка получения сделок: {e}", exc_info=True)
        return

    if not trades:
        return

    # Кэш цен, чтобы не запрашивать одну монету несколько раз
    price_cache = {}

    for t in trades:
        symbol = t["symbol"]
        try:
            if symbol not in price_cache:
                price_cache[symbol] = market.get_current_price(symbol)
            price = price_cache[symbol]
        except Exception as e:
            logger.warning(f"check_active_trades: не удалось получить цену {symbol}: {e}")
            continue

        direction = t["direction"]
        hit_tp = False
        hit_sl = False

        if direction == "LONG":
            if price >= t["take_profit_1"]:
                hit_tp = True
            elif price <= t["stop_loss"]:
                hit_sl = True
        else:  # SHORT
            if price <= t["take_profit_1"]:
                hit_tp = True
            elif price >= t["stop_loss"]:
                hit_sl = True

        if not (hit_tp or hit_sl):
            continue

        # Считаем результат в процентах от цены входа
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

        try:
            await context.bot.send_message(
                chat_id=t["user_id"], text=msg, parse_mode="Markdown"
            )
            storage.remove_active_trade(t["id"])
            logger.info(f"check_active_trades: {t['coin']} {direction} — {'TP' if hit_tp else 'SL'}, уведомление отправлено")
        except Exception as e:
            logger.warning(f"check_active_trades: ошибка отправки: {e}")


def format_signal_message(result: dict, user: dict) -> str:
    """Форматирует сигнал в текст сообщения (используется и в /signal, и в scheduler)."""
    trade = result["trade"]
    direction_emoji = "🟢 LONG" if trade["direction"] == "LONG" else "🔴 SHORT"
    is_range = result.get("market_phase") == "RANGE"

    leverage_note = ""
    if trade["leverage_reduced"]:
        leverage_note = (
            f"\n⚠️ Плечо снижено с x{trade['requested_leverage']} до x{trade['leverage']} "
            f"для безопасного запаса до ликвидации"
        )

    volume_line = ""
    if result.get("volume_ratio"):
        volume_line = f"📊 Объём: x{result['volume_ratio']:.1f} от среднего\n"

    phase_names = {
        "TREND_UP": "восходящий тренд ↗️",
        "TREND_DOWN": "нисходящий тренд ↘️",
        "RANGE": "боковик ↔️",
    }
    phase_str = phase_names.get(result.get("market_phase", ""), result.get("market_phase", ""))
    phase_line = f"🌐 Фаза рынка: {phase_str}\n"

    # Строка с контекстом тренда — для RANGE показываем границы диапазона,
    # для тренда — тренды 1h/4h как раньше
    if is_range:
        ri = result.get("range_info", {})
        context_lines = (
            f"📏 Диапазон: {ri.get('low', 0):.4f} — {ri.get('high', 0):.4f} "
            f"(ширина {ri.get('width_pct', 0):.1f}%)\n"
            f"🎯 Цель TP: {ri.get('tp_target', '')}\n"
        )
    else:
        trend_4h_str = result["trend_4h"] if result.get("trend_4h") else "—"
        context_lines = (
            f"📊 Тренд 1h: {result['trend_1h']}\n"
            f"📊 Тренд 4h: {trend_4h_str}\n"
        )

    return (
        f"🚀 *НОВЫЙ СИГНАЛ: {result['coin']}/USDT {direction_emoji}*\n\n"
        f"{context_lines}"
        f"📈 RSI 1h: {result['rsi_1h']:.1f}\n"
        f"📍 Причина входа: {result['entry_reason']}\n"
        f"{phase_line}"
        f"{volume_line}\n"
        f"💰 Цена входа: {trade['entry_price']:.4f}\n"
        f"🛑 Стоп-лосс: {trade['stop_loss']:.4f} (-{trade['sl_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 1: {trade['take_profit_1']:.4f} (+{trade['tp1_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 2: {trade['take_profit_2']:.4f}\n\n"
        f"📐 R/R: 1:{trade['risk_reward']:.2f}\n\n"
        f"💼 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск: {user['risk_percent']}% = {trade['risk_amount_usd']:.2f} USDT\n"
        f"📦 Размер позиции: {trade['position_size_usd']:.2f} USDT "
        f"(плечо x{trade['leverage']})\n"
        f"💵 Маржа: {trade['margin_required']:.2f} USDT\n"
        f"💀 Цена ликвидации: {trade['liquidation_price']:.4f}"
        f"{leverage_note}"
    )        logger.info("scan_market: нет настроенных пользователей, завершаю")
        return

    # Собираем уникальный набор монет, чтобы не запрашивать одно и то же
    # с Binance несколько раз для разных пользователей с похожими списками.
    # (Для простоты MVP считаем сигнал отдельно на каждого юзера+монету —
    # запросов всё равно немного: до ~5 монет x число пользователей)

    for user in users:
        logger.info(f"scan_market: пользователь {user['user_id']}, монеты: {user['coins']}")

        for coin in user["coins"]:
            coin = coin.strip().upper()
            if not coin:
                continue

            logger.info(f"scan_market: анализирую {coin} для {user['user_id']}")

            try:
                result = signals.find_signal(
                    coin=coin,
                    deposit=user["deposit"],
                    risk_percent=user["risk_percent"],
                    min_rr=2.0,
                )
            except Exception as e:
                logger.warning(f"scan_market: ошибка анализа {coin} для пользователя {user['user_id']}: {e}", exc_info=True)
                continue

            if result is None:
                logger.info(f"scan_market: {coin} — сигнала нет")
                continue

            trade = result["trade"]
            direction = trade["direction"]
            entry_price = trade["entry_price"]

            logger.info(f"scan_market: {coin} — НАЙДЕН сигнал {direction} @ {entry_price}")

            # Проверяем, не отправляли ли уже похожий сигнал недавно
            if storage.was_signal_sent_recently(user["user_id"], coin, direction, entry_price):
                logger.info(f"scan_market: {coin} {direction} — уже отправлялся недавно, пропускаю")
                continue

            text = format_signal_message(result, user)

            try:
                # Сохраняем сигнал и создаём кнопку "Вошёл" с его id
                signal_id = storage.save_pending_signal(
                    user["user_id"], coin, result["symbol"], direction,
                    entry_price, trade["stop_loss"], trade["take_profit_1"],
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Вошёл в сделку", callback_data=f"entered_{signal_id}")
                ]])

                await context.bot.send_message(
                    chat_id=user["user_id"],
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                storage.mark_signal_sent(user["user_id"], coin, direction, entry_price)
                logger.info(f"scan_market: сигнал {coin} {direction} отправлен пользователю {user['user_id']}")
            except Exception as e:
                logger.warning(f"scan_market: не удалось отправить сообщение пользователю {user['user_id']}: {e}", exc_info=True)

    logger.info("scan_market: сканирование завершено")


async def check_active_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Проверяет активные сделки (которые пользователь подтвердил кнопкой
    'Вошёл') — достигла ли цена TP или SL, и присылает уведомление.
    """
    try:
        trades = storage.get_all_active_trades()
    except Exception as e:
        logger.error(f"check_active_trades: ошибка получения сделок: {e}", exc_info=True)
        return

    if not trades:
        return

    # Кэш цен, чтобы не запрашивать одну монету несколько раз
    price_cache = {}

    for t in trades:
        symbol = t["symbol"]
        try:
            if symbol not in price_cache:
                price_cache[symbol] = market.get_current_price(symbol)
            price = price_cache[symbol]
        except Exception as e:
            logger.warning(f"check_active_trades: не удалось получить цену {symbol}: {e}")
            continue

        direction = t["direction"]
        hit_tp = False
        hit_sl = False

        if direction == "LONG":
            if price >= t["take_profit_1"]:
                hit_tp = True
            elif price <= t["stop_loss"]:
                hit_sl = True
        else:  # SHORT
            if price <= t["take_profit_1"]:
                hit_tp = True
            elif price >= t["stop_loss"]:
                hit_sl = True

        if not (hit_tp or hit_sl):
            continue

        # Считаем результат в процентах от цены входа
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

        try:
            await context.bot.send_message(
                chat_id=t["user_id"], text=msg, parse_mode="Markdown"
            )
            storage.remove_active_trade(t["id"])
            logger.info(f"check_active_trades: {t['coin']} {direction} — {'TP' if hit_tp else 'SL'}, уведомление отправлено")
        except Exception as e:
            logger.warning(f"check_active_trades: ошибка отправки: {e}")


def format_signal_message(result: dict, user: dict) -> str:
    """Форматирует сигнал в текст сообщения (используется и в /signal, и в scheduler)."""
    trade = result["trade"]
    direction_emoji = "🟢 LONG" if trade["direction"] == "LONG" else "🔴 SHORT"
    is_range = result.get("market_phase") == "RANGE"

    leverage_note = ""
    if trade["leverage_reduced"]:
        leverage_note = (
            f"\n⚠️ Плечо снижено с x{trade['requested_leverage']} до x{trade['leverage']} "
            f"для безопасного запаса до ликвидации"
        )

    volume_line = ""
    if result.get("volume_ratio"):
        volume_line = f"📊 Объём: x{result['volume_ratio']:.1f} от среднего\n"

    phase_names = {
        "TREND_UP": "восходящий тренд ↗️",
        "TREND_DOWN": "нисходящий тренд ↘️",
        "RANGE": "боковик ↔️",
    }
    phase_str = phase_names.get(result.get("market_phase", ""), result.get("market_phase", ""))
    phase_line = f"🌐 Фаза рынка: {phase_str}\n"

    # Строка с контекстом тренда — для RANGE показываем границы диапазона,
    # для тренда — тренды 1h/4h как раньше
    if is_range:
        ri = result.get("range_info", {})
        context_lines = (
            f"📏 Диапазон: {ri.get('low', 0):.4f} — {ri.get('high', 0):.4f} "
            f"(ширина {ri.get('width_pct', 0):.1f}%)\n"
            f"🎯 Цель TP: {ri.get('tp_target', '')}\n"
        )
    else:
        trend_4h_str = result["trend_4h"] if result.get("trend_4h") else "—"
        context_lines = (
            f"📊 Тренд 1h: {result['trend_1h']}\n"
            f"📊 Тренд 4h: {trend_4h_str}\n"
        )

    return (
        f"🚀 *НОВЫЙ СИГНАЛ: {result['coin']}/USDT {direction_emoji}*\n\n"
        f"{context_lines}"
        f"📈 RSI 1h: {result['rsi_1h']:.1f}\n"
        f"📍 Причина входа: {result['entry_reason']}\n"
        f"{phase_line}"
        f"{volume_line}\n"
        f"💰 Цена входа: {trade['entry_price']:.4f}\n"
        f"🛑 Стоп-лосс: {trade['stop_loss']:.4f} (-{trade['sl_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 1: {trade['take_profit_1']:.4f} (+{trade['tp1_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 2: {trade['take_profit_2']:.4f}\n\n"
        f"📐 R/R: 1:{trade['risk_reward']:.2f}\n\n"
        f"💼 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск: {user['risk_percent']}% = {trade['risk_amount_usd']:.2f} USDT\n"
        f"📦 Размер позиции: {trade['position_size_usd']:.2f} USDT "
        f"(плечо x{trade['leverage']})\n"
        f"💵 Маржа: {trade['margin_required']:.2f} USDT\n"
        f"💀 Цена ликвидации: {trade['liquidation_price']:.4f}"
        f"{leverage_note}"
    )
