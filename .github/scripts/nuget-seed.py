#!/usr/bin/env python3
"""
THE PACKAGE SEED — the NuGet closure of a core version, published to the control instance's build API
(MeshWeaver.Plugins Hosting/OneBuildTree §6.3; the routes: MeshWeaver.Plugins src/MeshWeaver.Fleet.Control/
BuildSeedEndpoints.cs). Run by .github/workflows/nuget-seed.yml.

  nuget-seed.py plan    --api <…/api/builds> [--channel latest]       which version, and whether its seed is published
  nuget-seed.py publish --api <…/api/builds> --version V --packages DIR [--source URL]
                        every complete package in the global-packages folder DIR: its `.nupkg` uploaded as a blob
                        (content-addressed — a blob the store holds is answered "already held"), then the manifest
                        (id, version, sha256, sha512, bytes, source), LAST and once. Signed with $SEED_SECRET
                        (HMAC-SHA256 of `seed-<kind>:<name>:<sha256>:<bytes>` — the build API's
                        BuildHostEndpoints.SeedSigningInput), the secret the platform-build hand-over is signed with.
  nuget-seed.py --self-test

`plan` prints GitHub output lines: version=…, build=true|false, why=….
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client as httpclient
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
NUGET_ORG = "https://api.nuget.org/v3/index.json"

Http = Callable[[str, str, Optional[bytes], dict], "tuple[int, bytes]"]


TRANSPORT_ERRORS = (urllib.error.URLError, OSError, ValueError, httpclient.HTTPException)


def http(method: str, url: str, body: Optional[bytes], headers: dict) -> "tuple[int, bytes]":
    """One exchange; (status, body), status 0 for a TRANSPORT failure. Never raises for one: a connection reset, a
    timeout, an IncompleteRead or a chunked answer cut off mid-way (`ValueError: invalid literal for int() with base 16`,
    run 43081471912) — including while reading an error answer's body — is status 0, which `exchange` retries."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read() if e.fp else b""
        except TRANSPORT_ERRORS:
            return (e.code if e.code < 500 else 0), b""
    except TRANSPORT_ERRORS as e:
        return 0, f"{type(e).__name__}: {e}".encode()


RETRY_DELAYS = (2, 5, 10, 20, 40, 60)                 # seconds before the 2nd … 7th attempt
# A package larger than this goes up in PARTS: control's ingress admits bodies up to 100m (deploy/helm/values.yaml
# proxy-body-size), and microsoft.playwright 1.63.0 alone is 213 MB (run 43090920090, a 413 from nginx). Each part is an
# ordinary content-addressed blob; the manifest lists them in order beside the WHOLE package's sha256 and sha512, which a host
# verifies after joining them. No route changes: the store holds bytes by hash, whole or part.
PART_BYTES = 64 << 20                 # 64 MiB per part, under the 100m ingress body limit


def retryable(status: int) -> bool:
    """A transport failure (0), a timeout or throttle (408, 429) or a server error (5xx) — never a refusal (4xx)."""
    return status == 0 or status in (408, 429) or status >= 500


def exchange(call: Http, method: str, url: str, body: Optional[bytes], headers: dict,
             sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> "tuple[int, bytes]":
    """`call` with retries and backoff on what a flaky control instance answers (`retryable`); an upload is idempotent —
    the store is content-addressed and a manifest is written once, so a retry can never write anything twice."""
    status, answer = call(method, url, body, headers)
    for delay in RETRY_DELAYS:
        if not retryable(status):
            break
        log(f"  {method} {urllib.parse.urlparse(url).path}: {status or 'no answer'} ({answer[:120]!r}) — retried in {delay} s")
        sleep(delay)
        status, answer = call(method, url, body, headers)
    return status, answer


def signature(secret: str, kind: str, name: str, sha256: str, size: int) -> str:
    msg = f"seed-{kind}:{name}:{sha256}:{size}".encode()
    return "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def plan(api: str, channel: str, call: Http, digests: Optional[dict] = None) -> "tuple[str, bool, str]":
    """(version, build?, why) for the set `channel` runs; `digests` receives its runner and portal digests. Raises
    SystemExit naming what cannot be read."""
    status, body = call("GET", f"{api.rstrip('/')}/channels/{channel}/set", None, {"Accept": "application/json"})
    try:
        doc = json.loads(body) if status == 200 else None
    except ValueError:
        doc = None
    version = doc.get("platformVersion") if isinstance(doc, dict) and doc.get("complete") is True else None
    if not isinstance(version, str) or not VERSION.match(version):
        raise SystemExit(f"::error::the control instance names no complete set for channel {channel} ({status})")
    if digests is not None:
        for k in ("runnerDigest", "portalDigest"):
            d = doc.get(k)
            if not (isinstance(d, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", d)):
                raise SystemExit(f"::error::channel {channel}'s {k} {d!r} is not sha256:<64 hex>")
            digests[k] = d
    status, body = call("GET", f"{api.rstrip('/')}/seeds/{version}/status", None, {"Accept": "application/json"})
    try:
        published = json.loads(body).get("published") if status == 200 else None
    except ValueError:
        published = None
    if published is None:
        raise SystemExit(f"::error::the seed status of {version} cannot be read ({status}) — is the seed route deployed on control?")
    return version, not published, ("published already" if published else "not published yet")


def packages(folder: str) -> list[dict]:
    """Every COMPLETE package of a global-packages folder (its `.nupkg.metadata` written), as manifest entries."""
    out = []
    for pid in sorted(os.listdir(folder)):
        pdir = os.path.join(folder, pid)
        if pid.startswith(".") or not os.path.isdir(pdir):
            continue
        for ver in sorted(os.listdir(pdir)):
            d = os.path.join(pdir, ver)
            nupkg = os.path.join(d, f"{pid}.{ver}.nupkg")
            if not (os.path.isfile(os.path.join(d, ".nupkg.metadata")) and os.path.isfile(nupkg)):
                continue
            h256, h512, size = hashlib.sha256(), hashlib.sha512(), 0
            with open(nupkg, "rb") as fh:            # streamed: a large closure is never held in memory twice
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h256.update(block)
                    h512.update(block)
                    size += len(block)
            try:
                with open(os.path.join(d, ".nupkg.metadata")) as fh:
                    source = json.load(fh).get("source")
            except (OSError, ValueError, AttributeError):
                source = None
            out.append({"id": pid, "version": ver, "file": nupkg, "bytes": size,
                        "sha256": h256.hexdigest(),
                        "sha512": base64.b64encode(h512.digest()).decode(),
                        "source": source if isinstance(source, str) else NUGET_ORG})
    return out


def publish(api: str, version: str, folder: str, secret: str, call: Http, log: Callable[[str], None] = print,
            sleep: Callable[[float], None] = time.sleep) -> int:
    if not secret:
        raise SystemExit("::error::SEED_SECRET is empty — the seed cannot be signed")
    entries = packages(folder)
    if not entries:
        raise SystemExit(f"::error::{folder} holds no complete package — the restore restored nothing")
    base = api.rstrip("/")
    uploaded = held = 0
    for e in entries:
        with open(e["file"], "rb") as fh:
            data = fh.read()
        chunks = [data] if len(data) <= PART_BYTES else [data[i:i + PART_BYTES] for i in range(0, len(data), PART_BYTES)]
        if len(chunks) > 1:
            e["parts"] = [{"sha256": hashlib.sha256(c).hexdigest(), "bytes": len(c)} for c in chunks]
        for chunk in chunks:
            h = hashlib.sha256(chunk).hexdigest()
            # RESUMABLE: a blob control already holds is answered "already held" (the store hashes nothing twice), so a run
            # that follows one cut off mid-way only re-confirms the blobs it had sent
            status, body = exchange(call, "PUT", f"{base}/seeds/blobs/{h}", chunk, {
                "Content-Type": "application/octet-stream",
                "X-Seed-Signature": signature(secret, "blob", h, h, len(chunk))}, sleep, log)
            if status not in (200, 201):
                raise SystemExit(f"::error::the blob of {e['id']}/{e['version']} was refused ({status}: {body[:300]!r})")
            try:
                existed = json.loads(body).get("existed") is True
            except ValueError:
                existed = False
            held += existed
            uploaded += not existed
    manifest = json.dumps({"platformVersion": version,
                           "packages": [{**{k: e[k] for k in ("id", "version", "sha256", "sha512", "bytes", "source")},
                                         **({"parts": e["parts"]} if "parts" in e else {})} for e in entries]},
                          separators=(",", ":"), sort_keys=True).encode()
    mhash = hashlib.sha256(manifest).hexdigest()
    # LAST, and only now: every blob above was confirmed (stored or already held) — a host never reads a manifest that names
    # a blob control does not have
    status, body = exchange(call, "PUT", f"{base}/seeds/{urllib.parse.quote(version, safe='')}/manifest?sha256={mhash}", manifest, {
        "Content-Type": "application/json", "X-Seed-Signature": signature(secret, "manifest", version, mhash, len(manifest))}, sleep, log)
    if status == 409:
        raise SystemExit(f"::error::the seed of {version} is published already with OTHER bytes — a published seed never changes")
    if status not in (200, 201):
        raise SystemExit(f"::error::the manifest of {version} was refused ({status}: {body[:300]!r})")
    log(f"package seed {version}: {len(entries)} package(s) — {uploaded} blob(s) uploaded, {held} already held; "
        f"manifest {mhash[:12]} published")
    return 0


def self_test() -> int:
    fails = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        if not ok:
            fails.append(f"{name}: {detail}")

    store: dict = {}

    def fake(method: str, url: str, body: Optional[bytes], headers: dict) -> "tuple[int, bytes]":
        path = urllib.parse.urlparse(url).path
        if method == "GET" and path.endswith("/channels/latest/set"):
            return 200, json.dumps({"channel": "latest", "platformVersion": "3.1.7", "complete": True,
                                    "runnerDigest": "sha256:" + "a" * 64, "portalDigest": "sha256:" + "b" * 64}).encode()
        if method == "GET" and path.endswith("/status"):
            return 200, json.dumps({"published": "manifest:3.1.7" in store}).encode()
        if method == "PUT":
            sha = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("sha256") or [path.rsplit("/", 1)[-1]])[0]
            kind, name = ("manifest", path.split("/")[-2]) if path.endswith("/manifest") else ("blob", sha)
            if headers.get("X-Seed-Signature") != signature("s3cret", kind, name, sha, len(body or b"")):
                return 401, b"{}"
            if hashlib.sha256(body or b"").hexdigest() != sha:
                return 400, b"{}"
            key = f"{kind}:{name}"
            if key in store and store[key] != body:
                return 409, b"{}"
            existed = key in store
            store[key] = body
            return (200 if existed else 201), json.dumps({"existed": existed}).encode()
        return 404, b""

    dg: dict = {}
    v, build, why = plan("https://ops.example/api/builds", "latest", fake, dg)
    check("plan: the version latest runs, not published → build, with its runner and portal digests",
          (v, build) == ("3.1.7", True) and dg == {"runnerDigest": "sha256:" + "a" * 64, "portalDigest": "sha256:" + "b" * 64}, (v, build, why, dg))
    with tempfile.TemporaryDirectory() as t:
        for pid, ver, data in (("demo.a", "1.0.0", b"A"), ("demo.b", "2.0.0", b"B")):
            d = os.path.join(t, pid, ver)
            os.makedirs(d)
            open(os.path.join(d, f"{pid}.{ver}.nupkg"), "wb").write(data)
            open(os.path.join(d, ".nupkg.metadata"), "w").write(json.dumps({"version": 2, "source": NUGET_ORG}))
        os.makedirs(os.path.join(t, "demo.c", "1.0.0"))       # incomplete: no metadata
        open(os.path.join(t, "demo.c", "1.0.0", "demo.c.1.0.0.nupkg"), "wb").write(b"C")
        lines: list = []
        publish("https://ops.example/api/builds", "3.1.7", t, "s3cret", fake, lines.append)
        man = json.loads(store["manifest:3.1.7"])
        check("publish: every COMPLETE package becomes a blob and a manifest entry (an incomplete one is not)",
              sorted(p["id"] for p in man["packages"]) == ["demo.a", "demo.b"] and len([k for k in store if k.startswith("blob:")]) == 2, man)
        check("publish: an entry carries id, version, sha256, sha512, bytes and source",
              set(man["packages"][0]) == {"id", "version", "sha256", "sha512", "bytes", "source"}
              and man["packages"][0]["sha256"] == hashlib.sha256(b"A").hexdigest(), man["packages"][0])
        check("plan: once published, nothing to build", plan("https://ops.example/api/builds", "latest", fake)[1] is False)
        lines2: list = []
        publish("https://ops.example/api/builds", "3.1.7", t, "s3cret", fake, lines2.append)
        check("publish: a second run uploads no new blob (already held) and the same manifest is accepted",
              "0 blob(s) uploaded, 2 already held" in lines2[0], lines2)
        open(os.path.join(t, "demo.a", "1.0.0", "demo.a.1.0.0.nupkg"), "wb").write(b"A2")
        try:
            publish("https://ops.example/api/builds", "3.1.7", t, "s3cret", fake, lambda m: None)
            check("publish: other bytes under a published version are refused", False, "accepted")
        except SystemExit as e:
            check("publish: other bytes under a published version are refused (409)", "never changes" in str(e), e)
        try:
            publish("https://ops.example/api/builds", "3.1.8", t, "wrong", fake, lambda m: None)
            check("publish: a wrong secret is refused", False, "accepted")
        except SystemExit as e:
            check("publish: a wrong secret is refused", "refused (401" in str(e), e)
    # ── a flaky control instance: transport failures and 5xx are retried, refusals are not ──
    with tempfile.TemporaryDirectory() as t2:
        d = os.path.join(t2, "demo.r", "1.0.0")
        os.makedirs(d)
        open(os.path.join(d, "demo.r.1.0.0.nupkg"), "wb").write(b"R")
        open(os.path.join(d, ".nupkg.metadata"), "w").write(json.dumps({"version": 2}))
        flaky_answers = [(0, b"ValueError: invalid literal for int() with base 16: b''"), (502, b""), (0, b"ConnectionResetError")]
        calls: list = []

        def flaky(method: str, url: str, body: Optional[bytes], headers: dict) -> "tuple[int, bytes]":
            calls.append(url)
            if flaky_answers and "/blobs/" in url:
                return flaky_answers.pop(0)
            return fake(method, url, body, headers)
        store.clear()
        slept: list = []
        publish("https://ops.example/api/builds", "3.1.9", t2, "s3cret", flaky, lambda m: None, slept.append)
        check("retry: a cut-off answer, a 502 and a reset are retried with backoff, then the blob and the manifest land",
              "manifest:3.1.9" in store and slept == list(RETRY_DELAYS[:3]) and len([c for c in calls if "/blobs/" in c]) == 4, (slept, calls))
        calls.clear()
        refused = lambda method, url, body, headers: (calls.append(url), (401, b"{}"))[1]
        try:
            publish("https://ops.example/api/builds", "3.1.10", t2, "s3cret", refused, lambda m: None, lambda s: None)
            check("retry: a refusal is not retried", False, "accepted")
        except SystemExit:
            check("retry: a refusal (401) is not retried, and no manifest is sent", calls and all("/blobs/" in c for c in calls) and len(calls) == 1, calls)
        attempts: list = []
        waits: list = []
        never = lambda method, url, body, headers: (attempts.append(url), (0, b"timeout"))[1]
        try:
            publish("https://ops.example/api/builds", "3.1.11", t2, "s3cret", never, lambda m: None, waits.append)
            check("retry: an instance that never answers ends red", False, "accepted")
        except SystemExit as e:
            check("retry: an instance that never answers ends red after EVERY attempt (1 + one per backoff step), naming the blob",
                  "demo.r/1.0.0" in str(e) and len(attempts) == 1 + len(RETRY_DELAYS) and waits == list(RETRY_DELAYS)
                  and not any("/manifest" in a for a in attempts), (len(attempts), waits, e))
    # ── a package above the ingress limit goes up in PARTS, each its own content-addressed blob ──
    global PART_BYTES
    real_part = PART_BYTES
    PART_BYTES = 4
    try:
        with tempfile.TemporaryDirectory() as t3:
            d = os.path.join(t3, "big.pkg", "1.0.0")
            os.makedirs(d)
            open(os.path.join(d, "big.pkg.1.0.0.nupkg"), "wb").write(b"0123456789")
            open(os.path.join(d, ".nupkg.metadata"), "w").write(json.dumps({"version": 2}))
            store.clear()
            puts: list = []
            spy = lambda method, url, body, headers: (puts.append((url, body)), fake(method, url, body, headers))[1]
            publish("https://ops.example/api/builds", "3.1.12", t3, "s3cret", spy, lambda m: None, lambda s: None)
            entry = json.loads(store["manifest:3.1.12"])["packages"][0]
            blobs = [b for u, b in puts if "/blobs/" in u]
            check("parts: a package over PART_BYTES is uploaded as ordered parts, none above the limit, and the manifest lists them "
                  "beside the WHOLE package's hashes",
                  blobs == [b"0123", b"4567", b"89"] and [p["sha256"] for p in entry["parts"]] == [hashlib.sha256(b).hexdigest() for b in blobs]
                  and entry["sha256"] == hashlib.sha256(b"0123456789").hexdigest() and entry["bytes"] == 10, (entry, blobs))
    finally:
        PART_BYTES = real_part
    check("http: a connection that cannot be made is a transport failure (0), never an exception",
          http("PUT", "http://127.0.0.1:9/never", b"x", {})[0] == 0)
    # the MOTIVATING failure, through http.client itself: a server that announces chunked encoding and closes mid-chunk
    # (run 43081471912: ValueError: invalid literal for int() with base 16: b'') — and the same while an ERROR body is read
    import socket
    import threading
    for status_line, name in ((b"HTTP/1.1 200 OK", "a success"), (b"HTTP/1.1 503 Service Unavailable", "an error answer")):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve(listener: socket.socket = srv, line: bytes = status_line) -> None:
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(line + b"\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n5\r\nhello\r\n")
            conn.close()
            listener.close()
        threading.Thread(target=serve, daemon=True).start()
        got = http("PUT", f"http://127.0.0.1:{port}/seeds/blobs/x", b"x", {})
        check(f"http: {name} whose chunked body is cut off mid-chunk is a transport failure (0), never an exception",
              got[0] == 0, got)

    if fails:
        for f in fails:
            print(f"SELF-TEST FAILED: {f}")
        return 1
    print("nuget-seed self-test: all cases passed (plan; complete packages only; manifest fields; idempotent re-run; "
          "write-once manifest; signature)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "publish"], nargs="?")
    ap.add_argument("--api", default="")
    ap.add_argument("--channel", default="latest")
    ap.add_argument("--version", default="")
    ap.add_argument("--packages", default="")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.api.startswith("https://"):
        raise SystemExit("::error::--api must be the control instance's https …/api/builds")
    if a.command == "plan":
        dg: dict = {}
        v, build, why = plan(a.api, a.channel, http, dg)
        print(f"version={v}\nbuild={'true' if build else 'false'}\nwhy={why}\nrunner={dg['runnerDigest']}\nportal={dg['portalDigest']}")
        return 0
    if a.command == "publish":
        if not VERSION.match(a.version) or not os.path.isdir(a.packages):
            raise SystemExit("::error::publish needs --version MAJOR.MINOR.PATCH and an existing --packages folder")
        return publish(a.api, a.version, a.packages, os.environ.get("SEED_SECRET", ""), http)
    ap.error("a command is required")
    return 2


if __name__ == "__main__":
    sys.exit(main())
