"""Fake HTTP endpoints for the bridge tests: Immich API, release compose files, cloudflared /ready,
GitHub releases. State lives in a JSON file the test edits.

Usage: python3 -I fake_api.py <port> <state.json>
"""
import json, os, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT, STATE = int(sys.argv[1]), sys.argv[2]
KEY = "test-key"

def load():
    return json.load(open(STATE))

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        st = load()
        if self.path.startswith("/release/"):
            ver = self.path.split("/")[2]
            body = (st.get("release_compose") or {}).get(ver)
            if body is None:
                self.send_response(404); self.end_headers(); return
            b = body.encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b); return
        if self.path == "/ready":
            r = st.get("ready") or {"status": 503, "readyConnections": 0, "connectorId": "x"}
            return self._send(r["status"], r)
        if self.path == "/gh/latest":
            gh = st.get("gh")
            if not gh:
                self.send_response(503); self.end_headers(); return
            return self._send(200, {"tag_name": gh, "html_url": f"https://github.com/cloudflare/cloudflared/releases/tag/{gh}"})
        if self.path.startswith("/gh-bridge/repos/") and self.path.endswith("/releases/latest"):
            rel = st.get("bridge_release")
            if not rel:
                return self._send(404, {"message": "Not Found"})
            return self._send(200, rel)
        if self.path.startswith("/files/"):
            p = os.path.join(st.get("files_dir", "/nonexistent"), os.path.basename(self.path))
            if not os.path.isfile(p):
                self.send_response(404); self.end_headers(); return
            b = open(p, "rb").read()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b); return
        if st.get("down"):
            self.send_response(502); self.end_headers(); return
        if self.headers.get("x-api-key") != KEY:
            return self._send(401, {"message": "Invalid API key"})
        p = self.path.split("?")[0]
        if p == "/api/admin/users":
            return self._send(200, st["users"])
        if p.startswith("/api/admin/users/") and p.endswith("/sessions"):
            uid = p.split("/")[4]
            return self._send(200, [s for s in st["sessions"] if s["userId"] == uid])
        if p == "/api/server/statistics":
            return self._send(200, st["stats"])
        if p == "/api/server/version":
            return self._send(200, {"major": 3, "minor": 3, "patch": 5, "prerelease": None})
        if p == "/api/queues":
            return self._send(200, st["queues"])
        return self._send(404, {"message": "not found"})

ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
