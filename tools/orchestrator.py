"""
orchestrator.py
Main runner. Polls all platforms in parallel, classifies new posts, sends SMS alerts.
Schedule this to run every 5 minutes via Windows Task Scheduler.

Usage:
    python orchestrator.py          # run one cycle
    python orchestrator.py --loop   # run continuously every 5 minutes
"""

import os
import sys
import time
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

# Add tools directory to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

import post_tracker
import classify_post as classifier
import send_sms
import scrape_facebook
import scrape_nextdoor
import scrape_gmail
import dedup_checker
import lead_enricher
import digest_formatter

SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "8"))
POLL_INTERVAL   = 120  # 2 minutes in seconds
CT_TZ           = ZoneInfo("America/New_York")
BUSINESS_START  = dt_time(7, 0)
BUSINESS_END    = dt_time(20, 0)

# Keywords that must appear in a post before we spend an API call classifying it
KEYWORDS = [
    # Masonry
    "chimney", "masonry", "mason", "brick", "mortar", "tuckpoint", "pointing",
    "retaining wall", "stone work", "stonework", "flagstone",
    # Hardscape
    "patio", "walkway", "pathway", "bluestone", "pavers", "concrete", "driveway",
    "foundation", "steps", "stoop",
    # Cleanup/junk
    "junk removal", "junk hauling", "debris removal", "yard cleanup", "cleanout",
    "hauling", "dump run", "trash removal",
    # Tree
    "tree removal", "tree trimming", "stump", "tree service", "fallen tree",
    "branch", "arborist",
    # Painting
    "painting", "power wash", "pressure wash", "exterior paint", "interior paint",
    # Landscaping
    "landscaping", "landscape", "lawn care", "mulch", "grading", "drainage",
    "lawn mowing", "yard work", "garden", "planting", "sod",
    # Weather/storm (surge events)
    "storm damage", "flood", "water damage", "ice dam", "roof damage",
    "branch fell", "tree fell", "hurricane damage",
    # Seasonal
    "spring cleanup", "fall cleanup", "winter prep", "before winter",
    "spring project", "outdoor project",
    # Request intent signals
    "contractor", "handyman", "estimate", "quote", "repair", "install",
    "need someone", "looking for", "recommend", "anyone know", "who does",
    "can anyone", "suggestions", "referral", "recommendations",
    # Budget signals
    "how much", "what does it cost", "price", "affordable", "asap", "urgent",
    "emergency", "need done quickly", "need done soon",
]


def keyword_match(text: str) -> bool:
    """Return True if post contains at least one relevant keyword."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in KEYWORDS)


def is_business_hours() -> bool:
    now = datetime.now(CT_TZ).time()
    return BUSINESS_START <= now <= BUSINESS_END


def run_scraper(scraper_module, results: list, errors: list):
    """Thread target — runs a scraper and appends posts to shared results list."""
    try:
        posts = scraper_module.fetch_posts()
        results.extend(posts)
    except Exception as e:
        errors.append(f"{scraper_module.__name__}: {e}")


def run_cycle():
    """Run one full poll-classify-alert cycle."""
    print(f"\n[orchestrator] Cycle start: {datetime.now(CT_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    post_tracker.init_db()

    # Step 1: Scrape all platforms in parallel
    all_posts = []
    errors    = []
    scrapers  = [scrape_gmail, scrape_nextdoor]
    threads   = []

    for scraper in scrapers:
        thread = threading.Thread(
            target=run_scraper,
            args=(scraper, all_posts, errors),
            daemon=True
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join(timeout=300)

    if errors:
        for err in errors:
            print(f"[orchestrator] Scraper error: {err}")

    print(f"[orchestrator] Total posts fetched: {len(all_posts)}")

    # Step 2: Filter out already-seen posts
    new_posts = [
        p for p in all_posts
        if not post_tracker.is_seen(p["post_id"], p["platform"])
    ]
    print(f"[orchestrator] New (unseen) posts: {len(new_posts)}")

    if not new_posts:
        print("[orchestrator] No new posts to process.")
        return

    # Step 3: Keyword pre-filter — skip Claude API call if no relevant terms found
    keyword_hits = [p for p in new_posts if keyword_match(p.get("text", "") + " " + p.get("title", ""))]
    skipped = len(new_posts) - len(keyword_hits)
    if skipped:
        print(f"[orchestrator] Skipped {skipped} posts (no relevant keywords) — API call saved")
        # Only mark as seen if we have a confirmed age — unknown-age posts get
        # another chance next cycle in case they were missed due to scroll depth.
        for post in new_posts:
            if post not in keyword_hits and not post.get("age_unknown"):
                post_tracker.mark_seen(post["post_id"], post["platform"],
                                       group_name=post.get("group_name"), town=post.get("town"), url=post.get("url"))

    print(f"[orchestrator] Keyword matches to classify: {len(keyword_hits)}")

    # Step 4: Classify keyword-matched posts with Claude
    leads = []
    for post in keyword_hits:
        # Mark as seen immediately to prevent duplicate processing on next cycle
        post_tracker.mark_seen(
            post["post_id"], post["platform"],
            group_name=post.get("group_name"),
            town=post.get("town"),
            url=post.get("url"),
        )

        result = classifier.classify(post.get("text", ""), post.get("town", "Unknown"))
        if not result:
            continue

        score = result.get("score", 0)
        print(f"[orchestrator] Score {score}/10 | {result.get('work_type')} | {post.get('group_name')} | {post.get('url', '')[:60]}")

        post_type = result.get("post_type", "request")
        if post_type in ("offer", "referral", "other"):
            print(f"[orchestrator] Skipping {post_type} post | {post.get('url', '')[:60]}")
            continue

        if score >= SCORE_THRESHOLD:
            reply_count = post.get("reply_count", 0) or 0
            if reply_count > 0:
                print(f"[orchestrator] Skipping — {reply_count} repl(ies) already | {post.get('url', '')[:60]}")
                continue

            # Dedup check: compare against recent notified leads with same work_type or town
            recent = post_tracker.get_recent_similar_posts(
                result.get("work_type", ""), post.get("town", "")
            )
            is_dup = False
            for prev in recent:
                prev_post = {
                    "text": f"{prev['work_type'] or 'unknown'} request in {prev['town'] or 'CT'}",
                    "town": prev["town"] or "",
                    "posted": prev["seen_at"],
                    "url": prev["url"] or ""
                }
                new_post = {
                    "text": post.get("text", ""),
                    "town": post.get("town", ""),
                    "posted": f"{post.get('age_minutes', '?')} min ago",
                    "url": post.get("url", "")
                }
                dup = dedup_checker.check_duplicate(prev_post, new_post)
                if dup:
                    print(f"[orchestrator] Dedup: {dup['recommendation'].upper()} | conf={dup['confidence']} | {dup['reason']}")
                    if dup["recommendation"] == "skip":
                        is_dup = True
                        break
            if is_dup:
                continue

            post.update(result)  # merge classification into post dict
            leads.append(post)

    print(f"[orchestrator] Qualifying leads (score >= {SCORE_THRESHOLD}, 0 replies): {len(leads)}")

    # Step 5: Send alerts
    if not leads:
        print("[orchestrator] No leads to alert on.")
        return

    in_hours = is_business_hours()
    if not in_hours:
        print("[orchestrator] Outside business hours (7am–8pm CT). Leads logged, no SMS sent.")
        # Leads are already in SQLite — the 7am morning digest will pick them up
        return

    sent_count = 0
    for lead in leads:
        # Enrich lead with business intelligence before alerting
        enrichment = lead_enricher.enrich_lead(lead)
        if enrichment:
            lead.update(enrichment)
            print(f"[orchestrator] Enriched | {lead.get('work_type')} | opener: {enrichment.get('suggested_opener', '')[:60]}")

        sent = send_sms.send_alert(lead)
        if sent:
            sent_count += 1
            post_tracker.mark_notified(lead["post_id"], lead["platform"])
            post_tracker.mark_seen(
                lead["post_id"], lead["platform"],
                notified=True,
                score=lead.get("score"),
                work_type=lead.get("work_type"),
                summary=lead.get("summary"),
                urgency=lead.get("urgency"),
            )

    print(f"[orchestrator] Cycle complete. Alerts sent: {sent_count}")


def send_morning_digest():
    """
    At 7am, send a smart digest of overnight leads using digest_formatter.
    Called automatically by the cycle loop when the time crosses 7am.
    """
    import sqlite3
    from datetime import datetime as dt
    db_path = Path(__file__).parent.parent / ".tmp" / "posts.db"
    if not db_path.exists():
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM seen_posts WHERE notified=0 AND score >= ? ORDER BY score DESC LIMIT 20",
        (SCORE_THRESHOLD,)
    ).fetchall()
    conn.close()

    if not rows:
        return

    print(f"[orchestrator] Morning digest: {len(rows)} overnight leads")

    # Build lead dicts for digest_formatter
    digest_leads = []
    for row in rows:
        try:
            seen_dt = dt.fromisoformat(row["seen_at"])
            age_min = int((dt.utcnow() - seen_dt).total_seconds() / 60)
        except Exception:
            age_min = 480  # fallback: assume 8 hours

        digest_leads.append({
            "score":       row["score"] or 0,
            "work_type":   row["work_type"] or "unknown",
            "town":        row["town"] or "CT",
            "urgency":     row["urgency"] or "medium",
            "summary":     row["summary"] or f"Lead from {row['platform']}",
            "url":         row["url"] or "",
            "age_minutes": age_min,
        })

    # Format into a single smart morning message
    result = digest_formatter.format_digest(digest_leads)
    if result:
        msg = result["digest_message"]
        top = result["top_lead"]
        full_msg = (
            f"{msg}\n\n"
            f"Top pick: {top['score']}/10 | {top['urgency'].upper()} | {top['town']}, CT\n"
            f"{top['summary']}\n"
            f"Link: {top['url']}"
        )
        # Send digest via Telegram directly
        import json, urllib.request
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id   = os.getenv("TELEGRAM_CHAT_ID")
        payload = json.dumps({"chat_id": chat_id, "text": full_msg}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f"[orchestrator] Morning digest sent to Telegram")
        except Exception as e:
            print(f"[orchestrator] Digest send error: {e}")

    # Mark all overnight leads as notified
    for row in rows:
        post_tracker.mark_notified(row["post_id"], row["platform"])


if __name__ == "__main__":
    loop_mode     = "--loop" in sys.argv
    last_digest   = None

    if loop_mode:
        print("[orchestrator] Running in loop mode (Ctrl+C to stop)")
        while True:
            try:
                # Send morning digest once per day at 7am
                now_ct = datetime.now(CT_TZ)
                if now_ct.hour == 7 and (last_digest is None or last_digest.date() < now_ct.date()):
                    send_morning_digest()
                    last_digest = now_ct

                run_cycle()
                print(f"[orchestrator] Sleeping {POLL_INTERVAL}s until next cycle...")
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print("\n[orchestrator] Stopped.")
                break
            except Exception as e:
                print(f"[orchestrator] Unexpected error: {e}")
                time.sleep(30)
    else:
        run_cycle()
