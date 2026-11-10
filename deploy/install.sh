#!/usr/bin/env bash
# Ubuntu/Debian 서버용 설치 스크립트. root 로 실행: sudo bash deploy/install.sh
# 주의: 이 스크립트는 실제 서버에서 검증되지 않았습니다. 처음 한 번은 내용을 읽고 실행하세요.
set -euo pipefail

APP_DIR=/opt/btc-bot
APP_USER=btcbot
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "root 로 실행하세요: sudo bash deploy/install.sh" >&2
  exit 1
fi

apt-get update -y
apt-get install -y python3 python3-venv rsync

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
rsync -a --exclude '.git' --exclude '.venv' --exclude '.env' --exclude 'position_state.json' --exclude 'trading.log*' "$SRC_DIR"/ "$APP_DIR"/

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo ".env 를 새로 만들었습니다. 키는 직접 편집해서 넣으세요: $APP_DIR/.env"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

install -m 644 "$APP_DIR/deploy/btc-bot.service" /etc/systemd/system/btc-bot.service
systemctl daemon-reload

cat <<MSG

설치 완료. 아직 서비스는 시작하지 않았습니다. 다음 순서로 진행하세요(DEPLOY.md 참고).
  1) sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/preflight.py --ip-only
  2) 업비트에서 위 IP 를 허용 IP 로 넣어 키 발급 -> sudo nano $APP_DIR/.env
  3) sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/preflight.py
  4) DRY_RUN=true 로 시작: sudo systemctl enable --now btc-bot && journalctl -u btc-bot -f
MSG
