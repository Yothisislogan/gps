"""Real nginx proxy checks; run in CI's deployment job."""

import json
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from scripts.releases import gateway

pytestmark = pytest.mark.docker


def test_generation_proxy_preserves_old_routes_ranges_and_client_identity(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("requires nginx executable")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(
                {
                    "path": self.path,
                    "port": self.server.server_port,
                    "range": self.headers.get("Range"),
                    "client": self.headers.get("X-Forwarded-For"),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    generations = {
        rid: {"id": rid, "port": server.server_port, "asset": letter * 20}
        for rid, server, letter in zip(["old", "new"], servers, ["a", "b"], strict=True)
    }
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"pid {tmp_path}/nginx.pid; error_log stderr; events {{}} "
        f"http {{ access_log off; server {{ listen 127.0.0.1:{port}; "
        + gateway(generations, "new")
        + "}}"
    )
    subprocess.run([nginx, "-t", "-c", str(config)], check=True, capture_output=True)
    subprocess.run([nginx, "-c", str(config)], check=True, capture_output=True)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert client.get("/api/route").json()["port"] == servers[1].server_port
            old = client.get(
                "/data-releases/old/tiles/base.pmtiles",
                headers={"Range": "bytes=0-126", "X-Forwarded-For": "attacker"},
            ).json()
            assert old["port"] == servers[0].server_port
            assert old["path"] == "/tiles/base.pmtiles"
            assert old["range"] == "bytes=0-126"
            assert old["client"] == "127.0.0.1"
            asset = "/releases/" + "a" * 20 + "/js/map.js"
            assert client.get(asset).json()["path"] == asset
            assert client.get("/data-releases/retired/api/route").status_code == 410
    finally:
        subprocess.run([nginx, "-c", str(config), "-s", "stop"], check=True, capture_output=True)
        for server in servers:
            server.shutdown()
            server.server_close()
