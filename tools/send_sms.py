"""
send_sms.py
Sends lead alerts to the Telegram group via bot.
Enforces the daily alert budget cap.

Usage:
    python send_sms.py           # sends a real test alert
    python send_sms.py --dry-run # prints message without sending
"""

import os
import sys
import json
import urllib.request
from dotenv import load_dotenv
from post_tracker import get_daily_sms_count, log_sms, init_db

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
DAILY_LIMIT = int(os.getenv("DAILY_SMS_LIMIT", "50"))


def format_alert(post: dict) -> str:
    score      = post.get("score", "?")
    work_type  = post.get("work_type", "Unknown").title()
    town       = post.get("town", "CT")
    summary    = post.get("summary", "")[:120]
    replies    = post.get("reply_count", 0)
    age_min    = post.get("age_minutes", "?")
    platform   = post.get("platform", "").title()
    url        = post.get("url", "")

    age_str = f"{age_min}min ago" if isinstance(age_min, int) else str(age_min)

    urgency   = post.get("urgency", "medium")
    if urgency == "high":
        header = f"URGENT LEAD {score}/10"
    else:
        header = f"LEAD {score}/10"

    lines = [
        f"{header} | {work_type} | {town}, CT",
        f'"{summary}"',
        f"Replies: {replies} | Posted: {age_str} | {urgency.upper()} priority",
        f"Platform: {platform}",
    ]

    # Enrichment fields — only shown when present
    if post.get("suggested_opener"):
        lines.append(f'Opener: "{post["suggested_opener"]}"')
    if post.get("key_question"):
        lines.append(f'Ask: {post["key_question"]}')
    if post.get("budget_signal"):
        signal_label = {"strong": "Quality-focused", "neutral": "No mention", "weak": "Price shopping"}
        lines.append(f'Budget: {signal_label.get(post["budget_signal"], post["budget_signal"])}')
    flags = []
    if post.get("competitor_mentioned"):
        flags.append("already got another quote")
    if post.get("hoa_required"):
        flags.append("HOA approval needed")
    if post.get("permit_likely"):
        flags.append("permit likely required")
    if post.get("deadline"):
        flags.append(f'deadline: {post["deadline"]}')
    if flags:
        lines.append(f'Notes: {" | ".join(flags)}')

    lines.append(f"Link: {url}")
    return "\n".join(lines)


def send_alert(post: dict, dry_run: bool = False) -> bool:
    init_db()

    if not all([BOT_TOKEN, CHAT_ID]):
        print("[send_alert] ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        return False

    daily_count = get_daily_sms_count()
    if daily_count >= DAILY_LIMIT:
        print(f"[send_alert] Daily limit reached ({daily_count}/{DAILY_LIMIT}). Skipping.")
        return False

    body = format_alert(post)
    post_id  = post.get("post_id", "unknown")
    platform = post.get("platform", "unknown")

    if dry_run:
        print("[send_alert] DRY RUN — would send:\n" + body)
        return True

    try:
        payload = json.dumps({
            "chat_id":    CHAT_ID,
            "text":       body,
            "parse_mode": "HTML"
        }).encode("utf-8")

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        res = json.loads(urllib.request.urlopen(req, timeout=10).read())

        if res.get("ok"):
            log_sms(post_id, platform, CHAT_ID)
            print(f"[send_alert] Sent to Telegram group | message_id: {res['result']['message_id']}")
            return True
        else:
            print(f"[send_alert] Telegram error: {res}")
            return False

    except Exception as e:
        print(f"[send_alert] ERROR: {e}")
        return False


if __name__ == "__main__":
    test_post = {
        "post_id":     "test_001",
        "platform":    "nextdoor",
        "score":       9,
        "work_type":   "chimney repair",
        "town":        "Westport",
        "summary":     "My chimney is crumbling and mortar is falling out. Need someone ASAP.",
        "reply_count": 0,
        "age_minutes": 8,
        "url":         "https://nextdoor.com/p/test123",
    }
    dry = "--dry-run" in sys.argv
    send_alert(test_post, dry_run=dry)
