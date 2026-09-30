"""Send one test position straight to Wialon over IPS (no HTTP layer).

  python send_position.py <imei> [lat lon] [--password PW] [--host H] [--port P]
"""
import argparse
import asyncio
import logging
from datetime import datetime, timezone

from wialon_ips.client import IPSGateway
from wialon_ips.protocol import Position


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("imei")
    ap.add_argument("lat", nargs="?", type=float, default=4.0511)    # Douala
    ap.add_argument("lon", nargs="?", type=float, default=9.7679)
    ap.add_argument("--password")
    ap.add_argument("--host", default="193.193.165.165")
    ap.add_argument("--port", type=int, default=20332)
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    gw = IPSGateway(a.host, a.port, ping_interval=0)
    pos = Position(time=datetime.now(timezone.utc), lat=a.lat, lon=a.lon, speed=42,
                   course=90, altitude=15, sats=9, hdop=0.9,
                   params={"source": "middleware", "battery": 87})
    resp = await gw.send(a.imei, pos, password=a.password)
    print("Wialon answered:", resp.meaning)
    await gw.stop()


if __name__ == "__main__":
    asyncio.run(main())
