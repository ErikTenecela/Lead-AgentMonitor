"""
analytics.py
Post volume analytics — peak hour detection and weekly group reports.

Used by orchestrator to:
  1. Pick an adaptive poll interval based on current hour's activity level
  2. Send a weekly breakdown report via Telegram every Monday at 7am

Standalone usage:
    python analytics.py --peak       # print peak hour table
    python analytics.py --weekly     # print weekly group report
"""

import os
import sqlite3
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

DB_PATH = Path(__file__).parent.parent / ".tmp" / "posts.db"

# ── Timing patterns ───────────────────────────────────────────────────────────
# Each pattern is (min_seconds, max_seconds, cycles_before_switch_min, cycles_before_switch_max)
TIMING_PATTERNS = {
    "quick":   (75,  95,  3, 5),   # burst — fresh posts coming in fast
    "normal":  (110, 150, 4, 7),   # standard cadence
    "relaxed": (170, 240, 3, 6),   # slow, human-like
}

# Weights for random pattern selection — relaxed most common, quick least
PATTERN_WEIGHTS = {
    "quick":   1,
    "normal":  3,
    "relaxed": 4,
}

# Minimum posts/hour to qualify as peak or moderate
PEAK_THRESHOLD     = 8   # >= 8 posts/hr historically → peak hour
MODERATE_THRESHOLD = 4   # >= 4 posts/hr → moderate


def _get_conn():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Peak hour analytics ───────────────────────────────────────────────────────

def get_peak_hours() -> dict[int, int]:
    """
    Return a dict of {hour (0-23): total post count} across all recorded history.
    Hours with no data return 0.
    """
    counts = {h: 0 for h in range(24)}
    conn = _get_conn()
    if not conn:
        return counts
    try:
        rows = conn.execute(
            "SELECT strftime('%H', seen_at) as hr, COUNT(*) as cnt FROM seen_posts GROUP BY hr"
        ).fetchall()
        for row in rows:
            counts[int(row["hr"])] = row["cnt"]
    finally:
        conn.close()
    return counts


def get_timing_mode(current_hour: int = None) -> tuple[str, int]:
    """
    Return (pattern_name, sleep_seconds) for the current hour.

    Logic:
    - If current hour is historically peak     → bias toward 'quick'
    - If current hour is historically moderate → bias toward 'normal'
    - If current hour is historically slow     → bias toward 'relaxed'
    - A random override fires ~20% of the time to break predictable patterns

    Returns the pattern name and a randomly chosen sleep duration within that pattern.
    """
    if current_hour is None:
        current_hour = datetime.now().hour

    peak_hours = get_peak_hours()
    hour_count = peak_hours.get(current_hour, 0)

    # Random override — break the rhythm occasionally
    if random.random() < 0.20:
        pattern = random.choices(
            list(PATTERN_WEIGHTS.keys()),
            weights=list(PATTERN_WEIGHTS.values())
        )[0]
    elif hour_count >= PEAK_THRESHOLD:
        pattern = random.choices(["quick", "normal"], weights=[3, 1])[0]
    elif hour_count >= MODERATE_THRESHOLD:
        pattern = random.choices(["normal", "relaxed"], weights=[3, 1])[0]
    else:
        pattern = random.choices(["relaxed", "normal"], weights=[3, 1])[0]

    min_s, max_s, _, _ = TIMING_PATTERNS[pattern]
    sleep_seconds = random.randint(min_s, max_s)
    return pattern, sleep_seconds


# ── Weekly group report ───────────────────────────────────────────────────────

def get_weekly_group_report() -> list[dict]:
    """
    Return per-group stats for the last 7 days, sorted by lead count desc.

    Each entry:
        platform, group_name, total_posts, keyword_hits (score not null),
        leads (score >= 8), top_work_types
    """
    conn = _get_conn()
    if not conn:
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        rows = conn.execute(
            """
            SELECT
                platform,
                group_name,
                COUNT(*)                                        AS total_posts,
                COUNT(CASE WHEN score IS NOT NULL THEN 1 END)  AS keyword_hits,
                COUNT(CASE WHEN score >= 8 THEN 1 END)         AS leads
            FROM seen_posts
            WHERE seen_at >= ?
            GROUP BY platform, group_name
            ORDER BY leads DESC, total_posts DESC
            """,
            (cutoff,)
        ).fetchall()

        # Top work types per group
        work_rows = conn.execute(
            """
            SELECT platform, group_name, work_type, COUNT(*) as cnt
            FROM seen_posts
            WHERE seen_at >= ? AND work_type IS NOT NULL AND score >= 8
            GROUP BY platform, group_name, work_type
            ORDER BY cnt DESC
            """,
            (cutoff,)
        ).fetchall()
    finally:
        conn.close()

    # Build top work types lookup {(platform, group_name): ["patio", "walkway", ...]}
    top_work: dict[tuple, list] = {}
    for wr in work_rows:
        key = (wr["platform"], wr["group_name"])
        if key not in top_work:
            top_work[key] = []
        if len(top_work[key]) < 3:
            top_work[key].append(wr["work_type"])

    report = []
    for row in rows:
        key = (row["platform"], row["group_name"])
        report.append({
            "platform":      row["platform"],
            "group_name":    row["group_name"] or "Unknown",
            "total_posts":   row["total_posts"],
            "keyword_hits":  row["keyword_hits"],
            "leads":         row["leads"],
            "top_work_types": top_work.get(key, []),
        })
    return report


def format_weekly_report() -> str:
    """Format the weekly report into a Telegram-ready message string."""
    report = get_weekly_group_report()
    peak_hours = get_peak_hours()

    if not report:
        return "Weekly Report: No data yet — keep the monitor running to build history."

    # Date range header
    now   = datetime.now()
    start = (now - timedelta(days=7)).strftime("%b %d")
    end   = now.strftime("%b %d")

    lines = [f"Weekly Report — {start} to {end}\n"]

    # Group by platform
    for platform in ["facebook", "nextdoor"]:
        platform_rows = [r for r in report if r["platform"] == platform]
        if not platform_rows:
            continue
        lines.append(f"{platform.capitalize()}")
        for r in platform_rows:
            work_str = ", ".join(r["top_work_types"]) if r["top_work_types"] else "—"
            lines.append(
                f"  {r['group_name'][:35]:<35} "
                f"{r['total_posts']:>3} posts | "
                f"{r['keyword_hits']:>2} matches | "
                f"{r['leads']:>2} leads"
            )
            if r["top_work_types"]:
                lines.append(f"    Top jobs: {work_str}")
        lines.append("")

    # Peak hours summary — top 5
    sorted_hours = sorted(peak_hours.items(), key=lambda x: x[1], reverse=True)
    top5 = [(h, c) for h, c in sorted_hours[:5] if c > 0]
    if top5:
        peak_str = ", ".join(
            f"{h % 12 or 12}{'am' if h < 12 else 'pm'}" for h, _ in top5
        )
        lines.append(f"Peak posting hours: {peak_str} (ET)")

    return "\n".join(lines)


def send_weekly_report():
    """Send the weekly report to Telegram."""
    import json, urllib.request
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("[analytics] Telegram credentials not set — skipping weekly report")
        return

    msg     = format_weekly_report()
    payload = json.dumps({"chat_id": chat_id, "text": msg}).encode("utf-8")
    req     = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print("[analytics] Weekly report sent to Telegram")
    except Exception as e:
        print(f"[analytics] Failed to send weekly report: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--peak" in sys.argv:
        hours = get_peak_hours()
        print("Hour | Posts | Level")
        print("-----|-------|------")
        for h in range(24):
            count = hours[h]
            label = "PEAK" if count >= PEAK_THRESHOLD else ("moderate" if count >= MODERATE_THRESHOLD else "low")
            bar   = "#" * min(count, 40)
            print(f" {h:02d}  |  {count:3d}  | {label:<8}  {bar}")

    elif "--weekly" in sys.argv:
        print(format_weekly_report())

    else:
        pattern, secs = get_timing_mode()
        print(f"Current hour: {datetime.now().hour} — pattern: {pattern} — sleep: {secs}s")
