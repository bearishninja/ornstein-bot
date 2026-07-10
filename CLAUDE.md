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
- **Feed fragility is the #1 ongoing risk.** As of Jul 2026 the primary source
  is `nitter.net` (returns ~20 tweets, verified working from both residential
  IPs and GitHub runners). The old RSSHub mirrors are dead (404/503) but kept
  as cheap extra chances. Use `python check_feeds.py` to see live status. If
  nitter.net dies, check https://status.d420.de/ for fresh healthy instances.
- **Safeguards against re-post/spam** (added Jul 2026, keep these):
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

## Planned migration (in progress)

The owner is getting a cheap DigitalOcean droplet (~$4-6/mo, also for other
personal microservices). Plan: run this same `bot.py` on the droplet via a
systemd timer (or loop) every 1-2 minutes, with `state.json` on local disk —
no Actions cache dance, no cron flakiness, ~1-2 min latency. The data source
stays free (nitter.net et al). Everything else (fixupx, dedup, age cutoff)
carries over unchanged. Keep the GitHub Actions workflow running until the
droplet version is verified, then disable the workflow (do not delete it — it
is a fallback).
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
