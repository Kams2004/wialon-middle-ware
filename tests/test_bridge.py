import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from wialon_ips import jimi as jimi_mod
from wialon_ips.bridge import BridgeConfig, JimiBridge, to_position
from wialon_ips.client import IPSGateway
from wialon_ips.jimi import JimiClient, fmt_time, sign
from wialon_ips.store import PENDING, REJECTED, SENT, Store

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ts(minutes_ago: float) -> str:
    return fmt_time(NOW - timedelta(minutes=minutes_ago))


# ---------- mapping ----------

def test_tag_track_point_mapping():
    p = to_position({"lat": 4.0227, "lng": 9.703, "gpsTime": "2026-09-30 12:16:29",
                     "direction": 0, "gpsSpeed": -1.0, "posType": 5, "confidence": 3, "gpsMode": 1})
    assert p.time == datetime(2026, 9, 30, 12, 16, 29, tzinfo=timezone.utc)
    assert (p.speed, p.course, p.sats) == (None, None, None)
    assert p.params == {"pos_type": "BEACON", "confidence": 3, "gps_mode": 1}


def test_latest_location_mapping_and_invalid_fix():
    rec = {"imei": "1", "lat": 4.02, "lng": 9.70, "gpsTime": "2026-09-30 12:29:23",
           "posType": "BEACON", "batteryPowerVal": "89.00", "speed": None, "status": "0",
           "accStatus": "0", "confidence": 3}
    p = to_position(rec, latest=True)
    assert p.params == {"pos_type": "BEACON", "confidence": 3, "battery": 89.0, "acc": 0, "online": 0}
    assert to_position({**rec, "lat": 0, "lng": 0}) is None
    assert to_position({**rec, "gpsTime": None}) is None


def test_gps_speed_and_course_kept_for_real_gps():
    p = to_position({"lat": 1, "lng": 1, "gpsTime": "2026-09-30 12:00:00",
                     "gpsSpeed": "42.6", "direction": "370"})
    assert (p.speed, p.course) == (43, 10)


def test_sign_is_md5_of_sorted_params_wrapped_in_secret():
    import hashlib
    assert sign({"b": "2", "a": "1"}, "S") == hashlib.md5(b"Sa1b2S").hexdigest().upper()


# ---------- fakes ----------

class FakeJimi:
    """httpx transport emulating the TrackSolid Pro Open API."""

    def __init__(self):
        self.calls: list[str] = []
        self.locations: list[dict] = []
        self.tracks: dict[str, list[dict]] = {}
        self.fail_refresh = False
        self.rate_limit_next = 0
        self.token_n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        m = p["method"]
        self.calls.append(m)
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(200, json={"code": 1006, "message": "too frequent"})
        if m == "jimi.oauth.token.get" or (m == "jimi.oauth.token.refresh" and not self.fail_refresh):
            self.token_n += 1
            return httpx.Response(200, json={"code": 0, "result": {
                "accessToken": f"tok{self.token_n}", "refreshToken": "r", "expiresIn": "7200"}})
        if m == "jimi.oauth.token.refresh":
            return httpx.Response(200, json={"code": 1004, "message": "refresh token invalid"})
        if m == "jimi.user.device.location.list":
            return httpx.Response(200, json={"code": 0, "result": self.locations})
        if m == "jimi.device.track.list":
            begin, end = p["begin_time"], p["end_time"]
            pts = [t for t in self.tracks.get(p["imei"], []) if begin <= t["gpsTime"] <= end]
            return httpx.Response(200, json={"code": 0, "result": pts})
        return httpx.Response(200, json={"code": 1001, "message": "unknown method"})


class FakeWialon:
    def __init__(self, known: set[str]):
        self.known = known
        self.log: list[tuple[str, str]] = []
        self.refuse_marker: str | None = None   # messages containing this are refused
        self.drop_after_answer = False

    async def handle(self, reader, writer):
        imei = None
        while line := await reader.readline():
            text = line.decode().strip()
            kind, body = text.split("#")[1], text.split("#", 2)[2]
            if kind == "L":
                imei = body.split(";")[1]
                answer = "#AL#1" if imei in self.known else "#AL#0"
            elif kind == "B":
                msgs = body.split("|")[:-1]
                ok = [m for m in msgs if not (self.refuse_marker and self.refuse_marker in m)]
                answer = f"#AB#{len(ok)}"
            elif kind == "D":
                answer = "#AD#0" if self.refuse_marker and self.refuse_marker in body else "#AD#1"
            else:
                answer = "#AP#"
            self.log.append((imei, kind))
            writer.write(answer.encode() + b"\r\n")
            await writer.drain()
            if kind == "L" and answer == "#AL#0" or (self.drop_after_answer and kind != "L"):
                break
        writer.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(jimi_mod.asyncio, "sleep", instant)


def make_jimi(fake: FakeJimi, store=None, clock=None) -> JimiClient:
    kw = {"clock": clock} if clock else {}
    return JimiClient("key", "secret", "acc", "pwmd5", base_url="https://jimi.test/route/rest",
                      token_store=store, transport=httpx.MockTransport(fake.handler), **kw)


# ---------- Jimi client ----------

def test_token_persisted_refreshed_and_recovered(tmp_path):
    async def run():
        fake = FakeJimi()
        store = Store(str(tmp_path / "db"))
        now = [1000.0]
        client = make_jimi(fake, store, clock=lambda: now[0])
        assert await client.access_token() == "tok1"

        # a restarted process reuses the persisted token: no new token request
        client2 = make_jimi(fake, store, clock=lambda: now[0])
        assert await client2.access_token() == "tok1"
        assert fake.calls.count("jimi.oauth.token.get") == 1

        now[0] += 7200 - 300              # close to expiry -> refresh
        assert await client2.access_token() == "tok2"
        assert fake.calls[-1] == "jimi.oauth.token.refresh"

        now[0] += 99999                   # expired and refresh broken -> new token
        fake.fail_refresh = True
        assert await client2.access_token() == "tok3"
        assert fake.calls[-1] == "jimi.oauth.token.get"

        fake.rate_limit_next = 2          # rate limiting is retried
        assert await client2.latest_locations() == []
        await client.close(); await client2.close()
    asyncio.run(run())


# ---------- bridge end to end ----------

def test_bridge_backfills_dedupes_and_waits_for_missing_unit(tmp_path):
    async def run():
        fake = FakeJimi()
        fake.locations = [
            {"imei": "A", "deviceName": "Tag A", "lat": 4.02, "lng": 9.70, "gpsTime": ts(1),
             "posType": "BEACON", "batteryPowerVal": "89.00", "expireFlag": "1"},
            {"imei": "B", "deviceName": "Tag B", "lat": 4.03, "lng": 9.68, "gpsTime": ts(2),
             "posType": "BEACON", "expireFlag": "1"},
            {"imei": "C", "lat": 0, "lng": 0, "gpsTime": ts(2)},              # no fix
        ]
        fake.tracks = {
            "A": [{"lat": 4.02, "lng": 9.70, "gpsTime": ts(m), "gpsSpeed": -1, "posType": 5}
                  for m in (50, 30, 10, 1)],       # ts(1) duplicates the latest point
            "B": [{"lat": 4.03, "lng": 9.68, "gpsTime": ts(20), "gpsSpeed": -1, "posType": 5}],
        }
        wialon = FakeWialon(known={"A"})
        server = await asyncio.start_server(wialon.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        store = Store(str(tmp_path / "db"))
        gw = IPSGateway("127.0.0.1", port, ping_interval=0, timeout=2)
        bridge = JimiBridge(make_jimi(fake), gw, store, BridgeConfig(unit_retry=600))

        assert await bridge.poll_once() == 4 + 2           # A: 4 unique, B: 2
        await bridge.send_pending()
        stats = store.stats()
        assert stats["A"] == {SENT: 4}
        newest = store.db.execute("SELECT payload FROM messages WHERE imei='A' "
                                  "ORDER BY gps_time DESC LIMIT 1").fetchone()[0]
        assert json.loads(newest)["params"]["battery"] == 89.0   # latest record won the dedupe
        assert stats["B"] == {PENDING: 2}
        assert bridge.devices["B"].unit == "not_in_wialon"
        assert ("A", "B") in wialon.log                     # sent as one black box

        # same latest time again -> no Jimi track call, nothing new
        n_track = fake.calls.count("jimi.device.track.list")
        assert await bridge.poll_once() == 0
        assert fake.calls.count("jimi.device.track.list") == n_track

        # unit B is created in Wialon -> its buffered history is delivered on retry
        wialon.known.add("B")
        bridge.devices["B"].next_attempt = 0
        await bridge.send_pending()
        assert store.stats()["B"] == {SENT: 2}
        assert bridge.devices["B"].unit == "ok"

        await gw.stop(); server.close()
    asyncio.run(run())


def test_refused_message_is_isolated_and_stale_connection_recovers(tmp_path):
    async def run():
        fake = FakeJimi()
        fake.locations = [{"imei": "A", "lat": 4.02, "lng": 9.70, "gpsTime": ts(1)}]
        fake.tracks = {"A": [{"lat": -1.5, "lng": 9.7, "gpsTime": ts(5)},   # lat "0130.0000;S"
                             {"lat": 4.02, "lng": 9.70, "gpsTime": ts(3)}]}
        wialon = FakeWialon(known={"A"})
        wialon.refuse_marker = "0130.0000;S"
        server = await asyncio.start_server(wialon.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        store = Store(str(tmp_path / "db"))
        gw = IPSGateway("127.0.0.1", port, ping_interval=0, timeout=2)
        bridge = JimiBridge(make_jimi(fake), gw, store)

        await bridge.poll_once()
        await bridge.send_pending()
        assert store.stats()["A"] == {SENT: 2, REJECTED: 1}

        # Wialon now closes idle connections; the next send must reconnect transparently
        wialon.drop_after_answer = True
        await gw.devices["A"].transmit(b"#P#\r\n")          # server drops us after this
        fake.locations[0]["gpsTime"] = ts(0.5)
        await bridge.poll_once()
        await bridge.send_pending()
        assert store.stats()["A"][SENT] == 3
        assert bridge.devices["A"].unit == "ok"
        await gw.stop(); server.close()
    asyncio.run(run())


def test_network_outage_keeps_data_and_backs_off(tmp_path):
    async def run():
        fake = FakeJimi()
        fake.locations = [{"imei": "A", "lat": 4.02, "lng": 9.70, "gpsTime": ts(1)}]
        store = Store(str(tmp_path / "db"))
        gw = IPSGateway("127.0.0.1", 1, ping_interval=0, timeout=1)   # nothing listens
        bridge = JimiBridge(make_jimi(fake), gw, store)
        await bridge.poll_once()
        await bridge.send_pending()
        st = bridge.devices["A"]
        assert store.stats()["A"] == {PENDING: 1}
        assert st.unit == "network_error" and st.backoff == 5
        await gw.stop()

        # restart: the queue survives and is delivered once Wialon is back
        wialon = FakeWialon(known={"A"})
        server = await asyncio.start_server(wialon.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        store2 = Store(str(tmp_path / "db"))
        gw2 = IPSGateway("127.0.0.1", port, ping_interval=0, timeout=2)
        bridge2 = JimiBridge(make_jimi(fake), gw2, store2)
        await bridge2.send_pending()
        assert store2.stats()["A"] == {SENT: 1}
        await gw2.stop(); server.close()
    asyncio.run(run())
