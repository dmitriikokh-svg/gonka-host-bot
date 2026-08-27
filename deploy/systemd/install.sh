#!/usr/bin/env bash
# Install oneshot watcher timers and the command listener.
# Does not enable units unless --enable is passed.
# Do not enable while GitHub Actions still sends Telegram.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENABLE=0
if [[ "${1:-}" == "--enable" ]]; then
  ENABLE=1
fi

install -m 0644 "$ROOT/deploy/gonka-command-listener.service.example" \
  /etc/systemd/system/gonka-command-listener.service

python_bin=/opt/gonka-host-bot/.venv/bin/python
while IFS='|' read -r name calendar script; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  service="gonka-${name}.service"
  timer="gonka-${name}.timer"
  cat > "/etc/systemd/system/${service}" <<EOF
[Unit]
Description=Gonka host-bot ${name}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
TimeoutStartSec=300
User=gonka-monitor
Group=gonka-monitor
WorkingDirectory=/opt/gonka-host-bot
EnvironmentFile=/etc/gonka-host-bot.env
ExecStart=${python_bin} ${script}
EOF
  cat > "/etc/systemd/system/${timer}" <<EOF
[Unit]
Description=Timer for ${service}

[Timer]
OnCalendar=${calendar}
Persistent=true
Unit=${service}

[Install]
WantedBy=timers.target
EOF
done < "$ROOT/deploy/systemd/watchers.conf"

systemctl daemon-reload
if [[ "$ENABLE" -eq 1 ]]; then
  systemctl enable --now gonka-command-listener.service
  while IFS='|' read -r name _calendar _script; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    systemctl enable --now "gonka-${name}.timer"
  done < "$ROOT/deploy/systemd/watchers.conf"
  echo "Enabled command listener and watcher timers."
else
  echo "Installed units. Timers are not enabled. Pass --enable after Actions schedules are off."
fi
