"""Async TCP client that speaks Wialon IPS on behalf of one or many devices.

Wialon binds a login to the TCP connection, so every IMEI gets its own
connection. Messages that fail to send are buffered and flushed as a single
black-box (#B#) packet once the connection is back.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque

from . import protocol
from .protocol import Position, Response

log = logging.getLogger(__name__)


class IPSError(Exception):
    pass


class IPSLoginError(IPSError):
    """Wialon refused the login: no unit with this unique ID, or wrong password."""


class IPSNetworkError(IPSError):
    """Could not reach Wialon or the connection broke; safe to retry later."""


class IPSRejected(IPSError):
    """Wialon answered but refused the message itself."""

    def __init__(self, msg: str, response: Response):
        super().__init__(msg)
        self.response = response


NETWORK_ERRORS = (OSError, ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError)


class DeviceConnection:
    def __init__(self, host: str, port: int, imei: str, password: str | None = None,
                 timeout: float = 30.0, max_buffer: int = 5000, black_box_batch: int = 50):
        self.host, self.port = host, port
        self.imei, self.password = imei, password
        self.timeout = timeout
        self.black_box_batch = black_box_batch
        self.buffer: deque[Position] = deque(maxlen=max_buffer)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _request(self, packet: bytes) -> Response:
        assert self._writer and self._reader
        log.debug("[%s] >> %r", self.imei, packet)
        self._writer.write(packet)
        await self._writer.drain()
        line = await asyncio.wait_for(self._reader.readline(), self.timeout)
        if not line:
            raise ConnectionError("server closed the connection")
        log.debug("[%s] << %r", self.imei, line)
        return protocol.parse_response(line)

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout)
        resp = await self._request(protocol.login(self.imei, self.password))
        if not resp.ok:
            await self._close()
            raise IPSLoginError(f"login failed for {self.imei}: {resp.meaning}")
        log.info("[%s] logged in to %s:%s", self.imei, self.host, self.port)

    async def _close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    async def _ensure(self) -> None:
        if not self.connected:
            await self._connect()

    async def _flush_buffer(self) -> None:
        while self.buffer:
            batch = [self.buffer[i] for i in range(min(self.black_box_batch, len(self.buffer)))]
            resp = await self._transmit(protocol.black_box(batch))
            if not resp.ok:
                raise IPSRejected(f"black box rejected: {resp.meaning}", resp)
            for _ in range(int(resp.code)):
                self.buffer.popleft()

    async def _transmit(self, packet: bytes) -> Response:
        """Send one packet, reconnecting once if a reused connection turned out to be dead."""
        reused = self.connected
        try:
            await self._ensure()
            return await self._request(packet)
        except NETWORK_ERRORS as e:
            await self._close()
            if not reused:
                raise IPSNetworkError(str(e) or type(e).__name__) from e
            log.info("[%s] connection went stale (%s), reconnecting", self.imei, e or type(e).__name__)
        try:
            await self._connect()
            return await self._request(packet)
        except NETWORK_ERRORS as e:
            await self._close()
            raise IPSNetworkError(str(e) or type(e).__name__) from e

    async def transmit(self, packet: bytes) -> Response:
        """Send a raw packet and return Wialon's answer (no buffering)."""
        async with self._lock:
            return await self._transmit(packet)

    async def send(self, pos: Position, short: bool = False) -> Response:
        """Send one position. On a network failure it is buffered in memory and the error re-raised."""
        async with self._lock:
            try:
                await self._flush_buffer()
                resp = await self._transmit(protocol.short_data(pos) if short else protocol.data(pos))
            except IPSNetworkError as e:
                self.buffer.append(pos)
                raise IPSNetworkError(f"network error, buffered ({len(self.buffer)} pending): {e}") from e
            if not resp.ok:
                raise IPSRejected(f"message rejected: {resp.meaning}", resp)
            return resp

    async def ping(self) -> None:
        async with self._lock:
            if self.connected:
                try:
                    await self._request(protocol.ping())
                except Exception:
                    await self._close()

    async def close(self) -> None:
        async with self._lock:
            await self._close()


class IPSGateway:
    """Pool of device connections keyed by IMEI."""

    def __init__(self, host: str, port: int = 20332, ping_interval: float = 60.0, **conn_kwargs):
        self.host, self.port = host, port
        self.ping_interval = ping_interval
        self.conn_kwargs = conn_kwargs
        self.devices: dict[str, DeviceConnection] = {}
        self._pinger: asyncio.Task | None = None

    def device(self, imei: str, password: str | None = None) -> DeviceConnection:
        dev = self.devices.get(imei)
        if dev is None:
            dev = self.devices[imei] = DeviceConnection(self.host, self.port, imei, password,
                                                        **self.conn_kwargs)
        elif password is not None:
            dev.password = password
        return dev

    async def send(self, imei: str, pos: Position, password: str | None = None,
                   short: bool = False) -> Response:
        return await self.device(imei, password).send(pos, short=short)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            await asyncio.gather(*(d.ping() for d in list(self.devices.values())))

    def start(self) -> None:
        if self.ping_interval and not self._pinger:
            self._pinger = asyncio.create_task(self._ping_loop())

    async def stop(self) -> None:
        if self._pinger:
            self._pinger.cancel()
            self._pinger = None
        await asyncio.gather(*(d.close() for d in self.devices.values()))
