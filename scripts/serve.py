#!/usr/bin/env python3
"""Local review server for the display kiosk.

Serves static files and provides API endpoints to read/write manual overrides
and recompute partner scores.
"""

import http.server
import json
import os
import socketserver
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
OVERRIDES_PATH = os.path.join(PHOTOS_DIR, "overrides.json")
VENV_PYTHON = os.path.join(BASE_DIR, ".venv", "bin", "python3")
SYNC_SCRIPT = os.path.join(BASE_DIR, "scripts", "sync-photos.py")


class ReviewRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        url_path = self.path.split("?")[0]
        if url_path == "/api/overrides":
            if os.path.isfile(OVERRIDES_PATH):
                try:
                    with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._send_json(200, data)
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
            else:
                self._send_json(200, {"always_pair": [], "never_pair": [], "tags": {}})
            return

        super().do_GET()

    def do_POST(self):
        url_path = self.path.split("?")[0]
        if url_path == "/api/overrides":
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                self._send_json(400, {"ok": False, "error": "Empty body"})
                return

            raw_body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(raw_body)
            except Exception as e:
                self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
                return

            # Normalize pairs: ensure [f1, f2] sorted order and no duplicates
            def _norm_pairs(pairs_list):
                seen = set()
                result = []
                for item in pairs_list or []:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        p = sorted([str(item[0]), str(item[1])])
                        key = (p[0], p[1])
                        if key not in seen:
                            seen.add(key)
                            result.append(p)
                return sorted(result, key=lambda x: (x[0], x[1]))

            overrides = {
                "always_pair": _norm_pairs(data.get("always_pair")),
                "never_pair": _norm_pairs(data.get("never_pair")),
                "tags": data.get("tags") or {},
            }

            try:
                os.makedirs(PHOTOS_DIR, exist_ok=True)
                with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
                    json.dump(overrides, f, indent=2)
                    f.write("\n")

                self._send_json(200, {
                    "ok": True,
                    "message": "Saved overrides to photos/overrides.json",
                    "always_count": len(overrides["always_pair"]),
                    "never_count": len(overrides["never_pair"]),
                })
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"Failed to write file: {e}"})
            return

        if url_path == "/api/sync":
            python_bin = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else sys.executable
            try:
                res = subprocess.run(
                    [python_bin, SYNC_SCRIPT],
                    capture_output=True,
                    text=True,
                    cwd=BASE_DIR,
                    timeout=30,
                )
                self._send_json(200, {
                    "ok": res.returncode == 0,
                    "output": res.stdout,
                    "error": res.stderr if res.returncode != 0 else "",
                })
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"Sync execution failed: {e}"})
            return

        self._send_json(404, {"ok": False, "error": "Not found"})


def run(port=8088):
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), ReviewRequestHandler) as httpd:
        print(f"Display Kiosk Review Server running at http://localhost:{port}/")
        print(f"  - Kiosk Slideshow:  http://localhost:{port}/")
        print(f"  - Pairing Review:   http://localhost:{port}/pairs.html")
        print(f"  - Photo Review:     http://localhost:{port}/review.html")
        print(f"  - Upscale Compare:  http://localhost:{port}/compare.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")


if __name__ == "__main__":
    port_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    run(port_arg)
