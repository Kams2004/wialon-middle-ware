#!/usr/bin/env bash
# Ask the bridge to poll Jimi immediately (JIMI_MODE=poll only).
source "$(dirname "$0")/_common.sh"
in_container <<'PY'
print(get("/bridge/poll", "POST"))
PY
