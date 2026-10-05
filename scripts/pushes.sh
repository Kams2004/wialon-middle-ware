#!/usr/bin/env bash
# The last 20 pushes received from Jimi: time, counts, IMEIs, validation errors.
source "$(dirname "$0")/_common.sh"
in_container <<'PY'
pushes = get("/bridge/webhook/recent")
if not pushes:
    print("No push received from Jimi since the last restart.")
for p in pushes:
    print(p["at"][:19].replace("T", " "), "UTC |", "received", p["received"], "| new", p["accepted"],
          "| dup", p["duplicates"], "| invalid", p["invalid"], "|", ", ".join(p["imeis"][:5]),
          "..." if len(p["imeis"]) > 5 else "")
    for e in p["errors"]:
        print("    invalid:", e)
    if p.get("unrecognized"):
        u = p["unrecognized"]
        print("    no positions (verification?) from", u["from"], "|", u["content_type"] or "no content-type")
        print("    body:", u["body"] or "(empty)")
PY
