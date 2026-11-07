#!/usr/bin/env python3
"""A stand-in for the two API-server calls headscale-keys makes: GET and merge-PATCH of a Secret.

Serves http://127.0.0.1:<port>/api/v1/namespaces/<ns>/secrets/<name> from an in-memory store seeded
with the names given on the command line (as the chart pre-creates them). It refuses a request
without the bearer token from the token file, answers 404 for any other Secret, and records each
write to <state dir>/<name>.json so the test can read what the script stored.
Usage: kube-stub.py <port> <token file> <state dir> <secret> [<secret> …]
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

port, token_file, state = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
store = {name: {"metadata": {"name": name, "annotations": {}}, "data": {}} for name in sys.argv[4:]}


def merge(base, patch):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        elif v is None:
            base.pop(k, None)
        else:
            base[k] = v


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def secret(self):
        if self.headers.get("Authorization") != "Bearer " + Path(token_file).read_text().strip():
            self.answer(401, {"reason": "Unauthorized"})
            return None
        parts = self.path.strip("/").split("/")
        if len(parts) != 6 or parts[:3] != ["api", "v1", "namespaces"] or parts[4] != "secrets":
            self.answer(404, {"reason": "NotFound"})
            return None
        if parts[5] not in store:
            self.answer(404, {"reason": "NotFound"})
            return None
        return parts[5]

    def do_GET(self):
        name = self.secret()
        if name:
            self.answer(200, store[name])

    def do_PATCH(self):
        name = self.secret()
        if not name:
            return
        if self.headers.get("Content-Type") != "application/merge-patch+json":
            self.answer(415, {"reason": "UnsupportedMediaType"})
            return
        patch = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        merge(store[name], patch)
        (state / f"{name}.json").write_text(json.dumps(store[name]))
        self.answer(200, store[name])


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
