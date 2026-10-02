#!/usr/bin/env bash
# Reload Caddy without downtime so it picks up a renewed certificate.
# Installed as a weekly cron job by scripts/enable-cert-reload.sh.
source "$(dirname "$0")/_common.sh"
$COMPOSE exec -T proxy caddy reload --config "/etc/caddy/$(grep -E '^CADDY_CONFIG=' .env | tail -1 | cut -d= -f2-).Caddyfile" --adapter caddyfile --force
echo "$(date -Is) proxy reloaded"
