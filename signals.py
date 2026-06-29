"""
signals.py
Логика поиска точек входа.

v4: тренд по 1h определяет направление для трендовой логики,
фаза рынка (market_phase) выбирает стратегию:
- CHAOS → не торгуем
- RANGE → отбой от границ диапазона (find_range_signal)
- TREND_UP / TREND_DOWN → трендовая логика (пуллбэк/RSI/MACD)
"""

import pandas as pd

import market
import indicators
import risk_manager
import market_phase


def find_range_signal(coin: str, symbol: str, df_1h: pd.DataFrame,
                      deposit: float, risk_percent: float,
                      min_rr: float = 2.0) -> dict | None:
    """
    Стратегия отбоя от границ диапазона (фаза RANGE / боковик).

    1. Границы диапазона = min/max за 48 свечей 1h (2 дня).
    2. Ширина диапазона >= 3% (иначе пила).
    3. Цена в зоне отбоя от границы (0.3–2%):
       - у нижней → LONG, у верхней → SHORT
       - ближе 0.3% = почти на границе/пробой (опасно), дальше 2% = не дошла
    4. RSI в рабочей зоне 30–70, объём >= 0.7.
    5. SL за границей, TP к противоположной границе (или к середине).
       Минимальный SL и обрезка R/R — в risk_manager.calculate_trade.
    """
    import logging
    logger = logging.getLogger("signals")

    last = df_1h.iloc[-1]
    close = last["close"]

    lookback = 48
    window = df_1h.iloc[-lookback:]
    range_high = window["high"].max()
    range_low = window["low"].min()
    range_width_pct = (range_high - range_low) / range_low * 100

    logger.info(
        f"diag {coin} [RANGE]: close={close:.4f}, "
        f"range_low={range_low:.4f}, range_high={range_high:.4f}, "
        f"width={range_width_pct:.2f}%"
    )

    if range_width_pct < 3.0:
        logger.info(f"diag {coin} [RANGE]: пропуск — диапазон слишком узкий ({range_width_pct:.2f}%)")
        return None

    rsi = last["rsi"]
    if rsi < 30 or rsi > 70:
        logger.info(f"diag {coin} [RANGE]: пропуск — RSI вне рабочей зоны ({rsi:.1f})")
        return None

    volume_ratio = None
    if "volume_avg" in df_1h.columns and not pd.isna(last.get("volume_avg")):
        volume_ratio = last["volume"] / last["volume_avg"] if last["volume_avg"] > 0 else 0
        logger.info(f"diag {coin} [RANGE]: volume_ratio={volume_ratio:.2f}")
        if volume_ratio < 0.7:
            logger.info(f"diag {coin} [RANGE]: пропуск — низкий объём")
            return None

    near_low_pct = (close - range_low) / range_low * 100
    near_high_pct = (range_high - close) / range_high * 100

    direction = None
    entry_reason = None

    # Зона отбоя 0.3–2%: не на самой границе (ложный пробой) и не далеко от неё
    if 0.3 <= near_low_pct <= 2.0:
        direction = "LONG"
        entry_reason = f"отбой от нижней границы диапазона ({range_low:.4f})"
    elif 0.3 <= near_high_pct <= 2.0:
        direction = "SHORT"
        entry_reason = f"отбой от верхней границы диапазона ({range_high:.4f})"
    else:
        logger.info(
            f"diag {coin} [RANGE]: пропуск — цена вне зоны отбоя "
            f"(+{near_low_pct:.1f}% от дна, -{near_high_pct:.1f}% от верха)"
        )
        return None

    range_mid = (range_high + range_low) / 2

    if direction == "LONG":
        trade = risk_manager.calculate_trade(
            direction=direction, entry_price=close,
            support=range_low, resistance=range_high,
            deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
        )
        tp_target = "верхняя граница диапазона"
        if trade is None:
            trade = risk_manager.calculate_trade(
                direction=direction, entry_price=close,
                support=range_low, resistance=range_mid,
                deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
            )
            tp_target = "середина диапазона"
    else:  # SHORT
        trade = risk_manager.calculate_trade(
            direction=direction, entry_price=close,
            support=range_low, resistance=range_high,
            deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
        )
        tp_target = "нижняя граница диапазона"
        if trade is None:
            trade = risk_manager.calculate_trade(
                direction=direction, entry_price=close,
                support=range_mid, resistance=range_high,
                deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
            )
            tp_target = "середина диапазона"

    if trade is None:
        logger.info(f"diag {coin} [RANGE]: пропуск — risk_manager вернул None (R/R недостаточен)")
        return None

    logger.info(
        f"diag {coin} [RANGE]: сигнал {direction}, "
        f"sl={trade['stop_loss']:.4f}, tp1={trade['take_profit_1']:.4f} ({tp_target}), "
        f"rr={trade['risk_reward']:.2f}"
    )

    return {
        "coin": coin,
        "symbol": symbol,
        "trend_1h": "range",
        "trend_4h": None,
        "rsi_1h": rsi,
        "macd_signal_1h": "bullish" if last["macd_diff"] > 0 else "bearish",
        "entry_reason": entry_reason,
        "market_phase": "RANGE",
        "volume_ratio": volume_ratio,
        "range_info": {
            "low": range_low,
            "high": range_high,
            "mid": range_mid,
            "width_pct": range_width_pct,
            "tp_target": tp_target,
        },
        "trade": trade,
    }


def find_signal(coin: str, deposit: float, risk_percent: float, min_rr: float = 2.0) -> dict | None:
    """
    Ищет торговый сигнал по монете.

    1. Фаза рынка (market_phase) выбирает стратегию.
    2. CHAOS → не торгуем. RANGE → find_range_signal. TREND → трендовая логика.
    3. Трендовая: тренд 1h определяет направление, вход по
       развороту RSI / пересечению MACD / пуллбэку к EMA20.
    4. R/R хуже min_rr — сигнал не возвращается.
    """
    symbol = market.get_symbol(coin)

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

    # --- Определение фазы рынка ---
    phase_info = market_phase.detect_phase(coin, df_1h)
    phase = phase_info["phase"]
    logger.info(f"diag {coin}: market_phase={phase} ({phase_info['reason']})")

    if phase == "CHAOS":
        logger.info(f"diag {coin}: пропуск — фаза CHAOS")
        return None

    # --- RANGE — стратегия отбоя от границ диапазона ---
    if phase == "RANGE":
        result = find_range_signal(coin, symbol, df_1h, deposit, risk_percent, min_rr=min_rr)
        if result is not None:
            df_4h = market.get_klines(symbol, "4h", limit=250)
            df_4h = indicators.add_indicators(df_4h)
            result["trend_4h"] = indicators.get_trend(df_4h)
        return result

    # --- Трендовая логика (TREND_UP / TREND_DOWN) ---

    if last["rsi"] < 25 or last["rsi"] > 75:
        logger.info(f"diag {coin}: пропуск — экстремальный RSI {last['rsi']:.1f}")
        return None

    if trend_1h == "flat":
        return None

    direction = None
    entry_reason = None

    ema20_distance_pct = abs(last["close"] - last["ema_20"]) / last["ema_20"] * 100
    near_ema20 = ema20_distance_pct <= 1.2

    if trend_1h == "bullish":
        rsi_recovering = prev["rsi"] < 35 and last["rsi"] > prev["rsi"]
        macd_cross_up = prev["macd_diff"] <= 0 and last["macd_diff"] > 0
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
        return None

    # --- Подтверждение объёмом ---
    if "volume_avg" in df_1h.columns and not pd.isna(last.get("volume_avg")):
        volume_ratio = last["volume"] / last["volume_avg"] if last["volume_avg"] > 0 else 0
        logger.info(f"diag {coin}: volume_ratio={volume_ratio:.2f}")
        if volume_ratio < 0.7:
            return None
    else:
        volume_ratio = None

    df_4h = market.get_klines(symbol, "4h", limit=250)
    df_4h = indicators.add_indicators(df_4h)
    trend_4h = indicators.get_trend(df_4h)

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
        return None

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
