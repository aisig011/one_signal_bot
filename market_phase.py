"""
market_phase.py
Определение фазы рынка по индикаторам (без GPT — быстро, без зависаний).

CHAOS:      ATR за 5 свечей > 5% цены (высокая волатильность)
RANGE:      наклон EMA20 близок к нулю (< 0.6%) И диапазон < 12%
TREND_UP:   наклон EMA20 положительный (>= 0.6%)
TREND_DOWN: наклон EMA20 отрицательный (<= -0.6%)

Порог 0.6 подобран по реальным логам: наклоны монет сбиваются в две
группы — вялые (0.14-0.48) и медленно ползущие (0.63-0.91), между ними
пустота. Старый порог 0.8 резал вторую группу пополам: 0.79 → боковик,
0.87 → тренд, хотя это одно и то же. Из-за этого в растущем рынке монеты
считались боковиком, стратегия отбоя предлагала только SHORT у верхней
границы, а BTC-фильтр их запрещал → сигналов не было вообще.

Раньше здесь был вызов GPT для проверки CHAOS, но он замедлял скан
(запрос на каждую монету) и вызывал зависания. Индикаторный ATR-чек
ловит резкую волатильность сам.
"""

import time
import logging

logger = logging.getLogger("market_phase")

# Порог наклона EMA20 (%) — граница между боковиком и трендом.
# Подобран по логам: реальная пустота между группами 0.48 и 0.63.
# Слишком много трендовых сигналов на вялом движении → подними до 0.7.
# Медленный тренд всё ещё зовётся боковиком → опусти до 0.5.
TREND_SLOPE_THRESHOLD = 0.6

_phase_cache = {}
# Свечи часовые — фаза не может меняться быстрее. 4 часа было слишком долго:
# рынок разворачивался, а бот ещё несколько часов держал старый вердикт.
CACHE_TTL_SECONDS = 1 * 60 * 60  # 1 час


def _calc_phase_by_indicators(df, coin: str = "?") -> dict:
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
        f"phase_indicators {coin}: ema20_slope={ema20_slope_pct:.2f}%, "
        f"range_width={range_width_pct:.2f}%, atr_pct={atr_pct:.2f}%"
    )

    if atr_pct > 5.0:
        return {"phase": "CHAOS", "reason": f"высокая волатильность ATR {atr_pct:.1f}%"}

    if abs(ema20_slope_pct) < TREND_SLOPE_THRESHOLD and range_width_pct < 12.0:
        return {"phase": "RANGE", "reason": f"EMA20 плоская ({ema20_slope_pct:.2f}%), диапазон {range_width_pct:.1f}%"}

    if ema20_slope_pct >= TREND_SLOPE_THRESHOLD:
        pos = "выше" if price > ema50_now else "ниже"
        return {"phase": "TREND_UP", "reason": f"EMA20 растёт ({ema20_slope_pct:.2f}%), цена {pos} EMA50"}

    if ema20_slope_pct <= -TREND_SLOPE_THRESHOLD:
        pos = "выше" if price > ema50_now else "ниже"
        return {"phase": "TREND_DOWN", "reason": f"EMA20 падает ({ema20_slope_pct:.2f}%), цена {pos} EMA50"}

    return {"phase": "RANGE", "reason": f"неопределённость, EMA20 slope={ema20_slope_pct:.2f}%"}


def detect_phase(coin: str, df) -> dict:
    """Определяет фазу рынка (по индикаторам, кэш 4 часа)."""
    cached = _phase_cache.get(coin)
    if cached and (time.time() - cached["ts"] < CACHE_TTL_SECONDS):
        logger.info(f"market_phase: {coin} — фаза из кэша: {cached['phase']} ({cached['reason']})")
        return {"phase": cached["phase"], "reason": cached["reason"]}

    result = _calc_phase_by_indicators(df, coin)
    phase = result["phase"]
    reason = result["reason"]

    _phase_cache[coin] = {"phase": phase, "reason": reason, "ts": time.time()}

    logger.info(f"market_phase: {coin} — фаза: {phase} ({reason})")
    return {"phase": phase, "reason": reason}
