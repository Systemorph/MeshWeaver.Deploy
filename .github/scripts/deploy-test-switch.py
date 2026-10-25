#!/usr/bin/env python3
"""deploy-test-switch.py — the events and the read-back of DEPLOY → TEST THE DEPLOY → SWITCH LATEST.

The target shape of the standing build hosts (platform Doc/Architecture/DeployTestSwitchLatest,
policy `deploy-test-switch-latest`): each sealed set is deployed as its OWN versioned build-server
deployment `build-<version>`, the tests run ON that deployment, and only a green deployment becomes
`latest` — the switch that makes a set available to modules. The workflow is
`.github/workflows/build-host-deploy-test-switch.yml`; this script is everything in it: the
declaration, the four signed event bodies, the reading of the build API's `GET /api/builds/hosts`,
and the pinned probe that proves and tests the deployment.

THE RECEIVERS are MeshWeaver.Plugins' (P3): each event is a signed HMAC POST into the control
plane's inbox, the same path and secret as `build-host-update-available`, and becomes a governed
InstanceAction (`BuildHostDeploy`, `BuildHostSwitch`, `BuildHostTeardown`), authorized as the
caller and executed as System. The shapes below are theirs, copied — change both together.

    deploy    {"event":"build-host-deploy-requested","deployment","version","testerDigest",
               "portalDigest","runnerDigest","coreTestsDigest"?,"reason":"candidate"|"cap:<pkg>"}
    switch    {"event":"build-host-switch-requested","deployment","version","alias":"latest",
               "run":"Hosting/Builds/<repo>/<runId>"}
    rollback  the switch event with "rollback": true and NO run (only to alias/previous)
    teardown  {"event":"build-host-teardown-requested","deployment","version"}

🚨 NEVER TRUST THE INBOX'S 202. Every event is followed by a re-read of `GET /api/builds/hosts`
(anonymous): `{"aliases":{"latest","candidate","previous"},"versions":[{"version",
"deploymentName","complete","maxReplicas","aliases":[…],"pinned":N, …}]}` (MeshWeaver.Plugins
`Hosting/StandingBuildHosts` §7b). A version is LISTED once its `v/` entry is written and COMPLETE
once the record holds its full set, so the chart renders its Deployment. An answer this reader
cannot read is RED, never "not yet".

🚨 THERE IS NO `ready`, AND NONE IS READ. The control instance cannot observe pods truthfully, and
KEDA scales an idle version to ZERO, so "is a pod up" is not a question `/hosts` answers. What
proves a host is up AND staged is a CLAIM: a host claims only after staging its set. So:

    deploy  = request → wait until `/hosts` lists the version `complete: true` → submit a probe
              PINNED to the version (`POST /api/builds/probes` with `version`) → wait until a host
              CLAIMS it. Not complete, or not claimed, inside the deploy window is the deploy
              FAILURE: red, the candidate is torn down, and `latest` does not move (§7).
    test    = the probe's verdict: its run must END `succeeded`. The switch receiver re-reads that
              run (Succeeded, every stage Succeeded, pinned to the version) and never trusts this job.

🚨 SWITCHED OFF UNTIL DECLARED. `.github/deploy-test-switch.json` `enabled: false` (the shipped
value): the workflow writes one summary line and sends nothing. It is turned on only once the
receivers (P3c) are deployed on the control instance.

    deploy-test-switch.py decide --declaration .github/deploy-test-switch.json
        → GITHUB_OUTPUT lines enabled=, why=
    deploy-test-switch.py body deploy --deployment control --version 3.1.N --tester-digest sha256:… \\
        --portal-digest sha256:… --runner-digest sha256:… [--core-tests-digest sha256:…] [--reason candidate]
    deploy-test-switch.py body switch --deployment control --version 3.1.N --run Hosting/Builds/<repo>/<runId>
    deploy-test-switch.py body rollback --deployment control --version 3.1.N
    deploy-test-switch.py body teardown --deployment control --version 3.1.N
        → the event JSON, one line (the bytes that are signed are the bytes that are sent)
    deploy-test-switch.py hosts --hosts hosts.json --version 3.1.N
        → GITHUB_OUTPUT lines listed=, complete=, latest=, candidate=, previous=, deployment_name=
    deploy-test-switch.py wait-complete --api https://<control>/api/builds --version 3.1.N --window S
        → the hosts lines once the version is complete; exit 1 when the window passes first
    deploy-test-switch.py probe-claim --api … --version 3.1.N --repo MeshWeaver.Plugins --sha <40 hex> \\
        [--channel latest] [--stage s …] --window S                         (token: $MW_PROBE_TOKEN)
        → probe=, path=, stage=, run_state= once a host CLAIMED it; exit 1 when the window passes first
    deploy-test-switch.py probe-verdict --api … --repo … --sha … [--channel latest] --probe <id> \\
        --stage s --window S                                                (token: $MW_PROBE_TOKEN)
        → run_state=; exit 0 only for a run that ENDED succeeded
    deploy-test-switch.py --self-test

Every HTTP call goes through `curl` on PATH — the self-test puts a fake one there. key=value lines
go to stdout (the workflow appends them to GITHUB_OUTPUT); everything a person reads goes to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
RUN = re.compile(r"^Hosting/Builds/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEPLOYMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ALIASES = ("latest", "candidate", "previous")
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CHANNEL = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
STAGE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
PROBE_ID = re.compile(r"^[0-9a-f]{12}$")
API = re.compile(r"^(https://[^/?#@\s]+|http://(127\.0\.0\.1|localhost)(:\d+)?)(/[^?#@\s]*)?/api/builds$")
PROBE_TOKEN_ENV = "MW_PROBE_TOKEN"
# The build API's run states (MeshWeaver.Plugins `BuildState`, lower-cased as the log route's `runState`).
UNCLAIMED = ("queued", "admitted")                       # waiting for a host
CLAIMED = ("running", "succeeded", "failed")             # a host took it — and claims only once staged
ENDED_UNCLAIMED = ("cancelled", "superseded", "unobserved")


class Refusal(Exception):
    pass


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def load_declaration(path: Path) -> dict:
    d = json.loads(path.read_text())
    if not isinstance(d, dict) or type(d.get("enabled")) is not bool:
        raise Refusal(f"{path}: `enabled` must be true or false")
    return d


def decide(declaration: dict) -> tuple[bool, str]:
    if not declaration["enabled"]:
        return False, ("deploy → test → switch is not enabled (.github/deploy-test-switch.json enabled: false) "
                       "— nothing is sent; build-latest follows the armed set through build-host.yml")
    return True, "deploy → test → switch is enabled: the candidate is deployed, tested on its deployment, then switched"


def _record(deployment: str) -> str:
    """The receivers name the record by PATH (`Deployments/<name>`); control-instance.json by name."""
    d = (deployment or "").strip()
    name = d[len("Deployments/"):] if d.startswith("Deployments/") else d
    if not DEPLOYMENT.fullmatch(name):
        raise Refusal(f"deployment {deployment!r} is not a plain record name")
    return f"Deployments/{name}"


def _version(v: str) -> str:
    if not VERSION.fullmatch(v or ""):
        raise Refusal(f"version {v!r} is not a released platform version (x.y.z)")
    return v


def _digest(name: str, d: str) -> str:
    if not DIGEST.fullmatch(d or ""):
        raise Refusal(f"{name} digest {d!r} is not sha256:<64 hex>")
    return d


def body(kind: str, *, deployment: str, version: str, tester_digest: str = "", portal_digest: str = "",
         runner_digest: str = "", core_tests_digest: str = "", reason: str = "candidate", run: str = "") -> dict:
    """One event, every field validated. Key order is the wire order: `event` first."""
    dep, ver = _record(deployment), _version(version)
    if kind == "deploy":
        if reason != "candidate" and not (reason.startswith("cap:") and PACKAGE.fullmatch(reason[4:])):
            raise Refusal(f"reason {reason!r} is neither `candidate` nor `cap:<package>`")
        ev = {"event": "build-host-deploy-requested", "deployment": dep, "version": ver,
              "testerDigest": _digest("tester", tester_digest), "portalDigest": _digest("portal", portal_digest),
              "runnerDigest": _digest("runner", runner_digest)}
        if core_tests_digest:
            ev["coreTestsDigest"] = _digest("core-tests", core_tests_digest)
        ev["reason"] = reason
        return ev
    if kind == "switch":
        if not RUN.fullmatch(run or ""):
            raise Refusal(f"run {run!r} is not a Hosting/Builds/<repo>/<runId> node — a switch names the green run "
                          "that tested the candidate")
        return {"event": "build-host-switch-requested", "deployment": dep, "version": ver, "alias": "latest", "run": run}
    if kind == "rollback":
        if run:
            raise Refusal("a rollback names no run — it moves latest back to alias/previous only")
        return {"event": "build-host-switch-requested", "deployment": dep, "version": ver, "alias": "latest",
                "rollback": True}
    if kind == "teardown":
        return {"event": "build-host-teardown-requested", "deployment": dep, "version": ver}
    raise Refusal(f"unknown event kind {kind!r}")


def read_hosts(doc: object, version: str) -> dict:
    """What GET /api/builds/hosts says about `version`. An unreadable answer is a Refusal — never
    "not listed yet", which would turn a broken endpoint into a wait that ends in a timeout."""
    if not isinstance(doc, dict):
        raise Refusal("the hosts answer is not an object")
    aliases, versions = doc.get("aliases"), doc.get("versions")
    if not isinstance(aliases, dict) or not isinstance(versions, list):
        raise Refusal("the hosts answer carries no `aliases` object and `versions` list")
    out = {a: str(aliases.get(a) or "") for a in ALIASES}
    for a in ALIASES:
        if out[a] and not VERSION.fullmatch(out[a]):
            raise Refusal(f"alias {a} names {out[a]!r}, which is not a version")
    entry = None
    for v in versions:
        if not isinstance(v, dict) or not VERSION.fullmatch(str(v.get("version") or "")):
            raise Refusal(f"a versions entry is malformed: {v!r}")
        if v["version"] == version:
            if entry is not None:
                raise Refusal(f"version {version} is listed twice")
            entry = v
    out["listed"] = "true" if entry is not None else "false"
    out["deployment_name"] = str((entry or {}).get("deploymentName") or "")
    if entry is not None and not out["deployment_name"]:
        raise Refusal(f"version {version} is listed with no deploymentName")
    # COMPLETE: the record holds the version's full set, so the chart renders its Deployment. It is
    # NOT "a pod is up" — a complete version may be scaled to zero; the probe's CLAIM proves that.
    # Anything but a JSON `true` is not complete, and `ready` is never read.
    out["complete"] = "true" if entry is not None and entry.get("complete") is True else "false"
    return out


def probe_body(*, repo: str, sha: str, channel: str, version: str, stages: list[str]) -> dict:
    """`POST /api/builds/probes`: the run that tests the candidate, PINNED to its version."""
    if not REPO_NAME.fullmatch(repo or ""):
        raise Refusal(f"probe repo {repo!r} is not a bare repository name (e.g. MeshWeaver.Plugins)")
    if not SHA.fullmatch(sha or ""):
        raise Refusal(f"probe sha {sha!r} is not a full 40-hex commit sha")
    if not CHANNEL.fullmatch(channel or ""):
        raise Refusal(f"probe channel {channel!r} is not channel-shaped")
    bad = [s for s in stages if not STAGE.fullmatch(s)]
    if bad:
        raise Refusal(f"probe stage(s) {bad!r} are not one lower-case path segment")
    out = {"repo": repo, "sha": sha, "channel": channel, "version": _version(version)}
    if stages:
        out["stages"] = list(stages)
    return out


def probe_state(page: object) -> tuple[str, bool]:
    """A probe log page → (run state, claimed). An answer it cannot place is a Refusal, never "not yet"."""
    if not isinstance(page, dict):
        raise Refusal("the probe's log page is not an object")
    state = str(page.get("runState") or "").lower()
    if state in UNCLAIMED:
        return state, False
    if state in CLAIMED:
        return state, True
    if state in ENDED_UNCLAIMED:
        raise Refusal(f"the probe ended `{state}` before any host claimed it")
    raise Refusal(f"the probe's log page carries no known runState (got {page.get('runState')!r}) — "
                  "the probe is unknown to the build API, or the answer's shape changed")


# ───────────────────── HTTP, through `curl` on PATH (the self-test's seam) ─────────────────────

def http(method: str, url: str, *, token: str = "", data: str | None = None) -> tuple[int, str]:
    """One request through curl → (status, body); status 0 when curl could not complete it. The
    token travels in a 0600 header FILE, never on a command line a process listing shows."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "body")
        cmd = ["curl", "-sS", "--connect-timeout", "10", "--max-time", "30", "-o", out, "-w", "%{http_code}",
               "-X", method, "-H", "Accept: application/json"]
        if token:
            hdr = os.path.join(tmp, "auth")
            fd = os.open(hdr, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"Authorization: Bearer {token}\n")
            cmd += ["-H", f"@{hdr}"]
        if data is not None:
            cmd += ["-H", "Content-Type: application/json", "--data", data]
        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        code = proc.stdout.strip()[-3:]
        status = int(code) if proc.returncode == 0 and code.isdigit() else 0
        text = Path(out).read_text() if os.path.exists(out) else ""
        return status, text


def _json(status: int, text: str, what: str) -> object:
    if status != 200:
        raise Refusal(f"{what} answered HTTP {status or 'nothing (curl failed)'}: {text[:300]}")
    try:
        return json.loads(text)
    except ValueError as e:
        raise Refusal(f"{what} answered something that is not JSON") from e


def _api(api: str) -> str:
    base = (api or "").strip().rstrip("/")
    if not API.fullmatch(base):
        raise Refusal(f"the build API {api!r} is not an https …/api/builds base")
    return base


def wait_complete(api: str, version: str, window: float, poll: float) -> dict:
    """Polls GET /hosts until `version` is listed COMPLETE. The window passing first is a Refusal
    naming which half it never reached; an unreadable answer is a Refusal at once."""
    base, version = _api(api), _version(version)
    deadline = time.monotonic() + window
    while True:
        out = read_hosts(_json(*http("GET", f"{base}/hosts"), what=f"GET {base}/hosts"), version)
        if out["complete"] == "true":
            return out
        if time.monotonic() >= deadline:
            raise Refusal(f"build-{version} did not become complete within {int(window)}s (listed: {out['listed']}) — "
                          "the record never held its full set; read the BuildHostDeploy InstanceAction on the control instance")
        time.sleep(poll)


def _log_url(base: str, repo: str, sha: str, stage: str, channel: str, probe: str) -> str:
    return f"{base}/{repo}/{sha}/stages/{stage}/log?after=-1&channel={channel}&probe={probe}"


def probe_claim(api: str, token: str, probe_: dict, window: float, poll: float) -> dict:
    """Submits the pinned probe, then waits until a host CLAIMS it. A host claims only after staging
    the version's set, so the claim is the proof that the deployment serves. Not claimed within the
    window is a Refusal: the deploy FAILED."""
    base = _api(api)
    if not token:
        raise Refusal(f"{PROBE_TOKEN_ENV} is empty — a probe is submitted with a global administrator's own mw_ token")
    status, raw = http("POST", f"{base}/probes", token=token, data=json.dumps(probe_, separators=(",", ":")))
    if status != 202:
        raise Refusal(f"POST {base}/probes answered HTTP {status or 'nothing (curl failed)'}: {raw[:300]}")
    try:
        ans = json.loads(raw)
    except ValueError as e:
        raise Refusal("POST /probes answered something that is not JSON") from e
    ans = ans if isinstance(ans, dict) else {}
    probe = str(ans.get("probe") or "")
    if not PROBE_ID.fullmatch(probe):
        raise Refusal(f"POST /probes answered no probe id (got {probe!r})")
    stages = ans.get("stages") or probe_.get("stages") or []
    # The RUN's state rides on every stage's page; with no stage named, any stage-shaped name reads it.
    stage = str(stages[0]) if stages and STAGE.fullmatch(str(stages[0])) else "run"
    say(f"probe {probe} submitted: {probe_['repo']}@{probe_['sha'][:12]} pinned to {probe_['version']} — waiting for a host to claim it")
    deadline = time.monotonic() + window
    while True:
        page = _json(*http("GET", _log_url(base, probe_["repo"], probe_["sha"], stage, probe_["channel"], probe),
                           token=token), what=f"the probe {probe}'s log")
        state, claimed = probe_state(page)
        if claimed:
            return {"probe": probe, "path": str(ans.get("path") or ""), "stage": stage, "run_state": state}
        if time.monotonic() >= deadline:
            raise Refusal(f"no host claimed probe {probe} (pinned to {probe_['version']}) within {int(window)}s "
                          f"(run state: {state}) — build-{probe_['version']} never served it; the deploy FAILED")
        time.sleep(poll)


def probe_verdict(api: str, token: str, *, repo: str, sha: str, channel: str, probe: str, stage: str,
                  window: float, poll: float) -> dict:
    """Follows the CLAIMED probe until its run ENDS. Green only for `succeeded`."""
    base = _api(api)
    if not PROBE_ID.fullmatch(probe or ""):
        raise Refusal(f"{probe!r} is not a probe id")
    deadline = time.monotonic() + window
    while True:
        page = _json(*http("GET", _log_url(base, repo, sha, stage, channel, probe), token=token),
                     what=f"the probe {probe}'s log")
        state, _ = probe_state(page)
        if isinstance(page, dict) and page.get("runTerminal") is True:
            if state != "succeeded":
                raise Refusal(f"the candidate's probe {probe} ENDED `{state}` — the test is red; latest is NOT switched")
            return {"run_state": state}
        if time.monotonic() >= deadline:
            raise Refusal(f"the candidate's probe {probe} did not end within {int(window)}s (run state: {state}) — "
                          "a test that did not finish is red")
        time.sleep(poll)


# ─────────────────────────────────────────── self-test ───────────────────────────────────────────

FAKE_CURL = r'''#!PYTHON
"""A fake `curl`: answers from the plan in $FAKE_CURL_PLAN and records every call in $FAKE_CURL_LOG."""
import json, os, sys
a = sys.argv[1:]
out = method = data = None
headers = []
i = 0
while i < len(a):
    x = a[i]
    if x in ("-o", "-w", "-X", "-H", "--data", "--connect-timeout", "--max-time"):
        v = a[i + 1]
        if x == "-o": out = v
        elif x == "-X": method = v
        elif x == "--data": data = v
        elif x == "-H": headers.append(open(v[1:]).read().strip() if v.startswith("@") else v)
        i += 2
        continue
    i += 1
url = a[-1]
plan = json.load(open(os.environ["FAKE_CURL_PLAN"]))
log = os.environ["FAKE_CURL_LOG"]
calls = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
with open(log, "a") as f:
    f.write(json.dumps({"method": method, "url": url, "data": data, "headers": headers, "argv": a}) + "\n")
for route in plan:
    if route["method"] == method and route["match"] in url:
        n = sum(1 for c in calls if c["method"] == method and route["match"] in c["url"])
        status, body = route["answers"][min(n, len(route["answers"]) - 1)]
        if status == 0:
            sys.stderr.write("curl: (7) Failed to connect\n"); sys.exit(7)
        open(out, "w").write(body if isinstance(body, str) else json.dumps(body))
        sys.stdout.write(str(status)); sys.exit(0)
open(out, "w").write("{}"); sys.stdout.write("404")
'''


def self_test() -> int:
    fails: list[str] = []
    d = lambda c: "sha256:" + c * 64  # noqa: E731
    total = 0

    def check(name, fn, want=None, refused=False, says=None):
        nonlocal total
        total += 1
        try:
            got = fn()
        except Refusal as e:
            if not refused:
                fails.append(f"{name}: refused — {e}")
            elif says and says not in str(e):
                fails.append(f"{name}: refused for another reason — {e}")
            return
        except Exception as e:  # a crash is never a refusal
            fails.append(f"{name}: {type(e).__name__}: {e}")
            return
        if refused:
            fails.append(f"{name}: accepted {got!r}")
        elif want is not None and not want(got):
            fails.append(f"{name}: {got!r}")

    dep = dict(deployment="control", version="3.1.10412", tester_digest=d("1"), portal_digest=d("2"), runner_digest=d("3"))
    check("deploy body has the receiver's shape", lambda: body("deploy", **dep),
          lambda b: list(b) == ["event", "deployment", "version", "testerDigest", "portalDigest", "runnerDigest", "reason"]
          and b["deployment"] == "Deployments/control" and b["reason"] == "candidate")
    check("deploy with the 5th digest", lambda: body("deploy", core_tests_digest=d("4"), **dep),
          lambda b: b["coreTestsDigest"] == d("4"))
    check("deploy for a cap", lambda: body("deploy", reason="cap:Acme.Pkg", **dep), lambda b: b["reason"] == "cap:Acme.Pkg")
    check("a record path is kept", lambda: body("teardown", deployment="Deployments/control", version="3.1.1"),
          lambda b: b == {"event": "build-host-teardown-requested", "deployment": "Deployments/control", "version": "3.1.1"})
    check("switch names its run", lambda: body("switch", deployment="control", version="3.1.1", run="Hosting/Builds/MeshWeaver/r-1"),
          lambda b: b["alias"] == "latest" and b["run"] == "Hosting/Builds/MeshWeaver/r-1" and "rollback" not in b)
    check("rollback names no run", lambda: body("rollback", deployment="control", version="3.1.1"),
          lambda b: b["rollback"] is True and "run" not in b)
    for name, kw in [
        ("a tag for a digest", {**dep, "runner_digest": "latest"}),
        ("a pre-release version", {**dep, "version": "3.1.1-rc.1"}),
        ("a bad reason", {**dep, "reason": "because"}),
        ("a cap with no package", {**dep, "reason": "cap:"}),
        ("a deployment path with a slash inside", {**dep, "deployment": "Deployments/a/b"}),
    ]:
        check(f"refuses {name}", lambda kw=kw: body("deploy", **kw), refused=True)
    check("refuses a switch with no run", lambda: body("switch", deployment="control", version="3.1.1"), refused=True)
    check("refuses a switch naming a non-run", lambda: body("switch", deployment="control", version="3.1.1", run="Feedback/x"), refused=True)
    check("refuses a rollback with a run", lambda: body("rollback", deployment="control", version="3.1.1", run="Hosting/Builds/a/b"), refused=True)
    check("refuses an unknown kind", lambda: body("promote", deployment="control", version="3.1.1"), refused=True)

    # ── the hosts reader: `complete`, never `ready` ──
    def row(v, complete, **extra):
        return {"version": v, "deploymentName": "build-" + v.replace(".", "-"), "complete": complete,
                "maxReplicas": 2, "aliases": [], "pinned": 0, **extra}
    hosts = {"aliases": {"latest": "3.1.10400", "candidate": "3.1.10412", "previous": "3.1.10390"},
             "versions": [row("3.1.10400", True), row("3.1.10412", False)]}
    check("a listed candidate whose set is not complete", lambda: read_hosts(hosts, "3.1.10412"),
          lambda o: o["listed"] == "true" and o["complete"] == "false" and o["candidate"] == "3.1.10412")
    check("a complete latest", lambda: read_hosts(hosts, "3.1.10400"),
          lambda o: o["complete"] == "true" and o["latest"] == "3.1.10400" and o["deployment_name"] == "build-3-1-10400")
    check("the reader writes no `ready` output", lambda: read_hosts(hosts, "3.1.10400"), lambda o: "ready" not in o)
    check("an absent version is not listed", lambda: read_hosts(hosts, "3.1.1"), lambda o: o["listed"] == "false" and o["complete"] == "false")
    check("`complete: \"true\"` (a string) is not complete",
          lambda: read_hosts({"aliases": {}, "versions": [{"version": "3.1.1", "deploymentName": "b", "complete": "true"}]}, "3.1.1"),
          lambda o: o["complete"] == "false")
    check("NEGATIVE: `ready: true` with no `complete` is NOT complete — `ready` is never read",
          lambda: read_hosts({"aliases": {}, "versions": [{"version": "3.1.1", "deploymentName": "b", "ready": True}]}, "3.1.1"),
          lambda o: o["complete"] == "false")
    check("complete and scaled to zero (`ready: false` beside it) IS complete",
          lambda: read_hosts({"aliases": {}, "versions": [row("3.1.1", True, ready=False)]}, "3.1.1"),
          lambda o: o["complete"] == "true")
    for name, doc in [("a list", []), ("no versions", {"aliases": {}}), ("a malformed entry", {"aliases": {}, "versions": [{"v": 1}]}),
                      ("a twice-listed version", {"aliases": {}, "versions": [{"version": "3.1.1", "deploymentName": "a"}] * 2}),
                      ("an entry with no deployment name", {"aliases": {}, "versions": [{"version": "3.1.1"}]}),
                      ("an alias that is not a version", {"aliases": {"latest": "main"}, "versions": []})]:
        check(f"the reader refuses {name}", lambda doc=doc: read_hosts(doc, "3.1.1"), refused=True)

    # ── the probe body and the run-state reader ──
    sha = "a" * 40
    check("the probe is PINNED to the version", lambda: probe_body(repo="MeshWeaver.Plugins", sha=sha, channel="latest",
                                                                   version="3.1.10412", stages=[]),
          lambda b: b == {"repo": "MeshWeaver.Plugins", "sha": sha, "channel": "latest", "version": "3.1.10412"})
    check("the probe carries its stages when named", lambda: probe_body(repo="R", sha=sha, channel="latest", version="3.1.1",
                                                                       stages=["compile-check"]),
          lambda b: b["stages"] == ["compile-check"])
    for name, kw in [("an owner/repo", {"repo": "Systemorph/R"}), ("a short sha", {"sha": "abc"}),
                     ("a bad channel", {"channel": "Latest"}), ("a bad stage", {"stages": ["A B"]}),
                     ("a bad version", {"version": "latest"})]:
        args = {"repo": "R", "sha": sha, "channel": "latest", "version": "3.1.1", "stages": [], **kw}
        check(f"the probe body refuses {name}", lambda args=args: probe_body(**args), refused=True)
    check("queued is not claimed", lambda: probe_state({"runState": "queued"}), lambda r: r == ("queued", False))
    check("admitted is not claimed", lambda: probe_state({"runState": "admitted"}), lambda r: r == ("admitted", False))
    check("running is claimed", lambda: probe_state({"runState": "running"}), lambda r: r == ("running", True))
    check("an unclaimed end is refused", lambda: probe_state({"runState": "unobserved"}), refused=True, says="before any host claimed")
    check("an unknown or absent runState is refused", lambda: probe_state({"state": "queued"}), refused=True)

    # ── the waits, through a FAKE curl on PATH ──
    with tempfile.TemporaryDirectory() as tmp:
        bin_ = os.path.join(tmp, "bin")
        os.mkdir(bin_)
        curl = os.path.join(bin_, "curl")
        Path(curl).write_text(FAKE_CURL.replace("#!PYTHON", "#!" + sys.executable, 1))
        os.chmod(curl, 0o755)
        saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_CURL_PLAN", "FAKE_CURL_LOG")}
        os.environ["PATH"] = bin_ + os.pathsep + os.environ.get("PATH", "")
        api = "https://control.example.com/api/builds"
        n = [0]

        def scenario(plan: list) -> str:
            n[0] += 1
            p, log = os.path.join(tmp, f"plan{n[0]}.json"), os.path.join(tmp, f"log{n[0]}.jsonl")
            Path(p).write_text(json.dumps(plan))
            os.environ["FAKE_CURL_PLAN"], os.environ["FAKE_CURL_LOG"] = p, log
            return log

        def calls(log: str) -> list[dict]:
            return [json.loads(line) for line in Path(log).read_text().splitlines()] if os.path.exists(log) else []

        def hosts_doc(*rows):
            return {"aliases": {"latest": "3.1.10400", "candidate": "3.1.10412", "previous": None}, "versions": list(rows)}

        V = "3.1.10412"
        probe = "0123456789ab"
        try:
            log = scenario([{"method": "GET", "match": "/hosts", "answers": [
                [200, hosts_doc(row("3.1.10400", True))], [200, hosts_doc(row("3.1.10400", True), row(V, False))],
                [200, hosts_doc(row("3.1.10400", True), row(V, True, ready=False))]]}])
            check("wait-complete waits through NOT LISTED and NOT COMPLETE, then takes a complete version scaled to zero",
                  lambda: wait_complete(api, V, window=5, poll=0.01),
                  lambda o: o["complete"] == "true" and len(calls(log)) == 3)
            scenario([{"method": "GET", "match": "/hosts", "answers": [[200, hosts_doc(row(V, None, ready=True))]]}])
            check("NEGATIVE: a version that only says `ready: true` never completes — red when the window passes",
                  lambda: wait_complete(api, V, window=0.1, poll=0.02), refused=True, says="did not become complete")
            log = scenario([{"method": "GET", "match": "/hosts", "answers": [[503, "{}"]]}])
            check("an unreadable /hosts is red at once, never a wait",
                  lambda: wait_complete(api, V, window=5, poll=0.01), refused=True, says="HTTP 503")
            scenario([{"method": "GET", "match": "/hosts", "answers": [[0, ""]]}])
            check("a /hosts curl cannot reach is red", lambda: wait_complete(api, V, window=5, poll=0.01),
                  refused=True, says="curl failed")

            pb = probe_body(repo="MeshWeaver.Plugins", sha=sha, channel="latest", version=V, stages=[])
            submitted = [202, {"path": f"Hosting/Builds/MeshWeaver.Plugins/{sha}-probe-{probe}", "probe": probe,
                               "channel": "latest", "stages": []}]
            log = scenario([{"method": "POST", "match": "/probes", "answers": [submitted]},
                            {"method": "GET", "match": f"probe={probe}", "answers": [
                                [200, {"runState": "queued"}], [200, {"runState": "admitted"}], [200, {"runState": "running"}]]}])
            check("probe-claim submits the PINNED probe and returns once a host CLAIMED it",
                  lambda: probe_claim(api, "mw_secret", pb, window=5, poll=0.01),
                  lambda o: o["probe"] == probe and o["run_state"] == "running" and o["stage"] == "run"
                  and o["path"].endswith(f"-probe-{probe}"))
            sent = calls(log)
            check("the probe POST carries `version`, and the token only in a header FILE, never on argv",
                  lambda: sent,
                  lambda c: json.loads(c[0]["data"])["version"] == V
                  and "Authorization: Bearer mw_secret" in c[0]["headers"]
                  and not any("mw_secret" in x for call in c for x in call["argv"]))
            check("the claim is read on the probe's log route, as its submitter",
                  lambda: sent[1], lambda c: f"/MeshWeaver.Plugins/{sha}/stages/run/log?" in c["url"]
                  and "channel=latest" in c["url"] and "Authorization: Bearer mw_secret" in c["headers"])
            scenario([{"method": "POST", "match": "/probes", "answers": [submitted]},
                      {"method": "GET", "match": f"probe={probe}", "answers": [[200, {"runState": "queued"}]]}])
            check("NEGATIVE CONTROL: a probe NO host ever claims is red — the deploy FAILED",
                  lambda: probe_claim(api, "mw_secret", pb, window=0.1, poll=0.02), refused=True,
                  says="no host claimed probe")
            scenario([{"method": "POST", "match": "/probes", "answers": [submitted]},
                      {"method": "GET", "match": f"probe={probe}", "answers": [[200, {"runState": "unobserved"}]]}])
            check("a probe that ended before any claim is red", lambda: probe_claim(api, "t", pb, window=5, poll=0.01),
                  refused=True, says="before any host claimed")
            scenario([{"method": "POST", "match": "/probes", "answers": [[403, {"error": "only a global administrator"}]]}])
            check("a refused probe submission is red, naming the answer", lambda: probe_claim(api, "t", pb, window=5, poll=0.01),
                  refused=True, says="HTTP 403")
            scenario([{"method": "POST", "match": "/probes", "answers": [[202, {"probe": "nope"}]]}])
            check("a submission with no probe id is red", lambda: probe_claim(api, "t", pb, window=5, poll=0.01),
                  refused=True, says="no probe id")
            log = scenario([])
            check("an empty token is red before anything is sent",
                  lambda: probe_claim(api, "", pb, window=5, poll=0.01), refused=True, says=PROBE_TOKEN_ENV)
            check("… and nothing was sent", lambda: calls(log), lambda c: c == [])
            check("a build API that is not https is refused", lambda: probe_claim("http://control.example.com/api/builds", "t", pb,
                                                                              window=5, poll=0.01), refused=True)

            follow = dict(repo="MeshWeaver.Plugins", sha=sha, channel="latest", probe=probe, stage="run")
            scenario([{"method": "GET", "match": f"probe={probe}", "answers": [
                [200, {"runState": "running", "runTerminal": False}], [200, {"runState": "succeeded", "runTerminal": True}]]}])
            check("probe-verdict is green only once the run ENDED succeeded",
                  lambda: probe_verdict(api, "t", window=5, poll=0.01, **follow), lambda o: o == {"run_state": "succeeded"})
            scenario([{"method": "GET", "match": f"probe={probe}", "answers": [[200, {"runState": "failed", "runTerminal": True}]]}])
            check("a probe that ENDED failed is a red test", lambda: probe_verdict(api, "t", window=5, poll=0.01, **follow),
                  refused=True, says="ENDED `failed`")
            scenario([{"method": "GET", "match": f"probe={probe}", "answers": [[200, {"runState": "succeeded", "runTerminal": False}]]}])
            check("NEGATIVE: `succeeded` before the run is terminal is not a verdict (a lost host can reset it)",
                  lambda: probe_verdict(api, "t", window=0.1, poll=0.02, **follow), refused=True, says="did not end")

            # The CLI as the workflow runs it: stdout carries ONLY key=value lines (it is appended to
            # GITHUB_OUTPUT, which refuses anything else), the refusal goes to stderr, exit 1.
            scenario([{"method": "POST", "match": "/probes", "answers": [submitted]},
                      {"method": "GET", "match": f"probe={probe}", "answers": [[200, {"runState": "queued"}]]}])
            env = {**os.environ, PROBE_TOKEN_ENV: "mw_secret"}
            cli = subprocess.run([sys.executable, os.path.abspath(__file__), "probe-claim", "--api", api, "--version", V,
                                  "--repo", "MeshWeaver.Plugins", "--sha", sha, "--window", "0.1", "--poll", "0.02"],
                                 capture_output=True, text=True, env=env)
            check("CLI: never claimed → exit 1, an ::error:: on stderr, nothing on stdout",
                  lambda: cli, lambda p: p.returncode == 1 and p.stdout == "" and "::error::no host claimed" in p.stderr)
            scenario([{"method": "POST", "match": "/probes", "answers": [submitted]},
                      {"method": "GET", "match": f"probe={probe}", "answers": [[200, {"runState": "running"}]]}])
            env = {**os.environ, PROBE_TOKEN_ENV: "mw_secret"}
            cli = subprocess.run([sys.executable, os.path.abspath(__file__), "probe-claim", "--api", api, "--version", V,
                                  "--repo", "MeshWeaver.Plugins", "--sha", sha, "--window", "5", "--poll", "0.01"],
                                 capture_output=True, text=True, env=env)
            check("CLI: claimed → exit 0 and only key=value lines on stdout",
                  lambda: cli, lambda p: p.returncode == 0 and all("=" in line and not line.startswith("::")
                                                                     for line in p.stdout.splitlines())
                  and f"probe={probe}" in p.stdout.splitlines())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    check("the shipped declaration is off", lambda: decide(load_declaration(Path(__file__).resolve().parents[1] / "deploy-test-switch.json")),
          lambda r: r[0] is False)
    if fails:
        print("deploy-test-switch self-test FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print(f"deploy-test-switch self-test: {total} cases passed (the four receiver shapes, every refusal, the hosts "
          "reader reads `complete` and never `ready`, a complete version scaled to zero is a target, the pinned probe "
          "is CLAIMED before the deploy is green and a never-claimed probe is red, the verdict is the probe's ENDED run, "
          "through a fake curl; the shipped declaration is off)")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("decide")
    s.add_argument("--declaration", type=Path, default=Path(".github/deploy-test-switch.json"))
    b = sub.add_parser("body")
    b.add_argument("kind", choices=["deploy", "switch", "rollback", "teardown"])
    b.add_argument("--deployment", required=True)
    b.add_argument("--version", required=True)
    for f in ("tester-digest", "portal-digest", "runner-digest", "core-tests-digest", "run"):
        b.add_argument(f"--{f}", default="")
    b.add_argument("--reason", default="candidate")
    h = sub.add_parser("hosts")
    h.add_argument("--hosts", type=Path, required=True)
    h.add_argument("--version", required=True)
    w = sub.add_parser("wait-complete")
    for p in (w, sub.add_parser("probe-claim"), sub.add_parser("probe-verdict")):
        p.add_argument("--api", required=True)
        p.add_argument("--window", type=float, required=True)
        p.add_argument("--poll", type=float, default=15.0)
        if p is not w:
            p.add_argument("--repo", required=True)
            p.add_argument("--sha", required=True)
            p.add_argument("--channel", default="latest")
        if p.prog.endswith("probe-verdict"):
            p.add_argument("--probe", required=True)
            p.add_argument("--stage", required=True)
        else:
            p.add_argument("--version", required=True)
        if p.prog.endswith("probe-claim"):
            p.add_argument("--stage", action="append", default=[])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "decide":
            on, why = decide(load_declaration(a.declaration))
            out = {"enabled": "true" if on else "false", "why": why}
        elif a.cmd == "body":
            print(json.dumps(body(a.kind, deployment=a.deployment, version=a.version, tester_digest=a.tester_digest,
                                  portal_digest=a.portal_digest, runner_digest=a.runner_digest,
                                  core_tests_digest=a.core_tests_digest, reason=a.reason, run=a.run),
                             separators=(",", ":")))
            return 0
        elif a.cmd == "hosts":
            out = read_hosts(json.loads(a.hosts.read_text()), _version(a.version))
        elif a.cmd == "wait-complete":
            out = wait_complete(a.api, a.version, a.window, a.poll)
        elif a.cmd == "probe-claim":
            out = probe_claim(a.api, os.environ.get(PROBE_TOKEN_ENV, ""),
                              probe_body(repo=a.repo, sha=a.sha, channel=a.channel, version=a.version, stages=a.stage),
                              a.window, a.poll)
        else:
            out = probe_verdict(a.api, os.environ.get(PROBE_TOKEN_ENV, ""), repo=a.repo, sha=a.sha, channel=a.channel,
                                probe=a.probe, stage=a.stage, window=a.window, poll=a.poll)
    except (Refusal, ValueError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
