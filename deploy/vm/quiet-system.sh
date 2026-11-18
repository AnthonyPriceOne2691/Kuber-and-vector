#!/bin/bash
# Тишина в системе: службы Ubuntu не ходят в сеть сами и ничего не обновляют.
# Lima запускает скрипт в режиме system на каждом старте; повторный запуск безопасен.
set -u

units=(
  unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer
  motd-news.timer update-notifier-download.timer update-notifier-motd.timer
  fwupd-refresh.timer ua-timer.timer
  snapd.snap-repair.timer snapd.service snapd.socket snapd.seeded.service snapd.autoimport.service
)
for u in "${units[@]}"; do
  systemctl disable --now "$u" >/dev/null 2>&1 || true
  systemctl mask "$u" >/dev/null 2>&1 || true
done
echo "quiet-system: done"
