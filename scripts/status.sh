#!/usr/bin/env bash
# Summary of the bridge: poll health + one line per Tag.
source "$(dirname "$0")/_common.sh"
$COMPOSE ps
in_container <<'PY'
h = get("/health")
s = get("/bridge/status")
print()
print("health:", h["status"], "| last Jimi poll OK:", s["last_poll_ok_at"], "| poll error:", s["last_poll_error"])
print()
row = "{:17} {:22} {:14} {:20} {:>6} {:>8} {:>4}"
print(row.format("IMEI", "NAME", "WIALON UNIT", "LAST GPS (UTC)", "SENT", "PENDING", "REJ"))
for d in s["devices"]:
    m = d["messages"]
    print(row.format(d["imei"], (d["name"] or "")[:22], d["wialon_unit"],
                     (d["last_gps_time"] or "-")[:19].replace("T", " "),
                     m.get("sent", 0), m.get("pending", 0), m.get("rejected", 0)))
PY
