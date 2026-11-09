#!/usr/bin/env python3
"""Follow a booting portal's progress in the terminal — the same facts the boot page shows.

  boot-progress.py --namespace memex [--context colima] [--timeout 900] [--interval 3]
  boot-progress.py --self-test

ONE source: the portal's own /_boot/progress (MeshWeaver.Plugins BootProgressEndpoint, rendered by
core's BootProgressReport). It is read through the API server's POD proxy of the NEWEST portal pod —
not the Service — so during a rolling restart the old, still-ready pod cannot answer for the new one.
Nothing here derives a fact: it prints what changed between two readings.

Exit 0 when the portal says ready, or when it cannot say anything (an image that predates the
endpoint: 404) — the caller's `rollout status` then decides as before. Exit 2 on timeout.
"""
import json
import subprocess
import sys
import time

SELECTOR = "app.kubernetes.io/component=memex-portal"
MARK = {"done": "✓", "failed": "✗", "running": "●", "skipped": "–", "pending": "○"}


def dur(s):
    if s is None:
        return ""
    s = int(round(s))
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"


def size(b):
    if b is None:
        return ""
    return f"{b / 1048576:.1f} MB" if b >= 1048576 else f"{max(1, round(b / 1024))} KB"


def lines(prev, cur, now, last_beat):
    """What to print for reading `cur` after reading `prev` (None = first). Pure.
    Returns (lines, beat_time)."""
    out = []
    prev = prev or {}
    if cur.get("ready"):
        out.append(f"ok portal ready after {dur(cur.get('uptimeSeconds'))}")
        return out, now
    pphases = {p["id"]: p for p in prev.get("phases") or []}
    for p in cur.get("phases") or []:
        before = pphases.get(p["id"])
        if before is None or before.get("state") != p.get("state"):
            if p.get("state") == "running":
                out.append(f"==> {p['title']}" + (f" — {p['detail']}" if p.get("detail") else ""))
            elif p.get("state") in ("done", "failed", "skipped"):
                counts = f" {p.get('done', 0)}/{p['total']}" if p.get("total") is not None else ""
                out.append(f"    {MARK.get(p['state'], '·')} {p['title']}{counts} {dur(p.get('seconds'))}"
                           + (f" — {p['detail']}" if p.get("detail") else ""))
    pmods = {m["package"]: m for m in prev.get("modules") or []}
    for m in cur.get("modules") or []:
        before = pmods.get(m["package"], {})
        if m.get("state") in ("landed", "current", "failed") and before.get("state") != m.get("state"):
            bits = [m["package"], m.get("version") or "", size(m.get("bytes")),
                    dur(m.get("downloadSeconds")), m["state"] + (f": {m['error']}" if m.get("error") else "")]
            out.append("      " + ("✗ " if m["state"] == "failed" else "✓ ") + "  ".join(b for b in bits if b))
    for e in (cur.get("errors") or [])[len(prev.get("errors") or []):]:
        out.append(f"warn {e}")
    pchecks = {c["name"]: c.get("description") for c in prev.get("checks") or []}
    checks = {c["name"]: c.get("description") for c in cur.get("checks") or []}
    if checks != pchecks and checks and not any(p.get("state") == "running" for p in cur.get("phases") or []):
        out.append("    waiting for: " + "; ".join(f"{k} — {v}" if v else k for k, v in sorted(checks.items())))
    if now - last_beat >= 15:
        running = [p for p in cur.get("phases") or [] if p.get("state") == "running"]
        beat = cur.get("headline", "")
        if running and running[-1].get("total") is not None:
            r = running[-1]
            beat = f"{r['title']}: {r.get('done', 0)} of {r['total']}"
        if cur.get("etaSeconds") is not None:
            beat += f" · about {dur(cur['etaSeconds'])} left"
        out.append(f"    … {beat} (up {dur(cur.get('uptimeSeconds'))})")
        last_beat = now
    return out, last_beat


def kubectl(context, *args):
    return subprocess.run(["kubectl", "--context", context, *args], capture_output=True, text=True, timeout=20)


def newest_pod(context, ns):
    p = kubectl(context, "-n", ns, "get", "pods", "-l", SELECTOR,
                "--sort-by=.metadata.creationTimestamp", "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.phase}{'\\n'}{end}")
    rows = [r.split() for r in p.stdout.splitlines() if r.strip()]
    return rows[-1] if rows else None


def read(context, ns, pod):
    p = kubectl(context, "get", "--raw", f"/api/v1/namespaces/{ns}/pods/{pod}:8080/proxy/_boot/progress")
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        return None, ("absent" if "could not find the requested resource" in err or "404" in err else err[-160:])
    try:
        return json.loads(p.stdout), None
    except ValueError:
        return None, "not JSON"


def follow(ns, context, timeout, interval):
    start = time.time()
    prev, last_beat, waiting_said, pod_seen = None, time.time(), 0.0, None
    while time.time() - start < timeout:
        pod = newest_pod(context, ns)
        if pod and pod[0] != pod_seen:
            pod_seen, prev = pod[0], None
            print(f"    following {pod[0]}", flush=True)
        if pod is None and time.time() - start >= 180:
            print("    no portal pod appeared in 180 s — waiting on the rollout instead", flush=True)
            return 0
        reading, why = read(context, ns, pod[0]) if pod else (None, "no portal pod yet")
        if why == "absent":
            print("    this portal image does not report its boot progress yet — waiting on the rollout instead", flush=True)
            return 0
        if reading is None:
            if time.time() - waiting_said >= 30:
                print(f"    the portal process is not answering yet ({pod[1] if pod else 'no pod'})", flush=True)
                waiting_said = time.time()
        else:
            out, last_beat = lines(prev, reading, time.time(), last_beat)
            for line in out:
                print(line, flush=True)
            if reading.get("ready"):
                return 0
            prev = reading
        time.sleep(interval)
    print(f"warn the portal did not report ready within {timeout} s", flush=True)
    return 2


def self_test():
    first = {"ready": False, "headline": "Landing modules — 0 of 2", "uptimeSeconds": 40,
             "phases": [{"id": "resolve", "title": "Resolving the module set", "state": "done", "seconds": 3.2,
                         "detail": "2 package(s)"},
                        {"id": "modules", "title": "Landing modules", "state": "running", "done": 0, "total": 2,
                         "detail": "2 module(s) in 1 dependency level(s), up to 4 at once"}],
             "modules": [{"package": "Mcp", "state": "running"}, {"package": "OgCard", "state": "queued"}]}
    out, _ = lines(None, first, 100.0, 100.0)
    assert out == ["    ✓ Resolving the module set 3 s — 2 package(s)",
                   "==> Landing modules — 2 module(s) in 1 dependency level(s), up to 4 at once"], out
    second = json.loads(json.dumps(first))
    second["phases"][1]["done"] = 1
    second["modules"][0].update(state="landed", version="1.7.14", bytes=19970639, downloadSeconds=18.3)
    second["etaSeconds"] = 20
    out, beat = lines(first, second, 120.0, 100.0)
    assert out[0] == "      ✓ Mcp  1.7.14  19.0 MB  18 s  landed", out
    assert out[1] == "    … Landing modules: 1 of 2 · about 20 s left (up 40 s)", out
    assert beat == 120.0
    out, _ = lines(second, second, 121.0, 120.0)
    assert out == [], "an unchanged reading prints nothing between beats"
    third = json.loads(json.dumps(second))
    third["errors"] = ["OgCard: HTTP 503 from the registry"]
    out, _ = lines(second, third, 122.0, 120.0)
    assert out == ["warn OgCard: HTTP 503 from the registry"], out
    out, _ = lines(third, {"ready": True, "uptimeSeconds": 300}, 130.0, 120.0)
    assert out == ["ok portal ready after 5 min 00 s"], out
    print("boot-progress.py self-test: ok")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    opts = {"--namespace": "memex", "--context": "colima", "--timeout": "900", "--interval": "3"}
    it = iter(argv)
    for a in it:
        if a not in opts:
            print(f"boot-progress.py: unknown argument {a}", file=sys.stderr)
            return 64
        opts[a] = next(it)
    return follow(opts["--namespace"], opts["--context"], int(opts["--timeout"]), float(opts["--interval"]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
