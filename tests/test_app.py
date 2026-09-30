from fastapi.testclient import TestClient

import app as app_module


def test_api_key_guards_everything_but_health(monkeypatch):
    monkeypatch.setattr(app_module.settings, "api_key", "s3cret")
    client = TestClient(app_module.app)          # no `with`: lifespan (bridge) not started
    assert client.get("/health").status_code == 200
    assert client.get("/devices").status_code == 401
    assert client.get("/devices", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/devices", headers={"X-API-Key": "s3cret"}).status_code == 200
