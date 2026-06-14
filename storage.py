"""
storage.py
Хранение настроек пользователя: депозит, риск (%), список монет.
Используем SQLite — простой файл базы данных, без отдельного сервера.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot_data.db"


def init_db():
    """Создаёт таблицу пользователей, если её ещё нет."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            deposit REAL,
            risk_percent REAL,
            coins TEXT DEFAULT 'BTC,ETH'
        )
    """)
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
