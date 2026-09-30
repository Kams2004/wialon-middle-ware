"""Jimi / TrackSolid Pro Open API client.

- Every request is signed: MD5(secret + sorted k+v pairs + secret), uppercase.
- The access token is cached (and persisted through `token_store`) and renewed
  with the refresh token before it expires. Jimi rate-limits token requests
  (code 1006), so a fresh token is only requested when refreshing fails.
- All times sent to and received from the API are UTC ("yyyy-MM-dd HH:mm:ss").
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Protocol

import httpx

log = logging.getLogger(__name__)

TIME_FMT = "%Y-%m-%d %H:%M:%S"
RATE_LIMITED = 1006
TOKEN_ERRORS = {1004, 1005}  # invalid / expired access token
REFRESH_MARGIN = 600         # renew the token when it has less than 10 min left


class JimiError(Exception):
    def __init__(self, code: int, method: str, message: str = ""):
        super().__init__(f"{method} failed: {code} {message}".strip())
        self.code = code
        self.message = message

    @property
    def token_problem(self) -> bool:
        return self.code in TOKEN_ERRORS or "token" in self.message.lower()


class TokenStore(Protocol):
    def get_json(self, key: str) -> dict | None: ...
    def set_json(self, key: str, value: dict) -> None: ...


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TIME_FMT)


def parse_time(value: str | int | float | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, timezone.utc)
    return datetime.strptime(value, TIME_FMT).replace(tzinfo=timezone.utc)


def sign(params: dict, secret: str) -> str:
    raw = secret + "".join(f"{k}{params[k]}" for k in sorted(params)) + secret
    return hashlib.md5(raw.encode()).hexdigest().upper()


class JimiClient:
    def __init__(self, app_key: str, app_secret: str, account: str, password_md5: str,
                 base_url: str = "https://eu-open.tracksolidpro.com/route/rest",
                 token_store: TokenStore | None = None, token_ttl: int = 7200,
                 max_retries: int = 4, clock: Callable[[], float] = time.time,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.app_key, self.app_secret = app_key, app_secret
        self.account, self.password_md5 = account, password_md5
        self.base_url = base_url
        self.token_store = token_store
        self.token_ttl = token_ttl
        self.max_retries = max_retries
        self.clock = clock
        self._http = httpx.AsyncClient(timeout=30, transport=transport)
        self._token: dict | None = token_store.get_json("jimi_token") if token_store else None
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # ---------- transport ----------

    async def _post(self, method: str, params: dict) -> dict:
        body = {k: v for k, v in params.items() if v is not None}
        body.update(method=method, app_key=self.app_key, format="json", sign_method="md5",
                    v="1.0", timestamp=fmt_time(datetime.fromtimestamp(self.clock(), timezone.utc)))
        body["sign"] = sign(body, self.app_secret)

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                r = await self._http.post(self.base_url, data=body)
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt == self.max_retries:
                    raise JimiError(-1, method, f"network error: {e}") from e
                log.warning("jimi %s: %s, retrying in %.0fs", method, e, delay)
            else:
                code = data.get("code", -1)
                if code == 0:
                    return data
                if code != RATE_LIMITED or attempt == self.max_retries:
                    raise JimiError(code, method, data.get("message", ""))
                log.warning("jimi %s rate limited, retrying in %.0fs", method, max(delay, 30))
                delay = max(delay, 30)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            body["timestamp"] = fmt_time(datetime.fromtimestamp(self.clock(), timezone.utc))
            body.pop("sign")
            body["sign"] = sign(body, self.app_secret)
        raise AssertionError("unreachable")

    # ---------- token management ----------

    def _save_token(self, result: dict) -> None:
        self._token = {
            "access": result["accessToken"],
            "refresh": result.get("refreshToken"),
            "expires_at": self.clock() + int(result.get("expiresIn", self.token_ttl)),
        }
        if self.token_store:
            self.token_store.set_json("jimi_token", self._token)

    async def _new_token(self) -> None:
        res = await self._post("jimi.oauth.token.get", {
            "user_id": self.account, "user_pwd_md5": self.password_md5,
            "expires_in": self.token_ttl})
        self._save_token(res["result"])
        log.info("jimi: new access token")

    async def _refresh_token(self) -> None:
        res = await self._post("jimi.oauth.token.refresh", {
            "access_token": self._token["access"], "refresh_token": self._token["refresh"],
            "expires_in": self.token_ttl})
        self._save_token(res["result"])
        log.info("jimi: access token refreshed")

    async def access_token(self, force_new: bool = False) -> str:
        async with self._token_lock:
            now = self.clock()
            if force_new or not self._token or self._token["expires_at"] <= now:
                if self._token and self._token.get("refresh") and not force_new:
                    try:
                        await self._refresh_token()
                        return self._token["access"]
                    except JimiError as e:
                        log.warning("jimi token refresh failed (%s), requesting a new one", e)
                await self._new_token()
            elif self._token["expires_at"] - now < REFRESH_MARGIN and self._token.get("refresh"):
                try:
                    await self._refresh_token()
                except JimiError as e:
                    log.warning("jimi early token refresh failed (%s), keeping current token", e)
            return self._token["access"]

    async def call(self, method: str, **params) -> dict | list | None:
        """Authenticated call; a rejected token triggers exactly one renewal and retry."""
        token = await self.access_token()
        try:
            return (await self._post(method, {"access_token": token, **params})).get("result")
        except JimiError as e:
            if not e.token_problem:
                raise
            log.warning("jimi token rejected (%s), renewing", e)
            async with self._token_lock:
                if self._token and self._token["access"] == token:
                    self._token["expires_at"] = 0  # let access_token() renew it
            token = await self.access_token()
            return (await self._post(method, {"access_token": token, **params})).get("result")

    # ---------- API methods ----------

    async def devices(self) -> list[dict]:
        return await self.call("jimi.user.device.list", target=self.account) or []

    async def latest_locations(self) -> list[dict]:
        return await self.call("jimi.user.device.location.list", target=self.account) or []

    async def track(self, imei: str, begin: datetime, end: datetime) -> list[dict]:
        """Track points in [begin, end]; Jimi allows at most 7 days per request."""
        return await self.call("jimi.device.track.list", imei=imei,
                               begin_time=fmt_time(begin), end_time=fmt_time(end)) or []
