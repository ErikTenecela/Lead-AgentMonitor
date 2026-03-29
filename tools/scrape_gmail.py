"""
scrape_gmail.py
Reads Facebook group notification emails from Gmail via IMAP.
Extracts post content and returns it in the same format as other scrapers.

Setup:
  1. In Facebook: turn on "All Posts" email notifications for each joined group
  2. In Gmail: enable IMAP (Settings > See all settings > Forwarding and POP/IMAP)
  3. Create a Gmail App Password (myaccount.google.com > Security > App passwords)
  4. Add to .env:
       GMAIL_ADDRESS=warrioragent9@gmail.com
       GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""

import os
import imaplib
import email
import hashlib
import re
from email.header import decode_header
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

GMAIL_ADDRESS     = os.getenv("GMAIL_ADDRESS", "warrioragent9@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
MAX_AGE_MINUTES   = int(os.getenv("MAX_POST_AGE_MINUTES", "120"))

# Facebook sends notifications from these domains
FACEBOOK_SENDERS = ["facebookmail.com", "notification.facebook.com"]


def _post_id(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:16]


def _decode_str(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="ignore")
        else:
            result += part
    return result


def _extract_post_text(msg) -> str:
    """Extract readable post text from a Facebook notification email."""
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="ignore")
                    break
            elif ctype == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode("utf-8", errors="ignore")
                    # Strip HTML tags
                    body = re.sub(r"<[^>]+>", " ", html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="ignore")

    # Clean up whitespace
    body = re.sub(r"\s+", " ", body).strip()

    # Facebook emails have lots of boilerplate — extract just the post content
    # Look for the actual post text between common markers
    patterns = [
        r"posted in .+?:\s*(.+?)(?:Reply to this|See Translation|View Post|Like|Comment|This message was sent)",
        r"wrote in .+?:\s*(.+?)(?:Reply to this|See Translation|View Post|Like|Comment)",
        r"new post in .+?:\s*(.+?)(?:Reply to this|See Translation|View Post|Like|Comment)",
    ]
    for pattern in patterns:
        m = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()[:1500]

    # Fallback: return cleaned body up to 1500 chars
    return body[:1500]


def _extract_group_name(subject: str, body: str) -> str:
    """Try to extract the group name from email subject or body."""
    # Subject often looks like: "Erik posted in Westport CT Community"
    m = re.search(r"posted in (.+?)$", subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"new post in (.+?):", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return "Facebook Group"


def _extract_post_url(body: str) -> str:
    """Extract Facebook post URL from email body."""
    m = re.search(r"https://www\.facebook\.com/groups/[^\s\"<>]+", body)
    if m:
        return m.group(0).split("?")[0]  # strip tracking params
    m = re.search(r"https://www\.facebook\.com/permalink/[^\s\"<>]+", body)
    if m:
        return m.group(0).split("?")[0]
    return ""


def fetch_posts() -> list[dict]:
    """Connect to Gmail via IMAP and read unread Facebook notification emails."""
    if not GMAIL_APP_PASSWORD:
        print("[gmail] GMAIL_APP_PASSWORD not set in .env — skipping")
        return []

    posts = []

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        # Search for unread emails from Facebook
        _, data = mail.search(None, '(UNSEEN FROM "facebookmail.com")')
        email_ids = data[0].split()

        if not email_ids:
            print(f"[gmail] No unread Facebook emails")
            mail.logout()
            return []

        print(f"[gmail] Found {len(email_ids)} unread Facebook email(s)")

        for eid in email_ids[-50:]:  # process up to 50 at a time
            try:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = _decode_str(msg.get("Subject", ""))
                sender  = msg.get("From", "")

                # Only process Facebook notification emails
                if not any(fb in sender for fb in FACEBOOK_SENDERS):
                    continue

                # Skip non-post notifications (friend requests, birthdays, etc.)
                skip_subjects = ["friend request", "birthday", "memory", "marketplace",
                                 "tagged you", "mentioned you", "event", "story"]
                if any(s in subject.lower() for s in skip_subjects):
                    # Mark as read so we don't reprocess
                    mail.store(eid, "+FLAGS", "\\Seen")
                    continue

                raw_body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                raw_body = payload.decode("utf-8", errors="ignore")
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        raw_body = payload.decode("utf-8", errors="ignore")

                post_text  = _extract_post_text(msg)
                group_name = _extract_group_name(subject, raw_body)
                post_url   = _extract_post_url(raw_body)

                if not post_text or len(post_text) < 15:
                    mail.store(eid, "+FLAGS", "\\Seen")
                    continue

                post_id = _post_id(post_text[:80])

                posts.append({
                    "post_id":     post_id,
                    "platform":    "facebook",
                    "group_name":  group_name,
                    "town":        "Fairfield County",
                    "url":         post_url or "https://www.facebook.com/groups/",
                    "title":       post_text[:80],
                    "text":        post_text,
                    "age_minutes": None,
                    "reply_count": 0,
                })

                # Mark as read so we don't process again
                mail.store(eid, "+FLAGS", "\\Seen")

            except Exception as e:
                print(f"[gmail] Error processing email {eid}: {e}")
                continue

        mail.logout()

    except imaplib.IMAP4.error as e:
        print(f"[gmail] Login failed: {e}")
    except Exception as e:
        print(f"[gmail] Error: {e}")

    print(f"[gmail] Extracted {len(posts)} posts from Facebook emails")
    return posts


if __name__ == "__main__":
    posts = fetch_posts()
    for p in posts:
        print(f"[{p['group_name']}] {p['text'][:120]}")
        print(f"  URL: {p['url']}")
        print()
