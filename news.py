"""
news.py
Получение крипто-новостей с CryptoCompare (один запрос за цикл, кэш)
и анализ их влияния на рынок через OpenAI API (ChatGPT).
"""

import os
import json
import time
import logging
import requests

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CRYPTOCOMPARE_API_KEY = os.environ.get("CRYPTOCOMPARE_API_KEY")
NEWS_URL = "https://min-api.cryptocompare.com/data/v2/news/"

# Ключевые слова для фильтрации новостей по монете (в заголовке/тексте)
COIN_FILTERS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth", "ether"],
    "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin", "binance"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
}

# Кэш новостей: храним общий список и время последнего запроса
_news_cache = {"articles": [], "fetched_at": 0}
CACHE_TTL_SECONDS = 25 * 60  # 25 минут (чуть меньше интервала сканирования)


def _fetch_all_news() -> list[dict]:
    """
    Делает ОДИН запрос к CryptoCompare за общим потоком новостей.
    Результат кэшируется на CACHE_TTL_SECONDS, чтобы не упираться
    в rate limit бесплатного ключа.
    """
    now = time.time()
    if _news_cache["articles"] and (now - _news_cache["fetched_at"] < CACHE_TTL_SECONDS):
        logger.info("news: использую кэш новостей")
        return _news_cache["articles"]

    try:
        headers = {}
        if CRYPTOCOMPARE_API_KEY:
            headers["authorization"] = f"Apikey {CRYPTOCOMPARE_API_KEY}"

        response = requests.get(NEWS_URL, params={"lang": "EN"}, headers=headers, timeout=10)
        logger.info(f"news: GET {response.url} -> status {response.status_code}")
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and data.get("Response") == "Error":
            logger.warning(f"news: CryptoCompare ошибка: {data.get('Message', '')}")
            # Если есть старый кэш — лучше вернуть его, чем ничего
            return _news_cache["articles"]

        raw = data.get("Data", []) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            logger.warning(f"news: неожиданный формат, тип Data: {type(raw).__name__}")
            return _news_cache["articles"]

        articles = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            articles.append({
                "title": item.get("title", "") or "",
                "body": (item.get("body", "") or "")[:300],
                "categories": (item.get("categories", "") or "").lower(),
                "tags": (item.get("tags", "") or "").lower(),
            })

        _news_cache["articles"] = articles
        _news_cache["fetched_at"] = now
        logger.info(f"news: загружено {len(articles)} новостей (общий поток)")
        return articles

    except Exception as e:
        logger.warning(f"news: ошибка запроса новостей: {e}")
        return _news_cache["articles"]


def get_news(coin: str, limit: int = 5) -> list[dict]:
    """
    Возвращает новости, относящиеся к конкретной монете, отфильтровав
    общий поток по ключевым словам.
    """
    all_articles = _fetch_all_news()
    if not all_articles:
        return []

    keywords = COIN_FILTERS.get(coin.upper(), [coin.lower()])

    matched = []
    for art in all_articles:
        haystack = f"{art['title']} {art['body']} {art['categories']} {art['tags']}".lower()
        if any(kw in haystack for kw in keywords):
            matched.append({
                "title": art["title"],
                "description": art["body"],
            })
        if len(matched) >= limit:
            break

    logger.info(f"news: для {coin} найдено {len(matched)} релевантных новостей")
    return matched


def analyze_news_with_openai(coin: str, articles: list[dict]) -> dict:
    if not OPENAI_API_KEY:
        logger.warning("news: OPENAI_API_KEY не задан, пропускаю анализ новостей")
        return _default_response()

    if not articles:
        return _default_response()

    news_text = "\n\n".join([
        f"- {a['title']}\n  {a['description'][:200] if a['description'] else ''}"
        for a in articles
    ])

    prompt = f"""Ты опытный крипто-трейдер. Проанализируй последние новости по {coin} и оцени, безопасно ли сейчас открывать торговую позицию.

Новости:
{news_text}

Ответь ТОЛЬКО в формате JSON (без markdown, без пояснений вне JSON):
{{
    "sentiment": "positive" или "negative" или "neutral",
    "risk_level": "low" или "medium" или "high",
    "should_trade": true или false,
    "reason": "одно предложение объясняющее решение"
}}

Правила:
- should_trade = false если: хак/взлом, регуляторный запрет, крупный скам, судебный иск, ожидается важное решение (ставки ФРС, SEC и т.д.)
- should_trade = false если risk_level = "high"
- should_trade = true если новости нейтральные или позитивные без явных рисков"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 200,
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
        result["sentiment"] = result.get("sentiment", "neutral")
        result["risk_level"] = result.get("risk_level", "low")
        result["should_trade"] = bool(result.get("should_trade", True))
        result["reason"] = result.get("reason", "")
        return result

    except json.JSONDecodeError as e:
        logger.warning(f"news: ошибка парсинга JSON от OpenAI: {e}")
        return _default_response()
    except Exception as e:
        logger.warning(f"news: ошибка запроса к OpenAI API: {e}")
        return _default_response()


def check_news_before_signal(coin: str) -> dict:
    logger.info(f"news: проверяю новостной фон для {coin}")
    articles = get_news(coin, limit=5)

    if not articles:
        logger.info(f"news: новостей не найдено для {coin}, продолжаю")
        return _default_response()

    result = analyze_news_with_openai(coin, articles)
    logger.info(
        f"news: {coin} — sentiment={result['sentiment']}, "
        f"risk={result['risk_level']}, should_trade={result['should_trade']}, "
        f"reason={result['reason']}"
    )
    return result


def _default_response() -> dict:
    return {
        "sentiment": "neutral",
        "risk_level": "low",
        "should_trade": True,
        "reason": "нет данных о новостях",
    }
