import asyncio
from datetime import datetime, timezone

import pytest

from wialon_ips import protocol
from wialon_ips.client import DeviceConnection, IPSError
from wialon_ips.protocol import Position, crc16, parse_response

T = datetime(2026, 9, 29, 14, 5, 9, tzinfo=timezone.utc)


def test_crc16_arc_check_value():
    assert crc16(b"123456789") == 0xBB3D


def test_login_packet():
    pkt = protocol.login("123456789012345")
    body = "2.0;123456789012345;NA;"
    assert pkt == f"#L#{body}{crc16(body.encode()):X}\r\n".encode()


def test_coordinates_and_short_data():
    pos = Position(time=T, lat=4.0511, lon=-9.7679, speed=42, course=90, altitude=15, sats=9)
    assert pos.short_body() == "290926;140509;0403.0660;N;00946.0740;W;42;90;15;9;"
    assert protocol.short_data(pos).startswith(b"#SD#290926;140509;")


def test_missing_values_are_na():
    pos = Position(time=T)
    assert pos.short_body() == "290926;140509;NA;NA;NA;NA;NA;NA;NA;NA;"


def test_full_data_params():
    pos = Position(time=T, lat=-1.5, lon=30.25, hdop=0.9, inputs=1, outputs=0, adc=[12.5],
                   params={"battery": 87, "temp": 21.5, "driver": "Paul", "ign": True})
    body = pos.full_body()
    assert body.startswith("290926;140509;0130.0000;S;03015.0000;E;")
    assert body.endswith(";0.9;1;0;12.5;NA;battery:1:87,temp:2:21.5,driver:3:Paul,ign:1:1;")


def test_black_box_joins_with_pipe():
    p = Position(time=T, lat=1.0, lon=1.0)
    pkt = protocol.black_box([p, p]).decode()
    inner = pkt[len("#B#"):-2]
    body, crc = inner.rsplit("|", 1)
    assert body.count("|") == 1
    assert crc == f"{crc16((body + '|').encode()):X}"


def test_parse_response():
    assert parse_response(b"#AL#1\r\n").ok
    r = parse_response("#AL#01")
    assert not r.ok and r.meaning == "bad password"
    assert parse_response("#AB#3").meaning == "3 message(s) accepted"


class FakeWialon:
    """Minimal IPS server: answers every packet with an ack."""

    def __init__(self):
        self.received: list[str] = []

    async def handle(self, reader, writer):
        while line := await reader.readline():
            text = line.decode().strip()
            self.received.append(text)
            kind = text.split("#")[1]
            if kind == "B":
                answer = f"#AB#{text.count('|')}"
            else:
                answer = {"L": "#AL#1", "D": "#AD#1", "SD": "#ASD#1", "P": "#AP#"}[kind]
            writer.write(answer.encode() + b"\r\n")
            await writer.drain()


def test_client_login_send_and_buffer_flush():
    async def run():
        fake = FakeWialon()
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        # nothing listening yet on this port -> buffered
        dead = DeviceConnection("127.0.0.1", 1, "111", timeout=1)
        with pytest.raises(IPSError):
            await dead.send(Position(time=T, lat=1, lon=1))
        assert len(dead.buffer) == 1

        dead.port = port
        await dead.send(Position(time=T, lat=2, lon=2))
        await dead.close()
        server.close()
        return fake.received

    received = asyncio.run(run())
    assert [r.split("#")[1] for r in received] == ["L", "B", "D"]
