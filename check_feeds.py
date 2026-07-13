"""
Standalone diagnostic: checks every RSS feed source bot.py can use and
reports how many entries each one returns right now.

Run anytime you want to see which mirrors are alive:
    python check_feeds.py

Mirrors bot.py's source list: the dynamic instance pool cached in state.json
(if present) plus the static fallbacks. Does not touch state, does not post
to Telegram, needs no env vars or secrets.
"""

import os
import re
import json
from pathlib import Path

import feedparser
import requests

TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "David_Ornstein")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
FEED_TIMEOUT = 12  # seconds

STATIC_FEEDS = [
    f"https://nitter.net/{TWITTER_USERNAME}/rss",
    f"https://xcancel.com/{TWITTER_USERNAME}/rss",
    f"https://nitter.privacyredirect.com/{TWITTER_USERNAME}/rss",
    f"https://rsshub.app/twitter/user/{TWITTER_USERNAME}",
    f"https://rss.diffbot.com/rss?url=https://x.com/{TWITTER_USERNAME}",
]


def feed_urls() -> list:
    urls = []
    path = Path(STATE_FILE)
    if path.exists():
        try:
            instances = json.loads(path.read_text()).get("instances", [])
            urls = [f"{inst}/{TWITTER_USERNAME}/rss" for inst in instances]
            if urls:
                print(f"(including {len(urls)} tracker-discovered instances "
                      f"from {STATE_FILE})\n")
        except Exception:
            pass
    for u in STATIC_FEEDS:
        if u not in urls:
            urls.append(u)
    return urls


def main():
    print(f"Checking feeds for @{TWITTER_USERNAME}...\n")
    best_count = -1
    best_url = None

    for url in feed_urls():
        try:
            resp = requests.get(
                url,
                timeout=FEED_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; OrnsteinBot/1.0)"},
            )
            if not resp.ok:
                print(f"  DOWN      HTTP {resp.status_code}          {url}")
                continue
            entries = feedparser.parse(resp.content).entries
            valid = [e for e in entries
                     if re.search(r"/status/\d+", e.get("link", "") or "")]
            label = "OK" if valid else "EMPTY"
            note = "" if len(valid) == len(entries) else \
                f"  ({len(entries) - len(valid)} junk discarded)"
            print(f"  {label:<9} {len(valid):3d} tweets        {url}{note}")
            if len(valid) > best_count:
                best_count = len(valid)
                best_url = url
        except Exception as e:
            print(f"  FAIL      {type(e).__name__:<20} {url}")

    print()
    if best_url and best_count > 0:
        print(f"Richest source right now: {best_url} ({best_count} tweets)")
        print("(bot.py merges ALL live sources, not just the richest)")
    else:
        print("No source is returning tweets — the bot is blind right now.")


if __name__ == "__main__":
    main()
