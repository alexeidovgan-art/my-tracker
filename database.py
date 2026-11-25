import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).with_name("tracker.db")


def connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gifts (
                gift_key TEXT PRIMARY KEY,
                gift_url TEXT,
                title TEXT,
                owner TEXT,
                owner_url TEXT,
                price REAL,
                floor REAL,
                model TEXT,
                backdrop TEXT,
                pattern TEXT,
                category TEXT,
                published_at TEXT
            )
        """)
        conn.commit()


def gift_exists(gift_key):
    with connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM gifts WHERE gift_key = ?",
            (str(gift_key),)
        ).fetchone()
        return row is not None


def save_gift(data):
    with connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO gifts (
                gift_key, gift_url, title, owner, owner_url,
                price, floor, model, backdrop, pattern,
                category, published_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(data["gift_key"]),
            data.get("gift_url"),
            data.get("title"),
            data.get("owner"),
            data.get("owner_url"),
            data.get("price"),
            data.get("floor"),
            data.get("model"),
            data.get("backdrop"),
            data.get("pattern"),
            data.get("category"),
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
