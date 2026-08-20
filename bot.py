"""
Telegram bot that forwards tweets from @David_Ornstein to a Telegram group.

Designed to run as a single invocation (systemd timer on the droplet, or
GitHub Actions cron as fallback). Uses RSS feeds — no Twitter API keys needed.
Persists seen-tweet fingerprints and operational state in a local JSON file.

Resilience model:
- Feed instance list is refreshed hourly from the community nitter health
  tracker (status.d420.de), cached in state; static list always appended.
- ALL sources are queried in parallel, each as RSS *and* as an HTML timeline,
  and their valid tweets MERGED (dedup by status ID) — no single feed is a
  point of failure or a freshness bottleneck.
- Replies are filtered out; only scoops and retweets reach the group.
- If no source returns a rich feed for ALERT_AFTER_HOURS, the bot DMs the
  owner (TELEGRAM_ALERT_CHAT_ID) — silence must not look like health.
- Every completed run pings HEALTHCHECK_URL, so an external service notices
  if this box/timer/script dies.
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

# Read with getenv (not os.environ[...]) so this module can be imported by
# check_feeds.py without secrets present. main() validates them and exits
# with a clear message if they are missing.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Optional: owner's private chat with the bot, for outage alerts (NOT the
# group). If unset, alerts only appear in the logs.
TELEGRAM_ALERT_CHAT_ID = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")
# Optional: healthchecks.io ping URL. Pinged after every completed run so an
# EXTERNAL service notices if this box/timer/script dies — the one failure
# class our own Telegram alerts cannot report (they need the bot alive).
# Treat as a secret. If unset, heartbeats are skipped entirely.
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "")
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "David_Ornstein")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Community-run health tracker for nitter instances. We pull healthy
# RSS-capable instances from here so the bot discovers replacements itself
# when instances die. Fail-soft: cached list, then static list.
INSTANCE_TRACKER_API = "https://status.d420.de/api/v1/instances"
# Hourly (not 6h): a 6h-old pool snapshot excluded the one fresh instance
# for hours on Jul 16 2026 while every pooled instance was stale/blind.
INSTANCE_REFRESH_HOURS = 1
# No top-N cutoff — take every healthy instance. ~2 req/min per instance
# (rss+html) is polite, and the excluded instance is always the one you need.

# Static fallbacks, used alongside whatever the tracker provides.
# Dropped Aug 20 2026 after measuring 60 consecutive cycles: rsshub.app
# (permanent 404) and rss.diffbot.com (junk/timeouts, never once a valid
# tweet — and the source that put 3 junk links in the group on Jul 13) never
# contributed anything. Do not re-add without evidence they work.
STATIC_FEEDS = [
    f"https://nitter.net/{TWITTER_USERNAME}/rss",
    f"https://xcancel.com/{TWITTER_USERNAME}/rss",
    f"https://nitter.privacyredirect.com/{TWITTER_USERNAME}/rss",
]

# Two-tier probing (Aug 20 2026). Measured: 20 probes/cycle, only 2 ever
# yielded tweets — ~26k useless requests/day, mostly to volunteer instances
# that 403 this droplet's datacenter IP. Normal cycles now probe only sources
# proven to work; the full list is swept this often to rediscover changes.
# Discovery still matters (kareem.one was the only fresh source on Jul 16),
# it just does not need to happen every single minute.
SWEEP_MINUTES = 30

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
    state.setdefault("proven", [])       # "kind|url" of sources that yielded
    state.setdefault("last_sweep", 0)
    # Inert leftovers from the retired mirror watchdog — drop on next write.
    state.pop("watchdog_last_alert", None)
    state.pop("watchdog_stale_since", None)
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
            changed = good != state["instances"]
            state["instances"] = good
            state["instances_fetched_at"] = time.time()
            if changed:
                # New/removed instances must be probed before they can become
                # "proven", so force a full sweep on this cycle.
                state["last_sweep"] = 0
                log.info(f"Instance pool refreshed from tracker: {good}")
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


def source_key(source: tuple) -> str:
    return f"{source[0]}|{source[1]}"


def select_sources(state: dict, all_sources: list) -> tuple[list, bool]:
    """Two-tier probing: normally probe only sources that produced tweets on
    the last sweep, which is ~2-4 requests instead of ~20. Sweep the full list
    every SWEEP_MINUTES (and whenever `proven` is empty or the instance pool
    changed) so a newly-working source gets discovered.

    Returns (sources_to_probe, is_full_sweep)."""
    due = (time.time() - state["last_sweep"]) > SWEEP_MINUTES * 60
    if not state["proven"] or due:
        return all_sources, True
    proven = [s for s in all_sources if source_key(s) in state["proven"]]
    if not proven:               # pool rotated away from everything proven
        return all_sources, True
    return proven, False


# ── RSS fetching ────────────────────────────────────────────────────────────

def parse_nitter_html(content: str) -> list:
    """Extract timeline tweets from a nitter instance's profile HTML page.
    Only `tweet-link` anchors (the timeline items' own permalinks) count —
    `quote-link` anchors are embedded QUOTED tweets and must be excluded.
    Timestamps derive from the snowflake ID. Returns feedparser-like dicts.

    Items carrying a `replying-to` marker are flagged is_reply. NOTE: nitter
    HTML only marks replies to OTHERS — self-thread replies look like normal
    tweets here; those are caught by the RSS title flag and the fxtwitter
    check in main()."""
    entries = []
    seen_ids = set()
    for item in re.split(r'class="timeline-item', content)[1:]:
        m = re.search(r'class="tweet-link" href="/([A-Za-z0-9_]+)/status/(\d+)', item)
        if not m:
            continue
        user, sid = m.group(1), m.group(2)
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        ts = ((int(sid) >> 22) + TWITTER_EPOCH_MS) / 1000
        entries.append({
            "id": sid,
            "link": f"https://x.com/{user}/status/{sid}",
            "published_parsed": time.gmtime(ts),
            "is_reply": 'class="replying-to"' in item or "replying-to" in item,
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
        for e in tweets:
            # Nitter RSS titles replies "R to @user: …" — covers BOTH replies
            # to others and self-thread replies (which HTML cannot see).
            e["is_reply"] = (e.get("title") or "").startswith("R to ")
        if len(tweets) != len(feed.entries):
            return (f"{len(feed.entries)} entries "
                    f"({len(tweets)} valid tweets, rest discarded)", tweets)
        return f"{len(tweets)} entries", tweets
    except Exception as e:
        return f"failed ({type(e).__name__}), skipping", []


def fetch_all_feeds(sources: list, verbose: bool = True) -> tuple[dict, bool, list]:
    """Query the given sources in parallel and MERGE their valid tweets, deduped
    by fingerprint. Merging (vs picking one winner) means a single stale or
    blipping source can't hide a tweet another source already has.

    `verbose` controls per-source logging: full detail on sweeps, but on a
    normal cycle only sources that STOPPED yielding are worth a line. Logging
    every source every minute produced ~43k journal lines/day and capped
    history at ~6 days.

    Returns (fingerprint -> entry map, any source was rich, productive keys)."""
    merged: dict = {}
    any_rich = False
    productive: list = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_one, sources))
    for source, (status_line, tweets) in zip(sources, results):
        kind, url = source
        if verbose or not tweets:
            log.info(f"  [{kind}] {url} → {status_line}")
        if tweets:
            productive.append(source_key(source))
        if len(tweets) >= RICH_FEED_MIN_TWEETS:
            any_rich = True
        for entry in tweets:
            fp = fingerprint(entry)
            if fp in merged:
                # A reply flag from ANY source sticks — an HTML source that
                # can't see self-replies must not launder the RSS flag away.
                if entry.get("is_reply") and not merged[fp].get("is_reply"):
                    merged[fp]["is_reply"] = True
            else:
                merged[fp] = entry
    log.info(f"Merged {len(merged)} unique tweets from {len(productive)}/"
             f"{len(sources)} source(s){' [full sweep]' if verbose else ''}.")
    return merged, any_rich, productive


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


def ping_heartbeat():
    """Phone home to healthchecks.io: "a run just completed". That service
    alerts the owner on SILENCE (no ping within period+grace), which is what
    makes it survive this box dying — it needs nothing from us to fire.

    Deliberately NOT tied to feed health: dead feeds are already covered by
    check_feed_health(), and conflating them would turn a feed outage into a
    false "box is down" email. This says only: the script ran to completion
    on a live box.

    Fail-soft: a failed ping is logged and ignored, never affects posting."""
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=8, headers=USER_AGENT)
    except Exception as e:
        log.info(f"Heartbeat ping skipped ({type(e).__name__}).")


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
    else:
        # Log every DM sent — otherwise alert noise is invisible in the journal
        # and impossible to audit after the fact.
        log.info(f"Owner alert DM sent: {text[:90]}")


def is_reply_via_api(status_id: str) -> bool:
    """Last-line reply check against the fxtwitter API (same FixTweet project
    as our card renderer). Only called for tweets about to be POSTED, so at
    most a couple of calls per real event. Fail-OPEN: if the API is down we
    post anyway — missing a scoop is worse than a rare stray reply."""
    try:
        resp = requests.get(f"https://api.fxtwitter.com/status/{status_id}",
                            timeout=8, headers=USER_AGENT)
        if not resp.ok:
            return False
        return bool(resp.json().get("tweet", {}).get("replying_to"))
    except Exception:
        return False


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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
                         "(see /opt/ornstein-bot/.env on the droplet).")

    log.info(f"Checking @{TWITTER_USERNAME} for new tweets…")

    state = load_state()
    seen = set(state["seen"])
    first_run = len(seen) == 0

    refresh_instances(state)
    all_sources = build_sources(state)
    to_probe, sweep = select_sources(state, all_sources)
    merged, any_rich, productive = fetch_all_feeds(to_probe, verbose=sweep)

    # Self-heal: a proven-only cycle that found nothing means a workhorse just
    # died. Sweep everything NOW rather than waiting up to SWEEP_MINUTES.
    if not merged and not sweep:
        log.info("Proven sources yielded nothing — sweeping all sources now.")
        merged, any_rich, productive = fetch_all_feeds(all_sources, verbose=True)
        sweep = True

    if sweep:
        state["proven"] = productive
        state["last_sweep"] = time.time()

    check_feed_health(state, any_rich)

    if not merged:
        log.warning("Nothing to process. Exiting.")
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
        skipped_replies = 0
        for entry in new_entries:
            age = entry_age_hours(entry)
            if age is not None and age > MAX_TWEET_AGE_HOURS:
                skipped_stale += 1
                continue
            link = entry.get("link", "")
            if not link:
                log.warning(f"Skipping entry with no link: {entry.get('title', '?')}")
                continue
            # The group wants scoops and RTs, not reply-thread chatter
            # (incident Jul 21 2026: two credit-replies posted). Source flags
            # first, fxtwitter confirmation as the last line.
            if entry.get("is_reply") or is_reply_via_api(fingerprint(entry)):
                skipped_replies += 1
                continue
            send_telegram(link)
            sent += 1
            time.sleep(1)
        if skipped_stale:
            log.info(f"Marked {skipped_stale} stale entry(ies) >"
                     f"{MAX_TWEET_AGE_HOURS}h old as seen without posting.")
        if skipped_replies:
            log.info(f"Skipped {skipped_replies} reply(ies) — marked seen, not posted.")
        log.info(f"Done — forwarded {sent} new tweet(s).")

    save_state(state, seen)


if __name__ == "__main__":
    main()
    # Only reached if main() did NOT raise — so repeated crashes stop the
    # heartbeat and healthchecks.io escalates to email.
    ping_heartbeat()
