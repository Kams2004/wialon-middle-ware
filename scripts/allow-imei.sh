#!/usr/bin/env bash
# Manage the IMEIs that may be sent to Wialon.
#   ./scripts/allow-imei.sh                 list allowed IMEIs
#   ./scripts/allow-imei.sh <IMEI>          allow (its kept positions are then sent)
#   ./scripts/allow-imei.sh --remove <IMEI> stop sending this IMEI
source "$(dirname "$0")/_common.sh"
if [ $# -eq 0 ]; then
  in_container <<'PY'
for a in get("/bridge/allowed"):
    print(a["imei"], " ", a["source"], " ", a["added_at"][:19])
PY
elif [ "$1" = "--remove" ] && [ -n "${2:-}" ]; then
  ARG="$2" in_container <<'PY'
import os
print(get("/bridge/allowed/" + os.environ["ARG"], "DELETE"))
PY
else
  ARG="$1" in_container <<'PY'
import os
print(get("/bridge/allowed/" + os.environ["ARG"], "POST"))
PY
fi
