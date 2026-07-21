"""
storage.py
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            coin TEXT,
            symbol TEXT,
            direction TEXT,
            strategy TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit_1 REAL,
            quality_score REAL DEFAULT 0,
            quality_max REAL DEFAULT 0,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            outcome TEXT DEFAULT NULL,
            outcome_price REAL DEFAULT NULL,
            outcome_at TIMESTAMP DEFAULT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_signals (
            id SERIAL PRIMARY KEY,
            coin TEXT,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit_1 REAL,
            take_profit_2 REAL,
            outcome TEXT DEFAULT NULL,
            outcome_price REAL DEFAULT NULL,
            posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            outcome_at TIMESTAMP DEFAULT NULL
        )
    """)

    # Миграции для существующих таблиц
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE pending_signals SET message_id = %s WHERE id = %s", (message_id, signal_id))
    conn.commit()
    cursor.close()
    conn.close()


def add_active_trade(user_id, coin, symbol, direction, entry_price, stop_loss, take_profit_1, signal_message_id=None):
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM active_trades WHERE user_id = %s AND coin = %s LIMIT 1", (user_id, coin))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row is not None


def remove_active_trade(trade_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_trades WHERE id = %s", (trade_id,))
    conn.commit()
    cursor.close()
    conn.close()


def remove_active_trade_by_coin(user_id: int, coin: str) -> int:
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT coin, direction, entry_price, stop_loss, take_profit_1, opened_at
        FROM active_trades WHERE user_id = %s ORDER BY opened_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {"coin": r[0], "direction": r[1], "entry_price": r[2],
         "stop_loss": r[3], "take_profit_1": r[4], "opened_at": r[5]}
        for r in rows
    ]


def get_active_cooldowns(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT coin, direction,
               EXTRACT(EPOCH FROM (sent_at + INTERVAL '4 hours' - NOW())) / 60 AS minutes_left
        FROM sent_signals
        WHERE user_id = %s AND sent_at > NOW() - INTERVAL '4 hours'
        ORDER BY sent_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {"coin": r[0], "direction": r[1], "minutes_left": int(r[2]) if r[2] else 0}
        for r in rows
    ]


# ============================================================
#  Теневой лог сигналов — статистика вне зависимости от входа
# ============================================================

def log_signal(user_id: int, coin: str, symbol: str, direction: str,
               strategy: str, entry_price: float, stop_loss: float,
               take_profit_1: float, quality_score: float = 0,
               quality_max: float = 0) -> int:
    """
    Записывает каждый отправленный сигнал в лог.
    Вызывается в момент отправки — вне зависимости от того, войдёт ли
    пользователь в сделку. outcome изначально NULL, заполняется позже.
    Возвращает id записи.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO signal_log
        (user_id, coin, symbol, direction, strategy,
         entry_price, stop_loss, take_profit_1, quality_score, quality_max)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (user_id, coin, symbol, direction, strategy,
          float(entry_price), float(stop_loss), float(take_profit_1),
          float(quality_score), float(quality_max)))
    log_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()
    return log_id


def resolve_signal_log(log_id: int, outcome: str, outcome_price: float):
    """
    Записывает результат сигнала: 'TP' или 'SL'.
    Вызывается фоновой задачей — опять же вне зависимости от того,
    был ли пользователь в сделке.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE signal_log
        SET outcome = %s, outcome_price = %s, outcome_at = CURRENT_TIMESTAMP
        WHERE id = %s AND outcome IS NULL
    """, (outcome, float(outcome_price), log_id))
    conn.commit()
    cursor.close()
    conn.close()


def get_all_open_signal_logs():
    """
    Все записи в логе без результата — для фоновой проверки цены.
    Ограничиваем 7 днями: если за неделю не дошло ни до TP ни до SL,
    значит сигнал был нерелевантен (рынок ушёл далеко).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, coin, symbol, direction,
               entry_price, stop_loss, take_profit_1
        FROM signal_log
        WHERE outcome IS NULL
        AND sent_at > NOW() - INTERVAL '7 days'
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {
            "id": r[0], "user_id": r[1], "coin": r[2], "symbol": r[3],
            "direction": r[4], "entry_price": r[5],
            "stop_loss": r[6], "take_profit_1": r[7],
        }
        for r in rows
    ]


def get_signal_stats(user_id: int, days: int = 30) -> dict:
    """Агрегирует статистику для /stats."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT direction, strategy, outcome, COUNT(*) as cnt
        FROM signal_log
        WHERE user_id = %s
        AND sent_at > NOW() - (%s || ' days')::INTERVAL
        GROUP BY direction, strategy, outcome
    """, (user_id, str(days)))
    rows = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) FROM signal_log
        WHERE user_id = %s AND sent_at > NOW() - (%s || ' days')::INTERVAL
    """, (user_id, str(days)))
    total = cursor.fetchone()[0]

    cursor.close()
    conn.close()

    stats = {
        "total": total, "days": days,
        "tp": 0, "sl": 0, "open": 0,
        "by_strategy": {}, "by_direction": {},
    }

    for direction, strategy, outcome, cnt in rows:
        if outcome == "TP":   stats["tp"] += cnt
        elif outcome == "SL": stats["sl"] += cnt
        else:                 stats["open"] += cnt

        s = stats["by_strategy"].setdefault(strategy, {"tp": 0, "sl": 0, "open": 0})
        if outcome == "TP":   s["tp"] += cnt
        elif outcome == "SL": s["sl"] += cnt
        else:                 s["open"] += cnt

        d = stats["by_direction"].setdefault(direction, {"tp": 0, "sl": 0, "open": 0})
        if outcome == "TP":   d["tp"] += cnt
        elif outcome == "SL": d["sl"] += cnt
        else:                 d["open"] += cnt

    return stats


# ============================================================
#  Пользователи
# ============================================================

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
        {"user_id": row[0], "deposit": row[1], "risk_percent": row[2],
         "coins": row[3].split(",") if row[3] else []}
        for row in rows
    ]


def was_signal_sent_recently(user_id: int, coin: str, direction: str = None,
                              entry_price: float = 0, threshold_pct: float = 0.5) -> bool:
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
    """, (int(user_id), str(coin), str(direction), float(entry_price)))
    conn.commit()
    cursor.close()
    conn.close()


# ============================================================
#  Канал: публикация, дневной лимит, трекинг результата
# ============================================================

def count_channel_signals_today(tz_offset_hours: int = 3) -> int:
    """Сколько сигналов опубликовано в канал сегодня (по Киеву)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM channel_signals
        WHERE posted_at::date = (NOW() + (%s || ' hours')::INTERVAL)::date
    """, (str(tz_offset_hours),))
    n = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return n


def was_channel_signal_recent(coin: str, hours: int = 6) -> bool:
    """Был ли уже сигнал по монете в канале за N часов."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1 FROM channel_signals
        WHERE coin = %s AND posted_at > NOW() - (%s || ' hours')::INTERVAL
        LIMIT 1
    """, (coin, str(hours)))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row is not None


def log_channel_signal(coin, symbol, direction, entry_price,
                       stop_loss, take_profit_1, take_profit_2) -> int:
    """Записывает опубликованный в канал сигнал. outcome заполнится позже."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO channel_signals
        (coin, symbol, direction, entry_price, stop_loss, take_profit_1, take_profit_2)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (coin, symbol, direction, float(entry_price), float(stop_loss),
          float(take_profit_1), float(take_profit_2)))
    new_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()
    return new_id


def get_open_channel_signals():
    """Канальные сигналы без результата (для фоновой проверки цены)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, coin, symbol, direction, entry_price, stop_loss, take_profit_1
        FROM channel_signals
        WHERE outcome IS NULL
        AND posted_at > NOW() - INTERVAL '7 days'
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [
        {"id": r[0], "coin": r[1], "symbol": r[2], "direction": r[3],
         "entry_price": r[4], "stop_loss": r[5], "take_profit_1": r[6]}
        for r in rows
    ]


def resolve_channel_signal(signal_id: int, outcome: str, outcome_price: float):
    """Записывает результат канального сигнала: 'WIN' (Ціль 1) или 'LOSS' (Стоп)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE channel_signals
        SET outcome = %s, outcome_price = %s, outcome_at = CURRENT_TIMESTAMP
        WHERE id = %s AND outcome IS NULL
    """, (outcome, float(outcome_price), signal_id))
    conn.commit()
    cursor.close()
    conn.close()


def get_channel_stats(days: int = 30) -> dict:
    """Статистика канала для раздела в /stats."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT outcome, COUNT(*) FROM channel_signals
        WHERE posted_at > NOW() - (%s || ' days')::INTERVAL
        GROUP BY outcome
    """, (str(days),))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    stats = {"total": 0, "win": 0, "loss": 0, "open": 0, "days": days}
    for outcome, cnt in rows:
        stats["total"] += cnt
        if outcome == "WIN":    stats["win"] += cnt
        elif outcome == "LOSS": stats["loss"] += cnt
        else:                   stats["open"] += cnt
    return stats


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
        "user_id": row[0], "deposit": row[1], "risk_percent": row[2],
        "coins": row[3].split(",") if row[3] else [],
    }


def set_deposit(user_id: int, deposit: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, deposit) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET deposit = EXCLUDED.deposit
    """, (user_id, deposit))
    conn.commit()
    cursor.close()
    conn.close()


def set_risk(user_id: int, risk_percent: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, risk_percent) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET risk_percent = EXCLUDED.risk_percent
    """, (user_id, risk_percent))
    conn.commit()
    cursor.close()
    conn.close()


def set_coins(user_id: int, coins: list[str]):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, coins) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET coins = EXCLUDED.coins
    """, (user_id, ",".join(coins)))
    conn.commit()
    cursor.close()
    conn.close()
