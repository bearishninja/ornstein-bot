"""
Standalone diagnostic: checks every RSS feed source bot.py can use and
reports how many entries each one returns right now.

Run anytime you want to see which mirrors are alive:
    python check_feeds.py

Does not touch state.json, does not post to Telegram, does not need any
env vars or secrets — just hits the feed URLs and reports what it finds.
"""

import os

import feedparser
import requests

TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "David_Ornstein")
FEED_TIMEOUT = 12  # seconds

RSS_FEEDS = [
    f"https://rsshub.app/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.rssforever.com/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.pseudoyu.com/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.ktachibana.party/twitter/user/{TWITTER_USERNAME}",
    f"https://rss.shab.fun/twitter/user/{TWITTER_USERNAME}",
    f"https://hub.slarker.me/twitter/user/{TWITTER_USERNAME}",
    f"https://rsshub.woodland.cafe/twitter/user/{TWITTER_USERNAME}",
    f"https://nitter.privacyredirect.com/{TWITTER_USERNAME}/rss",
    f"https://rss.diffbot.com/rss?url=https://x.com/{TWITTER_USERNAME}",
]


def main():
    print(f"Checking feeds for @{TWITTER_USERNAME}...\n")
    best_count = -1
    best_url = None

    for url in RSS_FEEDS:
        try:
            resp = requests.get(
                url,
                timeout=FEED_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; OrnsteinBot/1.0)"},
            )
            if not resp.ok:
                print(f"  DOWN      HTTP {resp.status_code}          {url}")
                continue
            count = len(feedparser.parse(resp.content).entries)
            print(f"  {'OK' if count else 'EMPTY':<9} {count:3d} entries        {url}")
            if count > best_count:
                best_count = count
                best_url = url
        except Exception as e:
            print(f"  FAIL      {type(e).__name__:<20} {url}")

    print()
    if best_url:
        print(f"bot.py would currently use: {best_url} ({best_count} entries)")
    else:
        print("bot.py would currently find nothing — all feeds are down.")


if __name__ == "__main__":
    main()
