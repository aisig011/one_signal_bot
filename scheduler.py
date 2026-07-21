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


# --- Лимиты экспозиции ---
MAX_ACTIVE_TRADES = 3
MAX_SIGNALS_PER_SCAN = 2
MAX_SAME_DIRECTION = 3


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
            if price >= t["take_profit_1"]: hit_tp = True
            elif price <= t["stop_loss"]:   hit_sl = True
        else:
            if price <= t["take_profit_1"]: hit_tp = True
            elif price >= t["stop_loss"]:   hit_sl = True

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

    price_cache = {}

    for entry in open_logs:
        symbol = entry["symbol"]
        try:
            if symbol not in price_cache:
                price_cache[symbol] = market.get_current_price(symbol)
            price = price_cache[symbol]
        except Exception as e:
            logger.warning(f"check_signal_log: цена {symbol} недоступна: {e}")
            continue

        direction = entry["direction"]
        hit_tp = False
        hit_sl = False

        if direction == "LONG":
            if price >= entry["take_profit_1"]: hit_tp = True
            elif price <= entry["stop_loss"]:   hit_sl = True
        else:
            if price <= entry["take_profit_1"]: hit_tp = True
            elif price >= entry["stop_loss"]:   hit_sl = True

        if hit_tp or hit_sl:
            outcome = "TP" if hit_tp else "SL"
            try:
                storage.resolve_signal_log(entry["id"], outcome, price)
                logger.info(f"check_signal_log: {entry['coin']} {direction} → {outcome} @ {price:.4f}")
            except Exception as e:
                logger.warning(f"check_signal_log: не удалось записать outcome: {e}")


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
