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


def feed_sources() -> list:
    """(kind, url) pairs mirroring bot.py: each instance tried as RSS and as
    an HTML timeline."""
    sources = []
    path = Path(STATE_FILE)
    if path.exists():
        try:
            instances = json.loads(path.read_text()).get("instances", [])
            for inst in instances:
                sources.append(("rss", f"{inst}/{TWITTER_USERNAME}/rss"))
                sources.append(("html", f"{inst}/{TWITTER_USERNAME}"))
            if sources:
                print(f"(including {len(instances)} tracker-discovered "
                      f"instances from {STATE_FILE})\n")
        except Exception:
            pass
    for u in STATIC_FEEDS:
        if ("rss", u) not in sources:
            sources.append(("rss", u))
    return sources


BROWSER_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 Chrome/126.0 Safari/537.36")}
BOT_UA = {"User-Agent": "Mozilla/5.0 (compatible; OrnsteinBot/1.0)"}


def newest_id(ids):
    return max((int(i) for i in ids), default=None)


def main():
    print(f"Checking feeds for @{TWITTER_USERNAME}...\n")
    freshest = (None, None)  # (status_id, source)

    for kind, url in feed_sources():
        try:
            headers = BROWSER_UA if kind == "html" else BOT_UA
            resp = requests.get(url, timeout=FEED_TIMEOUT, headers=headers)
            if not resp.ok:
                print(f"  DOWN      HTTP {resp.status_code}          [{kind}] {url}")
                continue
            if kind == "html":
                ids = [s for _, s in re.findall(
                    r'class="tweet-link" href="/([A-Za-z0-9_]+)/status/(\d+)',
                    resp.text)]
                note = ""
            else:
                entries = feedparser.parse(resp.content).entries
                valid = [e for e in entries
                         if re.search(r"/status/\d+", e.get("link", "") or "")]
                ids = [m.group(1) for e in valid
                       if (m := re.search(r"/status/(\d+)", e.get("link", "")))]
                note = "" if len(valid) == len(entries) else \
                    f"  ({len(entries) - len(valid)} junk discarded)"
            label = "OK" if ids else "EMPTY"
            print(f"  {label:<9} {len(ids):3d} tweets        [{kind}] {url}{note}")
            top = newest_id(ids)
            if top and (freshest[0] is None or top > freshest[0]):
                freshest = (top, f"[{kind}] {url}")
        except Exception as e:
            print(f"  FAIL      {type(e).__name__:<20} [{kind}] {url}")

    print()
    if freshest[0]:
        print(f"Freshest source right now: {freshest[1]}")
        print(f"  newest status id: {freshest[0]}")
        print("(bot.py merges ALL live sources, so freshness wins overall)")
    else:
        print("No source is returning tweets — the bot is blind right now.")


if __name__ == "__main__":
    main()
