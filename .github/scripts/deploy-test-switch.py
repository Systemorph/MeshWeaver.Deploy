#!/usr/bin/env python3
"""deploy-test-switch.py — the events and the read-back of DEPLOY → TEST THE DEPLOY → SWITCH LATEST.

The target shape of the standing build hosts (platform Doc/Architecture/DeployTestSwitchLatest,
policy `deploy-test-switch-latest`): each sealed set is deployed as its OWN versioned build-server
deployment `build-<version>`, the tests run ON that deployment, and only a green deployment becomes
`latest` — the switch that makes a set available to modules. The workflow is
`.github/workflows/build-host-deploy-test-switch.yml`; this script is everything in it that can be
decided without a network: the declaration, the four signed event bodies, and the reading of the
build API's `GET /api/builds/hosts`.

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
"deploymentName","maxReplicas","aliases":[…],"pinned":N, …}]}`. A version is LISTED once its `v/`
entry is written and READY once its Deployment is; the step that waits asks THIS reader, which
says which of the two it saw, and an answer it cannot read is RED, never "not yet".

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
        → GITHUB_OUTPUT lines listed=, ready=, latest=, candidate=, previous=, deployment_name=
    deploy-test-switch.py --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
RUN = re.compile(r"^Hosting/Builds/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEPLOYMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ALIASES = ("latest", "candidate", "previous")


class Refusal(Exception):
    pass


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
    # READY is the Deployment's own readiness. An entry that does not say is NOT ready: a wait that
    # reads "listed" as "ready" would test a deployment that has not staged its plugins layer.
    out["ready"] = "true" if entry is not None and entry.get("ready") is True else "false"
    return out


def self_test() -> int:
    fails: list[str] = []
    d = lambda c: "sha256:" + c * 64  # noqa: E731

    def check(name, fn, want=None, refused=False):
        try:
            got = fn()
        except Refusal as e:
            if not refused:
                fails.append(f"{name}: refused — {e}")
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

    hosts = {"aliases": {"latest": "3.1.10400", "candidate": "3.1.10412", "previous": "3.1.10390"},
             "versions": [{"version": "3.1.10400", "deploymentName": "build-3-1-10400", "maxReplicas": 2, "aliases": ["latest"], "pinned": 0, "ready": True},
                          {"version": "3.1.10412", "deploymentName": "build-3-1-10412", "maxReplicas": 2, "aliases": ["candidate"], "pinned": 0}]}
    check("a listed, unready candidate", lambda: read_hosts(hosts, "3.1.10412"),
          lambda o: o["listed"] == "true" and o["ready"] == "false" and o["candidate"] == "3.1.10412")
    check("a ready latest", lambda: read_hosts(hosts, "3.1.10400"),
          lambda o: o["ready"] == "true" and o["latest"] == "3.1.10400" and o["deployment_name"] == "build-3-1-10400")
    check("an absent version is not listed", lambda: read_hosts(hosts, "3.1.1"), lambda o: o["listed"] == "false" and o["ready"] == "false")
    check("`ready: \"true\"` (a string) is not ready",
          lambda: read_hosts({"aliases": {}, "versions": [{"version": "3.1.1", "deploymentName": "b", "ready": "true"}]}, "3.1.1"),
          lambda o: o["ready"] == "false")
    for name, doc in [("a list", []), ("no versions", {"aliases": {}}), ("a malformed entry", {"aliases": {}, "versions": [{"v": 1}]}),
                      ("a twice-listed version", {"aliases": {}, "versions": [{"version": "3.1.1", "deploymentName": "a"}] * 2}),
                      ("an entry with no deployment name", {"aliases": {}, "versions": [{"version": "3.1.1"}]}),
                      ("an alias that is not a version", {"aliases": {"latest": "main"}, "versions": []})]:
        check(f"the reader refuses {name}", lambda doc=doc: read_hosts(doc, "3.1.1"), refused=True)
    check("the shipped declaration is off", lambda: decide(load_declaration(Path(__file__).resolve().parents[1] / "deploy-test-switch.json")),
          lambda r: r[0] is False)
    if fails:
        print("deploy-test-switch self-test FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("deploy-test-switch self-test: all cases passed (the four receiver shapes, every refusal, the hosts reader, listed is not ready, the shipped declaration is off)")
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
    a = ap.parse_args(argv)
    try:
        if a.cmd == "decide":
            on, why = decide(load_declaration(a.declaration))
            print(f"enabled={'true' if on else 'false'}")
            print(f"why={why}")
        elif a.cmd == "body":
            print(json.dumps(body(a.kind, deployment=a.deployment, version=a.version, tester_digest=a.tester_digest,
                                  portal_digest=a.portal_digest, runner_digest=a.runner_digest,
                                  core_tests_digest=a.core_tests_digest, reason=a.reason, run=a.run),
                             separators=(",", ":")))
        else:
            for k, v in read_hosts(json.loads(a.hosts.read_text()), _version(a.version)).items():
                print(f"{k}={v}")
    except (Refusal, ValueError) as e:
        print(f"::error::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
