"""
scrape_nextdoor.py
Scrapes Nextdoor local feeds for home service posts.
Credentials are stored in .env — sessions are created and refreshed automatically.

First-time setup (new accounts or after rate limit):
    python scrape_nextdoor.py --setup

Normal operation (called by orchestrator):
    python scrape_nextdoor.py

Verify accounts loaded:
    python scrape_nextdoor.py --test
"""

import os
import sys
import time
import hashlib
import re
import random
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

SESSION_BASE    = Path(os.getenv("NEXTDOOR_SESSION_PATH", "./sessions/nextdoor")).parent
MAX_AGE_MINUTES = int(os.getenv("MAX_POST_AGE_MINUTES", "120"))

# Tracks per-account login failures for staggered backoff {city: last_fail_timestamp}
_login_failures: dict[str, float] = {}
LOGIN_BACKOFF_SECONDS = 600  # wait 10 min before retrying a failed account


# ── Feeds ────────────────────────────────────────────────────────────────────

def _with_recent_sort(url: str) -> str:
    if "sort=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return url.rstrip("/") + sep + "sort=recent"

_BASE_FEEDS = [
    {"url": "https://nextdoor.com/news_feed/", "section": "neighborhood feed"},
]
_extra_raw = os.getenv("NEXTDOOR_NEIGHBORHOOD_URLS", "")
_extra_feeds = [
    {"url": url.strip(), "section": url.strip().rstrip("/").split("/")[-1]}
    for url in _extra_raw.split(",") if url.strip()
]
NEXTDOOR_FEEDS = [
    {**feed, "url": _with_recent_sort(feed["url"])}
    for feed in _BASE_FEEDS + _extra_feeds
]


# ── Account loading ──────────────────────────────────────────────────────────

def _load_accounts() -> list[dict]:
    """Read all NEXTDOOR_ACCOUNT_N_City=email:password entries from .env."""
    accounts = []
    for key, value in sorted(os.environ.items()):
        if not re.match(r"NEXTDOOR_ACCOUNT_\d+", key):
            continue
        if not value or ":" not in value:
            continue
        parts = key.split("_", 3)
        city  = parts[3] if len(parts) > 3 else f"account{parts[2]}"
        email, password = value.split(":", 1)
        session_path = SESSION_BASE / f"nextdoor_{city.lower()}"
        accounts.append({
            "email":        email.strip(),
            "password":     password.strip(),
            "city":         city,
            "session_path": session_path,
        })
    return accounts


# ── Session health & auto re-login ──────────────────────────────────────────

def _is_logged_in(page) -> bool:
    url = page.url
    return (
        "nextdoor.com" in url
        and "/login"  not in url
        and "/auth"   not in url
        and "/verify" not in url
        and "/sso"    not in url
        and "accounts.google" not in url
    )


def _auto_login(page, email: str, password: str, city: str) -> bool:
    """Log in with email + password. Returns True on success."""
    print(f"[nextdoor:{city}] Session expired — auto re-login...")
    try:
        page.goto("https://nextdoor.com/login/", timeout=30000, wait_until="domcontentloaded")
        time.sleep(2)

        # Click email field and type like a human
        email_input = page.query_selector("input[name='email'], input[type='email']")
        if not email_input:
            print(f"[nextdoor:{city}] Email field not found")
            return False
        email_input.click()
        time.sleep(0.3)
        email_input.type(email, delay=50)  # type character by character
        time.sleep(0.5)

        # Tab to password if it's on the same page, otherwise click Continue
        pw_input = page.query_selector("input[type='password']")
        if pw_input:
            # Single-page form — tab to password and type it
            page.keyboard.press("Tab")
            time.sleep(0.3)
            pw_input.type(password, delay=50)
            time.sleep(0.5)
            page.keyboard.press("Enter")
        else:
            # Two-step — submit email first, wait for password screen
            page.keyboard.press("Enter")
            time.sleep(2)
            pw_input = page.query_selector("input[type='password']")
            if not pw_input:
                time.sleep(2)
                pw_input = page.query_selector("input[type='password']")
            if not pw_input:
                print(f"[nextdoor:{city}] Password field not found after email step")
                return False
            pw_input.click()
            time.sleep(0.3)
            pw_input.type(password, delay=50)
            time.sleep(0.5)
            page.keyboard.press("Enter")

        # Wait up to 30s for redirect to home feed
        for _ in range(20):
            time.sleep(1.5)
            if _is_logged_in(page):
                print(f"[nextdoor:{city}] Auto re-login successful")
                time.sleep(3)
                return True

        print(f"[nextdoor:{city}] Auto re-login failed — still on: {page.url}")
        return False

    except Exception as e:
        print(f"[nextdoor:{city}] Auto re-login error: {e}")
        return False


def _manual_login(session_path: Path, city: str, email: str):
    """
    Open a visible browser for manual one-time login.
    Used for first-time setup or after rate limiting.
    Saves session automatically once login is detected.
    """
    print(f"\n[nextdoor:{city}] Opening browser for manual login ({email})")
    print(f"[nextdoor:{city}] Log in normally — browser closes automatically when done.\n")
    session_path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            str(session_path),
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        page = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        page.goto("https://nextdoor.com/login/")
        # Poll until logged in (up to 10 minutes)
        for _ in range(200):
            time.sleep(3)
            if _is_logged_in(page):
                print(f"[nextdoor:{city}] Login confirmed — saving session...")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                time.sleep(5)
                break
        else:
            print(f"[nextdoor:{city}] Timed out waiting for login.")
        browser.close()
    print(f"[nextdoor:{city}] Session saved.")


def _send_login_failure_alert(city: str, email: str):
    """Send Telegram alert when auto re-login fails."""
    try:
        import json, urllib.request
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id   = os.getenv("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            return
        msg = (
            f"Nextdoor login FAILED\n"
            f"Account: {city} ({email})\n"
            f"Action needed: check credentials in .env"
        )
        payload = json.dumps({"chat_id": chat_id, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


# ── Post parsing ─────────────────────────────────────────────────────────────

def _post_id(url: str, text: str = "") -> str:
    return hashlib.md5((url + text[:40]).encode()).hexdigest()[:16]


def _parse_relative_time(text: str) -> int | None:
    text = text.lower().strip()
    if not text:
        return None
    if "just now" in text or text in ("now", "moments ago"):
        return 0
    m = re.search(r"(\d+)\s*(s|sec|second|m|min|minute|h|hr|hour|d|day|w|week)", text)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    if unit in ("s", "sec", "second"):  return 0
    if unit in ("m", "min", "minute"):  return val
    if unit in ("h", "hr", "hour"):     return val * 60
    if unit in ("d", "day"):            return val * 1440
    if unit in ("w", "week"):           return val * 10080
    return None


_EXTRACT_POST_JS = """el => {
    const key = Object.keys(el).find(k => k.startsWith('__reactFiber'));
    if (!key) return null;
    let fiber = el[key];
    for (let i = 0; i < 30; i++) {
        const props = fiber.memoizedProps || fiber.pendingProps;
        if (props && props.post) {
            const p = props.post;
            return {
                id:           p.id || null,
                href:         p.detailLink ? p.detailLink.href : null,
                body:         p.body || p.markdownBody || p.subject || "",
                relativeTime: p.createdAt ? p.createdAt.asDateTime.relativeTime : null,
                commentCount: p.comments ? (p.comments.totalCount || 0) : 0,
                town:         p.author ? (p.author.neighborhoodName || p.author.cityName || "") : "",
            };
        }
        fiber = fiber.return;
        if (!fiber) break;
    }
    return null;
}"""


def scrape_feed(page, feed: dict) -> list[dict]:
    posts = []
    try:
        page.goto(feed["url"], timeout=30000, wait_until="domcontentloaded")
        time.sleep(4)

        for _ in range(10):
            page.keyboard.press("End")
            time.sleep(1.5)

        cards = page.query_selector_all("div[data-testid='feed-item-card']")
        if not cards:
            # Fallback to old selector in case Nextdoor changes again
            cards = page.query_selector_all("div[data-testid='post-card'], article")

        for card in cards[:50]:
            try:
                data = card.evaluate(_EXTRACT_POST_JS)
                if not data or not data.get("href"):
                    continue

                text = (data.get("body") or "").strip()
                if not text or len(text) < 20:
                    continue

                # Use Nextdoor's own post ID when available — stable across scrapes.
                # Falling back to URL-only hash (no text) prevents duplicate IDs
                # caused by minor text extraction differences between cycles.
                native_id = data.get("id")
                if native_id:
                    post_id = f"nd_{native_id}"
                else:
                    post_url_for_id = "https://nextdoor.com" + data["href"]
                    post_id = hashlib.md5(post_url_for_id.encode()).hexdigest()[:16]

                post_url    = "https://nextdoor.com" + data["href"]
                age_min     = _parse_relative_time(data.get("relativeTime") or "")
                age_unknown = age_min is None

                # Skip posts we know are too old. Unknown-age posts pass through
                # but are flagged so the orchestrator can retry them next cycle.
                if age_min is not None and age_min > MAX_AGE_MINUTES:
                    continue

                reply_count = int(data.get("commentCount") or 0)
                town        = data.get("town") or "Fairfield County"

                posts.append({
                    "post_id":     post_id,
                    "platform":    "nextdoor",
                    "group_name":  feed["section"],
                    "town":        town,
                    "url":         post_url,
                    "title":       text[:80],
                    "text":        text[:1500],
                    "age_minutes": age_min,
                    "age_unknown": age_unknown,
                    "reply_count": reply_count,
                })

            except Exception:
                continue

    except PWTimeout:
        print(f"[nextdoor] Timeout loading {feed['section']}")
    except Exception as e:
        print(f"[nextdoor] Error scraping {feed['section']}: {e}")

    return posts


# ── Per-account scrape ───────────────────────────────────────────────────────

def _scrape_account(account: dict) -> list[dict]:
    """
    Scrape all feeds for one account.
    Auto-creates session on first run, silently re-logs in when session expires.
    """
    city         = account["city"]
    email        = account["email"]
    password     = account["password"]
    session_path = account["session_path"]
    session_path.mkdir(parents=True, exist_ok=True)

    all_posts = []

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            str(session_path),
            headless=True,
            channel="chrome",
            slow_mo=random.randint(30, 80),
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
            viewport={
                "width":  random.choice([1280, 1366, 1440, 1920]),
                "height": random.choice([768, 800, 900, 1080]),
            },
        )
        page = browser.new_page()
        if STEALTH_AVAILABLE:
            stealth_sync(page)
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

        # Session health check
        try:
            page.goto("https://nextdoor.com/news_feed/", timeout=20000, wait_until="domcontentloaded")
            time.sleep(3)
        except Exception:
            pass

        # Auto re-login if needed
        if not _is_logged_in(page):
            # Check backoff — skip account if it failed recently
            last_fail = _login_failures.get(city, 0)
            if time.time() - last_fail < LOGIN_BACKOFF_SECONDS:
                wait_left = int(LOGIN_BACKOFF_SECONDS - (time.time() - last_fail))
                print(f"[nextdoor:{city}] Skipping — cooling down ({wait_left}s left after last failure)")
                browser.close()
                return []

            success = _auto_login(page, email, password, city)
            if not success:
                _login_failures[city] = time.time()  # record failure time
                _send_login_failure_alert(city, email)
                browser.close()
                return []
            else:
                # Clear failure record on success
                _login_failures.pop(city, None)

        # Scrape feeds
        for feed in NEXTDOOR_FEEDS:
            posts = scrape_feed(page, feed)
            print(f"[nextdoor:{city}] {feed['section']}: {len(posts)} posts")
            all_posts.extend(posts)
            time.sleep(random.uniform(2.5, 5.0))

        browser.close()

    return all_posts


# ── Public interface ─────────────────────────────────────────────────────────

def fetch_posts() -> list[dict]:
    accounts = _load_accounts()
    if not accounts:
        print("[nextdoor] No accounts found in .env — add NEXTDOOR_ACCOUNT_1_City=email:password")
        return []

    print(f"[nextdoor] Running {len(accounts)} account(s): {[a['city'] for a in accounts]}")
    all_posts = []
    seen_ids  = set()

    for account in accounts:
        posts = _scrape_account(account)
        for post in posts:
            if post["post_id"] not in seen_ids:
                seen_ids.add(post["post_id"])
                all_posts.append(post)

    print(f"[nextdoor] Total unique posts: {len(all_posts)}")
    return all_posts


if __name__ == "__main__":
    if "--test" in sys.argv:
        accounts = _load_accounts()
        print(f"Found {len(accounts)} account(s) in .env:")
        for a in accounts:
            print(f"  {a['city']}: {a['email']} -> {a['session_path']}")

    elif "--setup" in sys.argv:
        # Manual first-time login for accounts that have no valid session
        accounts = _load_accounts()
        needs_setup = []
        for a in accounts:
            cookies_file = a["session_path"] / "Default" / "Cookies"
            if not cookies_file.exists() or cookies_file.stat().st_size < 5000:
                needs_setup.append(a)
        if not needs_setup:
            print("All accounts already have sessions. Nothing to set up.")
        else:
            print(f"{len(needs_setup)} account(s) need first-time login:")
            for a in needs_setup:
                print(f"  {a['city']} ({a['email']})")
            print()
            for a in needs_setup:
                _manual_login(a["session_path"], a["city"], a["email"])
                time.sleep(3)  # brief pause between accounts
            print("\nSetup complete. Run normally: python scrape_nextdoor.py")

    else:
        posts = fetch_posts()
        for p in posts[:5]:
            title = p['title'][:80].encode('ascii', errors='replace').decode()
            print(f"  [{p['town']}] {title}")
            print(f"    Age: {p['age_minutes']}min | Replies: {p['reply_count']} | {p['url']}")
