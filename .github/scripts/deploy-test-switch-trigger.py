#!/usr/bin/env python3
"""deploy-test-switch-trigger.py — WHEN deploy → test → switch starts on its own, and for WHICH set.

The platform's CD dispatches to no repository (policy `dependents-verify-on-their-side`), so
nothing outside this repository announces a sealed set. `build-host.yml` PULLS the sets from the
registry every 15 minutes and builds each one's runner image `mw-build-host:<version>`. The
deploy → test → switch lane (`build-host-deploy-test-switch.yml`, P4) is chained off THAT lane,
inside this repository: `on: workflow_run` of build-host, `types: [completed]`. A completed
build-host run starts P4 for a set only when ALL of these hold (`select`, below):

  1. the build-host run read a set — it ran on `schedule`, `workflow_dispatch` or
     `repository_dispatch`, never on a pull request or a push (those only test this tree);
  2. it concluded `success` — a red build-host run hands nothing on;
  3. the candidate is the NEWEST ARMED set (`memex-portal-ai`'s newest version tag, the same
     reading `platform-set-dispatch.py discover` makes). Armed, not merely promoted: the deploy
     event carries the portal digest, which exists only once the set is armed. A set whose runner
     was built while it was only promoted becomes a candidate on the first build-host run after it
     is armed — which is why the trigger reads the registry rather than "the image this run built";
  4. its runner `mw-build-host:<version>` EXISTS — the build host it deploys is that image;
  5. it is NEW: `GET /api/builds/hosts` does not list it at all (not as `v/<version>`, so not
     latest, candidate or previous either), and it is newer than `latest` (never backwards);
  6. no earlier AUTOMATIC run attempted it: the apply job records each automatic attempt as the
     artifact `deploy-test-switch-attempt-<version>` before it sends anything. A candidate that
     failed and was torn down is unlisted again — without this record every 15-minute tick would
     deploy and tear it down once more. A person re-runs a set by `workflow_dispatch`; a newer
     armed set supersedes it. 🚨 Only a marker THIS lane wrote counts: its run must be this
     workflow's own `workflow_run` execution on the default branch, in this repository. A marker
     from a pull request (a fork can upload any artifact name), a push or another workflow is
     ignored — otherwise anyone could hold a set back from automatic deployment for 90 days.

The selection runs TWICE: once in `select`, and again inside `apply` AFTER it has acquired the
`apply` concurrency group (`--exclude-run` = its own run). Two build-host completions can select
the same set before either records its attempt; the second waits for the group while the first
deploys, fails and tears down — so a run that waited re-reads hosts, the newest armed set and
the attempts, and stands down unless it would still start the SAME set.

Then (`record`) the candidate's commits come from the platform CD run that built it — its
`promotion-record` artifact, the run named by the set's `staging-<core7>-<run id>` tag: the full
core commit (whose seven characters must be the staging tag's) and the MeshWeaver.Plugins commit the
set was built with, which P4's dependent gate run tests. A record that does not match the set is
RED, never a guess.

    deploy-test-switch-trigger.py armed --portal p.json
        → version=<newest armed set>   (exit 1 when nothing was ever armed)
    deploy-test-switch-trigger.py select --upstream-event E --upstream-conclusion C --portal p.json \\
        --runner r.json (--hosts h.json | --api https://…/api/builds [--window S]) --attempts a.json \\
        --attempt-runs runs.json --default-branch main [--exclude-run ID]
        → start=true|false, version=, core7=, run_id=, why=
    deploy-test-switch-trigger.py record --record promotion-record.json --version V --core7 c
        → core_sha=, plugins_sha=
    deploy-test-switch-trigger.py --self-test
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
READ_EVENTS = ("schedule", "workflow_dispatch", "repository_dispatch")
ATTEMPT_PREFIX = "deploy-test-switch-attempt-"
# The ONE workflow whose automatic runs write attempt markers (`apply`, on `workflow_run`).
WORKFLOW = ".github/workflows/build-host-deploy-test-switch.yml"
SHA = re.compile(r"^[0-9a-f]{40}$")
CORE7 = re.compile(r"^[0-9a-f]{7}$")


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


psd = _load("platform_set_dispatch", "platform-set-dispatch.py")
dts = _load("deploy_test_switch", "deploy-test-switch.py")


class Refusal(Exception):
    pass


def armed(portal: object) -> dict:
    """{version, core7, run_id} of the newest ARMED set. Nothing ever armed is a Refusal: the
    registry this lane deploys from has never armed anything, which is a reading gone wrong."""
    try:
        got = psd.newest_set(portal, "memex-portal-ai")
    except psd.Refusal as e:
        raise Refusal(str(e)) from e
    if got is None:
        raise Refusal("memex-portal-ai carries no set version tag at all — refusing to read that as 'nothing to deploy'")
    return got


def _trusted(run: dict, default_branch: str) -> bool:
    """Whether `run` (GET /repos/{repo}/actions/runs/{id}) is THIS lane's own automatic execution:
    this workflow file, on `workflow_run`, on the default branch, from this repository (not a fork)."""
    path = str(run.get("path") or "").split("@", 1)[0]
    repo, head = run.get("repository"), run.get("head_repository")
    same_repo = isinstance(repo, dict) and isinstance(head, dict) and repo.get("id") is not None \
        and repo.get("id") == head.get("id")
    return path == WORKFLOW and run.get("event") == "workflow_run" \
        and run.get("head_branch") == default_branch and same_repo


def attempted(listing: object, version: str, runs: object, default_branch: str, exclude_run: str = "") -> str:
    """The run that already attempted `version` automatically, or "" when none did. `listing` is
    GET /repos/{repo}/actions/artifacts?name=<ATTEMPT_PREFIX><version>; `runs` is the list of
    GET /repos/{repo}/actions/runs/{id} answers for the runs those artifacts name. A marker counts
    only when its run is this lane's own automatic run (`_trusted`) — any other run can upload an
    artifact of that name. An answer that cannot be read is a Refusal — an unreadable record of
    attempts must never read as "never attempted"."""
    if not isinstance(listing, dict) or not isinstance(listing.get("artifacts"), list) \
            or type(listing.get("total_count")) is not int:
        raise Refusal("the artifacts listing is not {total_count, artifacts[]} — cannot say whether the set was attempted")
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        raise Refusal("the attempt runs are not a list of workflow-run objects — cannot say which markers this lane wrote")
    by_id = {str(r.get("id")): r for r in runs}
    want = ATTEMPT_PREFIX + version
    for a in listing["artifacts"]:
        if not (isinstance(a, dict) and a.get("name") == want):
            continue
        wr = a.get("workflow_run")
        run_id = str(wr.get("id") or "") if isinstance(wr, dict) else ""
        if run_id and run_id == str(exclude_run or ""):
            continue  # the run asking — it re-checks before it records its own attempt
        run = by_id.get(run_id) if run_id else None
        if run is None:
            raise Refusal(f"the marker {want} names run {run_id or '?'}, which was not read — cannot say whether this lane wrote it")
        if _trusted(run, default_branch):
            return run_id
    if listing["total_count"] and not listing["artifacts"]:
        raise Refusal(f"the artifacts listing counts {listing['total_count']} but lists none — cannot say whether the set was attempted")
    return ""


def _ordinal(version: str) -> int:
    n = psd.run_number_of(version)
    if n is None:
        raise Refusal(f"{version!r} carries no CD run ordinal — cannot order it against latest")
    return n


def select(*, upstream_event: str, upstream_conclusion: str, portal: object, runner: object,
           hosts: object, attempts: object, runs: object, default_branch: str, exclude_run: str = "") -> dict:
    """Whether a completed build-host run starts deploy → test → switch, and for which set. Every
    "no" says why; an input that cannot be read is a Refusal, never a "no"."""
    def no(why: str, cand: dict | None = None) -> dict:
        c = cand or {}
        return {"start": "false", "version": c.get("version", ""), "core7": c.get("core7", ""),
                "run_id": c.get("run_id", ""), "why": why}

    if upstream_event not in READ_EVENTS:
        return no(f"the build-host run ran on `{upstream_event}`, which reads no set (only {', '.join(READ_EVENTS)} do)")
    if upstream_conclusion != "success":
        return no(f"the build-host run concluded `{upstream_conclusion}` — only a successful one starts a deploy")
    cand = armed(portal)
    v = cand["version"]
    try:
        runners = {t for tags in psd._rows(runner, "mw-build-host") for t in tags}
    except psd.Refusal as e:
        raise Refusal(str(e)) from e
    if v not in runners:
        return no(f"the newest armed set {v} has no mw-build-host:{v} yet — the build-host run that builds it starts the deploy", cand)
    try:
        h = dts.read_hosts(hosts, v) if dts.VERSION.fullmatch(v) else None
    except dts.Refusal as e:
        raise Refusal(f"GET /api/builds/hosts: {e}") from e
    if h is None:
        raise Refusal(f"the newest armed set {v!r} is not an x.y.z version — the build hosts deploy only those")
    if h["latest"] == v:
        return no(f"{v} is already latest — never re-deployed", cand)
    if h["listed"] == "true":
        named = [a for a in dts.ALIASES if h[a] == v]
        return no(f"{v} is already deployed as v/{v}" + (f" (alias {', '.join(named)})" if named else ""), cand)
    if h["latest"] and _ordinal(v) <= _ordinal(h["latest"]):
        return no(f"{v} is not newer than latest {h['latest']} — never backwards", cand)
    by = attempted(attempts, v, runs, default_branch, exclude_run)
    if by:
        return no(f"{v} was already attempted automatically (run {by}) and is not deployed now — it is not retried "
                  "on its own; re-run it by workflow_dispatch, or a newer armed set supersedes it", cand)
    return {"start": "true", **cand, "why": f"{v} is the newest armed set, has its runner, and is not deployed — "
                                              f"deploy → test → switch starts for it (latest: {h['latest'] or 'none'})"}


def record(rec: object, version: str, core7: str) -> dict:
    """The candidate's commits from its CD run's promotion record — checked against the set."""
    if not isinstance(rec, dict):
        raise Refusal("the promotion record is not a JSON object")
    core, plugins = str(rec.get("core_sha") or ""), str(rec.get("plugins_sha") or "")
    if not SHA.fullmatch(core) or not SHA.fullmatch(plugins):
        raise Refusal(f"the promotion record's core_sha {core!r} / plugins_sha {plugins!r} are not 40-hex commits")
    if not CORE7.fullmatch(core7 or "") or not core.startswith(core7):
        raise Refusal(f"the promotion record's core_sha {core} is not the set's commit {core7!r} (its staging tag)")
    if str(rec.get("v_portal") or "") != version:
        raise Refusal(f"the promotion record is for portal {rec.get('v_portal')!r}, not the set {version}")
    return {"core_sha": core, "plugins_sha": plugins}


# ─────────────────────────────────────────── self-test ───────────────────────────────────────────

def _self_test() -> int:
    fails: list[str] = []
    total = 0

    def check(name, fn, want=None, refused=False):
        nonlocal total
        total += 1
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

    V, OLD = "3.1.10412", "3.1.10400"
    portal = [{"tags": ["staging-aaaaaaa-901", V, "arm-complete-" + V]},
              {"tags": ["staging-bbbbbbb-800", OLD]}]
    runner = [{"tags": [V]}, {"tags": [OLD]}]

    def hosts(latest=OLD, rows=(OLD,), candidate="", previous=""):
        return {"aliases": {"latest": latest, "candidate": candidate, "previous": previous},
                "versions": [{"version": r, "deploymentName": f"build-{r.replace('.', '-')}", "complete": True}
                             for r in rows]}

    none = {"total_count": 0, "artifacts": []}
    ok = dict(upstream_event="schedule", upstream_conclusion="success", portal=portal, runner=runner,
              hosts=hosts(), attempts=none, runs=[], default_branch="main")

    def run(rid, *, path=WORKFLOW, event="workflow_run", branch="main", head_repo=1):
        return {"id": rid, "path": path, "event": event, "head_branch": branch,
                "repository": {"id": 1}, "head_repository": {"id": head_repo}}

    def marker(rid, version=V):
        return {"total_count": 1, "artifacts": [{"name": ATTEMPT_PREFIX + version, "workflow_run": {"id": rid}}]}

    def sel(**kw):
        return lambda: select(**{**ok, **kw})

    starts = lambda o: o["start"] == "true"  # noqa: E731
    stays = lambda says: (lambda o: o["start"] == "false" and says in o["why"])  # noqa: E731

    # ── the positive case, and its set ──
    check("a successful scheduled build-host run with a NEW armed set that has its runner STARTS the deploy",
          sel(), lambda o: starts(o) and o["version"] == V and o["core7"] == "aaaaaaa" and o["run_id"] == "901")
    check("a dispatched build-host run starts it too", sel(upstream_event="workflow_dispatch"), starts)
    check("nothing deployed at all yet (no latest): starts", sel(hosts=hosts(latest="", rows=())), starts)
    check("a retained prerelease (legacy latest) does not block the selection",
          sel(hosts=hosts(latest="3.0.0-ci.9898", rows=("3.0.0-ci.9898", "3.1.1-alpha.1"))), starts)
    # ── 🚨 THE NEGATIVE CONTROL: a set that is already deployed does NOT start the deploy ──
    check("NEGATIVE CONTROL: the set already latest is never re-deployed",
          sel(hosts=hosts(latest=V, rows=(V, OLD), previous=OLD)), stays("already latest"))
    check("NEGATIVE CONTROL: the set already deployed as v/<version> (the candidate in flight) does not start",
          sel(hosts=hosts(rows=(OLD, V), candidate=V)), stays(f"already deployed as v/{V}"))
    check("the set listed with no alias (held by retention) does not start",
          sel(hosts=hosts(rows=(OLD, V))), stays("already deployed"))
    check("a set older than latest never starts (never backwards)",
          sel(hosts=hosts(latest="3.1.10500", rows=("3.1.10500",))), stays("never backwards"))
    check("a set already attempted automatically (failed, torn down, unlisted again) is NOT retried",
          sel(attempts=marker(77), runs=[run(77)]), stays("run 77"))
    check("another set's attempt record does not hold this one",
          sel(attempts={"total_count": 1, "artifacts": [{"name": ATTEMPT_PREFIX + OLD}]}), starts)
    # ── 🚨 only THIS lane's own automatic runs write a marker that counts ──
    check("a FORK pull request's marker of that name does NOT hold the set back",
          sel(attempts=marker(78), runs=[run(78, event="pull_request", branch="feat", head_repo=2)]), starts)
    check("a same-repository pull request's marker does not hold it either",
          sel(attempts=marker(79), runs=[run(79, event="pull_request", branch="feat")]), starts)
    check("another workflow's marker of that name does not hold it",
          sel(attempts=marker(80), runs=[run(80, path=".github/workflows/other.yml")]), starts)
    check("a marker from this workflow on another branch does not hold it",
          sel(attempts=marker(81), runs=[run(81, branch="feat")]), starts)
    check("a forged marker beside the real one: the real one still holds the set",
          sel(attempts={"total_count": 2, "artifacts": [{"name": ATTEMPT_PREFIX + V, "workflow_run": {"id": 78}},
                                                         {"name": ATTEMPT_PREFIX + V, "workflow_run": {"id": 77}}]},
              runs=[run(78, event="pull_request", head_repo=2), run(77)]), stays("run 77"))
    check("refuses a marker whose run was not read (cannot say who wrote it)",
          sel(attempts=marker(77), runs=[]), refused=True)
    check("refuses attempt runs that are not a list", sel(runs={}), refused=True)
    # ── the re-check inside `apply` (after the concurrency group): its OWN run is not an attempt,
    #    but an attempt another run made while it waited holds it ──
    check("the re-check does not count the asking run's own marker",
          sel(attempts=marker(90), runs=[], exclude_run="90"), starts)
    check("NEGATIVE CONTROL (the race): the run that waited sees the attempt the first run recorded, and stands down",
          sel(attempts=marker(77), runs=[run(77)], exclude_run="90"), stays("run 77"))
    check("a pull-request build-host run never starts it", sel(upstream_event="pull_request"), stays("reads no set"))
    check("a push build-host run never starts it", sel(upstream_event="push"), stays("reads no set"))
    check("a failed build-host run never starts it", sel(upstream_conclusion="failure"), stays("`failure`"))
    check("a cancelled build-host run never starts it", sel(upstream_conclusion="cancelled"), stays("`cancelled`"))
    check("the newest armed set without its runner does not start (yet)",
          sel(runner=[{"tags": [OLD]}]), stays(f"no mw-build-host:{V}"))
    check("a newer PROMOTED-only set is not the candidate: the newest ARMED one is",
          sel(portal=portal, runner=runner + [{"tags": ["3.1.10500"]}]), lambda o: starts(o) and o["version"] == V)
    # ── unreadable inputs are RED, never a "no" ──
    check("refuses a registry that never armed anything", sel(portal=[{"tags": ["main"]}]), refused=True)
    check("refuses an armed set naming two commits",
          sel(portal=[{"tags": [V, "staging-aaaaaaa-1", "staging-bbbbbbb-2"]}]), refused=True)
    check("refuses runner metadata that is not a list", sel(runner={}), refused=True)
    check("refuses an unreadable hosts answer", sel(hosts={"versions": []}), refused=True)
    check("refuses an unreadable attempts listing", sel(attempts={"artifacts": "?"}), refused=True)
    check("refuses an attempts listing that counts what it does not list",
          sel(attempts={"total_count": 2, "artifacts": []}), refused=True)
    # ── the promotion record ──
    c, p = "a" * 7 + "1" * 33, "c" * 40
    rec = {"core_sha": c, "plugins_sha": p, "v_portal": V}
    check("the record names the set's commits", lambda: record(rec, V, "aaaaaaa"),
          lambda o: o == {"core_sha": c, "plugins_sha": p})
    check("refuses a record of another commit", lambda: record(rec, V, "bbbbbbb"), refused=True)
    check("refuses a record of another set", lambda: record(rec, OLD, "aaaaaaa"), refused=True)
    check("refuses a record without the plugins commit", lambda: record({**rec, "plugins_sha": ""}, V, "aaaaaaa"), refused=True)
    check("refuses a record that is not an object", lambda: record([], V, "aaaaaaa"), refused=True)

    # ── the wiring: the trigger names build-host's REAL name, and nothing still waits on a dispatch ──
    wf = HERE.parent / "workflows"
    up = (wf / "build-host.yml").read_text()
    me = (wf / "build-host-deploy-test-switch.yml").read_text()
    name = re.search(r"^name:\s*(.+?)\s*$", up, re.MULTILINE)
    trig = re.search(r"^  workflow_run:\n    workflows: \[\"(.+?)\"\]\n    types: \[completed\]$", me, re.MULTILINE)
    check("P4 is chained on build-host's exact workflow name (a mismatch never fires, silently)",
          lambda: (name and name.group(1), trig and trig.group(1)), lambda t: t[0] and t[0] == t[1])
    check("P4 no longer waits on the platform-set-sealed dispatch nobody sends",
          lambda: me, lambda t: "repository_dispatch" not in t.split("\njobs:")[0])
    check("this module's WORKFLOW is the lane's real file", lambda: (HERE.parents[1] / WORKFLOW).is_file(), bool)

    def _apply_rechecks(t: str) -> bool:
        if "\n  apply:\n" not in t:
            return False
        a = t.split("\n  apply:\n", 1)[1].split("\n  report:\n", 1)[0]
        group = a.find("group: build-host-deploy-test-switch-apply")
        again = a.find("deploy-test-switch-select.sh")
        rec = a.find("- name: Record this automatic attempt")
        return -1 < group < again < rec and "EXCLUDE_RUN: ${{ github.run_id }}" in a[:rec] \
            and "actions: read" in a[:again] and "steps.what.outputs.action != 'none'" in a[rec:rec + 400]
    check("apply re-selects UNDER its concurrency group, excluding its own run, before it records an attempt",
          lambda: me, _apply_rechecks)
    sh = (HERE / "deploy-test-switch-select.sh").read_text()
    check("select and apply share ONE selection script, which reads every marker's run",
          lambda: sh, lambda t: "actions/runs/" in t and "--attempt-runs" in t and "--default-branch" in t)

    # ── the CLI, end to end through files and a FAKE curl for GET /hosts ──
    me_py = str(Path(__file__).resolve())
    with tempfile.TemporaryDirectory() as tmp:
        def f(n, v):
            Path(tmp, n).write_text(json.dumps(v))
            return os.path.join(tmp, n)

        def cli(*args, env=None):
            return subprocess.run([sys.executable, me_py, *args], capture_output=True, text=True, env=env)

        base = ["select", "--upstream-event", "schedule", "--upstream-conclusion", "success",
                "--portal", f("p.json", portal), "--runner", f("r.json", runner), "--attempts", f("a.json", none),
                "--attempt-runs", f("runs.json", []), "--default-branch", "main"]
        r = cli(*base, "--hosts", f("h.json", hosts()))
        check("CLI select: start=true and the set on stdout", lambda: r,
              lambda r: r.returncode == 0 and {"start=true", f"version={V}", "core7=aaaaaaa", "run_id=901"} <= set(r.stdout.splitlines()))
        r = cli(*base, "--hosts", f("h2.json", hosts(latest=V, rows=(V,))))
        check("CLI select NEGATIVE CONTROL: the deployed set → exit 0, start=false", lambda: r,
              lambda r: r.returncode == 0 and "start=false" in r.stdout.splitlines())
        r = cli(*base[:-6], "--attempts", f("a2.json", marker(77)), "--attempt-runs", f("runs2.json", [run(77)]),
                "--default-branch", "main", "--exclude-run", "90", "--hosts", f("h5.json", hosts()))
        check("CLI select --exclude-run: another run's recorded attempt → start=false", lambda: r,
              lambda r: r.returncode == 0 and "start=false" in r.stdout.splitlines())
        r = cli(*base, "--hosts", f("h3.json", {"nope": 1}))
        check("CLI select: an unreadable hosts answer → exit 1, nothing on stdout", lambda: r,
              lambda r: r.returncode == 1 and r.stdout == "" and "::error::" in r.stderr)
        r = cli("armed", "--portal", f("p.json", portal))
        check("CLI armed: the newest armed version", lambda: r, lambda r: r.returncode == 0 and r.stdout.strip() == f"version={V}")
        r = cli("record", "--record", f("rec.json", rec), "--version", V, "--core7", "aaaaaaa")
        check("CLI record: both commits", lambda: r,
              lambda r: r.returncode == 0 and {f"core_sha={c}", f"plugins_sha={p}"} <= set(r.stdout.splitlines()))
        # GET /hosts through a fake curl: a 503 (an outage) then the listing.
        bindir = Path(tmp, "bin")
        bindir.mkdir()
        state = Path(tmp, "n")
        state.write_text("0")
        fake = bindir / "curl"
        fake.write_text("#!/bin/sh\n"
                        f"n=$(cat {state}); echo $((n+1)) > {state}\n"
                        "out=''; while [ $# -gt 0 ]; do [ \"$1\" = -o ] && out=$2; shift; done\n"
                        f"if [ \"$n\" = 0 ]; then echo '{{}}' > \"$out\"; printf 503; "
                        f"else cp {f('h4.json', hosts())} \"$out\"; printf 200; fi\n")
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}
        r = cli(*base, "--api", "https://control.example.com/api/builds", "--window", "5", "--poll", "0.01", env=env)
        check("CLI select --api: a 503 outage is retried, then the listing decides (start=true)", lambda: r,
              lambda r: r.returncode == 0 and "start=true" in r.stdout.splitlines() and state.read_text().strip() == "2")

    if fails:
        print("deploy-test-switch-trigger self-test FAILED:")
        for x in fails:
            print("  - " + x)
        return 1
    print(f"deploy-test-switch-trigger self-test: {total} cases passed (a successful set-reading build-host run "
          "starts the deploy for the NEWEST ARMED set with its runner; NEGATIVE CONTROL: an already-deployed or "
          "already-latest set does not start; nor a PR/push/failed upstream, an older set, a set without its runner, "
          "or one already attempted by THIS lane (a fork's or another workflow's marker does not count; the "
          "re-check under the apply group sees an attempt made while it waited); unreadable inputs are red; the record must match the set; the trigger names "
          "build-host's real name)")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("armed")
    a_.add_argument("--portal", type=Path, required=True)
    s = sub.add_parser("select")
    s.add_argument("--upstream-event", required=True)
    s.add_argument("--upstream-conclusion", required=True)
    s.add_argument("--portal", type=Path, required=True)
    s.add_argument("--runner", type=Path, required=True)
    s.add_argument("--attempts", type=Path, required=True)
    s.add_argument("--attempt-runs", type=Path, required=True)
    s.add_argument("--default-branch", required=True)
    s.add_argument("--exclude-run", default="")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--hosts", type=Path)
    g.add_argument("--api")
    s.add_argument("--window", type=float, default=120.0)
    s.add_argument("--poll", type=float, default=10.0)
    r = sub.add_parser("record")
    r.add_argument("--record", type=Path, required=True)
    r.add_argument("--version", required=True)
    r.add_argument("--core7", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "armed":
            out = {"version": armed(json.loads(a.portal.read_text()))["version"]}
        elif a.cmd == "select":
            if a.hosts:
                hosts = json.loads(a.hosts.read_text())
            else:
                hosts = dts.get_hosts(dts._api(a.api), time.monotonic() + a.window, a.poll)
            out = select(upstream_event=a.upstream_event, upstream_conclusion=a.upstream_conclusion,
                         portal=json.loads(a.portal.read_text()), runner=json.loads(a.runner.read_text()),
                         hosts=hosts, attempts=json.loads(a.attempts.read_text()),
                         runs=json.loads(a.attempt_runs.read_text()), default_branch=a.default_branch,
                         exclude_run=a.exclude_run)
        else:
            out = record(json.loads(a.record.read_text()), a.version, a.core7)
    except (Refusal, dts.Refusal, psd.Refusal, ValueError, OSError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
