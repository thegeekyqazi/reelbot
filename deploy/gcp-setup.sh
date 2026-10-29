#!/usr/bin/env bash
# One-shot, idempotent setup for reelbot on a Debian 12 VM (e.g. GCP e2-micro).
# Run as a sudo-capable user:   bash gcp-setup.sh
# Override the source repo with: REPO_URL=https://github.com/<you>/reelbot.git bash gcp-setup.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/thegeekyqazi/reelbot.git}"
APP_DIR=/opt/reelbot
APP_USER=reelbot

if [[ $EUID -ne 0 ]]; then
  exec sudo REPO_URL="$REPO_URL" bash "$0" "$@"
fi

as_app() { runuser -u "$APP_USER" -- "$@"; }

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv python3-pip ffmpeg git

echo "==> Swap"
if [[ -z "$(swapon --show --noheadings)" ]]; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    1 GB swap enabled"
else
  echo "    swap already active, skipping"
fi

echo "==> User and code"
if ! id "$APP_USER" &>/dev/null; then
  useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$APP_DIR/data"

echo "==> Python venv"
[[ -x "$APP_DIR/.venv/bin/python" ]] || as_app python3 -m venv "$APP_DIR/.venv"
as_app "$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
as_app "$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"

echo "==> .env"
NEEDS_CONFIG=0
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  NEEDS_CONFIG=1
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
grep -q 'paste-your-bot-token-here' "$APP_DIR/.env" && NEEDS_CONFIG=1

echo "==> systemd units"
install -m 644 "$APP_DIR/deploy/reelbot.service" "$APP_DIR/deploy/ytdlp-update.service" \
  "$APP_DIR/deploy/ytdlp-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable reelbot.service
# ytdlp-update.service is a oneshot started by the timer; enabling the timer schedules it.
systemctl enable --now ytdlp-update.timer

echo
if [[ $NEEDS_CONFIG -eq 1 ]]; then
  cat <<MSG
==> Almost done. Fill in your settings, then start the bot:
      sudo nano $APP_DIR/.env          # set DISCORD_TOKEN and OWNER_ID
      sudo systemctl start reelbot
      journalctl -u reelbot -f         # watch the logs
MSG
else
  systemctl restart reelbot.service
  echo "==> reelbot (re)started. Logs: journalctl -u reelbot -f"
fi
