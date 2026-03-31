"""
post_tracker.py
SQLite-backed tracker for seen posts and daily SMS counts.
Prevents duplicate alerts and enforces daily budget cap.
"""

import sqlite3
import os
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / ".tmp" / "posts.db"


def _get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen_posts (
                post_id     TEXT NOT NULL,
                platform    TEXT NOT NULL,
                group_name  TEXT,
                town        TEXT,
                url         TEXT,
                seen_at     TEXT NOT NULL,
                notified    INTEGER DEFAULT 0,
                score       INTEGER,
                work_type   TEXT,
                summary     TEXT,
                urgency     TEXT,
                PRIMARY KEY (post_id, platform)
            );

            CREATE TABLE IF NOT EXISTS sms_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at     TEXT NOT NULL,
                sent_date   TEXT NOT NULL,
                post_id     TEXT,
                platform    TEXT,
                phone       TEXT
            );
        """)
        # Migrate existing DBs — add columns if they don't exist yet
        for col, dtype in [("summary", "TEXT"), ("urgency", "TEXT"), ("created_at", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE seen_posts ADD COLUMN {col} {dtype}")
            except Exception:
                pass  # column already exists


def is_seen(post_id: str, platform: str) -> bool:
    """Return True if this post has already been processed."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_posts WHERE post_id=? AND platform=?",
            (post_id, platform)
        ).fetchone()
    return row is not None


def mark_seen(post_id: str, platform: str, group_name: str = None,
              town: str = None, url: str = None, notified: bool = False,
              score: int = None, work_type: str = None,
              summary: str = None, urgency: str = None,
              age_minutes: int = None):
    """Record a post as seen. age_minutes is used to back-calculate actual post creation time."""
    now_utc = datetime.now(timezone.utc)
    seen_at = now_utc.isoformat()

    # Calculate when the post was actually created for accurate peak hour tracking
    if age_minutes is not None:
        created_at = (now_utc - timedelta(minutes=age_minutes)).isoformat()
    else:
        created_at = seen_at  # fallback: assume created now

    with _get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO seen_posts
               (post_id, platform, group_name, town, url, seen_at, created_at, notified, score, work_type, summary, urgency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, platform, group_name, town, url,
             seen_at, created_at, int(notified), score, work_type, summary, urgency)
        )


def mark_notified(post_id: str, platform: str):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE seen_posts SET notified=1 WHERE post_id=? AND platform=?",
            (post_id, platform)
        )


def get_daily_sms_count() -> int:
    """Return how many SMS alerts have been sent today (local date)."""
    today = date.today().isoformat()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM sms_log WHERE sent_date=?", (today,)
        ).fetchone()
    return row["cnt"] if row else 0


def log_sms(post_id: str, platform: str, phone: str):
    """Record that an SMS was sent."""
    now = datetime.utcnow().isoformat()
    today = date.today().isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO sms_log (sent_at, sent_date, post_id, platform, phone) VALUES (?,?,?,?,?)",
            (now, today, post_id, platform, phone)
        )


def get_recent_similar_posts(work_type: str, town: str, hours: int = 48) -> list:
    """Return recently notified posts with same work_type or town for dedup checking."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM seen_posts
               WHERE notified=1 AND seen_at > ? AND (work_type=? OR town=?)
               ORDER BY seen_at DESC LIMIT 5""",
            (cutoff, work_type, town)
        ).fetchall()
    return [dict(row) for row in rows]


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
    print(f"SMS sent today: {get_daily_sms_count()}")
