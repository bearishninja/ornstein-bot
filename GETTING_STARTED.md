# GETTING_STARTED.md — Working on this project with Claude Code

This guide gets you from "I have this folder" to "Claude Code is editing,
committing, and pushing this project for me." You won't need to copy-paste code
into GitHub by hand ever again — Claude Code does the git work for you.

---

## 0. What you need first

- **A paid Anthropic plan.** Claude Code needs Claude Pro, Max, Team, Enterprise,
  or a Console (API) account. The free Claude.ai plan does not include Claude Code.
- **A terminal.** On Mac: the Terminal app. On Windows: use WSL (Ubuntu) or the
  native install below. On Linux: your usual shell.
- **A GitHub account** — you already have this (`bearishninja`).

---

## 1. Install Claude Code

The **native installer** is recommended and needs no Node.js.

**macOS / Linux** — paste into your terminal:
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

**Windows** — paste into PowerShell:
```powershell
irm https://claude.ai/install.ps1 | iex
```

Then open a **new** terminal window and verify:
```bash
claude --version
```
If it prints a version number, you're good. (If it doesn't, run `claude doctor`
for a diagnosis.)

> Prefer npm? `npm install -g @anthropic-ai/claude-code` also works but needs
> Node.js. The native installer is simpler and auto-updates, so use that unless
> you already live in npm.

---

## 2. Put this project on your computer

Unzip the `ornstein-bot` folder somewhere sensible (e.g. your home directory or
Documents). You should see `bot.py`, `CLAUDE.md`, and the rest inside it.

---

## 3. Launch Claude Code in the project

```bash
cd path/to/ornstein-bot
claude
```

The first launch opens your browser to log in with your Anthropic account. After
that, Claude Code starts in the project folder and **automatically reads
`CLAUDE.md`**, so it already knows the full history, architecture, goals, and
open issues of this project. You don't have to re-explain anything.

---

## 4. First things to tell Claude Code

You can talk to it in plain English. Good opening moves, roughly in order:

**a) Connect it to GitHub and push the current code.** The repo already exists at
`bearishninja/ornstein-bot` but is running an outdated version. Say:

> Read CLAUDE.md so you have context. Then help me push this local folder to my
> existing GitHub repo `bearishninja/ornstein-bot`, replacing what's there on the
> `main` branch. Walk me through authenticating with GitHub if needed.

Claude Code will set up the git remote, handle auth (it may ask you to run a
`gh auth login` or paste a token — follow its lead), and push. This single step
replaces all the manual copy-paste-into-GitHub you were doing before.

**b) Confirm the deployment is healthy.** Say:

> The GitHub Actions workflow runs the bot every 5 minutes. Can you check the
> latest run's logs and tell me which RSS feed sources are actually returning
> tweets right now, and whether any are dead?

**c) From here, just describe what you want.** Examples:
> - "Add three more RSSHub mirror instances to the feed list and push."
> - "The card isn't showing video on some tweets — investigate why."
> - "Show me what a test run posts before it goes to the real group."

Claude Code edits the files, explains the change, and (with your OK) commits and
pushes. GitHub Actions then picks up the new version automatically.

---

## 5. Useful things to know

- **It asks before doing anything destructive.** Pushing, deleting, force-changes
  — it'll confirm with you first. You stay in control.
- **`CLAUDE.md` is the project's memory.** If you make a big decision (e.g. "we're
  moving to Cloudflare Workers"), ask Claude Code to update `CLAUDE.md` so future
  sessions remember it.
- **Secrets never go in the repo.** Your Telegram token and chat ID live only in
  GitHub Actions secrets. Don't paste them into files. Claude Code knows this
  rule (it's in `CLAUDE.md`), but don't override it.
- **Testing safely.** If you want to try changes without spamming the real group,
  ask Claude Code to help you point `TELEGRAM_CHAT_ID` at a private test group
  first.

---

## 6. If you get stuck

- `claude doctor` — checks your install and auth.
- Ask Claude Code directly: "I'm seeing X error, what do I do?" — it can read the
  error and usually fix it.
- Claude Code docs: https://docs.claude.com/en/docs/claude-code/overview
