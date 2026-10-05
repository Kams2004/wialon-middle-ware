import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import app as app_module
from tests.test_bridge import FakeWialon
from wialon_ips.bridge import JimiBridge
from wialon_ips.client import IPSGateway
from wialon_ips.store import PENDING, SENT, Store

URL = "/api/v1/tag/data/push"
IMEI = "990901807744578"


def point(minutes_ago: float = 5, imei: str = IMEI, **kw) -> dict:
    """A record shaped exactly like Jimi's documented push example."""
    return {"gpsNum": 3, "gpsTime": int((time.time() - minutes_ago * 60) * 1000),
            "imei": imei, "lat": 4.022735, "lng": 9.702963, "positionType": "BEACON", **kw}


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "db"))
    bridge = JimiBridge(None, IPSGateway("127.0.0.1", 1, ping_interval=0), store)
    monkeypatch.setattr(app_module, "bridge", bridge)
    monkeypatch.setattr(app_module.settings, "webhook_token", "")
    monkeypatch.setattr(app_module.settings, "api_key", "")
    yield TestClient(app_module.app)     # no `with`: the real lifespan is not started
    store.close()


def test_documented_payload_is_stored_and_acknowledged(client):
    r = client.post(URL, json=[point(10), point(4)])
    assert r.status_code == 200 and r.json() == {"code": 200, "msg": "success"}
    store = app_module.bridge.store
    assert store.stats() == {IMEI: {PENDING: 2}}
    payload = json.loads(store.db.execute("SELECT payload FROM messages LIMIT 1").fetchone()[0])
    assert payload["params"] == {"pos_type": "BEACON", "confidence": 3}
    assert app_module.bridge.mode == "webhook"


def test_duplicates_and_invalid_records_do_not_fail_the_push(client):
    p = point(3)
    client.post(URL, json=[p])
    r = client.post(URL, json=[
        p,                                       # duplicate
        point(2, imei="not-an-imei"),            # bad imei
        point(2, lat=0, lng=0),                  # no fix
        point(60 * 24 * 40),                     # 40 days old
        point(-60 * 24 * 3),                     # 3 days in the future
        point(1),                                # valid
    ])
    assert r.json()["code"] == 200
    assert app_module.bridge.store.stats()[IMEI] == {PENDING: 2}
    last = app_module.bridge.recent_pushes[0]
    assert (last["accepted"], last["duplicates"], last["invalid"]) == (1, 1, 4)


def test_wrapped_and_single_object_payloads(client):
    assert client.post(URL, json={"data": [point(3)]}).json()["code"] == 200
    assert client.post(URL, json=point(2)).json()["code"] == 200
    assert app_module.bridge.store.stats()[IMEI] == {PENDING: 2}


def test_verification_requests_get_success_and_are_recorded(client):
    # Jimi's "Verify" button: unknown shape, so every variant must answer success
    assert client.get(URL).json() == {"code": 200, "msg": "success"}
    assert client.head(URL).status_code == 200
    r = client.post(URL, content=b"", headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json()["code"] == 200
    assert client.post(URL, content=b"not json").json()["code"] == 200
    assert client.post(URL, json={"hello": 1}).json()["code"] == 200
    assert client.post(URL, json=[]).json()["code"] == 200
    last = app_module.bridge.recent_pushes[1]           # {"hello": 1}
    assert last["unrecognized"]["body"] == '{"hello":1}'
    assert app_module.bridge.store.stats() == {}        # nothing stored


def test_form_encoded_push_with_json_field(client):
    body = "data=" + json.dumps([point(3)])
    r = client.post(URL, content=body.encode(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.json()["code"] == 200
    assert app_module.bridge.store.stats()[IMEI] == {PENDING: 1}


def test_webhook_token_in_query_or_path(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "webhook_token", "t0k")
    assert client.post(URL, json=[point(3)]).status_code == 401
    assert client.post(URL + "?token=bad", json=[point(3)]).status_code == 401
    assert client.post(URL + "?token=t0k", json=[point(3)]).json()["code"] == 200
    assert client.post("/t0k" + URL, json=[point(2)]).json()["code"] == 200
    assert client.post("/bad" + URL, json=[point(1)]).status_code == 401


def test_webhook_bypasses_api_key_but_admin_endpoints_do_not(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "api_key", "k")
    assert client.post(URL, json=[point(3)]).json()["code"] == 200
    assert client.get("/bridge/status").status_code == 401
    assert client.get("/bridge/status", headers={"X-API-Key": "k"}).json()["mode"] == "webhook"


def test_pushed_points_are_delivered_to_wialon(tmp_path):
    async def run():
        wialon = FakeWialon(known={IMEI})
        server = await asyncio.start_server(wialon.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        store = Store(str(tmp_path / "db"))
        gw = IPSGateway("127.0.0.1", port, ping_interval=0, timeout=2)
        wialon.known.add("123456789012345")                   # someone else's unit on the hosting
        bridge = JimiBridge(None, gw, store)
        bridge.ingest([point(9), point(6), point(3), point(5, imei="123456789012345")])

        # nothing is sent for IMEIs that are not ours, even if Wialon would accept them
        await bridge.send_pending()
        assert store.stats() == {IMEI: {PENDING: 3}, "123456789012345": {PENDING: 1}}
        assert wialon.log == []
        assert bridge.status()["devices"][0]["wialon_unit"] == "not_allowed"

        bridge.allow_imei(IMEI)                               # one of our Tags
        await bridge.send_pending()
        assert store.stats()[IMEI] == {SENT: 3}
        assert store.stats()["123456789012345"] == {PENDING: 1}    # still held
        assert (IMEI, "B") in wialon.log
        assert all(i == IMEI for i, _ in wialon.log)
        await gw.stop(); server.close()
    asyncio.run(run())
