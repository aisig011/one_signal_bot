"""
storage.py
Хранение настроек пользователя: депозит, риск (%), список монет.
Используем PostgreSQL (Railway предоставляет переменную DATABASE_URL).
"""

import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("Переменная окружения DATABASE_URL не задана!")
    return psycopg2.connect(DATABASE_URL)


def init_db():
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
    # Отправленные сигналы — чтобы кнопка "Вошёл" могла по короткому id
    # достать все параметры сделки (callback_data ограничен 64 байтами).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_signals (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            coin TEXT,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit_1 REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Активные сделки, которые пользователь подтвердил кнопкой "Вошёл".
    # Бот отслеживает их и присылает уведомление при достижении TP или SL.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_trades (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            coin TEXT,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit_1 REAL,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def save_pending_signal(user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1) -> int:
    """Сохраняет отправленный сигнал, возвращает его id для кнопки 'Вошёл'."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_signals
        (user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1))
    new_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()
    return new_id


def get_pending_signal(signal_id: int):
    """Достаёт сохранённый сигнал по id (для кнопки 'Вошёл')."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1
        FROM pending_signals WHERE id = %s
    """, (signal_id,))
    r = cursor.fetchone()
    cursor.close()
    conn.close()
    if r is None:
        return None
    return {
        "id": r[0], "user_id": r[1], "coin": r[2], "symbol": r[3],
        "direction": r[4], "entry_price": r[5], "stop_loss": r[6],
        "take_profit_1": r[7],
    }


def add_active_trade(user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1):
    """Сохраняет сделку для отслеживания после нажатия кнопки 'Вошёл'."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO active_trades
        (user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1))
    conn.commit()
    cursor.close()
    conn.close()


def get_all_active_trades():
    """Возвращает все активные сделки для отслеживания."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1
        FROM active_trades
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {
            "id": r[0], "user_id": r[1], "coin": r[2], "symbol": r[3],
            "direction": r[4], "entry_price": r[5], "stop_loss": r[6],
            "take_profit_1": r[7],
        }
        for r in rows
    ]


def remove_active_trade(trade_id):
    """Удаляет сделку из отслеживания (после TP/SL)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_trades WHERE id = %s", (trade_id,))
    conn.commit()
    cursor.close()
    conn.close()


def get_all_configured_users():
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


def was_signal_sent_recently(user_id: int, coin: str, direction: str = None, entry_price: float = 0, threshold_pct: float = 0.5) -> bool:
    """
    Проверяет, отправлялся ли сигнал по монете за последние 4 часа.
    Направление (direction) НЕ учитывается намеренно: если по SOL уже был
    сигнал (LONG или SHORT) — не дублируем по этой монете, пока не пройдёт
    окно. Это убирает повторные сигналы по одной монете.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sent_at FROM sent_signals
        WHERE user_id = %s AND coin = %s
        AND sent_at > NOW() - INTERVAL '4 hours'
    """, (user_id, coin))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row is not None


def mark_signal_sent(user_id: int, coin: str, direction: str, entry_price: float):
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
