# Wialon IPS middleware

Pushes positions into Wialon over the **Wialon IPS 2.0** TCP protocol, from two sources:

1. **Jimi / TrackSolid Pro bridge**: polls the Jimi Open API for Tag locations and forwards them automatically.
2. **HTTP API**: your own systems `POST /positions`.

Receives positions over HTTP and pushes them to Wialon using the **Wialon IPS 2.0** TCP protocol.

```
your systems ──HTTP JSON──▶ middleware ──TCP #L# / #D# / #B#──▶ 193.193.165.165:20332 (Wialon)
                                │
                                └──Remote API (token → sid)──▶ hst-api.wialon.com   (admin only)
```

## Two separate Wialon interfaces

| | IPS protocol (data in) | Remote API (admin) |
|---|---|---|
| Transport | raw TCP, port 20332 | HTTPS `ajax.html` |
| Auth | unit IMEI + optional unit password | token → `eid` (sid) |
| Used for | pushing positions/sensors | listing units/resources, registering a unit's IMEI |

IPS **does not use the sid**. A message is attached to whichever unit has the IMEI that logged
in on the TCP connection, so every unit must be configured in Wialon with
device type **Wialon IPS** (hw id `96266`) and a unique ID equal to the IMEI you send.

## Deploy on a VPS (Docker)

```bash
# once
git clone https://github.com/Kams2004/wialon-middle-ware.git && cd wialon-middle-ware
./scripts/setup-vps.sh          # installs Docker (log out/in once if it says so)
cp .env.example .env && nano .env   # fill in credentials + API_KEY (openssl rand -hex 32)
./scripts/deploy.sh             # build, start, wait until healthy

# every update
./scripts/deploy.sh             # git pull + rebuild + restart; data volume is kept
```

| Command | Purpose |
|---|---|
| `./scripts/status.sh` | Container state, Jimi poll health, one line per Tag |
| `docker compose logs -f --tail 100` | Live logs |
| `./scripts/poll-now.sh` | Poll Jimi immediately |
| `./scripts/backup-db.sh` | Copy the outbox database to `backups/` |
| `docker compose restart` / `docker compose down` | Restart / stop (data is kept in the `bridge-data` volume) |

- The service listens on `127.0.0.1:8000` of the VPS only. To call it from outside, set
  `BIND_ADDRESS=0.0.0.0` in `.env` (and keep `API_KEY` set), or put a reverse proxy with HTTPS in front.
- Every endpoint except `/health` requires the header `X-API-Key: <API_KEY>`.
- Run **one** instance per Jimi account. Two copies would compete for the Jimi token and send everything twice.
- Outgoing access needed: `eu-open.tracksolidpro.com:443`, `hst-api.wialon.com:443`, `193.193.165.165:20332`.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/uvicorn app:app --port 8000
.venv/bin/pytest -q
```

## 1. Register a unit as an IPS device (once per unit)

```bash
# attach an existing unit
curl -X POST localhost:8000/wialon/units/register -H 'content-type: application/json' \
  -d '{"unit_id": 30541109, "imei": "860000000000001"}'
# or create a new one
curl -X POST localhost:8000/wialon/units/register -H 'content-type: application/json' \
  -d '{"name": "Truck 12", "imei": "860000000000002"}'
```

You can also do this in the Wialon UI: Unit properties → Device type *Wialon IPS* → Unique ID.

## 2. Push data

```bash
curl -X POST localhost:8000/positions -H 'content-type: application/json' -d '{
  "imei": "860000000000001",
  "lat": 4.0511, "lon": 9.7679, "speed": 42, "course": 90, "altitude": 15, "sats": 9,
  "params": {"battery": 87, "temp": 21.5, "driver": "Paul"}
}'
```

Also available: `POST /positions/batch` (list), `GET /devices`, `GET /wialon/units`, `GET /wialon/resources`.

Quick test without the HTTP layer: `.venv/bin/python send_position.py <imei> [lat lon]`.

## Protocol notes (IPS 2.0)

- Login `#L#2.0;IMEI;PASSWORD;CRC\r\n` → `#AL#1` ok, `0` unknown IMEI, `01` bad password, `10` CRC error
- Data `#D#DDMMYY;HHMMSS;lat;N;lon;E;speed;course;alt;sats;hdop;in;out;adc;ibutton;params;CRC`
  → `#AD#1`. Time is UTC, coordinates are `DDMM.MMMM` / `DDDMM.MMMM`, missing values are `NA`.
- Params are `name:type:value` (1 = int, 2 = float, 3 = text) and appear as sensor parameters in Wialon.
- CRC is CRC-16/ARC over the body including the last `;`, in hex. Verified against the live server.
- Failed sends are buffered per IMEI and flushed as a `#B#` black-box packet on reconnect.
  The buffer is in memory, so it is lost on restart.

## Jimi → Wialon bridge

```
            every BRIDGE_POLL_INTERVAL (180 s)
Jimi  ──jimi.user.device.location.list──▶  poller ──▶ SQLite outbox (data/bridge.db) ──▶ sender ──IPS──▶ Wialon
      ◀─jimi.device.track.list (only for Tags with new data, since last sync − 2 h)
```

**Wialon setup:** one unit per Tag, device type **Wialon IPS**, **Unique ID = the Tag's Jimi IMEI**.
A Tag without a unit is not lost: its positions wait in the outbox. The bridge retries every
10 minutes and sends the whole backlog once the unit exists.

**Robustness**

| Situation | What happens |
|---|---|
| Middleware restarts | Outbox, per-Tag sync cursor and Jimi token are in SQLite. Nothing is lost or resent twice, and no new token request is made. |
| Jimi token near expiry / rejected | Refreshed with the refresh token, and a new token is requested only if that fails. |
| Jimi rate limit (1006) / network error | Retried with backoff. A failed poll is retried sooner than the normal interval. |
| Jimi re-uploads older points late | Every sync re-reads 2 h before the cursor, and duplicates are ignored (unique IMEI + time). |
| Middleware was down for a while | The next poll fetches the gap from track history, up to 7-day windows at a time. |
| Tag has no unit in Wialon | Data is kept and the unit is retried every 10 min, without blocking other Tags. |
| Wialon unreachable | Data is kept, with a backoff of 5 s up to 5 min per Tag. |
| Wialon closed an idle connection | Reconnects and resends transparently. |
| Wialon refuses one message | Only that message is marked `rejected`, and the rest continue. |
| Housekeeping | Sent messages are deleted after 7 days. Pending messages older than 30 days are marked `expired`. |

**Data mapping** (Jimi → IPS). Times are UTC on both sides. This was verified against Jimi's Unix timestamps.

| Jimi | Wialon |
|---|---|
| `lat`, `lng` | position (a `0,0` fix is skipped) |
| `gpsTime` | message time |
| `gpsSpeed` / `speed`, `direction` | speed / course (`NA` for Tags, which report `-1`) |
| `posType` | param `pos_type` (GPS/LBS/WIFI/BEACON) |
| `confidence` | param `confidence` (1–3) |
| `gpsMode` | param `gps_mode` (0 real-time, 1 re-uploaded) |
| `batteryPowerVal` | param `battery` (latest point only) |
| `accStatus`, `status` | params `acc`, `online` (latest point only) |

**Monitoring**

- `GET /health`: returns 503 if Jimi polling keeps failing (for uptime checks).
- `GET /bridge/status`: per Tag, its Wialon unit state (`ok` / `not_in_wialon` / `network_error`), last GPS time and message counts.
- `GET /bridge/errors/{imei}`: the latest errors for one Tag.
- `POST /bridge/poll`: poll Jimi now.

**Without Docker:** a systemd unit is in [deploy/wialon-bridge.service](deploy/wialon-bridge.service). Always run a single worker.
