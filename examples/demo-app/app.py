# Example application: minimal HTTP service for Drawbridge onboarding.
"""Demo app: /healthz returns 200 once the app is ready."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("DEMO_CONFIG", "config/app.json")
READY = {"ok": False}


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/healthz":
            # Ready after one event-loop tick; keeps the health gate honest.
            READY["ok"] = True
            body = b'{"status":"ok"}'
            self.send_response(200)
        elif self.path == "/":
            body = json.dumps(
                {"app": "demo", "config": load_config(), "ready": READY["ok"]}
            ).encode()
            self.send_response(200)
        else:
            body = b'{"error":"not found"}'
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        print(format % args, flush=True)


def main() -> None:
    config = load_config()
    port = int(config.get("listen_port", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"demo app listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
