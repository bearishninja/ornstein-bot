#!/bin/bash
# Test suite for guard-droplet.sh.
#
# Run:  bash .claude/hooks/test-guard.sh
#
# The cases live in this file rather than being typed into a shell, because the
# guard (correctly) blocks any Bash command that merely looks like it targets
# the droplet's access path — including a test harness containing such strings.
#
# Re-run this after ANY edit to guard-droplet.sh. A guard that silently stops
# blocking is worse than no guard, because it is trusted.

GUARD="$(dirname "$0")/guard-droplet.sh"
HOST="root@168.144.155.254"
pass=0; fail=0

check() { # check <expect: BLOCK|ALLOW> <command>
  local expect="$1" cmd="$2" got
  if jq -nc --arg c "$cmd" '{tool_input:{command:$c}}' | bash "$GUARD" | grep -q '"deny"'; then
    got="BLOCK"
  else
    got="ALLOW"
  fi
  if [ "$got" = "$expect" ]; then
    pass=$((pass+1)); printf "  ok    %-5s  %s\n" "$got" "${cmd:0:78}"
  else
    fail=$((fail+1)); printf "  FAIL  want=%-5s got=%-5s  %s\n" "$expect" "$got" "${cmd:0:60}"
  fi
}

echo "--- must BLOCK: droplet access path ---"
check BLOCK "ssh $HOST 'ufw allow 9999/tcp'"
check BLOCK "ssh $HOST 'ufw delete allow 2222/tcp'"
check BLOCK "ssh $HOST 'systemctl restart ssh.socket'"
check BLOCK "ssh $HOST 'systemctl disable --now ssh.service'"
check BLOCK "ssh $HOST 'vi /etc/ssh/sshd_config'"
check BLOCK "ssh $HOST 'echo ListenStream=2222 > /etc/systemd/system/ssh.socket.d/p.conf'"
check BLOCK "ssh $HOST 'echo key >> ~/.ssh/authorized_keys'"
check BLOCK "ssh $HOST 'passwd root'"
check BLOCK "ssh $HOST 'iptables -F'"
check BLOCK "ssh $HOST 'nft flush ruleset'"
check BLOCK "ssh $HOST 'useradd bob'"
check BLOCK "scp cfg $HOST:/etc/ssh/sshd_config"

echo "--- must BLOCK: power state ---"
check BLOCK "ssh $HOST reboot"
check BLOCK "ssh $HOST 'shutdown -h now'"
check BLOCK "ssh $HOST 'systemctl poweroff'"
check BLOCK "sudo reboot"
check BLOCK "reboot"
check BLOCK "echo done; poweroff"

echo "--- must ALLOW: routine droplet ops ---"
check ALLOW "ssh $HOST 'uptime; free -h; df -h /'"
check ALLOW "ssh $HOST 'ufw status'"
check ALLOW "ssh $HOST 'sshd -T | grep passwordauthentication'"
check ALLOW "ssh $HOST 'fail2ban-client status sshd'"
check ALLOW "ssh $HOST 'journalctl -u ornstein-bot.service -n 50'"
check ALLOW "ssh $HOST 'journalctl --list-boots | tail -3'"
check ALLOW "ssh $HOST 'systemctl list-timers ornstein-bot.timer'"
check ALLOW "ssh $HOST 'systemctl start ornstein-bot.service'"
check ALLOW "ssh $HOST 'systemctl --failed'"
check ALLOW "ssh $HOST 'cd /opt/ornstein-bot && git pull -q origin main'"
check ALLOW "ssh $HOST 'cat /opt/ornstein-bot/state.json'"
check ALLOW "ssh $HOST 'cd /opt/vps && git pull -q'"

echo "--- must ALLOW: local work, including docs that MENTION blocked things ---"
check ALLOW "git add CLAUDE.md && git commit -m 'document the ssh.socket lockout'"
check ALLOW "grep -n 'ssh.socket' CLAUDE.md"
check ALLOW "python3 -c \"print('ssh.socket and /etc/ssh/sshd_config are frozen; reboot is the owner call')\""
check ALLOW "python3 -c \"import ast; ast.parse(open('bot.py').read())\""

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL $pass CASES PASSED"
else
  echo "$pass passed, $fail FAILED"
  exit 1
fi
