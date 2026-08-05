"""
Mitmproxy addon for dumping Devin CLI Connect-RPC traffic.

Usage:
  pip install mitmproxy
  mitmdump -s connect_capture.py -p 8080

Then run Devin CLI with:
  HTTPS_PROXY=http://127.0.0.1:8080 HTTP_PROXY=http://127.0.0.1:8080 \
  devin

Captured bodies are written to ./mitm_dump/.
"""
import os
import re
from datetime import datetime
from pathlib import Path

DUMP_DIR = Path("mitm_dump")
DUMP_DIR.mkdir(exist_ok=True)


def safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", s)[:80]


class ConnectDumper:
    def response(self, flow):
        host = flow.request.host
        path = flow.request.path
        if "api.devin.ai" not in host and "server.codeium.com" not in host and "raindrop.ai" not in host:
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        method_name = safe_name(path.strip("/").replace("/", "_"))
        base = DUMP_DIR / f"{ts}_{method_name}"

        # Save request metadata
        with open(f"{base}_req.txt", "w") as f:
            f.write(f"{flow.request.method} {flow.request.path}\n")
            for k, v in flow.request.headers.items():
                f.write(f"{k}: {v}\n")

        # Save request body
        if flow.request.content:
            with open(f"{base}_req.bin", "wb") as f:
                f.write(flow.request.content)

        # Save response metadata
        with open(f"{base}_resp.txt", "w") as f:
            f.write(f"Status: {flow.response.status_code}\n")
            for k, v in flow.response.headers.items():
                f.write(f"{k}: {v}\n")

        # Save response body
        if flow.response.content:
            with open(f"{base}_resp.bin", "wb") as f:
                f.write(flow.response.content)

        print(f"[connect_capture] dumped {path}")


addons = [ConnectDumper()]
