#!/usr/bin/env bash
# One-time VPS preparation (Ubuntu/Debian): installs Docker Engine + Compose plugin.
set -euo pipefail
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker
if [ "$(id -u)" -ne 0 ]; then
  sudo usermod -aG docker "$USER"
  echo "Added $USER to the docker group: log out and back in once, then continue."
fi
docker --version && (docker compose version || true)
