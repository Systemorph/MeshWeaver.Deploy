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
               "runs":["Hosting/Builds/<repo>/<runId>", …]}       (1–32 distinct; NEVER with "run")
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

    plan    = the candidate's runs (platform DeployTestSwitchLatest §2 step 3): ONE pinned run PER
              SHARD of core's suite — the count is `CI_SHARD_TOTAL` READ from core's
              `dotnet-test.yml`, never written here — the doc-gate run, and each affected
              dependent's gate run. 1–32 runs (the switch receiver's bound); zero is refused.
    deploy  = request → wait until `/hosts` lists the version `complete: true` → submit EVERY
              planned run as a probe PINNED to the version (`POST /api/builds/probes` with
              `version`) → wait until a host CLAIMS one. Fewer submitted than planned is red; not
              complete, or nothing claimed, inside the deploy window is the deploy FAILURE: red,
              the candidate is torn down, and `latest` does not move (§7).
    test    = the COMBINED verdict: every planned run must be CLAIMED and then END `succeeded`.
              One red, missing, never-claimed or unfinished run holds the switch. The switch names
              every run under `runs`, and its receiver re-reads each one (Succeeded, every stage
              Succeeded, pinned to the version) and never trusts this job.

🚨 KNOWN LIMITATION — A CONTRACT GAP. `POST /probes` (Plugins §7a/§7b) takes `repo`, `sha`,
`channel`, `stages` and `version` and NO shard selector, and core declares no per-shard stage
(`.github/build-stages.json` does not exist yet, DeployTestSwitchLatest §8 P4). A shard can
therefore only be selected as a STAGE, and there is none to name. Until there is,
`.github/deploy-test-switch.json` declares `shardStage: null` and `docGateStage: null`: the plan
holds the dependents' gate runs only, and every run says, as a `::warning::`, that the
CI_SHARD_TOTAL shards and the doc gate were NOT run on the candidate. Declaring `shardStage`
(e.g. `test-shard-{shard}`, shards 0-based as in `dotnet-test.yml`) turns the per-shard runs on.

🚨 SWITCHED OFF UNTIL DECLARED. `.github/deploy-test-switch.json` `enabled: false` (the shipped
value): the workflow writes one summary line and sends nothing. It is turned on only once the
receivers (P3c) are deployed on the control instance.

    deploy-test-switch.py decide --declaration .github/deploy-test-switch.json
        → GITHUB_OUTPUT lines enabled=, why=
    deploy-test-switch.py body deploy --deployment control --version 3.1.N --tester-digest sha256:… \\
        --portal-digest sha256:… --runner-digest sha256:… [--core-tests-digest sha256:…] [--reason candidate]
    deploy-test-switch.py body switch --deployment control --version 3.1.N --runs-file runs.json --planned N
    deploy-test-switch.py body rollback --deployment control --version 3.1.N
    deploy-test-switch.py body teardown --deployment control --version 3.1.N
        → the event JSON, one line (the bytes that are signed are the bytes that are sent)
    deploy-test-switch.py hosts --hosts hosts.json --version 3.1.N
        → GITHUB_OUTPUT lines listed=, complete=, latest=, candidate=, previous=, deployment_name=
    deploy-test-switch.py wait-complete --api https://<control>/api/builds --version 3.1.N --window S
        → the hosts lines once the version is complete; exit 1 when the window passes first
    deploy-test-switch.py wait-alias --api … --version 3.1.N [--alias latest] --window S
        → the hosts lines once the alias names the version (after a switch or rollback: the 202 is
          the inbox ACCEPTING the event, not the action having run); exit 1 when the window passes first
    deploy-test-switch.py wait-gone --api … --version 3.1.N --window S
        → torn_down=true once unlisted, or torn_down=false pinned=N when retention still holds it at
          the window's end; exit 1 when it is still listed and nothing pins it
    deploy-test-switch.py plan --declaration … --dotnet-test <core>/.github/workflows/dotnet-test.yml \\
        --version 3.1.N [--core-sha <40 hex>] --dependent MeshWeaver.Plugins=<40 hex> … --out plan.json
        → planned=, shard_total=; every limitation as a ::warning:: on stderr
    deploy-test-switch.py probes-submit --api … --plan plan.json --out runs.json --window S  (token: $MW_PROBE_TOKEN)
        → planned=, submitted=, claimed= once a host CLAIMED one; exit 1 on a short submission or no claim
    deploy-test-switch.py probes-verdict --api … --runs runs.json --planned N --out verdict.json --window S
        → succeeded=; exit 0 only when EVERY planned run was claimed and ENDED succeeded

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
# The switch receiver's bound on `runs` (Plugins `Hosting/StandingBuildHosts` §7b): 1–32 distinct run paths.
MAX_RUNS = 32
# `CI_SHARD_TOTAL: <n>` in the top-level `env:` of core's dotnet-test.yml — two-space indent, a literal.
SHARD_TOTAL_LINE = re.compile(r"^  CI_SHARD_TOTAL:[ \t]*(\S+)[ \t]*(#.*)?$", re.MULTILINE)
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
    # The per-shard and doc-gate stages are DECLARED (null = the contract gap: nothing to select by).
    for key in ("shardStage", "docGateStage"):
        if key not in d:
            raise Refusal(f"{path}: `{key}` must be declared — a stage name, or null while core declares none")
        v = d[key]
        if v is None:
            continue
        probe = v.replace("{shard}", "0") if isinstance(v, str) else None
        if probe is None or not STAGE.fullmatch(probe) or (key == "shardStage") != ("{shard}" in v):
            raise Refusal(f"{path}: `{key}` {v!r} is not a stage name"
                          + (" carrying `{shard}`" if key == "shardStage" else ""))
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


def _runs(runs: list[str] | None, planned: int) -> list[str]:
    """The runs a switch names: 1–32 distinct run paths, and EXACTLY as many as were planned — a
    switch naming fewer runs than the plan would publish a set some of whose tests never ran."""
    runs = list(runs or [])
    if not runs:
        raise Refusal("a switch names every run that tested the candidate under `runs` — none was given")
    bad = [r for r in runs if not isinstance(r, str) or not RUN.fullmatch(r)]
    if bad:
        raise Refusal(f"run(s) {bad!r} are not Hosting/Builds/<repo>/<runId> nodes")
    if len(set(runs)) != len(runs):
        raise Refusal(f"the runs are not distinct: {runs!r}")
    if len(runs) > MAX_RUNS:
        raise Refusal(f"{len(runs)} runs exceed the switch receiver's bound of {MAX_RUNS}")
    if type(planned) is not int or planned < 1:
        raise Refusal(f"the planned run count {planned!r} is not a positive number — a switch is checked against its plan")
    if len(runs) != planned:
        raise Refusal(f"the switch names {len(runs)} run(s) but {planned} were planned — a missing run is red, never a smaller verdict")
    return runs


def body(kind: str, *, deployment: str, version: str, tester_digest: str = "", portal_digest: str = "",
         runner_digest: str = "", core_tests_digest: str = "", reason: str = "candidate", run: str = "",
         runs: list[str] | None = None, planned: int = 0) -> dict:
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
        # 🚨 NEVER `run` AND `runs`: the receiver refuses the pair, and a single `run` would let one
        # green run stand for the whole plan. Every run goes under `runs`, checked against the plan.
        if run:
            raise Refusal("a switch names its runs under `runs` only — never `run`, and never both")
        return {"event": "build-host-switch-requested", "deployment": dep, "version": ver, "alias": "latest",
                "runs": _runs(runs, planned)}
    if kind == "rollback":
        if run or runs:
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


def wait_alias(api: str, version: str, alias: str, window: float, poll: float) -> dict:
    """After a switch or rollback: polls GET /hosts until `alias` names `version`. The inbox's 202
    says only that the event was ACCEPTED; the governed InstanceAction lands later, so one read
    can still show the old alias. An unreadable answer is a Refusal at once, never a wait."""
    base, version = _api(api), _version(version)
    if alias not in ALIASES:
        raise Refusal(f"alias {alias!r} is not one of {ALIASES}")
    deadline = time.monotonic() + window
    while True:
        out = read_hosts(_json(*http("GET", f"{base}/hosts"), what=f"GET {base}/hosts"), version)
        if out[alias] == version:
            return out
        if time.monotonic() >= deadline:
            raise Refusal(f"alias {alias} still names {out[alias] or 'nothing'!r}, not {version}, {int(window)}s after the "
                          "event was accepted — the receiver refused it or has not applied it; latest is UNVERIFIED: "
                          "read GET /api/builds/hosts and the InstanceAction before retrying")
        time.sleep(poll)


def wait_gone(api: str, version: str, window: float, poll: float) -> dict:
    """After a teardown: polls GET /hosts until `version` is no longer listed. Still listed when the
    window passes is retention only if something PINS it (said by name), else a Refusal."""
    base, version = _api(api), _version(version)
    deadline = time.monotonic() + window
    while True:
        doc = _json(*http("GET", f"{base}/hosts"), what=f"GET {base}/hosts")
        out = read_hosts(doc, version)
        if out["listed"] == "false":
            return {**out, "torn_down": "true", "pinned": "0"}
        if time.monotonic() >= deadline:
            entry = next(v for v in doc["versions"] if v["version"] == version)
            pinned = entry.get("pinned")
            if type(pinned) is int and pinned > 0:
                return {**out, "torn_down": "false", "pinned": str(pinned)}
            raise Refusal(f"build-{version} is still listed {int(window)}s after the teardown was accepted and nothing "
                          "pins it — the teardown did not happen; read the BuildHostTeardown InstanceAction")
        time.sleep(poll)


def _log_url(base: str, repo: str, sha: str, stage: str, channel: str, probe: str) -> str:
    return f"{base}/{repo}/{sha}/stages/{stage}/log?after=-1&channel={channel}&probe={probe}"


def shard_total(text: str, where: str = "dotnet-test.yml") -> int:
    """`CI_SHARD_TOTAL` from core's dotnet-test.yml — READ, never written here. Anything but exactly
    one positive integer literal is a Refusal: a guessed count would plan the wrong number of runs."""
    found = SHARD_TOTAL_LINE.findall(text)
    if len(found) != 1:
        raise Refusal(f"{where} declares CI_SHARD_TOTAL {len(found)} times in its top-level env — expected once")
    raw = found[0][0].strip("'\"")
    if not raw.isdigit() or int(raw) < 1:
        raise Refusal(f"{where}: CI_SHARD_TOTAL is {found[0][0]!r}, not a positive integer literal")
    return int(raw)


def plan(declaration: dict, *, shards: int, version: str, core_sha: str, dependents: list[tuple[str, str]],
         core_repo: str = "MeshWeaver", channel: str = "latest", dependents_complete: bool = False) -> dict:
    """The candidate's runs (DeployTestSwitchLatest §2 step 3): one PINNED run per shard, the doc
    gate, each affected dependent's gate. Returns {"runs": [...], "limitations": [...]}."""
    _version(version)
    if type(shards) is not int or shards < 1:
        raise Refusal(f"the shard count {shards!r} is not a positive number")
    runs: list[dict] = []
    limitations: list[str] = []
    shard_stage, doc_stage = declaration.get("shardStage"), declaration.get("docGateStage")
    if (shard_stage or doc_stage) and not SHA.fullmatch(core_sha or ""):
        raise Refusal(f"core sha {core_sha!r} is not a full 40-hex commit — the shard and doc-gate runs test core at the armed commit")
    if shard_stage:
        for k in range(shards):  # 0-based, as dotnet-test.yml's `matrix.shard`
            runs.append({"role": f"shard {k}/{shards}", "repo": core_repo, "sha": core_sha,
                         "stages": [shard_stage.replace("{shard}", str(k))]})
    else:
        limitations.append(f"contract gap: POST /probes carries no shard selector and core declares no per-shard stage "
                           f"(shardStage: null) — the {shards} shards of core's suite (CI_SHARD_TOTAL) are NOT run on the candidate")
    if doc_stage:
        runs.append({"role": "doc gate", "repo": core_repo, "sha": core_sha, "stages": [doc_stage]})
    else:
        limitations.append("contract gap: core declares no doc-gate stage (docGateStage: null) — the doc gate is NOT run on the candidate")
    if not dependents_complete:
        limitations.append(f"the dependents planned ({', '.join(r for r, _ in dependents) or 'none'}) are not known to be "
                           "every AFFECTED dependent — no affected-dependents set is handed over yet, so the satellites' "
                           "gates are NOT run on the candidate")
    seen = set()
    for repo, sha in dependents:
        if repo in seen:
            raise Refusal(f"dependent {repo} is named twice")
        seen.add(repo)
        runs.append({"role": f"{repo} gate", "repo": repo, "sha": sha, "stages": []})
    if not runs:
        raise Refusal("the plan holds no run — a candidate nothing tested is never switched")
    if len(runs) > MAX_RUNS:
        raise Refusal(f"the plan holds {len(runs)} runs, beyond the switch receiver's bound of {MAX_RUNS}")
    for r in runs:  # every run's probe body validates now, before anything is sent
        probe_body(repo=r["repo"], sha=r["sha"], channel=channel, version=version, stages=r["stages"])
        r["channel"], r["version"] = channel, version
    return {"runs": runs, "limitations": limitations}


def _submit(base: str, token: str, run: dict) -> dict:
    """POST one pinned probe → the run as followed: role, repo, sha, channel, stage, probe, path."""
    pb = probe_body(repo=run["repo"], sha=run["sha"], channel=run["channel"], version=run["version"], stages=run["stages"])
    status, raw = http("POST", f"{base}/probes", token=token, data=json.dumps(pb, separators=(",", ":")))
    if status != 202:
        raise Refusal(f"POST {base}/probes ({run['role']}) answered HTTP {status or 'nothing (curl failed)'}: {raw[:300]}")
    try:
        ans = json.loads(raw)
    except ValueError as e:
        raise Refusal(f"POST /probes ({run['role']}) answered something that is not JSON") from e
    ans = ans if isinstance(ans, dict) else {}
    probe, path = str(ans.get("probe") or ""), str(ans.get("path") or "")
    if not PROBE_ID.fullmatch(probe):
        raise Refusal(f"POST /probes ({run['role']}) answered no probe id (got {probe!r})")
    if not RUN.fullmatch(path):
        raise Refusal(f"POST /probes ({run['role']}) answered no run path (got {path!r}) — the switch could not name it")
    stages = ans.get("stages") or pb.get("stages") or []
    # The RUN's state rides on every stage's page; with no stage named, any stage-shaped name reads it.
    stage = str(stages[0]) if stages and STAGE.fullmatch(str(stages[0])) else "run"
    say(f"probe {probe} submitted ({run['role']}): {pb['repo']}@{pb['sha'][:12]} pinned to {pb['version']}")
    return {"role": run["role"], "repo": pb["repo"], "sha": pb["sha"], "channel": pb["channel"], "stage": stage,
            "probe": probe, "path": path}


def _page(base: str, token: str, r: dict) -> dict:
    page = _json(*http("GET", _log_url(base, r["repo"], r["sha"], r["stage"], r["channel"], r["probe"]), token=token),
                 what=f"the probe {r['probe']}'s log ({r['role']})")
    if not isinstance(page, dict):
        raise Refusal(f"the probe {r['probe']}'s log page ({r['role']}) is not an object")
    return page


def probes_submit(api: str, token: str, plan_: dict, window: float, poll: float) -> tuple[list[dict], str]:
    """Submits EVERY planned run as a pinned probe, then waits until a host CLAIMS one: a host claims
    only after staging the version's set, so the first claim proves the deployment serves. The
    rest are claimed as replicas free up (MaxReplicas bounds how many run at once), which the
    verdict follows. Fewer submitted than planned, or nothing claimed in the window, is a Refusal."""
    base = _api(api)
    if not token:
        raise Refusal(f"{PROBE_TOKEN_ENV} is empty — a probe is submitted with a global administrator's own mw_ token")
    planned = plan_.get("runs") if isinstance(plan_, dict) else None
    if not isinstance(planned, list) or not planned:
        raise Refusal("the plan holds no runs")
    runs = [_submit(base, token, r) for r in planned]
    paths = [r["path"] for r in runs]
    if len(runs) < len(planned) or len(set(paths)) != len(paths):
        raise Refusal(f"{len(set(paths))} distinct run(s) submitted for {len(planned)} planned — a missing run is red")
    deadline = time.monotonic() + window
    while True:
        states = {}
        for r in runs:
            state, claimed = probe_state(_page(base, token, r))
            if claimed:
                return runs, r["probe"]
            states[r["role"]] = state
        if time.monotonic() >= deadline:
            raise Refusal(f"no host claimed any of the {len(runs)} probes (pinned to {planned[0]['version']}) within "
                          f"{int(window)}s ({states}) — build-{planned[0]['version']} never served them; the deploy FAILED")
        time.sleep(poll)


def probes_verdict(api: str, token: str, runs: list[dict], planned: int, window: float, poll: float) -> list[str]:
    """The COMBINED verdict: every planned run CLAIMED and then ENDED `succeeded`. One run red,
    missing, never claimed or unfinished is a Refusal naming every run that is not green."""
    base = _api(api)
    if not isinstance(runs, list) or type(planned) is not int or planned < 1:
        raise Refusal("the verdict needs the submitted runs and a positive planned count")
    for r in runs:
        if not isinstance(r, dict) or not PROBE_ID.fullmatch(str(r.get("probe") or "")) or not RUN.fullmatch(str(r.get("path") or "")):
            raise Refusal(f"a submitted run is malformed: {r!r}")
    if len(runs) != planned or len({r["path"] for r in runs}) != planned:
        raise Refusal(f"{len({r['path'] for r in runs})} distinct run(s) to judge for {planned} planned — "
                      "a missing run is red, never a smaller verdict")
    ended: dict[str, str] = {}
    last: dict[str, str] = {}
    deadline = time.monotonic() + window
    while True:
        for r in runs:
            if r["path"] in ended:
                continue
            page = _page(base, token, r)
            try:
                state, claimed = probe_state(page)
            except Refusal as e:  # ended before any host claimed it: red, and the others are still judged
                ended[r["path"]] = f"unclaimed ({e})"
                continue
            last[r["path"]] = state if claimed else f"{state}, never claimed"
            if page.get("runTerminal") is True:
                ended[r["path"]] = state
        if len(ended) == len(runs) or time.monotonic() >= deadline:
            break
        time.sleep(poll)
    red = [f"{r['role']} ({r['probe']}): " + (f"ENDED `{ended[r['path']]}`" if r["path"] in ended
                                               else f"did not end within {int(window)}s ({last.get(r['path'], '?')})")
           for r in runs if ended.get(r["path"]) != "succeeded"]
    if red:
        raise Refusal(f"{len(red)} of {planned} candidate run(s) are not green — latest is NOT switched: " + "; ".join(red))
    return [r["path"] for r in runs]


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
    R3 = ["Hosting/Builds/MeshWeaver/r-1", "Hosting/Builds/MeshWeaver/r-2", "Hosting/Builds/MeshWeaver.Plugins/r-3"]
    check("switch names EVERY run under `runs`, and no `run`",
          lambda: body("switch", deployment="control", version="3.1.1", runs=R3, planned=3),
          lambda b: list(b) == ["event", "deployment", "version", "alias", "runs"] and b["alias"] == "latest"
          and b["runs"] == R3 and "run" not in b and "rollback" not in b)
    check("NEGATIVE: `run` and `runs` together are NEVER sent",
          lambda: body("switch", deployment="control", version="3.1.1", run=R3[0], runs=R3, planned=3),
          refused=True, says="never both")
    check("a single `run` is refused too — one green run never stands for the plan",
          lambda: body("switch", deployment="control", version="3.1.1", run=R3[0]), refused=True, says="never both")
    check("NEGATIVE: a switch naming FEWER runs than planned (a missing shard) is refused",
          lambda: body("switch", deployment="control", version="3.1.1", runs=R3[:2], planned=3), refused=True,
          says="2 run(s) but 3 were planned")
    check("a switch with no planned count is refused",
          lambda: body("switch", deployment="control", version="3.1.1", runs=R3), refused=True, says="planned run count")
    check("a switch naming a run twice is refused",
          lambda: body("switch", deployment="control", version="3.1.1", runs=[R3[0], R3[0]], planned=2),
          refused=True, says="not distinct")
    check("a switch beyond the receiver's 32 runs is refused",
          lambda: body("switch", deployment="control", version="3.1.1",
                       runs=[f"Hosting/Builds/MeshWeaver/r-{i}" for i in range(33)], planned=33), refused=True, says="bound of 32")
    check("32 runs are accepted", lambda: body("switch", deployment="control", version="3.1.1",
                                               runs=[f"Hosting/Builds/MeshWeaver/r-{i}" for i in range(32)], planned=32),
          lambda b: len(b["runs"]) == 32)
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
    check("refuses a switch with no run", lambda: body("switch", deployment="control", version="3.1.1", planned=1), refused=True)
    check("refuses a switch naming a non-run", lambda: body("switch", deployment="control", version="3.1.1", runs=["Feedback/x"],
                                                            planned=1), refused=True)
    check("refuses a rollback with a run", lambda: body("rollback", deployment="control", version="3.1.1", run="Hosting/Builds/a/b"), refused=True)
    check("refuses a rollback with runs", lambda: body("rollback", deployment="control", version="3.1.1", runs=["Hosting/Builds/a/b"]),
          refused=True)

    # ── the plan: CI_SHARD_TOTAL is READ, one run per shard, the doc gate, the dependents ──
    dotnet_test = "env:\n  DOTNET_NOLOGO: true\n  CI_SHARD_TOTAL: 8\n  OTHER: x\njobs:\n  t:\n    env:\n      CI_SHARD_TOTAL: 99\n"
    check("CI_SHARD_TOTAL is read from the top-level env (a job-level one is not it)", lambda: shard_total(dotnet_test),
          lambda n: n == 8)
    check("… and it follows the file: 5 there plans 5", lambda: shard_total(dotnet_test.replace("TOTAL: 8", "TOTAL: 5")),
          lambda n: n == 5)
    for name, text in [("absent", "env:\n  X: 1\n"), ("an expression", "env:\n  CI_SHARD_TOTAL: ${{ vars.N }}\n"),
                       ("zero", "env:\n  CI_SHARD_TOTAL: 0\n"), ("declared twice", "env:\n  CI_SHARD_TOTAL: 8\n  CI_SHARD_TOTAL: 9\n")]:
        check(f"CI_SHARD_TOTAL {name} is refused, never guessed", lambda text=text: shard_total(text), refused=True)
    core_sha, plug_sha = "c" * 40, "b" * 40
    on = {"enabled": True, "shardStage": "test-shard-{shard}", "docGateStage": "doc-gate"}
    off = {"enabled": True, "shardStage": None, "docGateStage": None}
    check("per-shard: ONE pinned run per shard (0-based), then the doc gate, then each dependent",
          lambda: plan(on, shards=8, version="3.1.1", core_sha=core_sha, dependents=[("MeshWeaver.Plugins", plug_sha)],
                       dependents_complete=True),
          lambda p: len(p["runs"]) == 10 and [r["stages"] for r in p["runs"][:8]] == [[f"test-shard-{k}"] for k in range(8)]
          and p["runs"][8]["stages"] == ["doc-gate"] and p["runs"][9]["repo"] == "MeshWeaver.Plugins"
          and all(r["version"] == "3.1.1" and r["sha"] == core_sha for r in p["runs"][:9]) and p["limitations"] == [])
    check("KNOWN LIMITATION: with no shard selector the plan is the dependents' runs, and says what it did not run",
          lambda: plan(off, shards=8, version="3.1.1", core_sha="", dependents=[("MeshWeaver.Plugins", plug_sha)]),
          lambda p: len(p["runs"]) == 1 and len(p["limitations"]) == 3 and "the 8 shards" in p["limitations"][0]
          and "doc gate" in p["limitations"][1] and "satellites' gates are NOT run" in p["limitations"][2])
    check("an affected-dependents set not known to be complete is named as a limitation, never silent",
          lambda: plan(on, shards=2, version="3.1.1", core_sha=core_sha, dependents=[("MeshWeaver.Plugins", plug_sha)]),
          lambda p: p["limitations"] == [p["limitations"][0]] and "MeshWeaver.Plugins" in p["limitations"][0])
    check("a plan with no run at all is refused", lambda: plan(off, shards=8, version="3.1.1", core_sha="", dependents=[]),
          refused=True, says="holds no run")
    check("shards declared but no core sha is refused", lambda: plan(on, shards=8, version="3.1.1", core_sha="",
                                                                    dependents=[]), refused=True, says="core sha")
    check("a plan beyond the receiver's 32 runs is refused", lambda: plan(on, shards=40, version="3.1.1", core_sha=core_sha,
                                                                         dependents=[]), refused=True, says="bound of 32")
    check("a dependent named twice is refused", lambda: plan(off, shards=8, version="3.1.1", core_sha="",
                                                            dependents=[("R", plug_sha), ("R", plug_sha)]), refused=True)
    check("a dependent with a short sha is refused before anything is sent",
          lambda: plan(off, shards=8, version="3.1.1", core_sha="", dependents=[("R", "abc")]), refused=True, says="40-hex")
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

            # ── the read-back after an ACCEPTED event: polled, because the 202 is not the action ──
            def aliased(latest, *rows):
                return {"aliases": {"latest": latest, "candidate": None, "previous": "3.1.10400"}, "versions": list(rows)}
            log = scenario([{"method": "GET", "match": "/hosts", "answers": [
                [200, aliased("3.1.10400", row(V, True))], [200, aliased("3.1.10400", row(V, True))],
                [200, aliased(V, row(V, True))]]}])
            check("wait-alias waits through the OLD alias until latest names the candidate",
                  lambda: wait_alias(api, V, "latest", window=5, poll=0.01), lambda o: o["latest"] == V and len(calls(log)) == 3)
            scenario([{"method": "GET", "match": "/hosts", "answers": [[200, aliased("3.1.10400", row(V, True))]]}])
            check("NEGATIVE: an alias that never moves is red, and says latest is UNVERIFIED",
                  lambda: wait_alias(api, V, "latest", window=0.1, poll=0.02), refused=True, says="UNVERIFIED")
            scenario([{"method": "GET", "match": "/hosts", "answers": [[200, aliased("3.1.10400")], [503, "{}"]]}])
            check("an unreadable /hosts during the read-back is red at once, never a wait",
                  lambda: wait_alias(api, V, "latest", window=5, poll=0.01), refused=True, says="HTTP 503")
            log = scenario([{"method": "GET", "match": "/hosts", "answers": [
                [200, aliased("3.1.10400", row(V, False))], [200, aliased("3.1.10400")]]}])
            check("wait-gone waits until the version is no longer listed",
                  lambda: wait_gone(api, V, window=5, poll=0.01), lambda o: o["torn_down"] == "true" and len(calls(log)) == 2)
            scenario([{"method": "GET", "match": "/hosts", "answers": [[200, aliased("3.1.10400", row(V, True, pinned=2))]]}])
            check("a version still listed when the window passes is KEPT only because something pins it, said by count",
                  lambda: wait_gone(api, V, window=0.1, poll=0.02), lambda o: o["torn_down"] == "false" and o["pinned"] == "2")
            scenario([{"method": "GET", "match": "/hosts", "answers": [[200, aliased("3.1.10400", row(V, True))]]}])
            check("NEGATIVE: still listed and nothing pins it — the teardown did not happen, red",
                  lambda: wait_gone(api, V, window=0.1, poll=0.02), refused=True, says="nothing pins it")

            # A plan of THREE runs (two shards, one dependent) — the per-shard shape, through a fake curl.
            p3 = plan({"enabled": True, "shardStage": "test-shard-{shard}", "docGateStage": None}, shards=2, version=V,
                      core_sha=core_sha, dependents=[("MeshWeaver.Plugins", sha)])
            ids = ["00000000000" + str(i) for i in range(3)]

            def accepted(i, r):
                return [202, {"path": f"Hosting/Builds/{r['repo']}/{r['sha']}-probe-{ids[i]}", "probe": ids[i],
                              "channel": "latest", "stages": r["stages"]}]
            submits = [accepted(i, r) for i, r in enumerate(p3["runs"])]

            def pages(*per_probe):
                return [{"method": "GET", "match": f"probe={ids[i]}", "answers": a} for i, a in enumerate(per_probe)]
            q, run_, ok, bad = ([200, {"runState": "queued"}], [200, {"runState": "running", "runTerminal": False}],
                                [200, {"runState": "succeeded", "runTerminal": True}], [200, {"runState": "failed", "runTerminal": True}])

            log = scenario([{"method": "POST", "match": "/probes", "answers": submits}, *pages([q, run_], [q], [q])])
            check("probes-submit sends ONE pinned probe PER planned run and returns once a host CLAIMED one",
                  lambda: probes_submit(api, "mw_secret", p3, window=5, poll=0.01),
                  lambda o: [r["probe"] for r in o[0]] == ids and o[1] == ids[0]
                  and [r["stage"] for r in o[0]] == ["test-shard-0", "test-shard-1", "run"])
            sent = calls(log)
            posts = [json.loads(c["data"]) for c in sent if c["method"] == "POST"]
            check("each probe POST is pinned to the version and selects its own shard",
                  lambda: posts, lambda b: len(b) == 3 and all(x["version"] == V for x in b)
                  and [x.get("stages") for x in b] == [["test-shard-0"], ["test-shard-1"], None])
            check("the token rides only in a header FILE, never on argv",
                  lambda: sent, lambda c: all("Authorization: Bearer mw_secret" in x["headers"] for x in c)
                  and not any("mw_secret" in a for x in c for a in x["argv"]))
            check("a claim is read on the probe's log route, as its submitter",
                  lambda: [c for c in sent if c["method"] == "GET"][0],
                  lambda c: f"/MeshWeaver/{core_sha}/stages/test-shard-0/log?" in c["url"] and "channel=latest" in c["url"])

            scenario([{"method": "POST", "match": "/probes", "answers": submits}, *pages([q], [q], [q])])
            check("NEGATIVE CONTROL: probes NO host ever claims are red — the deploy FAILED",
                  lambda: probes_submit(api, "mw_secret", p3, window=0.1, poll=0.02), refused=True, says="no host claimed any")
            scenario([{"method": "POST", "match": "/probes", "answers": [submits[0], submits[1], [503, {"error": "busy"}]]}])
            check("NEGATIVE: a planned run whose probe was not accepted (a missing shard) is red",
                  lambda: probes_submit(api, "t", p3, window=5, poll=0.01), refused=True, says="HTTP 503")
            scenario([{"method": "POST", "match": "/probes", "answers": [submits[0], submits[0], submits[0]]}])
            check("NEGATIVE: three submissions answering ONE run are fewer runs than planned — red",
                  lambda: probes_submit(api, "t", p3, window=5, poll=0.01), refused=True, says="1 distinct run(s) submitted for 3")
            scenario([{"method": "POST", "match": "/probes", "answers": submits}, *pages([[200, {"runState": "unobserved"}]])])
            check("a probe that ended before any claim is red", lambda: probes_submit(api, "t", p3, window=5, poll=0.01),
                  refused=True, says="before any host claimed")
            scenario([{"method": "POST", "match": "/probes", "answers": [[403, {"error": "only a global administrator"}]]}])
            check("a refused probe submission is red, naming the answer", lambda: probes_submit(api, "t", p3, window=5, poll=0.01),
                  refused=True, says="HTTP 403")
            scenario([{"method": "POST", "match": "/probes", "answers": [[202, {"probe": "nope"}]]}])
            check("a submission with no probe id is red", lambda: probes_submit(api, "t", p3, window=5, poll=0.01),
                  refused=True, says="no probe id")
            scenario([{"method": "POST", "match": "/probes", "answers": [[202, {"probe": ids[0], "path": "x"}]]}])
            check("a submission with no run path is red — the switch could not name it",
                  lambda: probes_submit(api, "t", p3, window=5, poll=0.01), refused=True, says="no run path")
            log = scenario([])
            check("an empty token is red before anything is sent",
                  lambda: probes_submit(api, "", p3, window=5, poll=0.01), refused=True, says=PROBE_TOKEN_ENV)
            check("… and nothing was sent", lambda: calls(log), lambda c: c == [])
            check("a build API that is not https is refused",
                  lambda: probes_submit("http://control.example.com/api/builds", "t", p3, window=5, poll=0.01), refused=True)

            # ── the COMBINED verdict ──
            subs = [{"role": r["role"], "repo": r["repo"], "sha": r["sha"], "channel": "latest",
                     "stage": (r["stages"] or ["run"])[0], "probe": ids[i], "path": submits[i][1]["path"]}
                    for i, r in enumerate(p3["runs"])]
            scenario(pages([run_, ok], [q, run_, run_, ok], [ok]))
            check("the verdict is green only once EVERY run was claimed and ENDED succeeded (claimed as replicas free up)",
                  lambda: probes_verdict(api, "t", subs, 3, window=5, poll=0.01), lambda o: o == [s["path"] for s in subs])
            log = scenario(pages([ok], [run_, bad], [ok]))
            check("NEGATIVE CONTROL: ONE shard fails → red, and the red names it — no switch",
                  lambda: probes_verdict(api, "t", subs, 3, window=5, poll=0.01), refused=True,
                  says="1 of 3 candidate run(s) are not green — latest is NOT switched: shard 1/2")
            check("… and the other runs were still judged to their end", lambda: calls(log),
                  lambda c: sum(1 for x in c if f"probe={ids[1]}" in x["url"]) == 2)
            scenario(pages([ok], [q], [ok]))
            check("NEGATIVE: a run NO host ever claimed is red, and says so",
                  lambda: probes_verdict(api, "t", subs, 3, window=0.1, poll=0.02), refused=True, says="queued, never claimed")
            scenario(pages([ok], [[200, {"runState": "succeeded", "runTerminal": False}]], [ok]))
            check("NEGATIVE: `succeeded` before the run is terminal is not a verdict (a lost host can reset it)",
                  lambda: probes_verdict(api, "t", subs, 3, window=0.1, poll=0.02), refused=True, says="did not end")
            scenario(pages([ok], [[200, {"runState": "cancelled"}]], [ok]))
            check("a run that ended unclaimed is red", lambda: probes_verdict(api, "t", subs, 3, window=5, poll=0.01),
                  refused=True, says="unclaimed")
            log = scenario(pages([ok], [ok], [ok]))
            check("NEGATIVE CONTROL: a MISSING shard (2 runs for 3 planned) is red before anything is read",
                  lambda: probes_verdict(api, "t", subs[:2], 3, window=5, poll=0.01), refused=True, says="for 3 planned")
            check("… and nothing was read", lambda: calls(log), lambda c: c == [])
            check("a run listed twice does not stand in for a missing one",
                  lambda: probes_verdict(api, "t", [subs[0], subs[0], subs[1]], 3, window=5, poll=0.01), refused=True,
                  says="2 distinct run(s) to judge for 3")

            # The CLI as the workflow runs it: stdout carries ONLY key=value lines (it is appended to
            # GITHUB_OUTPUT, which refuses anything else), the refusal goes to stderr, exit 1.
            files = os.path.join(tmp, "cli")
            os.mkdir(files)
            Path(files, "plan.json").write_text(json.dumps(p3))
            def cli(*args):  # the environment is read per call: each scenario names its own plan
                return subprocess.run([sys.executable, os.path.abspath(__file__), *args], capture_output=True, text=True,
                                      env={**os.environ, PROBE_TOKEN_ENV: "mw_secret"})
            scenario([{"method": "POST", "match": "/probes", "answers": submits}, *pages([q], [q], [q])])
            p = cli("probes-submit", "--api", api, "--plan", os.path.join(files, "plan.json"), "--out",
                    os.path.join(files, "runs.json"), "--window", "0.1", "--poll", "0.02")
            check("CLI: never claimed → exit 1, an ::error:: on stderr, nothing on stdout",
                  lambda: p, lambda p: p.returncode == 1 and p.stdout == "" and "::error::no host claimed" in p.stderr)
            scenario([{"method": "POST", "match": "/probes", "answers": submits}, *pages([run_])])
            p = cli("probes-submit", "--api", api, "--plan", os.path.join(files, "plan.json"), "--out",
                    os.path.join(files, "runs.json"), "--window", "5", "--poll", "0.01")
            check("CLI: claimed → exit 0, only key=value lines, planned = submitted = 3",
                  lambda: p, lambda p: p.returncode == 0 and all("=" in line and not line.startswith("::")
                                                                 for line in p.stdout.splitlines())
                  and {"planned=3", "submitted=3", f"claimed={ids[0]}"} <= set(p.stdout.splitlines()))
            scenario(pages([ok], [ok], [ok]))
            p = cli("probes-verdict", "--api", api, "--runs", os.path.join(files, "runs.json"), "--planned", "3",
                    "--out", os.path.join(files, "verdict.json"), "--window", "5", "--poll", "0.01")
            check("CLI: every run green → exit 0, succeeded=3, the verdict file lists the three runs",
                  lambda: p, lambda p: p.returncode == 0 and "succeeded=3" in p.stdout.splitlines()
                  and json.loads(Path(files, "verdict.json").read_text()) == [s["path"] for s in subs])
            p = cli("body", "switch", "--deployment", "control", "--version", V, "--runs-file",
                    os.path.join(files, "verdict.json"), "--planned", "3")
            check("CLI: the switch body carries the verdict's three runs under `runs`",
                  lambda: p, lambda p: p.returncode == 0 and json.loads(p.stdout)["runs"] == [s["path"] for s in subs]
                  and "run" not in json.loads(p.stdout))
            p = cli("body", "switch", "--deployment", "control", "--version", V, "--runs-file",
                    os.path.join(files, "verdict.json"), "--planned", "4")
            check("CLI: a switch body short of the plan → exit 1, nothing on stdout",
                  lambda: p, lambda p: p.returncode == 1 and p.stdout == "" and "4 were planned" in p.stderr)
            p = cli("body", "switch", "--deployment", "control", "--version", V, "--runs-file",
                    os.path.join(files, "verdict.json"), "--planned", "3", "--run", subs[0]["path"])
            check("CLI: `--run` beside `--runs-file` → exit 1, never sent",
                  lambda: p, lambda p: p.returncode == 1 and p.stdout == "" and "never both" in p.stderr)
            Path(files, "core").mkdir()
            Path(files, "core", "dotnet-test.yml").write_text(dotnet_test)
            Path(files, "decl.json").write_text(json.dumps(off))
            p = cli("plan", "--declaration", os.path.join(files, "decl.json"), "--dotnet-test",
                    os.path.join(files, "core", "dotnet-test.yml"), "--version", V, "--dependent", f"MeshWeaver.Plugins={sha}",
                    "--out", os.path.join(files, "p.json"))
            check("CLI plan (the shipped gap): planned=1, shard_total=8 READ, the limitation a ::warning::",
                  lambda: p, lambda p: p.returncode == 0 and {"planned=1", "shard_total=8"} <= set(p.stdout.splitlines())
                  and "::warning::contract gap: POST /probes carries no shard selector" in p.stderr)
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
    print(f"deploy-test-switch self-test: {total} cases passed (the four receiver shapes, every refusal; the switch "
          "names EVERY run under `runs` and never `run`; CI_SHARD_TOTAL is read, one pinned probe per shard, a missing "
          "shard and one failed shard are red; the hosts reader reads `complete` and never `ready`; a never-claimed probe "
          "is red; the verdict is every run's ENDED state, through a fake curl; the shipped declaration is off)")
    return 0


def _dependent(text: str) -> tuple[str, str]:
    repo, sep, sha = (text or "").partition("=")
    if not sep:
        raise Refusal(f"--dependent {text!r} is not <repo>=<sha>")
    return repo, sha


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=1) + "\n")


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
    b.add_argument("--runs-file", type=Path, help="switch: the verdict's JSON list of run paths")
    b.add_argument("--planned", type=int, default=0, help="switch: how many runs the plan held")
    b.add_argument("--reason", default="candidate")
    h = sub.add_parser("hosts")
    h.add_argument("--hosts", type=Path, required=True)
    h.add_argument("--version", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--declaration", type=Path, required=True)
    pl.add_argument("--dotnet-test", type=Path, required=True, help="core's .github/workflows/dotnet-test.yml (CI_SHARD_TOTAL)")
    pl.add_argument("--version", required=True)
    pl.add_argument("--core-sha", default="")
    pl.add_argument("--dependent", action="append", default=[], help="<repo>=<40 hex>, one per affected dependent")
    pl.add_argument("--out", type=Path, required=True)
    pl.add_argument("--dependents-complete", action="store_true",
                    help="the --dependent list IS every affected dependent (no satellite limitation is reported)")
    w = sub.add_parser("wait-complete")
    w.add_argument("--version", required=True)
    wa = sub.add_parser("wait-alias")
    wa.add_argument("--version", required=True)
    wa.add_argument("--alias", default="latest")
    wg = sub.add_parser("wait-gone")
    wg.add_argument("--version", required=True)
    ps = sub.add_parser("probes-submit")
    ps.add_argument("--plan", type=Path, required=True)
    pv = sub.add_parser("probes-verdict")
    pv.add_argument("--runs", type=Path, required=True)
    pv.add_argument("--planned", type=int, required=True)
    for p in (w, wa, wg, ps, pv):
        p.add_argument("--api", required=True)
        p.add_argument("--window", type=float, required=True)
        p.add_argument("--poll", type=float, default=15.0)
    for p in (ps, pv):
        p.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "decide":
            on, why = decide(load_declaration(a.declaration))
            out = {"enabled": "true" if on else "false", "why": why}
        elif a.cmd == "body":
            runs = json.loads(a.runs_file.read_text()) if a.runs_file else None
            if runs is not None and not isinstance(runs, list):
                raise Refusal(f"{a.runs_file} is not a JSON list of run paths")
            print(json.dumps(body(a.kind, deployment=a.deployment, version=a.version, tester_digest=a.tester_digest,
                                  portal_digest=a.portal_digest, runner_digest=a.runner_digest,
                                  core_tests_digest=a.core_tests_digest, reason=a.reason, run=a.run,
                                  runs=runs, planned=a.planned),
                             separators=(",", ":")))
            return 0
        elif a.cmd == "hosts":
            out = read_hosts(json.loads(a.hosts.read_text()), _version(a.version))
        elif a.cmd == "plan":
            shards = shard_total(a.dotnet_test.read_text(), str(a.dotnet_test))
            p = plan(load_declaration(a.declaration), shards=shards, version=a.version, core_sha=a.core_sha,
                     dependents=[_dependent(d) for d in a.dependent], dependents_complete=a.dependents_complete)
            for lim in p["limitations"]:
                print(f"::warning::{lim}", file=sys.stderr)
            _write_json(a.out, p)
            out = {"planned": str(len(p["runs"])), "shard_total": str(shards)}
        elif a.cmd == "wait-complete":
            out = wait_complete(a.api, a.version, a.window, a.poll)
        elif a.cmd == "wait-alias":
            out = wait_alias(a.api, a.version, a.alias, a.window, a.poll)
        elif a.cmd == "wait-gone":
            out = wait_gone(a.api, a.version, a.window, a.poll)
        elif a.cmd == "probes-submit":
            p = json.loads(a.plan.read_text())
            runs, first = probes_submit(a.api, os.environ.get(PROBE_TOKEN_ENV, ""), p, a.window, a.poll)
            _write_json(a.out, runs)
            out = {"planned": str(len(p["runs"])), "submitted": str(len(runs)), "claimed": first}
        else:
            paths = probes_verdict(a.api, os.environ.get(PROBE_TOKEN_ENV, ""), json.loads(a.runs.read_text()), a.planned,
                                   a.window, a.poll)
            _write_json(a.out, paths)
            out = {"succeeded": str(len(paths))}
    except (Refusal, ValueError, OSError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
