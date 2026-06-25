"""
market_phase.py
Определение фазы рынка.

Логика (v2):
- RANGE / TREND_UP / TREND_DOWN — определяется по индикаторам (быстро, без API)
- CHAOS — определяется через GPT (его сильная сторона: паника, новости, резкие скачки)

GPT вызывается только когда индикаторы показывают нормальную картину —
просто проверяем "нет ли скрытого хаоса". Это убирает конфликты между
GPT и EMA, и снижает количество запросов к OpenAI.
"""

import os
import json
import time
import logging

import requests
import pandas as pd

logger = logging.getLogger("market_phase")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Кэш по монетам: {coin: {"phase": ..., "reason": ..., "ts": ...}}
_phase_cache = {}
CACHE_TTL_SECONDS = 4 * 60 * 60  # 4 часа — фаза не меняется каждые полчаса


def _calc_phase_by_indicators(df) -> dict:
    """
    Определяет фазу по индикаторам — без GPT.

    CHAOS:      ATR/цена > 5% (очень высокая волатильность)
    RANGE:      наклон EMA20 за 10 свечей близок к нулю (< 1.5%)
                И ширина диапазона за 20 свечей < 12%
    TREND_UP:   наклон EMA20 положительный, цена выше EMA50
    TREND_DOWN: наклон EMA20 отрицательный, цена ниже EMA50
    """
    last = df.iloc[-1]
    recent_20 = df.tail(20)

    price = last["close"]
    ema20_now = last["ema_20"]
    ema50_now = last["ema_50"]

    # Наклон EMA20 за 10 свечей в %
    ema20_prev = df.iloc[-10]["ema_20"] if len(df) >= 10 else ema20_now
    ema20_slope_pct = (ema20_now - ema20_prev) / ema20_prev * 100 if ema20_prev > 0 else 0

    # Ширина диапазона за 20 свечей в %
    high_20 = recent_20["high"].max()
    low_20 = recent_20["low"].min()
    range_width_pct = (high_20 - low_20) / low_20 * 100 if low_20 > 0 else 0

    # ATR как мера волатильности (если есть, иначе считаем через диапазон)
    # Используем ширину диапазона за 5 свечей как упрощённый ATR
    recent_5 = df.tail(5)
    atr_pct = (recent_5["high"].max() - recent_5["low"].min()) / price * 100 if price > 0 else 0

    logger.info(
        f"phase_indicators: ema20_slope={ema20_slope_pct:.2f}%, "
        f"range_width={range_width_pct:.2f}%, atr_pct={atr_pct:.2f}%"
    )

    # CHAOS по индикаторам: ATR за 5 свечей > 5% цены
    if atr_pct > 5.0:
        return {"phase": "CHAOS", "reason": f"высокая волатильность ATR {atr_pct:.1f}%", "source": "indicators"}

    # RANGE: EMA20 почти горизонтальная И диапазон не слишком широкий
    if abs(ema20_slope_pct) < 1.5 and range_width_pct < 12.0:
        return {"phase": "RANGE", "reason": f"EMA20 плоская ({ema20_slope_pct:.2f}%), диапазон {range_width_pct:.1f}%", "source": "indicators"}

    # TREND по направлению EMA20 и положению цены относительно EMA50
    if ema20_slope_pct >= 1.5:
        return {"phase": "TREND_UP", "reason": f"EMA20 растёт ({ema20_slope_pct:.2f}%), цена {'выше' if price > ema50_now else 'ниже'} EMA50", "source": "indicators"}

    if ema20_slope_pct <= -1.5:
        return {"phase": "TREND_DOWN", "reason": f"EMA20 падает ({ema20_slope_pct:.2f}%), цена {'выше' if price > ema50_now else 'ниже'} EMA50", "source": "indicators"}

    # Промежуточная зона — считаем RANGE
    return {"phase": "RANGE", "reason": f"неопределённость, EMA20 slope={ema20_slope_pct:.2f}%", "source": "indicators"}


def _check_chaos_via_gpt(coin: str, df) -> bool:
    """
    Спрашивает GPT только одно: есть ли хаос/паника на рынке?
    Возвращает True если GPT видит CHAOS, False в остальных случаях.
    Если API недоступен — возвращает False (не блокируем торговлю).
    """
    if not OPENAI_API_KEY:
        return False

    last = df.iloc[-1]
    recent = df.tail(20)

    high = recent["high"].max()
    low = recent["low"].min()
    price = last["close"]
    range_width_pct = (high - low) / low * 100 if low > 0 else 0

    ema20_prev = df.iloc[-10]["ema_20"] if len(df) >= 10 else last["ema_20"]
    ema20_slope = (last["ema_20"] - ema20_prev) / ema20_prev * 100 if ema20_prev > 0 else 0

    prompt = f"""Крипто-монета {coin}, таймфрейм 1h.

Данные:
- Цена: {round(price, 6)}
- RSI: {round(last['rsi'], 1)}
- MACD гистограмма: {round(last['macd_diff'], 6)}
- Ширина диапазона за 20 свечей: {round(range_width_pct, 2)}%
- Наклон EMA20 за 10 свечей: {round(ema20_slope, 2)}%

Только один вопрос: есть ли сейчас ХАОС на рынке?
Хаос = паника, резкие скачки без направления, аномальная волатильность, реакция на новости.

Ответь ТОЛЬКО в формате JSON (без markdown):
{{"chaos": true}} или {{"chaos": false}}"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 20,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        result = json.loads(text)
        is_chaos = result.get("chaos", False)
        logger.info(f"market_phase GPT chaos-check {coin}: {is_chaos}")
        return bool(is_chaos)

    except Exception as e:
        logger.warning(f"market_phase: GPT chaos-check ошибка для {coin}: {e}")
        return False  # если GPT недоступен — не блокируем


def detect_phase(coin: str, df) -> dict:
    """
    Определяет фазу рынка для монеты.

    Возвращает:
    {
        "phase": "TREND_UP" | "TREND_DOWN" | "RANGE" | "CHAOS",
        "reason": "краткое объяснение"
    }
    """
    # Проверяем кэш (4 часа)
    cached = _phase_cache.get(coin)
    if cached and (time.time() - cached["ts"] < CACHE_TTL_SECONDS):
        logger.info(f"market_phase: {coin} — фаза из кэша: {cached['phase']} ({cached['reason']})")
        return {"phase": cached["phase"], "reason": cached["reason"]}

    # Шаг 1: определяем фазу по индикаторам
    indicator_result = _calc_phase_by_indicators(df)
    phase = indicator_result["phase"]
    reason = indicator_result["reason"]

    logger.info(f"market_phase: {coin} — индикаторы → {phase} ({reason})")

    # Шаг 2: если индикаторы не показали CHAOS — спрашиваем GPT
    # GPT проверяет только одно: нет ли скрытой паники/новостей
    if phase != "CHAOS":
        is_chaos = _check_chaos_via_gpt(coin, df)
        if is_chaos:
            phase = "CHAOS"
            reason = "GPT обнаружил хаос/панику на рынке"
            logger.info(f"market_phase: {coin} — GPT переопределил фазу на CHAOS")

    # Кэшируем на 4 часа
    _phase_cache[coin] = {"phase": phase, "reason": reason, "ts": time.time()}

    logger.info(f"market_phase: {coin} — итоговая фаза: {phase} ({reason})")
    return {"phase": phase, "reason": reason}
