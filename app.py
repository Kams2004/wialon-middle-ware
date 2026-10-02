"""HTTP middleware: accepts JSON positions and forwards them to Wialon over IPS.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from wialon_ips.bridge import BridgeConfig, JimiBridge
from wialon_ips.client import IPSError, IPSGateway
from wialon_ips.jimi import JimiClient
from wialon_ips.protocol import Position
from wialon_ips.remote_api import WialonAPIError, WialonRemoteAPI
from wialon_ips.store import Store


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    wialon_token: str = ""
    wialon_base_url: str = "https://hst-api.wialon.com"
    wialon_ips_host: str = "193.193.165.165"   # hw_gw_ip from token/login
    wialon_ips_port: int = 20332
    ips_ping_interval: float = 60.0
    log_level: str = "INFO"
    api_key: str = ""                # when set, every endpoint except /health needs X-API-Key

    # Jimi / TrackSolid Pro bridge
    #   webhook: Jimi pushes to POST /api/v1/tag/data/push (no Jimi API calls)
    #   poll:    poll the Jimi Open API (needs JIMI_APP_KEY etc.)
    #   hybrid:  poll, with the webhook on standby; the first push for one of our
    #            Tags switches polling off automatically
    #   off:     bridge disabled
    jimi_mode: str = "webhook"
    webhook_token: str = ""          # if set, Jimi's URL must carry it (see README)
    webhook_max_body: int = 2_000_000
    jimi_base_url: str = "https://eu-open.tracksolidpro.com/route/rest"
    jimi_app_key: str = ""
    jimi_app_secret: str = ""
    jimi_account: str = ""
    jimi_password_md5: str = ""      # lowercase MD5 of the account password
    jimi_password: str = ""          # or the plain password
    bridge_db_path: str = "data/bridge.db"
    bridge_poll_interval: float = 180
    bridge_backfill_hours: float = 24
    bridge_overlap_minutes: float = 120

    @property
    def bridge_enabled(self) -> bool:
        return self.jimi_mode in ("webhook", "poll", "hybrid")


settings = Settings()
logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
gateway = IPSGateway(settings.wialon_ips_host, settings.wialon_ips_port,
                     ping_interval=settings.ips_ping_interval)
api = WialonRemoteAPI(settings.wialon_token, settings.wialon_base_url) if settings.wialon_token else None
bridge: JimiBridge | None = None


def _build_bridge() -> JimiBridge:
    Path(settings.bridge_db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(settings.bridge_db_path)
    jimi = None
    if settings.jimi_mode in ("poll", "hybrid"):
        if not (settings.jimi_app_key and settings.jimi_account):
            raise RuntimeError(f"JIMI_MODE={settings.jimi_mode} needs JIMI_APP_KEY, JIMI_APP_SECRET, JIMI_ACCOUNT")
        jimi = JimiClient(
            settings.jimi_app_key, settings.jimi_app_secret, settings.jimi_account,
            settings.jimi_password_md5 or hashlib.md5(settings.jimi_password.encode()).hexdigest(),
            base_url=settings.jimi_base_url, token_store=store)
    return JimiBridge(jimi, gateway, store, BridgeConfig(
        poll_interval=settings.bridge_poll_interval,
        backfill_hours=settings.bridge_backfill_hours,
        overlap_minutes=settings.bridge_overlap_minutes), mode=settings.jimi_mode)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global bridge
    gateway.start()
    if settings.bridge_enabled:
        bridge = _build_bridge()
        bridge.start()
        logging.getLogger(__name__).info("bridge started in %s mode", bridge.mode)
    yield
    if bridge:
        await bridge.stop()
        if bridge.jimi:
            await bridge.jimi.close()
        bridge.store.close()
    await gateway.stop()
    if api:
        await api.logout()


app = FastAPI(title="Wialon IPS middleware", lifespan=lifespan)

OPEN_PATHS = {"/health", "/docs", "/openapi.json"}
WEBHOOK_PATH = "/api/v1/tag/data/push"


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    # the Jimi webhook cannot send our API key; it has its own optional token
    path = request.url.path.rstrip("/")
    if settings.api_key and path not in OPEN_PATHS and not path.endswith(WEBHOOK_PATH):
        given = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(given.encode(), settings.api_key.encode()):
            return JSONResponse({"detail": "missing or invalid X-API-Key"}, status_code=401)
    return await call_next(request)


class PositionIn(BaseModel):
    imei: str = Field(..., description="Unique ID of the unit in Wialon")
    password: str | None = None
    time: datetime | None = Field(None, description="Fix time; defaults to now (UTC)")
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    speed: int | None = Field(None, ge=0)
    course: int | None = Field(None, ge=0, le=359)
    altitude: int | None = None
    sats: int | None = Field(None, ge=0)
    hdop: float | None = None
    inputs: int | None = None
    outputs: int | None = None
    adc: list[float] = []
    ibutton: str | None = None
    params: dict[str, int | float | str | bool] = {}
    short: bool = Field(False, description="Send #SD# (position only) instead of #D#")

    def to_position(self) -> Position:
        return Position(
            time=self.time or datetime.now(timezone.utc),
            **self.model_dump(exclude={"imei", "password", "time", "short"}),
        )


@app.post("/positions")
async def push_position(p: PositionIn):
    try:
        resp = await gateway.send(p.imei, p.to_position(), password=p.password, short=p.short)
    except IPSError as e:
        raise HTTPException(502, str(e))
    return {"status": "ok", "wialon": resp.meaning}


@app.post("/positions/batch")
async def push_batch(items: list[PositionIn]):
    results = []
    for p in items:
        try:
            resp = await gateway.send(p.imei, p.to_position(), password=p.password, short=p.short)
            results.append({"imei": p.imei, "status": "ok", "wialon": resp.meaning})
        except IPSError as e:
            results.append({"imei": p.imei, "status": "error", "error": str(e)})
    return results


@app.get("/devices")
async def devices():
    return [{"imei": d.imei, "connected": d.connected, "buffered": len(d.buffer)}
            for d in gateway.devices.values()]


# ---------- Jimi webhook ----------

def _webhook_reply(code: int, msg: str) -> JSONResponse:
    return JSONResponse({"code": code, "msg": msg}, status_code=code)


def _records(payload) -> list | None:
    """Jimi sends a JSON array; also accept {"data": [...]} or a single object."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "list", "records"):
            if isinstance(payload.get(key), list):
                return payload[key]
        if "imei" in payload:
            return [payload]
    return None


@app.post(WEBHOOK_PATH)
@app.post("/{token}" + WEBHOOK_PATH)
async def jimi_tag_push(request: Request, token: str | None = None):
    """Receiver for Jimi's Tag location push. Stores and answers at once (Jimi does not retry)."""
    log = logging.getLogger("webhook")
    if not bridge:
        return _webhook_reply(503, "bridge disabled")
    if settings.webhook_token:
        given = token or request.query_params.get("token", "")
        if not secrets.compare_digest(given.encode(), settings.webhook_token.encode()):
            log.warning("push with missing/wrong token from %s", request.client.host if request.client else "?")
            return _webhook_reply(401, "unauthorized")

    body = await request.body()
    if len(body) > settings.webhook_max_body:
        log.error("push of %d bytes refused (limit %d)", len(body), settings.webhook_max_body)
        return _webhook_reply(413, "payload too large")
    try:
        records = _records(json.loads(body))
    except ValueError:
        records = None
    if records is None:
        log.error("unreadable push from %s: %r", request.client.host if request.client else "?",
                  body[:500])
        return _webhook_reply(400, "invalid payload")
    try:
        bridge.ingest(records)
    except Exception:
        log.exception("could not store push (%d records)", len(records))
        return _webhook_reply(500, "storage error")
    return _webhook_reply(200, "success")


# ---------- Jimi bridge ----------

@app.get("/health")
async def health():
    ok = True
    info: dict = {"bridge": "disabled"}
    if bridge and not bridge.polling_active:
        info = {"bridge": "running", "mode": bridge.mode, "polling": False,
                "last_push_at": bridge.webhook["last_at"]}
    elif bridge:
        stale = (bridge.last_poll_ok_at is None or
                 (datetime.now(timezone.utc) - bridge.last_poll_ok_at).total_seconds()
                 > 3 * settings.bridge_poll_interval + 60)
        ok = not (stale and bridge.last_poll_error)
        info = {"bridge": "running", "mode": bridge.mode, "polling": True,
                "last_poll_ok_at": bridge.last_poll_ok_at,
                "last_poll_error": bridge.last_poll_error,
                "last_push_at": bridge.webhook["last_at"]}
    if not ok:
        raise HTTPException(503, info)
    return {"status": "ok", **info}


def _require_bridge() -> JimiBridge:
    if not bridge:
        raise HTTPException(503, "Jimi bridge is not configured (JIMI_APP_KEY / JIMI_ACCOUNT)")
    return bridge


@app.get("/bridge/status")
async def bridge_status():
    return _require_bridge().status()


@app.post("/bridge/poll")
async def bridge_poll():
    """Poll Jimi now instead of waiting for the next cycle (poll mode only)."""
    b = _require_bridge()
    if not b.polling_active:
        raise HTTPException(409, "polling is off; Jimi pushes data itself")
    b.trigger_poll()
    return {"status": "poll triggered"}


@app.post("/bridge/polling/resume")
async def bridge_polling_resume():
    """Hybrid mode: switch polling back on after the webhook went live."""
    b = _require_bridge()
    if b.mode != "hybrid":
        raise HTTPException(409, "only available with JIMI_MODE=hybrid")
    b.reset_webhook_live()
    return {"status": "polling resumed"}


@app.get("/bridge/webhook/recent")
async def bridge_recent_pushes():
    """The last 20 pushes received from Jimi (counts, IMEIs, validation errors)."""
    return list(_require_bridge().recent_pushes)


@app.get("/bridge/errors/{imei}")
async def bridge_errors(imei: str):
    return _require_bridge().store.recent_errors(imei, 20)


# ---------- admin endpoints (Remote API) ----------

def _require_api() -> WialonRemoteAPI:
    if not api:
        raise HTTPException(503, "WIALON_TOKEN is not configured")
    return api


@app.get("/wialon/units")
async def wialon_units():
    try:
        return await _require_api().units()
    except WialonAPIError as e:
        raise HTTPException(502, str(e))


@app.get("/wialon/resources")
async def wialon_resources():
    try:
        return await _require_api().resources()
    except WialonAPIError as e:
        raise HTTPException(502, str(e))


class UnitRegistration(BaseModel):
    imei: str
    unit_id: int | None = Field(None, description="Existing unit to attach; omit to create one")
    name: str | None = None
    password: str | None = None


@app.post("/wialon/units/register")
async def register_unit(r: UnitRegistration):
    """Make a unit a 'Wialon IPS' device with this IMEI so the middleware can log in as it."""
    a = _require_api()
    try:
        if r.unit_id:
            hw = await a.hw_type_id()
            await a.set_device(r.unit_id, r.imei, hw["id"])
            unit_id = r.unit_id
        else:
            if not r.name:
                raise HTTPException(422, "name is required when creating a unit")
            unit_id = (await a.create_ips_unit(r.name, r.imei))["id"]
        if r.password:
            await a.set_password(unit_id, r.password)
    except WialonAPIError as e:
        raise HTTPException(502, str(e))
    return {"unit_id": unit_id, "imei": r.imei}
