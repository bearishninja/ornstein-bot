# CLAUDE.md — Project Context for Claude Code

This file is loaded automatically by Claude Code at the start of every session.
It is the single source of truth for what this project is, why it's built the
way it is, and what still needs doing. Keep it updated as the project evolves.

---

## What this project is

A Telegram bot that forwards new tweets from the journalist **David Ornstein**
([@David_Ornstein](https://x.com/David_Ornstein)) into a Telegram group called
**"BMS FC"** (a group of friends following football/transfer news).

Ornstein is the leading Premier League transfer journalist, so the group wants
his posts to appear **quickly** and, critically, with a **fully-rendered preview
card** (author, tweet text, images/video) so members can read, reply, react, and
quote-reply *without leaving Telegram or clicking through to X*.

## The core design goals (in priority order)

1. **Rich preview card must render.** This is the whole point. A bare `x.com`
   link renders a broken/blank card in Telegram because X stripped its Open
   Graph tags. We solve this with `fixupx.com` (see below). Do not regress this.
2. **Low maintenance.** The owner is not a developer. Prefer solutions that run
   themselves and rarely need touching.
3. **Free.** The owner has chosen to keep costs at zero and accept the
   reliability tradeoffs that come with free feed sources (see "Known issues").
4. **Fast.** Ideally tweets arrive within a couple of minutes. See "Latency
   reality" for why true 1-minute delivery is not achievable on this stack.

## How it works (architecture)

```
systemd timer on the droplet (every minute)
        │
        ▼
   bot.py runs once, exits
        │
        ├─ 1. Load state.json (seen tweet IDs, instance pool, proven sources)
        ├─ 2. Refresh the nitter instance pool from the health tracker (hourly)
        ├─ 3. Probe sources — normally just the PROVEN ones (~2-4 requests);
        │      full sweep of all ~20 every 30 min, or at once if a cycle
        │      yields nothing (see "Two-tier probing" below)
        ├─ 4. Merge every source's valid tweets, dedup by numeric status ID
        ├─ 5. Drop replies and anything older than MAX_TWEET_AGE_HOURS
        ├─ 6. Post the rest oldest-first as fixupx.com links
        ├─ 7. Save state, ping healthchecks.io
        └─ done (process exits)
```

- **No Twitter/X API keys.** Public RSS feeds and scraped nitter HTML only.
- **Runs on the owner's DigitalOcean droplet** via a systemd timer, not on
  GitHub Actions (that was the original design; the workflow is now a disabled
  fallback — see Deployment). State is a local `state.json`, gitignored.
- **~1-3 min latency** end to end, bounded by the upstream feeds' own refresh
  rate rather than by our polling.

### Two-tier probing (added Aug 20 2026 — keep this)

Measured over 60 consecutive cycles: 20 source probes per cycle, of which
exactly **2** ever returned tweets (`[rss] nitter.net`, `[html] xcancel.com`).
The other 18 failed identically every minute — six nitter instances return
**403 to this droplet's datacenter IP** even though the tracker reports them
healthy (they are, from a browser). That was ~26,000 useless requests/day
aimed at volunteer-run instances, and ~43,000 journal lines/day, which capped
log history at ~6 days.

So: normal cycles probe only `state["proven"]`; the full list is swept every
`SWEEP_MINUTES` (30) to rediscover, immediately if the pool changed, and
**immediately if a proven-only cycle yields nothing** — that last path is what
stops a dying workhorse from blinding the bot. Do not "simplify" this back to
probing everything every minute, and do not delete discovery either: on Jul 16
a tracker-discovered instance was the only source with a fresh tweet.

## The fixupx.com trick (do not remove)

`send_telegram()` in `bot.py` rewrites the tweet URL host from `x.com` /
`twitter.com` to `fixupx.com` before posting. `fixupx.com` (part of the
FixTweet project) serves proper Open Graph tags, so Telegram renders a full
card. The message body is *just the fixupx URL* — no HTML, no header text —
because that produces the cleanest native card and lets people reply/react/quote
it like any normal message. Tapping the card redirects to the real tweet on x.com.

If you ever need a fallback, `fxtwitter.com` behaves the same way.

## File structure

```
.
├── bot.py                          # The whole bot (~525 lines).
├── check_feeds.py                  # Diagnostic: probes every source (imports bot.py — one source list)
├── .claude/settings.json           # Registers the droplet guardrail hook
├── .claude/hooks/guard-droplet.sh  # HARD-BLOCKS access-path/power commands (see Hard rules)
├── .claude/hooks/test-guard.sh     # Tests for the guard — re-run after editing it
├── requirements.txt                # feedparser, requests
├── .github/workflows/tweet-check.yml  # Cron schedule + run + state cache
├── .gitignore                      # Ignores state.json, .env, __pycache__
├── README.md                       # User-facing setup guide
└── CLAUDE.md                       # This file
```

## Deployment

**Production: the owner's DigitalOcean droplet** (full detail, runbook and
box-wide conventions in the "Production deployment" section below and in the
`bearishninja/vps` repo).

- **Repo:** `bearishninja/ornstein-bot` (GitHub, public — note it documents the
  droplet's IP and layout; no secrets, but see "Note" at the end).
- **Trigger:** `ornstein-bot.timer` fires `ornstein-bot.service` every minute.
- **Secrets:** `/opt/ornstein-bot/.env` on the droplet (chmod 600).
- **Fallback:** the GitHub Actions workflow is kept but **disabled** so the two
  can't double-post. Re-enable from the Actions tab if the droplet dies.

### The Telegram side (already set up, for reference)
- Bot created via @BotFather; username `ornstein_alerts_bot`.
- Target group chat ID: `-1001510845978` (group "BMS FC").
- Owner's private chat with the bot receives outage DMs
  (`TELEGRAM_ALERT_CHAT_ID`).
- Bot must remain a member of the group with permission to post.

## Latency reality (don't re-litigate)

Delivery is ~1-3 min after Ornstein tweets, and the remaining delay is **the
upstream feeds' own refresh rate**, not our scheduler — the timer already runs
every minute. Polling faster buys nothing.

Historical note: on GitHub Actions this was far worse (cron floor of 5 min, and
observed gaps of 3-5 HOURS), which is why the bot moved to the droplet. If
someone asks for faster delivery, the honest answer is that it needs a better
data source (a logged-in X session or the paid API), not a faster loop.

## Safeguards (keep these — each one exists because of a real incident)

Feed fragility is the #1 ongoing risk. Everything below is load-bearing; the
*reason* is recorded with each rule because that reason is what should stop a
future session "simplifying" it away.

- **`is_tweet_entry()`** — only entries whose link contains `/status/<id>` are
  eligible, for posting *and* for source selection. Without it a scraper's
  login-page furniture (help/signup/t.co links) reaches the group.
- **Fingerprint = numeric status ID**, extracted from any source's link format,
  so the same tweet dedupes identically no matter which mirror served it.
  Switching sources must never re-post.
- **`send_telegram()` canonicalises every link** to
  `fixupx.com/<user>/status/<id>`. Never post a raw mirror URL — only the
  fixupx form renders the card, which is the entire point of the bot (goal #1).
- **`MAX_TWEET_AGE_HOURS = 24`** — older entries are marked seen but never
  posted, so a feed returning after an outage can't flood the group with stale
  news. Deliberate tradeoff: if every source is dead >24h, that window is lost.
- **Dual-mode fetching** — every instance is tried as RSS *and* as an HTML
  timeline, because an instance can serve one and not the other, and RSS can go
  rich-but-STALE while HTML is fresh. HTML parsing uses `tweet-link` anchors
  only; `quote-link` anchors are embedded quoted tweets and must stay excluded.
- **Merge, never winner-takes-all** — all responding sources are merged by
  status ID, so one stale source cannot hide a tweet another source has.
- **Dynamic instance discovery** — the pool refreshes hourly from
  `status.d420.de/api/v1/instances` (all healthy, not-bad-host instances; the
  `rss` flag is deliberately *not* required, since we also scrape HTML). Static
  entries are always appended so a dead or poisoned tracker can't blind the bot.
- **Two-tier probing** — see the architecture section. Normal cycles probe only
  proven sources; sweeps rediscover. Both halves matter.
- **Reply filtering** — the group wants scoops and retweets, not reply-thread
  chatter. Three layers, because no single source sees every reply: nitter RSS
  titles replies `"R to @user:"` (catches replies to others *and* self-replies);
  nitter HTML marks replies to others with `replying-to` (but not self-replies);
  and `is_reply_via_api()` confirms against `api.fxtwitter.com` immediately
  before posting. That last check **fails OPEN** — if the API is down we post
  anyway, because a missed scoop is worse than a stray reply. Reply flags
  survive the merge, so an unflagged duplicate can't launder a flagged one.
  Deliberate tradeoff: substantive self-thread continuations are skipped too.
- **Dead-feed alert** — if no source returns a rich feed (≥`RICH_FEED_MIN_TWEETS`)
  for `ALERT_AFTER_HOURS`, the bot DMs the owner (`TELEGRAM_ALERT_CHAT_ID`),
  re-alerting at most once per `REALERT_HOURS`, with a recovery DM when sources
  return. Never posts alerts to the group.
- **External heartbeat** — every completed run pings `HEALTHCHECK_URL`
  (healthchecks.io), which alerts by email on *silence*. This is the only
  monitoring that survives the box dying, since every other alarm runs on it.
  Deliberately independent of feed health: tying them together would email
  "box down" during an ordinary feed outage.

**Removed on purpose — do not rebuild:** a stale-feed watchdog that compared a
third-party Telegram mirror channel (`t.me/David_Ornstein`) against our newest
tweet. Its two real catches are now handled automatically by dual-mode
fetching, hourly pool refresh, and the sweep self-heal. After that it only
produced false alarms, because that channel posts **paid ads** — and an ad is
permanently "newer" than our latest tweet, so it defeated both a 45-minute
persistence check and a daily rate limit, ultimately paging the owner about a
music promo while feeds were perfectly healthy. **Lesson: an alert that cannot
be acted on is worse than no alert, because it trains the owner to ignore the
ones that matter.**

## Incident log (condensed — the safeguards above are the durable output)

| Date | What happened | Outcome |
|---|---|---|
| Jul 9 | Tweet missed ~6.5h: GitHub cron ran twice in 12h **and** every feed was dead | Feed list rebuilt around nitter.net; decision to move to a VPS |
| Jul 13 | Nitter blipped, a scraper "won" with X login-page links → 3 junk messages in the group | `is_tweet_entry()` |
| Jul 15 | nitter.net RSS went rich-but-STALE for 27h+ while an rss-less instance had the tweet fresh in HTML | Dual-mode fetching + merge |
| Jul 16 | A 6h-old top-6 pool snapshot excluded the only fresh instance | Hourly refresh, no top-N cutoff, sweep self-heal |
| Jul 15 | A manual salvage post raced an in-flight run → same tweet twice, seconds apart | Runbook: drain the service before manual posting |
| Jul 22 | Two of Ornstein's credit-replies posted to the group | Reply filtering (3 layers) |
| Aug 3 | DigitalOcean suspended the droplet for non-payment; ~73 min silent outage | healthchecks.io heartbeat; billing alerts |
| Aug 19 | **Self-inflicted:** a discretionary SSH port change (sole benefit: quieter logs) broke socket-activated SSH; ~50 min of no access, bot unaffected | "Rules for touching the droplet" + `guard-droplet.sh` hook |
| Aug 20 | Watchdog paged the owner about an advert in the mirror channel | Watchdog retired |
| Aug 20 | Measured 18 of 20 source probes failing identically every minute (~26k wasted requests/day) | Two-tier probing; dead sources pruned |

Live source status any time: `python check_feeds.py`.

## Production deployment: the droplet (detail)

- **Droplet:** `bearishninja-services` — DigitalOcean Basic $6/mo (1GB RAM,
  1 vCPU, 25GB SSD), Ubuntu 24.04 LTS, Bangalore region.
- **Access:** `ssh root@168.144.155.254` (key on the owner's Mac at
  `~/.ssh/id_ed25519`, no passphrase).
- **Bot location:** `/opt/ornstein-bot` (git clone of this repo + venv).
- **Secrets:** `/opt/ornstein-bot/.env` (chmod 600) — TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID, TWITTER_USERNAME, and optionally TELEGRAM_ALERT_CHAT_ID
  (the owner's private chat with the bot, for outage DMs — the owner must
  /start a private chat with @ornstein_alerts_bot once; find the chat id via
  the Telegram getUpdates API) and HEALTHCHECK_URL (see below). Never in
  the repo.
- **External heartbeat** (added Aug 3 2026): `ping_heartbeat()` hits the
  healthchecks.io URL in `HEALTHCHECK_URL` after every run that completes
  without raising. healthchecks.io alerts by email on SILENCE (period 5
  min + grace 15 min), so it catches box death, network death, a disabled
  timer, or the script crashing every cycle — the failure class the bot's
  own Telegram alerts CANNOT report, since those need the bot alive. Added
  after the Aug 3 2026 billing suspension went unnoticed for ~73 min.
  Deliberately independent of feed health (feeds have their own alerting);
  tying them together would email "box down" during a mere feed outage.
  Fail-soft and env-gated: unset (e.g. on GitHub Actions) = no-op.
- **Scheduling:** systemd timer `ornstein-bot.timer` fires
  `ornstein-bot.service` (oneshot, runs `bot.py` once) **every minute**.
  Unit files: `/etc/systemd/system/ornstein-bot.{service,timer}`.
- **State:** `/opt/ornstein-bot/state.json` on local disk. Fingerprints are
  numeric tweet status IDs.
- **Box hardening (done):** ufw (OpenSSH+80+443 only), fail2ban,
  unattended-upgrades, 1GB swapfile. The droplet also hosts (or will host)
  the owner's other personal microservices — don't assume this bot is the
  only thing on it.
- **Box-wide conventions & services inventory:** the `bearishninja/vps`
  repo (private; `/opt/vps/VPS.md` on the droplet, symlinked at
  `/opt/VPS.md`). New services follow those conventions; this bot is one
  inventory row there.

### Droplet runbook

```bash
ssh root@168.144.155.254

journalctl -u ornstein-bot.service -n 50        # recent bot logs
systemctl list-timers ornstein-bot.timer        # is the schedule alive?
systemctl start ornstein-bot.service            # force a run now
cd /opt/ornstein-bot && git pull                # deploy latest code
cat /opt/ornstein-bot/state.json                # what's been seen
systemctl stop ornstein-bot.timer               # pause posting (start to resume)
```

**Before ANY manual intervention that posts or edits state.json** (e.g.
manually pushing a missed tweet), pause the schedule AND wait out any
in-flight run — stopping the timer does NOT stop a run already started:

```bash
systemctl stop ornstein-bot.timer
while systemctl is-active -q ornstein-bot.service; do sleep 1; done
# ...now safe to post manually / edit state.json...
systemctl start ornstein-bot.timer
```

(Learned Jul 15 2026: a manual salvage post raced an in-flight run that had
loaded pre-edit state — the group got the same tweet twice, seconds apart.)

### GitHub Actions fallback (disabled, do not delete)

The old workflow `.github/workflows/tweet-check.yml` is kept but **disabled**
so the droplet and Actions don't double-post (their states are separate). If
the droplet dies, re-enable it from the Actions tab (or
`gh workflow enable tweet-check.yml`) for instant fallback coverage at
GitHub-cron latency. Its state cache starts empty → first run safely marks
everything seen, no spam.
- **Do not reintroduce the old cache bug.** An earlier workflow used a fixed
  cache key `tweet-state` plus a `gh cache delete` step. That delete failed with
  HTTP 403 (default `GITHUB_TOKEN` lacks `actions: write`), so state never
  updated and tweets got re-posted every run. The current workflow avoids this by
  using a unique key per run (`tweet-state-${{ github.run_id }}`) with a
  `restore-keys: tweet-state-` prefix fallback. Keep that pattern.

## Coding conventions / preferences

- Keep `bot.py` a single self-contained script. Simplicity beats cleverness here.
- **Log by exception, not by default.** A healthy cycle is 3 lines. Per-source
  detail appears on full sweeps and whenever a *proven* source stops yielding —
  the actionable cases. Logging all ~20 sources every minute produced ~43k
  lines/day and left only ~6 days of journal history under the 200 MB cap.
  Use `python check_feeds.py` when you want a full picture on demand.
- Fail soft: one dead feed host must never crash the run or block the others.
- Never post without deduping against `state.json`.
- Prove a source works before adding it, and remove sources that have stopped
  earning their request (measure over dozens of cycles, not one).

## Hard rules (do not violate)

- **Never commit secrets.** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` live only
  in GitHub Actions secrets, never in the repo. `.env` and `state.json` are
  gitignored.
- **Never break the fixupx card rendering** (goal #1).
- If asked to make it faster than every 5 minutes, explain the latency reality
  above rather than silently setting an impossible cron like `* * * * *` (which
  GitHub will just run every 5 min anyway).

### Rules for touching the droplet

Added Aug 19 2026 after a self-inflicted SSH lockout. Read before running
anything against the box.

- **The access path and power state are FROZEN.** SSH config, the ssh socket/
  service units, firewall rules, accounts/passwords, authorized_keys, and
  restarting the machine are all off limits. `.claude/hooks/guard-droplet.sh`
  blocks them mechanically (registered in `.claude/settings.json`; tests in
  `.claude/hooks/test-guard.sh`). **If you hit that block, STOP.** Do not look
  for a workaround, another tool, or a cleverer phrasing. Say what you wanted
  to run and why, and let the owner decide. Circumventing the guard is a worse
  failure than the one it exists to prevent.
- **No discretionary infrastructure changes.** If a change does not fix an
  observed, reported problem, do not propose it at all. "Cleaner logs",
  "tidier", "best practice", "while we're in here" are NOT reasons to touch a
  working box. The Aug 19 lockout came from a change whose entire benefit was
  quieter log lines.
- **Casual assent is not authorization.** "sure", "ok", "if it helps", "go
  ahead" said in passing does not authorize risky infrastructure work —
  especially from an owner who has said plainly that he is not a developer and
  does not want to babysit this box. Require an explicit, unprompted request,
  and state the rollback plan before starting.
- **Verify the invariant that matters, not the one that is easy.** Before
  replacing any path the system depends on, prove the EXISTING path still works
  from a FRESH connection. On Aug 19 the new port was verified while the old
  listener had already been destroyed — the check passed on an already-broken
  system.
- **Prefer doing nothing.** This box runs one thing that matters, it has run it
  reliably for weeks, and success is measured by the group getting tweets — not
  by how tidy the server is.

## Handy commands

```bash
# Run the bot locally (needs the two env vars set)
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python bot.py

# Check which RSS feed mirrors are currently alive (no secrets needed)
python check_feeds.py

# Quick syntax check
python -c "import ast; ast.parse(open('bot.py').read()); print('OK')"

# Watch the latest Actions run from the terminal (requires gh CLI)
gh run watch
```
