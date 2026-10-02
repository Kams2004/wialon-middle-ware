#!/usr/bin/env bash
# Print the URL to give Jimi for the Tag push, based on .env.
source "$(dirname "$0")/_common.sh"
val() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
token=$(val WEBHOOK_TOKEN); domain=$(val WEBHOOK_DOMAIN)
if [[ ",$(val COMPOSE_PROFILES)," == *",proxy,"* ]]; then          # Caddy on its own port
  port=$(val PROXY_PORT); port=${port:-8090}
  if [ "$(val CADDY_CONFIG)" = "https" ]; then host="https://$domain:$port"
  else host="http://$(curl -4 -s --max-time 5 ifconfig.me || hostname -I | awk '{print $1}'):$port"; fi
else                                                               # host Apache on 443
  host="https://$domain"
fi
prefix=""; [ -n "$token" ] && prefix="/$token"
echo "Push URL : $host$prefix/api/v1/tag/data/push"
