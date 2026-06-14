"""
storage.py
Хранение настроек пользователя: депозит, риск (%), список монет.
Используем SQLite — простой файл базы данных, без отдельного сервера.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot_data.db"


def init_db():
    """Создаёт таблицы, если их ещё нет."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            deposit REAL,
            risk_percent REAL,
            coins TEXT DEFAULT 'BTC,ETH,SOL,BNB,XRP'
        )
    """)
    # Таблица для отслеживания, какой сигнал последний раз отправляли
    # пользователю по каждой монете+направлению — чтобы не дублировать.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_signals (
            user_id INTEGER,
            coin TEXT,
            direction TEXT,
            entry_price REAL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, coin, direction)
        )
    """)
    conn.commit()
    conn.close()


def get_all_configured_users():
    """Возвращает список всех пользователей, у которых заданы депозит и риск."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, deposit, risk_percent, coins FROM users
        WHERE deposit IS NOT NULL AND risk_percent IS NOT NULL
    """)
    rows = cursor.fetchall()
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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT entry_price, sent_at FROM sent_signals
        WHERE user_id = ? AND coin = ? AND direction = ?
        AND sent_at > datetime('now', '-6 hours')
    """, (user_id, coin, direction))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return False

    old_price = row[0]
    price_diff_pct = abs(entry_price - old_price) / old_price * 100
    return price_diff_pct < threshold_pct


def mark_signal_sent(user_id: int, coin: str, direction: str, entry_price: float):
    """Записывает, что сигнал отправлен (для защиты от дублей)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sent_signals (user_id, coin, direction, entry_price, sent_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, coin, direction) DO UPDATE SET
            entry_price = ?, sent_at = CURRENT_TIMESTAMP
    """, (user_id, coin, direction, entry_price, entry_price))
    conn.commit()
    conn.close()


def get_user(user_id: int):
    """Возвращает настройки пользователя или None, если его ещё нет."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, deposit, risk_percent, coins FROM users WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, deposit)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET deposit = ?
    """, (user_id, deposit, deposit))
    conn.commit()
    conn.close()


def set_risk(user_id: int, risk_percent: float):
    """Сохраняет (или обновляет) % риска пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, risk_percent)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET risk_percent = ?
    """, (user_id, risk_percent, risk_percent))
    conn.commit()
    conn.close()


def set_coins(user_id: int, coins: list[str]):
    """Сохраняет список монет пользователя (например ['BTC', 'ETH', 'SOL'])."""
    coins_str = ",".join(coins)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, coins)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET coins = ?
    """, (user_id, coins_str, coins_str))
    conn.commit()
    conn.close()
