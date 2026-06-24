"""
signals.py
Логика поиска точек входа: тренд по 1h определяет направление,
4h используется как дополнительный контекст, сигнал входа ищется
на 1h по RSI/MACD/EMA.
"""

import pandas as pd

import market
import indicators
import risk_manager
import market_phase


def find_signal(coin: str, deposit: float, risk_percent: float, min_rr: float = 2.0) -> dict | None:
    """
    Ищет торговый сигнал по монете.

    Логика (v3, + пуллбэк):
    1. Тренд на 1h (по EMA50, см. indicators.get_trend) определяет
       допустимое направление сделки (LONG в bullish, SHORT в bearish).
       Тренд на 4h передаётся как доп. контекст в сигнале, но не
       блокирует сделку, если он "flat" — рынок часто переходит из
       одной фазы в другую, и 4h не всегда успевает за 1h.
    2. На 1h ищем точку входа (любое из условий):
       - Разворот: RSI выходит из зоны перепроданности/перекупленности
         (был <35/>65, сейчас разворачивается)
       - Пересечение MACD сигнальной линии в сторону тренда
       - Пуллбэк: цена откатилась близко к EMA20 (в пределах 1.2%)
         в рамках тренда, и RSI не в противоположной крайней зоне
         (для LONG: RSI < 65; для SHORT: RSI > 35) — классический
         вход "по тренду на откате"
    3. Если условия совпали — считаем SL/TP по уровням 1h и риск-менеджмент.
    4. Если R/R хуже min_rr — сигнал не возвращается.

    Возвращает словарь с данными сигнала, или None если сигнала нет.
    """
    symbol = market.get_symbol(coin)

    # --- Точка входа и тренд на 1h ---
    df_1h = market.get_klines(symbol, "1h", limit=250)
    df_1h = indicators.add_indicators(df_1h)

    import logging
    logger = logging.getLogger("signals")

    trend_1h = indicators.get_trend(df_1h)

    last = df_1h.iloc[-1]
    prev = df_1h.iloc[-2]

    ema20_dist = abs(last["close"] - last["ema_20"]) / last["ema_20"] * 100
    logger.info(
        f"diag {coin}: trend_1h={trend_1h}, rsi={last['rsi']:.1f}, "
        f"macd_diff={last['macd_diff']:.4f} (prev {prev['macd_diff']:.4f}), "
        f"ema20_dist={ema20_dist:.2f}%"
    )

    # --- Определение фазы рынка через ИИ ---
    phase_info = market_phase.detect_phase(coin, df_1h)
    phase = phase_info["phase"]
    logger.info(f"diag {coin}: market_phase={phase} ({phase_info['reason']})")

    # CHAOS — высокая волатильность/неопределённость, не торгуем
    if phase == "CHAOS":
        logger.info(f"diag {coin}: пропуск — фаза CHAOS")
        return None

    # Экстремальная перепроданность/перекупленность в тренде — опасный
    # момент: вход против тренда (ловля дна/хая) или поздний вход по тренду.
    # Лучше переждать, пока RSI не вернётся в рабочую зону.
    if last["rsi"] < 25 or last["rsi"] > 75:
        logger.info(f"diag {coin}: пропуск — экстремальный RSI {last['rsi']:.1f}")
        return None

    if trend_1h == "flat":
        return None  # нет чёткого тренда на 1h — не торгуем

    direction = None
    entry_reason = None

    # Расстояние цены от EMA20 в процентах (для пуллбэк-условия)
    ema20_distance_pct = abs(last["close"] - last["ema_20"]) / last["ema_20"] * 100
    near_ema20 = ema20_distance_pct <= 1.2

    if trend_1h == "bullish":
        # Разворот из перепроданности
        rsi_recovering = prev["rsi"] < 35 and last["rsi"] > prev["rsi"]
        # MACD пересекает сигнальную линию вверх
        macd_cross_up = prev["macd_diff"] <= 0 and last["macd_diff"] > 0
        # Пуллбэк к EMA20 в рамках тренда (не на перекупленности)
        pullback = near_ema20 and last["rsi"] < 65 and last["close"] >= last["ema_50"]

        if rsi_recovering:
            direction = "LONG"
            entry_reason = "разворот RSI из перепроданности"
        elif macd_cross_up:
            direction = "LONG"
            entry_reason = "пересечение MACD вверх"
        elif pullback:
            direction = "LONG"
            entry_reason = "пуллбэк к EMA20 в восходящем тренде"

    elif trend_1h == "bearish":
        # Симметрично для SHORT
        rsi_recovering = prev["rsi"] > 65 and last["rsi"] < prev["rsi"]
        macd_cross_down = prev["macd_diff"] >= 0 and last["macd_diff"] < 0
        pullback = near_ema20 and last["rsi"] > 35 and last["close"] <= last["ema_50"]

        if rsi_recovering:
            direction = "SHORT"
            entry_reason = "разворот RSI из перекупленности"
        elif macd_cross_down:
            direction = "SHORT"
            entry_reason = "пересечение MACD вниз"
        elif pullback:
            direction = "SHORT"
            entry_reason = "пуллбэк к EMA20 в нисходящем тренде"

    if direction is None:
        return None  # нет точки входа прямо сейчас

    # --- Подтверждение объёмом ---
    # Движение считается подтверждённым, если объём на свече входа
    # не ниже 70% от среднего за 20 свечей. Это отсекает совсем вялые
    # движения на мёртвом объёме, но не блокирует нормальные сигналы.
    volume_confirmed = True
    if "volume_avg" in df_1h.columns and not pd.isna(last.get("volume_avg")):
        volume_ratio = last["volume"] / last["volume_avg"] if last["volume_avg"] > 0 else 0
        volume_confirmed = volume_ratio >= 0.7
        logger.info(f"diag {coin}: volume_ratio={volume_ratio:.2f}, confirmed={volume_confirmed}")
        if not volume_confirmed:
            return None  # объём не подтверждает движение — пропускаем

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
        "entry_reason": entry_reason,
        "market_phase": phase,
        "volume_ratio": (last["volume"] / last["volume_avg"]) if ("volume_avg" in df_1h.columns and not pd.isna(last.get("volume_avg")) and last["volume_avg"] > 0) else None,
        "trade": trade,
    }
