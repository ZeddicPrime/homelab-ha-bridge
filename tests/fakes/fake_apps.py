"""Fake application servers for the app-plugin tests: Jellyfin's API, Foundry's
/api/status and /setup, package manifests, the Foundry releases page and a GitHub
release for the bridge itself. State lives in a JSON file the test edits; every
POST is appended to <state>.calls as one JSON line.

Usage: python3 -I fake_apps.py <port> <state.json>
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, STATE = int(sys.argv[1]), sys.argv[2]


def load():
    with open(STATE) as f:
        return json.load(f)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _record(self, body):
        with open(STATE + ".calls", "a") as f:
            f.write(json.dumps({"path": self.path, "auth": self.headers.get("Authorization"),
                                "body": body}) + "\n")

    def _jellyfin_ok(self, st):
        auth = self.headers.get("Authorization") or ""
        return f'Token="{st["jellyfin"]["key"]}"' in auth

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        self._record(body)
        st = load()
        if self.path == "/jf/Library/Refresh":
            if not self._jellyfin_ok(st):
                return self._send(401, {})
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/anth/"):
            an = st["anthias"]
            if self.headers.get("Authorization") != an["auth"]:
                return self._send(401, {"detail": "Authentication credentials were not provided."})
            if self.path in ("/anth/api/v2/display/off", "/anth/api/v2/display/on"):
                return self._send(200, {"message": "CEC command sent"})
            return self._send(404, {})
        if self.path == "/setup":
            fs = st["foundry"]
            if (body or {}).get("adminPassword") != fs["admin_password"]:
                return self._send(403, {"error": "Incorrect administrator password"})
            return self._send(200, {"status": "success"})
        return self._send(404, {"message": "not found"})

    def do_GET(self):
        st = load()
        path = self.path.split("?")[0]
        if path.startswith("/jf/"):
            jf = st["jellyfin"]
            if jf.get("down"):
                return self._send(502, {})
            if not self._jellyfin_ok(st):
                return self._send(401, {})
            p = path[3:]
            if p == "/System/Info":
                return self._send(200, jf["info"])
            if p == "/Sessions":
                return self._send(200, jf["sessions"])
            if p == "/ScheduledTasks":
                return self._send(200, jf["tasks"])
            if p == "/Items/Counts":
                return self._send(200, jf["counts"])
            return self._send(404, {})
        if path == "/api/status":
            fs = st["foundry"]
            if fs.get("down"):
                return self._send(502, "down", "text/plain")
            return self._send(200, fs["status"])
        if path.startswith("/manifest/"):
            m = st.get("manifests", {}).get(path.split("/", 2)[2])
            return self._send(200, m) if m else self._send(404, {})
        if path == "/releases/":
            return self._send(200, st.get("releases_html", ""), "text/html")
        if path.startswith("/anth/"):
            an = st["anthias"]
            if an.get("down"):
                return self._send(502, "Bad Gateway", "text/plain")
            if self.headers.get("Authorization") != an["auth"]:
                return self._send(401, {"detail": "Authentication credentials were not provided."})
            self._record(None)
            if path == "/anth/api/v2/info":
                return self._send(200, an["info"])
            if path == "/anth/api/v2/assets/control/next":
                return self._send(200, "Asset switched")
            return self._send(404, {})
        if path == "/gh-anthias/repos/Screenly/Anthias/releases/latest":
            rel = st.get("anthias_release")
            return self._send(200, rel) if rel else self._send(503, {})
        if path.startswith("/gh-bridge/repos/") and path.endswith("/releases/latest"):
            rel = st.get("bridge_release")
            return self._send(200, rel) if rel else self._send(404, {"message": "Not Found"})
        return self._send(404, {"message": "not found"})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
