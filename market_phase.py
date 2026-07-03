"""
market_phase.py
Определение фазы рынка по индикаторам (без GPT — быстро, без зависаний).

CHAOS:      ATR за 5 свечей > 5% цены (высокая волатильность)
RANGE:      наклон EMA20 близок к нулю (< 0.8%) И диапазон < 12%
TREND_UP:   наклон EMA20 положительный (>= 0.8%)
TREND_DOWN: наклон EMA20 отрицательный (<= -0.8%)

Раньше здесь был вызов GPT для проверки CHAOS, но он замедлял скан
(запрос на каждую монету) и вызывал зависания. Индикаторный ATR-чек
ловит резкую волатильность сам.
"""

import time
import logging

logger = logging.getLogger("market_phase")

_phase_cache = {}
CACHE_TTL_SECONDS = 4 * 60 * 60  # 4 часа


def _calc_phase_by_indicators(df) -> dict:
    """Определяет фазу по индикаторам."""
    last = df.iloc[-1]
    recent_20 = df.tail(20)

    price = last["close"]
    ema20_now = last["ema_20"]
    ema50_now = last["ema_50"]

    ema20_prev = df.iloc[-10]["ema_20"] if len(df) >= 10 else ema20_now
    ema20_slope_pct = (ema20_now - ema20_prev) / ema20_prev * 100 if ema20_prev > 0 else 0

    high_20 = recent_20["high"].max()
    low_20 = recent_20["low"].min()
    range_width_pct = (high_20 - low_20) / low_20 * 100 if low_20 > 0 else 0

    recent_5 = df.tail(5)
    atr_pct = (recent_5["high"].max() - recent_5["low"].min()) / price * 100 if price > 0 else 0

    logger.info(
        f"phase_indicators: ema20_slope={ema20_slope_pct:.2f}%, "
        f"range_width={range_width_pct:.2f}%, atr_pct={atr_pct:.2f}%"
    )

    if atr_pct > 5.0:
        return {"phase": "CHAOS", "reason": f"высокая волатильность ATR {atr_pct:.1f}%"}

    if abs(ema20_slope_pct) < 0.8 and range_width_pct < 12.0:
        return {"phase": "RANGE", "reason": f"EMA20 плоская ({ema20_slope_pct:.2f}%), диапазон {range_width_pct:.1f}%"}

    if ema20_slope_pct >= 0.8:
        pos = "выше" if price > ema50_now else "ниже"
        return {"phase": "TREND_UP", "reason": f"EMA20 растёт ({ema20_slope_pct:.2f}%), цена {pos} EMA50"}

    if ema20_slope_pct <= -0.8:
        pos = "выше" if price > ema50_now else "ниже"
        return {"phase": "TREND_DOWN", "reason": f"EMA20 падает ({ema20_slope_pct:.2f}%), цена {pos} EMA50"}

    return {"phase": "RANGE", "reason": f"неопределённость, EMA20 slope={ema20_slope_pct:.2f}%"}


def detect_phase(coin: str, df) -> dict:
    """Определяет фазу рынка (по индикаторам, кэш 4 часа)."""
    cached = _phase_cache.get(coin)
    if cached and (time.time() - cached["ts"] < CACHE_TTL_SECONDS):
        logger.info(f"market_phase: {coin} — фаза из кэша: {cached['phase']} ({cached['reason']})")
        return {"phase": cached["phase"], "reason": cached["reason"]}

    result = _calc_phase_by_indicators(df)
    phase = result["phase"]
    reason = result["reason"]

    _phase_cache[coin] = {"phase": phase, "reason": reason, "ts": time.time()}

    logger.info(f"market_phase: {coin} — фаза: {phase} ({reason})")
    return {"phase": phase, "reason": reason}
