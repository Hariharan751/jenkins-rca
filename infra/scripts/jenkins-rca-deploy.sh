#!/usr/bin/env bash
# One-shot deploy of jenkins-rca to the EC2 host.
# Service runs directly from the git checkout — no /opt/ copy.
# Run from the cloned Jenkins-rca repo root on the EC2 host.
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/project/Jenkins-rca}"
VENV="$APP_DIR/.venv"
CACHE_DIR="/var/cache/jenkins-rca"
AUDIT_DIR="/var/log/jenkins-rca"
SYSTEMD_DEST="/etc/systemd/system"

echo "==> APP_DIR: $APP_DIR"

# 1. Verify checkout exists
[[ -d "$APP_DIR/jenkins_rca" ]] || { echo "ERROR: $APP_DIR/jenkins_rca not found. Clone the repo first."; exit 1; }

# 2. Writable dirs (ProtectSystem=strict needs these as ReadWritePaths)
sudo mkdir -p "$CACHE_DIR" "$AUDIT_DIR"
sudo chown -R ubuntu:ubuntu "$CACHE_DIR" "$AUDIT_DIR"

# 3. Python venv + deps
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$APP_DIR/jenkins_rca/requirements.txt"

# 4. Start script executable
chmod +x "$APP_DIR/infra/scripts/jenkins-rca-start.sh"

# 5. Systemd unit
sudo cp "$APP_DIR/infra/systemd/jenkins-rca.service" "$SYSTEMD_DEST/"
sudo systemctl daemon-reload
sudo systemctl enable jenkins-rca.service
sudo systemctl restart jenkins-rca.service

echo ""
echo "==> done. status:"
sudo systemctl status jenkins-rca.service --no-pager -l
