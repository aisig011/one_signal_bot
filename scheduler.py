"""
scheduler.py
Фоновая задача: каждые 30 минут проверяет всех настроенных пользователей
по их списку монет, ищет сигналы и присылает их в Telegram.
"""

import logging

from telegram.ext import ContextTypes

import storage
import signals

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 30 * 60  # 30 минут


async def scan_market(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Запускается планировщиком каждые SCAN_INTERVAL_SECONDS.
    Проходит по всем пользователям и их монетам, ищет сигналы.
    """
    users = storage.get_all_configured_users()

    if not users:
        return

    # Собираем уникальный набор монет, чтобы не запрашивать одно и то же
    # с Binance несколько раз для разных пользователей с похожими списками.
    # (Для простоты MVP считаем сигнал отдельно на каждого юзера+монету —
    # запросов всё равно немного: до ~5 монет x число пользователей)

    for user in users:
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
                logger.warning(f"Ошибка анализа {coin} для пользователя {user['user_id']}: {e}")
                continue

            if result is None:
                continue

            trade = result["trade"]
            direction = trade["direction"]
            entry_price = trade["entry_price"]

            # Проверяем, не отправляли ли уже похожий сигнал недавно
            if storage.was_signal_sent_recently(user["user_id"], coin, direction, entry_price):
                continue

            text = format_signal_message(result, user)

            try:
                await context.bot.send_message(
                    chat_id=user["user_id"],
                    text=text,
                    parse_mode="Markdown",
                )
                storage.mark_signal_sent(user["user_id"], coin, direction, entry_price)
                logger.info(f"Сигнал {coin} {direction} отправлен пользователю {user['user_id']}")
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение пользователю {user['user_id']}: {e}")


def format_signal_message(result: dict, user: dict) -> str:
    """Форматирует сигнал в текст сообщения (используется и в /signal, и в scheduler)."""
    trade = result["trade"]
    direction_emoji = "🟢 LONG" if trade["direction"] == "LONG" else "🔴 SHORT"

    return (
        f"🚀 *НОВЫЙ СИГНАЛ: {result['coin']}/USDT {direction_emoji}*\n\n"
        f"📊 Тренд 4h: {result['trend_4h']}\n"
        f"📈 RSI 1h: {result['rsi_1h']:.1f}\n\n"
        f"💰 Цена входа: {trade['entry_price']:.4f}\n"
        f"🛑 Стоп-лосс: {trade['stop_loss']:.4f} (-{trade['sl_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 1: {trade['take_profit_1']:.4f} (+{trade['tp1_percent']:.2f}%)\n"
        f"🎯 Тейк-профит 2: {trade['take_profit_2']:.4f}\n\n"
        f"📐 R/R: 1:{trade['risk_reward']:.2f}\n\n"
        f"💼 Депозит: {user['deposit']} USDT\n"
        f"⚠️ Риск: {user['risk_percent']}% = {trade['risk_amount_usd']:.2f} USDT\n"
        f"📦 Размер позиции: {trade['position_size_usd']:.2f} USDT "
        f"(плечо x{trade['leverage']})\n"
        f"💵 Маржа: {trade['margin_required']:.2f} USDT"
    )
