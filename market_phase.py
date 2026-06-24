"""
market_phase.py
Определение фазы рынка через OpenAI (ChatGPT).

Бот отправляет ИИ техническую картину по монете и получает фазу:
- TREND_UP   — устойчивый восходящий тренд
- TREND_DOWN — устойчивый нисходящий тренд
- RANGE      — боковик / флэт (цена в диапазоне)
- CHAOS      — высокая волатильность / неопределённость (не торговать)

Результат кэшируется на цикл, чтобы не делать лишних запросов к API.
"""

import os
import json
import time
import logging

import requests
import pandas as pd

logger = logging.getLogger("market_phase")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Кэш фаз по монетам: {coin: {"phase": ..., "reason": ..., "ts": ...}}
_phase_cache = {}
CACHE_TTL_SECONDS = 25 * 60  # держим фазу ~цикл сканирования


def _build_market_summary(df) -> dict:
    """
    Готовит краткую техническую сводку по последним свечам для ИИ.
    df — DataFrame с индикаторами (close, ema_20, ema_50, rsi, macd_diff).
    """
    last = df.iloc[-1]
    recent = df.tail(20)

    high = recent["high"].max()
    low = recent["low"].min()
    price = last["close"]

    # Ширина диапазона за 20 свечей в % — индикатор боковика/тренда
    range_width_pct = (high - low) / low * 100 if low > 0 else 0

    # Положение цены внутри диапазона (0 = у дна, 1 = у вершины)
    pos_in_range = (price - low) / (high - low) if (high - low) > 0 else 0.5

    # Наклон EMA20 за последние 10 свечей (тренд вверх/вниз/плоско)
    ema20_now = last["ema_20"]
    ema20_prev = df.iloc[-10]["ema_20"] if len(df) >= 10 else ema20_now
    ema20_slope_pct = (ema20_now - ema20_prev) / ema20_prev * 100 if ema20_prev > 0 else 0

    return {
        "price": round(price, 6),
        "ema20": round(last["ema_20"], 6),
        "ema50": round(last["ema_50"], 6),
        "rsi": round(last["rsi"], 1),
        "macd_diff": round(last["macd_diff"], 6),
        "range_width_pct_20": round(range_width_pct, 2),
        "position_in_range": round(pos_in_range, 2),
        "ema20_slope_pct_10": round(ema20_slope_pct, 2),
    }


def detect_phase(coin: str, df) -> dict:
    """
    Определяет фазу рынка для монеты через ИИ.

    Возвращает:
    {
        "phase": "TREND_UP" | "TREND_DOWN" | "RANGE" | "CHAOS",
        "reason": "краткое объяснение"
    }

    Если API недоступен — возвращает фазу "UNKNOWN" (вызывающий код
    решает, что делать; по умолчанию лучше не блокировать торговлю).
    """
    # Проверяем кэш
    cached = _phase_cache.get(coin)
    if cached and (time.time() - cached["ts"] < CACHE_TTL_SECONDS):
        logger.info(f"market_phase: {coin} — фаза из кэша: {cached['phase']}")
        return {"phase": cached["phase"], "reason": cached["reason"]}

    if not OPENAI_API_KEY:
        logger.warning("market_phase: OPENAI_API_KEY не задан")
        return {"phase": "UNKNOWN", "reason": "нет ключа API"}

    summary = _build_market_summary(df)

    prompt = f"""Ты опытный крипто-трейдер. Определи текущую фазу рынка для {coin} по техническим данным.

Данные (таймфрейм 1h, последние свечи):
- Цена: {summary['price']}
- EMA20: {summary['ema20']}
- EMA50: {summary['ema50']}
- RSI: {summary['rsi']}
- MACD гистограмма: {summary['macd_diff']}
- Ширина диапазона за 20 свечей: {summary['range_width_pct_20']}%
- Положение цены в диапазоне: {summary['position_in_range']} (0=дно, 1=вершина)
- Наклон EMA20 за 10 свечей: {summary['ema20_slope_pct_10']}%

Ответь ТОЛЬКО в формате JSON (без markdown):
{{
    "phase": "TREND_UP" или "TREND_DOWN" или "RANGE" или "CHAOS",
    "reason": "одно короткое предложение"
}}

Критерии:
- TREND_UP: цена и EMA устойчиво растут (наклон EMA20 заметно положительный, цена выше EMA), движение направленное
- TREND_DOWN: цена и EMA устойчиво падают (наклон EMA20 заметно отрицательный, цена ниже EMA)
- RANGE: узкий диапазон, наклон EMA20 близок к нулю, цена колеблется туда-сюда (боковик/флэт)
- CHAOS: очень широкий диапазон при неясном направлении, резкие скачки (опасно торговать)"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 120,
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
        phase = result.get("phase", "UNKNOWN")
        reason = result.get("reason", "")

        # Валидация значения фазы
        if phase not in ("TREND_UP", "TREND_DOWN", "RANGE", "CHAOS"):
            phase = "UNKNOWN"

        # Кэшируем
        _phase_cache[coin] = {"phase": phase, "reason": reason, "ts": time.time()}

        logger.info(f"market_phase: {coin} — фаза {phase} ({reason})")
        return {"phase": phase, "reason": reason}

    except json.JSONDecodeError as e:
        logger.warning(f"market_phase: ошибка парсинга JSON для {coin}: {e}")
        return {"phase": "UNKNOWN", "reason": "ошибка ответа ИИ"}
    except Exception as e:
        logger.warning(f"market_phase: ошибка запроса для {coin}: {e}")
        return {"phase": "UNKNOWN", "reason": "ошибка API"}
