"""
market.py
Получение рыночных данных (свечи OHLCV) с Binance Futures.

Используем публичный REST API Binance Futures — он не требует
API-ключа для получения данных по ценам и свечам (только для
реальной торговли нужны ключи, но мы их пока не используем).
"""

import requests
import pandas as pd

BASE_URL = "https://fapi.binance.com"

# Соответствие коротких имён монет тикерам Binance Futures
SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}


def get_symbol(coin: str) -> str:
    """Преобразует короткое имя ('BTC') в тикер Binance ('BTCUSDT')."""
    coin = coin.upper()
    return SYMBOL_MAP.get(coin, f"{coin}USDT")


def get_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    """
    Загружает свечи (klines) с Binance Futures.

    symbol: например "BTCUSDT"
    interval: "15m", "1h", "4h", "1d" и т.д.
    limit: сколько последних свечей загрузить (макс. 1500)

    Возвращает DataFrame с колонками:
    open_time, open, high, low, close, volume, close_time
    """
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])

    # Преобразуем нужные колонки в числа
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")

    return df[["open_time", "open", "high", "low", "close", "volume", "close_time"]]


def get_current_price(symbol: str) -> float:
    """Возвращает текущую цену по символу (например 'BTCUSDT')."""
    url = f"{BASE_URL}/fapi/v1/ticker/price"
    params = {"symbol": symbol}

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    return float(data["price"])
