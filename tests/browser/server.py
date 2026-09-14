"""Deterministic browser fixture server. Never imported by production code."""

from __future__ import annotations

import json
import mimetypes
import struct
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "dist" / "web"
PLACE = {
    "id": "test-clinic",
    "kind": "poi",
    "name": "Clínica de prueba",
    "category": "clinica",
    "lat": 12.115,
    "lon": -86.25,
    "phone": "",
    "photos": [],
    "address_text": "Frente al parque",
}
STYLE = {
    "version": 8,
    "sources": {
        "fixture": {
            "type": "geojson",
            "data": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-86.27, 12.1], [-86.25, 12.115], [-86.23, 12.13]],
                        },
                    }
                ],
            },
        }
    },
    "layers": [
        {"id": "background", "type": "background", "paint": {"background-color": "#e9eee8"}},
        {
            "id": "road",
            "type": "line",
            "source": "fixture",
            "paint": {"line-color": "#ffffff", "line-width": 12},
        },
    ],
}


def polyline(points):
    result, last = [], (0, 0)
    for lat, lon in points:
        current = round(lat * 1e6), round(lon * 1e6)
        for delta in (current[0] - last[0], current[1] - last[1]):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 32:
                result.append(chr((32 | (value & 31)) + 63))
                value >>= 5
            result.append(chr(value + 63))
        last = current
    return "".join(result)


def route(length=1.1, time=120):
    summary = {"length": length, "time": time}
    return {
        "trip": {
            "summary": summary,
            "legs": [
                {
                    "shape": polyline([(12.11, -86.26), (12.115, -86.255), (12.115, -86.25)]),
                    "summary": summary,
                    "maneuvers": [
                        {
                            "type": 1,
                            "instruction": "Seguí hacia el parque",
                            "begin_shape_index": 0,
                            "end_shape_index": 1,
                            "length": 0.6,
                            "time": 60,
                        },
                        {
                            "type": 10,
                            "instruction": "Girá a la derecha",
                            "begin_shape_index": 1,
                            "end_shape_index": 2,
                            "length": 0.5,
                            "time": 60,
                        },
                    ],
                }
            ],
        }
    }


def archive():
    header = bytearray(127)
    header[:8] = b"PMTiles\x03"
    for offset, value in [
        (8, 127),
        (16, 5),
        (24, 132),
        (32, 2),
        (40, 134),
        (48, 0),
        (56, 134),
        (64, 1),
        (72, 1),
        (80, 1),
        (88, 1),
    ]:
        struct.pack_into("<Q", header, offset, value)
    header[96:102] = bytes([1, 1, 1, 1, 0, 0])
    for offset, value in [
        (102, -1800000000),
        (106, -850000000),
        (110, 1800000000),
        (114, 850000000),
    ]:
        struct.pack_into("<i", header, offset, value)
    return bytes(header) + bytes([1, 0, 1, 1, 1]) + b"{}" + b"\0"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, *_args):
        pass

    def send(self, payload, mime="application/json", status=200):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/config.js":
            return self.send(
                b'window.NICANAV_CONFIG={styleUrl:"/fixture/style.json",styleUrlNight:null};',
                "application/javascript",
            )
        if path == "/fixture/style.json":
            return self.send(STYLE)
        if path == "/tiles/circle.pmtiles":
            return self.send(archive(), "application/octet-stream")
        if path == "/api/search":
            return self.send({"query": "clínica", "hits": [PLACE], "geocode": {"candidates": []}})
        if path.startswith("/api/poi/"):
            return self.send(PLACE)
        if path == "/api/reverse":
            return self.send({"candidates": [{"label": "Frente al parque"}]})
        if path.startswith("/@"):
            self.path = "/index.html"
        if path.startswith("/api/"):
            return self.send({"status": "ok"})
        return super().do_GET()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if urlsplit(self.path).path == "/api/route":
            return self.send(
                {
                    **route(),
                    "alternates": [route(1.4, 180)],
                    "nicanav": {"closures_status": "checked"},
                }
            )
        return self.send({"id": "fixture", "status": "pending"})


mimetypes.add_type("application/javascript", ".mjs")
if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
