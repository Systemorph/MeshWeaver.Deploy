#!/usr/bin/env python3
"""
FOLLOW THE SEALED PLATFORM SET — a leased Mac's build host never falls behind what main needs.

A leased Mac attaches once (first-attach.sh). From then on THIS agent keeps its runners on the platform set the fleet's
`latest` channel runs: the set CD sealed, armed and handed over into the control instance's deployment record
(`buildHostSets.latest`), read from control as one COMPLETE set — version and the tester, portal and runner digests
(`GET /api/builds/channels/<follow>/set`, MeshWeaver.Plugins). When the release runs a different set, it re-applies the
SAME chart through the same `helm upgrade` first-attach uses, changing only the four set values (`--reuse-values`).

Why the Mac pulls, and why from `latest` (MeshWeaver.Plugins Hosting/StandingBuildHosts, "A leased host follows the set"):
  • control cannot reach a leased Mac (behind its owner's NAT) — the host must pull, whatever decides;
  • `latest`'s handed-over set is exactly what main is built and tested against (a sealed set no main merge outruns for
    long), and the set a Mac must present to SERVE `latest`'s runs at all (ServesTheSet) — so following it is following
    what main needs, with nothing pinned anywhere;
  • the digests come from the authority: the Mac needs no registry read and no cloud CLI to know what to run.

Draining: nothing here waits or kills. The runners stop claiming on SIGTERM and finish the run in hand
(build-run.py `stop`; terminationGracePeriodSeconds = the run deadline), so the chart's own rollout IS the drain.

Fails CLOSED — nothing is changed when control cannot be read, answers a set that is not complete or not well-formed,
the release cannot be read, or a helm operation on it is still pending. Every pass prints ONE line saying what it did.

Usage:
  follow-set.py --once   [--profile build] [--follow latest] [--chart DIR] [--dry-run]   one pass (launchd runs this)
  follow-set.py --install [--profile build] [--follow latest] [--interval 300]            copy chart + script to
                 ~/.memex-local/buildhost-follow/<profile>/ and load the launchd agent com.memex.local.buildhost-follow
  follow-set.py --uninstall [--profile build]
  follow-set.py --self-test
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Callable, Optional

LABEL = "com.memex.local.buildhost-follow"
VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
FIELDS = ("platformVersion", "testerDigest", "portalDigest", "runnerDigest")
HOME = os.environ.get("MEMEX_LOCAL_HOME") or os.path.join(os.path.expanduser("~"), ".memex-local")

Runner = Callable[[list], "tuple[int, str]"]
Fetcher = Callable[[str], "tuple[int, str]"]


def run(cmd: list) -> "tuple[int, str]":
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def fetch(url: str) -> "tuple[int, str]":
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (OSError, ValueError) as e:
        return 0, str(e)


def desired_set(api: str, follow: str, get: Fetcher) -> "tuple[Optional[dict], str]":
    """The complete set `follow` runs, as control's record hands it over; (None, why) for anything else."""
    status, body = get(f"{api.rstrip('/')}/channels/{follow}/set")
    if status != 200:
        return None, f"control answered {status or 'nothing'} for channel {follow}'s set"
    try:
        doc = json.loads(body)
    except ValueError:
        return None, "control's answer is not JSON"
    if not isinstance(doc, dict) or doc.get("complete") is not True:
        return None, f"channel {follow} names no COMPLETE set"
    s = {k: doc.get(k) for k in FIELDS}
    if not (isinstance(s["platformVersion"], str) and VERSION.match(s["platformVersion"])):
        return None, f"channel {follow}'s version {s['platformVersion']!r} is not a platform version"
    for k in FIELDS[1:]:
        if not (isinstance(s[k], str) and DIGEST.match(s[k])):
            return None, f"channel {follow}'s {k} {s[k]!r} is not sha256:<64 hex>"
    return s, "ok"


def deployed_set(release: str, ns: str, ctx: str, channel: str, sh: Runner) -> "tuple[Optional[dict], str]":
    """The set the release runs now ({} when it names none); None when the release cannot be read or is mid-operation."""
    rc, out = sh(["helm", "status", release, "-n", ns, "--kube-context", ctx, "-o", "json"])
    if rc != 0:
        return None, f"release {release} cannot be read (helm status exit {rc})"
    try:
        state = (json.loads(out).get("info") or {}).get("status")
    except ValueError:
        return None, "helm status is not JSON"
    if state != "deployed":
        return None, f"release {release} is {state!r}, not deployed — a pending operation is never overlapped"
    rc, out = sh(["helm", "get", "values", release, "-n", ns, "--kube-context", ctx, "-o", "json"])
    if rc != 0:
        return None, f"release {release}'s values cannot be read (exit {rc})"
    try:
        values = json.loads(out) or {}
    except ValueError:
        return None, "helm values are not JSON"
    ch = ((values.get("buildHosts") or {}).get(channel) or {})
    return {k: ch.get(k) for k in FIELDS}, "ok"


def upgrade_cmd(release: str, ns: str, ctx: str, chart: str, channel: str, s: dict) -> list:
    cmd = ["helm", "upgrade", release, chart, "--kube-context", ctx, "-n", ns, "--reuse-values"]
    for k in FIELDS:
        cmd += ["--set-string", f"buildHosts.{channel}.{k}={s[k]}"]
    return cmd + ["--wait=false"]


def follow_once(api: str, follow: str, release: str, ns: str, ctx: str, chart: str, channel: str,
                get: Fetcher, sh: Runner, dry_run: bool = False) -> "tuple[str, int]":
    """One pass: (the line it prints, exit code). Exit 0 for current/applied/nothing-to-follow, 1 for a failed apply."""
    want, why = desired_set(api, follow, get)
    if want is None:
        return f"follow-set: nothing applied — {why}", 0
    have, why = deployed_set(release, ns, ctx, channel, sh)
    if have is None:
        return f"follow-set: nothing applied — {why}", 0
    if have == want:
        return f"follow-set: current — {release} runs {follow}'s set {want['platformVersion']}", 0
    if not os.path.isfile(os.path.join(chart, "Chart.yaml")):
        return f"follow-set: nothing applied — no chart at {chart}", 0
    line = f"{have.get('platformVersion') or 'none'} → {want['platformVersion']} (runner {want['runnerDigest'][7:19]})"
    if dry_run:
        return f"follow-set: would apply {line}", 0
    rc, out = sh(upgrade_cmd(release, ns, ctx, chart, channel, want))
    if rc != 0:
        tail = out.strip().splitlines()[-1][:300] if out.strip() else ""
        return f"follow-set: FAILED to apply {line} (helm exit {rc}): {tail}", 1
    return f"follow-set: applied {line} — the runners drain their run in hand, then the new set claims", 0


def agent_dir(profile: str) -> str:
    return os.path.join(HOME, "buildhost-follow", profile)


def plist(script: str, chart: str, a) -> str:
    args = [sys.executable or "/usr/bin/python3", script, "--once", "--profile", a.profile, "--follow", a.follow,
            "--api", a.api, "--chart", chart]
    items = "".join(f"<string>{x}</string>" for x in args)
    log = os.path.join(agent_dir(a.profile), "follow.log")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}.{a.profile}</string>
    <key>ProgramArguments</key><array>{items}</array>
    <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
    <key>StartInterval</key><integer>{a.interval}</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def install(a, sh: Runner) -> int:
    d = agent_dir(a.profile)
    os.makedirs(d, exist_ok=True)
    chart = os.path.join(d, "helm")
    src_chart = a.chart or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "helm")
    if not os.path.isfile(os.path.join(src_chart, "Chart.yaml")):
        print(f"ERROR: no chart at {src_chart}", file=sys.stderr)
        return 2
    # the chart first-attach applied, kept beside the agent: a re-attach refreshes both, a follow pass changes only the set
    staged = chart + ".new"
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(src_chart, staged)
    shutil.rmtree(chart, ignore_errors=True)
    os.replace(staged, chart)
    script = os.path.join(d, "follow-set.py")
    shutil.copyfile(os.path.abspath(__file__), script)
    os.chmod(script, 0o755)
    agents = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
    os.makedirs(agents, exist_ok=True)
    dst = os.path.join(agents, f"{LABEL}.{a.profile}.plist")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(plist(script, chart, a))
    domain = f"gui/{os.getuid()}"
    sh(["launchctl", "bootout", domain, dst])
    rc, out = sh(["launchctl", "bootstrap", domain, dst])
    if rc != 0:
        print(f"ERROR: launchctl could not load {dst}: {out.strip()[:300]}", file=sys.stderr)
        return 1
    print(f"   follow agent {LABEL}.{a.profile} loaded: every {a.interval}s, channel {a.follow} → release build-host-{a.channel}")
    return 0


def uninstall(a, sh: Runner) -> int:
    dst = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", f"{LABEL}.{a.profile}.plist")
    if os.path.exists(dst):
        sh(["launchctl", "bootout", f"gui/{os.getuid()}", dst])
        os.remove(dst)
    print(f"follow agent {LABEL}.{a.profile} removed")
    return 0


# ─── self-test ──────────────────────────────────────────────────────────────────────────────────────────────────
def self_test() -> int:
    fails = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        if not ok:
            fails.append(f"{name}: {detail}")

    V, T, P, R = "3.1.12784", "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64
    good = {"channel": "latest", "platformVersion": V, "testerDigest": T, "portalDigest": P, "runnerDigest": R, "complete": True}
    old = {"platformVersion": "3.1.12752", "testerDigest": T, "portalDigest": P, "runnerDigest": "sha256:" + "d" * 64}

    def control(answer: Optional[dict], status: int = 200) -> Fetcher:
        return lambda url: (status, json.dumps(answer) if answer is not None else "")

    class Helm:
        def __init__(self, values: dict, status: str = "deployed", upgrade_rc: int = 0):
            self.values, self.status, self.upgrade_rc, self.calls = values, status, upgrade_rc, []

        def __call__(self, cmd: list) -> "tuple[int, str]":
            self.calls.append(cmd)
            if cmd[:2] == ["helm", "status"]:
                return 0, json.dumps({"info": {"status": self.status}})
            if cmd[:3] == ["helm", "get", "values"]:
                return 0, json.dumps({"buildHosts": {"mac": self.values}})
            if cmd[:2] == ["helm", "upgrade"]:
                return self.upgrade_rc, "Error: boom" if self.upgrade_rc else "Release upgraded"
            return 1, "unexpected"

        def upgrades(self) -> list:
            return [c for c in self.calls if c[:2] == ["helm", "upgrade"]]

    with tempfile.TemporaryDirectory() as t:
        chart = os.path.join(t, "helm")
        os.makedirs(chart)
        open(os.path.join(chart, "Chart.yaml"), "w").close()
        go = lambda get, sh, **k: follow_once("https://ops.example/api/builds", "latest", "build-host-mac", "build-mac",
                                              "colima-build", chart, "mac", get, sh, **k)

        h = Helm(old)
        line, rc = go(control(good), h)
        up = h.upgrades()
        check("a host BEHIND the followed set is upgraded to it", rc == 0 and len(up) == 1 and "applied 3.1.12752 → 3.1.12784" in line, (line, h.calls))
        check("the upgrade sets exactly the four set values and reuses every other value",
              up and "--reuse-values" in up[0] and f"buildHosts.mac.platformVersion={V}" in up[0]
              and f"buildHosts.mac.runnerDigest={R}" in up[0] and f"buildHosts.mac.testerDigest={T}" in up[0]
              and f"buildHosts.mac.portalDigest={P}" in up[0] and up[0][3] == chart and "--wait=false" in up[0], up)
        check("the upgrade is pinned to the profile's kube context, never the current one",
              up and up[0][up[0].index("--kube-context") + 1] == "colima-build", up)

        h = Helm({k: good[k] for k in FIELDS})
        line, rc = go(control(good), h)
        check("a host ON the followed set changes nothing (negative control)", rc == 0 and h.upgrades() == [] and "current" in line, line)

        for name, answer, status in [
            ("control unreachable", None, 0), ("control 503", good, 503),
            ("an incomplete set", {**good, "complete": False}, 200),
            ("no set named", {"channel": "latest", "platformVersion": None, "complete": False}, 200),
            ("a malformed digest", {**good, "runnerDigest": "sha256:xyz"}, 200),
            ("a malformed version", {**good, "platformVersion": "latest"}, 200),
        ]:
            h = Helm(old)
            line, rc = go(control(answer, status), h)
            check(f"{name}: nothing is applied (fail closed)", rc == 0 and h.upgrades() == [] and "nothing applied" in line, line)

        h = Helm(old, status="pending-upgrade")
        line, rc = go(control(good), h)
        check("a release mid-operation is never overlapped", h.upgrades() == [] and "pending" in line, line)

        h = Helm(old, upgrade_rc=1)
        line, rc = go(control(good), h)
        check("a failed upgrade is said and exits 1", rc == 1 and "FAILED" in line and "Error: boom" in line, line)

        h = Helm(old)
        line, rc = go(control(good), h, dry_run=True)
        check("--dry-run says what it would apply and applies nothing", rc == 0 and h.upgrades() == [] and "would apply" in line, line)

        h = Helm(old)
        line, rc = follow_once("https://ops.example/api/builds", "latest", "build-host-mac", "build-mac", "colima-build",
                               os.path.join(t, "nochart"), "mac", control(good), h)
        check("no chart beside the agent: nothing applied", h.upgrades() == [] and "no chart" in line, line)

        seen = []
        follow_once("https://ops.example/api/builds/", "latest", "r", "n", "c", chart, "mac",
                    lambda url: (seen.append(url), (200, json.dumps(good)))[1], Helm(old))
        check("the set is read from control's complete-set route", seen == ["https://ops.example/api/builds/channels/latest/set"], seen)

        a = argparse.Namespace(profile="build", follow="latest", api="https://ops.example/api/builds", interval=300, channel="mac")
        p = plist("/x/follow-set.py", "/x/helm", a)
        check("the agent runs one pass every interval with the attach's api and chart",
              f"<string>{LABEL}.build</string>" in p and "<integer>300</integer>" in p and "<string>--once</string>" in p
              and "<string>https://ops.example/api/builds</string>" in p and "<string>/x/helm</string>" in p, p)

    if fails:
        for f in fails:
            print(f"SELF-TEST FAILED: {f}")
        return 1
    print("follow-set self-test: all cases passed (behind → upgraded with exactly the four set values and --reuse-values, "
          "pinned context; current → nothing; unreachable/503/incomplete/unnamed/malformed → nothing (fail closed); "
          "pending release never overlapped; failed upgrade red; dry-run; no chart; the complete-set route; the agent plist)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    ap.add_argument("--profile", default="build")
    ap.add_argument("--channel", default="mac", help="the host's own channel (release build-host-<channel>)")
    ap.add_argument("--follow", default="latest", help="the channel whose sealed set this host follows")
    ap.add_argument("--api", default="https://control.systemorph.com/api/builds")
    ap.add_argument("--chart", default=None)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.install:
        return install(a, run)
    if a.uninstall:
        return uninstall(a, run)
    chart = a.chart or os.path.join(agent_dir(a.profile), "helm")
    os.makedirs(agent_dir(a.profile), exist_ok=True)
    # one pass at a time: launchd never overlaps a pass, but a hand-run one could
    with open(os.path.join(agent_dir(a.profile), ".lock"), "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("follow-set: nothing applied — another pass is running")
            return 0
        line, rc = follow_once(a.api, a.follow, f"build-host-{a.channel}", f"build-{a.channel}", f"colima-{a.profile}",
                               chart, a.channel, fetch, run, dry_run=a.dry_run)
    print(line, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
