# Ornstein Tweet Bot 🔔⚽

Forwards new tweets from [@David_Ornstein](https://x.com/David_Ornstein) into the
**BMS FC** Telegram group, with a fully-rendered preview card (text, images,
video) so everyone can read and reply without leaving Telegram.

**Runs on a personal VPS via a systemd timer — no Twitter API keys, no cost
beyond the box.**

---

## How it works

Every minute, a systemd timer on the owner's DigitalOcean droplet runs
`bot.py`. It reads Ornstein's tweets from public nitter mirrors (RSS *and*
scraped HTML timelines), merges everything it finds, drops replies and
anything it has already posted, and sends the rest to the group as
`fixupx.com` links — which Telegram renders as rich cards. A local
`state.json` remembers what's been posted.

Typical delivery: **1–3 minutes** after he tweets. The remaining delay is the
mirrors' own refresh rate, not the polling interval.

`CLAUDE.md` has the full architecture, the safeguards and why each exists, the
incident log, and the droplet runbook.

## Operating it

```bash
# which sources are alive right now (no secrets needed)
pip install -r requirements.txt && python check_feeds.py

# on the droplet
journalctl -u ornstein-bot.service -n 50     # recent activity
systemctl list-timers ornstein-bot.timer     # is the schedule alive?
systemctl start ornstein-bot.service         # force one run
```

A healthy cycle logs three lines. Per-source detail appears on the periodic
full sweep, or whenever a source that was working stops.

## Monitoring

You get told when something breaks, rather than having to check:

- **All sources dead for 2h** → Telegram DM to the owner.
- **Box, network, timer or script dead** → email from healthchecks.io, which
  alerts on *silence* and so survives the box dying.

## Customisation

- Track a different account: set `TWITTER_USERNAME` in
  `/opt/ornstein-bot/.env`.
- Feed sources, thresholds and the sweep interval are constants at the top of
  `bot.py`.

## Fallback

The original GitHub Actions workflow is kept but **disabled**, so the two can't
double-post. If the droplet dies, re-enable it from the Actions tab for
immediate (if slower) coverage — its state cache starts empty, so the first run
safely marks everything seen instead of spamming.
