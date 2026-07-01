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
    """Скан рынка по всем пользователям и их монетам каждые 30 минут."""
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
                logger.warning(f"scan_market: ошибка анализа {coin} для {user['user_id']}: {e}", exc_info=True)
                continue

            if result is None:
                logger.info(f"scan_market: {coin} — сигнала нет")
                continue

            trade = result["trade"]
            direction = trade["direction"]
            entry_price = trade["entry_price"]

            logger.info(f"scan_market: {coin} — НАЙДЕН сигнал {direction} @ {entry_price}")

            # Защита: уже есть активная сделка по этой монете
            if storage.has_active_trade(user["user_id"], coin):
                logger.info(f"scan_market: {coin} — уже есть активная сделка, пропускаю")
                continue

            # Кулдаун по монете (4 часа, любое направление)
            if storage.was_signal_sent_recently(user["user_id"], coin):
                logger.info(f"scan_market: {coin} — уже отправлялся недавно, пропускаю")
                continue

            text = format_signal_message(result, user)

            try:
                signal_id = storage.save_pending_signal(
                    user["user_id"], coin, result["symbol"], direction,
                    float(entry_price), float(trade["stop_loss"]), float(trade["take_profit_1"]),
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

                # ВАЖНО: кулдаун пишем СРАЗУ после отправки — приоритет №1,
                # чтобы дубля не было даже если что-то ниже упадёт.
                storage.mark_signal_sent(user["user_id"], coin, direction, entry_price)

                # message_id для reply — в отдельном try, его падение
                # не должно ломать кулдаун (был баг с дублями из-за этого).
                try:
                    storage.update_pending_signal_message_id(signal_id, sent_msg.message_id)
                except Exception as e:
                    logger.warning(f"scan_market: не удалось сохранить message_id: {e}")

                logger.info(f"scan_market: сигнал {coin} {direction} отправлен пользователю {user['user_id']}")
            except Exception as e:
                logger.warning(f"scan_market: не удалось отправить сообщение {user['user_id']}: {e}", exc_info=True)

    logger.info("scan_market: сканирование завершено")


async def check_active_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Проверяет активные сделки — достигла ли цена TP или SL.
    Уведомление приходит как reply на оригинальный сигнал (если возможно).
    """
    try:
        trades = storage.get_all_active_trades()
    except Exception as e:
        logger.error(f"check_active_trades: ошибка получения сделок: {e}", exc_info=True)
        return

    if not trades:
        return

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
                    chat_id=t["user_id"],
                    text=msg,
                    parse_mode="Markdown",
                    reply_to_message_id=msg_id,
                )
                sent_ok = True
            except Exception as e:
                logger.warning(f"check_active_trades: reply не удался ({e}), шлю без reply")

        if not sent_ok:
            try:
                await context.bot.send_message(
                    chat_id=t["user_id"],
                    text=msg,
                    parse_mode="Markdown",
                )
                sent_ok = True
            except Exception as e:
                logger.warning(f"check_active_trades: ошибка отправки: {e}")

        if sent_ok:
            storage.remove_active_trade(t["id"])
            logger.info(f"check_active_trades: {t['coin']} {direction} — {'TP' if hit_tp else 'SL'}, уведомление отправлено")


def format_signal_message(result: dict, user: dict) -> str:
    """Форматирует сигнал в текст сообщения (используется и в /signal, и в scheduler)."""
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
            f"\nℹ️ Размер позиции уменьшен, чтобы маржа влезла в 40% депозита. "
            f"Реальный риск меньше {user['risk_percent']}% — это безопаснее."
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
        f"{margin_note}"
    )
