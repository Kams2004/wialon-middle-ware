"""Jimi -> Wialon bridge.

Positions enter the SQLite outbox in one of two ways:

  webhook Jimi pushes Tag positions to POST /api/v1/tag/data/push; `ingest()`
          validates them and stores them immediately. Jimi does not retry
          failed pushes, so receiving never waits for Wialon.

  hybrid  (JIMI_MODE=hybrid) polls like `poll` while the webhook waits on standby;
          the first push containing one of our Tags (an IMEI already known from
          polling) switches polling off for good (persisted), so the cut-over to
          the webhook needs no redeploy.

  poller  (JIMI_MODE=poll or hybrid) every `poll_interval`: one
          jimi.user.device.location.list call for all devices; for each device
          whose latest gpsTime moved past its cursor, fetch jimi.device.track.list
          since (cursor - overlap) so intermediate points are not lost, and
          enqueue everything (duplicates are ignored).

and a single sender loop delivers the outbox to Wialon:

  sender  drains pending messages per IMEI, oldest first, over Wialon IPS
          (#D# for one message, #B# black box for several). Failures are
          classified so one bad unit or message never blocks the rest:
            - unit unknown to Wialon  -> keep data, retry that unit every `unit_retry` s
            - network problem         -> keep data, exponential backoff per unit
            - message refused         -> mark that message rejected, continue
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import protocol
from .client import IPSGateway, IPSLoginError, IPSNetworkError
from .jimi import JimiClient, JimiError, parse_time
from .protocol import Position
from .store import Store

log = logging.getLogger(__name__)

POS_TYPES = {"1": "GPS", "2": "LBS", "3": "WIFI", "5": "BEACON"}
MAX_TRACK_WINDOW = timedelta(days=7)
MAX_AGE = timedelta(days=30)        # pushed points older than this are refused
MAX_FUTURE = timedelta(days=1)      # ...and points this far in the future


# ---------- Jimi record -> Position ----------

def _num(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def to_position(rec: dict, latest: bool = False) -> Position | None:
    """Convert a Jimi location/track record. Returns None when it has no usable fix."""
    lat, lng = _num(rec.get("lat")), _num(rec.get("lng"))
    t = parse_time(rec.get("gpsTime"))
    if t is None or lat is None or lng is None or (lat == 0 and lng == 0):
        return None

    speed = _num(rec.get("gpsSpeed", rec.get("speed")))
    speed = None if speed is None or speed < 0 else round(speed)   # Tags report -1
    course = _num(rec.get("direction", rec.get("directions")))
    course = None if speed is None or course is None or course < 0 else round(course) % 360

    params: dict[str, int | float | str] = {}
    pos_type = rec.get("posType", rec.get("positionType"))
    if pos_type is not None:
        params["pos_type"] = POS_TYPES.get(str(pos_type), str(pos_type))
    # Tags report their accuracy level (1-3) as "confidence", or as gpsNum in pushes
    conf = _num(rec.get("confidence"))
    if conf is None and pos_type is not None and str(pos_type) in ("5", "BEACON"):
        conf = _num(rec.get("gpsNum"))
    if conf is not None:
        params["confidence"] = int(conf)
    if (mode := rec.get("gpsMode")) is not None:
        params["gps_mode"] = int(mode)   # 0 real-time, 1 re-uploaded
    if latest:
        battery = _num(rec.get("batteryPowerVal")) or _num(rec.get("electQuantity"))
        if battery is not None:
            params["battery"] = battery
        if rec.get("accStatus") not in (None, ""):
            params["acc"] = int(rec["accStatus"] in ("1", "ON"))
        if rec.get("status") not in (None, ""):
            params["online"] = int(rec["status"] == "1")

    return Position(time=t, lat=lat, lon=lng, speed=speed, course=course, params=params)


# ---------- bridge ----------

@dataclass
class DeviceState:
    imei: str
    name: str = ""
    last_gps_time: datetime | None = None
    unit: str = "unknown"          # ok | not_in_wialon | network_error | unknown
    last_error: str | None = None
    next_attempt: float = 0.0
    backoff: float = 0.0
    last_sent_at: datetime | None = None
    last_received_at: datetime | None = None


@dataclass
class BridgeConfig:
    poll_interval: float = 180
    backfill_hours: float = 24         # history fetched for a device seen for the first time
    overlap_minutes: float = 120       # re-read window, catches late re-uploaded points
    batch_size: int = 50
    unit_retry: float = 600            # retry interval for units missing in Wialon
    max_backoff: float = 300
    keep_sent_days: int = 7
    max_pending_days: int = 30


class JimiBridge:
    def __init__(self, jimi: JimiClient | None, gateway: IPSGateway, store: Store,
                 config: BridgeConfig | None = None, mode: str | None = None):
        """`jimi` is only needed for polling; pass None in webhook mode.
        mode: webhook | poll | hybrid (default: poll when `jimi` is given, else webhook)."""
        self._mode = mode or ("poll" if jimi else "webhook")
        self.jimi, self.gateway, self.store = jimi, gateway, store
        self.cfg = config or BridgeConfig()
        self.devices: dict[str, DeviceState] = {}
        self.last_poll_at: datetime | None = None
        self.last_poll_ok_at: datetime | None = None
        self.last_poll_error: str | None = None
        self._poll_failures = 0
        self._last_prune = 0.0
        self._wake = asyncio.Event()
        self._poll_now = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.webhook = {"requests": 0, "accepted": 0, "duplicates": 0, "invalid": 0,
                        "last_at": None}
        self.recent_pushes: deque[dict] = deque(maxlen=20)
        self._warned_unknown: set[str] = set()
        # hybrid: set once Jimi pushes one of our Tags; persisted across restarts
        self.webhook_live_since: str | None = (store.get_json("webhook_live") or {}).get("since")

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def polling_active(self) -> bool:
        if self.jimi is None:
            return False
        return not (self._mode == "hybrid" and self.webhook_live_since)

    def _maybe_go_live(self, imeis) -> None:
        """Hybrid: a push for a Tag we already know from polling means Jimi's webhook is live."""
        if self._mode != "hybrid" or self.webhook_live_since:
            return
        known = [i for i in imeis if self.store.cursor(i) is not None]
        if not known:
            return
        self.webhook_live_since = datetime.now(timezone.utc).isoformat()
        self.store.set_json("webhook_live", {"since": self.webhook_live_since, "first_imei": known[0]})
        log.warning("Jimi webhook is live (first push for our Tag %s): polling the Jimi API is "
                    "now switched off", known[0])

    def allow_imei(self, imei: str, source: str = "manual") -> bool:
        added = self.store.allow([imei], source) > 0
        self._warned_unknown.discard(imei)
        self._wake.set()
        return added

    def reset_webhook_live(self) -> None:
        """Hybrid: resume polling (e.g. if Jimi stopped pushing)."""
        self.webhook_live_since = None
        self.store.set_json("webhook_live", {})
        self._poll_now.set()

    def _state(self, imei: str) -> DeviceState:
        return self.devices.setdefault(imei, DeviceState(imei))

    # ---------- webhook ----------

    def ingest(self, records: list) -> dict:
        """Validate and store pushed records. Returns counts; never talks to Wialon."""
        now = datetime.now(timezone.utc)
        by_imei: dict[str, list[Position]] = {}
        invalid: list[str] = []
        for rec in records:
            imei = str(rec.get("imei") or "").strip() if isinstance(rec, dict) else ""
            if not (imei.isdigit() and 5 <= len(imei) <= 20):
                invalid.append(f"bad imei {imei!r}")
                continue
            try:
                pos = to_position(rec)
            except (TypeError, ValueError, OverflowError, OSError) as e:
                invalid.append(f"{imei}: {e}")
                continue
            if pos is None or not (-90 <= pos.lat <= 90 and -180 <= pos.lon <= 180):
                invalid.append(f"{imei}: no valid position")
                continue
            if not (now - MAX_AGE <= pos.time <= now + MAX_FUTURE):
                invalid.append(f"{imei}: time out of range {pos.time.isoformat()}")
                continue
            by_imei.setdefault(imei, []).append(pos)

        accepted = 0
        for imei, positions in by_imei.items():
            if not self.store.is_allowed(imei) and imei not in self._warned_unknown:
                self._warned_unknown.add(imei)
                log.warning("push for IMEI %s, which is not on the allowlist: kept but NOT sent "
                            "to Wialon (add it with scripts/allow-imei.sh if it is one of our Tags)",
                            imei)
            accepted += self.store.enqueue(imei, positions)
            st = self._state(imei)
            st.last_received_at = now
            latest = max(p.time for p in positions)
            if st.last_gps_time is None or latest > st.last_gps_time:
                st.last_gps_time = latest
        valid = sum(len(v) for v in by_imei.values())
        result = {"received": len(records), "accepted": accepted,
                  "duplicates": valid - accepted, "invalid": len(invalid)}

        self.webhook["requests"] += 1
        self.webhook["last_at"] = now
        for k in ("accepted", "duplicates", "invalid"):
            self.webhook[k] += result[k]
        self.recent_pushes.appendleft({"at": now, **result, "errors": invalid[:10],
                                       "imeis": sorted(by_imei)[:50]})
        if invalid:
            log.warning("webhook: %d invalid record(s), e.g. %s", len(invalid), invalid[:3])
        log.info("webhook: %d received, %d new, %d duplicate, %d invalid",
                 result["received"], accepted, result["duplicates"], len(invalid))
        self._maybe_go_live(by_imei)
        if accepted:
            self._wake.set()
        return result

    def note_unrecognized(self, sender: str, content_type: str, body: bytes) -> None:
        """Record a push that held no positions (e.g. Jimi's "Verify"), for inspection."""
        self.webhook["requests"] += 1
        self.webhook["last_at"] = datetime.now(timezone.utc)
        self.recent_pushes.appendleft({
            "at": self.webhook["last_at"], "received": 0, "accepted": 0, "duplicates": 0,
            "invalid": 0, "imeis": [], "errors": [],
            "unrecognized": {"from": sender, "content_type": content_type,
                             "body": body[:1000].decode(errors="replace")}})

    def housekeeping(self) -> None:
        if time.time() - self._last_prune > 86400:
            self.store.prune(self.cfg.keep_sent_days, self.cfg.max_pending_days)
            self._last_prune = time.time()

    # ---------- polling ----------

    async def poll_once(self) -> int:
        """One polling cycle. Returns the number of new messages queued."""
        self.last_poll_at = datetime.now(timezone.utc)
        locations = await self.jimi.latest_locations()
        new_tags = self.store.allow([l["imei"] for l in locations if l.get("imei")], "jimi")
        if new_tags:
            log.info("%d Tag(s) from the Jimi account added to the allowlist", new_tags)
        queued = 0
        for loc in locations:
            imei = loc.get("imei")
            if not imei:
                continue
            try:
                queued += await self._sync_device(loc)
            except Exception as e:  # one device must never stop the others
                log.exception("[%s] sync failed", imei)
                self._state(imei).last_error = f"jimi sync: {e}"
        self.last_poll_ok_at = datetime.now(timezone.utc)
        self.last_poll_error = None

        if queued:
            self._wake.set()
        return queued

    async def _sync_device(self, loc: dict) -> int:
        imei = loc["imei"]
        st = self._state(imei)
        st.name = loc.get("deviceName") or st.name
        if str(loc.get("expireFlag", "1")) == "0":
            st.last_error = "device subscription expired in Jimi"
            return 0
        latest = to_position(loc, latest=True)
        if latest is None:
            return 0
        st.last_gps_time = latest.time

        cursor = self.store.cursor(imei)
        if cursor and latest.time <= cursor:
            return 0

        now = datetime.now(timezone.utc)
        begin = (cursor - timedelta(minutes=self.cfg.overlap_minutes) if cursor
                 else now - timedelta(hours=self.cfg.backfill_hours))
        end = now - timedelta(seconds=5)   # Jimi wants end_time strictly in the past

        positions: list[Position] = []
        track_ok = True
        try:
            start = begin
            while start < end:
                stop = min(start + MAX_TRACK_WINDOW, end)
                positions += [p for rec in await self.jimi.track(imei, start, stop)
                              if (p := to_position(rec))]
                start = stop
        except JimiError as e:
            track_ok = False
            log.warning("[%s] track history unavailable (%s); sending latest point only", imei, e)

        # latest first: it carries battery/status, and for the same timestamp the
        # first stored record wins (the track copy of that point is then ignored)
        new = self.store.enqueue(imei, [latest, *positions])
        if track_ok:   # otherwise the same window is retried next cycle
            self.store.set_cursor(imei, latest.time)
        if new:
            log.info("[%s] queued %d new position(s) up to %s", imei, new, latest.time.isoformat())
        return new

    async def _poll_loop(self) -> None:
        announced = False
        while True:
            if not self.polling_active:
                if not announced:
                    log.info("polling on standby: data now arrives by webhook")
                    announced = True
                self._poll_now.clear()
                try:
                    await asyncio.wait_for(self._poll_now.wait(), self.cfg.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            announced = False
            try:
                await self.poll_once()
                self._poll_failures = 0
                delay = self.cfg.poll_interval
            except Exception as e:
                self._poll_failures += 1
                self.last_poll_error = str(e)
                delay = min(self.cfg.poll_interval, 30 * 2 ** min(self._poll_failures, 5))
                log.error("jimi poll failed (%d in a row): %s; next try in %.0fs",
                          self._poll_failures, e, delay)
            self._poll_now.clear()
            try:
                await asyncio.wait_for(self._poll_now.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def trigger_poll(self) -> None:
        self._poll_now.set()

    # ---------- sending ----------

    async def drain(self, imei: str) -> None:
        st = self._state(imei)
        conn = self.gateway.device(imei)
        while batch := self.store.pending(imei, self.cfg.batch_size):
            ids = [i for i, _ in batch]
            try:
                if len(batch) == 1:
                    await self._send_single(conn, *batch[0])
                else:
                    resp = await conn.transmit(protocol.black_box([p for _, p in batch]))
                    if resp.ok and int(resp.code) == len(batch):
                        self.store.mark_sent(ids)
                    else:
                        # partial or refused black box: isolate the bad message(s)
                        log.warning("[%s] black box answer %s for %d msgs, sending one by one",
                                    imei, resp.meaning, len(batch))
                        for msg_id, pos in batch:
                            await self._send_single(conn, msg_id, pos)
            except IPSLoginError as e:
                self.store.note_failure(imei, str(e))
                if st.unit != "not_in_wialon":
                    log.warning("[%s] %s — create a Wialon IPS unit with this unique ID; "
                                "data is kept and will be sent once it exists", imei, e)
                st.unit, st.last_error = "not_in_wialon", str(e)
                st.next_attempt = time.monotonic() + self.cfg.unit_retry
                return
            except (IPSNetworkError, ValueError) as e:
                self.store.note_failure(imei, f"network: {e}")
                await conn.close()
                st.backoff = min(max(st.backoff * 2, 5), self.cfg.max_backoff)
                st.unit, st.last_error = "network_error", str(e)
                st.next_attempt = time.monotonic() + st.backoff
                log.warning("[%s] Wialon unreachable (%s), retry in %.0fs", imei, e, st.backoff)
                return
            if st.unit != "ok":
                log.info("[%s] delivering to Wialon", imei)
            st.unit, st.backoff, st.last_error = "ok", 0.0, None
            st.last_sent_at = datetime.now(timezone.utc)

    async def _send_single(self, conn, msg_id: int, pos: Position) -> None:
        resp = await conn.transmit(protocol.data(pos))
        if resp.ok:
            self.store.mark_sent([msg_id])
        else:
            log.warning("[%s] Wialon refused message at %s: %s",
                        conn.imei, pos.time.isoformat(), resp.meaning)
            self.store.mark_rejected(msg_id, f"wialon: {resp.meaning}")

    async def send_pending(self) -> None:
        now = time.monotonic()
        due = [imei for imei in self.store.imeis_with_pending(allowed_only=True)
               if self._state(imei).next_attempt <= now]
        results = await asyncio.gather(*(self.drain(i) for i in due), return_exceptions=True)
        for imei, r in zip(due, results):
            if isinstance(r, Exception):
                log.error("[%s] unexpected send error: %r", imei, r)
                self._state(imei).last_error = repr(r)

    async def _send_loop(self) -> None:
        while True:
            try:
                self.housekeeping()
                await self.send_pending()
            except Exception:
                log.exception("sender cycle failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 15)
            except asyncio.TimeoutError:
                pass

    # ---------- lifecycle / status ----------

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._send_loop(), name="wialon-send")]
        if self.jimi:
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="jimi-poll"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def status(self) -> dict:
        counts = self.store.stats()
        imeis = sorted(set(self.devices) | set(counts))
        return {
            "mode": self.mode,
            "polling_active": self.polling_active,
            "webhook_live_since": self.webhook_live_since,
            "webhook": self.webhook,
            "last_poll_at": self.last_poll_at,
            "last_poll_ok_at": self.last_poll_ok_at,
            "last_poll_error": self.last_poll_error,
            "devices": [{
                "imei": i,
                "name": self._state(i).name,
                "wialon_unit": self._state(i).unit if self.store.is_allowed(i) else "not_allowed",
                "last_gps_time": self._state(i).last_gps_time,
                "last_received_at": self._state(i).last_received_at,
                "last_sent_at": self._state(i).last_sent_at,
                "messages": counts.get(i, {}),
                "last_error": self._state(i).last_error,
            } for i in imeis],
        }
