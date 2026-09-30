"""Jimi -> Wialon bridge.

Two independent loops share the SQLite outbox:

  poller  every `poll_interval`: one jimi.user.device.location.list call for all
          devices; for each device whose latest gpsTime moved past its cursor,
          fetch jimi.device.track.list since (cursor - overlap) so intermediate
          points are not lost, and enqueue everything (duplicates are ignored).

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
    if (conf := _num(rec.get("confidence"))) is not None:
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
    def __init__(self, jimi: JimiClient, gateway: IPSGateway, store: Store,
                 config: BridgeConfig | None = None):
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

    def _state(self, imei: str) -> DeviceState:
        return self.devices.setdefault(imei, DeviceState(imei))

    # ---------- polling ----------

    async def poll_once(self) -> int:
        """One polling cycle. Returns the number of new messages queued."""
        self.last_poll_at = datetime.now(timezone.utc)
        locations = await self.jimi.latest_locations()
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

        if time.time() - self._last_prune > 86400:
            self.store.prune(self.cfg.keep_sent_days, self.cfg.max_pending_days)
            self._last_prune = time.time()
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
        while True:
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
        due = [imei for imei in self.store.imeis_with_pending()
               if self._state(imei).next_attempt <= now]
        results = await asyncio.gather(*(self.drain(i) for i in due), return_exceptions=True)
        for imei, r in zip(due, results):
            if isinstance(r, Exception):
                log.error("[%s] unexpected send error: %r", imei, r)
                self._state(imei).last_error = repr(r)

    async def _send_loop(self) -> None:
        while True:
            try:
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
        self._tasks = [asyncio.create_task(self._poll_loop(), name="jimi-poll"),
                       asyncio.create_task(self._send_loop(), name="wialon-send")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def status(self) -> dict:
        counts = self.store.stats()
        imeis = sorted(set(self.devices) | set(counts))
        return {
            "last_poll_at": self.last_poll_at,
            "last_poll_ok_at": self.last_poll_ok_at,
            "last_poll_error": self.last_poll_error,
            "devices": [{
                "imei": i,
                "name": self._state(i).name,
                "wialon_unit": self._state(i).unit,
                "last_gps_time": self._state(i).last_gps_time,
                "last_sent_at": self._state(i).last_sent_at,
                "messages": counts.get(i, {}),
                "last_error": self._state(i).last_error,
            } for i in imeis],
        }
