"""Wialon Remote API client (token -> sid) for admin tasks.

Not needed to push data over IPS. Use it to look up units and to register a
unit as a "Wialon IPS" device with the IMEI the middleware will log in with.
"""
from __future__ import annotations

import asyncio
import json

import httpx

INVALID_SESSION = 1


class WialonAPIError(Exception):
    def __init__(self, code: int, svc: str, reason: str = ""):
        super().__init__(f"{svc} failed: error {code} {reason}".strip())
        self.code = code


class WialonRemoteAPI:
    def __init__(self, token: str, base_url: str = "https://hst-api.wialon.com"):
        self.token = token
        self.url = base_url.rstrip("/") + "/wialon/ajax.html"
        self.sid: str | None = None
        self.user: dict | None = None
        self.login_info: dict | None = None
        self._http = httpx.AsyncClient(timeout=30)
        self._login_lock = asyncio.Lock()

    async def _raw(self, svc: str, params: dict, sid: str | None) -> dict | list:
        # POST keeps the token and sid out of URLs and access logs
        data = {"svc": svc, "params": json.dumps(params)}
        if sid:
            data["sid"] = sid
        r = await self._http.post(self.url, data=data)
        r.raise_for_status()
        body = r.json()
        if isinstance(body, dict) and "error" in body and body["error"] != 0:
            raise WialonAPIError(body["error"], svc, body.get("reason", ""))
        return body

    async def login(self) -> str:
        async with self._login_lock:
            info = await self._raw("token/login", {"token": self.token}, None)
            self.sid = info["eid"]
            self.user = info["user"]
            self.login_info = info
            return self.sid

    async def call(self, svc: str, params: dict | None = None) -> dict | list:
        """Call a service, logging in (again) when the session is missing or expired."""
        if not self.sid:
            await self.login()
        try:
            return await self._raw(svc, params or {}, self.sid)
        except WialonAPIError as e:
            if e.code != INVALID_SESSION:
                raise
            await self.login()
            return await self._raw(svc, params or {}, self.sid)

    async def logout(self) -> None:
        if self.sid:
            try:
                await self._raw("core/logout", {}, self.sid)
            finally:
                self.sid = None
        await self._http.aclose()

    # ---------- helpers ----------

    @property
    def ips_host(self) -> str | None:
        """Hardware gateway for this account (the `hw_gw_ip` from the login response)."""
        return (self.login_info or {}).get("hw_gw_ip")

    async def search(self, items_type: str, flags: int = 1, mask: str = "*") -> list[dict]:
        res = await self.call("core/search_items", {
            "spec": {"itemsType": items_type, "propName": "sys_name",
                     "propValueMask": mask, "sortType": "sys_name"},
            "force": 1, "flags": flags, "from": 0, "to": 0,
        })
        return res["items"]

    async def resources(self) -> list[dict]:
        return await self.search("avl_resource")

    async def units(self) -> list[dict]:
        return await self.search("avl_unit", flags=1 | 256 | 1024)

    async def hw_type_id(self, name: str = "Wialon IPS") -> dict:
        res = await self.call("core/get_hw_types", {
            "filterType": "name", "filterValue": [name], "includeType": True})
        if not res:
            raise LookupError(f"hardware type {name!r} not found")
        return res[0]

    async def set_device(self, unit_id: int, imei: str, hw_type_id: int) -> dict:
        """Attach an existing unit to the Wialon IPS device type with this IMEI."""
        return await self.call("unit/update_device_type", {
            "itemId": unit_id, "deviceTypeId": hw_type_id, "uniqueId": imei})

    async def set_password(self, unit_id: int, password: str) -> dict:
        return await self.call("unit/update_access_password", {
            "itemId": unit_id, "accessPassword": password})

    async def create_ips_unit(self, name: str, imei: str) -> dict:
        hw = await self.hw_type_id()
        if not self.user:
            await self.login()
        unit = await self.call("core/create_unit", {
            "creatorId": self.user["id"], "name": name, "hwTypeId": hw["id"], "dataFlags": 1})
        await self.set_device(unit["item"]["id"], imei, hw["id"])
        return unit["item"]
