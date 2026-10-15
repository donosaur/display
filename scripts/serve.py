#!/usr/bin/env python3
"""Local review server for the display kiosk.

Serves static files and provides API endpoints to read/write manual overrides
and recompute partner scores.
"""

import argparse
import http.server
import json
import os
import posixpath
import socketserver
import subprocess
import sys
import urllib.parse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_DIR = os.path.join(BASE_DIR, "site")
SITE_PHOTOS_DIR = os.path.join(SITE_DIR, "photos")
ROOT_PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
OVERRIDES_PATH = os.path.join(ROOT_PHOTOS_DIR, "overrides.json")
SITE_OVERRIDES_PATH = os.path.join(SITE_PHOTOS_DIR, "overrides.json")
VENV_PYTHON = os.path.join(BASE_DIR, ".venv", "bin", "python3")
SYNC_SCRIPT = os.path.join(BASE_DIR, "scripts", "sync-photos.py")


class ReviewRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SITE_DIR, **kwargs)

    def translate_path(self, path):
        clean_path = urllib.parse.urlparse(path).path
        clean_path = posixpath.normpath(urllib.parse.unquote(clean_path))

        if clean_path in ("/settings", "/settings/index.html", "/pairs.html", "/tools/pairs.html", "/tools/settings"):
            return os.path.join(SITE_DIR, "settings", "index.html")

        # Direct tool endpoints or /tools/<tool>.html
        if clean_path.startswith("/tools/"):
            sub = clean_path[len("/tools/"):].lstrip("/")
            if sub.startswith("photos/"):
                clean_path = "/" + sub
            else:
                return os.path.join(BASE_DIR, sub)

        if clean_path in ("/review.html", "/compare.html"):
            return os.path.join(BASE_DIR, clean_path.lstrip("/"))

        if clean_path.startswith("/photos/"):
            rel_photo = clean_path[len("/photos/"):].lstrip("/")
            # Check site/photos first (images and photos.json)
            site_photo_path = os.path.join(SITE_PHOTOS_DIR, rel_photo)
            if os.path.exists(site_photo_path):
                return site_photo_path
            # Check root photos/ (comparisons, overrides.json, quality-report.json, photos.debug.json)
            root_photo_path = os.path.join(ROOT_PHOTOS_DIR, rel_photo)
            if os.path.exists(root_photo_path):
                return root_photo_path
            return site_photo_path

        return os.path.join(SITE_DIR, clean_path.lstrip("/"))

    def _is_path_forbidden(self, raw_path):
        url_path = urllib.parse.urlparse(raw_path).path
        url_path = urllib.parse.unquote(url_path)
        norm_path = posixpath.normpath(url_path)
        segments = [seg for seg in norm_path.strip("/").split("/") if seg]
        for seg in segments:
            if seg.startswith("."):
                return True
        if segments:
            if segments[0] == "scripts" or (len(segments) >= 2 and segments[0] == "tools" and segments[1] == "scripts"):
                return True
        return False

    def _is_same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host")
        parsed = urllib.parse.urlparse(origin)
        return parsed.netloc == host

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin")
        if origin and self._is_same_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if self._is_path_forbidden(self.path):
            self.send_error(403, "Forbidden")
            return
        origin = self.headers.get("Origin")
        if origin and not self._is_same_origin():
            self.send_error(403, "Forbidden")
            return
        self.send_response(204)
        if origin and self._is_same_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self._is_path_forbidden(self.path):
            self.send_error(403, "Forbidden")
            return

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
                self._send_json(200, {"always_pair": [], "never_pair": [], "tags": {}, "use_original": []})
            return

        super().do_GET()

    def do_HEAD(self):
        if self._is_path_forbidden(self.path):
            self.send_error(403, "Forbidden")
            return
        super().do_HEAD()

    def do_POST(self):
        if self._is_path_forbidden(self.path):
            self.send_error(403, "Forbidden")
            return

        if not self._is_same_origin():
            self._send_json(403, {"ok": False, "error": "Cross-origin request rejected"})
            return

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

            existing = {}
            if os.path.isfile(OVERRIDES_PATH):
                try:
                    with open(OVERRIDES_PATH, "r", encoding="utf-8") as ef:
                        existing = json.load(ef) or {}
                except Exception:
                    existing = {}

            raw_use_orig = data.get("use_original") if "use_original" in data else existing.get("use_original", [])
            norm_use_orig = sorted({str(x) for x in (raw_use_orig or []) if x})

            overrides = {
                "always_pair": _norm_pairs(data.get("always_pair")),
                "never_pair": _norm_pairs(data.get("never_pair")),
                "tags": data.get("tags") if "tags" in data and data.get("tags") else existing.get("tags", {}),
                "use_original": norm_use_orig,
            }
            if "dates" in data or "dates" in existing:
                overrides["dates"] = data.get("dates") or existing.get("dates", {})

            try:
                os.makedirs(ROOT_PHOTOS_DIR, exist_ok=True)
                for target_path in (OVERRIDES_PATH, SITE_OVERRIDES_PATH):
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    tmp_overrides = target_path + ".tmp"
                    with open(tmp_overrides, "w", encoding="utf-8") as f:
                        json.dump(overrides, f, indent=2)
                        f.write("\n")
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp_overrides, target_path)

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
                    timeout=900,  # 15 minutes
                )
                self._send_json(200 if res.returncode == 0 else 500, {
                    "ok": res.returncode == 0,
                    "output": res.stdout,
                    "error": res.stderr if res.returncode != 0 else "",
                })
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"Sync execution failed: {e}"})
            return

        self._send_json(404, {"ok": False, "error": "Not found"})


def run(port=8088, host="127.0.0.1"):
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), ReviewRequestHandler) as httpd:
        display_host = "localhost" if host == "127.0.0.1" else (host if host != "0.0.0.0" else "0.0.0.0")
        print(f"Display Kiosk Review Server running at http://{display_host}:{port}/ (bound to {host})")
        print(f"  - Kiosk Slideshow:  http://{display_host}:{port}/")
        print(f"  - Pairing Review:   http://{display_host}:{port}/settings")
        print(f"  - Photo Review:     http://{display_host}:{port}/tools/review.html (or /review.html)")
        print(f"  - Upscale Compare:  http://{display_host}:{port}/tools/compare.html (or /compare.html)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")


def main():
    parser = argparse.ArgumentParser(description="Local review server for the display kiosk.")
    parser.add_argument("port", nargs="?", type=int, default=8088, help="Port to serve on (default: 8088)")
    parser.add_argument("--lan", action="store_true", help="Bind to all interfaces (0.0.0.0) instead of localhost")
    args = parser.parse_args()

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    run(port=args.port, host=host)


if __name__ == "__main__":
    main()
