"""Wialon IPS 2.0 packet encoding.

Packet shape:  #TYPE#body\r\n
In v2.0 every body ends with ";<CRC16>" (or "|<CRC16>" for black box), where the
CRC is CRC-16/ARC over the body up to and including that last separator,
written as uppercase hex.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping

PROTOCOL_VERSION = "2.0"
NA = "NA"


def _make_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _make_table()


def crc16(data: bytes) -> int:
    """CRC-16/ARC (poly 0x8005 reflected, init 0) — the variant Wialon IPS uses."""
    crc = 0
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


def _with_crc(body: str) -> str:
    return f"{body}{crc16(body.encode()):X}"


def _packet(ptype: str, body: str) -> bytes:
    return f"#{ptype}#{body}\r\n".encode()


# ---------- value formatting ----------

def _fmt_lat(lat: float | None) -> str:
    if lat is None:
        return f"{NA};{NA}"
    hemi = "N" if lat >= 0 else "S"
    lat = abs(lat)
    deg = int(lat)
    return f"{deg:02d}{(lat - deg) * 60:07.4f};{hemi}"


def _fmt_lon(lon: float | None) -> str:
    if lon is None:
        return f"{NA};{NA}"
    hemi = "E" if lon >= 0 else "W"
    lon = abs(lon)
    deg = int(lon)
    return f"{deg:03d}{(lon - deg) * 60:07.4f};{hemi}"


def _fmt(v, fmt: str = "{}") -> str:
    return NA if v is None else fmt.format(v)


def _fmt_param(name: str, value) -> str:
    # type codes: 1 = integer, 2 = double, 3 = string
    if isinstance(value, bool):
        return f"{name}:1:{int(value)}"
    if isinstance(value, int):
        return f"{name}:1:{value}"
    if isinstance(value, float):
        return f"{name}:2:{value}"
    text = str(value).replace(",", " ").replace(";", " ").replace("|", " ")
    return f"{name}:3:{text}"


# ---------- message model ----------

@dataclass
class Position:
    time: datetime
    lat: float | None = None
    lon: float | None = None
    speed: int | None = None       # km/h
    course: int | None = None      # degrees 0-359
    altitude: int | None = None    # metres
    sats: int | None = None
    hdop: float | None = None
    inputs: int | None = None      # bitmask
    outputs: int | None = None     # bitmask
    adc: list[float] = field(default_factory=list)
    ibutton: str | None = None
    params: Mapping[str, int | float | str | bool] = field(default_factory=dict)

    def _common(self) -> str:
        t = self.time.astimezone(timezone.utc)
        return ";".join([
            t.strftime("%d%m%y"),
            t.strftime("%H%M%S"),
            _fmt_lat(self.lat),
            _fmt_lon(self.lon),
            _fmt(self.speed),
            _fmt(self.course),
            _fmt(self.altitude),
            _fmt(self.sats),
        ])

    def short_body(self) -> str:
        return self._common() + ";"

    def full_body(self) -> str:
        return ";".join([
            self._common(),
            _fmt(self.hdop),
            _fmt(self.inputs),
            _fmt(self.outputs),
            ",".join(str(a) for a in self.adc),
            self.ibutton or NA,
            ",".join(_fmt_param(k, v) for k, v in self.params.items()),
        ]) + ";"


# ---------- packet builders ----------

def login(imei: str, password: str | None = None) -> bytes:
    return _packet("L", _with_crc(f"{PROTOCOL_VERSION};{imei};{password or NA};"))


def short_data(pos: Position) -> bytes:
    return _packet("SD", _with_crc(pos.short_body()))


def data(pos: Position) -> bytes:
    return _packet("D", _with_crc(pos.full_body()))


def black_box(positions: Iterable[Position]) -> bytes:
    """Several buffered messages in one packet (use after a reconnect)."""
    # each message body drops its trailing ';' and messages are joined by '|'
    body = "|".join(p.full_body()[:-1] for p in positions) + "|"
    return _packet("B", _with_crc(body))


def ping() -> bytes:
    return b"#P#\r\n"


def driver_message(text: str) -> bytes:
    return _packet("M", _with_crc(text.replace(";", " ") + ";"))


# ---------- responses ----------

RESPONSE_MEANINGS = {
    "AL": {"1": "ok", "0": "rejected", "01": "bad password", "10": "CRC error"},
    "ASD": {"-1": "bad packet structure", "0": "bad time", "1": "ok",
            "10": "bad coordinates", "11": "bad speed/course/altitude",
            "12": "bad satellites", "13": "CRC error"},
    "AD": {"-1": "bad packet structure", "0": "bad time", "1": "ok",
           "10": "bad coordinates", "11": "bad speed/course/altitude",
           "12": "bad satellites/hdop", "13": "bad inputs/outputs",
           "14": "bad adc", "15": "bad params", "16": "CRC error"},
    "AM": {"1": "ok", "0": "error", "01": "CRC error"},
}


@dataclass
class Response:
    ptype: str
    code: str

    @property
    def ok(self) -> bool:
        if self.ptype == "AP":
            return True
        if self.ptype == "AB":
            return self.code.isdigit() and int(self.code) > 0
        return self.code == "1"

    @property
    def meaning(self) -> str:
        if self.ptype == "AB":
            return f"{self.code} message(s) accepted"
        return RESPONSE_MEANINGS.get(self.ptype, {}).get(self.code, self.code)


def parse_response(line: bytes | str) -> Response:
    text = line.decode() if isinstance(line, bytes) else line
    text = text.strip()
    if not text.startswith("#") or text.count("#") < 2:
        raise ValueError(f"not an IPS response: {text!r}")
    _, ptype, code = text.split("#", 2)
    return Response(ptype, code)
