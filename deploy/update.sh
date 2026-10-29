#!/usr/bin/env bash
# Pull the latest code, update dependencies and restart reelbot.
# Run on the VM:  sudo bash /opt/reelbot/deploy/update.sh
set -euo pipefail

APP_DIR=/opt/reelbot
APP_USER=reelbot

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

as_app() { runuser -u "$APP_USER" -- "$@"; }

as_app git -C "$APP_DIR" pull --ff-only
as_app "$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"
as_app "$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check --upgrade yt-dlp

install -m 644 "$APP_DIR/deploy/reelbot.service" "$APP_DIR/deploy/ytdlp-update.service" \
  "$APP_DIR/deploy/ytdlp-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl restart reelbot.service
systemctl --no-pager --lines=5 status reelbot.service
