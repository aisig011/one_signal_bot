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
            message_id BIGINT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
            signal_message_id BIGINT DEFAULT NULL,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Миграции для существующих таблиц (добавляют колонки если их нет)
    cursor.execute("""
        ALTER TABLE active_trades
        ADD COLUMN IF NOT EXISTS signal_message_id BIGINT DEFAULT NULL
    """)
    cursor.execute("""
        ALTER TABLE pending_signals
        ADD COLUMN IF NOT EXISTS message_id BIGINT DEFAULT NULL
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
    """, (user_id, coin, symbol, direction, float(entry_price), float(stop_loss), float(take_profit_1)))
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
        SELECT id, user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1, message_id
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
        "take_profit_1": r[7], "message_id": r[8],
    }


def update_pending_signal_message_id(signal_id: int, message_id: int):
    """Сохраняет message_id отправленного сигнала для последующего reply."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE pending_signals SET message_id = %s WHERE id = %s
    """, (message_id, signal_id))
    conn.commit()
    cursor.close()
    conn.close()


def add_active_trade(user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1, signal_message_id=None):
    """Сохраняет сделку для отслеживания после нажатия кнопки 'Вошёл'."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO active_trades
        (user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1, signal_message_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1, signal_message_id))
    conn.commit()
    cursor.close()
    conn.close()


def get_all_active_trades():
    """Возвращает все активные сделки для отслеживания."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1, signal_message_id
        FROM active_trades
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {
            "id": r[0], "user_id": r[1], "coin": r[2], "symbol": r[3],
            "direction": r[4], "entry_price": r[5], "stop_loss": r[6],
            "take_profit_1": r[7], "signal_message_id": r[8],
        }
        for r in rows
    ]


def has_active_trade(user_id: int, coin: str) -> bool:
    """Проверяет, есть ли уже активная (отслеживаемая) сделка по монете."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1 FROM active_trades
        WHERE user_id = %s AND coin = %s
        LIMIT 1
    """, (user_id, coin))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row is not None


def remove_active_trade(trade_id):
    """Удаляет сделку из отслеживания (после TP/SL)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_trades WHERE id = %s", (trade_id,))
    conn.commit()
    cursor.close()
    conn.close()


def remove_active_trade_by_coin(user_id: int, coin: str) -> int:
    """
    Убирает активную сделку по монете вручную (команда /close).

    Нужна, когда сделка попала в отслеживание по ошибке (случайно нажата
    кнопка «Вошёл в сделку») или закрыта руками на бирже. Такая сделка
    иначе висит вечно: занимает слот и однажды пришлёт TP/SL по сделке,
    которой не было.

    Возвращает количество удалённых строк (0 = такой сделки не было).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM active_trades WHERE user_id = %s AND coin = %s",
        (user_id, coin.strip().upper()),
    )
    deleted = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    return deleted


def get_active_trades_for_user(user_id: int):
    """Активные (отслеживаемые) сделки конкретного пользователя — для /debug."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT coin, direction, entry_price, stop_loss, take_profit_1, opened_at
        FROM active_trades
        WHERE user_id = %s
        ORDER BY opened_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {
            "coin": r[0], "direction": r[1], "entry_price": r[2],
            "stop_loss": r[3], "take_profit_1": r[4], "opened_at": r[5],
        }
        for r in rows
    ]


def get_active_cooldowns(user_id: int):
    """Монеты в кулдауне (сигнал был за последние 4 часа) — для /debug."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT coin, direction,
               EXTRACT(EPOCH FROM (sent_at + INTERVAL '4 hours' - NOW())) / 60 AS minutes_left
        FROM sent_signals
        WHERE user_id = %s
        AND sent_at > NOW() - INTERVAL '4 hours'
        ORDER BY sent_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {"coin": r[0], "direction": r[1], "minutes_left": int(r[2]) if r[2] else 0}
        for r in rows
    ]


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
    Направление НЕ учитывается: если по монете уже был сигнал (LONG или
    SHORT) — не дублируем, пока не пройдёт окно.
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
    # float() обязателен: entry_price может прийти как numpy.float64,
    # который psycopg2 не понимает (ошибка schema "np" does not exist).
    cursor.execute("""
        INSERT INTO sent_signals (user_id, coin, direction, entry_price, sent_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id, coin, direction) DO UPDATE SET
            entry_price = EXCLUDED.entry_price, sent_at = CURRENT_TIMESTAMP
    """, (int(user_id), str(coin), str(direction), float(entry_price)))
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
