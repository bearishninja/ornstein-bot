#!/bin/bash
# Guardrail: hard-deny commands that can lock us out of the droplet or power
# it off.
#
# WHY THIS EXISTS (Aug 19 2026): a discretionary SSH port change — whose only
# benefit was quieter logs — broke the socket-activated SSH unit on this
# Ubuntu 24.04 box, killed the listener on restart AND on reboot, and cost the
# owner ~50 min of recovery (root password reset + VNC recovery console). The
# bot never stopped; only access was lost. Documentation did not prevent it,
# so this block is mechanical.
#
# If you are Claude and you hit this: STOP. Do not look for a workaround, a
# different tool, or a cleverer phrasing. Tell the owner what you want to run
# and why, and let them decide and run it.
#
# SCOPE: the strict list applies to commands that actually reach the droplet
# (they mention the droplet host or an ssh/scp/rsync invocation). Purely local
# commands — including editing docs that merely MENTION these units — are not
# blocked, except for power commands in a genuinely executable position.
# Read-only droplet checks stay allowed: `ufw status`, `sshd -T`,
# `fail2ban-client status sshd`, journalctl, systemctl status/list-*.
#
# Tests: bash .claude/hooks/test-guard.sh  (re-run after ANY edit here)

set -uo pipefail

DROPLET_IP="168.144.155.254"

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
# FAIL SAFE, not fail open: if jq is missing or the payload shape changes, scan
# the raw input instead of waving the command through. Over-blocking is the
# correct direction of error for a guardrail.
[ -z "$CMD" ] && CMD="$INPUT"
[ -z "$CMD" ] && exit 0

deny() {
  jq -n --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

# Does this command actually reach the droplet?
TARGETS_DROPLET=0
if grep -qE "$DROPLET_IP|(^|[[:space:];&|(])(ssh|scp|rsync|sftp)[[:space:]]" <<<"$CMD"; then
  TARGETS_DROPLET=1
fi

if [ "$TARGETS_DROPLET" = "1" ]; then
  # --- SSH / access path ----------------------------------------------------
  grep -qiE 'ssh\.socket|ssh\.service' <<<"$CMD" && \
    deny "BLOCKED: touches the SSH socket/service units — the exact units whose override caused the Aug 19 2026 lockout. The droplet's access path is frozen. If this is genuinely needed, explain it to the owner and let them run it from the DigitalOcean Recovery Console."
  grep -qiE 'sshd_config|/etc/ssh|ListenStream' <<<"$CMD" && \
    deny "BLOCKED: touches SSH configuration on the droplet. The access path is frozen (SSH stays on port 22). Hand this to the owner rather than working around it."
  grep -qiE 'authorized_keys' <<<"$CMD" && \
    deny "BLOCKED: touches authorized_keys. Key access changes belong to the owner."

  # --- Firewall -------------------------------------------------------------
  grep -qiE 'ufw[[:space:]]+(allow|deny|limit|reject|delete|insert|default|reset|disable|enable|--force)' <<<"$CMD" && \
    deny "BLOCKED: mutates the ufw firewall, which can cut off SSH. Read-only 'ufw status' is allowed. Ask the owner to make firewall changes."
  grep -qiE '\b(iptables|ip6tables|nft)\b' <<<"$CMD" && \
    deny "BLOCKED: direct firewall manipulation (iptables/nft) can cut off SSH. Ask the owner."

  # --- Auth -----------------------------------------------------------------
  grep -qiE '\b(passwd|chpasswd|usermod|useradd|adduser|userdel|deluser)\b' <<<"$CMD" && \
    deny "BLOCKED: changes system accounts or passwords on the droplet. Account/auth changes belong to the owner."

  # --- Power ----------------------------------------------------------------
  grep -qiE '\b(reboot|shutdown|poweroff|halt)\b|systemctl[[:space:]]+(reboot|poweroff|halt|kexec)|\binit[[:space:]]+[06]\b' <<<"$CMD" && \
    deny "BLOCKED: changes the droplet's power state. Reboots are the owner's call (DigitalOcean panel → Power). For boot history use 'journalctl --list-boots' instead of 'last -x reboot'."
fi

# --- Local machine: power commands actually being INVOKED --------------------
# Must sit at a command position AND be followed by end-of-command, a shell
# separator, or a flag — so prose like "…; reboot is the owner's call" inside a
# doc edit is not mistaken for an instruction to reboot anything.
grep -qE '(^|[;&|]|\$\()[[:space:]]*(sudo[[:space:]]+)?(reboot|shutdown|poweroff|halt)([[:space:]]*($|[;&|])|[[:space:]]+-)' <<<"$CMD" && \
  deny "BLOCKED: power-state command. If a machine genuinely needs restarting, ask the owner to do it."

exit 0
