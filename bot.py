"""
Telegram bot that forwards tweets from @David_Ornstein to a Telegram group.

Designed to run as a single invocation (systemd timer on the droplet, or
GitHub Actions cron as fallback). Uses RSS feeds — no Twitter API keys needed.
Persists seen-tweet fingerprints and operational state in a local JSON file.

Resilience model (Jul 2026):
- Feed instance list is refreshed from the community nitter health tracker
  (status.d420.de) every few hours, cached in state; static list as fallback.
- ALL sources are queried in parallel and their valid tweets MERGED (dedup by
  status ID) — no single feed is a point of failure or freshness bottleneck.
- If no source returns a rich feed for ALERT_AFTER_HOURS, the bot DMs the
  owner (TELEGRAM_ALERT_CHAT_ID) — silence must not look like health.
"""

import os
import re
import json
import time
import logging
import hashlib
import calendar
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import feedparser
import requests

# ── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# Optional: owner's private chat with the bot, for outage alerts (NOT the
# group). If unset, alerts only appear in the logs.
TELEGRAM_ALERT_CHAT_ID = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "David_Ornstein")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Community-run health tracker for nitter instances. We pull healthy
# RSS-capable instances from here so the bot discovers replacements itself
# when instances die. Fail-soft: cached list, then static list.
INSTANCE_TRACKER_API = "https://status.d420.de/api/v1/instances"
INSTANCE_REFRESH_HOURS = 6
MAX_DYNAMIC_INSTANCES = 6

# Static fallbacks, used alongside whatever the tracker provides.
STATIC_FEEDS = [
    f"https://nitter.net/{TWITTER_USERNAME}/rss",
    f"https://xcancel.com/{TWITTER_USERNAME}/rss",
    f"https://nitter.privacyredirect.com/{TWITTER_USERNAME}/rss",
    # RSSHub mirrors — mostly dead as of Jul 2026, kept as cheap extra chances
    f"https://rsshub.app/twitter/user/{TWITTER_USERNAME}",
    # Thin scraper fallback; its junk entries are filtered by is_tweet_entry()
    f"https://rss.diffbot.com/rss?url=https://x.com/{TWITTER_USERNAME}",
]

# Never post tweets older than this. Protects against a burst of stale posts
# when a rich feed comes back after an outage or when the source switches
# (older entries are silently marked as seen instead).
MAX_TWEET_AGE_HOURS = 24

# A source counts as "rich" when it returns at least this many valid tweets.
# If NO source is rich for ALERT_AFTER_HOURS, the owner gets a Telegram DM
# (re-sent at most once per REALERT_HOURS while the outage lasts).
RICH_FEED_MIN_TWEETS = 5
ALERT_AFTER_HOURS = 2
REALERT_HOURS = 24

# Feeds that respond slower than this are skipped so one dead host can't stall
# the whole run. Fetches run in parallel, so this bounds the whole fetch step.
FEED_TIMEOUT = 12  # seconds

# Independent watchdog: a third-party Telegram channel that mirrors the tweets
# as text (no links, and slower than our feeds — measured +6 to +49 min).
# NEVER used for posting — only to detect the "feeds are rich but STALE"
# failure mode that source alerting can't see: if the mirror shows activity
# meaningfully newer than the newest tweet our feeds have surfaced, DM the
# owner. Empty env disables the watchdog.
MIRROR_CHANNEL = os.getenv("MIRROR_CHANNEL", "David_Ornstein")
WATCHDOG_GAP_MINUTES = 45  # > the mirror's own worst observed lag
TWITTER_EPOCH_MS = 1288834974657  # snowflake ID → timestamp

USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; OrnsteinBot/1.0)"}
BROWSER_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 Chrome/126.0 Safari/537.36")}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── State persistence ───────────────────────────────────────────────────────

def load_state() -> dict:
    """State is a dict; older state files that only had 'seen' still load."""
    path = Path(STATE_FILE)
    state = json.loads(path.read_text()) if path.exists() else {}
    state.setdefault("seen", [])
    state.setdefault("instances", [])
    state.setdefault("instances_fetched_at", 0)
    state.setdefault("last_rich_fetch", 0)
    state.setdefault("alert_active", False)
    state.setdefault("last_alert", 0)
    state.setdefault("watchdog_last_alert", 0)
    return state


def save_state(state: dict, seen: set):
    state["seen"] = list(seen)[-500:]
    Path(STATE_FILE).write_text(json.dumps(state))


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
    never be posted and must never influence source selection.
    (Incident: 3 junk links posted to the group on Jul 13 2026.)"""
    return bool(re.search(r"/status/\d+", entry.get("link", "") or ""))


# ── Feed discovery ──────────────────────────────────────────────────────────

def refresh_instances(state: dict):
    """Refresh the nitter instance pool from the community health tracker.
    Fail-soft: on any problem we keep the cached list (and always merge with
    STATIC_FEEDS later, so an empty/poisoned tracker can't blind the bot)."""
    fresh_for = time.time() - state["instances_fetched_at"]
    if state["instances"] and fresh_for < INSTANCE_REFRESH_HOURS * 3600:
        return
    try:
        resp = requests.get(INSTANCE_TRACKER_API, timeout=10, headers=USER_AGENT)
        resp.raise_for_status()
        hosts = resp.json().get("hosts", [])
        # Healthy instances regardless of the rss flag: instances without RSS
        # still serve scrapeable HTML timelines (we consume both).
        good = [
            h["url"].rstrip("/")
            for h in sorted(hosts, key=lambda h: h.get("points") or 0, reverse=True)
            if h.get("healthy") and not h.get("is_bad_host")
        ]
        if good:
            state["instances"] = good[:MAX_DYNAMIC_INSTANCES]
            state["instances_fetched_at"] = time.time()
            log.info(f"Instance pool refreshed from tracker: {state['instances']}")
        else:
            log.warning("Tracker returned no healthy RSS instances; keeping cache.")
    except Exception as e:
        log.info(f"Instance tracker unavailable ({type(e).__name__}); "
                 f"using cached/static list.")


def build_sources(state: dict) -> list:
    """(kind, url) pairs: every instance is tried BOTH as RSS and as an HTML
    timeline (instances without RSS still serve scrapeable HTML — that is
    what caught the Jul 15 2026 stale-nitter.net incident). Dynamic
    instances first, then static fallbacks, deduped."""
    sources = []
    for inst in state["instances"]:
        sources.append(("rss", f"{inst}/{TWITTER_USERNAME}/rss"))
        sources.append(("html", f"{inst}/{TWITTER_USERNAME}"))
    for u in STATIC_FEEDS:
        if ("rss", u) not in sources:
            sources.append(("rss", u))
    return sources


# ── RSS fetching ────────────────────────────────────────────────────────────

def parse_nitter_html(content: str) -> list:
    """Extract timeline tweets from a nitter instance's profile HTML page.
    Only `tweet-link` anchors (the timeline items' own permalinks) count —
    `quote-link` anchors are embedded QUOTED tweets and must be excluded.
    Timestamps derive from the snowflake ID. Returns feedparser-like dicts."""
    entries = []
    seen_ids = set()
    for user, sid in re.findall(
        r'class="tweet-link" href="/([A-Za-z0-9_]+)/status/(\d+)', content
    ):
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        ts = ((int(sid) >> 22) + TWITTER_EPOCH_MS) / 1000
        entries.append({
            "id": sid,
            "link": f"https://x.com/{user}/status/{sid}",
            "published_parsed": time.gmtime(ts),
        })
    return entries


def fetch_one(source: tuple):
    """Fetch one (kind, url) source; returns (status_line, valid_tweet_entries)."""
    kind, url = source
    try:
        if kind == "html":
            resp = requests.get(url, timeout=FEED_TIMEOUT, headers=BROWSER_UA)
            if not resp.ok:
                return f"HTTP {resp.status_code}, skipping", []
            tweets = parse_nitter_html(resp.text)
            return f"{len(tweets)} timeline tweets", tweets
        resp = requests.get(url, timeout=FEED_TIMEOUT, headers=USER_AGENT)
        if not resp.ok:
            return f"HTTP {resp.status_code}, skipping", []
        feed = feedparser.parse(resp.content)
        tweets = [e for e in feed.entries if is_tweet_entry(e)]
        if len(tweets) != len(feed.entries):
            return (f"{len(feed.entries)} entries "
                    f"({len(tweets)} valid tweets, rest discarded)", tweets)
        return f"{len(tweets)} entries", tweets
    except Exception as e:
        return f"failed ({type(e).__name__}), skipping", []


def fetch_all_feeds(sources: list) -> tuple[dict, bool]:
    """Query every source in parallel and MERGE their valid tweets, deduped
    by fingerprint. Merging (vs picking one winner) means a single stale or
    blipping source can't hide a tweet another source already has.

    Returns (fingerprint -> entry map, whether any source was rich)."""
    merged: dict = {}
    any_rich = False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_one, sources))
    for (kind, url), (status_line, tweets) in zip(sources, results):
        log.info(f"  [{kind}] {url} → {status_line}")
        if len(tweets) >= RICH_FEED_MIN_TWEETS:
            any_rich = True
        for entry in tweets:
            merged.setdefault(fingerprint(entry), entry)
    log.info(f"Merged {len(merged)} unique tweets "
             f"from {sum(1 for _, t in results if t)} live source(s).")
    return merged, any_rich


# ── Telegram ────────────────────────────────────────────────────────────────

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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": embed_url}
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        log.error(f"Telegram API error: {resp.status_code} {resp.text}")
    else:
        log.info(f"Posted to Telegram: {embed_url}")


def send_owner_alert(text: str):
    """DM the owner (never the group). Logs-only if no alert chat configured."""
    if not TELEGRAM_ALERT_CHAT_ID:
        log.warning(f"ALERT (set TELEGRAM_ALERT_CHAT_ID for DMs): {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url, json={"chat_id": TELEGRAM_ALERT_CHAT_ID, "text": text}, timeout=15
    )
    if not resp.ok:
        log.error(f"Alert DM failed: {resp.status_code} {resp.text}")


def newest_seen_tweet_time(seen: set) -> float | None:
    """Unix time of the newest tweet we've seen, derived from the largest
    numeric fingerprint (status IDs are snowflakes: time-ordered, and they
    embed their creation timestamp)."""
    ids = [int(fp) for fp in seen if fp.isdigit()]
    if not ids:
        return None
    return ((max(ids) >> 22) + TWITTER_EPOCH_MS) / 1000


def check_mirror_watchdog(state: dict, seen: set):
    """Compare the mirror channel's newest post time against the newest tweet
    our feeds have surfaced. A large gap means the feeds are likely serving
    stale data (or the mirror posted an ad — hence daily rate limit)."""
    if not MIRROR_CHANNEL:
        return
    try:
        resp = requests.get(f"https://t.me/s/{MIRROR_CHANNEL}",
                            timeout=10, headers=BROWSER_UA)
        if not resp.ok:
            log.info(f"Mirror watchdog: t.me/s/{MIRROR_CHANNEL} → HTTP {resp.status_code}")
            return
        stamps = re.findall(r'datetime="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', resp.text)
        ours = newest_seen_tweet_time(seen)
        if not stamps or ours is None:
            return
        mirror_newest = max(
            calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")) for s in stamps
        )
        gap_min = (mirror_newest - ours) / 60
        if gap_min <= WATCHDOG_GAP_MINUTES:
            return
        if time.time() - state["watchdog_last_alert"] < REALERT_HOURS * 3600:
            return
        state["watchdog_last_alert"] = time.time()
        send_owner_alert(
            f"⚠️ ornstein-bot watchdog: the t.me/{MIRROR_CHANNEL} mirror has a "
            f"post ~{gap_min:.0f} min newer than the newest tweet our feeds "
            f"show. Feeds may be serving stale data (or the mirror posted an "
            f"ad). Check https://x.com/{TWITTER_USERNAME} to compare."
        )
    except Exception as e:
        log.info(f"Mirror watchdog skipped ({type(e).__name__}).")


def check_feed_health(state: dict, any_rich: bool):
    """Track when we last saw a rich feed; DM the owner if it's been too long.
    Sends a recovery DM when sources come back."""
    now = time.time()
    if any_rich:
        state["last_rich_fetch"] = now
        if state["alert_active"]:
            state["alert_active"] = False
            send_owner_alert("✅ ornstein-bot: tweet sources recovered.")
        return
    if not state["last_rich_fetch"]:
        # Fresh state: start the clock now rather than alerting immediately.
        state["last_rich_fetch"] = now
        return
    hours_blind = (now - state["last_rich_fetch"]) / 3600
    realert_due = (now - state["last_alert"]) > REALERT_HOURS * 3600
    if hours_blind > ALERT_AFTER_HOURS and (not state["alert_active"] or realert_due):
        state["alert_active"] = True
        state["last_alert"] = now
        send_owner_alert(
            f"⚠️ ornstein-bot: no rich tweet feed for {hours_blind:.1f}h — "
            f"all sources may be dead. Check `python check_feeds.py` on the "
            f"droplet, and https://status.d420.de/ for fresh instances."
        )


# ── Single run ─────────────────────────────────────────────────────────────

def main():
    log.info(f"Checking @{TWITTER_USERNAME} for new tweets…")

    state = load_state()
    seen = set(state["seen"])
    first_run = len(seen) == 0

    refresh_instances(state)
    merged, any_rich = fetch_all_feeds(build_sources(state))
    check_feed_health(state, any_rich)

    if not merged:
        log.warning("Nothing to process. Exiting.")
        if seen:
            check_mirror_watchdog(state, seen)
        save_state(state, seen)
        return

    # Oldest-first so Telegram messages arrive in chronological order.
    # Status IDs are snowflakes (time-ordered), so sorting by ID is exact.
    new_entries = []
    for fp in sorted(merged, key=lambda f: int(f) if f.isdigit() else 0):
        if fp not in seen:
            seen.add(fp)
            new_entries.append(merged[fp])

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
        check_mirror_watchdog(state, seen)

    save_state(state, seen)


if __name__ == "__main__":
    main()
