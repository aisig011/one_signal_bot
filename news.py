"""
news.py
Получение крипто-новостей с cryptocurrency.cv (бесплатно, без ключа)
и анализ их влияния на рынок через Claude API.
"""

import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NEWS_BASE_URL = "https://cryptocurrency.cv/api/v1"

# Маппинг монет в поисковые запросы для новостей
COIN_KEYWORDS = {
    "BTC": "Bitcoin BTC",
    "ETH": "Ethereum ETH",
    "SOL": "Solana SOL",
    "BNB": "BNB Binance",
    "XRP": "XRP Ripple",
}


def get_news(coin: str, limit: int = 5) -> list[dict]:
    """
    Получает последние новости по монете с cryptocurrency.cv.
    Возвращает список статей (title, description, published_at).
    """
    keyword = COIN_KEYWORDS.get(coin.upper(), coin)

    try:
        response = requests.get(
            f"{NEWS_BASE_URL}/news",
            params={"q": keyword, "limit": limit},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        articles = data.get("data", data.get("articles", data.get("results", [])))

        result = []
        for item in articles[:limit]:
            result.append({
                "title": item.get("title", ""),
                "description": item.get("description", item.get("summary", "")),
                "published_at": item.get("published_at", item.get("publishedAt", "")),
            })
        return result

    except Exception as e:
        logger.warning(f"news: ошибка получения новостей для {coin}: {e}")
        return []


def analyze_news_with_claude(coin: str, articles: list[dict]) -> dict:
    """
    Отправляет новости в Claude API и получает оценку влияния на рынок.

    Возвращает словарь:
    {
        "sentiment": "positive" | "negative" | "neutral",
        "risk_level": "low" | "medium" | "high",
        "should_trade": True | False,
        "reason": "краткое объяснение"
    }
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("news: ANTHROPIC_API_KEY не задан, пропускаю анализ новостей")
        return _default_response()

    if not articles:
        return _default_response()

    # Формируем текст новостей для промпта
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
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        text = data["content"][0]["text"].strip()

        # Убираем markdown-обёртку если есть
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        result = json.loads(text)

        # Валидация полей
        result["sentiment"] = result.get("sentiment", "neutral")
        result["risk_level"] = result.get("risk_level", "low")
        result["should_trade"] = bool(result.get("should_trade", True))
        result["reason"] = result.get("reason", "")

        return result

    except json.JSONDecodeError as e:
        logger.warning(f"news: ошибка парсинга JSON от Claude: {e}")
        return _default_response()
    except Exception as e:
        logger.warning(f"news: ошибка запроса к Claude API: {e}")
        return _default_response()


def check_news_before_signal(coin: str) -> dict:
    """
    Главная функция — получает новости и анализирует их.
    Вызывается из signals.py перед формированием сигнала.

    Возвращает результат анализа. Если API недоступен или новостей нет —
    возвращает дефолт (should_trade=True, risk=low) чтобы не блокировать
    сигналы при технических проблемах.
    """
    logger.info(f"news: проверяю новостной фон для {coin}")

    articles = get_news(coin, limit=5)

    if not articles:
        logger.info(f"news: новостей не найдено для {coin}, продолжаю")
        return _default_response()

    result = analyze_news_with_claude(coin, articles)
    logger.info(f"news: {coin} — sentiment={result['sentiment']}, risk={result['risk_level']}, should_trade={result['should_trade']}, reason={result['reason']}")

    return result


def _default_response() -> dict:
    """Дефолтный ответ когда новостей нет или API недоступен — не блокируем сигнал."""
    return {
        "sentiment": "neutral",
        "risk_level": "low",
        "should_trade": True,
        "reason": "нет данных о новостях",
    }
