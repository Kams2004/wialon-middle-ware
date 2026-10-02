#!/usr/bin/env bash
# Add a weekly cron job (current user) that reloads the proxy, so a certificate
# renewed by certbot is used without restarting anything. Safe to run again.
source "$(dirname "$0")/_common.sh"
dir="$(pwd)"
line="17 4 * * 1 $dir/scripts/reload-proxy.sh >> $dir/data-reload.log 2>&1"
( crontab -l 2>/dev/null | grep -v "scripts/reload-proxy.sh"; echo "$line" ) | crontab -
echo "cron installed:"; crontab -l | grep reload-proxy
