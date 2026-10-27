#!/usr/bin/env python3
"""platform-set-dispatch.py — read the platform's sealed sets, and any hand-over of one, for this repository.

The platform's CD (Systemorph/MeshWeaver `main-cd.yml`) BUILDS and SEALS the product image set
and nothing else, and it dispatches to NO repository: the platform knows nothing about the
repositories that consume its sets (policy `dependents-verify-on-their-side`; core's guard
`PlatformReleaseNotifyGuard.CoreDispatchesToNoRepository`). So this repository PULLS: on its own
`schedule`, `build-host.yml` reads the platform's PUBLISHED, SEALED sets from the registry they
were sealed into (`discover`, below) and turns the newest one that still lacks a build-host runner
into a hand-over (`claim`). This repository owns what happens next: the build-host runner image for
the set, and the hand-over of the newest ARMED set to the standing build host `build-latest`.

The same contract is read for the two events that carry a hand-over explicitly: a
`workflow_dispatch` (a person re-running one) and a `repository_dispatch` of type
`platform-set-sealed` — the shape an event-based emitter (memex, which fans the platform's events
out — never the platform's CD itself) sends.

THE CONTRACT (schema 1) — `client_payload`:

    {
      "schema": 1,
      "run":   {"url": "<the CD run's URL>", "coreSha": "<40 hex: the commit the CD run ran>"},
      "set":   {"version": "<x.y.z>" | "", "coreSha": "<40 hex>" | ""},
      "armed": {"armed": true | false, "version": "<x.y.z>" | "",
                "coreSha": "<40 hex>" | "", "pluginsSha": "<40 hex>" | ""}
    }

  * `set` is the set THIS CD run published (its image version and the commit it was built from),
    or both empty when the run published nothing (a reconcile tick with nothing missing). An empty
    set is a statement, not an absence: the image job says so by name and builds nothing.
  * `armed` is what this CD run's `arm` job decided. It is a HINT, never the truth: the hand-over
    re-reads the newest armed set from the registry (`arm-promoted-set.py armed-state`, the same
    reading the platform makes) and uses `armed.coreSha`/`pluginsSha` only when `armed.version` IS
    that newest set — exactly what core's former `build-host-latest` job did with its `needs.arm`
    outputs.
  * `run.coreSha` is the platform commit whose scripts the jobs here fetch (acr-login.sh,
    arm-promoted-set.py, …): the scripts of the same vintage as the run that sent the event.

🚨 NOTHING IN THE PAYLOAD IS TRUSTED BY SHAPE ALONE. Every field is validated here (a malformed
payload is RED, naming the field), every commit is then checked to be on the platform's `main`
(`build-host.yml`, GitHub's compare API on the public repository), and every version is resolved
against the registry before anything is built or signed. A `workflow_dispatch` (a person re-running
a hand-over) goes through the same reader with the same rules.

    platform-set-dispatch.py read --event "$GITHUB_EVENT_PATH" --event-name "$GITHUB_EVENT_NAME"
        → GITHUB_OUTPUT lines: run_url, run_core_sha, set_version, set_core_sha, armed,
          armed_version, armed_core_sha, armed_plugins_sha, shas (space-separated, de-duplicated)
    platform-set-dispatch.py discover --tester t.json --portal p.json --runner r.json
        (each `acr-data-plane.py manifests` JSON — the rows `az acr manifest list-metadata -o json` prints — of mw-plugin-test / memex-portal-ai /
        mw-build-host) → lines: promoted_version, promoted_core7, promoted_run_id, need_version,
        need_core7, need_run_id, backlog (retained sets in the window still lacking a runner), why
    platform-set-dispatch.py claim --run-url U --run-core-sha S [--set-version V --set-core-sha S]
        → the same lines `read` prints, for a hand-over this repository discovered itself
    platform-set-dispatch.py runner-key --repo <this repository's checkout, FULL history>
        → lines: inputs_commit (the newest commit touching the runner inputs), runner_rev (its
          ancestor count — the revision every runner built from those inputs is tagged with)
    platform-set-dispatch.py runner-choice --runner r.json --version V
        → lines: runner_tag, runner_digest, runner_rev, superseded (space-separated digests of the
          set's OLDER runners) — what the hand-over sends for set V
    platform-set-dispatch.py --self-test

DISCOVERY (the pull). A set is PROMOTED when `mw-plugin-test` carries its version tag (main-cd
phase A) and ARMED when `memex-portal-ai` does (phase C). Both tags sit on the manifest the run's
own `staging-<core7>-<run id>` tag names, so the manifest says which platform commit and which CD
run the set is. `discover` picks the ONE set this tick builds a runner for:

  1. the newest ARMED set, when `mw-build-host:<version>` does not exist yet — the build-latest
     hand-over needs its runner NOW;
  2. otherwise the OLDEST retained PROMOTED set (an armed set is a promoted one) that still lacks
     its runner (BACKFILL). Every set `mw-plugin-test` still carries a version tag for is a
     candidate, not only the newest — the image job needs that tag, so a version only the portal
     still carries is not one:
     two sets that both arrive between two ticks (A, then B, so only B is ever the newest) would
     otherwise leave A without a runner forever, and a build-stable hand-over whose main-passed
     ceiling is A would fail on the missing `mw-build-host:A`.

THE BOUNDS. One runner image per tick (the `image` job builds exactly the one set named here; a tick
is 15 minutes). The backfill window is the retained sets NOT OLDER than the oldest set
`mw-build-host` still carries — a set older than every runner predates the runner lane, or its
runner aged out with the registry's retention, and is not rebuilt — and at most the
`BACKFILL_WINDOW` newest of them. A registry with no runner at all backfills nothing: only the
newest armed and the newest promoted set get one. A ceiling outside the window still gets its runner
— `build-host-stable.yml` builds it on demand before it hands the ceiling over.

THE RUNNER FOLLOWS DEPLOY'S MAIN (owner: "always latest everything is installed"). A set's runner is
built from TWO sources: the platform at the set's commit, and THIS repository's runner inputs
(`RUNNER_INPUTS`: `build-run.py`, `deploy/build-host/`, `deploy/helm/`). Keyed by the set alone, a
runner was built once and never again, so a Deploy change to the runner reached the host only when
the platform moved. A runner is therefore keyed by (set, runner revision):
`mw-build-host:<version>-r<rev>`, where `rev` is the ancestor count of the newest commit that touched
the runner inputs (`runner-key`) — monotone along `main`, unchanged by commits that do not touch
them. The plain `mw-build-host:<version>` tag is still written once (the first runner of the set),
for the lanes that hand over a version's ONE runner (build-host-stable.yml, the versioned
deploy-test-switch). `discover` adds a third reason to build, between the first two: the newest armed
set HAS a runner but none at Deploy main's current `rev` — a REFRESH. The hand-over then sends the
set's highest-`rev` runner and names every older one as superseded (`runner-choice`), and the
receiver takes a runner for the SAME set only when it supersedes the runner held — so a re-run of an
old hand-over can never move the host's runner backwards (MeshWeaver.Plugins `BuildHostUpdates`).

The registry is the record: what a registry listing cannot place (a NEWEST version tag with no
staging tag, or naming two commits) is a Refusal, never a guess. An OLDER set it cannot place is
skipped from the backfill and named in `why` — one unreadable old manifest must not stop the newest
set from getting its runner.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EVENT_TYPE = "platform-set-sealed"
SCHEMA = 1
SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
URL = re.compile(r"^https://[^\s\"'`$\\]+$")
PLATFORM_RUNS = "https://github.com/Systemorph/MeshWeaver/actions/runs/"
# The platform's set names, read exactly as its `arm-promoted-set.py` `run_number_of` reads them
# (the run ordinal orders sets): `<x.y.z>[-pre]-ci.<run>` before line 3.1, the plain
# `<major>.<minor>.<run>` from 3.1 on (policy `platform-semver-versioning`). A zero patch is a floor
# or a release, never a CD run.
LEGACY_SET = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?[.-]ci\.(\d+)$")
SEMVER_SET = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SEMVER_ERA_START = (3, 1)
STAGING_TAG = re.compile(r"^staging-([0-9a-f]{7})-([0-9]+)$")
# The backfill considers at most this many of the newest retained sets (see THE BOUNDS above). A
# ceiling older than that is built on demand by build-host-stable.yml.
BACKFILL_WINDOW = 60
# What of THIS repository a runner image is built from (deploy/build-host/Dockerfile copies exactly
# these: the runner, the prepared-cluster script, the Dockerfile itself, and the portal chart handed
# to stages as MW_DEPLOY_SOURCE). A commit touching none of them builds byte-identical runners.
RUNNER_INPUTS = (".github/scripts/build-run.py", "deploy/build-host", "deploy/helm")
# `<set version>-r<rev>`: a runner keyed by its set AND its runner revision. The plain `<version>`
# tag is revision 0 (the runner written before the key existed, or the set's first one).
RUNNER_REV_TAG = re.compile(r"^(.+)-r([1-9][0-9]{0,8})$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class Refusal(Exception):
    pass


def _sha(v: object, field: str, allow_empty: bool) -> str:
    s = "" if v is None else str(v).strip()
    if not s and allow_empty:
        return ""
    if not SHA.fullmatch(s):
        raise Refusal(f"{field} must be a full 40-hex commit sha, got {s!r}")
    return s


def _version(v: object, field: str) -> str:
    s = "" if v is None else str(v).strip()
    if s and not VERSION.fullmatch(s):
        raise Refusal(f"{field} must be a platform version (x.y.z[-pre]) or empty, got {s!r}")
    return s


def _bool(v: object, field: str) -> bool:
    if isinstance(v, bool):
        return v
    s = "" if v is None else str(v).strip().lower()
    if s in ("true", "false"):
        return s == "true"
    raise Refusal(f"{field} must be true or false, got {v!r}")


def read_payload(p: object) -> dict:
    """The validated hand-over. Raises Refusal naming the first field that is wrong."""
    if not isinstance(p, dict):
        raise Refusal("client_payload is not an object")
    if p.get("schema") != SCHEMA:
        raise Refusal(f"client_payload.schema must be {SCHEMA}, got {p.get('schema')!r} — "
                      "the sender and this reader disagree on the contract; change both together")
    run = p.get("run") if isinstance(p.get("run"), dict) else None
    st = p.get("set") if isinstance(p.get("set"), dict) else None
    armed = p.get("armed") if isinstance(p.get("armed"), dict) else None
    if run is None or st is None or armed is None:
        raise Refusal("client_payload must carry the objects run, set and armed")
    url = str(run.get("url") or "").strip()
    if not URL.fullmatch(url):
        raise Refusal(f"run.url must be an https URL, got {url!r}")
    out = {"run_url": url, "run_core_sha": _sha(run.get("coreSha"), "run.coreSha", False)}
    out["set_version"] = _version(st.get("version"), "set.version")
    out["set_core_sha"] = _sha(st.get("coreSha"), "set.coreSha", True)
    if bool(out["set_version"]) != bool(out["set_core_sha"]):
        raise Refusal("set.version and set.coreSha are both present or both empty — a version without "
                      "its commit (or a commit without its version) names no set")
    is_armed = _bool(armed.get("armed"), "armed.armed")
    out["armed"] = "true" if is_armed else "false"
    out["armed_version"] = _version(armed.get("version"), "armed.version")
    out["armed_core_sha"] = _sha(armed.get("coreSha"), "armed.coreSha", True)
    out["armed_plugins_sha"] = _sha(armed.get("pluginsSha"), "armed.pluginsSha", True)
    if is_armed and not (out["armed_version"] and out["armed_core_sha"] and out["armed_plugins_sha"]):
        raise Refusal("armed.armed is true but armed.version / coreSha / pluginsSha are not all present")
    if not is_armed and (out["armed_version"] or out["armed_core_sha"] or out["armed_plugins_sha"]):
        raise Refusal("armed.armed is false but the payload names an armed set — contradictory")
    # The core commits the workflow proves are on the platform's main (plugins_sha is a
    # MeshWeaver.Plugins commit the receiver records; it is not a platform commit).
    shas = [out["run_core_sha"], out["set_core_sha"], out["armed_core_sha"]]
    out["shas"] = " ".join(dict.fromkeys(s for s in shas if s))
    return out


def read_event(event: dict, event_name: str) -> dict:
    if event_name == "repository_dispatch":
        action = event.get("action")
        if action != EVENT_TYPE:
            raise Refusal(f"repository_dispatch of type {action!r} — this workflow reads only {EVENT_TYPE!r}")
        return read_payload(event.get("client_payload"))
    if event_name == "workflow_dispatch":
        i = event.get("inputs") or {}
        return read_payload({
            "schema": SCHEMA,
            "run": {"url": i.get("run_url"), "coreSha": i.get("run_core_sha")},
            "set": {"version": i.get("set_version"), "coreSha": i.get("set_core_sha")},
            # A person re-running a hand-over names no arming: the hand-over reads the newest
            # armed set from the registry, as it always does, and records no commits for it.
            "armed": {"armed": False},
        })
    raise Refusal(f"event {event_name!r} carries no hand-over (only repository_dispatch / workflow_dispatch do)")


def run_number_of(tag: str) -> int | None:
    """The CD run ordinal a set name carries, or None when the tag names no CD set."""
    m = LEGACY_SET.match(tag or "")
    if m:
        return int(m.group(1))
    m = SEMVER_SET.match(tag or "")
    if m and (int(m.group(1)), int(m.group(2))) >= SEMVER_ERA_START and int(m.group(3)) > 0:
        return int(m.group(3))
    return None


def _rows(manifests: object, what: str) -> list[list[str]]:
    if not isinstance(manifests, list):
        raise Refusal(f"the {what} manifest metadata is not a JSON list")
    out = []
    for row in manifests:
        tags = row.get("tags") if isinstance(row, dict) else None
        if isinstance(tags, list):
            out.append([str(t) for t in tags])
    return out


def _place(version: str, tags: list[str], what: str) -> dict:
    """{version, core7, run_id} of `version` from the staging tags on ITS manifest, or a Refusal."""
    staging = [m for t in tags if (m := STAGING_TAG.match(t))]
    cores = {m.group(1) for m in staging}
    if len(cores) != 1:
        raise Refusal(f"{what} {version} names {len(cores)} platform commits through its staging tags "
                      f"({tags}) — cannot say which commit the set is")
    run_id = max(int(m.group(2)) for m in staging)
    return {"version": version, "core7": next(iter(cores)), "run_id": str(run_id)}


def retained_sets(manifests: object, what: str) -> tuple[dict[str, dict], dict[str, str]]:
    """Every set a repository's manifests still carry a version tag for: ({version: {version, core7,
    run_id}}, {version: why it cannot be placed}). Nothing here is a Refusal per set — the caller
    decides whether an unplaceable set matters (the NEWEST one does; an older one is skipped)."""
    placed: dict[str, dict] = {}
    unplaced: dict[str, str] = {}
    for tags in _rows(manifests, what):
        for t in tags:
            if run_number_of(t) is None:
                continue
            try:
                placed[t] = _place(t, tags, what)
            except Refusal as e:
                unplaced[t] = str(e)
    return placed, unplaced


def newest_set(manifests: object, what: str) -> dict | None:
    """{version, core7, run_id} of the newest set a repository's manifests carry a version tag for,
    or None when none carries one. The commit and the run come from the SAME manifest's
    `staging-<core7>-<run id>` tag; a manifest that names no commit, or two, is a Refusal."""
    best: tuple[int, str, list[str]] | None = None
    for tags in _rows(manifests, what):
        for t in tags:
            n = run_number_of(t)
            if n is not None and (best is None or n > best[0]):
                best = (n, t, tags)
    if best is None:
        return None
    return _place(best[1], best[2], what)


def runner_tag(version: str, rev: int) -> str:
    """The tag of `version`'s runner at runner revision `rev`. Pure."""
    if run_number_of(version) is None:
        raise Refusal(f"{version!r} is no set version — refusing to name a runner for it")
    if rev < 1:
        raise Refusal(f"runner revision must be positive, got {rev}")
    return f"{version}-r{rev}"


def parse_runner_tag(tag: str) -> tuple[str, int] | None:
    """(set version, runner revision) a `mw-build-host` tag names — the plain `<version>` is revision 0 —
    or None for a tag that names no set's runner (a commit, `main`, …). Pure."""
    if run_number_of(tag) is not None:
        return tag, 0
    m = RUNNER_REV_TAG.match(tag or "")
    if m and run_number_of(m.group(1)) is not None:
        return m.group(1), int(m.group(2))
    return None


def runner_revisions(runner: object) -> dict[str, dict[int, str]]:
    """{set version: {runner revision: digest}} from the `mw-build-host` manifest metadata. A row with no
    digest still counts as a runner that EXISTS (its digest reads as ""). Pure."""
    out: dict[str, dict[int, str]] = {}
    for row in runner if isinstance(runner, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("tags"), list):
            continue
        digest = str(row.get("digest") or "")
        for t in row["tags"]:
            parsed = parse_runner_tag(str(t))
            if parsed is not None:
                out.setdefault(parsed[0], {})[parsed[1]] = digest
    return out


def runner_choice(runner: object, version: str) -> dict:
    """What the hand-over sends for set `version`: its HIGHEST-revision runner, and every older runner of
    the set (a different digest) named as superseded — so the receiver can tell a newer runner of the same
    set from a re-sent older one. An empty `runner_digest` when the set has no runner at all. Pure."""
    if not isinstance(runner, list):
        raise Refusal("the mw-build-host manifest metadata is not a JSON list")
    revs = runner_revisions(runner).get(version, {})
    if not revs:
        return {"runner_tag": "", "runner_digest": "", "runner_rev": "", "superseded": ""}
    top = max(revs)
    digest = revs[top]
    if not DIGEST.fullmatch(digest):
        raise Refusal(f"mw-build-host revision {top} of {version} carries no readable digest ({digest!r})")
    older = sorted({d for r, d in revs.items() if r < top and d != digest})
    for d in older:
        if not DIGEST.fullmatch(d):
            raise Refusal(f"an older runner of {version} carries no readable digest ({d!r}) — refusing to "
                          "hand over a runner whose predecessors cannot all be named")
    return {"runner_tag": version if top == 0 else runner_tag(version, top), "runner_digest": digest,
            "runner_rev": str(top), "superseded": " ".join(older)}


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise Refusal(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def runner_key(repo: Path) -> dict:
    """{inputs_commit, runner_rev} of the checkout at `repo`: the newest commit (from HEAD) that touched
    `RUNNER_INPUTS`, and its ancestor count. 🚨 A SHALLOW checkout is refused: its ancestor count is
    the clone depth, not the history, and would tag a runner with a revision that means nothing."""
    if _git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise Refusal(f"{repo} is a shallow clone — the runner revision is an ancestor count and needs the "
                      "full history (actions/checkout fetch-depth: 0)")
    commit = _git(repo, "log", "-1", "--format=%H", "HEAD", "--", *RUNNER_INPUTS)
    if not SHA.fullmatch(commit):
        raise Refusal(f"no commit in {repo} touches the runner inputs {list(RUNNER_INPUTS)} — not this repository?")
    return {"inputs_commit": commit, "runner_rev": str(int(_git(repo, "rev-list", "--count", commit)))}


def discover(tester: object, portal: object, runner: object, runner_rev: int | None = None) -> dict:
    """The newest PROMOTED set (mw-plugin-test's newest version tag) and the ONE set this tick builds a
    runner for: the newest ARMED set (memex-portal-ai's) when it lacks any runner, else — given Deploy
    main's `runner_rev` — the newest ARMED set again when all its runners predate that revision (a
    REFRESH: THE RUNNER FOLLOWS DEPLOY'S MAIN), else the OLDEST retained set in the backfill window
    that lacks one (module docstring, DISCOVERY and THE BOUNDS). A tester repository with no version tag at all is a Refusal: a registry that
    never promoted anything is not 'nothing to do', it is a reading that went wrong."""
    promoted = newest_set(tester, "mw-plugin-test")
    if promoted is None:
        raise Refusal("mw-plugin-test carries no set version tag at all — refusing to read that as "
                      "'nothing was promoted'")
    armed = newest_set(portal, "memex-portal-ai")
    _rows(runner, "mw-build-host")  # a listing that is not a list is refused, as before
    revisions = runner_revisions(runner)
    # A set HAS a runner when any revision of it exists — the plain tag or a keyed one.
    runners = set(revisions)

    # Every retained PROMOTED set, placed by its own mw-plugin-test manifest: the image job builds a
    # runner only for a set whose `mw-plugin-test:<version>` exists, so a version only the portal
    # still carries (its tester tag aged out) is no backfill candidate — selecting it would fail the
    # image job on every tick and stall the backfill behind it. The portal's placement is a CHECK: a
    # version both repositories carry must name the same commit in both, or it is unplaceable, never
    # a coin toss.
    sets, unplaced = retained_sets(tester, "mw-plugin-test")
    portal_sets, portal_unplaced = retained_sets(portal, "memex-portal-ai")
    for v, why in portal_unplaced.items():
        if v in sets:
            unplaced.setdefault(v, why)
    for v, cand in portal_sets.items():
        mine = sets.get(v)
        if mine is not None and mine["core7"] != cand["core7"]:
            unplaced[v] = (f"{v} is commit {mine['core7']} in mw-plugin-test but {cand['core7']} in "
                           "memex-portal-ai — cannot say which commit the set is")
    # The newest two are what discovery always guaranteed. One of them unplaceable is a Refusal —
    # never a runner built from whichever repository's commit happened to be read last.
    for c in (promoted, armed):
        if c is not None and c["version"] in unplaced:
            raise Refusal(f"the newest set {c['version']} is unplaceable: {unplaced[c['version']]}")
    for v in unplaced:
        sets.pop(v, None)
    sets[promoted["version"]] = promoted
    if armed is not None:
        sets[armed["version"]] = armed

    runner_ordinals = [n for t in runners if (n := run_number_of(t)) is not None]
    floor = min(runner_ordinals) if runner_ordinals else None
    ordered = sorted(sets.values(), key=lambda c: run_number_of(c["version"]) or 0)
    if floor is None:
        # No runner anywhere: nothing to backfill against — the newest armed and promoted sets only.
        window = [c for c in ordered if c["version"] in {promoted["version"], (armed or {}).get("version")}]
    else:
        window = [c for c in ordered if (run_number_of(c["version"]) or 0) >= floor][-BACKFILL_WINDOW:]
        for c in (promoted, armed):  # the newest two are never outside the window
            if c is not None and c not in window:
                window.append(c)
        window.sort(key=lambda c: run_number_of(c["version"]) or 0)
    missing = [c for c in window if c["version"] not in runners]

    need, why, refresh = None, "", False
    if armed is not None and armed["version"] not in runners:
        need, why = armed, f"the newest armed set {armed['version']} has no mw-build-host image yet"
    elif armed is not None and runner_rev is not None and max(revisions[armed["version"]]) < runner_rev:
        need, refresh = armed, True
        why = (f"runner refresh: the newest armed set {armed['version']} has runners up to revision "
               f"{max(revisions[armed['version']])}, Deploy main's runner inputs are revision {runner_rev} — "
               f"build mw-build-host:{runner_tag(armed['version'], runner_rev)}")
    elif missing:
        need = missing[0]
        label = "newest promoted" if need is promoted else "retained"
        why = (f"the oldest {label} set without a runner is {need['version']} — "
               f"backfill, {len(missing)} set(s) in the window still lack one")
    else:
        why = (f"every runner is there: newest promoted {promoted['version']}, newest armed "
               f"{armed['version'] if armed else '(none)'}, {len(window)} retained set(s) in the "
               f"backfill window — nothing to build")
    skipped = sorted(v for v in unplaced if floor is None or (run_number_of(v) or 0) >= floor)
    if skipped:
        why += f" (not backfilled, unplaceable: {', '.join(skipped)})"
    out = {f"promoted_{k}": v for k, v in promoted.items()}
    out.update({f"need_{k}": (need or {}).get(k, "") for k in ("version", "core7", "run_id")})
    out["backlog"] = str(len(missing))
    out["refresh"] = "true" if refresh else "false"
    out["why"] = why
    return out


def claim(run_url: str, run_core_sha: str, set_version: str, set_core_sha: str) -> dict:
    """A hand-over this repository discovered itself, read through the SAME contract as a received
    one. It names no arming: the hand-over reads the newest armed set from the registry, as it
    always does."""
    return read_payload({"schema": SCHEMA, "run": {"url": run_url, "coreSha": run_core_sha},
                         "set": {"version": set_version, "coreSha": set_core_sha},
                         "armed": {"armed": False}})


def _self_test() -> int:
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    good = {"schema": 1, "run": {"url": "https://github.com/o/r/actions/runs/1", "coreSha": a},
            "set": {"version": "3.1.10412", "coreSha": a},
            "armed": {"armed": True, "version": "3.1.10400", "coreSha": b, "pluginsSha": c}}
    fails = []

    def check(name, fn, want):
        try:
            got = fn()
            ok = False if want is REFUSED else want(got)
        except Refusal as e:
            got, ok = f"Refusal: {e}", want is REFUSED
        except Exception as e:  # a crash is never a refusal
            got, ok = f"{type(e).__name__}: {e}", False
        if not ok:
            fails.append(f"{name}: {got}")

    REFUSED = object()

    def refuse(p):
        return lambda: read_payload(p)

    def mod(**kw):
        p = json.loads(json.dumps(good))
        for k, v in kw.items():
            sect, _, field = k.partition("__")
            if field:
                p[sect][field] = v
            else:
                p[sect] = v
        return p

    check("a good payload reads", lambda: read_payload(good),
          lambda o: o["set_version"] == "3.1.10412" and o["armed"] == "true" and o["shas"] == f"{a} {b}")
    check("an empty set is a statement", lambda: read_payload(mod(set__version="", set__coreSha="")),
          lambda o: o["set_version"] == "" and o["set_core_sha"] == "")
    check("not armed reads", lambda: read_payload(mod(armed={"armed": False})),
          lambda o: o["armed"] == "false" and o["armed_version"] == "")
    check("armed as the string 'true' reads", lambda: read_payload(mod(armed__armed="true")),
          lambda o: o["armed"] == "true")
    for name, p in [
        ("a wrong schema", mod(schema=2)),
        ("a missing run", {k: v for k, v in good.items() if k != "run"}),
        ("a short run sha", mod(run__coreSha="abc1234")),
        ("an http run url", mod(run__url="http://github.com/x")),
        ("a url with a shell metacharacter", mod(run__url="https://x/$(id)")),
        ("a version that is not a version", mod(set__version="latest")),
        ("a version without its commit", mod(set__coreSha="")),
        ("a commit without its version", mod(set__version="")),
        ("armed without its commits", mod(armed__pluginsSha="")),
        ("not armed but naming a set", mod(armed__armed=False)),
        ("armed that is not a boolean", mod(armed={"armed": "yes"})),
        ("an upper-case sha", mod(set__coreSha="A" * 40)),
    ]:
        check(f"refuses {name}", refuse(p), REFUSED)
    check("refuses another dispatch type",
          lambda: read_event({"action": "something-else", "client_payload": good}, "repository_dispatch"), REFUSED)
    check("reads its own dispatch type",
          lambda: read_event({"action": EVENT_TYPE, "client_payload": good}, "repository_dispatch"),
          lambda o: o["run_core_sha"] == a)
    check("a workflow_dispatch reads, never armed",
          lambda: read_event({"inputs": {"run_url": "https://github.com/o/r/actions/runs/2", "run_core_sha": a,
                                         "set_version": "3.1.1", "set_core_sha": a}}, "workflow_dispatch"),
          lambda o: o["armed"] == "false" and o["set_version"] == "3.1.1")
    check("refuses a push event", lambda: read_event({}, "push"), REFUSED)
    check("refuses a schedule event (a schedule DISCOVERS, it carries nothing)",
          lambda: read_event({}, "schedule"), REFUSED)

    # ── discovery: the pull from the registry ──
    tester = [{"tags": ["staging-aaaaaaa-901", "3.1.10412", "aaaaaaa", "main"]},
              {"tags": ["staging-bbbbbbb-800", "3.1.10400", "bbbbbbb"]},
              {"tags": ["staging-ccccccc-700", "3.0.0-ci.9000"]}]
    portal = [{"tags": ["staging-bbbbbbb-800", "3.1.10400", "arm-complete-3.1.10400"]},
              {"tags": ["staging-aaaaaaa-901", "aaaaaaa"]}]
    check("run ordinals: semver patch from 3.1, ci.<n> before, never a floor or a sha",
          lambda: [run_number_of(t) for t in ("3.1.10412", "3.0.0-ci.9000", "3.1.0", "3.0.5", "aaaaaaa", "main")],
          lambda o: o == [10412, 9000, None, None, None, None])
    check("the newest promoted set, its commit and run",
          lambda: newest_set(tester, "t"),
          lambda o: o == {"version": "3.1.10412", "core7": "aaaaaaa", "run_id": "901"})
    check("the armed set lacking a runner comes FIRST",
          lambda: discover(tester, portal, []),
          lambda o: o["need_version"] == "3.1.10400" and o["need_core7"] == "bbbbbbb"
          and o["promoted_version"] == "3.1.10412")
    check("then the promoted set lacking one",
          lambda: discover(tester, portal, [{"tags": ["3.1.10400"]}]),
          lambda o: o["need_version"] == "3.1.10412" and o["need_run_id"] == "901")
    check("every runner there: nothing to build, said so",
          lambda: discover(tester, portal, [{"tags": ["3.1.10400"]}, {"tags": ["3.1.10412"]}]),
          lambda o: o["need_version"] == "" and "nothing to build" in o["why"])
    check("nothing armed yet: the promoted set still gets its runner",
          lambda: discover(tester, [], []), lambda o: o["need_version"] == "3.1.10412")
    check("refuses a tester that never promoted anything",
          lambda: discover([{"tags": ["main"]}], portal, []), REFUSED)
    check("refuses a newest set with no staging tag",
          lambda: newest_set([{"tags": ["3.1.10412", "aaaaaaa"]}], "t"), REFUSED)
    check("refuses a newest set naming two commits",
          lambda: newest_set([{"tags": ["3.1.10412", "staging-aaaaaaa-1", "staging-bbbbbbb-2"]}], "t"), REFUSED)
    check("refuses metadata that is not a list", lambda: discover({}, portal, []), REFUSED)

    # ── backfill: every retained set gets its runner, not only the newest ──
    # THE REGRESSION (core #7196 review): two sets arrive between two ticks — A, then B — so both
    # the newest armed and the newest promoted set jump from Z straight to B. B gets its runner on
    # the first tick; A must get one on the next, or a build-stable hand-over whose main-passed
    # ceiling is A fails on the missing mw-build-host:A.
    z, a_, b_ = "3.1.10390", "3.1.10400", "3.1.10412"
    two_tester = [{"tags": ["staging-bbbbbbb-902", b_]}, {"tags": ["staging-aaaaaaa-901", a_]},
                  {"tags": ["staging-ccccccc-900", z]}]
    two_portal = [{"tags": ["staging-bbbbbbb-902", b_]}, {"tags": ["staging-aaaaaaa-901", a_]},
                  {"tags": ["staging-ccccccc-900", z]}]
    check("two sets arriving between ticks: the first tick builds the newest armed set B",
          lambda: discover(two_tester, two_portal, [{"tags": [z]}]),
          lambda o: o["need_version"] == b_ and o["need_run_id"] == "902" and o["backlog"] == "2")
    check("two sets arriving between ticks: A still gets its runner on the next tick",
          lambda: discover(two_tester, two_portal, [{"tags": [z]}, {"tags": [b_]}]),
          lambda o: o["need_version"] == a_ and o["need_core7"] == "aaaaaaa" and o["need_run_id"] == "901"
          and o["backlog"] == "1" and "backfill" in o["why"])
    check("two sets arriving between ticks: then nothing is left to build",
          lambda: discover(two_tester, two_portal, [{"tags": [z]}, {"tags": [b_]}, {"tags": [a_]}]),
          lambda o: o["need_version"] == "" and o["backlog"] == "0" and "nothing to build" in o["why"])
    check("the same, promoted only (nothing armed): A after B, oldest first",
          lambda: discover(two_tester, [], [{"tags": [z]}, {"tags": [b_]}]),
          lambda o: o["need_version"] == a_)
    check("backfill builds the OLDEST missing set first",
          lambda: discover(two_tester, two_portal[:1], [{"tags": [z]}]),
          lambda o: o["need_version"] == b_)  # the newest ARMED still comes first …
    check("… and with the armed set's runner there, the oldest missing retained set is next",
          lambda: discover([{"tags": ["staging-ddddddd-903", "3.1.10420"]}] + two_tester, two_portal[:1],
                           [{"tags": [z]}, {"tags": [b_]}]),
          lambda o: o["need_version"] == a_ and o["backlog"] == "2")
    check("a set OLDER than every runner is not backfilled (it predates the runner lane)",
          lambda: discover(two_tester, two_portal, [{"tags": [a_]}, {"tags": [b_]}]),
          lambda o: o["need_version"] == "" and o["backlog"] == "0")
    check("a registry with no runner at all backfills nothing older than the newest two",
          lambda: discover(two_tester, two_portal[1:], []),
          lambda o: o["need_version"] == a_ and o["backlog"] == "2")
    many = [{"tags": [f"staging-eeeeeee-{1000 + i}", f"3.1.{11000 + i}"]} for i in range(BACKFILL_WINDOW + 20)]
    check(f"the backfill window is the {BACKFILL_WINDOW} newest retained sets",
          lambda: discover(many, [], [{"tags": ["3.1.10000"]}]),
          lambda o: o["need_version"] == f"3.1.{11000 + 20}" and o["backlog"] == str(BACKFILL_WINDOW))
    check("an unplaceable OLDER set is skipped and named, never a refusal of the tick",
          lambda: discover([two_tester[0], {"tags": [a_, "aaaaaaa"]}, two_tester[2]], [],
                           [{"tags": [z]}, {"tags": [b_]}]),
          lambda o: o["need_version"] == "" and a_ in o["why"] and "unplaceable" in o["why"])
    check("a set naming different commits in the two repositories is skipped, never guessed",
          lambda: discover(two_tester, [two_portal[0], {"tags": ["staging-fffffff-901", a_]}, two_portal[2]],
                           [{"tags": [z]}, {"tags": [b_]}]),
          lambda o: o["need_version"] == "" and a_ in o["why"])
    check("a set only the portal still carries is NOT backfilled (its tester tag aged out; the image job could not build it)",
          lambda: discover([two_tester[0], two_tester[2]], two_portal, [{"tags": [z]}, {"tags": [b_]}]),
          lambda o: o["need_version"] == "" and o["backlog"] == "0")
    check("refuses a NEWEST armed set whose commit differs between the two repositories",
          lambda: discover(two_tester, [{"tags": ["staging-fffffff-902", b_]}] + two_portal[1:], [{"tags": [z]}]),
          REFUSED)
    check("refuses a NEWEST promoted set the portal places on another commit",
          lambda: discover(two_tester, [{"tags": ["staging-fffffff-902", b_, "x"]}], [{"tags": [z]}]),
          REFUSED)
    check("a discovered claim reads through the contract, never armed",
          lambda: claim("https://github.com/o/r/actions/runs/901", a, "3.1.10412", a),
          lambda o: o["set_version"] == "3.1.10412" and o["armed"] == "false" and o["shas"] == a)
    check("a discovered claim with nothing to build reads as an empty set",
          lambda: claim("https://github.com/o/r/actions/runs/901", a, "", ""),
          lambda o: o["set_version"] == "")
    check("a discovered claim with an abbreviated commit is refused",
          lambda: claim("https://github.com/o/r/actions/runs/901", a, "3.1.10412", "aaaaaaa"), REFUSED)

    # ── the runner follows Deploy's main: keyed by (set, runner revision) ──
    # THE DEFECT: once a set had `mw-build-host:<version>`, a Deploy change to the runner never reached
    # the host until the platform moved. Each case below that says "refresh" was "nothing to build" before.
    da, db, dc = ("sha256:" + ch * 64 for ch in "abc")
    armed_b = [{"tags": ["staging-bbbbbbb-902", b_]}]
    check("tags: the plain version is revision 0, a keyed one its revision, anything else none",
          lambda: [parse_runner_tag(t) for t in (b_, f"{b_}-r57", "main", "aaaaaaa", f"{b_}-r0", "x-r5", f"{b_}-rx")],
          lambda o: o == [(b_, 0), (b_, 57), None, None, None, None, None])
    check("refresh: the armed set's only runner is the plain (pre-key) tag → build it at Deploy main's revision",
          lambda: discover(two_tester, armed_b, [{"tags": [b_], "digest": da}], runner_rev=57),
          lambda o: o["need_version"] == b_ and o["refresh"] == "true" and f"{b_}-r57" in o["why"])
    check("refresh: an older keyed runner → build the current revision",
          lambda: discover(two_tester, armed_b, [{"tags": [b_, f"{b_}-r40"], "digest": da}], runner_rev=57),
          lambda o: o["need_version"] == b_ and o["refresh"] == "true")
    check("NEGATIVE CONTROL: the current revision is there → nothing to refresh",
          lambda: discover(two_tester, armed_b, [{"tags": [b_, f"{b_}-r40"], "digest": da}, {"tags": [f"{b_}-r57"], "digest": db},
                                                 {"tags": [a_]}, {"tags": [z]}], runner_rev=57),
          lambda o: o["need_version"] == "" and o["refresh"] == "false")
    check("NEGATIVE CONTROL: a NEWER revision is there (an old commit's tick) → never rebuilt backwards",
          lambda: discover(two_tester, armed_b, [{"tags": [f"{b_}-r60"], "digest": da}, {"tags": [a_]}, {"tags": [z]}], runner_rev=57),
          lambda o: o["need_version"] == "" and o["refresh"] == "false")
    check("NEGATIVE CONTROL: no revision given (a person's dispatch path) → the old rule, nothing to refresh",
          lambda: discover(two_tester, armed_b, [{"tags": [b_]}, {"tags": [a_]}, {"tags": [z]}]),
          lambda o: o["need_version"] == "" and o["refresh"] == "false")
    check("an armed set with NO runner still comes first (and is no refresh)",
          lambda: discover(two_tester, armed_b, [{"tags": [z]}], runner_rev=57),
          lambda o: o["need_version"] == b_ and o["refresh"] == "false")
    check("a refresh comes before a backfill, a backfill after it",
          lambda: [discover(two_tester, two_portal, [{"tags": [z]}, {"tags": [b_]}], runner_rev=57)["need_version"],
                   discover(two_tester, two_portal, [{"tags": [z]}, {"tags": [b_, f"{b_}-r57"]}], runner_rev=57)["need_version"]],
          lambda o: o == [b_, a_])
    check("a keyed runner counts as the set HAVING a runner for the backfill",
          lambda: discover(two_tester, two_portal, [{"tags": [f"{z}-r3"]}, {"tags": [f"{b_}-r57"]}, {"tags": [f"{a_}-r9"]}], runner_rev=57),
          lambda o: o["need_version"] == "" and o["backlog"] == "0")
    rows = [{"tags": [b_, f"{b_}-r40"], "digest": da}, {"tags": [f"{b_}-r57"], "digest": db}, {"tags": [f"{a_}-r70"], "digest": dc}]
    check("choice: the highest revision, every older one named as superseded",
          lambda: runner_choice(rows, b_),
          lambda o: o == {"runner_tag": f"{b_}-r57", "runner_digest": db, "runner_rev": "57", "superseded": da})
    check("choice: a plain-only set hands over the plain runner, superseding nothing",
          lambda: runner_choice([{"tags": [b_], "digest": da}], b_),
          lambda o: o["runner_tag"] == b_ and o["runner_digest"] == da and o["superseded"] == "")
    check("choice: another set's revisions are never this set's",
          lambda: runner_choice(rows, a_), lambda o: o["runner_digest"] == dc and o["superseded"] == "")
    check("choice: the same digest under two revisions is never 'superseded' by itself",
          lambda: runner_choice([{"tags": [b_, f"{b_}-r57"], "digest": db}], b_),
          lambda o: o["runner_digest"] == db and o["superseded"] == "")
    check("choice: no runner → empty, never a guess", lambda: runner_choice(rows, z), lambda o: o["runner_digest"] == "")
    check("choice: refuses a newest runner with no readable digest",
          lambda: runner_choice([{"tags": [f"{b_}-r57"], "digest": ""}], b_), REFUSED)
    check("choice: refuses an older runner with no readable digest (it could not be named as superseded)",
          lambda: runner_choice([{"tags": [b_], "digest": "nope"}, {"tags": [f"{b_}-r57"], "digest": db}], b_), REFUSED)
    check("refuses naming a keyed runner for a non-set", lambda: runner_tag("main", 3), REFUSED)

    # runner-key over REAL git history: commits touching the inputs move the revision, others do not,
    # and a shallow clone (whose ancestor count is its depth) is refused.
    def git(repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                               "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(repo: Path, path: str, text: str) -> str:
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", path)
        return git(repo, "rev-parse", "HEAD")

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "deploy"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        commit(repo, "README.md", "1")                                   # 1
        c2 = commit(repo, ".github/scripts/build-run.py", "runner v1")  # 2 — an input
        commit(repo, "deploy/ocr/x", "unrelated")                        # 3
        check("runner-key: an unrelated commit keeps the inputs' commit and revision",
              lambda: runner_key(repo), lambda o: o == {"inputs_commit": c2, "runner_rev": "2"})
        c4 = commit(repo, "deploy/helm/values.yaml", "chart")             # 4 — an input (the chart is copied in)
        check("runner-key: a chart change moves the revision", lambda: runner_key(repo),
              lambda o: o == {"inputs_commit": c4, "runner_rev": "4"})
        c5 = commit(repo, "deploy/build-host/Dockerfile", "FROM x")      # 5 — an input
        check("runner-key: a Dockerfile change moves it again — monotone along main", lambda: runner_key(repo),
              lambda o: o["inputs_commit"] == c5 and int(o["runner_rev"]) > 4)
        shallow = Path(tmp) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True, capture_output=True)
        check("runner-key: refuses a shallow clone (its count is the depth, not the history)",
              lambda: runner_key(shallow), REFUSED)
        empty = Path(tmp) / "empty"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "main")
        commit(empty, "README.md", "x")
        check("runner-key: refuses a repository no commit of which touches the inputs", lambda: runner_key(empty), REFUSED)

    if fails:
        print("platform-set-dispatch self-test FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("platform-set-dispatch self-test: all cases passed (the contract, every refusal, both events, the backfill, "
          "the runner refresh and its choice, runner-key over real history)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("read")
    r.add_argument("--event", type=Path, required=True)
    r.add_argument("--event-name", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--tester", type=Path, required=True)
    d.add_argument("--portal", type=Path, required=True)
    d.add_argument("--runner", type=Path, required=True)
    d.add_argument("--runner-rev", type=int, default=None,
                   help="Deploy main's runner revision (runner-key) — a newest armed set whose runners all predate it is REFRESHED")
    k = sub.add_parser("runner-key")
    k.add_argument("--repo", type=Path, required=True)
    rc = sub.add_parser("runner-choice")
    rc.add_argument("--runner", type=Path, required=True)
    rc.add_argument("--version", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--run-url", required=True)
    c.add_argument("--run-core-sha", required=True)
    c.add_argument("--set-version", default="")
    c.add_argument("--set-core-sha", default="")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    try:
        if a.cmd == "read":
            out = read_event(json.loads(a.event.read_text()), a.event_name)
        elif a.cmd == "discover":
            out = discover(*(json.loads(p.read_text()) for p in (a.tester, a.portal, a.runner)), runner_rev=a.runner_rev)
        elif a.cmd == "runner-key":
            out = runner_key(a.repo)
        elif a.cmd == "runner-choice":
            out = runner_choice(json.loads(a.runner.read_text()), _version(a.version, "--version"))
        elif a.cmd == "claim":
            out = claim(a.run_url, a.run_core_sha, a.set_version, a.set_core_sha)
        else:
            ap.error("read, discover, claim, runner-key, runner-choice or --self-test")
    except (OSError, ValueError) as e:
        print(f"::error::the platform's sets cannot be read: {e}")
        return 1
    except Refusal as e:
        print(f"::error::the platform's hand-over is refused: {e}")
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
