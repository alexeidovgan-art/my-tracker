import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).with_name("tracker.db")


def now():
    return datetime.now(timezone.utc).isoformat()


def connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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
                owner_id INTEGER,
                price REAL,
                floor REAL,
                model TEXT,
                backdrop TEXT,
                pattern TEXT,
                category TEXT,
                published_at TEXT,
                claimed_by INTEGER,
                claimed_at TEXT,
                group_chat_id INTEGER,
                group_message_id INTEGER
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(gifts)")
        }

        migrations = {
            "id": "INTEGER",
            "owner_id": "INTEGER",
            "claimed_by": "INTEGER",
            "claimed_at": "TEXT",
            "group_chat_id": "INTEGER",
            "group_message_id": "INTEGER",
        }

        for name, sql_type in migrations.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE gifts ADD COLUMN {name} {sql_type}"
                )

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gifts_id
            ON gifts(id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_gifts (
                gift_key TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Старые опубликованные подарки тоже считаем уже встреченными.
        conn.execute("""
            INSERT OR IGNORE INTO seen_gifts (gift_key, first_seen)
            SELECT gift_key, COALESCE(published_at, ?)
            FROM gifts
        """, (now(),))

        conn.commit()


def baseline_done():
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'baseline_done'"
        ).fetchone()

        return bool(row and row["value"] == "1")


def set_baseline_done():
    with connection() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO settings (key, value)
            VALUES ('baseline_done', '1')
        """)
        conn.commit()


def is_seen(gift_key):
    with connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_gifts WHERE gift_key = ?",
            (str(gift_key),)
        ).fetchone()

        return row is not None


def mark_seen(gift_key):
    with connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO seen_gifts (gift_key, first_seen)
            VALUES (?, ?)
        """, (str(gift_key), now()))
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
                gift_key, gift_url, title, owner, owner_url, owner_id,
                price, floor, model, backdrop, pattern, category,
                published_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(data["gift_key"]),
            data.get("gift_url"),
            data.get("title"),
            data.get("owner"),
            data.get("owner_url"),
            data.get("owner_id"),
            data.get("price"),
            data.get("floor"),
            data.get("model"),
            data.get("backdrop"),
            data.get("pattern"),
            data.get("category"),
            now()
        ))

        row = conn.execute(
            "SELECT id FROM gifts WHERE gift_key = ?",
            (str(data["gift_key"]),)
        ).fetchone()

        conn.commit()

        return row["id"] if row else None


def delete_unclaimed_gift(gift_id):
    with connection() as conn:
        conn.execute("""
            DELETE FROM gifts
            WHERE id = ? AND claimed_by IS NULL
        """, (int(gift_id),))
        conn.commit()


def set_group_message(gift_id, chat_id, message_id):
    with connection() as conn:
        conn.execute("""
            UPDATE gifts
            SET group_chat_id = ?, group_message_id = ?
            WHERE id = ?
        """, (int(chat_id), int(message_id), int(gift_id)))
        conn.commit()


def claim_gift(gift_id, user_id):
    """
    Атомарное занятие лога.
    Из двух одновременных нажатий победит только одно.
    """

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT *
            FROM gifts
            WHERE id = ? AND claimed_by IS NULL
        """, (int(gift_id),)).fetchone()

        if row is None:
            conn.commit()
            return None

        conn.execute("""
            UPDATE gifts
            SET claimed_by = ?, claimed_at = ?
            WHERE id = ? AND claimed_by IS NULL
        """, (int(user_id), now(), int(gift_id)))

        updated = conn.execute(
            "SELECT changes() AS changed"
        ).fetchone()["changed"]

        if updated != 1:
            conn.commit()
            return None

        result = dict(row)
        result["claimed_by"] = int(user_id)

        conn.commit()
        return result


def release_claim(gift_id, user_id):
    """
    Освобождает лог, если Telegram не смог отправить личное сообщение.
    """

    with connection() as conn:
        conn.execute("""
            UPDATE gifts
            SET claimed_by = NULL, claimed_at = NULL
            WHERE id = ? AND claimed_by = ?
        """, (int(gift_id), int(user_id)))
        conn.commit()
