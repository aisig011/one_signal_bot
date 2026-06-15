"""
signals.py
Логика поиска точек входа: тренд по 1h определяет направление,
4h используется как дополнительный контекст, сигнал входа ищется
на 1h по RSI/MACD/EMA.
"""

import market
import indicators
import risk_manager


def find_signal(coin: str, deposit: float, risk_percent: float, min_rr: float = 2.0) -> dict | None:
    """
    Ищет торговый сигнал по монете.

    Логика (v2, смягчённая):
    1. Тренд на 1h (по EMA50, см. indicators.get_trend) определяет
       допустимое направление сделки (LONG в bullish, SHORT в bearish).
       Тренд на 4h передаётся как доп. контекст в сигнале, но не
       блокирует сделку, если он "flat" — рынок часто переходит из
       одной фазы в другую, и 4h не всегда успевает за 1h.
    2. На 1h ищем точку входа:
       - LONG: RSI выходит из зоны перепроданности (был <35, сейчас растёт)
               ИЛИ MACD только что пересёк сигнальную линию вверх
       - SHORT: симметрично для перепроданности/верхнего пересечения
    3. Если условия совпали — считаем SL/TP по уровням 1h и риск-менеджмент.
    4. Если R/R хуже min_rr — сигнал не возвращается.

    Возвращает словарь с данными сигнала, или None если сигнала нет.
    """
    symbol = market.get_symbol(coin)

    # --- Точка входа и тренд на 1h ---
    df_1h = market.get_klines(symbol, "1h", limit=250)
    df_1h = indicators.add_indicators(df_1h)

    trend_1h = indicators.get_trend(df_1h)

    if trend_1h == "flat":
        return None  # нет чёткого тренда на 1h — не торгуем

    last = df_1h.iloc[-1]
    prev = df_1h.iloc[-2]

    direction = None

    if trend_1h == "bullish":
        # Ищем точку входа в LONG: RSI разворачивается из перепроданности,
        # либо MACD пересекает сигнальную линию вверх
        rsi_recovering = prev["rsi"] < 35 and last["rsi"] > prev["rsi"]
        macd_cross_up = prev["macd_diff"] <= 0 and last["macd_diff"] > 0

        if rsi_recovering or macd_cross_up:
            direction = "LONG"

    elif trend_1h == "bearish":
        # Симметрично для SHORT
        rsi_recovering = prev["rsi"] > 65 and last["rsi"] < prev["rsi"]
        macd_cross_down = prev["macd_diff"] >= 0 and last["macd_diff"] < 0

        if rsi_recovering or macd_cross_down:
            direction = "SHORT"

    if direction is None:
        return None  # нет точки входа прямо сейчас

    # --- Тренд на 4h как доп. контекст (информационно, не блокирует) ---
    df_4h = market.get_klines(symbol, "4h", limit=250)
    df_4h = indicators.add_indicators(df_4h)
    trend_4h = indicators.get_trend(df_4h)

    # --- Уровни поддержки/сопротивления на 1h ---
    levels = risk_manager.find_support_resistance(df_1h, lookback=30)

    entry_price = last["close"]

    trade = risk_manager.calculate_trade(
        direction=direction,
        entry_price=entry_price,
        support=levels["support"],
        resistance=levels["resistance"],
        deposit=deposit,
        risk_percent=risk_percent,
        min_rr=min_rr,
    )

    if trade is None:
        return None  # R/R недостаточен или уровни некорректны

    return {
        "coin": coin,
        "symbol": symbol,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "rsi_1h": last["rsi"],
        "macd_signal_1h": "bullish" if last["macd_diff"] > 0 else "bearish",
        "trade": trade,
    }
