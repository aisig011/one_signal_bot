"""
risk_manager.py
Расчёт стоп-лосса, тейк-профитов, размера позиции и R/R
на основе депозита и риска пользователя.
"""

import pandas as pd

# Максимальная доля депозита, которую можно выделить под маржу одной сделки.
MAX_MARGIN_FRACTION = 0.40  # 40% депозита

# Максимальный R/R — если TP оказывается дальше, обрезаем его до этого уровня.
MAX_RR = 6.0

# Минимальное расстояние стоп-лосса от цены входа (в %).
# Защищает от микро-стопов (0.3–0.5%), которые выбивает рыночным шумом.
# Если расчётный стоп ближе — отодвигаем его до этого минимума.
MIN_SL_PERCENT = 1.0  # 1%


def find_support_resistance(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    Находит последние локальные минимум и максимум за `lookback` свечей —
    это будущие уровни поддержки и сопротивления.
    """
    recent = df.tail(lookback)
    return {
        "support": recent["low"].min(),
        "resistance": recent["high"].max(),
    }


def calculate_liquidation_price(direction: str, entry_price: float, leverage: int, maintenance_margin_rate: float = 0.005) -> float:
    """
    Приблизительная цена ликвидации для изолированной маржи на Binance Futures.
    """
    if direction == "LONG":
        return entry_price * (1 - 1 / leverage + maintenance_margin_rate)
    else:  # SHORT
        return entry_price * (1 + 1 / leverage - maintenance_margin_rate)


def find_safe_leverage(direction: str, entry_price: float, stop_loss: float, max_leverage: int = 5, safety_margin: float = 1.3) -> int:
    """
    Подбирает максимально допустимое плечо так, чтобы цена ликвидации
    была дальше стоп-лосса с запасом `safety_margin`.
    """
    risk_distance = abs(entry_price - stop_loss)

    for leverage in range(max_leverage, 0, -1):
        liq_price = calculate_liquidation_price(direction, entry_price, leverage)
        liq_distance = abs(entry_price - liq_price)

        if liq_distance >= risk_distance * safety_margin:
            return leverage

    return 1


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

    Защиты:
    - SL не ближе MIN_SL_PERCENT от входа (защита от микро-стопов и шума)
    - R/R обрезается сверху до MAX_RR (TP не дальше разумного)
    - маржа не больше MAX_MARGIN_FRACTION депозита (иначе уменьшаем позицию)
    """
    buffer_pct = 0.002  # 0.2% буфер за уровень

    if direction == "LONG":
        stop_loss = support * (1 - buffer_pct)
        risk_distance = entry_price - stop_loss

        if risk_distance <= 0:
            return None

        # --- Минимальное расстояние SL ---
        # Если стоп ближе MIN_SL_PERCENT от входа — отодвигаем его дальше.
        # Микро-стоп (0.3–0.5%) выбивается шумом, это убивало сделки.
        min_sl_distance = entry_price * (MIN_SL_PERCENT / 100)
        if risk_distance < min_sl_distance:
            stop_loss = entry_price - min_sl_distance
            risk_distance = min_sl_distance

        take_profit_1 = resistance
        reward_distance_1 = take_profit_1 - entry_price

    elif direction == "SHORT":
        stop_loss = resistance * (1 + buffer_pct)
        risk_distance = stop_loss - entry_price

        if risk_distance <= 0:
            return None

        # --- Минимальное расстояние SL ---
        min_sl_distance = entry_price * (MIN_SL_PERCENT / 100)
        if risk_distance < min_sl_distance:
            stop_loss = entry_price + min_sl_distance
            risk_distance = min_sl_distance

        take_profit_1 = support
        reward_distance_1 = entry_price - take_profit_1

    else:
        raise ValueError("direction должен быть 'LONG' или 'SHORT'")

    if reward_distance_1 <= 0:
        return None  # нет пространства для движения в нужную сторону

    # R/R считаем по TP1 (с учётом возможно отодвинутого стопа)
    risk_reward = reward_distance_1 / risk_distance

    if risk_reward < min_rr:
        return None  # R/R недостаточен (в т.ч. если стоп отодвинули и R/R упал)

    # --- Обрезаем R/R сверху до MAX_RR ---
    if risk_reward > MAX_RR:
        reward_distance_1 = risk_distance * MAX_RR
        if direction == "LONG":
            take_profit_1 = entry_price + reward_distance_1
        else:
            take_profit_1 = entry_price - reward_distance_1
        risk_reward = MAX_RR

    # TP2 = дальше TP1 с тем же шагом
    if direction == "LONG":
        take_profit_2 = entry_price + reward_distance_1 * 1.7
    else:
        take_profit_2 = entry_price - reward_distance_1 * 1.7

    # --- Расчёт размера позиции по риску ---
    risk_amount_usd = deposit * (risk_percent / 100)
    position_size_coin = risk_amount_usd / risk_distance
    position_size_usd = position_size_coin * entry_price

    # --- Подбор безопасного плеча ---
    safe_leverage = find_safe_leverage(direction, entry_price, stop_loss, max_leverage=leverage)
    leverage_reduced = safe_leverage < leverage
    used_leverage = safe_leverage

    margin_required = position_size_usd / used_leverage

    # --- Защита от слишком большой маржи ---
    max_margin = deposit * MAX_MARGIN_FRACTION
    margin_capped = False
    if margin_required > max_margin:
        scale = max_margin / margin_required
        position_size_usd *= scale
        position_size_coin *= scale
        risk_amount_usd *= scale
        margin_required = max_margin
        margin_capped = True

    liquidation_price = calculate_liquidation_price(direction, entry_price, used_leverage)

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
        "margin_capped": margin_capped,
        "leverage": used_leverage,
        "requested_leverage": leverage,
        "leverage_reduced": leverage_reduced,
        "liquidation_price": liquidation_price,
        "sl_percent": abs(risk_distance / entry_price) * 100,
        "tp1_percent": abs(reward_distance_1 / entry_price) * 100,
    }
