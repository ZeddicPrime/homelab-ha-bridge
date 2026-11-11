import json, os, sys, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakelib
PORT = int(sys.argv[1]); TOKEN = "tok-123"
LOG = os.environ.get("FAKE_REG_LOG", "/dev/null")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def handle_req(self):
        with open(LOG, "a") as f: f.write(f"{self.command} {self.path} auth={'Authorization' in self.headers}\n")
        if self.path.startswith("/token"):
            return self._send(200, json.dumps({"token": TOKEN}).encode())
        if self.path.startswith("/blobstore/"):
            if "Authorization" in self.headers:  # like S3: reject foreign auth
                return self._send(400, b"auth not allowed")
            d = self.path.split("/")[-1]
            for repo, tags in fakelib.load_reg().items():
                for tag in tags:
                    _, blobs, _ = fakelib.objects(repo, tag)
                    if d in blobs: return self._send(200, blobs[d][0])
            return self._send(404)
        m = re.match(r"^/v2/(.+)/(manifests|blobs)/(.+)$", self.path)
        if not m: return self._send(404)
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, b"{}", {"WWW-Authenticate": f'Bearer realm="http://localhost:{PORT}/token",service="fake",scope="repository:{m[1]}:pull"'})
        repo, kind, ref = m.groups()
        reg = fakelib.load_reg()
        if repo not in reg: return self._send(404)
        if kind == "blobs":
            return self._send(307, b"", {"Location": f"/blobstore/{ref}"})
        for tag in reg[repo]:
            idx, blobs, _ = fakelib.objects(repo, tag)
            if ref == tag: ref = idx
            if ref in blobs:
                body, mt = blobs[ref]
                return self._send(200, body, {"Content-Type": mt, "Docker-Content-Digest": ref})
        return self._send(404)
    do_GET = do_HEAD = handle_req
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
