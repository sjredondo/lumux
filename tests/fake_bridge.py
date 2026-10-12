"""A fake Hue bridge for end-to-end tests.

* HTTPS CLIP v2 REST on 127.0.0.1:443 (self-signed), recording every PUT.
* DTLS 1.2 PSK listener on UDP 2100 via ``openssl s_server``, counting the
  HueStream frames it receives.

Needs root (ports 443/2100) and the ``openssl`` binary; the e2e tests skip
otherwise. Run them in the Arch container described in tests/README.md.
"""

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_KEY = "test-app-key"
APP_ID = "test-app-id"
CLIENT_KEY = "0123456789abcdef0123456789abcdef"
ENT_ID = "11111111-2222-3333-4444-555555555555"

# Two lights in the entertainment area, two outside it.
DEVICES = [
    {"id": "dev1", "services": [{"rid": "L1", "rtype": "light"}, {"rid": "E1", "rtype": "entertainment"}]},
    {"id": "dev2", "services": [{"rid": "L2", "rtype": "light"}, {"rid": "E2", "rtype": "entertainment"}]},
    {"id": "dev3", "services": [{"rid": "L3", "rtype": "light"}, {"rid": "E3", "rtype": "entertainment"}]},
    {"id": "dev4", "services": [{"rid": "L4", "rtype": "light"}]},
]
LIGHTS = [
    {"id": f"L{i}", "type": "light", "metadata": {"name": f"Light {i}"}, "on": {"on": False}}
    for i in range(1, 5)
]
ENT_CONFIG = {
    "id": ENT_ID,
    "type": "entertainment_configuration",
    "metadata": {"name": "TV area"},
    "status": "inactive",
    "configuration_type": "screen",
    "channels": [
        {"channel_id": 0, "position": {"x": -1.0, "y": 0.8, "z": 0.0},
         "members": [{"service": {"rid": "E1", "rtype": "entertainment"}, "index": 0}]},
        {"channel_id": 1, "position": {"x": 1.0, "y": 0.8, "z": 0.0},
         "members": [{"service": {"rid": "E2", "rtype": "entertainment"}, "index": 0}]},
    ],
    "locations": {"service_locations": [
        {"service": {"rid": "E1", "rtype": "entertainment"}, "position": {"x": -1.0, "y": 0.8, "z": 0.0}},
        {"service": {"rid": "E2", "rtype": "entertainment"}, "position": {"x": 1.0, "y": 0.8, "z": 0.0}},
    ]},
}


def can_run() -> bool:
    return os.geteuid() == 0 and shutil.which("openssl") is not None


class FakeBridge:
    def __init__(self):
        self.puts = []  # (path, body)
        self.frames = 0
        self.link_pressed = False  # pairing: POST /api succeeds only when True
        self._tmp = tempfile.mkdtemp(prefix="fakehue")
        self._http = None
        self._dtls = None
        self._dtls_lock = threading.Lock()

    # -- REST -------------------------------------------------------------
    def _handler(bridge):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, payload, status=200, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/auth/v1":
                    return self._send({}, headers={"hue-application-id": APP_ID})
                if self.headers.get("hue-application-key") != APP_KEY:
                    return self._send({"errors": [{"description": "unauthorized"}]}, 401)
                p = self.path.removeprefix("/clip/v2/resource/")
                if p == "device":
                    return self._send({"data": DEVICES})
                if p == "light":
                    return self._send({"data": LIGHTS})
                if p == "zone":
                    return self._send({"data": []})
                if p == "entertainment_configuration":
                    return self._send({"data": [ENT_CONFIG]})
                if p == f"entertainment_configuration/{ENT_ID}":
                    return self._send({"data": [ENT_CONFIG]})
                return self._send({"errors": []}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/api" or "devicetype" not in body:
                    return self._send({"errors": []}, 404)
                if not bridge.link_pressed:
                    return self._send([{"error": {"type": 101, "address": "",
                                                  "description": "link button not pressed"}}])
                return self._send([{"success": {"username": APP_KEY, "clientkey": CLIENT_KEY}}])

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                bridge.puts.append((self.path, body))
                return self._send({"data": [], "errors": []})

        return Handler

    def start(self):
        cert, key = os.path.join(self._tmp, "c.pem"), os.path.join(self._tmp, "k.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=fakehue", "-keyout", key, "-out", cert],
            check=True, capture_output=True,
        )
        self._http = ThreadingHTTPServer(("127.0.0.1", 443), self._handler())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self._http.socket = ctx.wrap_socket(self._http.socket, server_side=True)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self.start_dtls()
        return self

    # -- DTLS -------------------------------------------------------------
    def start_dtls(self):
        self._dtls = subprocess.Popen(
            ["openssl", "s_server", "-dtls1_2", "-nocert", "-psk", CLIENT_KEY,
             "-psk_identity", APP_ID, "-cipher", "PSK-AES128-GCM-SHA256",
             "-accept", "2100", "-quiet"],
            # stdin must stay open, or s_server closes the session on EOF
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        proc = self._dtls
        threading.Thread(target=self._count_frames, args=(proc,), daemon=True).start()

    def _count_frames(self, proc):
        tail = b""
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                return
            data = tail + chunk
            with self._dtls_lock:
                self.frames += data.count(b"HueStream")
            tail = data[-8:]

    def stop_dtls(self):
        if self._dtls:
            self._dtls.kill()
            self._dtls.wait()
            self._dtls = None

    def stop(self):
        self.stop_dtls()
        if self._http:
            self._http.shutdown()
            self._http.server_close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def stream_actions(self):
        return [b.get("action") for p, b in self.puts if "entertainment_configuration" in p]

    def light_puts(self):
        return [p.rsplit("/", 1)[-1] for p, _ in self.puts if "/resource/light/" in p]
