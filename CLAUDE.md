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
GitHub Actions cron (every 5 min)
        │
        ▼
   bot.py runs once
        │
        ├─ 1. Load "seen tweet" fingerprints from state.json (restored from Actions cache)
        ├─ 2. Query ALL RSS feed sources; keep the response with the MOST entries
        ├─ 3. For each entry not already seen (oldest-first):
        │        rewrite x.com URL → fixupx.com URL
        │        POST it to Telegram sendMessage
        ├─ 4. Save updated fingerprints back to state.json (saved to Actions cache)
        └─ done (process exits)
```

- **No Twitter/X API keys.** We read public RSS feeds instead. This avoids X's
  paid API entirely.
- **No server.** It runs as a scheduled GitHub Actions workflow. Public repo =
  unlimited free Actions minutes.
- **State** (which tweets we've already posted) is a small `state.json` file,
  persisted between runs via the Actions cache (NOT committed to the repo).

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
├── bot.py                          # The whole bot. ~180 lines.
├── check_feeds.py                  # Standalone diagnostic: reports which RSS mirrors are alive
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

- **Repo:** `bearishninja/ornstein-bot` (GitHub, public).
- **Trigger:** `.github/workflows/tweet-check.yml` runs on a `*/5 * * * *` cron
  and on manual `workflow_dispatch` (Actions tab → Run workflow).
- **Secrets** (repo Settings → Secrets and variables → Actions):
  - `TELEGRAM_BOT_TOKEN` — the BotFather token.
  - `TELEGRAM_CHAT_ID` — the group chat ID (a negative number, e.g. `-1001510845978`).
- **Optional variable:** `TWITTER_USERNAME` (defaults to `David_Ornstein`).

### The Telegram side (already set up, for reference)
- Bot created via @BotFather.
- Bot username: `ornstein_alerts_bot`.
- Target group chat ID: `-1001510845978` (group "BMS FC").
- Bot must be a member of the group with permission to post.

## Latency reality (important context, don't re-litigate)

The owner asked for ~1-minute delivery. This is **not achievable for free on
GitHub Actions**, for two independent reasons — both already researched:

1. **GitHub Actions cron floor is 5 minutes** and scheduled runs are frequently
   delayed 5–30 min under load (no SLA). `*/5` is the fastest valid schedule.
2. **The free RSS feed's own refresh rate is the real bottleneck.** Polling
   faster than the upstream feed updates buys nothing.

True ~1-minute delivery would require moving off GitHub Actions to **Cloudflare
Workers** (free tier, 1-minute cron, always-on, KV for state) — but that means a
JavaScript rewrite. This is a documented, deliberate future option, NOT a bug.
Only pursue it if the owner explicitly decides speed is worth the rewrite.

## Known issues / current state

- **A tweet was missed on Jul 9 2026** (posted ~6.5h late after a fix): GitHub's
  cron ran only twice in 12 hours AND all feed sources were dead simultaneously.
  This prompted two changes: (a) the feed list was refreshed around nitter.net
  (see below), and (b) the owner decided to migrate the bot to a personal
  DigitalOcean VPS — see "Planned migration" below.
- **Feed fragility is the #1 ongoing risk — now mitigated three ways**
  (added Jul 13 2026):
  1. **Dynamic discovery:** the bot refreshes its nitter instance pool from
     the community health tracker (`status.d420.de/api/v1/instances`, ALL
     healthy + not bad-host instances — the rss flag is NOT required, see
     #2, and there is NO top-N cutoff) every 1h, cached in state.json.
     Static fallbacks (nitter.net, xcancel, rsshub, diffbot) are always
     appended, so a dead/poisoned tracker can't blind the bot. (Jul 16 2026
     incident: a 6h-old top-6 snapshot excluded kareem.one — the only
     fresh instance — while every pooled source was stale; hence hourly
     refresh and no cutoff.)
  2. **Dual-mode source merging:** every instance is fetched BOTH as RSS and
     as an HTML timeline (`tweet-link` anchors only — `quote-link` anchors
     are embedded quoted tweets and are excluded; timestamps derived from
     snowflake IDs). All responding sources are fetched in parallel and
     their valid tweets merged (dedup by status ID) — no winner-takes-all.
     Added after the Jul 15 2026 incident: nitter.net's RSS went
     rich-but-STALE for 27h+ (a fresh EXCL was posted manually, ~7 min
     late), while rss-less nitter.kareem.one had it fresh in HTML.
  3. **Owner alerting:** if no source returns a rich feed (≥5 valid tweets)
     for >2h, the bot DMs the owner via `TELEGRAM_ALERT_CHAT_ID` (re-alerts
     at most daily; sends a recovery DM when sources return). If that env is
     unset, alerts are log-only. The DM goes to the owner, NEVER the group.
  4. **Stale-feed watchdog — REMOVED Aug 20 2026. Do not rebuild it.** A
     third-party Telegram mirror channel (`t.me/David_Ornstein`) was polled to
     catch feeds that were rich-but-STALE, by comparing its newest post time
     against the newest tweet our feeds had surfaced. It caught two real
     incidents (Jul 15 and Jul 16 2026) — both of which are now handled
     automatically by dual-mode fetching (#2), hourly pool refresh (#1), and
     the pool-refresh self-heal. After that it produced only false alarms,
     because the channel posts **paid advertisements**: an ad is permanently
     "newer" than our latest tweet, so it defeated both the 45-minute
     persistence check and the daily rate limit and paged the owner about a
     music promo. The owner (correctly) retired it. Lesson worth keeping: an
     alert that cannot be acted on is worse than no alert, because it trains
     the owner to ignore the alerts that matter.

  As of Jul 2026 the richest source is `nitter.net` (~20 tweets). Use
  `python check_feeds.py` (mirrors the bot's dynamic+static list) for live
  status.
- **Safeguards against re-post/spam** (added Jul 2026, keep these):
  - `is_tweet_entry()`: only entries whose link contains `/status/<id>` are
    eligible — for posting AND for the most-entries feed contest. Added after
    a Jul 13 2026 incident: nitter blipped for one cycle, diffbot won with 4
    "entries" that were actually X login-page furniture (help/signup/t.co
    links), and 3 junk messages hit the group.
  - **Reply filtering** (added Jul 22 2026 after two of Ornstein's
    credit-replies hit the group): the group wants scoops and RTs, NOT
    reply-thread chatter. Three layers, because no single source sees all
    replies: (1) nitter RSS titles replies "R to @user:" — covers replies
    to others AND self-thread replies; (2) nitter HTML marks replies to
    others with a `replying-to` class — but NOT self-replies; (3) before
    actually posting, `is_reply_via_api()` confirms via
    `api.fxtwitter.com/status/<id>` (fail-OPEN: if that API is down, post
    anyway — a missed scoop is worse than a stray reply). Reply flags stick
    through the merge (an unflagged HTML copy must not launder the RSS
    flag). Tradeoff, deliberate: substantive self-thread CONTINUATIONS are
    also skipped — the group reads the original scoop and can tap through.
    Replies are marked seen, so they never resurface.
  - Fingerprints are the tweet's numeric status ID, extracted from any source's
    link format — the same tweet dedupes identically across feed sources.
  - `MAX_TWEET_AGE_HOURS = 24`: entries older than 24h are marked seen but
    never posted, so a recovering rich feed can't flood the group with stale
    news. Tradeoff: if ALL feeds are dead for >24h, that window's tweets are
    silently dropped.
  - `send_telegram()` canonicalizes any mirror's link (nitter.net, xcancel,
    etc.) to `fixupx.com/<user>/status/<id>` — never post a raw mirror URL.
- **GitHub Actions cron is unreliable in practice.** Observed: 2 runs in 12
  hours on a `*/5` schedule (Jul 9 2026). This is GitHub's infra, not a config
  bug — and is the main motivation for the VPS migration.
- **Self-inflicted SSH lockout, Aug 19 2026 (~50 min, bot unaffected).** A
  discretionary SSH port change — sole benefit: quieter logs — broke the
  socket-activated SSH unit; the listener came back on neither restart nor
  reboot. Recovery needed a root password reset and the VNC Recovery Console.
  The bot posted normally throughout; only access was lost. Full technical
  detail and the recovery runbook live in `VPS.md` (bearishninja/vps). The
  behavioural fallout is the "Rules for touching the droplet" in Hard rules
  below, enforced by `.claude/hooks/guard-droplet.sh`.

## Production deployment: DigitalOcean droplet (since Jul 10 2026)

The bot now runs on the owner's personal droplet. GitHub Actions remains as a
disabled fallback workflow (see below).

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
- Every feed source should log its entry count (`  <url> → N entries`) so the
  owner can eyeball which mirrors are alive from the Actions logs.
- Fail soft: one dead feed host must never crash the run or block the others.
- Never post without deduping against `state.json`.

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
