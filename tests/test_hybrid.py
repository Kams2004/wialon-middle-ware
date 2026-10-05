import asyncio
import time

import pytest

from tests.test_bridge import FakeJimi, make_jimi, ts
from wialon_ips import jimi as jimi_mod
from wialon_ips.bridge import JimiBridge
from wialon_ips.client import IPSGateway
from wialon_ips.store import PENDING, Store

TAG = "990901807744578"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(jimi_mod.asyncio, "sleep", instant)


def push(imei: str, minutes_ago: float = 1) -> dict:
    return {"gpsNum": 3, "gpsTime": int((time.time() - minutes_ago * 60) * 1000),
            "imei": imei, "lat": 4.0227, "lng": 9.7029, "positionType": "BEACON"}


def test_hybrid_polls_until_jimi_pushes_one_of_our_tags(tmp_path):
    async def run():
        fake = FakeJimi()
        fake.locations = [{"imei": TAG, "lat": 4.02, "lng": 9.70, "gpsTime": ts(5)}]
        store = Store(str(tmp_path / "db"))
        gw = IPSGateway("127.0.0.1", 1, ping_interval=0)
        bridge = JimiBridge(make_jimi(fake), gw, store, mode="hybrid")

        assert bridge.polling_active
        assert await bridge.poll_once() == 1               # works as before

        bridge.ingest([push("111111111111111")])           # our own test push: no switch
        assert bridge.polling_active

        bridge.ingest([push(TAG, 0.5)])                    # Jimi pushes a real Tag
        assert not bridge.polling_active
        assert store.stats()[TAG] == {PENDING: 2}          # both sources share the outbox

        # restart: the switch is remembered, and the poll loop makes no Jimi call
        bridge2 = JimiBridge(make_jimi(fake), gw, Store(str(tmp_path / "db")), mode="hybrid")
        assert not bridge2.polling_active
        calls = len(fake.calls)
        task = asyncio.create_task(bridge2._poll_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        assert len(fake.calls) == calls

        bridge2.reset_webhook_live()                       # manual resume
        assert bridge2.polling_active
    asyncio.run(run())


def test_poll_and_webhook_modes_are_unchanged(tmp_path):
    store = Store(str(tmp_path / "db"))
    gw = IPSGateway("127.0.0.1", 1, ping_interval=0)
    poll = JimiBridge(make_jimi(FakeJimi()), gw, store)
    assert poll.mode == "poll" and poll.polling_active
    poll.ingest([push(TAG)])
    assert poll.polling_active                             # only hybrid switches itself off
    hook = JimiBridge(None, gw, store)
    assert hook.mode == "webhook" and not hook.polling_active


def test_polled_tags_are_allowed_and_cursors_seed_the_allowlist(tmp_path):
    async def run():
        fake = FakeJimi()
        fake.locations = [{"imei": TAG, "lat": 4.02, "lng": 9.70, "gpsTime": ts(5)}]
        store = Store(str(tmp_path / "db"))
        bridge = JimiBridge(make_jimi(fake), IPSGateway("127.0.0.1", 1, ping_interval=0), store)
        assert not store.is_allowed(TAG)
        await bridge.poll_once()
        assert store.is_allowed(TAG)
        # an existing database (cursors from earlier polling) seeds the list on open
        store.db.execute("DELETE FROM allowed_imeis")
        assert Store(str(tmp_path / "db")).is_allowed(TAG)
    asyncio.run(run())
