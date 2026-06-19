"""
storage.py
Хранение настроек пользователя: депозит, риск (%), список монет.

Используем PostgreSQL (Railway предоставляет переменную DATABASE_URL
при подключении PostgreSQL-сервиса). Это даёт постоянное хранилище,
не зависящее от Volumes/деплоев.
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_connection():
    """Создаёт новое соединение с PostgreSQL."""
    if not DATABASE_URL:
        raise RuntimeError(
            "Переменная окружения DATABASE_URL не задана! "
            "Подключи PostgreSQL к проекту на Railway и убедись, "
            "что переменная доступна сервису бота."
        )
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Создаёт таблицы, если их ещё нет."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            deposit REAL,
            risk_percent REAL,
            coins TEXT DEFAULT 'BTC,ETH,SOL,BNB,XRP'
        )
    """)

    # Таблица для отслеживания, какой сигнал последний раз отправляли
    # пользователю по каждой монете+направлению — чтобы не дублировать.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_signals (
            user_id BIGINT,
            coin TEXT,
            direction TEXT,
            entry_price REAL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, coin, direction)
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_all_configured_users():
    """Возвращает список всех пользователей, у которых заданы депозит и риск."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, deposit, risk_percent, coins FROM users
        WHERE deposit IS NOT NULL AND risk_percent IS NOT NULL
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return [
        {
            "user_id": row[0],
            "deposit": row[1],
            "risk_percent": row[2],
            "coins": row[3].split(",") if row[3] else [],
        }
        for row in rows
    ]


def was_signal_sent_recently(user_id: int, coin: str, direction: str, entry_price: float = 0, threshold_pct: float = 0.5) -> bool:
    """
    Проверяет, отправляли ли уже сигнал по этой монете+направлению
    за последние 4 часа. Жёсткий кулдаун — без привязки к цене,
    чтобы исключить спам одинаковыми сигналами каждые 30 минут.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sent_at FROM sent_signals
        WHERE user_id = %s AND coin = %s AND direction = %s
        AND sent_at > NOW() - INTERVAL '4 hours'
    """, (user_id, coin, direction))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    return row is not None


def mark_signal_sent(user_id: int, coin: str, direction: str, entry_price: float):
    """Записывает, что сигнал отправлен (для защиты от дублей)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sent_signals (user_id, coin, direction, entry_price, sent_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id, coin, direction) DO UPDATE SET
            entry_price = EXCLUDED.entry_price, sent_at = CURRENT_TIMESTAMP
    """, (user_id, coin, direction, entry_price))
    conn.commit()
    cursor.close()
    conn.close()


def get_user(user_id: int):
    """Возвращает настройки пользователя или None, если его ещё нет."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, deposit, risk_percent, coins FROM users WHERE user_id = %s",
        (user_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if row is None:
        return None

    return {
        "user_id": row[0],
        "deposit": row[1],
        "risk_percent": row[2],
        "coins": row[3].split(",") if row[3] else [],
    }


def set_deposit(user_id: int, deposit: float):
    """Сохраняет (или обновляет) депозит пользователя."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, deposit)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET deposit = EXCLUDED.deposit
    """, (user_id, deposit))
    conn.commit()
    cursor.close()
    conn.close()


def set_risk(user_id: int, risk_percent: float):
    """Сохраняет (или обновляет) % риска пользователя."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, risk_percent)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET risk_percent = EXCLUDED.risk_percent
    """, (user_id, risk_percent))
    conn.commit()
    cursor.close()
    conn.close()


def set_coins(user_id: int, coins: list[str]):
    """Сохраняет список монет пользователя (например ['BTC', 'ETH', 'SOL'])."""
    coins_str = ",".join(coins)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, coins)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET coins = EXCLUDED.coins
    """, (user_id, coins_str))
    conn.commit()
    cursor.close()
    conn.close()            risk_percent REAL,
            coins TEXT DEFAULT 'BTC,ETH,SOL,BNB,XRP'
        )
    """)

    # Таблица для отслеживания, какой сигнал последний раз отправляли
    # пользователю по каждой монете+направлению — чтобы не дублировать.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_signals (
            user_id BIGINT,
            coin TEXT,
            direction TEXT,
            entry_price REAL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, coin, direction)
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_all_configured_users():
    """Возвращает список всех пользователей, у которых заданы депозит и риск."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, deposit, risk_percent, coins FROM users
        WHERE deposit IS NOT NULL AND risk_percent IS NOT NULL
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return [
        {
            "user_id": row[0],
            "deposit": row[1],
            "risk_percent": row[2],
            "coins": row[3].split(",") if row[3] else [],
        }
        for row in rows
    ]


def was_signal_sent_recently(user_id: int, coin: str, direction: str, entry_price: float = 0, threshold_pct: float = 0.5) -> bool:
    """
    Проверяет, отправляли ли уже сигнал по этой монете+направлению
    за последние 4 часа. Жёсткий кулдаун — без привязки к цене,
    чтобы исключить спам одинаковыми сигналами каждые 30 минут.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sent_at FROM sent_signals
        WHERE user_id = %s AND coin = %s AND direction = %s
        AND sent_at > NOW() - INTERVAL '4 hours'
    """, (user_id, coin, direction))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    return row is not None


def mark_signal_sent(user_id: int, coin: str, direction: str, entry_price: float):
    """Записывает, что сигнал отправлен (для защиты от дублей)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sent_signals (user_id, coin, direction, entry_price, sent_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id, coin, direction) DO UPDATE SET
            entry_price = EXCLUDED.entry_price, sent_at = CURRENT_TIMESTAMP
    """, (user_id, coin, direction, entry_price))
    conn.commit()
    cursor.close()
    conn.close()


def get_user(user_id: int):
    """Возвращает настройки пользователя или None, если его ещё нет."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, deposit, risk_percent, coins FROM users WHERE user_id = %s",
        (user_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if row is None:
        return None

    return {
        "user_id": row[0],
        "deposit": row[1],
        "risk_percent": row[2],
        "coins": row[3].split(",") if row[3] else [],
    }


def set_deposit(user_id: int, deposit: float):
    """Сохраняет (или обновляет) депозит пользователя."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, deposit)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET deposit = EXCLUDED.deposit
    """, (user_id, deposit))
    conn.commit()
    cursor.close()
    conn.close()


def set_risk(user_id: int, risk_percent: float):
    """Сохраняет (или обновляет) % риска пользователя."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, risk_percent)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET risk_percent = EXCLUDED.risk_percent
    """, (user_id, risk_percent))
    conn.commit()
    cursor.close()
    conn.close()


def set_coins(user_id: int, coins: list[str]):
    """Сохраняет список монет пользователя (например ['BTC', 'ETH', 'SOL'])."""
    coins_str = ",".join(coins)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, coins)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET coins = EXCLUDED.coins
    """, (user_id, coins_str))
    conn.commit()
    cursor.close()
    conn.close()
            risk_percent REAL,
            coins TEXT DEFAULT 'BTC,ETH,SOL,BNB,XRP'
        )
    """)

    # Таблица для отслеживания, какой сигнал последний раз отправляли
    # пользователю по каждой монете+направлению — чтобы не дублировать.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_signals (
            user_id BIGINT,
            coin TEXT,
            direction TEXT,
            entry_price REAL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, coin, direction)
        )
    """)

    conn.commit()
    cursor.close()
    conn.close()


def get_all_configured_users():
    """Возвращает список всех пользователей, у которых заданы депозит и риск."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, deposit, risk_percent, coins FROM users
        WHERE deposit IS NOT NULL AND risk_percent IS NOT NULL
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return [
        {
            "user_id": row[0],
            "deposit": row[1],
            "risk_percent": row[2],
            "coins": row[3].split(",") if row[3] else [],
        }
        for row in rows
    ]


def was_signal_sent_recently(user_id: int, coin: str, direction: str, entry_price: float, threshold_pct: float = 0.5) -> bool:
    """
    Проверяет, отправляли ли уже похожий сигнал (та же монета+направление,
    цена входа отличается менее чем на threshold_pct%) за последние 6 часов.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT entry_price FROM sent_signals
        WHERE user_id = %s AND coin = %s AND direction = %s
        AND sent_at > NOW() - INTERVAL '6 hours'
    """, (user_id, coin, direction))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if row is None:
        return False

    old_price = row[0]
    price_diff_pct = abs(entry_price - old_price) / old_price * 100
    return price_diff_pct < threshold_pct


def mark_signal_sent(user_id: int, coin: str, direction: str, entry_price: float):
    """Записывает, что сигнал отправлен (для защиты от дублей)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sent_signals (user_id, coin, direction, entry_price, sent_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id, coin, direction) DO UPDATE SET
            entry_price = EXCLUDED.entry_price, sent_at = CURRENT_TIMESTAMP
    """, (user_id, coin, direction, entry_price))
    conn.commit()
    cursor.close()
    conn.close()


def get_user(user_id: int):
    """Возвращает настройки пользователя или None, если его ещё нет."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, deposit, risk_percent, coins FROM users WHERE user_id = %s",
        (user_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if row is None:
        return None

    return {
        "user_id": row[0],
        "deposit": row[1],
        "risk_percent": row[2],
        "coins": row[3].split(",") if row[3] else [],
    }


def set_deposit(user_id: int, deposit: float):
    """Сохраняет (или обновляет) депозит пользователя."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, deposit)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET deposit = EXCLUDED.deposit
    """, (user_id, deposit))
    conn.commit()
    cursor.close()
    conn.close()


def set_risk(user_id: int, risk_percent: float):
    """Сохраняет (или обновляет) % риска пользователя."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, risk_percent)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET risk_percent = EXCLUDED.risk_percent
    """, (user_id, risk_percent))
    conn.commit()
    cursor.close()
    conn.close()


def set_coins(user_id: int, coins: list[str]):
    """Сохраняет список монет пользователя (например ['BTC', 'ETH', 'SOL'])."""
    coins_str = ",".join(coins)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, coins)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET coins = EXCLUDED.coins
    """, (user_id, coins_str))
    conn.commit()
    cursor.close()
    conn.close()
