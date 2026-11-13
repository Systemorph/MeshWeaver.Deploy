#!/usr/bin/env python3
"""memex-local IDLE-ONLY MODELS — "CI first, a large model when the build host is idle" (MeshWeaver.Plugins
Hosting/BackLease §15.9).

A Mac Studio that is both a CI build host (Colima profile `build`) and a token-serving Mac cannot hold the build VM
and a large model at once: measured on a 128 GB M5 Max, the build VM holds about 56 GB resident even when idle and
gpt-oss:120b needs about 70 GB. The owner DECLARES the models that may run only while the build VM is stopped, beside
the other token opt-in keys in ~/.memex-local/backlease.yaml:

    Hosting__BackLease__Accept__Tokens__IdleOnlyModels__0: "gpt-oss:120b"

This program is the HOST half: it decides when the build VM yields to those models and when CI takes it back, and it
writes the decision where the portal's token worker reads it — ~/.memex-local/backlease/buildhost.json, mounted
read-only in the portal as /host-readings/buildhost.json:

    {"state": "running|draining|stopped", "since": "<ISO-8601 UTC>", "reason": "<short text>"}

The worker lists an idle-only model in a claim ONLY while the file says `stopped`. An absent or unreadable file serves
none, so CI first is the default at every step where something is unknown.

THE STATE MACHINE (every MEMEX_IDLE_INTERVAL seconds, default 15; never preempts):
    running  → stopped   CI's `mac` demand is 0 AND some idle-only model has a job queued for this Mac.
                         The CI demand is read AGAIN right before `colima stop -p build`; the state is written only
                         once the VM has stopped.
    stopped  → draining  CI's `mac` demand is > 0 (or unreadable — CI first), OR no idle-only job queued or running for
                         MEMEX_IDLE_GIVEBACK seconds (default 600), OR no idle-only model is declared any more.
                         `draining` is written FIRST: the worker stops claiming idle-only jobs; the step in progress
                         finishes.
    draining → running   once every idle-only model reports running == 0 (an unreadable demand counts as 0 — CI
                         first; a drain is bounded by MEMEX_IDLE_DRAIN_MAX, default 1800 s): each idle-only model is
                         unloaded from Ollama (keep_alive 0 — never another model, never a server restart), the VM is
                         started (`colima start -p build`, the kubectl context kept), then `running` is written.

THE READS:
    CI demand        GET {control}/api/builds/queue?channel=mac — anonymous; {"demand": claimable + running}.
    idle-only demand POST {control}/api/fleet-models/demand {"host", "models"} — SIGNED with this Mac's OWN key exactly as
                     the portal's token worker signs: X-MeshWeaver-Deployment, X-MeshWeaver-Timestamp (unix seconds),
                     X-Hub-Signature-256 = sha256=hex(HMAC-SHA256(key, "<dep>\\n<ts>\\nPOST /demand\\n\\n\\n<body>")) —
                     the two empty lines are the unused residency and user fields. A failed read (control down, a
                     control that does not serve the route yet: 404) is NO idle demand.
The key is Hosting__ModuleReportSecret from the files in MEMEX_IDLE_KEY_FILES (fleet.yaml, then the overlay). It is
never printed, logged or put on an argument list.

Run by `memex-local buildhost idle run` (the launchd agent com.memex.local.buildhost-idle), which puts the toolchain
memex-local resolved (colima, kubectl) on PATH. Commands: run | once | status. Python 3.9 grammar (macOS's python3).
Nothing here builds a shell command from a string: every external program is called with an argument LIST.
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

STATES = ("running", "draining", "stopped")
STATE_FILE_NAME = "buildhost.json"
IDLE_KEY = "Hosting__BackLease__Accept__Tokens__IdleOnlyModels"
LOG_MAX_BYTES = 1048576


def env(name, default):
    v = os.environ.get(name, "")
    return v if v != "" else default


def env_int(name, default):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


LOCAL_HOME = env("MEMEX_LOCAL_HOME", os.path.join(os.path.expanduser("~"), ".memex-local"))
PROFILE = env("MEMEX_BUILD_PROFILE", "build")
BACKLEASE_FILE = env("MEMEX_IDLE_BACKLEASE_FILE", os.path.join(LOCAL_HOME, "backlease.yaml"))
READINGS_DIR = env("MEMEX_IDLE_READINGS_DIR", os.path.join(LOCAL_HOME, "backlease"))
STATE_PATH = os.path.join(READINGS_DIR, STATE_FILE_NAME)
MEMORY_PATH = env("MEMEX_IDLE_MEMORY", os.path.join(LOCAL_HOME, "buildhost-idle.memory.json"))
LOG_PATH = env("MEMEX_IDLE_LOG", os.path.join(LOCAL_HOME, "buildhost-idle.log"))
KEY_FILES = [p for p in env("MEMEX_IDLE_KEY_FILES",
                            os.path.join(LOCAL_HOME, "fleet.yaml") + ":" + os.path.join(LOCAL_HOME, "values.local.yaml")).split(":") if p]
COLIMA_HOME = env("COLIMA_HOME", os.path.join(os.path.expanduser("~"), ".colima"))
OLLAMA = env("MEMEX_IDLE_OLLAMA", "http://127.0.0.1:11434").rstrip("/")
CHANNEL = env("MEMEX_IDLE_CHANNEL", "mac")
INTERVAL = env_int("MEMEX_IDLE_INTERVAL", 15)
GIVEBACK = env_int("MEMEX_IDLE_GIVEBACK", 600)
DRAIN_MAX = env_int("MEMEX_IDLE_DRAIN_MAX", 1800)
HTTP_TIMEOUT = env_int("MEMEX_IDLE_HTTP_TIMEOUT", 10)
VM_TIMEOUT = env_int("MEMEX_IDLE_VM_TIMEOUT", 900)
USER_AGENT = "memex-local-buildhost-idle"


def now_s():
    fake = os.environ.get("MEMEX_IDLE_NOW", "")
    return int(fake) if fake else int(time.time())


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            open(LOG_PATH, "w").close()
        with open(LOG_PATH, "a") as f:
            f.write("%s %s\n" % (iso(now_s()), msg))
    except OSError:
        pass


# ── the declaration (re-read every tick: a changed answer takes effect without a restart) ─────────────────────────────
def layer_value(text, key):
    m = re.search(r'(?m)^\s*' + re.escape(key) + r':\s*"?([^"\n]*?)"?\s*$', text)
    return m.group(1).strip() if m else ""


def declaration(path=None):
    """(control, deployment, [idle-only models]) from the back-lease layer; empty values when it is absent."""
    try:
        with open(path or BACKLEASE_FILE) as f:
            text = f.read()
    except OSError:
        return "", "", []
    models = {}
    for m in re.finditer(r'(?m)^\s*' + re.escape(IDLE_KEY) + r'__(\d+):\s*"?([^"\s]+)"?\s*$', text):
        models[int(m.group(1))] = m.group(2)
    return (layer_value(text, "Hosting__ReportTo").rstrip("/"), layer_value(text, "Hosting__Deployment"),
            [models[i] for i in sorted(models)])


def read_key(files=None):
    for p in files or KEY_FILES:
        try:
            with open(p) as f:
                for line in f:
                    m = re.match(r'\s*Hosting__ModuleReportSecret:\s*"?([^"\s]+)"?', line)
                    if m:
                        return m.group(1)
        except OSError:
            continue
    return None


# ── the signed demand read (the portal's FleetModels.SignedText with no residency and no user) ─────────────────────────
def signed_text(deployment, timestamp, route, body):
    return "%s\n%s\nPOST %s\n\n\n%s" % (deployment, timestamp, route, body)


def sign(text, key):
    return "sha256=" + hmac.new(key.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def demand_request(control, deployment, models, key, timestamp):
    body = json.dumps({"host": deployment, "models": models}, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-MeshWeaver-Deployment": deployment,
        "X-MeshWeaver-Timestamp": str(timestamp),
        "X-Hub-Signature-256": sign(signed_text(deployment, str(timestamp), "/demand", body), key),
    }
    return control + "/api/fleet-models/demand", body, headers


def http(url, body=None, headers=None):
    """(status, text) — status 0 with the reason when nothing answered."""
    req = urllib.request.Request(url, data=body.encode("utf-8") if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers=headers or {"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001 — any failure to read is "unread", said by the caller
        return 0, str(e)[:160]


def read_ci_demand(control):
    """(demand int | None, why). None = unread, which counts as demand: CI first."""
    if not control.startswith("https://") and not control.startswith("http://"):
        return None, "no control instance declared (Hosting__ReportTo)"
    st, text = http("%s/api/builds/queue?channel=%s" % (control, CHANNEL))
    if st != 200:
        return None, "build queue unread (%s)" % ("HTTP %d" % st if st else text or "no answer")
    try:
        d = json.loads(text)
        v = d.get("demand")
        if v is None:
            v = int(d.get("claimable", 0)) + int(d.get("running", 0))
        return int(v), "ok"
    except (ValueError, TypeError, AttributeError):
        return None, "build queue answer unreadable"


def read_idle_demand(control, deployment, models):
    """({model: (queued, running)} | None, why). None = unread, which is NO idle demand: CI first."""
    if not models:
        return {}, "none declared"
    key = read_key()
    if not key:
        return None, "no Hosting__ModuleReportSecret on this Mac to sign with"
    if not deployment:
        return None, "no Hosting__Deployment declared"
    url, body, headers = demand_request(control, deployment, models, key, now_s())
    st, text = http(url, body, headers)
    if st != 200:
        return None, "demand unread (%s)" % ("HTTP %d" % st if st else text or "no answer")
    try:
        got = json.loads(text).get("models") or {}
        out = {}
        for m in models:
            v = got.get(m) or {}
            out[m] = (int(v.get("queued", 0)), int(v.get("running", 0)))
        return out, "ok"
    except (ValueError, TypeError, AttributeError):
        return None, "demand answer unreadable"


# ── the decision, pure ───────────────────────────────────────────────────────────────────────────────────────────────
def decide(state, declared, ci, idle, now, last_idle, drain_since):
    """The next step: (action, reason) with action in None | "stop" | "drain" | "start". Pure.

    ci: the build queue's demand for the channel, None when unread (counts as demand — CI first).
    idle: {model: (queued, running)}, None when unread (counts as no idle demand — CI first).
    last_idle: when an idle-only job was last seen queued or running; drain_since: when draining began.
    """
    queued = sum(q for q, _ in (idle or {}).values())
    running = sum(r for _, r in (idle or {}).values())
    ci_wants = ci is None or ci > 0
    if state == "running":
        if declared and not ci_wants and queued > 0:
            return "stop", "no %s build demand; %d idle-only job(s) queued" % (CHANNEL, queued)
        return None, ""
    if state == "stopped":
        if ci is None:
            return "drain", "build queue unread — CI first"
        if ci > 0:
            return "drain", "%s build demand %d" % (CHANNEL, ci)
        if not declared:
            return "drain", "no idle-only model declared any more"
        if queued + running == 0 and now - last_idle >= GIVEBACK:
            return "drain", "no idle-only job queued or running for %d s" % (now - last_idle)
        return None, ""
    if state == "draining":
        if idle is None:
            return "start", "idle-only demand unread — CI first"
        if running == 0:
            return "start", "no idle-only step running"
        if now - drain_since >= DRAIN_MAX:
            return "start", "%d idle-only step(s) still running after %d s — CI first" % (running, now - drain_since)
        return None, ""
    return None, ""


# ── the state file (atomic: temp + rename in the same directory) ────────────────────────────────────────────────────
def write_json_atomic(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_state():
    d = read_json(STATE_PATH)
    if isinstance(d, dict) and d.get("state") in STATES:
        return d
    return None


def write_state(state, reason, now):
    write_json_atomic(STATE_PATH, {"state": state, "since": iso(now), "reason": reason[:200]})


# ── the host: the build VM and Ollama ────────────────────────────────────────────────────────────────────────────────
def has_build_profile():
    return os.path.isdir(os.path.join(COLIMA_HOME, PROFILE))


def run(cmd, timeout=VM_TIMEOUT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()[-300:]
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)[:200]


def kube_context():
    rc, out = run(["kubectl", "config", "current-context"], timeout=20)
    return out.strip() if rc == 0 else ""


def colima(verb):
    """`colima stop|start -p <profile>`, the kubectl context kept as it was (colima switches it on start)."""
    ctx = kube_context()
    rc, out = run(["colima", verb, "-p", PROFILE])
    if ctx:
        run(["kubectl", "config", "use-context", ctx], timeout=20)
    return rc, out


def unload(model):
    st, text = http(OLLAMA + "/api/generate", json.dumps({"model": model, "keep_alive": 0}),
                    {"Content-Type": "application/json", "User-Agent": USER_AGENT})
    return st, text


def still_resident(models):
    """The given models Ollama still holds loaded (GET /api/ps). An Ollama that does not answer holds nothing: none."""
    st, text = http(OLLAMA + "/api/ps")
    if st != 200:
        return []
    try:
        loaded = [m.get("name") or m.get("model") for m in json.loads(text).get("models") or []]
    except (ValueError, TypeError, AttributeError):
        return []
    return [m for m in models if m in loaded or (m + ":latest") in loaded]


# ── one tick ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def tick():
    now = now_s()
    control, deployment, models = declaration()
    cur = read_state()
    memory = read_json(MEMORY_PATH) or {}
    state = cur["state"] if cur else "running"
    if cur is None:
        if not models:
            return "idle: nothing declared"
        write_state("running", "the build host serves CI (idle-only models declared)", now)
        log("running: state file written (idle-only models: %s)" % " ".join(models))
    if not has_build_profile():
        if state != "running":
            write_state("running", "no build profile %s on this Mac" % PROFILE, now)
            log("running: no build profile %s — idle-only models are not served" % PROFILE)
        return "no build profile"
    if state == "running" and not models:
        return "running; nothing declared"
    ci, ci_why = read_ci_demand(control)
    idle, idle_why = read_idle_demand(control, deployment, models)
    if idle and any(q + r > 0 for q, r in idle.values()):
        memory["lastIdleDemandAt"] = now
    last_idle = int(memory.get("lastIdleDemandAt", 0) or 0)
    drain_since = int(memory.get("drainSince", now) or now)
    action, reason = decide(state, bool(models), ci, idle, now, last_idle, drain_since)
    reading = "ci=%s%s idle=%s%s" % (ci, "" if ci_why == "ok" else " (%s)" % ci_why,
                                    json.dumps(idle, separators=(",", ":")) if idle is not None else "None",
                                    "" if idle_why in ("ok", "none declared") else " (%s)" % idle_why)
    if memory.get("lastReading") != reading:
        log("%s: %s" % (state, reading))
        memory["lastReading"] = reading
    if action == "stop":
        ci2, why2 = read_ci_demand(control)
        if ci2 != 0:
            log("running: stop cancelled — the build queue now reads %s (%s)" % (ci2, why2))
        else:
            rc, out = colima("stop")
            if rc == 0:
                write_state("stopped", reason, now)
                memory["lastIdleDemandAt"] = now
                memory["servedWhileStopped"] = models
                log("stopped: colima stop -p %s — %s" % (PROFILE, reason))
            else:
                log("ERROR: colima stop -p %s failed (rc %d): %s — the build host stays running" % (PROFILE, rc, out))
    elif action == "drain":
        write_state("draining", reason, now)
        memory["drainSince"] = now
        log("draining: %s" % reason)
        state = "draining"
    elif action == "start":
        for m in models_to_unload(models, memory):
            st, why = unload(m)
            log("unload %s from Ollama: %s" % (m, "ok" if st == 200 else ("HTTP %d" % st if st else why)))
        resident = still_resident(models_to_unload(models, memory))
        if resident and now - drain_since < DRAIN_MAX:
            # never start the VM under a resident large model — the contention this governor exists to prevent
            log("draining: %s still resident in Ollama — the VM starts once it is unloaded (retried next tick)" % " ".join(resident))
            action = None
        elif resident:
            log("WARN: %s still resident after %d s of draining — starting the VM anyway, CI first" % (" ".join(resident), now - drain_since))
    if action == "start":
        rc, out = colima("start")
        if rc == 0:
            write_state("running", "CI first: %s" % reason, now)
            memory.pop("drainSince", None)
            memory.pop("servedWhileStopped", None)
            log("running: colima start -p %s — %s" % (PROFILE, reason))
        else:
            log("ERROR: colima start -p %s failed (rc %d): %s — still draining, retried next tick" % (PROFILE, rc, out))
    try:
        write_json_atomic(MEMORY_PATH, memory)
    except OSError:
        pass
    return "%s %s" % (action or "hold", reason)


def models_to_unload(declared, memory):
    """The idle-only models to unload: those declared now plus those declared when the VM stopped (a withdrawn one too).
    Only these: a model the owner serves beside the build VM is never touched."""
    seen = []
    for m in list(declared) + list(memory.get("servedWhileStopped", [])):
        if m not in seen:
            seen.append(m)
    return seen


def status():
    control, deployment, models = declaration()
    st = read_state()
    print("idle-only models: %s" % (" ".join(models) if models else "none declared"))
    if st:
        print("build host: %s since %s (%s)" % (st["state"], st.get("since", "?"), st.get("reason", "")))
    else:
        print("build host: no state file (%s) — the worker serves no idle-only model" % STATE_PATH)
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "once":
        print(tick())
        return 0
    if cmd == "status":
        return status()
    if cmd == "run":
        log("started: profile %s, every %d s, give back after %d s without idle-only demand" % (PROFILE, INTERVAL, GIVEBACK))
        me = os.path.realpath(__file__)
        mtime = os.path.getmtime(me)
        while True:
            try:
                tick()
            except Exception as e:  # noqa: BLE001 — one bad tick never ends the governor; it is logged
                log("ERROR: tick failed: %s" % str(e)[:200])
            time.sleep(INTERVAL)
            # an upgraded executor takes over: exit when this file changed (launchd's KeepAlive starts the new one)
            try:
                if os.path.getmtime(me) != mtime:
                    log("exiting: the executor changed on disk — launchd starts the new one")
                    return 0
            except OSError:
                return 0
    print("usage: buildhost-idle.py [run | once | status]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
