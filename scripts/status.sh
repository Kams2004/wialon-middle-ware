#!/usr/bin/env bash
# Summary of the bridge: mode, webhook/poll health, one line per Tag.
source "$(dirname "$0")/_common.sh"
$COMPOSE ps
in_container <<'PY'
s = get("/bridge/status")
print()
if s["mode"] == "webhook":
    w = s["webhook"]
    print("mode: webhook | last push from Jimi:", w["last_at"] or "none yet",
          "| pushes:", w["requests"], "| points new:", w["accepted"],
          "dup:", w["duplicates"], "invalid:", w["invalid"])
else:
    print("mode: poll | last Jimi poll OK:", s["last_poll_ok_at"], "| poll error:", s["last_poll_error"])
print()
row = "{:17} {:22} {:14} {:20} {:>6} {:>8} {:>4}"
print(row.format("IMEI", "NAME", "WIALON UNIT", "LAST GPS (UTC)", "SENT", "PENDING", "REJ"))
for d in s["devices"]:
    m = d["messages"]
    print(row.format(d["imei"], (d["name"] or "")[:22], d["wialon_unit"],
                     (d["last_gps_time"] or "-")[:19].replace("T", " "),
                     m.get("sent", 0), m.get("pending", 0), m.get("rejected", 0)))
PY
