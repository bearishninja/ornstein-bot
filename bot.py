"""
Telegram bot that forwards tweets from @David_Ornstein to a Telegram group.

Designed to run as a single invocation (e.g. via GitHub Actions cron).
Uses RSS feeds — no Twitter API keys needed.
Persists seen-tweet fingerprints in a local JSON file (cached between runs).
"""

import os
import re
import json
import time
import logging
import hashlib
import calendar
from pathlib import Path

import feedparser
import requests

# ── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "David_Ornstein")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# RSS feed sources to try. We query ALL of them and use whichever returns the
# MOST entries — a full-batch feed (~20 tweets) protects against missing a
# burst of tweets between polls, whereas a thin 1-entry feed would drop them.
# RSSHub public instances rotate and go up/down, so we list several mirrors.
RSS_FEEDS = [
    # Nitter instances — return ~20 recent tweets when reachable. Some block
    # datacenter IPs (GitHub runners), so results vary by where this runs.
    f"https://nitter.net/{TWITTER_USERNAME}/rss",
    f"https://xcancel.com/{TWITTER_USERNAME}/rss",
    f"https://nitter.tiekoetter.com/{TWITTER_USERNAME}/rss",
    f"https://nitter.privacyredirect.com/{TWITTER_USERNAME}/rss",
    # RSSHub mirrors — mostly dead as of Jul 2026, kept as cheap extra chances
    f"https://rsshub.app/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.rssforever.com/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.pseudoyu.com/twitter/user/{TWITTER_USERNAME}",
    # Thin fallback — usually only the latest 1 tweet. Last resort only.
    f"https://rss.diffbot.com/rss?url=https://x.com/{TWITTER_USERNAME}",
]

# Never post tweets older than this. Protects against a burst of stale posts
# when a rich feed comes back after an outage or when the source switches
# (older entries are silently marked as seen instead).
MAX_TWEET_AGE_HOURS = 24

# Feeds that respond slower than this are skipped so one dead host can't stall
# the whole run. GitHub Actions gives us plenty of headroom, but keep it tidy.
FEED_TIMEOUT = 12  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── State persistence ───────────────────────────────────────────────────────

def load_state() -> set:
    path = Path(STATE_FILE)
    if path.exists():
        data = json.loads(path.read_text())
        return set(data.get("seen", []))
    return set()


def save_state(seen: set):
    recent = list(seen)[-500:]
    Path(STATE_FILE).write_text(json.dumps({"seen": recent}))


def fingerprint(entry) -> str:
    """Identify a tweet by its numeric status ID so the same tweet dedupes
    identically no matter which feed source it came from (nitter, rsshub,
    diffbot all embed /status/<id> in their links). Falls back to hashing
    the raw id/link/title if no status ID is found."""
    raw = entry.get("id", "") or entry.get("link", "") or entry.get("title", "")
    if raw.isdigit():  # nitter guids are the bare numeric status ID
        return raw
    m = re.search(r"/status/(\d+)", raw)
    if m:
        return m.group(1)
    # id may be an opaque guid while the link still holds /status/<id>
    m = re.search(r"/status/(\d+)", entry.get("link", ""))
    if m:
        return m.group(1)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def entry_age_hours(entry) -> float | None:
    """Age of an entry in hours, or None if the feed gave no usable date."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return (time.time() - calendar.timegm(t)) / 3600


def is_tweet_entry(entry) -> bool:
    """Only entries whose link is an actual tweet permalink count. Scrapers
    (diffbot especially) sometimes return a login/landing page's furniture —
    help links, signup links, t.co redirects — as feed entries. Those must
    never be posted and must never win the most-entries feed contest.
    (Incident: 3 junk links posted to the group on Jul 13 2026.)"""
    return bool(re.search(r"/status/\d+", entry.get("link", "") or ""))


# ── RSS fetching ────────────────────────────────────────────────────────────

def fetch_feed() -> list:
    """Query every source and return the entries from whichever gave the MOST.

    Preferring the largest result set means a full-batch feed (~20 tweets)
    always wins over a thin 1-entry fallback, so a burst of tweets posted
    between polls won't be silently dropped.
    """
    best_entries: list = []
    best_url = None

    for url in RSS_FEEDS:
        try:
            resp = requests.get(
                url,
                timeout=FEED_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; OrnsteinBot/1.0)"},
            )
            if not resp.ok:
                log.info(f"  {url} → HTTP {resp.status_code}, skipping")
                continue
            feed = feedparser.parse(resp.content)
            tweets = [e for e in feed.entries if is_tweet_entry(e)]
            if len(tweets) != len(feed.entries):
                log.info(f"  {url} → {len(feed.entries)} entries "
                         f"({len(tweets)} valid tweets, rest discarded)")
            else:
                log.info(f"  {url} → {len(tweets)} entries")
            if len(tweets) > len(best_entries):
                best_entries = tweets
                best_url = url
        except Exception as e:
            log.info(f"  {url} → failed ({type(e).__name__}), skipping")

    if best_url:
        log.info(f"Using {len(best_entries)} entries from {best_url}")
    else:
        log.warning("No feed source returned any entries this cycle.")
    return best_entries


# ── Telegram posting ───────────────────────────────────────────────────────

def send_telegram(tweet_url: str):
    """Send a fixupx.com URL to the group. Telegram renders the full card
    (author, tweet text, images/video) automatically. Tapping the card
    redirects to the real x.com tweet."""
    # Canonicalize ANY source's link (x.com, nitter.net, xcancel.com, …) to
    # fixupx.com/<user>/status/<id>. Never post a raw mirror URL — only the
    # fixupx form renders the rich card (goal #1).
    m = re.search(r"https?://[^/]+/(\w+)/status/(\d+)", tweet_url)
    if m:
        embed_url = f"https://fixupx.com/{m.group(1)}/status/{m.group(2)}"
    else:
        embed_url = (
            tweet_url
            .replace("https://x.com/", "https://fixupx.com/")
            .replace("https://twitter.com/", "https://fixupx.com/")
            .replace("http://x.com/", "https://fixupx.com/")
            .replace("http://twitter.com/", "https://fixupx.com/")
        )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": embed_url,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        log.error(f"Telegram API error: {resp.status_code} {resp.text}")
    else:
        log.info(f"Posted to Telegram: {embed_url}")


# ── Single run ─────────────────────────────────────────────────────────────

def main():
    log.info(f"Checking @{TWITTER_USERNAME} for new tweets…")

    seen = load_state()
    first_run = len(seen) == 0

    entries = fetch_feed()

    if not entries:
        log.warning("Nothing to process. Exiting.")
        save_state(seen)
        return

    # Process oldest-first so Telegram messages arrive in chronological order
    new_entries = []
    for entry in reversed(entries):
        fp = fingerprint(entry)
        if fp not in seen:
            seen.add(fp)
            new_entries.append(entry)

    if first_run:
        log.info(f"First run — marked {len(new_entries)} existing tweets as seen (no spam).")
    else:
        sent = 0
        skipped_stale = 0
        for entry in new_entries:
            age = entry_age_hours(entry)
            if age is not None and age > MAX_TWEET_AGE_HOURS:
                skipped_stale += 1
                continue
            link = entry.get("link", "")
            if link:
                send_telegram(link)
                sent += 1
                time.sleep(1)
            else:
                log.warning(f"Skipping entry with no link: {entry.get('title', '?')}")
        if skipped_stale:
            log.info(f"Marked {skipped_stale} stale entry(ies) >"
                     f"{MAX_TWEET_AGE_HOURS}h old as seen without posting.")
        log.info(f"Done — forwarded {sent} new tweet(s).")

    save_state(seen)


if __name__ == "__main__":
    main()
