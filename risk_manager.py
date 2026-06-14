"""
risk_manager.py
Расчёт стоп-лосса, тейк-профитов, размера позиции и R/R
на основе депозита и риска пользователя.
"""

import pandas as pd


def find_support_resistance(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    Находит последние локальные минимум и максимум за `lookback` свечей —
    это будущие уровни поддержки и сопротивления.

    df: DataFrame со свечами (нужны колонки 'high', 'low')
    lookback: сколько последних свечей анализировать
    """
    recent = df.tail(lookback)
    return {
        "support": recent["low"].min(),
        "resistance": recent["high"].max(),
    }


def calculate_trade(
    direction: str,
    entry_price: float,
    support: float,
    resistance: float,
    deposit: float,
    risk_percent: float,
    min_rr: float = 2.0,
    leverage: int = 5,
) -> dict | None:
    """
    Рассчитывает параметры сделки: SL, TP, размер позиции, R/R.

    direction: "LONG" или "SHORT"
    entry_price: цена входа (текущая цена)
    support: уровень поддержки (для LONG — стоп ниже этого уровня)
    resistance: уровень сопротивления (для SHORT — стоп выше этого уровня)
    deposit: депозит пользователя в USDT
    risk_percent: % риска на сделку (например 1 = 1%)
    min_rr: минимальное допустимое соотношение risk/reward
    leverage: плечо для расчёта размера позиции

    Возвращает словарь с параметрами сделки, или None если R/R хуже min_rr.
    """
    # Небольшой запас (буфер) за уровень, чтобы стоп не выбивался "по фитилю"
    buffer_pct = 0.002  # 0.2%

    if direction == "LONG":
        stop_loss = support * (1 - buffer_pct)
        risk_distance = entry_price - stop_loss

        if risk_distance <= 0:
            return None  # уровень поддержки выше цены входа — некорректная ситуация

        # TP1 = ближайшее сопротивление, TP2 = дальше с тем же шагом
        take_profit_1 = resistance
        reward_distance_1 = take_profit_1 - entry_price
        take_profit_2 = entry_price + reward_distance_1 * 1.7

    elif direction == "SHORT":
        stop_loss = resistance * (1 + buffer_pct)
        risk_distance = stop_loss - entry_price

        if risk_distance <= 0:
            return None  # уровень сопротивления ниже цены входа — некорректная ситуация

        take_profit_1 = support
        reward_distance_1 = entry_price - take_profit_1
        take_profit_2 = entry_price - reward_distance_1 * 1.7

    else:
        raise ValueError("direction должен быть 'LONG' или 'SHORT'")

    if reward_distance_1 <= 0:
        return None  # нет пространства для движения в нужную сторону

    # R/R считаем по TP1 (более консервативная оценка)
    risk_reward = reward_distance_1 / risk_distance

    if risk_reward < min_rr:
        return None  # сигнал отбрасывается, R/R недостаточен

    # --- Расчёт размера позиции ---
    risk_amount_usd = deposit * (risk_percent / 100)  # сколько $ готовы потерять

    # Размер позиции (в монете) = риск в $ / расстояние до стопа (в $)
    position_size_coin = risk_amount_usd / risk_distance

    # Номинальный объём позиции в USDT
    position_size_usd = position_size_coin * entry_price

    # Маржа, которую нужно выделить с учётом плеча
    margin_required = position_size_usd / leverage

    return {
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward": risk_reward,
        "risk_amount_usd": risk_amount_usd,
        "position_size_coin": position_size_coin,
        "position_size_usd": position_size_usd,
        "margin_required": margin_required,
        "leverage": leverage,
        "sl_percent": abs(risk_distance / entry_price) * 100,
        "tp1_percent": abs(reward_distance_1 / entry_price) * 100,
    }
