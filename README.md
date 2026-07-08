# Ornstein Tweet Bot 🔔⚽

Forwards new tweets from [@David_Ornstein](https://x.com/David_Ornstein) into the
**BMS FC** Telegram group, with a fully-rendered preview card (text, images,
video) so everyone can read and reply without leaving Telegram.

**Runs on GitHub Actions — no server, no cost, no Twitter API keys.**

---

## How it works

Every 5 minutes, GitHub Actions runs `bot.py`, which checks public RSS feeds of
Ornstein's tweets, finds any it hasn't posted yet, and sends each to the group as
a `fixupx.com` link — which Telegram renders as a rich card. A cached
`state.json` remembers what's already been posted so nothing repeats.

## Setup

See `CLAUDE.md` for full architecture and history. Quick version:

1. **Secrets** — in repo Settings → Secrets and variables → Actions:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `TELEGRAM_CHAT_ID` — the group ID (negative number)
2. **Deploy** — it's already wired. The workflow runs every 5 minutes.
3. **Test** — Actions tab → "Tweet Forwarder" → Run workflow → check the logs.

## Working on this with Claude Code

See `GETTING_STARTED.md` for how to install Claude Code and start iterating on
this project hands-free (Claude Code commits and pushes for you).

## Customisation

- Track a different account: set repo variable `TWITTER_USERNAME`.
- Change frequency: edit the `cron` in `.github/workflows/tweet-check.yml`
  (note: GitHub's minimum is every 5 minutes).

## Troubleshooting

- **Posting nothing?** Run the workflow manually and read the logs. Each feed
  source reports its entry count; if all show `0 entries`, the feed sources are
  down and need refreshing (see `CLAUDE.md` → Known issues).
- **Duplicates?** Shouldn't happen with the current workflow. If it does, check
  that the cache steps in the workflow still use the unique-key pattern.
