#!/usr/bin/env bash
# Print the URL to give Jimi for the Tag push, based on .env.
source "$(dirname "$0")/_common.sh"
val() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
port=$(val PROXY_PORT); port=${port:-8090}
token=$(val WEBHOOK_TOKEN)
if [ "$(val CADDY_CONFIG)" = "https" ]; then
  host="https://$(val WEBHOOK_DOMAIN):$port"
else
  host="http://$(curl -4 -s --max-time 5 ifconfig.me || hostname -I | awk '{print $1}'):$port"
fi
prefix=""; [ -n "$token" ] && prefix="/$token"
echo "Full push URL : $host$prefix/api/v1/tag/data/push"
echo "Base URL      : $host$prefix      (if Jimi appends /api/v1/tag/data/push itself)"
