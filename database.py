
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).with_name("tracker.db")


@contextmanager
def connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS gifts (
                gift_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                gift_url TEXT,
                owner_id TEXT,
                owner_username TEXT,
                owner_url TEXT,
                price TEXT,
                floor REAL,
                model TEXT,
                backdrop TEXT,
                pattern TEXT,
                topic_id INTEGER,
                message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_gifts_status
            ON gifts(status)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_gifts_created
            ON gifts(created_at)
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def gift_exists(gift_id):
    with connection() as db:
        row = db.execute(
            "SELECT 1 FROM gifts WHERE gift_id = ?",
            (str(gift_id),)
        ).fetchone()

        return row is not None


def save_gift(gift):
    with connection() as db:
        db.execute("""
            INSERT INTO gifts (
                gift_id, title, gift_url,
                owner_id, owner_username, owner_url,
                price, floor, model, backdrop, pattern,
                topic_id, message_id, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gift_id) DO UPDATE SET
                title = excluded.title,
                gift_url = COALESCE(excluded.gift_url, gifts.gift_url),
                owner_id = COALESCE(excluded.owner_id, gifts.owner_id),
                owner_username = COALESCE(
                    excluded.owner_username, gifts.owner_username
                ),
                owner_url = COALESCE(excluded.owner_url, gifts.owner_url),
                price = COALESCE(excluded.price, gifts.price),
                floor = COALESCE(excluded.floor, gifts.floor),
                model = COALESCE(excluded.model, gifts.model),
                backdrop = COALESCE(excluded.backdrop, gifts.backdrop),
                pattern = COALESCE(excluded.pattern, gifts.pattern),
                updated_at = CURRENT_TIMESTAMP
        """, (
            str(gift["gift_id"]),
            gift.get("title") or "Подарок Telegram",
            gift.get("gift_url"),
            str(gift["owner_id"]) if gift.get("owner_id") else None,
            gift.get("owner_username"),
            gift.get("owner_url"),
            gift.get("price"),
            gift.get("floor"),
            gift.get("model"),
            gift.get("backdrop"),
            gift.get("pattern"),
            gift.get("topic_id"),
            gift.get("message_id"),
            gift.get("status", "new")
        ))


def mark_published(gift_id, topic_id, message_id):
    with connection() as db:
        db.execute("""
            UPDATE gifts
            SET status = 'published',
                topic_id = ?,
                message_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE gift_id = ?
        """, (topic_id, message_id, str(gift_id)))


def get_setting(key, default=None):
    with connection() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ).fetchone()

        return row["value"] if row else default


def set_setting(key, value):
    with connection() as db:
        db.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
