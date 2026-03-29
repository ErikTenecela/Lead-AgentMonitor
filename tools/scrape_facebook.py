"""
scrape_facebook.py
Scrapes Facebook Groups for new posts using Playwright with a saved session.
Each group is checked independently. Run once manually to create the session first.

Usage:
    # First-time login (opens browser for you to log in manually):
    python scrape_facebook.py --login

    # Normal scrape run:
    python scrape_facebook.py
"""

import os
import sys
import time
import random
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

SESSION_PATH    = Path(os.getenv("FACEBOOK_SESSION_PATH", "./sessions/facebook"))
MAX_AGE_MINUTES = int(os.getenv("MAX_POST_AGE_MINUTES", "120"))

# Facebook groups to monitor — loaded from workflows/monitor_posts.md config
# Keys: url, name, town
GROUPS_CONFIG_PATH = Path(__file__).parent.parent / "workflows" / "facebook_groups.json"

DEFAULT_GROUPS = [
    {"url": "https://www.facebook.com/groups/westportct",          "name": "Westport CT Community",         "town": "Westport"},
    {"url": "https://www.facebook.com/groups/greenwichctcommunity","name": "Greenwich CT Community",        "town": "Greenwich"},
    {"url": "https://www.facebook.com/groups/stamfordct",          "name": "Stamford CT Community",         "town": "Stamford"},
    {"url": "https://www.facebook.com/groups/norwalkct",           "name": "Norwalk CT Community",          "town": "Norwalk"},
    {"url": "https://www.facebook.com/groups/fairfieldcountyrealestate","name": "Fairfield County Real Estate","town": "Fairfield County"},
    {"url": "https://www.facebook.com/groups/cthomeimprovement",   "name": "CT Home Improvement & Repair",  "town": "CT"},
]


def _load_groups() -> list[dict]:
    if GROUPS_CONFIG_PATH.exists():
        with open(GROUPS_CONFIG_PATH) as f:
            return json.load(f)
    return DEFAULT_GROUPS


def _save_groups(groups: list[dict]):
    GROUPS_CONFIG_PATH.parent.mkdir(exist_ok=True)
    with open(GROUPS_CONFIG_PATH, "w") as f:
        json.dump(groups, f, indent=2)


def _post_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]


def _parse_relative_time(text: str) -> int | None:
    """Convert Facebook relative time ('5 minutes ago', '2 hours ago') to minutes."""
    text = text.lower().strip()
    if "just now" in text or "now" == text:
        return 0
    m = re.search(r"(\d+)\s*(minute|min|hour|hr|day)", text)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    if "min" in unit:
        return val
    if "hour" in unit or "hr" in unit:
        return val * 60
    if "day" in unit:
        return val * 1440
    return None


def login(session_path: Path):
    """Open a browser for manual Facebook login. Saves session automatically when login is detected."""
    session_path.mkdir(parents=True, exist_ok=True)
    print("Opening Facebook login browser... Log in normally (including any 2FA).")
    print("The browser will close automatically once you're logged in.")
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            str(session_path),
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        page = browser.new_page()
        page.goto("https://www.facebook.com/login")

        # Wait up to 5 minutes for successful login (URL leaves /login page)
        try:
            page.wait_for_url(
                lambda url: "facebook.com" in url and "/login" not in url,
                timeout=300_000  # 5 minutes
            )
            print("Facebook login detected! Saving session...")
            time.sleep(2)  # let cookies settle
        except Exception:
            print("Timed out waiting for login. Session may not be saved.")

        browser.close()
    print(f"Facebook session saved to {session_path}")


def discover_joined_groups(page) -> list[dict]:
    """Visit facebook.com/groups to find all groups the account has joined."""
    groups = []
    try:
        page.goto("https://www.facebook.com/groups/", timeout=30000, wait_until="domcontentloaded")
        time.sleep(4)
        for _ in range(3):
            page.mouse.wheel(0, random.randint(400, 700))
            time.sleep(random.uniform(1.0, 2.0))

        # Find group links in the sidebar / joined groups list
        links = page.query_selector_all("a[href*='facebook.com/groups/'], a[href^='/groups/']")
        seen = set()
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                if href.startswith("/groups/"):
                    href = "https://www.facebook.com" + href
                # Only keep actual group pages, skip feed/discover/etc
                if not re.search(r"/groups/[\w.]+/?$", href):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                name = link.inner_text().strip() or href.split("/groups/")[-1].strip("/")
                if name and len(name) > 1:
                    groups.append({"url": href, "name": name, "town": "Fairfield County"})
            except Exception:
                continue
    except Exception as e:
        print(f"[facebook] Error discovering groups: {e}")

    print(f"[facebook] Found {len(groups)} joined groups")
    return groups


def scrape_group(page, group: dict) -> list[dict]:
    """Scrape a single Facebook group for recent posts."""
    posts = []
    try:
        page.goto(group["url"], timeout=30000, wait_until="domcontentloaded")
        time.sleep(3)

        for _ in range(3):
            page.keyboard.press("End")
            time.sleep(1.5)

        articles = page.query_selector_all("div[role='article']")

        for article in articles[:20]:
            try:
                text_el = article.query_selector("div[data-ad-comet-preview='message']") or \
                          article.query_selector("div[dir='auto']")
                text = text_el.inner_text() if text_el else ""
                if not text or len(text) < 15:
                    continue

                link_el = article.query_selector("a[href*='/posts/'], a[href*='/groups/'][href*='permalink']")
                post_url = ""
                if link_el:
                    post_url = link_el.get_attribute("href") or ""
                    if post_url.startswith("/"):
                        post_url = "https://www.facebook.com" + post_url

                time_el = article.query_selector("abbr, span[data-utime], a[role='link'] abbr")
                age_str = time_el.inner_text() if time_el else ""
                age_min = _parse_relative_time(age_str)

                if age_min is not None and age_min > MAX_AGE_MINUTES:
                    continue

                reply_count = 0
                reply_els = article.query_selector_all("span:has-text('comment'), span:has-text('Comment')")
                for el in reply_els[:1]:
                    m = re.search(r"(\d+)", el.inner_text())
                    if m:
                        reply_count = int(m.group(1))

                if not post_url:
                    continue

                posts.append({
                    "post_id":     _post_id(post_url + text[:50]),
                    "platform":    "facebook",
                    "group_name":  group["name"],
                    "town":        group["town"],
                    "url":         post_url,
                    "title":       text[:80],
                    "text":        text[:1500],
                    "age_minutes": age_min,
                    "reply_count": reply_count,
                })
            except Exception:
                continue

    except PWTimeout:
        print(f"[facebook] Timeout loading {group['name']}")
    except Exception as e:
        print(f"[facebook] Error scraping {group['name']}: {e}")

    return posts


def fetch_posts() -> list[dict]:
    """Discover joined groups then scrape each for recent posts."""
    if not SESSION_PATH.exists():
        print(f"[facebook] No session found at {SESSION_PATH}. Run with --login first.")
        return []

    all_posts = []

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            str(SESSION_PATH),
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        page = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

        # Step 1: discover which groups the account has joined
        groups = discover_joined_groups(page)
        if not groups:
            print("[facebook] No joined groups found — may need to re-login")
            browser.close()
            return []

        # Step 2: scrape each group
        for group in groups:
            try:
                _ = page.url  # check browser still open
            except Exception:
                print("[facebook] Browser closed — stopping")
                break
            posts = scrape_group(page, group)
            print(f"[facebook] {group['name']}: {len(posts)} posts")
            all_posts.extend(posts)
            time.sleep(random.uniform(6, 12))

        try:
            browser.close()
        except Exception:
            pass

    print(f"[facebook] Total: {len(all_posts)} recent posts from groups feed")
    return all_posts


if __name__ == "__main__":
    if "--login" in sys.argv:
        SESSION_PATH.mkdir(parents=True, exist_ok=True)
        login(SESSION_PATH)
    else:
        posts = fetch_posts()
        for p in posts[:5]:
            print(f"  [{p['group_name']}] {p['title'][:80]}")
            print(f"    Age: {p['age_minutes']}min | Replies: {p['reply_count']} | {p['url']}")
