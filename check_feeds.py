"""
Standalone diagnostic: probes every source bot.py can use and reports which
are alive, how many tweets each returns, and which is freshest.

    python check_feeds.py

Imports the source list and fetchers from bot.py so there is ONE definition of
where tweets come from (this file used to keep its own copy, which drifted).
Touches no state, posts nothing, needs no secrets.
"""

import json
import re
from pathlib import Path

from bot import (
    STATE_FILE,
    TWITTER_USERNAME,
    build_sources,
    fetch_one,
    fingerprint,
    source_key,
)

TWITTER_EPOCH_MS = 1288834974657


def load_pool() -> dict:
    """Minimal state stand-in: the cached instance pool and proven set, so this
    reports on exactly the sources the bot would use."""
    path = Path(STATE_FILE)
    state = {"instances": [], "proven": []}
    if path.exists():
        try:
            saved = json.loads(path.read_text())
            state["instances"] = saved.get("instances", [])
            state["proven"] = saved.get("proven", [])
        except Exception:
            pass
    return state


def tweet_time(sid: str) -> str:
    import datetime
    ts = ((int(sid) >> 22) + TWITTER_EPOCH_MS) / 1000
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%b %d %H:%M UTC")


def main():
    state = load_pool()
    sources = build_sources(state)
    print(f"Checking {len(sources)} sources for @{TWITTER_USERNAME}"
          f" ({len(state['instances'])} from the cached tracker pool)\n")

    freshest = (None, None)
    for source in sources:
        kind, url = source
        status, tweets = fetch_one(source)
        star = " *proven*" if source_key(source) in state["proven"] else ""
        label = "OK   " if tweets else "     "
        print(f"  {label} [{kind:4}] {url}\n         → {status}{star}")
        for e in tweets:
            fp = fingerprint(e)
            if fp.isdigit() and (freshest[0] is None or int(fp) > int(freshest[0])):
                freshest = (fp, f"[{kind}] {url}")

    print()
    if freshest[0]:
        print(f"Freshest tweet seen: {tweet_time(freshest[0])} "
              f"(id {freshest[0]})\n  via {freshest[1]}")
        print("bot.py merges ALL responding sources, so freshness wins overall.")
    else:
        print("No source returned a tweet — the bot is blind right now.")


if __name__ == "__main__":
    main()
