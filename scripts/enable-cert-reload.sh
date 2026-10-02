#!/usr/bin/env bash
# Add a weekly cron job (current user) that reloads the proxy, so a certificate
# renewed by certbot is used without restarting anything. Safe to run again;
# other cron entries are kept.
source "$(dirname "$0")/_common.sh"
dir="$(pwd)"
line="17 4 * * 1 $dir/scripts/reload-proxy.sh >> $dir/reload-proxy.log 2>&1"
current="$(crontab -l 2>/dev/null || true)"          # empty when the user has no crontab yet
{ printf '%s\n' "$current" | grep -v -e 'scripts/reload-proxy.sh' -e '^$' || true; echo "$line"; } | crontab -
echo "cron installed:"
crontab -l | grep reload-proxy
