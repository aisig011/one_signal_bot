"""
indicators.py
Расчёт технических индикаторов: RSI, EMA, MACD.

Используем библиотеку `ta` (technical analysis) — она считает
индикаторы по готовым формулам, нам не нужно писать их с нуля.
"""

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Добавляет в DataFrame колонки с индикаторами:
    rsi, ema_20, ema_50, ema_200, macd, macd_signal, macd_diff

    df должен содержать колонку 'close'.
    """
    df = df.copy()

    # RSI (Relative Strength Index) - показывает перекупленность/перепроданность
    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()

    # EMA (Exponential Moving Average) - сглаженные средние для определения тренда
    df["ema_20"] = EMAIndicator(close=df["close"], window=20).ema_indicator()
    df["ema_50"] = EMAIndicator(close=df["close"], window=50).ema_indicator()
    df["ema_200"] = EMAIndicator(close=df["close"], window=200).ema_indicator()

    # MACD - momentum индикатор, показывает смену тренда
    macd_calc = MACD(close=df["close"])
    df["macd"] = macd_calc.macd()
    df["macd_signal"] = macd_calc.macd_signal()
    df["macd_diff"] = macd_calc.macd_diff()  # гистограмма (macd - signal)

    return df


def get_trend(df: pd.DataFrame) -> str:
    """
    Определяет тренд по EMA на последней свече.
    Возвращает 'bullish' (восходящий), 'bearish' (нисходящий) или 'flat' (флэт/неясно).

    Смягчённая логика (v3): ориентируемся только на EMA20 vs EMA50 и
    положение цены относительно EMA50. EMA200 больше не используется
    как обязательное условие — на 4h она требует слишком много данных
    и часто "запаздывает", из-за чего реальный тренд (видимый на
    младших ТФ и по EMA20/50) ошибочно определялся как "флэт".
    """
    last = df.iloc[-1]

    if last["close"] > last["ema_50"] and last["ema_20"] > last["ema_50"]:
        return "bullish"
    elif last["close"] < last["ema_50"] and last["ema_20"] < last["ema_50"]:
        return "bearish"
    return "flat"


def get_rsi_state(rsi_value: float) -> str:
    """Текстовая интерпретация значения RSI."""
    if pd.isna(rsi_value):
        return "недостаточно данных"
    if rsi_value >= 70:
        return "перекуплен"
    elif rsi_value <= 30:
        return "перепродан"
    return "нейтрально"


def summarize(df: pd.DataFrame) -> dict:
    """
    Возвращает краткую сводку по последней свече:
    цена, RSI, тренд, MACD-сигнал.
    """
    df = add_indicators(df)
    last = df.iloc[-1]

    macd_signal = "neutral"
    if not pd.isna(last["macd_diff"]):
        if last["macd_diff"] > 0:
            macd_signal = "bullish"
        elif last["macd_diff"] < 0:
            macd_signal = "bearish"

    return {
        "close": last["close"],
        "rsi": last["rsi"],
        "rsi_state": get_rsi_state(last["rsi"]),
        "trend": get_trend(df),
        "macd_signal": macd_signal,
        "ema_20": last["ema_20"],
        "ema_50": last["ema_50"],
        "ema_200": last["ema_200"],
    }
