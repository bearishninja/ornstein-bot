"""
Telegram bot that forwards tweets from @David_Ornstein to a Telegram group.

Designed to run as a single invocation (e.g. via GitHub Actions cron).
Uses RSS feeds — no Twitter API keys needed.
Persists seen-tweet fingerprints in a local JSON file (cached between runs).
"""

import os
import json
import time
import logging
import hashlib
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
    # RSSHub mirrors — these typically return ~20 recent tweets (best case)
    f"https://rsshub.app/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.rssforever.com/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.pseudoyu.com/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.ktachibana.party/twitter/user/{TWITTER_USERNAME}",
    f"https://rss.shab.fun/twitter/user/{TWITTER_USERNAME}",
    f"https://hub.slarker.me/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.woodland.cafe/twitter/user/{TWITTER_USERNAME}",
    # Nitter-based mirror (also multi-entry when up)
    f"https://nitter.privacyredirect.com/{TWITTER_USERNAME}/rss",
    # Thin fallback — usually only the latest 1 tweet. Last resort only.
    f"https://rss.diffbot.com/rss?url=https://x.com/{TWITTER_USERNAME}",
]

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
    raw = entry.get("id", "") or entry.get("link", "") or entry.get("title", "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


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
            count = len(feed.entries)
            log.info(f"  {url} → {count} entries")
            if count > len(best_entries):
                best_entries = feed.entries
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
        for entry in new_entries:
            link = entry.get("link", "")
            if link:
                send_telegram(link)
                sent += 1
                time.sleep(1)
            else:
                log.warning(f"Skipping entry with no link: {entry.get('title', '?')}")
        log.info(f"Done — forwarded {sent} new tweet(s).")

    save_state(seen)


if __name__ == "__main__":
    main()
