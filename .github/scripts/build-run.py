#!/usr/bin/env python3
"""build-run.py — the standing build host's runner: claim a run, build and test it in fresh processes.

The HOST half of the standing build hosts (MeshWeaver.Plugins `Hosting/StandingBuildHosts` §2 and
§4.3; platform reference Doc/Architecture/BuildHosts). One warm Deployment per release channel
(`build-latest`, `build-stable`, chart `deploy/helm/templates/build-host/` in Systemorph/MeshWeaver.Deploy) runs this as its main
process:

    build-run.py --host          the loop: refresh the plugins layer → claim → run → repeat
    build-run.py --once          one cycle of that loop, then exit (diagnostics)
    build-run.py --self-test     the parsers, the refresh-before-claim ordering, the stage runner

THE LOOP, and the order that matters:
  1. `GET {api}/hosts/inputs` → the sealed plugins publication the fleet's modules are built from.
     When its digest differs from the one this host has staged, the new publication is pulled
     (ORAS, by DIGEST) into the content-addressed cache `/cache/plugins/<hex>/` BEFORE anything is
     claimed. A host never claims on a plugins layer it has not staged: an unreadable `inputs` or a
     failed pull skips the claim for this cycle (said, then retried) — because `claim` stamps the
     host's `pluginsDigest` on the run, and a run must record exactly what compiled it.
     With MW_REGISTRY_URL set (H8) the layer is the registry's BUNDLE INDEX instead (read with the
     host's mwi_ instance key, a host-only file), keyed by the digest of its sorted
     `package@version[@hash]` list, changed bundles fetched with core's `module-fetch`. The index is
     read when the module HAND-OVER signal moved (`inputs` → `modules.key`: the registry's
     module-published broadcast as the operations instance reconciled it), so an IDLE host installs a
     handed-over module on its next loop turn, ahead of the next build; it is read on every turn while
     there is no signal, and whenever the staged layer is no longer intact on the cache
     (install-on-miss). The installed layer is recorded on the shared cache
     (`<cache>/plugin-sets/current-<identity digest>.json`), so the next host starts with it installed.
  2. `POST {api}/claim` with the host's channel, platform set, framework identity, plugins digest
     or key, and a fresh `claimId` (the SAME one on a network-error retry). 204 = nothing
     claimable → sleep. 409 `stale-set` = this host's set is no longer its lane's → DRAINED: it
     claims nothing and idles, asking again each minute, until SIGTERM (a replica of the new set
     replaces it) or the instance answers its set current again; it never exits on its own, so a
     record change that is not applied yet cannot crash-loop the host (Memex#885). 200 = a run: `{path, repo, sha, refs, isTrunk, publish, stages, labels}` — `path`
     is authoritative; `labels` (the pull request's live labels, which a host has no credential to read) reach every stage
     as `MW_BUILD_LABELS` (comma-separated; left UNSET when the claim carries none, so the stage says it was not told); the base a
     pull-request head was merged onto reaches every stage as `MW_BUILD_MERGE_BASE` (also when the head already contained it and
     git made no merge commit; UNSET on an unmerged run) (a non-latest run's node id is `<sha>-<channel>`). The host then posts the
     PLAN (`{"kind":"plan","stages":[…]}`, host-emitted only) and keeps the claim's lease with a
     batch at least every 45 s (an empty batch is the heartbeat); every stage route's batches are
     numbered (`seq`) and a retried batch keeps its number. No claim survives a host restart.
  3. The HOST checks the run out at `sha` into a FRESH work directory through the shared git
     mirror `<cache>/git/<repo>.git` (cloned `--mirror` on first use, fetched before every
     checkout; the run's one-repository, contents:read installation token stays in the host
     process), and every stage the repository declares in
     `.github/build-stages.json` runs in an ENFORCED SANDBOX: `bwrap` with user, PID, mount, IPC
     and UTS namespaces, `--die-with-parent`, a fresh /proc, `--clearenv` + an explicit allow-list
     (the worker token is a mounted FILE read into the host process and never in any environment),
     the system and every cache bound READ-ONLY, and only the run's checkout, its outputs directory
     and a tmpfs scratch writable. When the stage ends its whole process tree ends with it; the host
     checks no process survives in another pid namespace before it claims again, and EXITS if one
     does. If the node cannot create the namespaces, the host refuses to START, naming why — it
     never runs a stage unsandboxed. It also refuses to start when, from INSIDE a sandbox, the
     instance metadata service, a cluster-private address or the Kubernetes API ANSWERS (the
     boundary self-check): the NetworkPolicy, not the sandbox, is the network boundary. `MW_BUILD_PUBLISH=true|false` (from the claim) is
     informational: a host holds no publication credential and mounts no bake share; publication
     is the publish lane's, on the aggregate check `build`.
  4. The child's output is translated into PROGRESS EVENTS (§7a) and POSTed in batches to
     `{api}/{repo}/{sha}/stages/{stage}/events?channel=<c>`: compiler `file(line,col): error CODE:
     message` → `diagnostic`; test results (vstest `Passed/Failed/Skipped` and xunit `[FAIL]`) →
     `test-finished`; a structured `::mw-event::{json}` line → that event; plus the host's own
     `line`, `stage-finished` and `run-finished`. A stage that runs CONCURRENT LANES tags each lane's
     line `::mw-lane::<lane>::<line>`: every lane keeps its own current suite and pending failure, so
     interleaved lanes are never attributed to one another (Deploy#47); an untagged line is the
     stage's own. Everything else the child prints stays in a local
     tail and is forwarded ONLY when the stage failed with nothing else to explain it ("CI logs
     warn/error + verdicts only"). A stage writes outputs to `$MW_BUILD_OUTPUTS/<content key>/`
     with a `.cache-key.json`; the HOST promotes them into the shared cache (`<cache>/outputs/<key>/`)
     ONLY for a succeeded BUILD stage of a trunk run, and ONLY when it recomputes the key itself
     (module: core's module-build-key.py; NodeType: sources + reference set + compile-check options
     + compiler). Never a test verdict; never a pull request's output. A stage needing PostgreSQL
     starts a COPY of the image's PREPARED cluster inside its own sandbox (host-postgres.sh: UTF8, a
     pgvector template every suite database is cloned from; MW_TEST_PG_SNAPSHOT, MW_TEST_PG_SERVER) —
     never an initdb, never a container. The prepared host's other facts — core's source at the set's
     commit, the portal chart, the tester /app — are handed when the image carries them (prepared_env:
     MW_PLATFORM_SOURCE, MW_DEPLOY_SOURCE, MW_BUILD_TESTER_APP).
  5. 409 on an event POST = the run is claimed by another host: the child is killed, nothing more
     is posted for that run.

`.github/build-stages.json` (declared by each repository; validated here):
    {"stages": [{"name": "compile-check", "kind": "build", "command": "python3 …",
                 "timeoutMinutes": 40}, …]}
  * `name` is ONE path segment ([a-z0-9][a-z0-9._-]*); its Check Run is `build/<name>`
    on latest and `build-<channel>/<name>` otherwise; the run's AGGREGATE check is `build` /
    `build-<channel>` (no slash), so no stage — not even one named `verdict` — can publish under it.
  * `kind` is `build` or `test`; `timeoutMinutes` defaults to 45.
  * a PREPARED server, never a prepared run: a command that stages, resolves, pulls or restores per
    run (resolve-platform.py, fetch-refs.py, platform-mount, module-fetch, an image pull, dotnet
    restore, a NuGet/pip/npm/system install) is REFUSED, naming where the step belongs. Every stage
    is bound `<cache>/nuget` read-only and handed it as its NuGet FALLBACK folder (declared
    `needs: ["nuget"]` or not — a build's project.assets.json names it, so a test stage that could
    not see it failed ResolvePackageAssets). The cache is WARMED from the DEFAULT BRANCH, never from
    a run's content: before a run's stages, the host reads the tip's declared
    `"restore": {"command": …, "timeoutMinutes": N}` from its mirror (`git show <tip>:…`), keys it
    (the tip's NuGet-relevant files by blob id + the command + the platform) and, once per key, runs
    the TIP's command on a host-made checkout of the tip into a fresh folder — handed the installed
    platform as a stage is (the reference set, the set's identity) and its OWN writable copy of the
    platform source as MW_PLATFORM_SOURCE, as a prepared input's build is, because a repository whose
    projects reference the platform cannot restore them from a bare checkout — verifies every package
    and promotes it (a failure is said, MWPREP003, and never reds the run). A NuGet package a stage
    still had to restore (the warm cache lacked it) is SAID as a warning (MWPREP001); on a trunk
    build it is verified (the .nupkg's SHA-512 against what NuGet recorded) and promoted too. A pull
    request's own restore — and any package source its tree adds — never writes the shared cache.
    PROVENANCE: a package is promoted (warm restore or trunk build alike) only when its archive is
    byte-for-byte the one its SOURCE FEED publishes: `.nupkg.metadata` → `source` names the feed's v3
    service index, and the host reads the published hash the documented v3 way (service index →
    `RegistrationsBaseUrl` → registration leaf → `catalogEntry` → `packageHash`, SHA512 — over the
    .nupkg as served, a signed package's repository signature included, so it is compared with the
    archive's SHA-512, not the metadata's contentHash). A mismatch REFUSES the package; so does a source
    that cannot be verified (a local folder, not a v3 feed, plain http, no hash published, a 404, a
    timeout — each request bounded, the whole check within a budget): refused by name, never promoted.
  * `prepared` (optional, beside `stages`): inputs built from the repository's OWN sources that are
    not the run's subject (MeshWeaver.Plugins' module-libraries shelf and Razor build extension,
    `MW_BUILD_MODULE_LIBS` / `MW_BUILD_EXTENSIONS`). The host keys each with the repository's own
    `key` command and installs it once per (repository, input, platform, key) under
    `<cache>/prepared/`, stamped `.source-key`; a run whose key is installed prepares nothing. What
    is installed is only ever built from DEFAULT-BRANCH content (`build`, against the image's
    platform source `MW_PLATFORM_SOURCE`): a trunk run's own tree, or — for ANY run whose key is not
    installed, trunk runs never reaching the host included — the default branch's tip, which the
    host checks out from its mirror and keys and builds with the TIP's own commands. A pull request
    whose key equals the tip's installs that build and reads it, so the server is prepared for a new
    platform or a new main key by the first run that needs it. A pull request with other sources
    builds it for that run only (MWPREP002) and never installs.
  * FAN-OUT (`"parallel": true` beside `stages`, and the host's `MW_BUILD_STAGE_SLOTS` > 1): a run's
    independent stages run CONCURRENTLY, up to the host's slots — core's 8 test shards and doc-gate at once
    after its build, as Actions' matrix runs them. A stage starts once every stage it runs `after` finished
    green; a `build` stage runs ALONE (nothing else in flight, nothing declared after it starts while it waits),
    because it writes the shared $MW_BUILD_OUTPUTS and the host promotes outputs and NuGet packages after it.
    Each concurrent stage keeps its own sandbox, /tmp, scratch, PostgreSQL socket and event route, and hands over
    its OWN `$MW_BUILD_ARTIFACTS` (`<run>/artifacts/<stage>/`), so a sibling can neither see nor contribute to
    another's receipts; a red stage's directory is quarantined whole, AFTER the host handed a COPY of everything the
    red stage added or rewrote over too, before its failed verdict, every receipt marked `"stageState": "failed"`
    (red_stage_artifacts: a shard's .trx files, markers and logs, the dumps — the evidence of WHY it is red;
    MeshWeaver.Deploy#52, built on the diagnostics-only hand-over of MeshWeaver.Plugins#4499), serial or fanned out
    alike. A green stage's receipts are marked `"succeeded"`. A receipt never means "the stage succeeded" by itself:
    every reader reads the marker (MeshWeaver.Plugins' build API serves it; core's build-artifacts-fetch.py publishes
    and ratchets only `succeeded`, host-shard-results.py consolidates a red shard on purpose). The declared budget is the schedule
    (`planned_seconds`: the critical path through the slots — and the scheduler is PINNED to that all-caps schedule,
    `cap_schedule`, so a stage finishing early can never push a later one past it), the run deadline still ends every stage not started,
    a 409 on any stage's route kills every sibling in flight and abandons the run, and the pid-namespace leftover
    check runs whenever no stage is in flight. Without both declarations the run is serial, exactly as below.
  * stages run in declaration order, and a stage is skipped ONLY when a stage it runs AFTER failed:
    its `after` (names of stages declared earlier), or — when it declares none — every earlier
    stage of kind `build` (what produces the outputs later stages read). A skipped stage is finished
    `failed` with "not run — stage <x>, which it runs after, failed first" — never left open, never
    silently skipped — and it counts as failed for ITS dependants. An independent sibling of a red
    stage still runs, so one red test shard never hides the others. A prepared input that cannot be
    provided, or the run deadline passing, ends every remaining stage "not run". The run is red if
    ANY stage is. When the claim names `stages`, only those run (a dependency the claim did not
    select is not run here and blocks nothing), and a claimed stage the repository does not declare
    is RED.

Environment (rendered by the chart from `buildHosts.*`):
  BUILD_API                    https://<operations instance>/api/builds              required
  BUILD_TOKEN_FILE             the mounted worker token (Hosting:Builds:WorkerToken)  default /run/build-worker/token
                               (BUILD_TOKEN in the environment is REFUSED)
  MW_BUILD_CHANNEL             latest | stable                     required unless MW_BUILD_VERSION
  MW_BUILD_VERSION             a VERSIONED build server's version (Doc/Architecture/DeployTestSwitchLatest §3):
                               it claims, reports and scales by version, never by channel; exactly one of
                               the two is set, and it must equal MW_BUILD_PLATFORM_VERSION
  MW_BUILD_PLATFORM_VERSION    the set this host runs (3.x.y-ci.N / 3.x.N)            required
  MW_BUILD_FRAMEWORK_IDENTITY  or MW_BUILD_IDENTITY_FILE (first token of the file)    required
  MW_BUILD_HOST                the claiming host's name (default: hostname)
  MW_BUILD_CACHE               the shared cache volume                    default /cache
  MW_BUILD_WORK                the per-run work root                       default /work
  MW_BUILD_READY_FILE          written once the host is staged (readiness) default /tmp/build-host-ready
  MW_BUILD_COMPILE_REFS        the compile-check's reference set, prepared once per roll: the portal's
                               /app + shared-frameworks/ (CI's refs/ shape); handed to stages; the host
                               refuses to START when it carries no System.Private.CoreLib.dll or Rx
                               OUTSIDE sdk-generators/ and analyzers/ (core's compile-check excludes those),
                               or when it overlaps the stages' writable /scratch/{home,tmp,nuget}
  MW_BUILD_GIT_BASE            clone base                                  default https://github.com/Systemorph
  MW_BUILD_IDLE_SECONDS        sleep after a 204                           default 15
  MW_BUILD_RUN_DEADLINE_SECONDS  the whole run's deadline (= the pod's grace) default 3600
  MW_BUILD_SLICE_CACHE_MAX_GIB  the bound of <cache>/module-slices, GiB; least-recently-used slices above it are
                               evicted (a cache of control's append-only module store)   default 32
  MW_BUILD_STAGE_SLOTS         how many of a `parallel` run's stages run at once  default 1 (serial); the host
                               refuses to START when its cgroup cannot give each slot 1 CPU and 2 GiB, or
                               when neither the cgroup v2 nor the v1 limits can be read (more than 1 slot)
  BWRAP                        the bubblewrap binary                       default bwrap
  MW_PLUGINS_REGISTRY          registry host the plugins publication is pulled from
  MW_PLUGINS_SOURCE            the publication's source name               default plugins
  AZURE_FEDERATED_TOKEN_FILE   (projected by Azure workload identity) — the hosts' ONE credential,
  AZURE_CLIENT_ID, _TENANT_ID   AcrPull only, read in the host and never bound into a sandbox;
                               absent ⇒ the publication is pulled anonymously
  ORAS                         the oras binary                             default oras
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import concurrent.futures
import gzip
import json
import os
import queue
import random
import re
import select
import shutil
import signal
import socket
import unicodedata
import ssl
import stat
import struct
import zlib
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import hashlib
import http.client
import io
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Iterable, Optional, Sequence

STAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CHANNEL = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9._-]+$")
KINDS = {"build", "test"}
DEFAULT_STAGE_TIMEOUT_MINUTES = 45
MAX_LINE = 2000
BATCH_EVENTS = 200
BATCH_SECONDS = 2.0
MAX_DIAGNOSTICS_PER_STAGE = 500
TAIL_LINES = 60
# Event kinds a CHILD may emit with `::mw-event::`. The verdicts (stage-finished, run-finished)
# are the HOST's: a child that could declare its own stage green would make the exit code moot.
CHILD_KINDS = {"unit-started", "unit-finished", "diagnostic", "test-finished", "count", "line"}
SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|APIKEY)", re.I)


# ───────────────────────────────── output parsers ─────────────────────────────────────────

# Roslyn / MSBuild: `path/File.cs(41,13): error CS0246: The type … [/work/x/Proj.csproj]`
MSBUILD = re.compile(
    r"^\s*(?P<file>[^\s(][^(]*?)\((?P<line>\d+)(?:,(?P<col>\d+))?(?:,\d+,\d+)?\)\s*:\s*"
    r"(?P<sev>error|warning)\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<msg>.*?)(?:\s+\[[^\]]+\])?\s*$")
# A GitHub workflow command a child already printed: `::error file=a.cs,line=3::CS0246 msg`
WFCMD = re.compile(r"^::(?P<sev>error|warning)(?:\s+(?P<props>[^:]*))?::(?P<msg>.*)$")
CODE_IN_MSG = re.compile(r"^(?P<code>[A-Z]{2,}\d{2,})\b[:\s]*(?P<rest>.*)$")
# vstest console: `  Passed Ns.Class.Test [12 ms]` / `  Failed …` / `  Skipped …`
VSTEST = re.compile(r"^\s*(?P<o>Passed|Failed|Skipped)\s+(?P<test>[^\s\[][^\[]*?)\s*(?:\[(?P<d>[^\]]*)\])?\s*$")
# xunit (v2/v3 console): `    Ns.Class.Test [FAIL]`
XUNIT = re.compile(r"^\s*(?P<test>\S[^\[]*?)\s+\[(?P<o>FAIL|PASS|SKIP)\]\s*$")
SUMMARY = re.compile(r"^\s*((Passed|Failed|Skipped)!\s+-|Total tests:|=== TEST EXECUTION SUMMARY ===|Test Run (Successful|Failed|Aborted))")
TEST_RUN_FOR = re.compile(r"Test run for (?P<dll>\S+?\.dll)")
STACK_FILE = re.compile(r"\bin (?P<file>\S+\.(?:cs|fs|vb|razor)):line (?P<line>\d+)")
MW_EVENT = re.compile(r"^::mw-event::(?P<json>.*)$")
# A PARALLEL LANE's line: `::mw-lane::<lane>::<the lane's own line>`. A stage that runs suites in
# concurrent lanes (MeshWeaver.Plugins scripts/host-portal-hosts.sh) writes whole lines, but the lanes
# INTERLEAVE: one lane's failed test is followed by another lane's `Test run for`, and its error
# message arrives after that. Each lane is therefore parsed with its OWN suite and pending failure
# (OutputParser.LANE_STATE); a line without the tag is the stage's own, parsed as before.
MW_LANE = re.compile(r"^::mw-lane::(?P<lane>[A-Za-z0-9._-]{1,40})::(?P<rest>.*)$")
OUTCOME = {"Passed": "passed", "Failed": "failed", "Skipped": "skipped",
           "PASS": "passed", "FAIL": "failed", "SKIP": "skipped"}


def parse_duration_ms(text: Optional[str]) -> int:
    """`12 ms`, `1 s`, `< 1 ms`, `2m 3s`, `1.5 s` → milliseconds (0 when unreadable)."""
    if not text:
        return 0
    t = text.strip().lstrip("<").strip()
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)\s*(ms|s|m|h)\b", t):
        try:
            v = float(num)
        except ValueError:
            continue
        total += v * {"ms": 1, "s": 1000, "m": 60000, "h": 3600000}[unit]
    return int(total)


class OutputParser:
    """Translates one stage's child output into §7a progress events. Stateful (a failed test's
    message and file follow it on later lines); feed every line, then call finish().

    The per-LANE state (`LANE_STATE`: the current suite and the pending failure) is kept per lane
    (`MW_LANE`), so interleaved lines of concurrent lanes are never attributed across lanes; the
    counters, the diagnostic de-duplication and the tail are the stage's, shared by every lane."""

    LANE_STATE = ("suite", "pending", "pending_state", "pending_lines")

    def __init__(self, workdir: str, stage: str, inside: str = "") -> None:
        self.workdir = os.path.realpath(workdir).rstrip("/") + "/" if workdir else ""
        # where the stage SAW its checkout (SANDBOX_CHECKOUT): a path under it is made relative exactly like one under
        # the host's own path, so a diagnostic names the repository file whichever side printed it
        self.inside = inside.rstrip("/") + "/" if inside else ""
        self.stage = stage
        self.suite = stage
        self.pending: Optional[dict] = None
        self.pending_state = ""        # "", "message", "stack"
        self.pending_lines = 0
        self.lane: Optional[str] = None            # the lane whose state is loaded (None: the stage's own)
        self.parked: dict = {}                     # lane → its saved LANE_STATE while another lane's is loaded
        self.seen_diag: set[tuple] = set()
        self.diag_count = 0
        self.diag_dropped = 0
        self.errors = 0
        self.warnings = 0
        self.tests = {"passed": 0, "failed": 0, "skipped": 0}
        self.tail: list[str] = []

    # -- helpers ---------------------------------------------------------------------------
    def rel(self, path: str) -> str:
        p = path.strip().replace("\\", "/")
        if self.inside and p.startswith(self.inside):
            return p[len(self.inside):]
        if self.workdir and p.startswith(self.workdir):
            return p[len(self.workdir):]
        if self.workdir:
            real = self.workdir.rstrip("/")
            # a path relative to a project dir stays as printed; an absolute one under a symlinked
            # work root is normalised too
            try:
                rp = os.path.realpath(p)
                if rp.startswith(real + "/"):
                    return rp[len(real) + 1:]
            except (OSError, ValueError):
                pass
        return p

    def _diag(self, sev: str, file: str, line: int, code: str, message: str) -> list[dict]:
        key = (sev, file, line, code, message)
        if key in self.seen_diag:
            return []           # MSBuild repeats every diagnostic in its closing summary
        self.seen_diag.add(key)
        if sev == "error":
            self.errors += 1
        else:
            self.warnings += 1
        if self.diag_count >= MAX_DIAGNOSTICS_PER_STAGE:
            self.diag_dropped += 1
            return []
        self.diag_count += 1
        return [{"kind": "diagnostic", "severity": sev, "file": file, "line": line,
                 "code": code, "message": message[:MAX_LINE]}]

    def _flush_pending(self) -> list[dict]:
        if self.pending is None:
            return []
        ev, self.pending = self.pending, None
        self.pending_state = ""
        self.pending_lines = 0
        return [ev]

    def _test(self, outcome: str, test: str, dur: Optional[str]) -> list[dict]:
        out = self._flush_pending()
        outcome = OUTCOME[outcome]
        self.tests[outcome] += 1
        test = test.strip()
        suite = self.suite if self.suite != self.stage else (test.rsplit(".", 1)[0] if "." in test else self.stage)
        ev = {"kind": "test-finished", "suite": suite, "test": test, "outcome": outcome,
              "durationMs": parse_duration_ms(dur), "message": "", "file": "", "line": 0}
        if outcome == "failed":
            self.pending = ev           # its message and source line follow
        else:
            out.append(ev)
        return out

    def _child_event(self, raw: str) -> list[dict]:
        """A stage's own `::mw-event::{json}` — parsed TOTALLY: a malformed one is dropped with a
        line saying so, never an exception. Only the kinds in CHILD_KINDS are accepted, each field
        coerced to its type; `stage-finished` / `run-finished` are the HOST's alone (decided from
        the sandbox's exit status), so a child naming them is dropped and said."""
        def dropped(why: str) -> list[dict]:
            return [{"kind": "line", "text": f"malformed ::mw-event:: dropped ({why})"}]
        try:
            ev = json.loads(raw)
        except ValueError:
            return dropped("not JSON")
        if not isinstance(ev, dict):
            return dropped("not an object")
        kind = ev.get("kind")
        if kind in ("stage-finished", "run-finished", "plan"):
            return [{"kind": "line", "text": f"a stage tried to emit `{kind}` — refused: only the host decides a verdict"}]
        if kind not in CHILD_KINDS:
            return dropped(f"unknown kind {str(kind)[:40]!r}")

        def text(k: str, limit: int = MAX_LINE) -> str:
            v = ev.get(k)
            return v[:limit] if isinstance(v, str) else ""

        def num(k: str) -> int:
            v = ev.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 2**31:
                raise ValueError(k)
            return int(v)

        def opt_num(k: str) -> int:
            return num(k) if ev.get(k) is not None else 0
        try:
            if kind == "diagnostic":
                sev = text("severity") or "error"
                if sev not in ("error", "warning"):
                    return dropped(f"severity {sev!r}")
                return self._diag(sev, self.rel(text("file")), opt_num("line"), text("code", 40), text("message"))
            if kind == "test-finished":
                outcome = text("outcome")
                if outcome not in self.tests:
                    return dropped(f"outcome {outcome!r}")
                self.tests[outcome] += 1
                return [{"kind": kind, "suite": text("suite", 300) or self.stage, "test": text("test", 500),
                         "outcome": outcome, "durationMs": opt_num("durationMs"), "message": text("message"),
                         "file": self.rel(text("file")), "line": opt_num("line")}]
            if kind == "unit-started":
                return [{"kind": kind, "unit": text("unit", 300)}] if text("unit") else dropped("no unit")
            if kind == "unit-finished":
                outcome = text("outcome")
                if not text("unit") or outcome not in ("compiled", "cached", "failed"):
                    return dropped("unit/outcome")
                return [{"kind": kind, "unit": text("unit", 300), "outcome": outcome, "durationMs": opt_num("durationMs")}]
            if kind == "count":
                return [{"kind": kind, "unitsTotal": opt_num("unitsTotal"), "testsTotal": opt_num("testsTotal")}]
            return [{"kind": "line", "text": text("text")}] if text("text") else dropped("no text")
        except ValueError as e:
            return dropped(f"field {e}")

    # -- the feed --------------------------------------------------------------------------
    def feed(self, raw: str) -> list[dict]:
        """Never raises: a parser fault on one line is reported as a line, and the stage goes on."""
        try:
            return self._feed(raw)
        except Exception as e:      # the parser is total by contract; this is its seatbelt
            return [{"kind": "line", "text": f"output line not parsed ({type(e).__name__})"}]

    def _switch_lane(self, lane: Optional[str]) -> None:
        """Load `lane`'s state, parking the current one — WITHOUT flushing its pending failure, whose
        message lines may still arrive after another lane's lines."""
        if lane == self.lane:
            return
        self.parked[self.lane] = {k: getattr(self, k) for k in self.LANE_STATE}
        state = self.parked.pop(lane, None) or {"suite": self.stage, "pending": None, "pending_state": "", "pending_lines": 0}
        for k, v in state.items():
            setattr(self, k, v)
        self.lane = lane

    def _feed(self, raw: str) -> list[dict]:
        line = raw.rstrip("\r\n")
        self.tail.append(line[:MAX_LINE])
        if len(self.tail) > TAIL_LINES:
            del self.tail[0]

        m = MW_LANE.match(line)
        self._switch_lane(m.group("lane") if m else None)
        if m:
            line = m.group("rest")

        m = MW_EVENT.match(line)
        if m:
            return self._child_event(m.group("json"))

        m = TEST_RUN_FOR.search(line)
        if m:
            out = self._flush_pending()
            self.suite = os.path.basename(m.group("dll"))[:-4]
            return out

        m = VSTEST.match(line)
        if m and not SUMMARY.match(line):
            return self._test(m.group("o"), m.group("test"), m.group("d"))
        m = XUNIT.match(line)
        if m:
            return self._test(m.group("o"), m.group("test"), None)

        if self.pending is not None:
            if SUMMARY.match(line):
                return self._flush_pending()
            self.pending_lines += 1
            stripped = line.strip()
            if stripped.startswith("Error Message:"):
                self.pending_state = "message"
            elif stripped.startswith("Stack Trace:"):
                self.pending_state = "stack"
            elif stripped:
                if not self.pending["message"] and self.pending_state in ("", "message") \
                        and not stripped.startswith("at "):
                    self.pending["message"] = stripped[:MAX_LINE]
                sm = STACK_FILE.search(line)
                if sm and not self.pending["file"]:
                    f = self.rel(sm.group("file"))
                    if not f.startswith("/"):       # a repository file, not a framework frame
                        self.pending["file"] = f
                        self.pending["line"] = int(sm.group("line"))
            if self.pending_lines > 50:
                return self._flush_pending()
            return []

        m = MSBUILD.match(line)
        if m:
            return self._diag(m.group("sev"), self.rel(m.group("file")), int(m.group("line")),
                              m.group("code"), m.group("msg").strip())
        m = WFCMD.match(line)
        if m:
            props = dict(p.split("=", 1) for p in (m.group("props") or "").split(",") if "=" in p)
            msg = m.group("msg").strip()
            code = ""
            cm = CODE_IN_MSG.match(msg)
            if cm:
                code, msg = cm.group("code"), cm.group("rest").strip()
            try:
                ln = int(props.get("line", "0") or 0)
            except ValueError:
                ln = 0
            return self._diag(m.group("sev"), self.rel(props.get("file", "")), ln, code, msg)
        return []

    def finish(self) -> list[dict]:
        out = self._flush_pending()
        for lane in list(self.parked):             # every lane's last failure, not only the loaded one's
            self._switch_lane(lane)
            out += self._flush_pending()
        if self.diag_dropped:
            out.append({"kind": "line", "text": f"{self.diag_dropped} further diagnostic(s) not forwarded "
                                                f"(cap {MAX_DIAGNOSTICS_PER_STAGE} per stage)"})
        return out

    def summary(self, rc: Optional[int], seconds: float, note: str = "") -> str:
        t = self.tests
        parts = [f"exit {rc}" if rc is not None else "no exit code",
                 f"{self.errors} error(s)", f"{self.warnings} warning(s)"]
        if sum(t.values()):
            parts.append(f"tests {t['passed']} passed, {t['failed']} failed, {t['skipped']} skipped")
        parts.append(_hms(seconds))
        if note:
            parts.append(note)
        return " · ".join(parts)


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s"


# ───────────────────────────────── stage declaration ──────────────────────────────────────

class Refusal(Exception):
    """A run that cannot proceed, with the reason it is red."""


# PREPARED INPUTS (MeshWeaver.Plugins Hosting/StandingBuildHosts §7, "A prepared server"): what a repository's stages
# read that is BUILT FROM ITS OWN SOURCES but is not the run's subject — MeshWeaver.Plugins' module-libraries shelf and
# Razor build extension. Declared beside the stages:
#   "prepared": [{"name": "shelf", "env": "MW_BUILD_MODULE_LIBS",
#                 "key": "python3 scripts/host-in-mesh-modules.py source-key shelf",
#                 "build": "dotnet publish src/…/X.csproj -c Release -o \"$MW_PREPARE_OUT\" -p:MeshWeaverRoot=\"$MW_PLATFORM_SOURCE\"",
#                 "timeoutMinutes": 20}]
# The host computes each input's source key from the run's tree (the repository's OWN `key` command, so the stage's own
# check agrees by construction), and installs it PERSISTENTLY per (repository, input, platform, key) under
# `<cache>/prepared/`. A run whose key is installed reads it, read-only, and prepares nothing. A miss is installed ONCE,
# built only from DEFAULT-BRANCH content: a trunk run builds its own tree; any other run (trunk runs need not reach the
# host at all) has the host check out the default branch's tip from the mirror and key and build THAT with the tip's own
# commands — installed when its key equals the run's. A pull request whose sources differ from the tip's builds it for
# that run only (said, MWPREP002), and never writes the shared copy: no PR-controlled command ever builds installed bytes.
# Only the names below may carry a prepared input: a stage's environment is an allow-list.
PREPARED_ENV = ("MW_BUILD_MODULE_LIBS", "MW_BUILD_EXTENSIONS")
PREPARED_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
SOURCE_KEY = re.compile(r"^[0-9a-f]{64}$")


def validate_prepared(doc: object) -> list[dict]:
    items = doc.get("prepared", []) if isinstance(doc, dict) else []
    if not isinstance(items, list):
        raise Refusal("`prepared` in .github/build-stages.json must be a list")
    out, names, envs = [], set(), set()
    for i, p in enumerate(items):
        if not isinstance(p, dict):
            raise Refusal(f"prepared input #{i} is not an object")
        name, env = str(p.get("name") or ""), str(p.get("env") or "")
        if not PREPARED_NAME.match(name) or name in names:
            raise Refusal(f"prepared input #{i} name {name!r} must be [a-z][a-z0-9-]* and unique")
        if env not in PREPARED_ENV or env in envs:
            raise Refusal(f"prepared input {name!r} env {env!r} must be one of {list(PREPARED_ENV)}, once each — "
                          "a stage's environment is an allow-list")
        for f in ("key", "build"):
            if not isinstance(p.get(f), str) or not p[f].strip():
                raise Refusal(f"prepared input {name!r} has no `{f}` command")
        tmo = p.get("timeoutMinutes", 20)
        if not isinstance(tmo, (int, float)) or not (0 < tmo <= DEFAULT_STAGE_TIMEOUT_MINUTES):
            raise Refusal(f"prepared input {name!r} timeoutMinutes {tmo!r} must be in (0, {DEFAULT_STAGE_TIMEOUT_MINUTES}]")
        names.add(name)
        envs.add(env)
        out.append({"name": name, "env": env, "key": p["key"], "build": p["build"], "timeoutMinutes": float(tmo)})
    return out


def write_marker(directory: str, name: str, text: str) -> None:
    """Write `<directory>/<name>` as a FRESH regular file, never through what is already there: whatever a sandboxed
    build left under that name (a symlink to a host file, a directory) is removed first, and the new file is created
    beside it (mkstemp: O_CREAT|O_EXCL, which never follows a link) and renamed over the entry — so a host-side write cannot be redirected."""
    target = os.path.join(directory, name)
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    fd, tmp = tempfile.mkstemp(prefix=f".{name.lstrip('.')}-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text + "\n")
        os.replace(tmp, target)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def prepared_store(cache: str, repo: str, name: str, platform: str, key: str) -> str:
    """Where an installed prepared input lives: one directory per (repository, input, platform, source key)."""
    return os.path.join(cache, "prepared", repo, name, hashlib.sha256(f"{platform}|{key}".encode()).hexdigest()[:32])


def load_prepared(path: str) -> list[dict]:
    try:
        with open(path) as fh:
            return validate_prepared(json.load(fh))
    except (OSError, ValueError) as e:
        raise Refusal(f".github/build-stages.json is unreadable: {e}")


# THE WARM NUGET CACHE, prepared from the DEFAULT BRANCH (the same trust boundary as the prepared inputs above). Every
# stage reads `<cache>/nuget` read-only as its NuGet FALLBACK folder; what it lacks a stage restores per run (MWPREP001)
# into a folder of its own that is never shared unless the run is a trunk build. Trunk runs need not reach the host at
# all, so the host fills the cache ITSELF, once per (repository, platform, restore key), from the default branch's tip:
#   "restore": {"command": "dotnet restore MeshWeaver.slnx -p:CIRun=true", "timeoutMinutes": 20}
# declared beside the stages and read ONLY from the tip (`git show <tip>:.github/build-stages.json` on the host's
# mirror) — a run's own declaration is never executed, so neither its command nor a package source its tree adds ever
# writes the shared cache. The restore runs in the sandbox on a host-made checkout of the tip — handed what a stage is
# handed of the installed platform (the reference set, the set's identity) and its own WRITABLE copy of the platform
# source as MW_PLATFORM_SOURCE, exactly as a prepared input's build is — writes into a fresh
# host-owned folder, and every package it fetched is verified and rebuilt into the cache by `promote_nuget`. The key is
# the host's own: every NuGet-relevant file of the tip (`NUGET_KEY_FILE`, by blob id) + the command + the platform, so
# a second run on the same tip finds the marker and restores nothing.
NUGET_KEY_FILE = re.compile(r"(?:^|/)(?:[^/]+\.(?:csproj|fsproj|vbproj|props|targets|sln|slnx)|nuget\.config|global\.json"
                            r"|packages\.lock\.json)$", re.I)


def validate_restore(doc: object) -> Optional[dict]:
    """The declared default-branch restore, or None when the repository declares none. Raises Refusal on a malformed one."""
    r = doc.get("restore") if isinstance(doc, dict) else None
    if r is None:
        return None
    if not isinstance(r, dict) or not isinstance(r.get("command"), str) or not r["command"].strip():
        raise Refusal("`restore` in .github/build-stages.json must be {\"command\": \"…\", \"timeoutMinutes\": N}")
    tmo = r.get("timeoutMinutes", 20)
    if not isinstance(tmo, (int, float)) or isinstance(tmo, bool) or not (0 < tmo <= DEFAULT_STAGE_TIMEOUT_MINUTES):
        raise Refusal(f"`restore` timeoutMinutes {tmo!r} must be in (0, {DEFAULT_STAGE_TIMEOUT_MINUTES}]")
    return {"command": r["command"], "timeoutMinutes": float(tmo)}


# ───────────────────────────────── the module-hub broker (Plugins Hosting/BuildQueue.md §7.4, "wiring") ─────────────
#
# Owner, 2026-11-07: "the inner most build job must be locked by a dedicated hub updating the file system and building one
# module at a time". A stage never holds the worker token, so it cannot ask a module's hub itself: the HOST brokers. And a
# stage is never trusted with a module's IDENTITY — a pull request's stage could otherwise store crafted bytes under main's
# fingerprint, which main would then adopt. So:
#   * the module map (`moduleHub.modules`: id → paths, deps; `moduleHub.recipe`: the shared build inputs) is read from the
#     DEFAULT BRANCH tip's .github/build-stages.json, never from the run's tree;
#   * the HOST computes every declared module's fingerprint right after the checkout (and merge), BEFORE any stage runs,
#     from the pristine tree: sha256 over the module's paths' blob ids, the recipe's blob ids, its dependencies'
#     fingerprints (transitively every input — known before anything builds; SEAM(ref-asm): the dependencies' API hashes
#     replace them once the platform publishes its reference-assembly hash) and the toolchain/platform set;
#   * the HOST decides lane and commit: a trunk run asks on main at the run's sha with its merge order (first-parent count
#     from the mirror); any other run on the pull-request lane — so a pull request can never write main's sequence;
#   * the stage only NAMES a module (`$MW_BUILD_MODULE_HUB/req/<module>.json`) and is answered (`resp/<module>.json`):
#     `Hit` with the module's output extracted under `outputs/<module>/`, or `Grant` — then it builds the module into
#     `build/<module>/` and says `done/<module>.json`; the host tars that directory deterministically, uploads it by its
#     sha256, completes the step and answers `ack/<module>.json`. `Refused`/`Failed` name why.
MODULE_HUB_POLL = 0.25
MODULE_HUB_RETRY = 2.0
MODULE_HUB_WAIT = 45 * 60
MODULE_HUB_LEASE_MARGIN = 30      # seconds: hand nothing over this close to the lease's end (clock skew, the upload's own time)


def pull_request_number(refs: list) -> Optional[int]:
    """The pull request a claim's refs name: a `{kind: PullRequest, number}` object — the operations instance's claim answer
    serialises the kind as its NUMBER (BuildRefKind.PullRequest = 1; System.Text.Json's web defaults write enums as numbers),
    other writers as the name — or a `refs/pull/<n>/head|merge` string (every form reaches the host). Pure."""
    for r in refs:
        kind = r.get("kind") if isinstance(r, dict) else None
        is_pr = (kind == 1 and not isinstance(kind, bool)) or str(kind).strip().lower() in ("pullrequest", "pull_request", "1")
        if isinstance(r, dict) and is_pr and isinstance(r.get("number"), int) and not isinstance(r.get("number"), bool):
            return r["number"]
        if isinstance(r, str):
            m = re.fullmatch(r"refs/pull/(\d+)/(?:head|merge)", r.strip())
            if m:
                return int(m.group(1))
    return None


def module_hub_enabled(env: dict) -> bool:
    return (env.get("MW_BUILD_MODULE_HUB_ENABLED") or "").strip().lower() in ("1", "true", "yes")


def validate_module_hub(doc: object) -> Optional[dict]:
    """`"moduleHub": {"modules": {"<id>": {"paths": [...], "deps": [...]}}, "recipe": [...]}` beside `stages` (the DEFAULT
    BRANCH tip's). Absent → None. Malformed (unknown dep, a cycle, an unsafe id or path) → Refusal."""
    if not isinstance(doc, dict) or "moduleHub" not in doc:
        return None
    mh = doc["moduleHub"]
    mods = mh.get("modules") if isinstance(mh, dict) else None
    if not isinstance(mods, dict) or not mods:
        raise Refusal("`moduleHub.modules` must map module ids to {paths, deps}")
    safe = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    out: dict = {}
    for mid, m in mods.items():
        if not safe.match(str(mid)) or mid in (".", ".."):
            raise Refusal(f"`moduleHub` module id {mid!r} is not one safe path segment")
        paths = m.get("paths") if isinstance(m, dict) else None
        deps = m.get("deps", []) if isinstance(m, dict) else None
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p and not p.startswith("/") and ".." not in p.split("/") for p in paths):
            raise Refusal(f"`moduleHub.modules.{mid}.paths` must be relative paths")
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise Refusal(f"`moduleHub.modules.{mid}.deps` must list module ids")
        options = m.get("options", []) if isinstance(m, dict) else []
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            raise Refusal(f"`moduleHub.modules.{mid}.options` must list strings (the build options that shape the compile)")
        out[mid] = {"paths": sorted(set(paths)), "deps": sorted(set(deps)), "options": sorted(set(options))}
    for mid, m in out.items():
        for d in m["deps"]:
            if d not in out:
                raise Refusal(f"`moduleHub.modules.{mid}` depends on {d!r}, which is not declared")
    recipe = mh.get("recipe", [])
    if not isinstance(recipe, list) or not all(isinstance(p, str) and p and not p.startswith("/") and ".." not in p.split("/") for p in recipe):
        raise Refusal("`moduleHub.recipe` must list relative paths")
    module_order(out)       # refuses a cycle
    return {"modules": out, "recipe": sorted(set(recipe))}


def widen_module_hub(trusted: dict, candidate: Optional[dict]) -> dict:
    """The TRUSTED declaration widened by the run's own: per trusted module, its paths, recipe and options UNIONED with what
    the candidate tree declares for it (a pull request that adds a dependency lists its directory — CI's
    gen-module-hub --check holds the candidate to its projects). A union can only ADD inputs to a key — a candidate can
    never remove a trusted path, rename a module or add one — so a lying candidate makes keys stricter, never looser. Pure."""
    if not candidate:
        return trusted
    mods = {}
    for mid, spec in trusted["modules"].items():
        c = candidate["modules"].get(mid) or {}
        # options are NOT unioned (a union would hide a REMOVED option): the trusted set and the candidate's set both enter
        # the key, so any change to the candidate's options — an addition or a removal — moves it
        mods[mid] = {"paths": sorted(set(spec["paths"]) | set(c.get("paths", []))), "deps": spec["deps"],
                     "options": spec.get("options", []), "candidateOptions": sorted(set(c.get("options", [])))}
    return {"modules": mods, "recipe": sorted(set(trusted["recipe"]) | set(candidate.get("recipe", [])))}


def module_order(mods: dict) -> list:
    """The declared modules leaves first (dependencies before dependents); a cycle is a Refusal. Pure."""
    order, state = [], {}

    def visit(m: str, trail: tuple) -> None:
        if state.get(m) == 2:
            return
        if state.get(m) == 1:
            raise Refusal(f"`moduleHub` declares a dependency cycle: {' → '.join(trail + (m,))}")
        state[m] = 1
        for d in mods[m]["deps"]:
            visit(d, trail + (m,))
        state[m] = 2
        order.append(m)
    for m in sorted(mods):
        visit(m, ())
    return order


def module_fingerprints(decl: dict, ls_tree: Callable[[list], str], toolchain: str) -> dict:
    """Every declared module's input fingerprint, from the PRISTINE tree (`ls_tree(paths)` → `git ls-tree -r HEAD -- paths`
    output: mode, type, blob id and path per line). Dependencies' fingerprints are inputs, so the key is known for every
    module before anything builds. Pure apart from `ls_tree`."""
    recipe = ls_tree(decl["recipe"]) if decl["recipe"] else ""
    fps: dict = {}
    for m in module_order(decl["modules"]):
        spec = decl["modules"][m]
        rows = [f"module={m}", f"toolchain={toolchain}", *[f"option:{o}" for o in spec.get("options", [])],
                *[f"candidate-option:{o}" for o in spec.get("candidateOptions", [])],
                "sources:", ls_tree(spec["paths"]).strip(), "recipe:", recipe.strip()]
        rows += [f"dep:{d}={fps[d]}" for d in spec["deps"]]          # SEAM(ref-asm): the dependency's API hash, once published
        fps[m] = "sha256:" + hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return fps


MODULE_OUTPUT_GZIP_LEVEL = 6


def deterministic_tar(src_dir: str, dest_file: str, compress: bool = False) -> str:
    """Pack `src_dir` so equal content gives equal bytes (sorted entries, mtime 0, no owners, no links followed); return
    the file's sha256 (hex). `compress`: gzip it, deterministically too (a fixed level, header mtime 0 and no file name —
    tarfile's own "w:gz" writes the clock and the name). A module output is handed over compressed: uncompressed, the
    larger ones (MeshWeaver.AI and the modules that carry it, 81–94 MiB and more, measured 2026-11-08) crossed the build
    API's ingress body limit (100 MiB) and were refused with a bare 413, so they were never retained for reuse."""
    import tarfile
    import gzip
    entries = []
    for root, dirs, files in os.walk(src_dir):
        dirs.sort()
        for n in sorted(files):
            entries.append(os.path.join(root, n))
    with open(dest_file, "wb") as raw:
        stream = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=MODULE_OUTPUT_GZIP_LEVEL) if compress else raw
        try:
            with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for full in entries:
                    rel = os.path.relpath(full, src_dir)
                    if os.path.islink(full):
                        raise Refusal(f"the module output carries a symbolic link ({rel}) — not handed over")
                    ti = tarfile.TarInfo(rel)
                    ti.size = os.path.getsize(full)
                    ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
                    ti.mode = 0o755 if os.access(full, os.X_OK) else 0o644
                    with open(full, "rb") as fh:
                        tar.addfile(ti, fh)
        finally:
            if compress:
                stream.close()
    return _sha256_file(dest_file)


def safe_extract(tar_file: str, dest: str) -> None:
    """Extract a module output, refusing anything but regular files and directories inside `dest`."""
    import tarfile
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    with tarfile.open(tar_file) as tar:
        for ti in tar.getmembers():
            target = os.path.realpath(os.path.join(root, ti.name))
            if not (ti.isfile() or ti.isdir()) or not (target == root or target.startswith(root + os.sep)):
                raise Refusal(f"the module output carries an unsafe entry ({ti.name!r})")
        tar.extractall(root, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(root)


# ───────────── the host's slice cache is BOUNDED: a size cap and least-recently-used eviction (Plugins OneBuildTree §5) ─────
# `<cache>/module-slices/<repo>/<module>/<sha256>/` held every output any run ever handed over or fetched, and nothing ever
# removed one: 117 GB on eiger on 2026-11-10, the node at DiskPressure and 18 build pods evicted. The slices are a CACHE of
# the control instance's content-addressed store (append-only, never pruned): an evicted slice is fetched again by its hash
# on its next Hit, so eviction loses no version. Bound: MW_BUILD_SLICE_CACHE_MAX_GIB (chart buildHosts.sliceCacheMaxGi).
# Each entry records its size (`.bytes`) and its last use (the mtime of `.complete`, touched on every Hit); a reader holds
# a SHARED flock on the entry's sibling lock (`<sha256>.lock`) while it copies, and the evictor takes it EXCLUSIVE and
# non-blocking, so an entry being read is never removed under its reader.
# The node's disk is shared by every replica's work volume, the cache and the images; the chart's per-replica work limit
# times the replicas can exceed it (moench 2026-11-10: 3 × 52 GiB of work on a 98 GB disk, 17 pods evicted for
# DiskPressure at 59 % used at rest). So a host claims a run only when the disk keeps the eviction headroom free AFTER one
# run's footprint — measured on the node at claim time, never assumed from the chart.
RUN_DISK_ENV = "MW_BUILD_RUN_DISK_GIB"
# The defaults are what the chart renders (buildHosts.runDiskGi / diskHeadroomPercent), so a host whose chart copy predates
# the values (a leased Mac's follower re-applies only the set) is guarded all the same; 15 % is above the kubelet's
# default hard threshold, nodefs.available < 10 %. 0 / 0 turns the guard off.
RUN_DISK_DEFAULT_GIB = 20.0
DISK_HEADROOM_ENV = "MW_BUILD_DISK_HEADROOM_PERCENT"
DISK_HEADROOM_DEFAULT_PERCENT = 15.0


def disk_gib(text: Optional[str], default: float) -> int:
    """A GiB value in [0, 2^20] as bytes; anything else the default. Pure."""
    try:
        v = float((text or "").strip() or default)
    except ValueError:
        v = default
    return int((v if 0 <= v <= 1 << 20 else default) * (1 << 30))


def disk_percent(text: Optional[str], default: float) -> float:
    """A percentage in [0, 90]; anything else the default. Pure."""
    try:
        v = float((text or "").strip() or default)
    except ValueError:
        v = default
    return v if 0 <= v <= 90 else default


def _disk_free(path: str) -> "tuple[int, int]":
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


SLICE_CACHE_MAX_ENV = "MW_BUILD_SLICE_CACHE_MAX_GIB"
SLICE_CACHE_DEFAULT_GIB = 32.0
SLICE_CACHE_MIN_AGE = 600.0          # seconds: an entry used this recently is never evicted (a run is about to read it)
SLICE_CACHE_LOW_WATER = 0.9          # evict down to this fraction of the cap, so the next hand-over does not evict again
SLICE_STAGING_MAX_AGE = 86400.0      # a staging directory a killed process left behind is removed after a day


def slice_cache_cap(env: dict) -> int:
    """The slice cache's bound in bytes: MW_BUILD_SLICE_CACHE_MAX_GIB (a positive number), else the default. A value that
    is not a positive number is the DEFAULT, never unbounded. Pure."""
    try:
        gib = float((env.get(SLICE_CACHE_MAX_ENV) or "").strip() or SLICE_CACHE_DEFAULT_GIB)
    except ValueError:
        gib = SLICE_CACHE_DEFAULT_GIB
    if not 0 < gib <= 1 << 20:                                 # NaN, zero, negative and infinity alike: the default
        gib = SLICE_CACHE_DEFAULT_GIB
    return int(gib * (1 << 30))


def _tree_bytes(path: str) -> int:
    total = 0
    for d, _ds, fs in os.walk(path, onerror=lambda e: None):
        for f in fs:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return total


def slice_entry_bytes(entry: str) -> int:
    """An entry's size: its recorded `.bytes`, else measured once and recorded. Never raises."""
    try:
        with open(os.path.join(entry, ".bytes")) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        pass
    n = _tree_bytes(entry)
    try:
        with open(os.path.join(entry, ".bytes"), "w") as fh:
            fh.write(str(n))
    except OSError:
        pass
    return n


def slice_cache_entries(root: str) -> list:
    """Every COMPLETE entry under `root` as (last use, bytes, entry dir, lock path), oldest use first. Never raises."""
    out = []
    for repo in _listdir(root):
        if repo.startswith("."):
            continue
        for module in _listdir(os.path.join(root, repo)):
            mdir = os.path.join(root, repo, module)
            for name in _listdir(mdir):
                entry = os.path.join(mdir, name)
                if not re.fullmatch(r"[0-9a-f]{64}", name) or os.path.islink(entry):
                    continue
                try:
                    used = os.stat(os.path.join(entry, ".complete")).st_mtime
                except OSError:
                    continue                                   # not complete: a put still renaming, or a broken entry
                out.append((used, slice_entry_bytes(entry), entry, entry + ".lock"))
    out.sort()
    return out


def _listdir(path: str) -> list:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def bound_slice_cache(root: str, cap: int, log: Callable[[str], None], now: Optional[float] = None,
                      min_age: float = SLICE_CACHE_MIN_AGE) -> "tuple[int, int]":
    """Keep `root` (the host's slice cache) at or under `cap` bytes: when it is over, evict the least-recently-used complete
    entries — none used in the last `min_age` seconds, none whose lock a reader holds — down to SLICE_CACHE_LOW_WATER of the
    cap; and remove staging directories older than a day. One evictor at a time per cache (`.bound.lock`, non-blocking: a
    second one skips). An entry is renamed out of the tree under its exclusive lock, then deleted. Returns (entries evicted,
    bytes held afterwards). Never raises."""
    import fcntl
    now = time.time() if now is None else now
    if not os.path.isdir(root):
        return 0, 0
    guard = _try_lock(os.path.join(root, ".bound.lock"), fcntl.LOCK_EX | fcntl.LOCK_NB)
    if guard is None:
        return 0, -1                                           # another evictor is at work on this cache
    try:
        for repo in _listdir(root):
            for module in ([] if repo.startswith(".") else _listdir(os.path.join(root, repo))):
                for name in _listdir(os.path.join(root, repo, module)):
                    if ".staging-" in name:
                        stg = os.path.join(root, repo, module, name)
                        try:
                            if now - os.lstat(stg).st_mtime > SLICE_STAGING_MAX_AGE:
                                _rm_tree(stg)
                        except OSError:
                            pass
        for name in _listdir(root):
            if name.startswith(".evicted-"):
                _rm_tree(os.path.join(root, name))             # an eviction a killed process did not finish
        entries = slice_cache_entries(root)
        total = sum(e[1] for e in entries)
        if total <= cap:
            return 0, total
        target = int(cap * SLICE_CACHE_LOW_WATER)
        before, evicted, freed = total, 0, 0
        for used, size, entry, lock in entries:
            if total <= target:
                break
            if now - used < min_age:
                continue
            fh = _try_lock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if fh is None:
                continue                                       # a reader is copying it: kept, the next one is tried
            try:
                gone = os.path.join(root, f".evicted-{os.getpid()}-{threading.get_ident()}-{evicted}")
                try:
                    os.rename(entry, gone)
                except OSError:
                    continue
                try:
                    os.remove(lock)
                except OSError:
                    pass
            finally:
                fh.close()
            _rm_tree(gone)
            total -= size
            freed += size
            evicted += 1
        log(f"slice cache: {len(entries)} entries, {before / (1 << 30):.1f} GiB over the bound of {cap / (1 << 30):.1f} GiB "
            f"({SLICE_CACHE_MAX_ENV}) — evicted the {evicted} least recently used ({freed / (1 << 30):.1f} GiB); "
            f"{total / (1 << 30):.1f} GiB held{'' if total <= cap else ' — STILL OVER: every remaining entry is in use or younger than ' + str(int(min_age)) + ' s'}")
        return evicted, total
    finally:
        guard.close()


class ModuleHubBroker:
    """The host side of a stage's module-hub requests. Identity — fingerprint, lane, commit, order, holder — is the HOST's;
    the stage only names modules and hands over the output directory the broker assigned.

    🚨 The stage is NOT trusted with the filesystem either. Three directories:
      * `io`  — bound WRITABLE into the stage: `req/<m>.json`, `done/<m>.json`, `build/<m>/`. The host never follows a path
        through it: it holds directory descriptors opened BEFORE any stage ran, opens every entry with O_NOFOLLOW and accepts
        regular files only, and copies an output into host-private staging before it packs it;
      * `out` — bound READ-ONLY into the stage: `plan.json`, `resp/`, `ack/`, `outputs/<m>/` — written by the host alone;
      * `host` — never bound: downloads, tars, staging. Nothing a stage can reach is ever read back as trusted.
    """

    def __init__(self, api: "Api", repo: str, sha: str, host: str, root: str, fingerprints: dict, trunk: bool,
                 order: int, pull_request: Optional[int], log: Callable[[str], None],
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 slice_cache: Optional[str] = None, build_tag: Optional[dict] = None) -> None:
        self.api, self.repo, self.sha, self.host, self.root = api, repo, sha, host, root
        # what the outputs were built AGAINST (the platform set, the framework identity), sent with every completion so the
        # store's entry is TAGGED with it — its key stays the content hash (Plugins OneBuildTree §3)
        self.build_tag = {k: v for k, v in (build_tag or {}).items() if isinstance(v, str) and v}
        self.fps, self.trunk, self.order, self.pr, self.log = fingerprints, trunk, order, pull_request, log
        self.sleep, self.clock = sleep, clock
        self.utcnow = lambda: datetime.datetime.now(datetime.timezone.utc)
        self.slice_cache = slice_cache
        self.io, self.out, self.priv = (os.path.join(root, d) for d in ("io", "out", "host"))
        for d in ("io/req", "io/done", "io/build", "out/resp", "out/ack", "out/outputs", "host"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        # descriptors held from BEFORE any stage ran: a stage replacing a path with a link later redirects nothing
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self.fd = {k: os.open(os.path.join(self.io, k), flags) for k in ("req", "done", "build")}
        self.granted: dict = {}        # module → the step seq the hub granted THIS host; removed only once the server ACKED
        self.lease_until: dict = {}    # module → the lease's end on the SERVER's clock
        self.pending: dict = {}        # module → the monotonic time its Wait/Queued request is asked again
        self.answered: set = set()
        self.cancel = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._write_out("plan.json", {"fingerprints": fingerprints, "lane": "Main" if trunk else "PullRequest"})

    # ── the protocol's files ─────────────────────────────────────────────────────────────────────────────────────────
    def _write_out(self, rel: str, doc: dict) -> None:
        target = os.path.join(self.out, rel)
        tmp = os.path.join(self.priv, "w-" + rel.replace("/", "-"))
        with open(tmp, "w") as fh:
            json.dump(doc, fh)
        shutil.move(tmp, target)

    def _read_io_json(self, kind: str, name: str) -> Optional[dict]:
        """A stage-written JSON file, opened no-follow through the held descriptor; None while it is not (yet) a complete
        JSON object — it is then asked again on the next pass, never consumed as empty."""
        try:
            # O_NONBLOCK: a FIFO the stage planted must never block the broker's open (the regular-file check follows)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd[kind])
        except OSError:
            return None
        try:
            import stat as _stat
            if not _stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, "rb", closefd=False) as fh:
                doc = json.loads(fh.read(1 << 20).decode() or "x")
            return doc if isinstance(doc, dict) else None
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        finally:
            os.close(fd)

    def _list_io(self, kind: str) -> list:
        try:
            return sorted(os.listdir(self.fd[kind]))
        except OSError:
            return []

    def _copy_output_private(self, module: str, dest: str) -> Optional[str]:
        """Copy the stage's `build/<module>/` into host-private `dest` WITHOUT following a link anywhere: the module
        directory is opened no-follow through the held `build` descriptor, the tree walked by descriptors
        (os.fwalk, follow_symlinks=False), each file opened no-follow and accepted only as a regular file. Returns why
        it was refused, or None."""
        import stat as _stat
        try:
            mfd = os.open(module, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd["build"])
        except OSError:
            return f"build/{module}/ is missing or is not a plain directory"
        try:
            shutil.rmtree(dest, ignore_errors=True)
            os.makedirs(dest)
            count = 0
            for dirpath, dirnames, filenames, dfd in os.fwalk(".", dir_fd=mfd, follow_symlinks=False):
                rel_dir = os.path.normpath(dirpath)
                for d in dirnames:
                    if _stat.S_ISLNK(os.lstat(d, dir_fd=dfd).st_mode):
                        return f"build/{module}/ carries a link ({os.path.join(rel_dir, d)}) — refused"
                    os.makedirs(os.path.join(dest, rel_dir, d), exist_ok=True)
                for f in filenames:
                    try:
                        ffd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
                    except OSError:
                        return f"build/{module}/ carries a link or unreadable entry ({os.path.join(rel_dir, f)}) — refused"
                    try:
                        st = os.fstat(ffd)
                        if not _stat.S_ISREG(st.st_mode):
                            return f"build/{module}/ carries a non-file ({os.path.join(rel_dir, f)}) — refused"
                        target = os.path.join(dest, rel_dir, f)
                        with os.fdopen(ffd, "rb", closefd=False) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1 << 20)
                        if st.st_mode & 0o111:
                            os.chmod(target, 0o755)
                        count += 1
                    finally:
                        os.close(ffd)
            return None if count else f"the stage reported success but left no output in build/{module}/"
        finally:
            os.close(mfd)

    # ── the hub ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _path(self, module: str, tail: str = "") -> str:
        return f"/modules/{urllib.parse.quote(self.repo, safe='')}/{urllib.parse.quote(module, safe='')}{tail}"

    def ask(self, module: str) -> Optional[dict]:
        """ONE request to the module's hub. Returns the answer for the stage (Hit, Grant, Refused, Failed), or None when
        the hub said Wait/Queued — then the module is asked again later, without holding the broker."""
        fp = self.fps.get(module)
        if fp is None:
            return {"verdict": "Refused", "why": f"{module!r} is not a module the default branch declares (moduleHub)"}
        body = {"commit": self.sha, "fingerprint": fp, "requester": self.host,
                **({"lane": "Main", "order": self.order} if self.trunk else {"lane": "PullRequest", "pullRequest": self.pr})}
        status, answer = self.api.call("POST", self._path(module, "/request"), body)
        verdict = answer.get("verdict") if isinstance(answer, dict) else None
        step = answer.get("step") if isinstance(answer, dict) else None
        if status == 200 and verdict == "Grant" and isinstance(step, dict):
            self.granted[module] = int(step.get("seq") or 0)
            until = answer.get("leaseUntil") if isinstance(answer, dict) else None
            try:
                self.lease_until[module] = datetime.datetime.fromisoformat(str(until).replace("Z", "+00:00")) if until else None
            except ValueError:
                self.lease_until[module] = None
            return {"verdict": "Grant", "seq": self.granted[module], "fingerprint": fp, "outputDir": os.path.join(self.io, "build", module)}
        if status == 200 and verdict == "Hit" and isinstance(step, dict) and step.get("outputHash"):
            return self._fetch(module, str(step["outputHash"]), int(step.get("seq") or 0))
        if status == 409 or verdict == "Refused":
            return {"verdict": "Refused", "why": (answer or {}).get("why") or (answer or {}).get("error") or f"refused ({status})"}
        return None

    def _fetch(self, module: str, output_hash: str, seq: int) -> dict:
        """A Hit's output into the READ-ONLY `out/outputs/<module>/`: from this host's slice cache when it holds that hash,
        else downloaded into HOST-PRIVATE scratch, verified against the hash, cached, then copied out."""
        hexd = output_hash.split(":", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{64}", hexd):
            return {"verdict": "Failed", "why": f"the hub names an output that is no sha256 ({output_hash!r})"}
        dest = os.path.join(self.out, "outputs", module)
        if self._copy_cached(module, hexd, dest):
            return {"verdict": "Hit", "seq": seq, "outputHash": hexd, "outputDir": dest, "source": "from this host's slice cache"}
        # a COLD host fills from the one store: the content-addressed output, verified against its hash, then cached
        tmp = os.path.join(self.priv, f"{module}.tar")
        status, why = self.api.download(self._path(module, f"/outputs/{hexd}"), tmp, timeout=600)
        if status != 200:
            return {"verdict": "Failed", "why": f"the output {hexd[:12]} could not be fetched ({status}: {why})"}
        if _sha256_file(tmp) != hexd:
            return {"verdict": "Failed", "why": f"the fetched output does not hash to {hexd[:12]} — refused"}
        cached = self._cache_put(module, hexd, tmp)
        if cached is None or not self._copy_cached(module, hexd, dest):
            # the bound evicted it between the put and the copy (or the cache is unwritable): served from the download
            shutil.rmtree(dest, ignore_errors=True)
            try:
                safe_extract(tmp, dest)
            except (Refusal, OSError) as e:
                os.remove(tmp)
                return {"verdict": "Failed", "why": f"the output could not be extracted ({str(e)[:160]})"}
        os.remove(tmp)
        return {"verdict": "Hit", "seq": seq, "outputHash": hexd, "outputDir": dest, "source": "fetched from the hub's store"}

    def _cache_dir(self, module: str, hexd: str) -> str:
        return os.path.join(self.slice_cache or os.path.join(self.priv, "slices"), self.repo, module, hexd)

    def _cached(self, module: str, hexd: str) -> Optional[str]:
        d = self._cache_dir(module, hexd)
        return d if os.path.isfile(os.path.join(d, ".complete")) else None

    def _copy_cached(self, module: str, hexd: str, dest: str) -> bool:
        """Copy a complete cached slice to `dest` holding its SHARED lock (the bound never evicts it meanwhile) and mark it
        used (the `.complete` mtime the LRU reads). False when the cache does not hold it."""
        import fcntl
        d = self._cache_dir(module, hexd)
        fh = _try_lock(d + ".lock", fcntl.LOCK_SH)
        if fh is None:
            return False
        try:
            if not os.path.isfile(os.path.join(d, ".complete")):
                return False
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(d, dest, symlinks=False, ignore=shutil.ignore_patterns(".complete", ".bytes"))
            try:
                os.utime(os.path.join(d, ".complete"))
            except OSError:
                pass
            return True
        except OSError:
            shutil.rmtree(dest, ignore_errors=True)
            return False
        finally:
            fh.close()

    def _cache_put(self, module: str, hexd: str, tar: str) -> Optional[str]:
        """Extract a VERIFIED output into the (host-only) slice cache atomically: staging renamed into place."""
        final = self._cache_dir(module, hexd)
        if os.path.isfile(os.path.join(final, ".complete")):
            return final
        staging = f"{final}.staging-{os.getpid()}-{threading.get_ident()}"
        shutil.rmtree(staging, ignore_errors=True)
        safe_extract(tar, staging)
        with open(os.path.join(staging, ".bytes"), "w") as fh:
            fh.write(str(_tree_bytes(staging)))
        with open(os.path.join(staging, ".complete"), "w") as fh:
            fh.write(hexd)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        try:
            os.rename(staging, final)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
        return final if os.path.isfile(os.path.join(final, ".complete")) else None

    def complete(self, module: str, done: dict) -> dict:
        seq = self.granted.get(module)
        if seq is None:
            return {"accepted": False, "why": f"this host holds no grant for {module!r}"}
        fp = self.fps[module]
        until = self.lease_until.get(module)
        if until is not None and self.utcnow() >= until - datetime.timedelta(seconds=MODULE_HUB_LEASE_MARGIN):
            return {"accepted": False, "succeeded": False, "why": f"the lease ended at {until.isoformat()} — nothing was handed over"}
        # the tag FIRST: a tag key can never override the host's identity fields; the success branch below only updates
        body = {**self.build_tag, "holder": self.host, "seq": seq, "fingerprint": fp, "succeeded": False,
                "error": str(done.get("error") or "the stage reported a failure")[:2000]}
        if done.get("succeeded") is True:
            staged = os.path.join(self.priv, f"stage-{module}")
            refused = self._copy_output_private(module, staged)
            if refused:
                body["error"] = refused
            else:
                tar = os.path.join(self.priv, f"{module}-out.tar")
                try:
                    hexd = deterministic_tar(staged, tar, compress=True)
                except Refusal as e:
                    body["error"] = str(e)
                else:
                    # the upload names its LEASE: control accepts an output only from the live granted holder (§7.3)
                    status, answer = self.api.upload(self._path(module, f"/outputs/{hexd}"),
                                                     {"holder": self.host, "seq": str(seq), "fingerprint": fp}, tar, os.path.getsize(tar),
                                                     timeout=1800, cancel=self.cancel)
                    if status in (200, 201):
                        self._cache_put(module, hexd, tar)
                        # SEAM(ref-asm): the reference-assembly hash once the platform publishes it; the output hash meanwhile
                        body.update(succeeded=True, outputHash=f"sha256:{hexd}", refAsmHash=f"sha256:{hexd}", error=None)
                    else:
                        body["error"] = f"the output upload was refused ({status}: {_answer_text(answer)})"
                    os.remove(tar)
                shutil.rmtree(staged, ignore_errors=True)
        status, answer = self.api.call("POST", self._path(module, "/complete"), body)
        accepted = status == 200 and isinstance(answer, dict) and answer.get("accepted") is True
        if status == 200 or status == 409:
            # the server ANSWERED (accepted, or refused as no longer this host's): the grant is settled either way
            self.granted.pop(module, None)
        server_why = (answer or {}).get("why") if isinstance(answer, dict) else f"{status}"
        # a hand-over that failed HERE (the upload refused, a link in the output, the tar) is named with the server's answer —
        # the server records it as the step's error, but the stage's warning read only "failed — the head stays at the last
        # good step" and twenty modules lost their reuse with no reason in the run's log (2026-11-08)
        why = (server_why if body["succeeded"] or not done.get("succeeded")
               else f"{body.get('error') or 'the hand-over failed with no local reason recorded'}; the hub recorded: {server_why}")
        return {"accepted": accepted, "succeeded": accepted and body["succeeded"], "why": why, "outputHash": body.get("outputHash")}

    def release_open_grants(self, why: str) -> None:
        """Complete every grant the server has not acknowledged as FAILED (the run is ending). Never raises."""
        for module, seq in list(self.granted.items()):
            try:
                status, _ = self.api.call("POST", self._path(module, "/complete"),
                                          {"holder": self.host, "seq": seq, "fingerprint": self.fps[module], "succeeded": False, "error": why})
                if status in (200, 409):
                    self.granted.pop(module, None)
                self.log(f"module hub: {module} released ({why})")
            except Exception as e:   # teardown never fails the run's verdict
                self.log(f"module hub: {module} could not be released: {type(e).__name__}")

    # ── the loop ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def poll_once(self) -> int:
        """One pass: completions first (they free grants), then new requests, then Wait/Queued requests that are due.
        A file is consumed only once it parsed as a complete JSON object. Returns how many answers it wrote."""
        n = 0
        for f in self._list_io("done"):
            module = f[:-5] if f.endswith(".json") else None
            if module is None or f"done:{module}" in self.answered or self.cancel.is_set():
                continue
            doc = self._read_io_json("done", f)
            if doc is None:
                continue                                  # still being written: asked again next pass
            self.answered.add(f"done:{module}")
            ack = self.complete(module, doc)
            self._write_out(f"ack/{module}.json", ack)
            self.log(f"module hub: {module} done → {'accepted' if ack.get('accepted') else 'refused'}")
            n += 1
        now = self.clock()
        for f in self._list_io("req"):
            module = f[:-5] if f.endswith(".json") else None
            if module is None or f"req:{module}" in self.answered or self.cancel.is_set():
                continue
            if module in self.pending and self.pending[module] > now:
                continue
            if module not in self.pending and self._read_io_json("req", f) is None:
                continue
            answer = self.ask(module)
            if answer is None:                            # Wait/Queued: asked again later, the broker moves on
                self.pending[module] = now + MODULE_HUB_RETRY
                continue
            self.pending.pop(module, None)
            self.answered.add(f"req:{module}")
            self._write_out(f"resp/{module}.json", answer)
            self.log(f"module hub: {module} req → {answer.get('verdict')}")
            n += 1
        return n

    def serve(self) -> None:
        while not self.cancel.is_set():
            try:
                if self.poll_once() == 0:
                    self.cancel.wait(MODULE_HUB_POLL)
            except Exception as e:      # the broker must never take the stage down; the stage sees no answer and times out
                self.log(f"module hub broker: {type(e).__name__}: {str(e)[:200]}")
                self.cancel.wait(MODULE_HUB_RETRY)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def stop(self, why: str, bound: float = 60.0) -> None:
        """Stop serving (an upload in flight observes the cancel), wait — bounded — for the thread, release every grant
        the server has not acknowledged, close the held descriptors. FAIL-CLOSED: a thread that will not stop is Fatal, so
        no next run starts beside a broker still talking to the hubs."""
        self.cancel.set()
        if self.thread is not None:
            self.thread.join(timeout=bound)
            if self.thread.is_alive():
                raise Fatal(f"the module-hub broker did not stop within {bound:g} s — this host claims nothing more")
        self.release_open_grants(why)
        for fd in self.fd.values():
            try:
                os.close(fd)
            except OSError:
                pass

def nuget_warm_key(ls_tree: str, command: str, platform: str) -> str:
    """The restore key of a tree from its `git ls-tree -r` listing: the blob ids of every NuGet-relevant file, the
    restore command and the platform. A change to none of them restores nothing new."""
    rows = sorted(line.strip() for line in ls_tree.splitlines()
                  if "\t" in line and NUGET_KEY_FILE.search(line.split("\t", 1)[1].strip()))
    return hashlib.sha256("\n".join([platform, command, *rows]).encode()).hexdigest()


def nuget_warm_marker(cache: str, repo: str, key: str) -> str:
    """Where the host records that the default branch's restore closure for `key` is in `<cache>/nuget`."""
    return os.path.join(cache, "nuget-warm", repo, key[:32])


def load_stages(path: str) -> list[dict]:
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise Refusal(f"the repository declares no build stages ({os.path.basename(os.path.dirname(path))}/"
                      f"build-stages.json is absent) — a run with nothing to run is not a green one")
    except (OSError, ValueError) as e:
        raise Refusal(f".github/build-stages.json is unreadable: {e}")
    return validate_stages(doc)


# The shared-cache entries a stage may DECLARE it reads (`needs`), each bound read-only into its
# sandbox: the plugins layer, its repository's own compile outputs, the NuGet package cache. `nuget` is
# bound for EVERY stage whether declared or not (`Host.stage_readonly`): every stage is handed
# NUGET_FALLBACK_PACKAGES, and a build stage's project.assets.json NAMES that folder, so a later stage
# that cannot see it fails ResolvePackageAssets ("Unable to find fallback package folder"). `nuget` stays
# accepted so existing declarations remain valid.
STAGE_NEEDS = {"plugins", "outputs", "nuget"}


# 🚨 A PREPARED SERVER, NEVER A PREPARED RUN (MeshWeaver.Plugins Hosting/StandingBuildHosts §H7 → "A prepared
# server"; owner: "nothing should ever run this prepare step"). A run arrives on a host that already holds the
# platform it was rolled to, the reference set, the installed plugins layer and the warm NuGet cache. A declared stage
# that stages, resolves, pulls or restores per run is REFUSED — the step belongs in the host image, its init
# containers or its persistent caches, and the refusal names which. Matched as whole words on the command text; a
# stage that legitimately needs one of these is a server that is not prepared yet, never an exception to grant here.
# A tool's GLOBAL options may stand between it and its subcommand (`pip --no-cache-dir install`, `npm --prefix ./app
# ci`, `docker --context x pull`): each is skipped as an option word with an optional value word.
_OPTS = r"(?:\s+-{1,2}[A-Za-z][\w.-]*(?:=\S*|\s+[^-\s;&|]\S*)?)*"
PREPARE_STEPS = (
    (re.compile(r"\bresolve-platform\.py\b"), "resolve-platform.py — the host's set IS the platform (the claim stamps it); nothing resolves one per run"),
    (re.compile(r"\bfetch-refs\.py\b"), "fetch-refs.py — the reference set is staged by the host's init containers, $MeshWeaverRefs"),
    (re.compile(r"\bplatform-mount(?:\.py|-reference-set)?\b"), "platform-mount — the platform is mounted once per roll, by digest"),
    (re.compile(r"\bmodule-fetch\b"), "module-fetch — the plugins layer is installed by the host on the module hand-over"),
    (re.compile(r"\b(?:docker|podman|crane|skopeo|oras)" + _OPTS + r"\s+(?:image\s+)?(?:pull|copy|cp)\b"), "an image pull — images are pre-pulled into the host image or its node-local cache"),
    (re.compile(r"\bprepull\b"), "an image pre-pull — done by the host image, not per run"),
    (re.compile(r"\bdotnet" + _OPTS + r"\s+restore\b"), "dotnet restore — packages come from the host's warm NuGet cache (stage `needs: [\"nuget\"]`)"),
    (re.compile(r"\b(?:nuget|dotnet" + _OPTS + r"\s+nuget)" + _OPTS + r"\s+(?:install|restore)\b"), "a NuGet install — packages come from the host's warm NuGet cache"),
    (re.compile(r"\b(?:apt-get|apt|apk|dnf|yum)\s+(?:-\S+\s+)*install\b"), "a system package install — the host image carries its tools"),
    (re.compile(r"\bapk" + _OPTS + r"\s+add\b"), "a system package install (apk add) — the host image carries its tools"),
    (re.compile(r"\b(?:pip3?|python3?(?:\s+-\S+)*\s+-m\s+pip)" + _OPTS + r"\s+install\b"), "a pip install — the host image carries its tools"),
    (re.compile(r"\bnpm" + _OPTS + r"\s+(?:ci|install|i)\b"), "an npm install — the host image carries its tools"),
    (re.compile(r"\bactions/(?:setup-|cache)"), "a GitHub Actions setup/cache step — the host is already set up"),
)


def per_run_prepare_steps(command: str) -> list[str]:
    """Why `command` would prepare per run — one entry per prepare step it contains; empty = prepared."""
    return [why for rx, why in PREPARE_STEPS if rx.search(command)]


def nuget_packages(folder: str) -> set[str]:
    """`id/version` of every COMPLETE package in a NuGet global-packages folder (one with its
    `.nupkg.metadata`, which NuGet writes last)."""
    found: set[str] = set()
    try:
        for pid in os.listdir(folder):
            pdir = os.path.join(folder, pid)
            if pid.startswith(".") or not os.path.isdir(pdir) or os.path.islink(pdir):
                continue
            for ver in os.listdir(pdir):
                if os.path.isfile(os.path.join(pdir, ver, ".nupkg.metadata")):
                    found.add(f"{pid}/{ver}")
    except OSError:
        pass
    return found


NUGET_SIGNATURE = ".signature.p7s"


def nupkg_content_hash(data: bytes) -> Optional[str]:
    """See `_nupkg_content_hash`; a directory it cannot parse (truncated records) is None, never an exception."""
    try:
        return _nupkg_content_hash(data)
    except (struct.error, IndexError, ValueError):
        return None


def _nupkg_content_hash(data: bytes) -> Optional[str]:
    """NuGet's CONTENT hash of a .nupkg (base64 SHA-512): the archive's own hash when it is unsigned; for a signed
    package the hash of the package WITHOUT its signature entry — the bytes NuGet's
    SignedPackageArchiveUtility.GetPackageContentHash feeds SHA-512: every local entry but the signature's, every
    central-directory record but the signature's (with local-header offsets shifted past the removed entry), and the
    end record with one entry fewer and the directory's size and offset adjusted. None when the archive cannot be
    read that way (ZIP64, a malformed directory) — the package is then NOT promoted."""
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0 or len(data) < eocd + 22:
        return None
    _, _, _, n_disk, n_total, cd_size, cd_off = struct.unpack_from("<IHHHHII", data, eocd)
    if n_disk != n_total or cd_off == 0xFFFFFFFF or n_total == 0xFFFF or cd_off + cd_size > eocd:
        return None
    records, pos = [], cd_off
    for _ in range(n_total):
        if data[pos:pos + 4] != b"PK\x01\x02":
            return None
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, pos + 28)
        local_off = struct.unpack_from("<I", data, pos + 42)[0]
        size = 46 + name_len + extra_len + comment_len
        records.append({"pos": pos, "size": size, "local": local_off, "name": data[pos + 46:pos + 46 + name_len]})
        pos += size
    sig = [r for r in records if r["name"] == NUGET_SIGNATURE.encode()]
    if not sig:
        return base64.b64encode(hashlib.sha512(data).digest()).decode()
    if len(sig) > 1:
        return None
    sig = sig[0]
    by_local = sorted(records, key=lambda r: r["local"])
    for i, r in enumerate(by_local):
        r["entry"] = (by_local[i + 1]["local"] if i + 1 < len(by_local) else cd_off) - r["local"]
    h = hashlib.sha512()
    rest = [r for r in by_local if r is not sig]
    if not rest:
        return None
    h.update(data[0:rest[0]["local"]])
    for r in rest:
        h.update(data[r["local"]:r["local"] + r["entry"]])
    for r in sorted(rest, key=lambda r: r["pos"]):
        h.update(data[r["pos"]:r["pos"] + 42])
        shift = sig["entry"] if r["local"] > sig["local"] else 0
        h.update(struct.pack("<I", r["local"] - shift))
        h.update(data[r["pos"] + 46:r["pos"] + r["size"]])
    h.update(data[eocd:eocd + 8])
    h.update(struct.pack("<HHII", n_disk - 1, n_total - 1, cd_size - sig["size"], cd_off - sig["entry"]))
    h.update(data[eocd + 20:])
    return base64.b64encode(h.digest()).decode()


# What NuGet does NOT extract from a .nupkg into its global-packages folder (OPC plumbing).
NUGET_NOT_EXTRACTED = re.compile(r"^(?:\[Content_Types\]\.xml$|_rels/|package/)", re.I)

# The mode NuGet creates every extracted file with on Linux and macOS (NuGet.Packaging NuGetExtractionFileIO: 0766, the
# process umask applied by open(2)), so a package's native tools stay runnable from the global-packages folder — e.g.
# Grpc.Tools' tools/linux_x64/protoc, which the build EXECUTES from there. A folder this host rebuilt must be the
# folder NuGet would have written: a file created with Python's default 0666 lost the execute bit, and every build
# that compiles a .proto failed with MSB6003 "Permission denied" from the warm cache (MeshWeaver.Plugins#4478).
NUGET_EXTRACTED_FILE_MODE = 0o766
# Written LAST into a package folder this host rebuilt with NuGet's file modes. A folder without it was rebuilt by an
# older runner (files 0666 & ~umask), and `promote_nuget` repairs its modes before keeping it. NuGet ignores the file.
NUGET_MATERIALIZED_MARK = ".mw-materialized-v2"


def nuget_entry_parts(filename: str) -> list[str]:
    """The path segments a .nupkg entry is extracted to, as NuGet places it on disk: empty segments collapse (an entry
    named `analyzers/cs//X.dll` — Nerdbank.MessagePack 1.2.4 ships one — lands at `analyzers/cs/X.dll`, exactly as
    a POSIX path with `//` resolves). Raises ValueError for an entry that would leave the package folder: absolute,
    a backslash, a `.`/`..` segment, or no segment at all."""
    name = urllib.parse.unquote(filename)
    raw = name.split("/")
    if name.startswith("/") or "\\" in name or any(x in (".", "..") for x in raw):
        raise ValueError(f"archive entry {filename!r} would leave the package folder")
    parts = [x for x in raw if x]
    if not parts:
        raise ValueError(f"archive entry {filename!r} would leave the package folder")
    return parts


def nuget_extracted_mode(probe_dir: str) -> int:
    """NUGET_EXTRACTED_FILE_MODE with this process's umask applied — read off a file the kernel created with it (no
    os.umask round-trip, which would race any thread creating files meanwhile)."""
    probe = tempfile.mkdtemp(prefix=".mode-", dir=probe_dir)
    try:
        fd = os.open(os.path.join(probe, "m"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, NUGET_EXTRACTED_FILE_MODE)
        try:
            return os.fstat(fd).st_mode & 0o777
        finally:
            os.close(fd)
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def repair_nuget_modes(final: str) -> int:
    """Give every extracted file of a package folder an OLDER runner rebuilt the mode NuGet would have, then mark it.
    Only mode bits change — no byte the provenance check verified moves — and a chmod is atomic for a reader building
    from the folder meanwhile. Returns the number of files whose mode changed."""
    mode = nuget_extracted_mode(os.path.dirname(final))
    changed = 0
    for root, _dirs, files in os.walk(final):
        for n in files:
            p = os.path.join(root, n)
            if os.path.islink(p) or not os.path.isfile(p) or n == NUGET_MATERIALIZED_MARK:
                continue
            if os.stat(p).st_mode & 0o777 != mode:
                os.chmod(p, mode)
                changed += 1
    with open(os.path.join(final, NUGET_MATERIALIZED_MARK), "w") as fh:
        fh.write("nuget-file-modes\n")
    return changed


def repair_nuget_cache(root: str, log: Callable[[str], None]) -> int:
    """Repair, before a run's first stage, every package folder of the warm cache an older runner rebuilt (no
    NUGET_MATERIALIZED_MARK). A stage reads such a package IN PLACE from the fallback folder, so it never reaches the
    run's own restore and `promote_nuget` never sees it — and a held warm key skips promotion altogether. One stat per
    package once the cache is marked. Returns the number of folders repaired; a folder that cannot be is said, by name."""
    repaired = 0
    for entry in sorted(nuget_packages(root)):
        final = os.path.join(root, *entry.split("/", 1))
        if os.path.isfile(os.path.join(final, NUGET_MATERIALIZED_MARK)):
            continue
        try:
            changed = repair_nuget_modes(final)
            repaired += 1
            log(f"NuGet package {entry}: its file modes repaired to NuGet's ({changed} file(s))")
        except OSError as e:
            log(f"NuGet package {entry}: its file modes could NOT be repaired ({e})")
    return repaired


def nuget_package_problem(pkg_dir: str, pid: str, ver: str) -> Optional[str]:
    """Why a restored package folder may NOT enter the shared warm cache, or None. The folder must hold no symlink, its
    `.nupkg` must hash (SHA-512, base64) to the `.nupkg.sha512` NuGet wrote beside it, and the metadata's
    `contentHash` must be the archive's NuGet CONTENT hash (`nupkg_content_hash` — a signed package's differs from its
    archive hash). What is promoted is then REBUILT from that verified archive (`promote_nuget`), never copied from
    the stage-writable extracted files."""
    for root, dirs, files in os.walk(pkg_dir):
        if any(os.path.islink(os.path.join(root, n)) for n in dirs + files):
            return "it contains a symlink"
    nupkg = f"{pid}.{ver}.nupkg"
    try:
        with open(os.path.join(pkg_dir, nupkg), "rb") as fh:
            data = fh.read()
        with open(os.path.join(pkg_dir, nupkg + ".sha512")) as fh:
            stated = fh.read().strip()
        with open(os.path.join(pkg_dir, ".nupkg.metadata")) as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as e:
        return f"unreadable ({e})"
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != stated:
        return "its archive hash does not match what NuGet recorded"
    content = nupkg_content_hash(data)
    if content is None:
        return "its archive cannot be read as NuGet hashes it (malformed or ZIP64)"
    if not isinstance(meta, dict) or meta.get("contentHash") != content:
        return "its content hash does not match what NuGet recorded"
    return None


def materialize_nuget_package(pkg_dir: str, pid: str, ver: str, dest: str, expected_sha512: Optional[str] = None) -> None:
    """Rebuild a VERIFIED package folder at `dest` from its archive alone: the .nupkg, its .sha512 and .nupkg.metadata
    (re-written from the verified values), and every archive entry NuGet extracts (the nuspec under its lower-case
    id) — nothing a stage left beside them. Raises ValueError for an entry that would leave `dest`, and — given
    `expected_sha512`, the hash the source feed published — for an archive whose bytes are not those (read once here,
    so what is written is what was compared)."""
    nupkg = f"{pid}.{ver}.nupkg"
    with open(os.path.join(pkg_dir, nupkg), "rb") as fh:
        data = fh.read()
    if expected_sha512 is not None and base64.b64encode(hashlib.sha512(data).digest()).decode() != expected_sha512:
        raise ValueError("its archive is not the one its source feed publishes")
    with open(os.path.join(pkg_dir, ".nupkg.metadata")) as fh:
        meta = json.load(fh)
    os.makedirs(dest)
    with open(os.path.join(dest, nupkg), "wb") as fh:
        fh.write(data)
    with open(os.path.join(dest, nupkg + ".sha512"), "w") as fh:
        fh.write(base64.b64encode(hashlib.sha512(data).digest()).decode())
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = urllib.parse.unquote(info.filename)
            if info.is_dir() or NUGET_NOT_EXTRACTED.match(name):
                continue
            parts = nuget_entry_parts(info.filename)
            if len(parts) == 1 and parts[0].lower().endswith(".nuspec"):
                parts = [f"{pid}.nuspec"]
            target = os.path.join(dest, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # NuGet's mode (NUGET_EXTRACTED_FILE_MODE), O_EXCL as before: a second entry onto one path is refused.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, NUGET_EXTRACTED_FILE_MODE)
            # The output owns the descriptor FIRST, so an entry zipfile cannot open (encrypted, unsupported, malformed)
            # still closes it — promote_nuget refuses that package and carries on in a long-running host.
            with os.fdopen(fd, "wb") as out, z.open(info) as src:
                shutil.copyfileobj(src, out)
    out_meta = {"version": meta.get("version", 2), "contentHash": meta["contentHash"]}
    if isinstance(meta.get("source"), str):
        out_meta["source"] = meta["source"]
    with open(os.path.join(dest, NUGET_MATERIALIZED_MARK), "w") as fh:
        fh.write("nuget-file-modes\n")
    with open(os.path.join(dest, ".nupkg.metadata"), "w") as fh:
        json.dump(out_meta, fh, indent=2)


# PROVENANCE: a package enters the shared warm cache only when its archive is byte-for-byte the one its SOURCE FEED
# publishes (MeshWeaver.Plugins#4493). The checks above prove the archive is consistent with ITSELF (its .sha512, its
# content hash); a self-consistent archive under a real id/version that did not come from the feed would pass them.
# NuGet records where a package came from in `.nupkg.metadata` → `source` (the feed's v3 service index URL). The feed's
# published hash is read the documented v3 way: the service index → its `RegistrationsBaseUrl` resource → the
# registration LEAF `{base}/{lower id}/{lower version}.json` → its `catalogEntry` → `packageHash` (base64) with
# `packageHashAlgorithm` SHA512. That hash is over the .nupkg FILE as the feed serves it — a signed package's
# repository signature included — which is exactly the archive NuGet keeps in the global-packages folder, so it is
# compared with the archive's SHA-512 (never with the metadata's contentHash, which excludes a signature). A package
# whose feed cannot be asked or publishes no hash — a local-folder source, a feed that is not v3, an answer that is not
# one, a timeout, a 404 — is REFUSED, by name: never promoted on a missing answer.
NUGET_FEED_TIMEOUT_SECONDS = 20
NUGET_FEED_MAX_BYTES = 16 << 20
NUGET_FEED_WORKERS = 8
NUGET_FEED_BUDGET_SECONDS = 600
# WHICH feeds may vouch for a package: `.nupkg.metadata` → `source` is written by the restore inside the stage, so a
# stage could name a feed it controls that publishes its forged archive's hash. The source must therefore be one the
# HOST trusts, configured independently of anything a stage writes: MW_NUGET_TRUSTED_SOURCES (service-index URLs,
# separated by whitespace or commas), nuget.org's when unset. A package from any other source is refused, by name.
NUGET_TRUSTED_SOURCES_ENV = "MW_NUGET_TRUSTED_SOURCES"
NUGET_DEFAULT_TRUSTED_SOURCES = ("https://api.nuget.org/v3/index.json",)
NUGET_REGISTRATION_TYPES = ("RegistrationsBaseUrl/3.6.0", "RegistrationsBaseUrl/3.4.0", "RegistrationsBaseUrl/3.0.0-rc",
                            "RegistrationsBaseUrl/3.0.0-beta", "RegistrationsBaseUrl")


def nuget_trusted_sources(env: dict) -> tuple:
    """The service-index URLs this host lets vouch for a package (NUGET_TRUSTED_SOURCES_ENV, else nuget.org's)."""
    raw = env.get(NUGET_TRUSTED_SOURCES_ENV, "")
    found = tuple(x for x in re.split(r"[\s,]+", raw) if x)
    return found or NUGET_DEFAULT_TRUSTED_SOURCES


def _source_key(url: str) -> str:
    """A feed URL compared case-insensitively in scheme and host, without a trailing slash."""
    u = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"), u.query, ""))


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a feed's redirect only to another https URL: urllib would follow https → http, and an answer read over
    plain http could vouch for a package with nobody authenticated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError(f"refused a redirect to a non-https URL: {newurl[:200]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def feed_opener() -> urllib.request.OpenerDirector:
    """The opener every feed request goes through: redirects only to https (HttpsOnlyRedirect)."""
    return urllib.request.build_opener(HttpsOnlyRedirect)


def read_bounded(r, deadline: float, limit: int, clock: Callable[[], float] = time.monotonic) -> bytes:
    """Read `r` to its end, refusing past `limit` bytes and past the ABSOLUTE `deadline`: the socket timeout bounds
    each read, not a response that keeps trickling bytes, so without this a worker could outlive any budget."""
    chunks: list = []
    size = 0
    while True:
        if clock() >= deadline:
            raise TimeoutError("the answer did not complete within its deadline")
        chunk = r.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return b"".join(chunks)


def feed_get(url: str, timeout: float) -> bytes:
    """GET one NuGet v3 document over HTTPS — redirects only to https (`feed_opener`) — bounded in time (`timeout`
    per socket operation AND as an absolute deadline over the whole body, `read_bounded`) and size
    (NUGET_FEED_MAX_BYTES, after gzip too: nuget.org serves its semver2 registration hive gzip-encoded). Raises on any
    failure."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"not an https URL: {url[:200]}")
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "gzip",
                                               "User-Agent": "meshweaver-build-host"})
    with feed_opener().open(req, timeout=timeout) as r:
        if urllib.parse.urlsplit(r.geturl()).scheme != "https":
            raise ValueError(f"the answer came from a non-https URL: {r.geturl()[:200]}")
        data = read_bounded(r, deadline, NUGET_FEED_MAX_BYTES)
        gz = (r.headers.get("Content-Encoding") or "").lower() == "gzip"
    if len(data) > NUGET_FEED_MAX_BYTES:
        raise ValueError(f"the answer exceeds {NUGET_FEED_MAX_BYTES} bytes: {url[:200]}")
    if gz:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            data = g.read(NUGET_FEED_MAX_BYTES + 1)
        if len(data) > NUGET_FEED_MAX_BYTES:
            raise ValueError(f"the decompressed answer exceeds {NUGET_FEED_MAX_BYTES} bytes: {url[:200]}")
    return data


class NugetFeeds:
    """The hash each package's SOURCE FEED publishes, read through `get(url, timeout) -> bytes` (`feed_get`; the
    self-test injects a stand-in, never the network). One service index read per source; every answer must be https,
    and the source must be one of `trusted` (nuget_trusted_sources — the host's configuration, never the package's)."""

    def __init__(self, get: Callable[[str, float], bytes] = feed_get,
                 trusted: Sequence[str] = NUGET_DEFAULT_TRUSTED_SOURCES) -> None:
        self.get = get
        self.trusted = frozenset(_source_key(t) for t in trusted)
        self.bases: dict = {}
        self.lock = threading.Lock()

    def _json(self, url: str) -> object:
        return json.loads(self.get(url, NUGET_FEED_TIMEOUT_SECONDS))

    def _registrations(self, source: str) -> str:
        with self.lock:
            if source in self.bases:
                return self.bases[source]
        doc = self._json(source)
        res = doc.get("resources") if isinstance(doc, dict) else None
        found = {}
        for r in res if isinstance(res, list) else []:
            types = r.get("@type") if isinstance(r, dict) else None
            for t in (types if isinstance(types, list) else [types]):
                if isinstance(t, str) and isinstance(r.get("@id"), str):
                    found.setdefault(t, r["@id"])
        base = next((found[t] for t in NUGET_REGISTRATION_TYPES if t in found), None)
        if base is None:
            raise ValueError("its service index names no RegistrationsBaseUrl resource")
        with self.lock:
            self.bases[source] = base
        return base

    def published_hash(self, source: object, pid: str, ver: str) -> str:
        """The base64 SHA-512 `source` publishes for `pid`/`ver`. Raises ValueError (naming why) when it cannot be
        read — the caller then refuses the package."""
        if not isinstance(source, str) or urllib.parse.urlsplit(source).scheme != "https" \
                or not source.rstrip("/").lower().endswith("index.json"):
            raise ValueError(f"its source {str(source)[:120]!r} is not a NuGet v3 feed this host can verify")
        if _source_key(source) not in self.trusted:
            raise ValueError(f"its source {source[:120]!r} is not a feed this host trusts ({NUGET_TRUSTED_SOURCES_ENV})")
        try:
            base = self._registrations(source)
            leaf = self._json(f"{base.rstrip('/')}/{urllib.parse.quote(pid.lower())}/{urllib.parse.quote(ver.lower())}.json")
            entry = leaf.get("catalogEntry") if isinstance(leaf, dict) else None
            if isinstance(entry, str):
                entry = self._json(entry)
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise ValueError(f"its source feed could not be asked ({type(e).__name__}: {str(e)[:160]})")
        if not isinstance(entry, dict):
            raise ValueError("its source feed's registration names no catalog entry")
        def bare(v: object) -> str:     # build metadata never names another package (and the folder drops it)
            return str(v).split("+", 1)[0].lower()
        if str(entry.get("id", "")).lower() != pid.lower() or bare(entry.get("version", "")) != bare(ver):
            raise ValueError("its source feed's catalog entry names another package")
        algo, h = entry.get("packageHashAlgorithm"), entry.get("packageHash")
        if not isinstance(h, str) or not h or str(algo).upper() != "SHA512":
            raise ValueError("its source feed publishes no SHA512 package hash")
        return h

    def published_hashes(self, wanted: Sequence[tuple[str, object, str, str]],
                         budget: float = NUGET_FEED_BUDGET_SECONDS) -> dict:
        """{key: hash or ValueError} for every (key, source, id, version), asked NUGET_FEED_WORKERS at a time; one not
        answered within `budget` seconds is a ValueError too. Every request is itself bounded (feed_get)."""
        out: dict = {}
        if not wanted:
            return out
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=NUGET_FEED_WORKERS)
        try:
            futs = {pool.submit(self.published_hash, src, pid, ver): key for key, src, pid, ver in wanted}
            done, _ = concurrent.futures.wait(futs, timeout=budget)
            for f, key in futs.items():
                if f not in done:
                    out[key] = ValueError(f"its source feed did not answer within {int(budget)} s")
                    continue
                try:
                    out[key] = f.result()
                except ValueError as e:
                    out[key] = e
                except Exception as e:   # never an unverdicted promotion: anything else is a refusal too
                    out[key] = ValueError(f"its source feed could not be asked ({type(e).__name__})")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return out


PREPARED_OUTPUT_TAIL = 64 * 1024


def file_tail(fh, limit: int) -> str:
    """The last `limit` bytes of an open binary file, decoded leniently — a bounded read of untrusted output."""
    fh.flush()
    size = fh.seek(0, os.SEEK_END)
    fh.seek(max(0, size - limit))
    return fh.read().decode("utf-8", errors="replace")


def _artifact_identity(st: os.stat_result):
    # st_ctime_ns too: no process can set it, so a same-size rewrite that restores the old mtime (`cp -p`, `touch -d`,
    # a reproducible-build timestamp normalisation) onto the same inode still reads as rewritten.
    return "dir" if stat.S_ISDIR(st.st_mode) else (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns, st.st_ino,
                                                   st.st_ctime_ns)


def artifact_snapshot(root: str) -> dict:
    """Every entry under the shared artifacts directory, by relative path: a real directory as "dir" (what changes
    inside it is found by descending), anything else — file, symlink, fifo — by its lstat identity (type, size,
    mtime, inode, ctime), so a stage that ADDS or REWRITES an entry is visible against the snapshot taken before it ran."""
    snap: dict = {}
    for base, dirs, names in os.walk(root):
        for n in dirs + names:
            full = os.path.join(base, n)
            snap[os.path.relpath(full, root)] = _artifact_identity(os.lstat(full))
    return snap


# The OUTCOME of the stage a receipt comes from (MeshWeaver.Deploy#52). A RED stage hands over everything it added or
# rewrote — a test shard's results tree, its `diagnostics/` unit (MeshWeaver.Plugins#4499), and also whatever it left
# unfinished (a half-written bundle) — because that is the evidence of WHY it is red; every one of its receipts says
# `failed`, so no reader takes it for a green stage's output: the publish lane and the gate-log ratchet read ONLY
# `succeeded` receipts, and consolidation reads a red shard on purpose. The marker rides on the `artifact` event and, on
# the API store, on the upload (`stageState=`), where the instance records it on the receipt it writes.
STAGE_SUCCEEDED = "succeeded"
STAGE_FAILED = "failed"


RED_STAGE_COPY_CHUNK = 8 << 20


def red_stage_artifacts(root: str, before: dict, dest: str,
                        proceed: Callable[[], None] = lambda: None) -> list[str]:
    """COPY into `dest` every REGULAR file under `root` that a failed stage added or rewrote (anything not in, or
    different from, `before`), at the same relative path. A symlink, fifo or device is skipped, never followed (nor is a
    symlinked directory descended): a red stage's leftovers are untrusted, and evidence is not a reason to fail the
    hand-over. The sources are left in place for the quarantine. Returns the copied relative paths. `proceed` is called
    before each file and between bounded chunks of it, and raises to stop: results trees and dumps can be large, and an
    expired or abandoned run must not keep copying them."""
    copied: list[str] = []
    if not os.path.isdir(root) or os.path.islink(root):
        return copied
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for n in sorted(names):
            full = os.path.join(base, n)
            st_ = os.lstat(full)
            rel = os.path.relpath(full, root)
            if not stat.S_ISREG(st_.st_mode) or before.get(rel) == _artifact_identity(st_):
                continue
            proceed()
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src_fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(src_fd, "rb") as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(RED_STAGE_COPY_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    proceed()
            copied.append(rel.replace(os.sep, "/"))
    return sorted(copied)


def quarantine_artifacts(root: str, before: dict, dest: str) -> list[str]:
    """Move out of the shared artifacts directory every entry a FAILED stage added or rewrote (anything not in, or
    different from, `before`), into `dest`. The artifacts directory is shared by the run's stages and each successful
    stage hands over everything under it, so a failed stage's unfinished files would otherwise be uploaded — and
    receipted — under a later independent sibling's name, and an invalid leftover (a dangling symlink) would turn that
    sibling red. Returns the moved relative paths."""
    moved: list[str] = []
    for base, dirs, names in os.walk(root):
        for n in list(dirs) + names:
            full = os.path.join(base, n)
            rel = os.path.relpath(full, root)
            if before.get(rel) == _artifact_identity(os.lstat(full)):
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(full, target)
            moved.append(rel)
            if n in dirs:
                dirs.remove(n)
    return sorted(moved)


def promote_nuget(restored: str, cache: str, log: Callable[[str], None],
                  feeds: Optional[NugetFeeds] = None,
                  budget: float = NUGET_FEED_BUDGET_SECONDS) -> tuple[int, int, list[str]]:
    """Promote the packages a TRUNK build stage restored into the shared warm NuGet cache
    (`<cache>/nuget/<id>/<version>/`, the stages' read-only fallback folder), so the next run restores
    nothing. Each verified (`nuget_package_problem`), compared with the hash its SOURCE FEED publishes
    (`NugetFeeds.published_hash` — a mismatch, or no answer, refuses it), REBUILT from its verified archive in a temp
    dir (`materialize_nuget_package`) and renamed into place; a
    package already present is kept (a NuGet id/version is immutable). The feed is asked within `budget` seconds —
    the caller passes what its RUN has left, so verification never overruns the run; with none left nothing is asked
    and every candidate is refused (retried by a later run). Returns (promoted, kept, refused)."""
    feeds = feeds or NugetFeeds()
    root = os.path.join(cache, "nuget")
    os.makedirs(root, exist_ok=True)
    promoted, kept, refused = 0, 0, []
    candidates: list[tuple[str, object, str, str]] = []
    for entry in sorted(nuget_packages(restored)):
        pid, ver = entry.split("/", 1)
        final = os.path.join(root, pid, ver)
        if os.path.isfile(os.path.join(final, ".nupkg.metadata")):
            kept += 1
            if not os.path.isfile(os.path.join(final, NUGET_MATERIALIZED_MARK)):
                # Rebuilt by an older runner without NuGet's file modes: a native tool in it cannot be executed.
                try:
                    changed = repair_nuget_modes(final)
                    log(f"NuGet package {entry}: kept, its file modes repaired to NuGet's ({changed} file(s))")
                except OSError as e:
                    log(f"NuGet package {entry}: kept, but its file modes could NOT be repaired ({e})")
            continue
        why = nuget_package_problem(os.path.join(restored, pid, ver), pid, ver)
        if why:
            refused.append(f"{entry} ({why})")
            continue
        try:
            with open(os.path.join(restored, pid, ver, ".nupkg.metadata")) as fh:
                source = json.load(fh).get("source")
        except (OSError, ValueError, AttributeError) as e:
            refused.append(f"{entry} (its source cannot be read: {e})")
            continue
        candidates.append((entry, source, pid, ver))
    budget = min(budget, NUGET_FEED_BUDGET_SECONDS)
    published = feeds.published_hashes(candidates, budget) if budget > 0 else \
        {entry: ValueError("the run had no time left to ask its source feed") for entry, _s, _p, _v in candidates}
    for entry, _source, pid, ver in candidates:
        final = os.path.join(root, pid, ver)
        answer = published.get(entry, ValueError("its source feed was not asked"))
        if isinstance(answer, ValueError):
            refused.append(f"{entry} ({answer})")
            continue
        # The .sha512 was proved to be the archive's own hash (nuget_package_problem); materialize re-hashes the bytes
        # it actually copies against the same answer, so a swap between the two reads is refused too.
        try:
            with open(os.path.join(restored, pid, ver, f"{pid}.{ver}.nupkg.sha512")) as fh:
                archive_hash = fh.read().strip()
        except OSError as e:
            refused.append(f"{entry} (unreadable ({e}))")
            continue
        if archive_hash != answer:
            refused.append(f"{entry} (its archive is not the one its source feed publishes)")
            continue
        os.makedirs(os.path.join(root, pid), exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".{ver}-", dir=os.path.join(root, pid))
        try:
            materialize_nuget_package(os.path.join(restored, pid, ver), pid, ver, os.path.join(tmp, "p"), answer)
            os.rename(os.path.join(tmp, "p"), final)
            promoted += 1
        except (OSError, ValueError, zipfile.BadZipFile, KeyError, RuntimeError, zlib.error, EOFError) as e:
            # RuntimeError: an ENCRYPTED entry (zipfile asks for a password) and NotImplementedError (an unsupported
            # compression method, a RuntimeError subclass) — the package is refused, never an unverdicted host.
            if not os.path.isfile(os.path.join(final, ".nupkg.metadata")):
                refused.append(f"{entry} (could not be moved into place: {e})")
            else:
                kept += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if refused:
        log(f"NuGet packages NOT promoted into the warm cache: {refused[:5]}")
    return promoted, kept, refused


def validate_stages(doc: object) -> list[dict]:
    stages = doc.get("stages") if isinstance(doc, dict) else None
    if not isinstance(stages, list) or not stages:
        raise Refusal(".github/build-stages.json must be {\"stages\": [ … ]} with at least one stage")
    seen: set[str] = set()
    out = []
    for i, s in enumerate(stages):
        if not isinstance(s, dict):
            raise Refusal(f"stage #{i} is not an object")
        name = str(s.get("name") or "")
        if not STAGE_NAME.match(name):
            raise Refusal(f"stage #{i} name {name!r} is not one path segment [a-z0-9][a-z0-9._-]* "
                          f"(its Check Run is build/<name>, or build-<channel>/<name>)")
        if name in seen:
            raise Refusal(f"stage {name!r} is declared twice")
        seen.add(name)
        kind = str(s.get("kind") or "")
        if kind not in KINDS:
            raise Refusal(f"stage {name!r} kind {kind!r} must be one of {sorted(KINDS)}")
        cmd = s.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise Refusal(f"stage {name!r} has no command")
        prepares = per_run_prepare_steps(cmd)
        if prepares:
            raise Refusal(f"stage {name!r} prepares per run — a host stage runs on a PREPARED server, so it may not "
                          f"stage, resolve, pull or restore: " + "; ".join(prepares))
        tmo = s.get("timeoutMinutes", DEFAULT_STAGE_TIMEOUT_MINUTES)
        if not isinstance(tmo, (int, float)) or not (0 < tmo <= DEFAULT_STAGE_TIMEOUT_MINUTES):
            raise Refusal(f"stage {name!r} timeoutMinutes {tmo!r} must be in (0, {DEFAULT_STAGE_TIMEOUT_MINUTES}]")
        needs = s.get("needs", [])
        if not isinstance(needs, list) or not all(isinstance(n, str) and n in STAGE_NEEDS for n in needs):
            raise Refusal(f"stage {name!r} needs {needs!r} must be a list drawn from {sorted(STAGE_NEEDS)} — "
                          f"the shared-cache entries the stage may read (its own repository's mirror always)")
        # `after`: the stages this one DEPENDS ON — it is skipped when any of them failed. Only names declared EARLIER
        # (declaration order is execution order, so a dependency always runs first and no cycle can be written).
        # Absent: every earlier `build` stage — the build that produces the outputs later stages read. `[]` declares
        # a stage independent of every other.
        if "after" in s:
            after = s["after"]
            if not isinstance(after, list) or not all(isinstance(a, str) for a in after) or len(set(after)) != len(after):
                raise Refusal(f"stage {name!r} after {after!r} must be a list of distinct stage names")
            unknown = [a for a in after if a not in seen or a == name]
            if unknown:
                raise Refusal(f"stage {name!r} runs after {unknown}, which no stage declared BEFORE it names — "
                              f"a stage may run only after stages declared earlier in .github/build-stages.json")
        else:
            after = [o["name"] for o in out if o["kind"] == "build"]
        out.append({"name": name, "kind": kind, "command": cmd, "timeoutMinutes": float(tmo), "needs": list(needs),
                    "after": list(after)})
    return out


CHECK_PREFIX = re.compile(r"^build(-[a-z0-9][a-z0-9._-]*)?/")


def bare_stage(name: str) -> str:
    """A stage from its check name: `build/<stage>` (latest) or `build-<channel>/<stage>`."""
    return CHECK_PREFIX.sub("", name, count=1)


def select_stages(declared: list[dict], claimed: object) -> list[dict]:
    """The stages this run executes: all declared, or — when the claim names stages — those, in
    declaration order. A claimed stage the repository does not declare is a Refusal."""
    names = []
    for c in claimed or []:
        n = c.get("name") if isinstance(c, dict) else c
        if isinstance(n, str) and n:
            names.append(bare_stage(n))
    if not names:
        return declared
    known = {s["name"] for s in declared}
    missing = [n for n in names if n not in known]
    if missing:
        raise Refusal(f"the claim names stage(s) {missing} that .github/build-stages.json does not declare")
    want = set(names)
    return [s for s in declared if s["name"] in want]


# ───────────────────────────────── fan-out: a run's stages in parallel ───────────────────────
#
# A run's independent stages run CONCURRENTLY on one host when the REPOSITORY declares them safe to (`"parallel": true`
# beside `stages`) and the HOST declares room for more than one (`MW_BUILD_STAGE_SLOTS`, chart `buildHosts.stageSlots`).
# Both halves are declared, never derived: the repository is the only party that knows its sibling stages share no
# writable state beyond what the host separates (each stage's own sandbox, /tmp, scratch and PostgreSQL socket; with
# fan-out, its own `$MW_BUILD_ARTIFACTS`), and the host's slots are checked against the cgroup it runs in at start.
# The rules (`next_stages`, shared by the scheduler and by the budget's simulation, so the declared budget is the
# schedule the host actually runs):
#   * a stage starts once every stage it runs `after` (among the selected ones) has finished green;
#   * a `build` stage runs ALONE — nothing else is in flight while it runs, and nothing later starts while one waits
#     for the stages in flight to drain: it writes the shared `$MW_BUILD_OUTPUTS` and the host promotes outputs and
#     NuGet packages after it, so no sibling may observe or contribute a half-written tree;
#   * at most `slots` stages are in flight; within that, declaration order decides which starts first.
# Everything else is unchanged: a stage whose dependency failed is finished "not run" naming it, the run deadline ends
# every stage not yet started, a red stage's artifacts never reach a hand-over, and the namespace leftover check runs
# whenever no stage is in flight (between stages it would see the live siblings' sandboxes).
MAX_STAGE_SLOTS = 32
# What one concurrent stage needs at least, of the cgroup the host runs in: a test host and its own PostgreSQL. A
# declaration past that is refused at start, naming the measured capacity — never clamped silently.
MIN_CPU_PER_SLOT = 1.0
MIN_MEMORY_PER_SLOT = 2 * 1024 ** 3


def load_parallel(path: str) -> bool:
    """`"parallel": true` beside `stages`: the repository declares its stages safe to run concurrently. Absent = false."""
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise Refusal(f".github/build-stages.json is unreadable: {e}")
    value = doc.get("parallel", False) if isinstance(doc, dict) else False
    if not isinstance(value, bool):
        raise Refusal(f".github/build-stages.json `parallel` is {value!r} — true, false or absent")
    return value


def next_stages(pending: Sequence[dict], selected: set, finished: set, failed: set, running: dict,
                slots: int, prereqs: Optional[dict] = None) -> tuple[list[tuple[dict, str]], list[dict]]:
    """One scheduling decision. `pending` in declaration order; `running` maps each in-flight stage to its kind.
    Returns (blocked, start): the pending stages a FAILED dependency blocks — each with the first such dependency — and
    the stages to start now. A dependency the claim did not select is not run here and blocks nothing. `prereqs` (fan-out,
    `cap_schedule`) PINS the schedule: a stage also waits until every stage its all-caps schedule finished before it
    started has finished here — so no stage ever starts later than the declared budget planned it."""
    blocked: list[tuple[dict, str]] = []
    start: list[dict] = []
    running = dict(running)
    for st in pending:
        deps = [a for a in st.get("after", []) if a in selected]
        bad = [a for a in deps if a in failed]
        if bad:
            blocked.append((st, bad[0]))
            continue
        if any(a not in finished for a in deps):
            continue
        if prereqs is not None and any(p not in finished for p in prereqs.get(st["name"], ())):
            continue
        if "build" in running.values() or (st["kind"] == "build" and running) or len(running) >= slots:
            break
        start.append(st)
        running[st["name"]] = st["kind"]
    return blocked, start


def simulate_schedule(stages: Sequence[dict], slots: int, duration: Callable[[dict], float],
                      prereqs: Optional[dict] = None) -> tuple[float, dict]:
    """Run `next_stages` over `stages` (none failing) with each stage taking `duration(stage)` seconds. Returns the
    makespan and, per stage, the stages that had FINISHED before it started — the pin `cap_schedule` hands the scheduler."""
    stages = [dict(st, name=st.get("name", f"#{i}"), kind=st.get("kind", "test")) for i, st in enumerate(stages)]
    selected = {s["name"] for s in stages}
    pending = list(stages)
    finished: set = set()
    inflight: dict = {}
    before: dict = {}
    now = 0.0
    while pending or inflight:
        _, start = next_stages(pending, selected, finished, set(), {n: k for n, (_, k) in inflight.items()}, slots, prereqs)
        for st in start:
            pending.remove(st)
            before[st["name"]] = frozenset(finished)
            inflight[st["name"]] = (now + float(duration(st)), st["kind"])
        if not inflight:
            break
        now = min(end for end, _ in inflight.values())
        for n in [n for n, (end, _) in inflight.items() if end <= now]:
            finished.add(n)
            del inflight[n]
    return now, before


def _cap_seconds(st: dict) -> float:
    return float(st.get("timeoutMinutes", DEFAULT_STAGE_TIMEOUT_MINUTES)) * 60


def cap_schedule(stages: Sequence[dict], slots: int) -> tuple[float, Optional[dict]]:
    """The schedule when every stage takes its full cap, and the PIN that makes it a bound: per stage, the stages that
    finished before it started there. A greedy schedule is NOT monotone in its durations (Graham's anomaly — a stage
    finishing early hands its slot to a stage that then delays a longer one), so the all-caps length alone bounds
    nothing; with every stage also waiting for its pinned predecessors (`next_stages(prereqs=…)`), each start is the
    latest finish of a fixed set of stages, so no stage starts later than it does here and the run ends within it.
    One slot: the caps' sum, no pin (one stage at a time is monotone by itself)."""
    if slots <= 1:
        return sum(_cap_seconds(st) for st in stages), None
    return simulate_schedule(stages, slots, _cap_seconds)


def planned_seconds(stages: Sequence[dict], slots: int) -> float:
    """The schedule's length when every stage takes its full cap (`cap_schedule`). With one slot it is the caps' sum — the
    serial run; with fan-out, the critical path through the slots — a BOUND on the run, because the scheduler is pinned
    to that schedule."""
    return cap_schedule(stages, slots)[0]


# A cgroup v1 limit at or past this is the kernel's "no limit" (page-rounded LONG_MAX), never a real bound.
CGROUP_V1_UNLIMITED = 1 << 60


def _read_cgroup_file(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def cgroup_capacity(root: str = "/sys/fs/cgroup") -> "tuple[float, Optional[int]] | str":
    """(CPUs, memory bytes or None = no limit) this process may use, read from the cgroup v2 `cpu.max`/`memory.max` or
    the cgroup v1 `cpu.cfs_quota_us`/`cpu.cfs_period_us`/`memory.limit_in_bytes` — or, when neither layout yields both
    limits, a string naming what could not be read. An unmeasured cgroup is never taken for an unrestricted one."""
    machine = float(os.cpu_count() or 1)
    try:
        if os.path.exists(os.path.join(root, "cgroup.controllers")):
            cpu_raw = _read_cgroup_file(os.path.join(root, "cpu.max"))
            mem_raw = _read_cgroup_file(os.path.join(root, "memory.max"))
            if cpu_raw is None or mem_raw is None:
                return (f"the cgroup v2 limits under {root} (cpu.max {'unreadable' if cpu_raw is None else 'read'}, "
                        f"memory.max {'unreadable' if mem_raw is None else 'read'})")
            quota, period = (cpu_raw.split() + ["100000"])[:2]
            cpus = machine if quota == "max" else min(machine, float(quota) / float(period))
            return cpus, (None if mem_raw == "max" else int(mem_raw))
        quota_raw = _read_cgroup_file(os.path.join(root, "cpu", "cpu.cfs_quota_us"))
        period_raw = _read_cgroup_file(os.path.join(root, "cpu", "cpu.cfs_period_us"))
        mem_raw = _read_cgroup_file(os.path.join(root, "memory", "memory.limit_in_bytes"))
        if quota_raw is None or period_raw is None or mem_raw is None:
            return (f"the cgroup limits under {root} (neither cgroup v2 cpu.max/memory.max nor cgroup v1 "
                    f"cpu/cpu.cfs_quota_us, cpu/cpu.cfs_period_us and memory/memory.limit_in_bytes are readable)")
        quota_us = int(quota_raw)
        cpus = machine if quota_us < 0 else min(machine, quota_us / float(period_raw))
        mem = int(mem_raw)
        return cpus, (None if mem >= CGROUP_V1_UNLIMITED else mem)
    except (ValueError, ZeroDivisionError) as e:
        return f"the cgroup limits under {root} (unparseable: {e})"


def stage_slots_refusal(slots_text: str, capacity: "tuple[float, Optional[int]] | str") -> Optional[str]:
    """Why MW_BUILD_STAGE_SLOTS cannot be run on this host's cgroup, or None. `capacity` as a string = the cgroup's
    limits could not be established, which refuses more than one slot rather than assuming none apply."""
    try:
        slots = int(slots_text)
    except ValueError:
        return f"MW_BUILD_STAGE_SLOTS as a whole number in [1, {MAX_STAGE_SLOTS}] (got {slots_text!r})"
    if not (1 <= slots <= MAX_STAGE_SLOTS):
        return f"MW_BUILD_STAGE_SLOTS within [1, {MAX_STAGE_SLOTS}] (got {slots})"
    if isinstance(capacity, str):
        if slots > 1:
            return (f"MW_BUILD_STAGE_SLOTS {slots} on a host whose capacity cannot be measured — could not read "
                    f"{capacity}; run one slot (buildHosts.stageSlots: 1) or run on a supported cgroup layout")
        return None
    cpus, mem = capacity
    if slots > 1 and cpus / slots < MIN_CPU_PER_SLOT:
        return (f"MW_BUILD_STAGE_SLOTS {slots} on {cpus:g} CPU(s) — each concurrent stage needs at least "
                f"{MIN_CPU_PER_SLOT:g} CPU; declare fewer slots or more CPU (buildHosts.stageSlots / resources)")
    if slots > 1 and mem is not None and mem / slots < MIN_MEMORY_PER_SLOT:
        return (f"MW_BUILD_STAGE_SLOTS {slots} in {mem / 1024 ** 3:.1f} GiB — each concurrent stage needs at least "
                f"{MIN_MEMORY_PER_SLOT // 1024 ** 3} GiB; declare fewer slots or more memory (buildHosts.stageSlots / resources)")
    return None


# ───────────────────────────────── the build API ──────────────────────────────────────────

class Lost(Exception):
    """409: the run is claimed by another host. Stop everything for it."""


# One block of an artifact upload: between two blocks the upload checks `cancel`, polls for an early answer and gives
# the next send only the time left of its absolute budget (Api.upload).
UPLOAD_BLOCK = 256 * 1024


class _AnswerReader(io.RawIOBase):
    """The answer's first bytes — taken off the socket while the body was still streaming — then the socket itself."""

    def __init__(self, raw, head: bytes) -> None:
        super().__init__()
        self._raw, self._head = raw, head

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self._head:
            n = min(len(b), len(self._head))
            b[:n] = self._head[:n]
            self._head = self._head[n:]
            return n
        return self._raw.readinto(b)

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


class _AnsweredSocket:
    """The upload's socket with the answer's already-read first bytes put back in front, for http.client to parse."""

    def __init__(self, sock, head: bytes) -> None:
        self._sock, self._head = sock, head

    def makefile(self, mode: str = "rb", *args, **kwargs):
        # the socket's OWN unbuffered reader underneath: it holds the descriptor open after the connection closes
        # (http.client closes it once a `Connection: close` answer's head is read), exactly as a plain makefile does
        return io.BufferedReader(_AnswerReader(self._sock.makefile("rb", buffering=0), self._head))

    def __getattr__(self, name: str):
        return getattr(self._sock, name)


def upload_endpoint(url: urllib.parse.SplitResult) -> tuple[str, int]:
    """(host, port) for http.client from a split URL, the port ALWAYS explicit: `urlsplit` strips an IPv6
    literal's brackets, and http.client re-parses a bare `::1` with no port as host `:` port `1`."""
    return url.hostname or "", url.port or (443 if url.scheme == "https" else 80)


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: Optional[dict] = None,
             query: Optional[dict] = None) -> tuple[int, object]:
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            # The BODY of a refusal is part of the answer: a 409's `error` tells `stale-set` (drain)
            # from `stale-claim-id` (claim again with a fresh id). Kept beside its status code.
            try:
                raw = e.read().decode()
                return e.code, (json.loads(raw) if raw.strip() else None)
            except (OSError, ValueError, http.client.HTTPException):
                # A refusal whose body was cut short is still that refusal: keep its status.
                return e.code, None
        # http.client.HTTPException covers IncompleteRead: a response closed before its Content-Length
        # arrived is a transport failure (status 0, retried inside the budget), never an escaping traceback.
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            return 0, str(e)


    def download(self, path: str, dest: str, timeout: float) -> tuple[int, str]:
        """GET `path`'s bytes into `dest` with the worker token, streamed; (status, why) — 0 = transport failure."""
        req = urllib.request.Request(self.base + path, method="GET", headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=min(timeout, 600)) as r, open(dest, "wb") as fh:
                shutil.copyfileobj(r, fh, 1 << 20)
                return r.status, ""
        except urllib.error.HTTPError as e:
            return e.code, str(e.reason)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            return 0, str(e)

    def upload(self, path: str, query: dict, file_path: str, size: int, timeout: float,
               cancel: Optional[threading.Event] = None) -> tuple[int, object]:
        """PUT one file's bytes to `path` (`Content-Length` = `size`, streamed from disk in blocks, never read whole into
        memory) with the worker token; the answer's status and JSON body, as `call` returns them (0 = transport failure).
        `cancel` set while the body streams raises Lost at the next block — an abandoned run stops sending at once.

        `timeout` is an ABSOLUTE budget for the whole exchange — connect, every block sent, the answer read — never a
        per-socket-operation bound (MeshWeaver.Deploy#63): an upload that keeps making slow progress used to run past the
        run's budget, and a success read after it was accepted. Every socket operation gets only the time left, and an
        exchange that runs out is status 0 naming the budget, the same transport failure the hand-over bounds.

        The server may answer BEFORE it reads the body: the build API refuses an oversize artifact (413) on the declared
        Content-Length (MeshWeaver.Deploy#64). Between blocks the socket is polled for an answer, and a send that fails
        (broken pipe, reset) still reads the answer the server sent — so the host reports the server's reason and does
        not retry a refusal as if the transport had failed."""
        url = urllib.parse.urlsplit(self.base + path + (("?" + urllib.parse.urlencode(query)) if query else ""))
        target = (url.path or "/") + (("?" + url.query) if url.query else "")
        deadline = time.monotonic() + timeout

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the upload ran past its {timeout:g} s budget")
            return remaining

        factory = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = None
        if cancel is not None and cancel.is_set():
            raise Lost("the run was abandoned before an artifact upload started")
        try:
            conn = factory(*upload_endpoint(url), timeout=left())
            with open(file_path, "rb") as fh:
                conn.putrequest("PUT", target, skip_accept_encoding=True)
                for k, v in (("Authorization", f"Bearer {self.token}"), ("Accept", "application/json"),
                             ("Content-Type", "application/octet-stream"), ("Content-Length", str(size))):
                    conn.putheader(k, v)
                conn.endheaders()
                sock = conn.sock
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Lost("the run was abandoned while an artifact upload was streaming")
                    if select.select([sock], [], [], 0)[0]:
                        # A readable socket is NOT yet an answer on HTTPS: a TLS 1.3 session ticket (or any other TLS
                        # control record) makes it readable too, and stopping the body for one leaves both sides
                        # waiting for the other. Read what is there without blocking: the TLS layer consumes control
                        # records itself and hands back only HTTP bytes; none means keep sending.
                        sock.setblocking(False)
                        try:
                            head = sock.recv(65536)
                        except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                            head = None
                        sock.settimeout(left())
                        if head is not None:
                            # the server answered (or closed: b"") before the body ended — the answer is read below,
                            # starting with the bytes already taken off the socket
                            conn.sock = _AnsweredSocket(sock, head)
                            break
                    block = fh.read(UPLOAD_BLOCK)
                    if not block:
                        break
                    sock.settimeout(left())
                    try:
                        sock.sendall(block)
                    except (BrokenPipeError, ConnectionResetError):
                        break          # the server stopped reading — its answer, if it sent one, is read below
            sock.settimeout(left())
            r = conn.getresponse()
            chunks = []
            try:
                while not r.isclosed():     # the response closes itself once its Content-Length has arrived
                    # read1: ONE socket read per call, so each gets only the time left — `read(n)` loops over many
                    # reads under a single timeout and a trickle would outlive the budget inside it.
                    sock.settimeout(left())
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                if r.length:                # closed before its Content-Length arrived
                    raise http.client.IncompleteRead(b"".join(chunks), r.length)
            except (OSError, http.client.HTTPException):
                if not (200 <= r.status < 300):
                    # A refusal whose body was cut short (or reset) is still that refusal: keep its status, as `call`
                    # does, so a 413/401 fails at once and is never retried as a transport failure.
                    return r.status, None
                raise
            left()                      # an answer that arrived after the budget is not accepted
            try:
                # decoded INSIDE the guard: a refusal whose body is not UTF-8 is still that refusal
                raw = b"".join(chunks).decode()
                return r.status, (json.loads(raw) if raw.strip() else None)
            except ValueError:
                # A refusal whose body is not JSON is still that refusal: keep its status.
                return (r.status, None) if not (200 <= r.status < 300) else (0, f"unreadable answer to HTTP {r.status}")
        # http.client.HTTPException covers IncompleteRead: a response closed before its Content-Length
        # arrived is a transport failure (status 0, retried inside the budget), never an escaping traceback.
        except (OSError, ValueError, http.client.HTTPException) as e:
            if time.monotonic() >= deadline and "budget" not in str(e):
                return 0, f"the upload ran past its {timeout:g} s budget ({e})"
            return 0, str(e)
        finally:
            if conn is not None:
                conn.close()


def artifact_path(repo: str, sha: str, digest: str) -> str:
    return f"/{urllib.parse.quote(repo, safe='')}/{sha}/artifacts/{digest}"


# The API artifact store (`MW_BUILD_ARTIFACT_STORE=api:`): every artifact is PUT to the build API, which verifies its
# length and sha256 while it stores it under the instance's own root and records the receipt. A transport failure or a
# 5xx is retried at most API_UPLOAD_ATTEMPTS times in all, with a doubling pause, inside the run budget; anything else —
# and the last failed attempt — fails the stage, naming the answer. Never a silent pass.
# 10 attempts with the pause doubling from 2 s and capped at 60 s wait ~5 min in all (2+4+…+32+60·4 ≈ 302 s): a stage's
# whole verdict is lost when its hand-over fails, and the 503 that cost one (Plugins fee21cf7, 2026-11-07, "artifact
# hand-over 503 after 4 attempts" — then ~14 s of patience) fell inside a control-database throttling window that lasted
# minutes, not seconds (Plugins Hosting/CiProfile.md §7). Still bounded by the run budget, which every pause respects.
API_STORE_SPEC = "api:"
API_UPLOAD_ATTEMPTS = 10


def events_path(repo: str, sha: str, stage: str) -> str:
    return f"/{urllib.parse.quote(repo, safe='')}/{sha}/stages/{urllib.parse.quote(stage, safe='')}/events"


# THE RUN BUDGET. A run's deadline is the HOST's (MW_BUILD_RUN_DEADLINE_SECONDS, default 120 min,
# at most 720 — the contract's `budgetMinutes` range): it must cover the stages a repository
# declares, which run SEQUENTIALLY unless the repository declares `parallel` and the host has more
# than one stage slot (MeshWeaver.Plugins: compile-check 40 + gate 45 = 85 min). Each stage's
# `timeoutMinutes` is a PER-STAGE CAP inside it. The run's EFFECTIVE budget is the smaller of the
# deadline and the selected stages' caps AS SCHEDULED — their sum on one slot, the critical path
# through the slots with fan-out (`planned_seconds`) — + RUN_MARGIN_SECONDS for checkout, merge and
# hand-over; it is DECLARED in the plan (`budgetMinutes`, so the instance's attempt deadline is
# claim time + budget + 15 min). When the sum exceeds the deadline the run still starts — stages
# often finish well inside their caps — and the stages the deadline does not reach are finished
# `failed` with "not run — the run deadline passed": red, never skipped.
# The 45-minute GitHub job cap bounds a FOLLOW job, not a run: a follower that reaches its window
# with its stage's verdict in exits with that verdict (build-follow.py); a run longer than a
# follower's window is judged on the instance's live Check Runs.
MIN_RUN_DEADLINE = 600
MAX_RUN_DEADLINE = 720 * 60
DEFAULT_RUN_DEADLINE = 7200
RUN_MARGIN_SECONDS = 600


PREPARED_KEY_SECONDS = 300


def claim_labels(claim: dict) -> Optional[str]:
    """The claim's `labels` (the pull request's live labels, carried from the admission by the operations instance) as
    the comma-separated `MW_BUILD_LABELS` a stage's selectors pass to `ci-change-set.py --labels`; None when the claim
    carries none or a malformed list — the variable is then left UNSET, so the stage says it was not told rather than
    classifying an unknown set as known-none. An empty list is "" (known to be none)."""
    raw = claim.get("labels")
    # NO count cap: the claim's set is the UNION of every request naming the commit (one build per commit, however many
    # pull requests name it), so it may hold more than one pull request's 100 — dropping it would drop a `full-run`.
    # Each name is still a valid label name (validated per request by the instance as well).
    if not isinstance(raw, list):
        return None
    names = []
    for n in raw:
        if not isinstance(n, str) or not n.strip() or len(n) > 50 or "," in n or any(unicodedata.category(c) == "Cc" for c in n):
            return None
        names.append(n.strip())
    return ",".join(sorted(set(names)))


def run_budget_seconds(stages: list[dict], deadline: float, prepared: Sequence[dict] = (), trunk: bool = True,
                       warm_minutes: float = 0.0, slots: int = 1) -> int:
    """The run's effective budget: the selected stages' summed caps + each declared prepared input's build cap
    and TWO key-command allowances (a miss keys the run's tree and the default branch's tip, then builds ONE of
    them inside the run) + a margin, never past the deadline. On a NON-trunk run the build a miss runs may be the
    DEFAULT BRANCH's declaration, whose cap this run cannot know before it keys the tip — so it reserves the largest
    cap any declaration may carry, never this run's own (a 1-minute PR cap must not cut a valid 45-minute tip build)."""
    # The stages' caps as the host SCHEDULES them: their sum on one slot, the critical path through the slots with
    # fan-out (`planned_seconds`, the same `next_stages` rules the scheduler runs).
    total = planned_seconds(stages, max(1, slots)) + RUN_MARGIN_SECONDS
    total += sum((float(p.get("timeoutMinutes", 20)) if trunk else float(DEFAULT_STAGE_TIMEOUT_MINUTES)) * 60
                 + 2 * PREPARED_KEY_SECONDS for p in prepared)
    # the default branch's restore, when the warm NuGet cache does not hold its key yet (`Host.nuget_warm_plan`)
    total += float(warm_minutes) * 60
    return int(min(deadline, total))


def budget_minutes(seconds: float) -> int:
    """`budgetMinutes` for the plan: whole minutes, rounded UP, within the contract's 10..720."""
    return max(10, min(720, int(-(-seconds // 60))))

HEARTBEAT_SECONDS = 45     # < 60: the instance's lease lasts 10 min past the host's last batch


class EventSink:
    """Batches one stage's events and POSTs them as `{"host","seq","events"}`. Every batch is
    numbered 1, 2, … per stage (`seq`) and a RETRIED batch keeps its number, so the instance can
    drop a duplicate. An EMPTY batch is the heartbeat that keeps the claim's lease (`beat`). 409
    raises Lost; other failures are retried. Thread-safe: the run's heartbeat thread beats the
    sink the stage is writing to."""

    def __init__(self, api: Api, host: str, repo: str, sha: str, stage: str, channel: str,
                 log: Callable[[str], None], sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, claim_id: str = "", probe: Optional[str] = None) -> None:
        self.api, self.host, self.log, self.sleep, self.clock = api, host, log, sleep, clock
        self.claim_id = claim_id   # the assignment every batch belongs to
        self.path = events_path(repo, sha, stage)
        # A probe run's identity is (repo, sha, channel, probe): its id rides on every callback.
        # `channel` is a channel name, or a host's whole claim scope ({"version": v} on a versioned server).
        scope = dict(channel) if isinstance(channel, dict) else {"channel": channel}
        self.query = {**scope, **({"probe": probe} if probe else {})}
        self.buf: list[dict] = []
        self.last = clock()
        self.dropped = 0
        self.seq = 0               # the last batch number ACKNOWLEDGED
        self.inflight: Optional[list[dict]] = None    # the batch sent as seq+1, frozen until acknowledged
        self.lock = threading.RLock()

    def add(self, events: Iterable[dict]) -> None:
        with self.lock:
            for e in events:
                if e.get("kind") == "line":
                    e = dict(e, text=str(e.get("text", ""))[:MAX_LINE])
                self.buf.append(e)
            if len(self.buf) > 20 * BATCH_EVENTS:      # the instance is unreachable for a long time
                over = len(self.buf) - 20 * BATCH_EVENTS
                del self.buf[:over]
                self.dropped += over
            if len(self.buf) >= BATCH_EVENTS or (self.buf and self.clock() - self.last >= BATCH_SECONDS):
                self.flush(attempts=1)
            elif self.clock() - self.last >= HEARTBEAT_SECONDS:
                self.beat()

    def _send_inflight(self) -> int:
        """POST the ONE in-flight batch. It is frozen when first sent and re-sent byte-identical
        under the same `seq` until acknowledged — events added meanwhile wait for the NEXT seq, so a
        receiver that committed a batch whose acknowledgement was lost can never be handed a
        different payload under a number it already holds."""
        status, _ = self.api.call("POST", self.path, {"host": self.host, "claimId": self.claim_id,
                                                      "seq": self.seq + 1, "events": self.inflight}, self.query)
        if status == 409:
            # Not held by this host, or a batch of an EARLIER assignment (`stale-claim`): either way
            # the run is no longer this host's — stop, kill the child, post nothing more.
            raise Lost(f"{self.path}: the run is not this host's claim any more (409)")
        # 503 = the instance cannot ingest right now (e.g. it states no redaction terms — ingestion
        # fails closed): retryable, the SAME batch with the SAME seq, never a run failure.
        if 200 <= status < 300:
            self.seq += 1
            self.inflight = None
            self.last = self.clock()
        return status

    def beat(self) -> bool:
        """The heartbeat: when nothing was posted for HEARTBEAT_SECONDS, re-send the in-flight batch,
        else flush what is buffered, else send an EMPTY batch (a batch of its own, with its own seq)."""
        with self.lock:
            if self.clock() - self.last < HEARTBEAT_SECONDS:
                return True
            if self.inflight is None and self.buf:
                return self.flush(attempts=1)
            if self.inflight is None:
                self.inflight = []
            return 200 <= self._send_inflight() < 300

    def flush(self, attempts: int = 4) -> bool:
        """POST everything buffered, one frozen batch at a time. Only FAILED posts consume
        `attempts` — a long buffer that drains in many successful batches never runs out of retries.
        Returns True when nothing is held (no in-flight batch, nothing buffered)."""
        with self.lock:
            if self.inflight is None and not self.buf:
                return True
            if self.dropped:
                self.buf.insert(0, {"kind": "line", "text": f"{self.dropped} progress event(s) were dropped "
                                                            f"while the build API was unreachable"})
                self.dropped = 0
            delay = 2.0
            failures = 0
            while (self.inflight is not None or self.buf) and failures < attempts:
                if self.inflight is None:
                    self.inflight = self.buf[:BATCH_EVENTS]
                    del self.buf[:len(self.inflight)]
                status = self._send_inflight()
                if 200 <= status < 300:
                    continue
                failures += 1
                self.log(f"events POST {self.path} → {status}; batch {self.seq + 1} held, "
                         f"{len(self.buf)} more buffered (failure {failures}/{attempts})")
                if failures < attempts:
                    self.sleep(delay)
                    delay = min(delay * 2, 60.0)
            return self.inflight is None and not self.buf


class Heartbeat:
    """Keeps the claim's lease for the whole run — checkout included — by beating whichever sink
    the run currently writes to (`target`), every `period` seconds, from its own thread."""

    def __init__(self, period: float = 15.0) -> None:
        self.target: Optional[EventSink] = None
        self.period = period
        self.stop = threading.Event()
        # SET when a beat met the 409: the claim is gone. Work that posts nothing for minutes (a prepared
        # input's build, Deploy#49) polls it and ends at once, instead of finding out on its next post.
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self.stop.wait(self.period):
            t = self.target
            if t is None:
                continue
            try:
                t.beat()
            except Lost:
                self.lost.set()     # the run's own next post raises Lost too; nothing more is beaten for it
                return
            except Exception:       # a transient fault never kills the run; the next beat retries
                pass

    def __enter__(self) -> "Heartbeat":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=5)


# ───────────────────────────────── the plugins layer ──────────────────────────────────────

def stage_plugins(plugins: dict, cache: str, env: dict,
                  pull: Callable[[list[str]], int], post: Callable[[str, dict], dict] = None) -> str:
    """Stage the plugins publication named by `plugins.digest` into `<cache>/plugins/<hex>/`
    (content-addressed: an existing complete directory is reused, never re-pulled). Returns the
    staged directory; raises Refusal when it cannot be staged."""
    digest = str(plugins.get("digest") or "")
    if not DIGEST.match(digest):
        raise Refusal(f"hosts/inputs names a plugins digest {digest!r} that is not sha256:<64 hex>")
    root = os.path.join(cache, "plugins")
    final = os.path.join(root, digest.split(":", 1)[1])
    if os.path.isfile(os.path.join(final, ".complete")):
        return final
    registry = env.get("MW_PLUGINS_REGISTRY", "").strip()
    if not registry:
        raise Refusal("hosts/inputs names a plugins publication but MW_PLUGINS_REGISTRY is not set — "
                      "the host cannot stage it, so it will not claim")
    source = env.get("MW_PLUGINS_SOURCE", "plugins").strip() or "plugins"
    os.makedirs(root, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".staging-", dir=root)
    cmd = [env.get("ORAS", "oras"), "pull", f"{registry}/plugins/{source}@{digest}", "-o", tmp]
    auth_dir = tempfile.mkdtemp(prefix=".auth-", dir=root)
    try:
        config = acr_registry_config(registry, env, post or _post_form, auth_dir)
        if config:
            cmd += ["--registry-config", config]
        rc = pull(cmd)
    except Refusal:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(auth_dir, ignore_errors=True)      # the refresh token never outlives the pull
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise Refusal(f"pulling the plugins publication {digest} failed (oras exit {rc})")
    with open(os.path.join(tmp, ".complete"), "w") as fh:
        json.dump({k: plugins.get(k) for k in ("digest", "identity", "version", "sha")}, fh)
    try:
        os.rename(tmp, final)
    except OSError:
        # another replica staged the same digest first; its directory is the same content
        shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.isfile(os.path.join(final, ".complete")):
            raise Refusal(f"could not move the staged plugins publication into {final}")
    return final


# ───────────────────────────────── the plugins layer, H8: the registry's bundle index ───────

BUNDLE_HASH_FIELDS = ("bundleHash", "hash", "sha256", "contentHash")


def bundle_lines(index: dict) -> list[str]:
    """The index as the sorted `package@version[@bundle-hash]` lines the plugins KEY is the digest
    of. An entry without a package or a version is a Refusal: a key over a partial list would name
    a layer the host does not hold."""
    entries = index.get("bundles") if isinstance(index, dict) else None
    if entries is None and isinstance(index, dict):
        entries = index.get("Bundles")
    if not isinstance(entries, list):
        raise Refusal("the bundle index carries no `bundles` list")
    lines = []
    for e in entries:
        if not isinstance(e, dict):
            raise Refusal("a bundle index entry is not an object")
        pkg = e.get("plugin") or e.get("Plugin") or e.get("package")
        ver = e.get("version") or e.get("Version")
        if not isinstance(pkg, str) or not REPO.match(pkg) or not isinstance(ver, str) or not ver.strip() or "@" in ver:
            raise Refusal(f"a bundle index entry names no usable package@version ({str(e)[:120]})")
        h = next((e[f] for f in BUNDLE_HASH_FIELDS if isinstance(e.get(f), str) and e[f]), None)
        lines.append(f"{pkg}@{ver.strip()}" + (f"@{h}" if h else ""))
    return sorted(set(lines))


def plugins_key_of(lines: list[str]) -> str:
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


# A DISPLACED cache entry (`.stale-*`, renamed aside by `_swap_into_place`) may still be bind-mounted by a running
# sandbox on another replica (`stage_readonly`): the rename keeps that mount on the moved inode, but deleting the files
# would pull them out from under the build (Deploy#46). It is therefore kept until no run that could have mounted it
# can still be alive — the longest run a host may be configured for, plus the run margin — and swept after.
STALE_RECLAIM_SECONDS = MAX_RUN_DEADLINE + RUN_MARGIN_SECONDS


def sweep_displaced(parent: str, now: Optional[float] = None, grace: float = STALE_RECLAIM_SECONDS) -> list[str]:
    """Remove the `.stale-*` entries under `parent` displaced longer than `grace` seconds ago (their mtime is stamped
    when they are displaced). Returns what was removed. Never raises: a sweep is housekeeping, not a verdict."""
    now = time.time() if now is None else now
    removed = []
    try:
        names = os.listdir(parent)
    except OSError:
        return removed
    for n in names:
        p = os.path.join(parent, n)
        if not n.startswith(".stale-") or os.path.islink(p) or not os.path.isdir(p):
            continue
        try:
            if now - os.stat(p).st_mtime < grace:
                continue
        except OSError:
            continue
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p)
    return removed


def _swap_into_place(tmp: str, final: str, intact: Callable[[str], bool], lock_fn: Optional[Callable] = None) -> None:
    """Move a freshly built `tmp` to `final`, REPLACING a damaged `final` (a populated directory a plain rename
    cannot overwrite): the damaged one is renamed aside first and RECLAIMED LATER (`sweep_displaced`), never deleted
    while a running sandbox on another replica may still read it. Validation and publication are
    SERIALISED per cache entry (an exclusive lock on `.<name>.lock` beside it), and `final` is re-validated under
    that lock: when another replica installed an intact entry while this one fetched, `tmp` is discarded and the
    installed entry is never moved aside — a running sandbox may bind-mount it. When the lock CANNOT be taken
    (a shared-cache filesystem without flock), only the non-destructive steps run: an intact entry is kept and an
    absent one is renamed into place, but a damaged one is never moved aside — that is a Refusal, because without
    exclusion two replicas could each judge it damaged and the slower would remove the faster one's install."""
    lock = os.path.join(os.path.dirname(final), "." + os.path.basename(final) + ".lock")
    with open(lock, "a") as lk:
        locked = (lock_fn or _flock)(lk)
        if intact(final):
            shutil.rmtree(tmp, ignore_errors=True)
            return
        try:
            os.rename(tmp, final)
            return
        except OSError:
            pass
        if not locked:
            shutil.rmtree(tmp, ignore_errors=True)
            raise Refusal(f"{final} is damaged, and its lock could not be taken on this cache: not replacing it "
                          "without exclusion (another replica may be installing it)")
        # Under the lock: reclaim what was displaced long enough ago that no run can still read it.
        sweep_displaced(os.path.dirname(final))
        stale = tempfile.mkdtemp(prefix=".stale-", dir=os.path.dirname(final))
        os.rmdir(stale)
        try:
            # The displacement time, which the sweep's grace counts from, is stamped BEFORE the rename: a sweep run
            # under ANOTHER entry's lock may scan the parent the instant the `.stale-*` name exists, and must never
            # see it carrying the entry's old mtime (a rename does not change it) — it would delete it at once.
            os.utime(final)
            os.rename(final, stale)
        except OSError:
            pass
        try:
            os.rename(tmp, final)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)


def bundle_intact(final: str, line: str) -> bool:
    """A cached bundle is still INSTALLED: its `.complete` names this very line and its package directory exists."""
    pkg = line.split("@")[0]
    try:
        with open(os.path.join(final, ".complete")) as fh:
            marked = fh.read().strip()
    except OSError:
        return False
    return marked == line and os.path.isdir(os.path.join(final, pkg)) and not os.path.islink(os.path.join(final, pkg))


def stage_bundle_set(lines: list[str], cache: str, fetch: Callable[[str, str, str], int]) -> str:
    """Every bundle the index names, in the content-addressed cache `<cache>/bundles/<h>/<package>/`
    (h = the line's digest, so a re-published version with a new hash is a new entry and an unchanged
    one is never fetched again), and the SET as `<cache>/plugin-sets/<key-hex>/<package>` links.
    `fetch(package, version, out)` is `module-fetch`; returns the set directory. A damaged bundle or set on
    the shared cache (a missing package directory, link or `.complete`) is REPAIRED — fetched or linked
    again and swapped into place — and the returned layer is validated (`layer_intact`): a layer that
    is still not intact is a Refusal, never recorded and never claimed on."""
    root = os.path.join(cache, "bundles")
    os.makedirs(root, exist_ok=True)
    places = {}
    for line in lines:
        pkg, ver = line.split("@")[0], line.split("@")[1]
        final = os.path.join(root, hashlib.sha256(line.encode()).hexdigest()[:32])
        if not bundle_intact(final, line):
            tmp = tempfile.mkdtemp(prefix=".fetch-", dir=root)
            rc = fetch(pkg, ver, tmp)
            if rc != 0 or not os.path.isdir(os.path.join(tmp, pkg)):
                shutil.rmtree(tmp, ignore_errors=True)
                raise Refusal(f"module-fetch {pkg}@{ver} failed (exit {rc}) — not claiming on a partial plugins layer")
            with open(os.path.join(tmp, ".complete"), "w") as fh:
                fh.write(line + "\n")
            _swap_into_place(tmp, final, lambda f, ln=line: bundle_intact(f, ln))
            if not bundle_intact(final, line):
                raise Refusal(f"could not move the fetched bundle {line} into the cache")
        places[pkg] = os.path.join(final, pkg)
    key = plugins_key_of(lines)
    sets = os.path.join(cache, "plugin-sets")
    os.makedirs(sets, exist_ok=True)
    set_dir = os.path.join(sets, key.split(":", 1)[1])
    if not layer_intact(set_dir, cache):
        tmp = tempfile.mkdtemp(prefix=".set-", dir=sets)
        for pkg, place in places.items():
            os.symlink(place, os.path.join(tmp, pkg))
        with open(os.path.join(tmp, ".complete"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        _swap_into_place(tmp, set_dir, lambda d: layer_intact(d, cache))
    if not layer_intact(set_dir, cache):
        raise Refusal(f"the plugins layer {key[:19]}… is not intact on the cache after staging — not claiming on it")
    return set_dir


def layer_intact(set_dir: Optional[str], cache: Optional[str] = None) -> bool:
    """A staged plugins layer is still INSTALLED: its set's `.complete` manifest lists the bundle lines, and for
    EVERY line the set holds the package link, resolving to that line's own content-addressed bundle
    (`<cache>/bundles/<digest>/<package>`) whose `.complete` names the line — and the set holds nothing else. A
    layer missing a plugin (a removed link, a removed package directory, an evicted bundle) or a replaced
    volume is not, and is installed again from the index whatever the hand-over key says."""
    if not set_dir:
        return False
    try:
        with open(os.path.join(set_dir, ".complete")) as fh:
            lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
        present = {n for n in os.listdir(set_dir) if not n.startswith(".")}
    except OSError:
        return False
    # The manifest must be the one this set's content-addressed NAME is the key of: an emptied (or edited) manifest
    # of a non-empty set is not intact, while the legitimate EMPTY set (an index with `bundles: []`) is.
    if os.path.basename(os.path.normpath(set_dir)) != plugins_key_of(lines).split(":", 1)[1]:
        return False
    root = os.path.realpath(os.path.join(cache or os.path.dirname(os.path.dirname(set_dir)), "bundles"))
    pkgs = set()
    for line in lines:
        pkg = line.split("@")[0]
        pkgs.add(pkg)
        link = os.path.join(set_dir, pkg)
        final = os.path.join(root, hashlib.sha256(line.encode()).hexdigest()[:32])
        if not os.path.islink(link) or os.path.realpath(link) != os.path.realpath(os.path.join(final, pkg)):
            return False
        if not bundle_intact(final, line):
            return False
    return present == pkgs


def layer_record_path(cache: str, identity: str) -> str:
    """Where the INSTALLED plugins layer for one framework identity is recorded on the shared cache, so
    the next host (a new replica, a restart, a scale-up from zero) starts with it installed."""
    return os.path.join(cache, "plugin-sets", "current-" + hashlib.sha256(identity.encode()).hexdigest()[:16] + ".json")


def read_layer_record(cache: str, identity: str) -> Optional[dict]:
    """The recorded layer for `identity`, or None when there is none, it is unreadable, it names
    another identity, or the layer it names is no longer intact on the cache."""
    try:
        with open(layer_record_path(cache, identity)) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("identity") != identity or not isinstance(rec.get("key"), str):
        return None
    d = rec.get("dir")
    if not isinstance(d, str) or os.path.realpath(d) != os.path.realpath(os.path.join(cache, "plugin-sets", rec["key"].split(":", 1)[-1])):
        return None
    return rec if layer_intact(d, cache) else None


def write_layer_record(cache: str, identity: str, key: str, set_dir: str, modules_key: Optional[str]) -> None:
    """Record the installed layer atomically (write + rename): a reader sees the old record or the new."""
    path = layer_record_path(cache, identity)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".current-", dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        json.dump({"identity": identity, "key": key, "dir": set_dir, "modulesKey": modules_key}, fh)
    os.replace(tmp, path)


# ───────────────────────────────── the host ───────────────────────────────────────────────

class Fatal(Exception):
    """The HOST is no longer fit to claim (no sandbox, a namespace that will not empty). Exit
    non-zero; Kubernetes replaces the container. Never caught into another claim."""


def api_url_acceptable(url: str) -> bool:
    """https anywhere; plain http only to a cluster-local host (no dot, *.svc, *.cluster.local,
    localhost) — the same rule the chart's `memex.buildHosts` gate applies."""
    p = urllib.parse.urlparse(url or "")
    host = (p.hostname or "").lower()
    if p.scheme == "https" and host:
        return True
    return p.scheme == "http" and bool(host) and (
        "." not in host or host.endswith(".svc") or host.endswith(".cluster.local"))


def read_worker_token(env: dict) -> str:
    """The worker token, from the mounted Secret FILE (`BUILD_TOKEN_FILE`). Never from the
    environment: an env var is inherited by anything the host starts, and a file is not."""
    if env.get("BUILD_TOKEN"):
        raise Refusal("BUILD_TOKEN is set in the host's environment — the worker token must be a mounted "
                      "file (BUILD_TOKEN_FILE), never an environment variable a process could inherit")
    path = env.get("BUILD_TOKEN_FILE", "/run/build-worker/token")
    try:
        with open(path) as fh:
            tok = fh.read().strip()
    except OSError as e:
        raise Refusal(f"the worker token file {path} is unreadable: {e}")
    if not tok:
        raise Refusal(f"the worker token file {path} is empty")
    return tok


# What a stage process may see of the host's environment. EVERYTHING else is cleared
# (`bwrap --clearenv`); the run's own facts are added explicitly by the host.
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TZ", "DOTNET_ROOT", "DOTNET_CLI_TELEMETRY_OPTOUT", "DOTNET_NOLOGO",
             "DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "SSL_CERT_FILE", "SSL_CERT_DIR", "MeshWeaverRefs",
             "MW_BUILD_GATE_HOST", "MW_BUILD_PORTAL_DOTNET", "MW_BUILD_MODULE_LIBS", "MW_BUILD_EXTENSIONS",
             "MW_BUILD_COMPILE_REFS",
             # The image's headless browser (deploy/build-host/Dockerfile): core's `test` shards export
             # MESHWEAVER_CHROMIUM_PATH for the tests, so the renderer resolves THAT binary, never another
             # browser on the machine; a stage gets the same three facts the image declares.
             "MESHWEAVER_CHROMIUM_PATH", "CHROME_BIN", "PLAYWRIGHT_BROWSERS_PATH")
SCRATCH = "/scratch"
# Where a RUN's checkout and the warm main tree appear inside the sandbox: ONE fixed path, so every absolute path MSBuild
# records in obj/ (project.assets.json, the reference caches, nuget.g.props) is the same in the warm build and in every
# run that copies its obj/ and bin/. At the host's per-run path (`<work>/<repo>-<sha>-<random>/src`) each run would
# restore and rebuild everything the warm tree had built (Plugins Hosting/BuildQueue.md §7.2).
SANDBOX_CHECKOUT = "/workspace/src"
# The writable NuGet package folder, at ONE fixed path inside the sandbox for the same reason: restore records the
# package roots (NUGET_PACKAGES and the fallback folders) in project.assets.json and nuget.g.props, so a run whose
# package folder sits at another path than the warm build's re-restores every project the warm tree had restored.
SANDBOX_NUGET = "/workspace/nuget"

# THE COMPILE REFERENCE SET, PREPARED ONCE PER ROLL (the `portal` init container of
# deploy/helm/templates/build-host/build-host.yaml): the portal's /app plus the image's SHARED
# FRAMEWORKS under `shared-frameworks/` — the exact shape of CI's `refs/` (MeshWeaver.Plugins ci.yml,
# "Take the framework assemblies from the platform image") and of `fetch-refs.py`'s cache. core's
# compile-check.py REFUSES (exit 4) a flat image set without System.Private.CoreLib.dll: /app's
# assemblies are container-built and bind CoreLib's identity directly, which the SDK ref pack cannot
# resolve, so every NodeType would fail CS0012 naming the CONTENT (#1032, #1911). `MeshWeaverRefs`
# stays the portal's /app as shipped (a stage's `--app`); this is the compile-check's `--refs`.
# Measured: the first probe of MeshWeaver.Plugins on build-latest (platform 3.1.11832) handed
# `--refs /portal/app` and compile-check was refused in 0 s. A host whose set is incomplete does not
# START — a stage never prepares it per run.
COMPILE_REFS_MARKER = "System.Private.CoreLib.dll"
COMPILE_REFS_SHARED = "shared-frameworks"
COMPILE_REFS_RX = "System.Reactive.dll"


# Compiler TOOLING, never a reference: core's compile-check.py (`is_reference_assembly`) drops every
# assembly under one of these directories before it looks for Rx or CoreLib, so a set whose only
# System.Reactive.dll sits there is refused by compile-check (exit 4). The preflight applies the SAME
# exclusion, or it accepts a set that is red later, never green (MeshWeaver.Deploy#58).
COMPILE_REFS_TOOLING_DIRS = frozenset({"sdk-generators", "analyzers"})


def _reference_dll_present(root: str, name: str) -> bool:
    """True iff `name` exists under `root` outside every compiler-tooling directory."""
    for _here, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in COMPILE_REFS_TOOLING_DIRS]
        if name in files:
            return True
    return False


def _paths_overlap(a: str, b: str) -> bool:
    a, b = os.path.normpath(a), os.path.normpath(b)
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def compile_refs_refusal(refs: str) -> Optional[str]:
    """Why `refs` is not a complete compile reference set, or None. The same test core's
    compile-check.py applies (`short_reference_set_refusal`): the image's MeshWeaver assemblies AND
    System.Private.CoreLib.dll under `shared-frameworks/`, each counted only OUTSIDE compiler-tooling
    directories. Also refused: a set that overlaps a stage's WRITABLE scratch ($SCRATCH/home, tmp,
    nuget) — it is bound read-only into every sandbox AFTER those are created, so every stage that
    writes a temp file or HOME fails after the host started (MeshWeaver.Deploy#58). Pure apart from
    reading the tree."""
    for writable in (f"{SCRATCH}/home", f"{SCRATCH}/tmp", f"{SCRATCH}/nuget"):
        if _paths_overlap(refs, writable):
            return (f"MW_BUILD_COMPILE_REFS={refs} overlaps the stages' writable {writable} — it is bound READ-ONLY "
                    f"into every sandbox over that directory; stage the set elsewhere")
    if not os.path.isdir(refs):
        return f"MW_BUILD_COMPILE_REFS={refs} is not a directory"
    if not any(n.startswith("MeshWeaver.") and n.endswith(".dll") for n in os.listdir(refs)):
        return f"MW_BUILD_COMPILE_REFS={refs} carries no MeshWeaver.*.dll — it is not the portal's /app"
    shared = os.path.join(refs, COMPILE_REFS_SHARED)
    if not _reference_dll_present(shared, COMPILE_REFS_MARKER):
        return (f"MW_BUILD_COMPILE_REFS={refs} carries NO shared frameworks ({COMPILE_REFS_SHARED}/**/{COMPILE_REFS_MARKER} "
                f"is missing) — core's compile-check.py refuses such a set (exit 4); the portal init container must "
                f"stage /usr/share/dotnet/shared beside /app")
    # core's short_reference_set_refusal ALSO refuses a set without Rx (every MeshWeaver assembly binds it).
    if not _reference_dll_present(refs, COMPILE_REFS_RX):
        return (f"MW_BUILD_COMPILE_REFS={refs} carries NO {COMPILE_REFS_RX} — core's compile-check.py refuses such a set "
                f"(exit 4): every MeshWeaver assembly binds Rx, and the portal's /app ships it")
    return None

# The platform's canonical checkers a repository's stages run (MeshWeaver.Plugins
# `scripts/canonical-compile-check.py`, `scripts/check-scripts.py`): the image ships core's
# `compile-check.py` beside this runner, AT THE SET'S COMMIT (deploy/build-host/Dockerfile), and a
# stage is told where through `MW_PLATFORM_SCRIPTS`. A sandbox holds no GitHub credential, so
# without it the launcher's fallback fetch fails and the stage is red. /opt is bound read-only.
PLATFORM_SCRIPTS = ("compile-check.py",)


def platform_scripts_env(scripts_dir: str) -> dict:
    """`{"MW_PLATFORM_SCRIPTS": scripts_dir}` when every canonical checker is there, `{}` otherwise —
    never an empty value, which the launcher would read as "fetch it" and fail on, unexplained."""
    ok = all(os.path.isfile(os.path.join(scripts_dir, n)) for n in PLATFORM_SCRIPTS)
    return {"MW_PLATFORM_SCRIPTS": scripts_dir} if ok else {}


def sandbox_env(host_env: dict, extra: dict, cache: str) -> dict:
    """The explicit allow-list a stage gets. A key that looks like a credential is refused even when
    the host itself adds it — the worker token can never reach a child, by construction."""
    env = {k: host_env[k] for k in ENV_ALLOW if host_env.get(k)}
    env.update({"HOME": f"{SCRATCH}/home", "TMPDIR": f"{SCRATCH}/tmp",
                # NuGet writes its package folder; the shared cache is READ-ONLY, so it is the
                # fallback folder and the writable folder is the stage's own scratch.
                "NUGET_PACKAGES": f"{SCRATCH}/nuget", "NUGET_FALLBACK_PACKAGES": os.path.join(cache, "nuget")})
    env.update(extra)
    leaked = [k for k in env if SECRET_ENV.search(k)]
    if leaked:
        raise Refusal(f"refusing to hand a stage credential-shaped variables: {sorted(leaked)}")
    return env


STAGE_NETWORK_ENV = "MW_BUILD_STAGE_NETWORK"
STAGE_NETWORKS = ("open", "offline")


def offline_restore_env(cache: str) -> dict:
    """What an OFFLINE run stage restores from: the prepared package folder as NuGet's ONLY source. MSBuild reads an
    environment variable as a property, so `RestoreSources` replaces every source a NuGet.Config names — a pull request
    that adds a feed reaches nothing. The folder is the global-packages layout NuGet also reads as a local feed. Pure."""
    return {"RestoreSources": os.path.join(cache, "nuget")}


# ─── THE PREPARED HOST, not a prepared run (MeshWeaver.Plugins Hosting/StandingBuildHosts.md) ─────────
# What the portal-host lane reads besides the reference set, installed in the image BY COMMIT
# (deploy/build-host/Dockerfile) and bound read-only into every stage (/opt is system, /tester a staged
# image root): core's source at the set's commit (the test-support projects no image ships), this
# repository's portal chart (the chart guards) and the tester's own /app (its deps.json). A fact the
# image does not carry is simply not handed — the stage that needs it is red naming the variable.
PREPARED_ROOT = "/opt/build-host"
TESTER_APP = "/tester"


def prepared_env(root: str = PREPARED_ROOT, tester: str = TESTER_APP) -> dict:
    env = {}
    platform_src = os.path.join(root, "platform-src")
    if os.path.isfile(os.path.join(platform_src, ".source-commit")) and os.path.isfile(os.path.join(platform_src, "Directory.Build.props")):
        env["MW_PLATFORM_SOURCE"] = platform_src
    deploy_src = os.path.join(root, "deploy-src")
    if os.path.isfile(os.path.join(deploy_src, "deploy", "helm", "Chart.yaml")):
        env["MW_DEPLOY_SOURCE"] = deploy_src
    if os.path.isfile(os.path.join(tester, "mw-plugin-test.dll")):
        env["MW_BUILD_TESTER_APP"] = tester
    # THE PREPARED TEST CLUSTER (deploy/build-host/host-postgres.sh, built at image build): UTF8 with a
    # pgvector template. A stage that needs PostgreSQL starts a COPY of it inside its own sandbox —
    # `eval "$(bash "$MW_TEST_PG_SERVER" start "$MW_TEST_PG_SNAPSHOT" <dir>)"` — never an initdb, never
    # a container; every suite database is a clone of the template (MW_TEST_PG_TEMPLATE).
    snapshot, server = os.path.join(root, "pg-snapshot"), os.path.join(root, "host-postgres.sh")
    if os.path.isfile(os.path.join(snapshot, "ready")) and os.path.isfile(server):
        env["MW_TEST_PG_SNAPSHOT"] = snapshot
        env["MW_TEST_PG_SERVER"] = server
    return env


ARTIFACT_UNIT_MAX = 200   # the store's receipt locator holds 512 characters; the repo, sha, run, stage and hash take ~200


def artifact_unit(unit: str) -> str:
    """`unit` as handed over: unchanged when it fits; otherwise its file name is shortened DETERMINISTICALLY — a prefix,
    `~`, the first 12 hex of the full name's SHA-256, the extension kept — so the receipt's locator stays inside the
    store's 512 characters (a test log named after a long test once took 513 and its hand-over was refused). Directories
    are kept as they are, so a consumer's unit (`gate-log/gate.log`) is never renamed. Pure."""
    if len(unit) <= ARTIFACT_UNIT_MAX:
        return unit
    head, _, name = unit.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 12:
        stem, ext = name, ""
    tag = hashlib.sha256(unit.encode()).hexdigest()[:12]
    room = max(16, ARTIFACT_UNIT_MAX - len(head) - 1 - len(tag) - 2 - len(ext))
    short = f"{stem[:room]}~{tag}" + (f".{ext}" if ext else "")
    out = f"{head}/{short}" if head else short
    if len(out) > ARTIFACT_UNIT_MAX:
        # the DIRECTORY itself is too long: keep only the unit's top directory (what consumers group by) and the hash
        top = unit.split("/", 1)[0][:64]
        out = f"{top}/~{hashlib.sha256(unit.encode()).hexdigest()[:24]}" + (f".{ext}" if ext else "")
    return out


def sandbox_readonly_roots(env: dict) -> list[str]:
    """The host-side roots every stage's sandbox binds READ-ONLY: the tester, the portal's /app, the compile
    reference set (wherever MW_BUILD_COMPILE_REFS points — a path handed to a stage must be visible in it),
    and the staged image roots."""
    return [p for p in (env.get("MW_BUILD_GATE_HOST"), env.get("MeshWeaverRefs"), env.get("MW_BUILD_COMPILE_REFS"),
                        "/portal", "/tester", "/tools", "/core-tests") if p]


def outer_ids(env: dict) -> Optional[tuple]:
    """The pod's uid/gid when this runner lives in its own user namespace (`enter_run_namespace`), else None."""
    if env.get(IN_NS_ENV) != "1":
        return None
    u, g = str(env.get("MW_BUILD_OUTER_UID", "")), str(env.get("MW_BUILD_OUTER_GID", ""))
    return (u, g) if u.isdigit() and g.isdigit() else None


class Sandbox:
    """`bwrap` per stage: user, PID, mount, IPC and UTS namespaces; `--die-with-parent`; a fresh
    /proc; `--clearenv` + the allow-list; the system and the caches bound READ-ONLY; writable only
    the run's checkout, its outputs directory and a tmpfs scratch. No network namespace — a build
    restores packages and fetches sources; the NetworkPolicy bounds where it can go."""

    SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt")

    def __init__(self, bwrap: str, readonly: Iterable[str], ids: Optional[tuple] = None) -> None:
        self.bwrap = bwrap
        self.readonly = tuple(readonly)
        # inside the runner's OWN user namespace (run overlay) this process is uid 0: a stage keeps the pod's uid/gid
        self.ids = ids

    def argv(self, command: str, checkout: str, outputs: str, env: dict,
             readonly: Iterable[str] = (), writable: Iterable[str] = (), mount: Optional[str] = None,
             binds: Iterable[tuple[str, str]] = (), network: bool = True) -> list[str]:
        """`readonly` are the cache entries THIS stage may read (its own repository's mirror and the
        dependency artifacts it declares) — never the cache root; `writable` the run's extra output
        directories (artifacts) beside the checkout and the outputs. `mount` is where the checkout appears
        inside the sandbox (SANDBOX_CHECKOUT for a run's stages and the warm build); absent, its own path. `binds` are
        further WRITABLE (host path, path inside) pairs — the package folder at SANDBOX_NUGET. `network=False` gives the
        stage its OWN network namespace (`--unshare-net`: loopback only, so a stage's own Postgres still answers), so a
        download a run was not prepared for fails at once naming its host, never as a 60 s timeout mid-test."""
        inside = mount or checkout
        a = [self.bwrap, "--die-with-parent", "--new-session",
             "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
             *([] if network else ["--unshare-net"]),
             "--clearenv"]
        if self.ids:
            a[a.index("--unshare-user") + 1:a.index("--unshare-user") + 1] = ["--uid", str(self.ids[0]), "--gid", str(self.ids[1])]
        for k in sorted(env):
            a += ["--setenv", k, env[k]]
        # The tmpfs mounts come FIRST: bwrap applies mounts in order, so a tmpfs laid after a bind hides
        # whatever was bound beneath it — a read-only root under /tmp or /scratch would pass preflight
        # and then be invisible to the stage.
        a += ["--tmpfs", "/tmp", "--tmpfs", SCRATCH,
              "--dir", f"{SCRATCH}/home", "--dir", f"{SCRATCH}/tmp", "--dir", f"{SCRATCH}/nuget"]
        for p in self.SYSTEM_RO + self.readonly + tuple(readonly):
            a += ["--ro-bind-try", p, p]
        a += ["--proc", "/proc", "--dev", "/dev",
              "--bind", checkout, inside, "--bind", outputs, outputs]
        for w in writable:
            a += ["--bind", w, w]
        for host_path, in_path in binds:
            a += ["--bind", host_path, in_path]
        a += ["--chdir", inside, "--", "/bin/bash", "-c", command]
        return a

    def probe(self, run: Callable[[list[str]], tuple[int, str]]) -> None:
        """Create the namespaces once at startup. If the node does not allow them, the host REFUSES
        to start, naming why — it never runs a stage unsandboxed."""
        d = tempfile.mkdtemp(prefix="bwrap-probe-")
        try:
            rc, err = run(self.argv("test \"$$\" -le 3 && test ! -e /run/build-worker", d, d, {"PATH": "/usr/bin:/bin"}))
        except OSError as e:
            rc, err = 127, str(e)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if rc != 0:
            raise Fatal(f"the sandbox cannot be created on this node (bwrap exit {rc}: {err.strip()[-300:]}) — "
                        f"user/PID/mount namespaces are required; the host refuses to run any stage unsandboxed")


# The places PR code must NOT reach from a stage, probed from INSIDE a sandbox at host start
# (Doc/Architecture/BuildHosts §2): the instance metadata service (node-identity tokens), a
# cluster-private address, and the Kubernetes API. Any of them ANSWERING — any HTTP status, even a
# 401/403 — is a hole in the NetworkPolicy, and the host refuses to start naming it.
BOUNDARY_PROBES = (
    ("instance metadata (169.254.169.254)", "http://169.254.169.254/metadata/instance?api-version=2021-02-01", {"Metadata": "true"}),
    ("a cluster-private address", None, {}),          # MW_BUILD_BOUNDARY_PRIVATE_URL, default below
    ("the Kubernetes API (kubernetes.default.svc)", "https://kubernetes.default.svc/version", {}),
)
DEFAULT_PRIVATE_PROBE = "http://10.0.0.1/"
BOUNDARY_SCRIPT = r"""
import json, ssl, sys, urllib.request, urllib.error
out = {}
for name, url, headers in json.loads(sys.argv[1]):
    try:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=4, context=ctx)
        out[name] = "answered"
    except urllib.error.HTTPError as e:
        out[name] = "answered (HTTP %d)" % e.code
    except Exception as e:
        out[name] = "blocked (%s)" % type(e).__name__
print(json.dumps(out))
"""


def boundary_problems(results: dict) -> list[str]:
    """The probes that ANSWERED — each one a boundary the NetworkPolicy does not hold."""
    return sorted(f"{name}: {r}" for name, r in results.items() if str(r).startswith("answered"))


def boundary_check(sandbox, env: dict, run: Callable[[list[str]], tuple[int, str]]) -> None:
    """Probe the boundary from inside a sandbox (the place a stage would try from). The host REFUSES
    TO START when any probe answers, or when the probe itself cannot report — a check that could not
    run is not a check that passed."""
    probes = [(name, url or env.get("MW_BUILD_BOUNDARY_PRIVATE_URL") or DEFAULT_PRIVATE_PROBE, headers)
              for name, url, headers in BOUNDARY_PROBES]
    d = tempfile.mkdtemp(prefix="boundary-")
    try:
        argv = sandbox.argv("python3 -c " + shlex_quote(BOUNDARY_SCRIPT) + " " + shlex_quote(json.dumps(probes)),
                            d, d, {"PATH": "/usr/bin:/bin"})
        rc, out = run(argv)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    try:
        results = json.loads(out.strip().splitlines()[-1]) if rc == 0 and out.strip() else None
    except ValueError:
        results = None
    if not isinstance(results, dict) or len(results) != len(probes):
        raise Fatal(f"the boundary self-check could not run inside the sandbox (exit {rc}): {out.strip()[-300:]} — "
                    f"the host refuses to start on an unmeasured boundary")
    holes = boundary_problems(results)
    if holes:
        raise Fatal("the network boundary does not hold — from inside a stage's sandbox these ANSWERED: "
                    + "; ".join(holes) + ". The host refuses to start: fix the build-host NetworkPolicy "
                    "(169.254.0.0/16, 168.63.129.16 and the private ranges must be unreachable)")


def scoping_check(host, run: Callable[[list[str]], tuple[int, str]]) -> None:
    """A PUBLIC repository's sandbox must not be able to read a PRIVATE repository's mirror or
    cache entry — nor the cache root. Seeded entries for two scratch repositories are created in the
    real cache, a sandbox is built for the public one exactly as for a stage that declares every
    `needs`, and it must read its own entry and fail to read the other's and to list the root. Any
    other outcome — including a check that cannot run — and the host refuses to start."""
    cache = host.cache
    pub, priv = ".boundary-public", ".boundary-private"
    entries = {
        "pub-mirror": os.path.join(cache, "git", f"{pub}.git", "PUBLIC"),
        "priv-mirror": os.path.join(cache, "git", f"{priv}.git", "SECRET"),
        "priv-outputs": os.path.join(outputs_root(cache, priv), "SECRET"),
        "root-canary": os.path.join(cache, ".boundary-root-canary"),
    }
    for f in entries.values():
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "w") as fh:
            fh.write("boundary\n")
    d = tempfile.mkdtemp(prefix="scoping-", dir=host.work if os.path.isdir(host.work) else None)
    try:
        ro = host.stage_readonly(pub, {"needs": sorted(STAGE_NEEDS)})
        if any(os.path.realpath(p) in (os.path.realpath(cache), os.path.realpath(os.path.join(cache, "outputs")),
                                       os.path.realpath(os.path.join(cache, "git"))) for p in ro):
            raise Fatal(f"a stage would be bound a cache ROOT ({ro}) — the host refuses to start")
        script = (f"cat {shlex_quote(entries['pub-mirror'])} >/dev/null || {{ echo own-unreadable; exit 3; }}; "
                  f"if cat {shlex_quote(entries['priv-mirror'])} >/dev/null 2>&1; then echo private-mirror-readable; exit 4; fi; "
                  f"if cat {shlex_quote(entries['priv-outputs'])} >/dev/null 2>&1; then echo private-outputs-readable; exit 5; fi; "
                  f"if cat {shlex_quote(entries['root-canary'])} >/dev/null 2>&1; then echo cache-root-readable; exit 6; fi; echo scoped")
        rc, out = run(host.sandbox.argv(script, d, d, {"PATH": "/usr/bin:/bin"}, readonly=ro))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if rc != 0 or "scoped" not in out:
        reason = {3: "its own mirror is not readable", 4: "a PRIVATE repository's mirror is readable",
                  5: "a PRIVATE repository's compile outputs are readable", 6: "a file at the cache ROOT is readable"}.get(rc, out.strip()[-200:])
        raise Fatal(f"the cache-scoping self-check failed: from a public repository's sandbox, {reason} — "
                    f"the host refuses to start")


def shlex_quote(v: str) -> str:
    import shlex
    return shlex.quote(v)


def pid_namespace_leftovers(proc_root: str = "/proc") -> list[int]:
    """Processes visible to the host that live in ANOTHER pid namespace than the host's — what a
    finished stage left behind. The host runs one run at a time and starts nothing else in a
    namespace, so whenever no stage is in flight (with fan-out, siblings may be) this must be empty."""
    try:
        mine = os.readlink(os.path.join(proc_root, "self", "ns", "pid"))
    except OSError:
        return []
    out = []
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        try:
            ns = os.readlink(os.path.join(proc_root, name, "ns", "pid"))
        except OSError:
            continue
        if ns != mine and not _is_zombie(proc_root, name):
            out.append(int(name))
    return sorted(out)


CONTENT_KEY = re.compile(r"^[0-9a-f]{16,128}$")


CACHE_KEY_RECIPE = 1
CACHE_KEY_FILE = ".cache-key.json"



def _is_zombie(proc_root: str, pid: str) -> bool:
    """A zombie has already exited: it runs nothing and holds nothing but its process-table slot.
    A stage's sandbox processes are reparented to the host when bwrap exits, and the host runs as
    the container's PID 1 (`exec python3 build-run.py`), so they stay ZOMBIES until it reaps them
    (measured on the user-namespace pool, 2026-10-27: `bwrap` State Z, PPid 1, after the startup
    probe). Counting them as survivors made the host refuse to claim on every start."""
    try:
        # errors="replace": a task name may hold non-UTF-8 bytes, and the name is read before State
        # — a decode error must never abort cleanup (review of #7287).
        # newline="\n": the kernel ends lines with "\n" only, while a task name is chosen by the process
        # (PR_SET_NAME) and may hold "\r" — universal newlines would turn "\rState:\tZ\r" into a forged
        # State line ahead of the real one, and a live survivor would pass as a zombie (review of Deploy#28).
        with open(os.path.join(proc_root, pid, "status"), encoding="utf-8", errors="replace", newline="\n") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return line.split()[1:2] == ["Z"]
    except OSError:
        return False
    return False


def reap_children() -> int:
    """Reap every exited child without blocking — what PID 1 must do for the processes reparented
    to it. Returns how many were reaped."""
    reaped = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if pid == 0:
            return reaped
        reaped += 1

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(root: str, rel: str) -> str:
    """`rel` resolved under `root`, refusing an absolute path, a `..` escape or a symlink out."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        raise Refusal(f"{rel!r} is not a repository-relative path")
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(os.path.realpath(root).rstrip("/") + "/"):
        raise Refusal(f"{rel!r} escapes the checkout")
    if not os.path.isfile(full):
        raise Refusal(f"{rel!r} is not a file in the checkout")
    return full


def nodetype_cache_key(checkout: str, spec: dict, env: dict) -> str:
    """A NodeType's cross-run compile key: its DECLARED source set (each file's content hash), the
    reference-set identity (framework identity, portal digest, the plugins layer's key), the
    canonical compile-check OPTIONS and allow-list, and the compiler (the tester digest). An input
    the host cannot read is a Refusal — never a key over less than was compiled."""
    sources = spec.get("sources")
    if not isinstance(sources, list) or not sources:
        raise Refusal("a nodetype key names its sources")
    refset = {"frameworkIdentity": env.get("MW_BUILD_FRAMEWORK_IDENTITY", ""),
              "portalDigest": env.get("MW_BUILD_PORTAL_DIGEST", ""),
              "pluginsKey": env.get("MW_PLUGINS_KEY", "")}
    compiler = env.get("MW_BUILD_TESTER_DIGEST", "")
    if not refset["frameworkIdentity"] or not DIGEST.match(refset["portalDigest"]) or not DIGEST.match(compiler):
        raise Refusal("the reference-set identity (framework identity, portal and tester digests) is not known")
    options = spec.get("options", "")
    if not isinstance(options, str):
        raise Refusal("a nodetype key's options are the canonical compile-check option string")
    allow = spec.get("allowList")
    doc = {"kind": "nodetype", "recipe": CACHE_KEY_RECIPE,
           "sources": {rel: _sha256_file(_inside(checkout, rel)) for rel in sorted(set(sources))},
           "refset": refset, "options": options, "compiler": compiler,
           "allowList": {allow: _sha256_file(_inside(checkout, allow))} if allow else None}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def module_cache_key(checkout: str, spec: dict, env: dict,
                     run: Callable[[list[str]], tuple[int, str]] = None) -> str:
    """A module's cross-run key: core's own `module-build-key.py` (sources, the dependency closure,
    Directory.Build.props/.targets, Directory.Packages.props, the project file, the toolchain
    identity), run with the HOST's tester and portal digests — never a key the stage asserts."""
    entry = spec.get("entry")
    if not isinstance(entry, dict):
        raise Refusal("a module key names its matrix entry")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "module-build-key.py")
    argv = [sys.executable, script, "--root", checkout, "--entry", json.dumps(entry, sort_keys=True),
            "--tester-digest", env.get("MW_BUILD_TESTER_DIGEST", ""),
            "--platform-digest", env.get("MW_BUILD_PORTAL_DIGEST", ""),
            "--platform-ref", env.get("MW_BUILD_PLATFORM_REF", "")]
    if run is None:
        def run(a):
            r = subprocess.run(a, capture_output=True, text=True, timeout=300)
            return r.returncode, (r.stdout or "") if r.returncode == 0 else (r.stderr or "")
    rc, out = run(argv)
    key = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
    if rc != 0 or not CONTENT_KEY.match(key):
        raise Refusal(f"module-build-key.py could not key the module (exit {rc}): {out.strip()[-200:]}")
    return key


def cache_key_of(checkout: str, spec: object, env: dict, run=None) -> str:
    if not isinstance(spec, dict):
        raise Refusal(f"{CACHE_KEY_FILE} is not an object")
    if spec.get("kind") == "nodetype":
        return nodetype_cache_key(checkout, spec, env)
    if spec.get("kind") == "module":
        return module_cache_key(checkout, spec, env, run)
    raise Refusal(f"{CACHE_KEY_FILE} kind must be nodetype or module (got {spec.get('kind')!r})")


def refset_key(tester_digest: str, portal_digest: str) -> str:
    """The staged reference set's key: BOTH images' digests — the compiler (tester) and the
    reference assemblies (portal) — never one of them alone."""
    return hashlib.sha256(f"tester={tester_digest}\nportal={portal_digest}".encode()).hexdigest()[:40]


def outputs_root(cache: str, repo: str) -> str:
    """A repository's OWN compile-output cache: outputs are scoped by repository, so a stage of one
    repository can never be bound another's compiled bytes (a private repo's into a public run)."""
    return os.path.join(cache, "outputs", repo)


def promote_outputs(outputs: str, cache: str, log: Callable[[str], None],
                    verify: Callable[[str], Optional[str]] = None, repo: str = "") -> tuple[int, int, list[str]]:
    """Copy a TRUNK stage's content-addressed outputs (`<outputs>/<key>/…`) into the shared cache
    (`<cache>/outputs/<key>/`). A key already present is kept (same key ⇒ same content); a name
    that is not a content key is not promoted. Symlinks are copied as links, never followed.
    Returns (promoted, kept, refused names)."""
    root = outputs_root(cache, repo) if repo else os.path.join(cache, "outputs")
    promoted = kept = 0
    refused: list[str] = []
    if not os.path.isdir(outputs):
        return 0, 0, []
    for name in sorted(os.listdir(outputs)):
        src = os.path.join(outputs, name)
        if not CONTENT_KEY.match(name) or os.path.islink(src) or not os.path.isdir(src):
            refused.append(name)
            continue
        problem = verify(src) if verify else None
        if problem:
            log(f"cache: {name} NOT promoted — {problem}")
            refused.append(name)
            continue
        dst = os.path.join(root, name)
        if os.path.isdir(dst):
            kept += 1
            continue
        os.makedirs(root, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".promote-{name[:12]}-", dir=root)
        shutil.copytree(src, os.path.join(tmp, "c"), symlinks=True)
        try:
            os.rename(os.path.join(tmp, "c"), dst)
            promoted += 1
        except OSError:
            kept += 1          # another host promoted the same key first
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return promoted, kept, refused


def git_auth_env(token: Optional[str]) -> dict:
    """Git credentials through the ENVIRONMENT (never argv, never a URL, never the checkout)."""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"})
    return env


def default_checkout(repo: str, sha: str, refs: list, dest: str, token: Optional[str],
                     env: dict, log: Callable[[str], None], git: Callable[..., None] = None,
                     deadline: Optional[float] = None) -> None:
    """The HOST checks out (the stage never holds the installation token), through the shared git
    MIRROR `<cache>/git/<repo>.git`: `git clone --mirror` on first use, `git fetch` before every
    checkout, then a clone `--reference`-ing the mirror into the run's directory, so a run fetches
    only what the mirror lacks. The mirror is written by the host alone — it is bound READ-ONLY into
    every sandbox — and only from GitHub. Several replicas share it: an exclusive file lock
    serialises its updates."""
    base = env.get("MW_BUILD_GIT_BASE", "https://github.com/Systemorph").rstrip("/")
    url = f"{base}/{repo}.git"
    mirror_root = os.path.join(env.get("MW_BUILD_CACHE", "/cache"), "git")
    mirror = os.path.join(mirror_root, f"{repo}.git")
    genv = {**{k: env[k] for k in ("PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR") if env.get(k)},
            "HOME": os.path.dirname(dest), **git_auth_env(token)}

    def run_git(*args: str) -> None:
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            raise Refusal("the run's deadline passed during the checkout")
        try:
            # subprocess.run kills and reaps the child when the timeout expires
            r = subprocess.run(["git", *args], env=genv, capture_output=True, text=True, timeout=left)
        except subprocess.TimeoutExpired:
            raise Refusal(f"git {args[0] if args[0] != '-C' else args[2]} did not finish inside the run's deadline — killed")
        if r.returncode != 0:
            raise Refusal(f"git {args[0] if args[0] != '-C' else args[2]} failed (exit {r.returncode}): "
                          f"{(r.stderr or '').strip()[-400:]}")
    git = git or run_git
    wanted = [sha] + [r for r in (refs or []) if isinstance(r, str) and r]
    _update_mirror(repo, url, mirror_root, mirror, wanted, git, log)
    git("clone", "--no-checkout", "--quiet", "--reference", mirror, url, dest)
    git("-C", dest, "fetch", "--quiet", "origin", *wanted)
    git("-C", dest, "checkout", "--quiet", "--detach", sha)


def _update_mirror(repo: str, url: str, mirror_root: str, mirror: str, wanted: list, git: Callable[..., None],
                   log: Callable[[str], None]) -> None:
    """Create or fetch the shared mirror under its exclusive lock, then fetch the wanted refs into it."""
    with master_lock(mirror_root, repo):
        if not os.path.isfile(os.path.join(mirror, "HEAD")):
            tmp = tempfile.mkdtemp(prefix=f".{repo}-", dir=mirror_root)
            try:
                git("clone", "--mirror", "--quiet", url, os.path.join(tmp, "m"))
                os.rename(os.path.join(tmp, "m"), mirror)
                log(f"git mirror created: {mirror}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        else:
            # Said, not silent: on a cache that outlives the pod (a node hostPath, a leased Mac's volume) this line is the
            # evidence that the master was already there and only fetched (Plugins Hosting/BuildQueue.md §7.3b).
            started = time.monotonic()
            git("-C", mirror, "fetch", "--quiet", "--prune", "origin")
            log(f"git mirror fetched (existing): {mirror} in {time.monotonic() - started:.1f} s")
        # NO automatic gc on a master: runs borrow its objects through `alternates` (overlay and cold trees alike) with no
        # ref the master knows about, so a fetch's auto-gc / maintenance could prune what a running stage still reads
        git("-C", mirror, "config", "gc.auto", "0")
        git("-C", mirror, "config", "maintenance.auto", "false")
        # A PR head (refs/pull/N/head) or the exact sha may not be on a mirrored ref yet.
        git("-C", mirror, "fetch", "--quiet", "origin", *wanted)


# ───────────────────── one master, a worktree per deployment — nothing is ever copied (Plugins Hosting/BuildQueue.md §7.3b) ─────
#
# Maintainer, 2026-11-08: "we must see that we keep on file share without any copying" · "only git fetch / worktrees / etc"
# · "we can even have one master checkout" · "and then worktree per deployment". With MW_BUILD_WARM_WORKSPACE=true:
#   * the MASTER is the host's bare mirror per repository, `<cache>/git/<repo>.git` (`_update_mirror`), written only by
#     `git fetch`; ONE master lock (`master_lock`, the mirror's flock) serialises fetch, `git worktree add`, `remove`, `prune`;
#   * ONE persistent worktree per repository per HOST, kept across platform updates (Plugins Hosting/OneBuildTree.md):
#     `<cache>/deploy/<channel>-tree/<repo>`, a `git worktree add --detach` of the master. A new BUILD IDENTITY (`warm_key`:
#     a platform update, a new SDK) rebuilds that same tree WHOLE in place; a runner roll rebuilds nothing. Its single WRITER (an exclusive,
#     NON-blocking flock on the tree's lock — taken only while no reader holds it) advances it IN PLACE while idle:
#     `checkout --detach -f <tip>` (only changed files are rewritten), `clean` keeping obj/ and bin/, the tip's `warmBuild`,
#     then the integrity manifest and the state, before the lock is released. A failed build leaves it "not built";
#   * a RUN takes a SHARED non-blocking flock on that tree for its WHOLE life, verifies state and manifest and mounts the tree
#     as the read-only LOWER of an overlay (its own upper takes every write); its `.git` is made by git (`init`, alternates
#     to the master, `read-tree`), never copied — not even the index;
#   * the ONLY fallback (lock busy, nothing built, integrity refused, a host that cannot overlay) is a COLD `git worktree
#     add --detach` off the master, said loudly, removed and pruned under the master lock when the run ends.
# No byte of a source or build tree is ever copied (`no_copy_violations` is the self-test's gate); every run's plan line
# states `bytes copied = N`, measured (`copied_bytes`).
WARM_RETRY_SECONDS = 600.0   # a failed warm advancement is retried after this, never on every idle pass
WARM_KEEP = ("obj/", "bin/")
WARM_BUILD_MAX_MINUTES = 60
DEPLOY_DIR = "deploy"
TREE_SAMPLE = 32
PLATFORM_REPO = "MeshWeaver"


# ONE TREE, kept across core updates (Plugins Hosting/OneBuildTree.md, maintainer 2026-11-10: "we can keep the tree stable
# even across the core updates ==> it is like updating nuget packages" · "updating core means updating the compiler" · "we
# always ever need one tree, in case of platform update, we need whole rebuild"). The tree's DIRECTORY is named by the host's
# lane alone (`deployment_name`), so neither a new platform set, a new image digest nor a runner roll starts a second tree.
# What the tree's obj/ and bin/ were built BY is its BUILD IDENTITY (`warm_key`), recorded in the tree's state: a different
# identity is a COMPILER change and the writer rebuilds the whole tree IN PLACE (obj/ and bin/ of every project removed
# first, `advance_deployment_tree(whole=True)`), so no assembly built by the previous compiler survives.
#
# The identity is the platform set and what is genuinely NOT determined by it — nothing that a merge to this repository
# moves:
#   * the platform set (MW_BUILD_PLATFORM_VERSION), its framework identity and its tester and portal image digests: the
#     compiler and the reference set a build compiles against (determined by the set; listed so a re-sealed set with the same
#     version still rebuilds);
#   * the .NET SDKs the runner image carries: the floating `dotnet/sdk:10.0` base can move under an unchanged Dockerfile, and
#     the SDK is the compiler too;
#   * the CPU architecture (a property of the machine, never of the set);
#   * WARM_LAYOUT, bumped BY HAND for a change to this runner that changes what a tree's outputs mean (rare).
# NOT the runner's own code, NOT its build helpers' content and NOT the runner image's digest: each of those moved on every
# merge to this repository and threw every tree and every module output away (eiger 2026-11-09: 50 rolls in a day, no warm
# run; the module hubs 2026-11-10: no two runs on consecutive pods shared a fingerprint, so the store never answered a Hit).
# The module fingerprints carry the same identity (`Host.start_module_broker`), so the shared store's entries are reused
# across rolls and across hosts.
WARM_LAYOUT = "2026-11-10.2"
TOOLCHAIN_ENV = "MW_BUILD_TOOLCHAIN"


def build_toolchain(which: Callable[[str], Optional[str]] = shutil.which) -> str:
    """The compiler part of the build identity that the platform set does NOT determine: WARM_LAYOUT, the CPU architecture
    and the .NET SDKs installed. Never the runner's code, its helpers or the image digest (a Deploy merge moves those).
    Never raises: what cannot be read is named in the value, so it keys distinctly."""
    import platform as _platform
    parts = [f"layout={WARM_LAYOUT}", f"arch={_platform.machine()}"]
    dn = which("dotnet")
    try:
        sdks = sorted(os.listdir(os.path.join(os.path.dirname(os.path.realpath(dn)), "sdk"))) if dn else ["no-dotnet"]
    except OSError:
        sdks = ["unreadable"]
    parts.append("sdk=" + ",".join(sdks))
    return ";".join(parts)


def warm_key(env: dict, identity: str) -> str:
    """The BUILD IDENTITY of a tree (what built its obj/ and bin/): the platform set, the framework identity, the tester and
    portal images a build compiles against, and `build_toolchain` (SDKs, architecture, WARM_LAYOUT). It names no directory
    — a change rebuilds the one tree in place. Not the runner image's digest, not the runner's code. Pure."""
    parts = [f"platform={env.get('MW_BUILD_PLATFORM_VERSION', '')}", f"identity={identity}"]
    parts += [f"{k}={env.get(k, '')}" for k in ("MW_BUILD_TESTER_DIGEST", "MW_BUILD_PORTAL_DIGEST", TOOLCHAIN_ENV)]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def warm_enabled(env: dict) -> bool:
    return (env.get("MW_BUILD_WARM_WORKSPACE") or "").strip().lower() in ("1", "true", "yes")


def shared_tree_enabled(env: dict) -> bool:
    """The chart's `sharedTree` switch. Since §7.3b it selects the SAME master + deployment-tree path as the warm workspace
    alone (the deployment trees always live on the cache volume); kept as a switch, never a route to a copy."""
    return warm_enabled(env) and (env.get("MW_BUILD_SHARED_TREE") or "").strip().lower() in ("1", "true", "yes")


def deployment_name(lane: Optional[str]) -> str:
    """The ONE tree directory a host keeps per repository: its lane alone, never a key — a platform update rebuilds this
    tree in place (`warm_key` is recorded in its state), it never starts another. A path segment overlayfs can parse (no
    `,`, `:` or `\\`). Pure."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", (lane or "").strip()) or "default"
    return f"{safe[:48]}-tree"


STALE_DEPLOYMENT_SECONDS = 3600.0   # a LEGACY keyed deployment untouched this long is retired (a draining replica keeps it)


def _rm_tree(path: str) -> None:
    """Remove `path` whole, including overlay work dirs the kernel left mode 000. Never raises."""
    for d, dirs, _files in os.walk(path, onerror=lambda e: None):
        for n in dirs:
            try:
                os.chmod(os.path.join(d, n), 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _newest_mtime(path: str, depth: int = 3) -> float:
    """The newest mtime among `path` and its entries down to `depth` levels (state files, locks, trees). Never raises."""
    newest = 0.0
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return 0.0
    base = path.rstrip(os.sep).count(os.sep)
    for d, dirs, files in os.walk(path, onerror=lambda e: None):
        if d.count(os.sep) - base >= depth:
            dirs[:] = []
        for n in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(d, n)).st_mtime)
            except OSError:
                pass
    return newest


def retire_legacy_deployments(cache: str, lane: Optional[str], current: str, log: Callable[[str], None],
                             git: Callable[..., None], now: Optional[float] = None,
                             stale: float = STALE_DEPLOYMENT_SECONDS) -> list:
    """A ONE-TIME MIGRATION, nothing more: remove THIS lane's LEGACY keyed deployment directories
    (`<cache>/deploy/<lane>-<16-char key>`), left by the runner that kept a tree per platform set and image. Since one tree
    per host (Plugins Hosting/OneBuildTree.md) the current tree is `<lane>-tree` and a platform update rebuilds it in place,
    so there is no other key to retire — only what the old shape left on disk (eiger 2026-11-09: nine deployments, 92 GB in
    one day, the node at DiskPressure and 18 build pods evicted). A legacy deployment is removed only when (1) none of its
    repository locks is held — no reader runs on it, no writer advances it — taken exclusively and non-blocking for the
    whole removal, and (2) nothing in it changed for `stale` seconds, so a replica of the old runner still draining keeps
    its tree. Its worktrees are deregistered from the masters (`worktree prune` under the master lock), then the directory
    is deleted — a deletion, never a copy. Returns what was removed. Never raises."""
    import fcntl
    now = time.time() if now is None else now
    root = os.path.join(cache, DEPLOY_DIR)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", (lane or "").strip()) or "default"
    removed: list = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return removed
    for name in names:
        dep = os.path.join(root, name)
        # exactly `deployment_name`'s shape for THIS lane: the lane prefix and a 16-character key, nothing longer or shorter
        if name == current or not re.fullmatch(re.escape(safe[:48]) + r"-[0-9A-Za-z]{16}", name) or not os.path.isdir(dep) \
                or os.path.islink(dep):
            continue
        age = now - _newest_mtime(dep)
        if age < stale:
            continue
        locks_dir = os.path.join(dep, ".locks")
        held: list = []
        busy = False
        try:
            for ln in sorted(os.listdir(locks_dir)) if os.path.isdir(locks_dir) else []:
                fh = _try_lock(os.path.join(locks_dir, ln), fcntl.LOCK_EX | fcntl.LOCK_NB)
                if fh is None:
                    busy = True
                    break
                held.append(fh)
            if busy:
                log(f"deployment {name}: a legacy keyed deployment is still in use (a lock is held) — kept, retried when idle")
                continue
            repos = {d for d in os.listdir(dep) if REPO.match(d) and not d.startswith(".")}
            if os.path.lexists(os.path.join(dep, ".platform")):
                repos.add(PLATFORM_REPO)                   # read BEFORE the removal: the platform tree's master is pruned too
            _rm_tree(dep)
            for repo in sorted(repos):
                mirror = master_path(cache, repo)
                if os.path.isfile(os.path.join(mirror, "HEAD")):
                    try:
                        with master_lock(os.path.dirname(mirror), repo):
                            git("--git-dir", mirror, "worktree", "prune")
                    except (Refusal, OSError, subprocess.SubprocessError):
                        pass
            gone = not os.path.exists(dep)
            log(f"deployment {name}: a legacy keyed deployment {'retired' if gone else 'NOT fully removed'} (untouched "
                f"{int(age)} s, no lock held) — the one tree is {current}")
            if gone:
                removed.append(dep)
        except OSError as e:
            log(f"deployment {name}: not retired ({type(e).__name__}: {str(e)[:160]})")
        finally:
            for fh in held:
                fh.close()
    return removed


LEGACY_SHARED_DIR = "shared"     # §7.3's copied generations (`<cache>/shared/<repo>/gens/…`), superseded by §7.3b


def retire_legacy_shared(cache: str, log: Callable[[str], None]) -> bool:
    """Delete the §7.3 generations a host left on its cache (`<cache>/shared`): nothing reads them since §7.3b, and on a
    roll they would sit beside the new deployment trees (~18 GiB each for MeshWeaver.Plugins). A deletion, never a copy.
    Returns whether anything was removed. Never raises."""
    legacy = os.path.join(cache, LEGACY_SHARED_DIR)
    if not os.path.isdir(legacy) or os.path.islink(legacy):
        return False
    for d, dirs, _files in os.walk(legacy, onerror=lambda e: None):
        for n in dirs:
            try:
                os.chmod(os.path.join(d, n), 0o700)       # an overlay work dir the kernel made mode 000
            except OSError:
                pass
    shutil.rmtree(legacy, ignore_errors=True)
    gone = not os.path.exists(legacy)
    log(f"legacy shared tree: {legacy} {'removed' if gone else 'NOT fully removed'} — §7.3b deployment trees replace it")
    return gone


def master_path(cache: str, repo: str) -> str:
    return os.path.join(cache, "git", f"{repo}.git")


@contextlib.contextmanager
def master_lock(mirror_root: str, repo: str):
    """The ONE master lock of `repo`'s mirror: fetch, `worktree add`, `worktree remove` and `prune` all take it, so concurrent
    runs (and replicas sharing the cache) never race on the packs or `.git/worktrees`. Blocking; never nested."""
    os.makedirs(mirror_root, exist_ok=True)
    with open(os.path.join(mirror_root, f".{repo}.lock"), "w") as lock:
        _flock(lock)
        yield


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_manifest(tree: str, ident: str) -> dict:
    """What a built deployment tree IS: every file's content hash, and the stat fingerprint (size, mtime, ctime, inode) it had
    once hashed. An unprivileged process cannot set a ctime back, so a later write to any file of the tree — content, mode or
    a rewrite that restores the mtime — no longer matches; `digest` covers the whole list, `tree` names what it describes."""
    entries = []
    for d, dirs, files in os.walk(tree):
        dirs.sort()
        for n in sorted(files):
            full = os.path.join(d, n)
            rel = os.path.relpath(full, tree)
            st_ = os.lstat(full)
            if stat.S_ISLNK(st_.st_mode):
                entries.append([rel, "link", 0, st_.st_mtime_ns, st_.st_ctime_ns, st_.st_ino, os.readlink(full)])
            elif stat.S_ISREG(st_.st_mode):
                entries.append([rel, "file", st_.st_size, st_.st_mtime_ns, st_.st_ctime_ns, st_.st_ino, _file_sha256(full)])
        for n in list(dirs):
            if os.path.islink(os.path.join(d, n)):   # a symlinked directory: recorded as a link, never followed
                full = os.path.join(d, n)
                st_ = os.lstat(full)
                entries.append([os.path.relpath(full, tree), "link", 0, st_.st_mtime_ns, st_.st_ctime_ns, st_.st_ino, os.readlink(full)])
                dirs.remove(n)
    entries.sort(key=lambda e: e[0])
    return {"tree": ident, "entries": entries,
            "digest": hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()}


def verify_tree(tree: str, manifest_path: str, ident: str, sample: int = TREE_SAMPLE,
                rnd: Optional[random.Random] = None) -> Optional[str]:
    """None when `tree` is exactly what its writer hashed, else WHY not: no manifest, a manifest whose digest does not cover
    its entries, another tree's (or another tip's) manifest, a file added, missing or changed since (stat fingerprint), or —
    for a random sample of files — content that no longer hashes as recorded. Reads stat for every file, content for
    `sample`."""
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return "no integrity manifest (never built, or removed)"
    entries = m.get("entries") if isinstance(m, dict) else None
    if not isinstance(entries, list):
        return "the integrity manifest is malformed"
    if hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest() != m.get("digest"):
        return "the integrity manifest's digest does not cover its entries"
    if m.get("tree") != ident:
        return f"the manifest describes {m.get('tree')!r}, not {ident!r}"
    want = {e[0]: e for e in entries if isinstance(e, list) and len(e) == 7}
    seen = set()
    for d, dirs, files in os.walk(tree):
        names = list(files) + [n for n in dirs if os.path.islink(os.path.join(d, n))]
        dirs[:] = [n for n in dirs if not os.path.islink(os.path.join(d, n))]
        for n in names:
            full = os.path.join(d, n)
            rel = os.path.relpath(full, tree)
            e = want.get(rel)
            if e is None:
                return f"{rel} was added after the tree was built"
            try:
                st_ = os.lstat(full)
            except OSError:
                return f"{rel} cannot be read"
            if [st_.st_size if e[1] == "file" else 0, st_.st_mtime_ns, st_.st_ctime_ns, st_.st_ino] != e[2:6]:
                return f"{rel} changed after the tree was built"
            seen.add(rel)
    missing = sorted(set(want) - seen)
    if missing:
        return f"{missing[0]} was removed after the tree was built"
    files = [e for e in want.values() if e[1] == "file"]
    for e in (rnd or random.Random()).sample(files, min(sample, len(files))):
        try:
            if _file_sha256(os.path.join(tree, e[0])) != e[6]:
                return f"{e[0]}'s content no longer hashes as built"
        except OSError:
            return f"{e[0]} cannot be read"
    return None


def _try_lock(path: str, mode: int) -> Optional[object]:
    """`path` opened and flock'ed with `mode` (NON-blocking when it carries LOCK_NB); None when the lock is held elsewhere
    or cannot be set up at all (a full or read-only cache) — never an aborted run."""
    import fcntl
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        return None
    try:
        fcntl.flock(fh.fileno(), mode)
        return fh
    except OSError:
        fh.close()
        return None


class DeploymentTree:
    """ONE persistent worktree of `repo` for a deployment: `<cache>/deploy/<deployment>/<repo>`, a `git worktree add
    --detach` of the master. Its state, integrity manifest and recorded admin directory live in the host-private
    `<cache>/deploy/<deployment>/.host/<repo>/` (never inside the tree, so neither a warmBuild nor a run's overlay sees
    them); its lock in `.locks/<repo>.lock`. One WRITER (exclusive, non-blocking — only while no reader holds it), any
    number of READERS (shared, non-blocking, held for a run's whole life)."""

    def __init__(self, cache: str, deployment: str, repo: str) -> None:
        self.cache, self.deployment, self.repo = cache, deployment, repo
        self.root = os.path.join(cache, DEPLOY_DIR, deployment)
        self.tree = os.path.join(self.root, repo)
        self.host = os.path.join(self.root, ".host", repo)
        self.lock_path = os.path.join(self.root, ".locks", f"{repo}.lock")
        self.mirror = master_path(cache, repo)
        self.manifest_path = os.path.join(self.host, "manifest.json")
        # core's worktree for THIS repository's builds (§7.3b step 2): a `git worktree` of the platform master at the set's
        # commit, built IN PLACE by the warmBuild and the read-only lower of every stage's writable platform view — never
        # inside the repository's tree (a stage's discovery walks the checkout), never a copy
        self.platform = os.path.join(self.root, ".platform", repo)
        self.platform_manifest_path = os.path.join(self.host, "platform-manifest.json")
        self.last_refusal: Optional[str] = None
        self.platform_lower: Optional[str] = None

    def ident(self, tip: str) -> str:
        return f"{self.deployment}/{self.repo}@{tip}"

    def state(self) -> Optional[dict]:
        try:
            fd = os.open(os.path.join(self.host, "state.json"), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as fh:
                st = json.load(fh)
            return st if isinstance(st, dict) else None
        except (OSError, ValueError):
            return None

    def write_state(self, state: dict) -> None:
        os.makedirs(self.host, exist_ok=True)
        tmp = os.path.join(self.host, f".state-{os.getpid()}")
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, os.path.join(self.host, "state.json"))

    def admin(self) -> Optional[str]:
        """The worktree's git admin directory as recorded when the HOST added it — never read back from the tree's own
        `.git` file, which a warmBuild could rewrite."""
        try:
            with open(os.path.join(self.host, "admin")) as fh:
                a = fh.read().strip()
        except OSError:
            return None
        return a if a and os.path.isfile(os.path.join(a, "HEAD")) else None

    def platform_ident(self, commit: str) -> str:
        return f"{self.deployment}/{self.repo}/platform@{commit}"

    def publish(self, tip: str, key: str, platform: Optional[str] = None) -> None:
        """The writer's last step, still under its lock: the manifests FIRST (the tree's, and the platform tree's when the
        warmBuild built core in place at `platform`), then the state that names what was built — a reader verifies each
        tree against its manifest before it mounts it."""
        os.makedirs(self.host, exist_ok=True)
        for tree, path, ident in ((self.tree, self.manifest_path, self.ident(tip)),
                                  *([(self.platform, self.platform_manifest_path, self.platform_ident(platform))] if platform else [])):
            part = path + f".{os.getpid()}.part"
            with open(part, "w", encoding="utf-8") as fh:
                json.dump(tree_manifest(tree, ident), fh, separators=(",", ":"))
            os.replace(part, path)
        if not platform:
            try:
                os.remove(self.platform_manifest_path)
            except OSError:
                pass
        self.write_state({"key": key, "tip": tip, "built": tip, **({"platform": platform} if platform else {})})

    def unbuild(self, key: str, tip: str, whole: Optional[bool] = None) -> None:
        """Not built: readers go cold until the writer's next successful build (the manifests go first). A pending whole
        rebuild (`whole`, or — when not given — the one the state already records) is kept, so only `publish` clears it."""
        for path in (self.manifest_path, self.platform_manifest_path):
            try:
                os.remove(path)
            except OSError:
                pass
        if whole is None:
            whole = (self.state() or {}).get("whole") is True
        self.write_state({"key": key, "tip": tip, "built": None, **({"whole": True} if whole else {})})

    @contextlib.contextmanager
    def writer(self):
        """This process as the tree's ONE writer, or None when another writer or ANY reader holds the tree."""
        import fcntl
        fh = _try_lock(self.lock_path, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield self if fh is not None else None
        finally:
            if fh is not None:
                fh.close()

    @contextlib.contextmanager
    def reader(self, key: str):
        """The built tree and its tip, held (shared lock) while the block runs; None — with WHY in `last_refusal` — when the
        writer holds it, nothing is built for this key, or it fails its integrity check."""
        import fcntl
        self.last_refusal = None
        fh = _try_lock(self.lock_path, fcntl.LOCK_SH | fcntl.LOCK_NB)
        held = None
        if fh is None:
            self.last_refusal = "the deployment tree is locked (its writer is advancing it)"
        else:
            st = self.state()
            tip = str((st or {}).get("tip") or "")
            if st is None or not os.path.isdir(self.tree):
                self.last_refusal = "no deployment tree yet (the host builds it from main's tip while idle)"
            elif st.get("key") != key:
                self.last_refusal = "the tree was built by another build identity (a platform update or a new SDK) — the host rebuilds it whole while idle"
            elif not SHA.match(tip) or st.get("built") != tip:
                self.last_refusal = "the deployment tree is not built (its last build failed, or was interrupted)"
            else:
                why = verify_tree(self.tree, self.manifest_path, self.ident(tip))
                if why is not None:
                    self.last_refusal = f"the deployment tree's integrity check refused it: {why}"
                else:
                    held = (self.tree, tip)
                    # the warm platform tree is OPTIONAL: a missing or refused one leaves the stages on the image's tree
                    self.platform_lower = None
                    pc = str(st.get("platform") or "")
                    if SHA.match(pc) and os.path.isdir(self.platform):
                        pwhy = verify_tree(self.platform, self.platform_manifest_path, self.platform_ident(pc))
                        if pwhy is None:
                            self.platform_lower = self.platform
                        else:
                            self.last_refusal = f"the platform tree's integrity check refused it (stages take the image's tree): {pwhy}"
            if held is None:
                fh.close()
                fh = None
        try:
            yield held
        finally:
            if fh is not None:
                fh.close()


def _restore_git_link(tree: str, admin: str) -> None:
    """The tree's `.git` exactly as `git worktree add` wrote it: a planted directory, link or rewritten file is replaced
    without following it, so no host git step ever discovers a configuration a warmBuild wrote."""
    dotgit = os.path.join(tree, ".git")
    if os.path.isdir(dotgit) and not os.path.islink(dotgit):
        shutil.rmtree(dotgit)
    elif os.path.lexists(dotgit):
        os.remove(dotgit)
    with open(dotgit, "w") as fh:
        fh.write(f"gitdir: {admin}\n")


def whole_rebuild_due(prior: Optional[dict], key: str) -> bool:
    """Whether the tree's obj/ and bin/ must go before it is built: its state names another BUILD IDENTITY (a platform
    update — a new compiler), names none (a tree whose state was lost: nothing tells what built it), or a whole rebuild
    that was started was never finished (`whole` still set: the removal or the build after it was interrupted). Pure."""
    return prior is None or prior.get("key") != key or prior.get("whole") is True


def advance_deployment_tree(dt: DeploymentTree, tip: str, key: str, git: Callable[..., None]) -> str:
    """The WRITER's move of its ONE tree to `tip` (call it holding `dt.writer()`), from the LOCAL master only:
    first use → `git worktree add --detach` under the master lock; afterwards IN PLACE — `checkout --detach -f`, which
    rewrites only the files that differ (unchanged sources keep their mtimes, so MSBuild's incremental checks hold), then
    `clean` keeping obj/ and bin/ — UNLESS the build identity changed (`whole_rebuild_due`: a platform update), when the
    `clean` keeps NOTHING, so every project is rebuilt by the new compiler and no assembly of the old one survives. The
    pending whole rebuild is recorded (`whole: true`) BEFORE the removal and cleared only by `publish`, so an interrupted one
    is redone. Every git step names the host-recorded admin directory explicitly. The tree is "not built" from the first
    step until `publish`. Returns what happened; raises Refusal/OSError."""
    prior = dt.state()
    whole = whole_rebuild_due(prior, key)
    dt.unbuild(key, tip, whole=whole)
    admin = dt.admin()
    if admin is None or not os.path.isdir(dt.tree):
        with master_lock(os.path.dirname(dt.mirror), dt.repo):
            if os.path.lexists(dt.tree):
                shutil.rmtree(dt.tree)                     # a half-added tree: removed, never adopted
            git("--git-dir", dt.mirror, "worktree", "prune")
            os.makedirs(dt.root, exist_ok=True)
            git("--git-dir", dt.mirror, "worktree", "add", "--detach", "--quiet", dt.tree, tip)
            with open(os.path.join(dt.tree, ".git")) as fh:
                line = fh.read().strip()
            admin = os.path.normpath(os.path.join(dt.tree, line[len("gitdir:"):].strip())) if line.startswith("gitdir:") else ""
            if not os.path.isfile(os.path.join(admin, "HEAD")):
                raise Refusal(f"git worktree add left no admin directory ({line[:120]!r})")
            os.makedirs(dt.host, exist_ok=True)
            with open(os.path.join(dt.host, "admin"), "w") as fh:
                fh.write(admin + "\n")
        dt.write_state({"key": key, "tip": tip, "built": None, "whole": True})
        return f"deployment tree: added at main@{tip[:12]} ({dt.tree})"
    _restore_git_link(dt.tree, admin)
    g = ("--git-dir", admin, "--work-tree", dt.tree)
    # a stat-dirty entry (a touched but unchanged file) is re-hashed first, so `checkout -f` rewrites only what differs
    git(*g, "update-index", "-q", "--refresh")
    git(*g, "checkout", "--quiet", "--detach", "-f", tip)
    git(*g, "clean", "-ffdxq", *([] if whole else [a for k in WARM_KEEP for a in ("-e", k)]))
    dt.write_state({"key": key, "tip": tip, "built": None, **({"whole": True} if whole else {})})
    if whole:
        was = str((prior or {}).get("key") or "none")[:16]
        return (f"deployment tree: moved to main@{tip[:12]} in place — WHOLE REBUILD: the build identity changed "
                f"({was} → {key[:16]}, a platform update or a new SDK), obj/ and bin/ of every project removed")
    return f"deployment tree: moved to main@{tip[:12]} in place (obj/ and bin/ kept)"


def source_commit(platform_src: str) -> Optional[str]:
    """The commit the image's platform source was archived from (its `.source-commit`), or None. Pure apart from the read."""
    try:
        with open(os.path.join(platform_src, ".source-commit")) as fh:
            c = fh.read().strip().lower()
    except OSError:
        return None
    return c if SHA.match(c) else None


def advance_platform_tree(dt: DeploymentTree, commit: str, git: Callable[..., None], whole: bool = False) -> str:
    """The WRITER's move of the deployment's PLATFORM tree (`dt.platform`) to core at `commit` (call it holding
    `dt.writer()`, the master fetched to `commit`): first use → `git worktree add --detach` of the platform master under its
    lock; afterwards IN PLACE — `checkout --detach -f` and `clean` keeping obj/ and bin/, so core's outputs from the last
    warmBuild stay valid while the set's commit holds and rebuild incrementally when it moves. Writes `.source-commit`, as the
    image's tree carries it. Never a copy. Raises Refusal/OSError."""
    mirror = master_path(dt.cache, PLATFORM_REPO)
    admin_file = os.path.join(dt.host, "platform-admin")
    try:
        with open(admin_file) as fh:
            admin: Optional[str] = fh.read().strip() or None
    except OSError:
        admin = None
    if admin is not None and not os.path.isfile(os.path.join(admin, "HEAD")):
        admin = None
    if admin is None or not os.path.isdir(dt.platform):
        with master_lock(os.path.dirname(mirror), PLATFORM_REPO):
            if os.path.islink(dt.platform):
                os.remove(dt.platform)                     # a planted link: removed, never followed
            elif os.path.lexists(dt.platform):
                shutil.rmtree(dt.platform)                 # a half-added tree: removed, never adopted
            git("--git-dir", mirror, "worktree", "prune")
            os.makedirs(os.path.dirname(dt.platform), exist_ok=True)
            git("--git-dir", mirror, "worktree", "add", "--detach", "--quiet", dt.platform, commit)
            with open(os.path.join(dt.platform, ".git")) as fh:
                line = fh.read().strip()
            admin = os.path.normpath(os.path.join(dt.platform, line[len("gitdir:"):].strip())) if line.startswith("gitdir:") else ""
            if not os.path.isfile(os.path.join(admin, "HEAD")):
                raise Refusal(f"git worktree add left no admin directory for the platform tree ({line[:120]!r})")
            os.makedirs(dt.host, exist_ok=True)
            with open(admin_file, "w") as fh:
                fh.write(admin + "\n")
        verb = "added"
    else:
        _restore_git_link(dt.platform, admin)
        g = ("--git-dir", admin, "--work-tree", dt.platform)
        git(*g, "update-index", "-q", "--refresh")
        git(*g, "checkout", "--quiet", "--detach", "-f", commit)
        # a WHOLE rebuild of the repository's tree (a new build identity) rebuilds core's tree with it: nothing it keeps was
        # built by the previous compiler
        git(*g, "clean", "-ffdxq", *([] if whole else [a for k in WARM_KEEP for a in ("-e", k)]))
        verb = "moved in place — WHOLE REBUILD (obj/ and bin/ removed)" if whole else "moved in place (obj/ and bin/ kept)"
    with open(os.path.join(dt.platform, ".source-commit"), "w") as fh:
        fh.write(commit + "\n")
    return f"platform tree: {verb} at {commit[:12]} ({dt.platform}) — core is built where it lies, never copied"


def cold_worktree(cache: str, repo: str, sha: str, dest: str, git: Callable[..., None]) -> None:
    """The ONE fallback: a cold `git worktree add --detach` of the master at `sha` (fetched beforehand), under the master
    lock. Never a copy."""
    mirror = master_path(cache, repo)
    with master_lock(os.path.dirname(mirror), repo):
        git("--git-dir", mirror, "worktree", "prune")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        git("--git-dir", mirror, "worktree", "add", "--detach", "--quiet", dest, sha)


def cold_run_tree(cache: str, repo: str, sha: str, dest: str, base: str, git: Callable[..., None]) -> None:
    """A RUN's cold tree (§7.3b's one fallback): `dest` with its OWN `.git` — `init`, objects borrowed from the master
    through `alternates`, `origin` named, `read-tree -u <sha>` (git writes the files from its objects) and HEAD at `sha` —
    exactly the git shape an overlay run's tree has (`overlay_run_tree`). Never a registered worktree of the master: a
    stage sees the master READ-ONLY, so a ref a stage writes (MeshWeaver.Plugins host-affected.py names the admitted main
    as `refs/remotes/origin/…` for the selector's diff) lands in the run's own repository instead of failing against the
    master's shared refs (eiger 2026-11-09: "could not name the admitted main … as refs/remotes/origin/mw-host-modules-base",
    every cold run red in its first seconds). Never a copy; removed with the run's workdir. Raises Refusal."""
    mirror = master_path(cache, repo)
    os.makedirs(dest, exist_ok=True)
    git("init", "--quiet", dest)
    with open(os.path.join(dest, ".git", "objects", "info", "alternates"), "w") as fh:
        fh.write(os.path.join(mirror, "objects") + "\n")
    git("-C", dest, "remote", "add", "origin", f"{base}/{repo}.git")
    git("-C", dest, "read-tree", "-u", "--reset", sha)
    git("-C", dest, "update-ref", "--no-deref", "HEAD", sha)


def remove_cold_worktree(cache: str, repo: str, dest: str, git: Callable[..., None]) -> None:
    """`git worktree remove --force` and `prune`, under the master lock — so nothing stays registered. Never raises."""
    mirror = master_path(cache, repo)
    try:
        with master_lock(os.path.dirname(mirror), repo):
            try:
                git("--git-dir", mirror, "worktree", "remove", "--force", dest)
            except (Refusal, OSError):
                shutil.rmtree(dest, ignore_errors=True)    # a tree a stage left unremovable to git: gone either way
            git("--git-dir", mirror, "worktree", "prune")
    except (Refusal, OSError):
        pass


def copied_bytes(private: str, lower: Optional[str]) -> int:
    """MEASURED, never a constant: the bytes of the run's own layer (`private`, an overlay's upper) that duplicate the file
    at the same path of the tree it is layered on — exactly what a copy (or an overlay copy-up of an unchanged file) would
    have put there. `.git` (git's own, made by git) is skipped. A run with no lower copied nothing from one: 0."""
    if not lower or not os.path.isdir(private):
        return 0
    total = 0
    for d, dirs, files in os.walk(private):
        if d == private and ".git" in dirs:
            dirs.remove(".git")
        for n in files:
            full = os.path.join(d, n)
            rel = os.path.relpath(full, private)
            other = os.path.join(lower, rel)
            try:
                a, b = os.lstat(full), os.lstat(other)
            except OSError:
                continue
            if stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode) and a.st_size == b.st_size:
                try:
                    if _file_sha256(full) == _file_sha256(other):
                        total += a.st_size
                except OSError:
                    continue
    return total


def overlay_run_tree(repo: str, sha: str, refs: list, lower: str, lower_tip: str, dest: str, token: Optional[str], env: dict,
                     log: Callable[[str], None], git: Optional[Callable[..., None]] = None, deadline: Optional[float] = None,
                     mount: Optional[Callable[[str, str], None]] = None) -> None:
    """A run's tree `dest` as an OVERLAY of the built deployment tree `lower` (at `lower_tip`), with a `.git` made by git:
    `init`, objects borrowed from the master through `alternates`, `read-tree <lower_tip>` (no -u: the index only), a
    refresh that re-hashes what the overlay shows without touching a file, then the one-way `read-tree --reset -u <sha>` —
    so only the files `sha` changes are written (into the run's upper), and every unchanged source keeps the timestamp it
    has beneath the carried obj/ and bin/. Nothing is copied, not even the index. Raises Refusal/OSError."""
    g = git or _git_runner(env, os.path.dirname(dest), token, deadline)
    base = env.get("MW_BUILD_GIT_BASE", "https://github.com/Systemorph").rstrip("/")
    cache = env.get("MW_BUILD_CACHE", "/cache")
    mirror = master_path(cache, repo)
    wanted = [sha] + [r for r in (refs or []) if isinstance(r, str) and r]
    _update_mirror(repo, f"{base}/{repo}.git", os.path.dirname(mirror), mirror, wanted, g, log)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    (mount or _overlay_tree)(lower, dest)
    g("init", "--quiet", dest)
    with open(os.path.join(dest, ".git", "objects", "info", "alternates"), "w") as fh:
        fh.write(os.path.join(mirror, "objects") + "\n")
    g("-C", dest, "remote", "add", "origin", f"{base}/{repo}.git")
    g("-C", dest, "read-tree", lower_tip)
    g("-C", dest, "update-index", "-q", "--refresh")
    g("-C", dest, "read-tree", "--reset", "-u", sha)
    g("-C", dest, "clean", "-ffdxq", *[a for k in WARM_KEEP for a in ("-e", k)])
    g("-C", dest, "update-ref", "--no-deref", "HEAD", sha)


def validate_warm_build(doc: object) -> Optional[dict]:
    """`"warmBuild": {"command": …, "timeoutMinutes": N}` beside `stages` (the DEFAULT BRANCH tip's): what builds the warm
    deployment tree. Absent → None (the tree then carries sources only). Malformed → Refusal."""
    if not isinstance(doc, dict) or "warmBuild" not in doc:
        return None
    w = doc["warmBuild"]
    if not isinstance(w, dict) or not isinstance(w.get("command"), str) or not w["command"].strip():
        raise Refusal("`warmBuild` needs a non-empty `command`")
    tmo = w.get("timeoutMinutes", 30)
    if not isinstance(tmo, (int, float)) or isinstance(tmo, bool) or not 0 < tmo <= WARM_BUILD_MAX_MINUTES:
        raise Refusal(f"`warmBuild.timeoutMinutes` {tmo!r} is outside (0, {WARM_BUILD_MAX_MINUTES}]")
    return {"command": w["command"], "timeoutMinutes": float(tmo)}


def _git_runner(env: dict, home: str, token: Optional[str], deadline: Optional[float]) -> Callable[..., None]:
    genv = {**{k: env[k] for k in ("PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR") if env.get(k)}, "HOME": home,
            "GIT_CONFIG_NOSYSTEM": "1", **git_auth_env(token)}

    def run_git(*args: str) -> None:
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            raise Refusal("the deadline passed during a git step")
        try:
            # no hook and no fsmonitor ever run: every host git step runs OUTSIDE the sandbox
            r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
                               env=genv, capture_output=True, text=True, timeout=left)
        except subprocess.TimeoutExpired:
            raise Refusal("git did not finish inside the deadline — killed")
        if r.returncode != 0:
            raise Refusal(f"git {args[0]} failed (exit {r.returncode}): {(r.stderr or '').strip()[-300:]}")
    return run_git


RUN_OVERLAY_ENV = "MW_BUILD_RUN_OVERLAY"
IN_NS_ENV = "MW_BUILD_IN_NS"
# The probe mounts on the SAME filesystems a run's overlay uses — the lower on the cache (`$1`, where the deployment trees
# live) and the upper + workdir on the work root (`$2`, where a run's `.upper` lives). Never on `mktemp -d`'s /tmp: that is
# the container's own overlayfs root, which the kernel refuses as an upperdir ("not supported as upperdir"), so a probe
# there read every host as unable to overlay — measured on eiger 2026-11-09, where /cache → /work mounts fine.
_OVERLAY_PROBE = ("l=$(mktemp -d -p \"$1\" .overlay-probe.XXXXXX) && d=$(mktemp -d -p \"$2\" .overlay-probe.XXXXXX) && mkdir \"$d/u\" \"$d/w\" \"$d/m\" && echo x > \"$l/f\" && "
                  "mount -t overlay overlay -o \"lowerdir=$l,upperdir=$d/u,workdir=$d/w\" \"$d/m\" && "
                  "echo y > \"$d/m/f\" && umount \"$d/m\" && [ \"$(cat \"$l/f\")\" = x ] && [ \"$(cat \"$d/u/f\")\" = y ]; "
                  "rc=$?; for x in \"$d\" \"$l\"; do [ -n \"$x\" ] && { chmod -R u+rwx \"$x\"; rm -rf \"$x\"; } 2>/dev/null; done; exit $rc")
# A probe killed by its timeout leaves its directories behind on the SHARED cache and work roots (several pods mount them):
# the next start sweeps those older than this, never a probe another pod may still be running.
OVERLAY_PROBE_STALE_SECONDS = 600


def sweep_stale_overlay_probes(roots: list, now: Optional[float] = None) -> list:
    """Remove `.overlay-probe.*` directories older than OVERLAY_PROBE_STALE_SECONDS under each root; returns what it
    removed. A younger one may be another pod's probe in flight and is left alone."""
    now = time.time() if now is None else now
    gone = []
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            try:
                if n.startswith(".overlay-probe.") and os.path.isdir(p) and now - os.path.getmtime(p) > OVERLAY_PROBE_STALE_SECONDS:
                    for dp, dn, _ in os.walk(p):
                        for x in dn:
                            os.chmod(os.path.join(dp, x), 0o700)
                    shutil.rmtree(p, ignore_errors=True)
                    gone.append(p)
            except OSError:
                continue
    return gone


def run_overlay_enabled(env: dict) -> bool:
    """A run's tree is an OVERLAY of its deployment tree (read-only lower, per-run upper) — asked for by the chart
    (`buildHosts.runOverlay` → MW_BUILD_RUN_OVERLAY) AND this process living in its own user+mount namespace, where it may
    mount one (`enter_run_namespace`). Only with the warm workspace: there is no deployment tree without it."""
    return str(env.get(RUN_OVERLAY_ENV, "")).lower() == "true" and env.get(IN_NS_ENV) == "1" and warm_enabled(env)


def enter_run_namespace(argv: list, env: dict, log: Callable[[str], None],
                        which: Callable[[str], Optional[str]] = shutil.which,
                        probe: Optional[Callable[[list], int]] = None,
                        execvpe: Callable[..., None] = os.execvpe) -> None:
    """Re-exec this runner inside its OWN user + mount namespace (`unshare -Urm`, private propagation) when the run overlay
    is asked for, so a run's tree can be an overlay mount that every host git step and every stage sandbox (bwrap, nested)
    of this process sees. An exec, no fork: the same pid keeps receiving SIGTERM. Returns only when NOT re-executed — the
    overlay is not asked for, this process already is in its namespace, or the host cannot make one (no `unshare`, or the
    probe — a real overlay mount, a write through it, the lower untouched — fails), which is said; every run then takes a
    COLD worktree off the master (never a copy)."""
    if str(env.get(RUN_OVERLAY_ENV, "")).lower() != "true" or env.get(IN_NS_ENV) == "1":
        return
    u = which("unshare")
    if not u:
        log("run overlay: no `unshare` on this host — every run takes a cold worktree off the master")
        return
    cache, work = env.get("MW_BUILD_CACHE", "/cache"), env.get("MW_BUILD_WORK", "/work")
    cmd = [u, "-Urm", "--propagation", "private", "sh", "-c", _OVERLAY_PROBE, "overlay-probe", cache, work]
    if not probe:
        for g in sweep_stale_overlay_probes([cache, work]):
            log(f"run overlay: removed a stale probe directory {g} (a probe killed by its timeout)")
    why = ""
    try:
        if probe:
            rc = probe(cmd)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            rc, why = r.returncode, (r.stderr or "").strip().splitlines()[-1:] or [""]
            why = why[0][-200:]
    except (OSError, subprocess.SubprocessError) as e:
        rc = f"{type(e).__name__}"
    if rc != 0:
        log(f"run overlay: this host cannot mount an overlay in its own user namespace (probe: {rc}"
            f"{', ' + why if why else ''}; lower on {cache}, upper on {work}) — every run takes a cold worktree off the master")
        return
    new = dict(env, **{IN_NS_ENV: "1", "MW_BUILD_OUTER_UID": str(os.getuid()), "MW_BUILD_OUTER_GID": str(os.getgid())})
    log("run overlay: re-entering this runner in its own user + mount namespace — a run's tree is an overlay of its deployment tree")
    execvpe(u, [u, "-Urm", "--propagation", "private", sys.executable, os.path.abspath(__file__), *argv], new)


def _mount_runner(cmd: list) -> "tuple[int, str]":
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def _overlay_tree(lower: str, dest: str, sh: Callable[[list], "tuple[int, str]"] = _mount_runner) -> None:
    """A tree as an OVERLAY: `lower` (a deployment tree, or the image's platform source) read-only beneath, `dest.upper`
    taking every write — so the user writes only its DELTA and can never change the lower. `.git` of the lower is hidden
    (a run gets a fresh one). Raises Refusal when the mount is refused; the caller takes a cold worktree instead."""
    if any(c in lower for c in ",:\\") or not os.path.isdir(lower):
        raise Refusal(f"the path cannot be an overlay lower ({lower!r})")
    up, wk = dest + ".upper", dest + ".ovlwork"
    for d in (up, wk, dest):
        os.makedirs(d, exist_ok=True)
    rc, out = sh(["mount", "-t", "overlay", "overlay", "-o", f"lowerdir={lower},upperdir={up},workdir={wk}", dest])
    if rc != 0:
        release_run_tree(dest, sh)
        raise Refusal(f"the overlay mount was refused ({out[:160] or rc})")
    g = os.path.join(dest, ".git")
    if os.path.isdir(g) and not os.path.islink(g):
        shutil.rmtree(g)
    elif os.path.lexists(g):
        os.remove(g)


def release_run_tree(dest: str, sh: Callable[[list], "tuple[int, str]"] = _mount_runner,
                     ismount: Callable[[str], bool] = os.path.ismount) -> None:
    """Undo `_overlay_tree`: unmount (lazily, should a stage still hold a file) and make the overlay's work directory —
    created mode 000 by the kernel — removable. Never raises: the workspace sweep removes whatever is left."""
    try:
        if ismount(dest):
            rc, _ = sh(["umount", dest])
            if rc != 0:
                sh(["umount", "-l", dest])
    except OSError:
        pass
    for root in (dest + ".ovlwork", dest + ".upper"):
        for d, dirs, _files in os.walk(root, topdown=True, onerror=lambda e: None):
            for n in dirs:
                try:
                    os.chmod(os.path.join(d, n), 0o700)
                except OSError:
                    pass
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass


def remove_workspace(path: str, release: Callable[[str], None] = release_run_tree) -> None:
    """Remove a run's workspace, its overlay (if any) released first."""
    release(os.path.join(path, "src"))
    shutil.rmtree(path, ignore_errors=True)


def lower_is_writable_through(lower: str) -> bool:
    """Whether an overlay with `lower` beneath can be WRITTEN by this process, read from the lower's ROOT directory's owner:
    this process's uid for the runner's own trees under the cache, an id this user namespace does not map for the image's
    tree (overlayfs checks the lower's permissions on every copy-up into a directory). Both real lowers are single-owner
    trees, so the root decides; a mixed-ownership lower is not detected here. A missing or unreadable lower is not
    writable-through. Pure apart from a stat."""
    try:
        return os.stat(lower).st_uid == os.getuid()
    except OSError:
        return False


@contextlib.contextmanager
def platform_source_view(platform_src: str, dest: str, env: dict, log: Callable[[str], None],
                         git: Optional[Callable[..., None]] = None, mount: Optional[Callable[[str, str], None]] = None,
                         can_overlay: Optional[bool] = None, create_master: bool = True):
    """A WRITABLE view of the host's platform source (MW_PLATFORM_SOURCE, read-only in the image) at `dest`, for a
    repository-declared command that builds or restores projects referencing the platform (MSBuild writes their obj/ and
    bin/ into that tree) — and NOT A COPY: an overlay with the image's tree as the read-only lower where this runner may
    mount one, else a cold `git worktree add` of the platform's master at the tree's `.source-commit` (the image's tree IS
    that commit's `git archive`), said. Removed when the block ends. Raises Refusal when neither is possible."""
    can = env.get(IN_NS_ENV) == "1" if can_overlay is None else can_overlay
    if can and not lower_is_writable_through(platform_src):
        # The IMAGE's tree is owned by the pod's root, which this runner's own user namespace does not map: an overlay of
        # it mounts, but no directory of it can be written through it ("Access to the path …/obj is denied" — measured on
        # eiger 2026-11-09, every prepared input and warm restore red the moment the overlay probe passed, #213). Such a
        # lower takes the cold worktree, whose files this process owns; the deployment's platform tree (owned) overlays.
        try:
            owner: object = os.stat(platform_src).st_uid
        except OSError as e:
            owner = f"unreadable: {type(e).__name__}"
        log(f"platform source: {platform_src} is not owned by this runner (owner {owner}, runner uid {os.getuid()}) — an "
            f"overlay of it could not be written; a cold worktree of the platform's master instead")
        can = False
    layered, cold = False, False
    if can:
        try:
            (mount or _overlay_tree)(platform_src, dest)
            layered = True
        except (Refusal, OSError) as e:
            log(f"platform source: the overlay was refused ({e}) — a cold worktree of the platform's master instead")
    if not layered:
        try:
            with open(os.path.join(platform_src, ".source-commit")) as fh:
                commit = fh.read().strip().lower()
        except OSError:
            commit = ""
        if not SHA.match(commit):
            raise Refusal("the platform source cannot be made writable without a copy: this runner cannot overlay it and "
                          "the image's tree names no .source-commit to check out from the master")
        cache = env.get("MW_BUILD_CACHE", "/cache")
        repo = env.get("MW_BUILD_PLATFORM_REPO", PLATFORM_REPO)
        base = env.get("MW_BUILD_GIT_BASE", "https://github.com/Systemorph").rstrip("/")
        g = git or _git_runner(env, os.path.dirname(dest), None, None)
        mirror = master_path(cache, repo)
        if not create_master and not os.path.isfile(os.path.join(mirror, "HEAD")):
            # a STAGE never creates (clones) the platform master — the warm build and the prepared inputs own that; a stage
            # without one binds no view and says so, instead of cloning the platform per stage
            raise Refusal(f"no {repo} master on this host yet ({mirror}) — a stage does not clone one")
        log(f"platform source: COLD worktree of the {repo} master at {commit[:12]} (this runner cannot overlay the image's tree) — nothing copied")
        _update_mirror(repo, f"{base}/{repo}.git", os.path.dirname(mirror), mirror, [commit], g, log)
        shutil.rmtree(dest, ignore_errors=True)
        cold_worktree(cache, repo, commit, dest, g)
        cold = True
    try:
        yield dest
    finally:
        if layered:
            release_run_tree(dest)
            for d in (dest, dest + ".upper", dest + ".ovlwork"):
                shutil.rmtree(d, ignore_errors=True)
        if cold:
            remove_cold_worktree(env.get("MW_BUILD_CACHE", "/cache"), env.get("MW_BUILD_PLATFORM_REPO", PLATFORM_REPO), dest,
                                 git or _git_runner(env, os.path.dirname(dest), None, None))


# ─── the no-copy gate: build-run.py reads its own source; any tree copy outside the named allow-list fails the self-test ───
# Each allowed site copies something that is NOT a source or build tree, and says what.
NO_COPY_ALLOWED = {
    "ModuleHubBroker._copy_cached":
        "materialises ONE verified module output (a content-addressed slice from the host's slice cache) where the stage "
        "reads it — an artifact of the module store (§7.3), not a source or build tree",
    "promote_outputs":
        "promotes a content-keyed, verified OUTPUT directory into the content-addressed cache — an artifact, not a tree",
    "Host.stage_refset":
        "stages the IMAGE's reference set (compiled reference assemblies) once per image digest pair — image content, not a "
        "source or build tree",
    "Host.prepare_inputs.install":
        "installs a prepared input's built OUTPUT into its keyed store — an artifact, not a source or build tree",
}
_COPY_CALLS = {"copytree", "copyfile", "copy", "copy2"}
_COPY_TOOLS = {"cp", "rsync", "tar"}
_COPY_SHELL = re.compile(r"(?:^|[\s;&|(`])(?:cp|rsync|tar)(?:\s|$)")


def no_copy_violations(source: str, allowed: Optional[dict] = None) -> list[str]:
    """Every place `source` copies a tree: a `shutil.copytree/copyfile/copy/copy2` call, an argv literal running `cp`,
    `rsync` or `tar`, or a shell string doing so — outside `self_test` and outside the named, reasoned allow-list
    (`NO_COPY_ALLOWED`, keyed by the enclosing function's qualified name). An allow-list entry that no longer matches any
    site is a violation too (the list may only shrink honestly). Pure."""
    import ast
    allowed = NO_COPY_ALLOWED if allowed is None else allowed
    out: list[str] = []
    used: set = set()

    def visit(node, scope: list[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, ast.FunctionDef) and node.name == "self_test" and not scope:
                return
            scope = scope + [node.name]
        where = ".".join(scope) or "<module>"
        hit = None
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _COPY_CALLS and isinstance(f.value, ast.Name) and f.value.id == "shutil":
                hit = f"shutil.{f.attr}"
            else:
                for a in list(node.args) + [k.value for k in node.keywords]:
                    for c in ast.walk(a):
                        if isinstance(c, ast.Constant) and isinstance(c.value, str) and _COPY_SHELL.search(c.value):
                            hit = f"a shell `{c.value.strip()[:40]}`"
        elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            e0 = node.elts[0]
            if isinstance(e0, ast.Constant) and isinstance(e0.value, str) and os.path.basename(e0.value) in _COPY_TOOLS:
                hit = f"an argv running `{e0.value}`"
        if hit:
            if where in allowed:
                used.add(where)
            else:
                out.append(f"line {getattr(node, 'lineno', '?')} in {where}: {hit}")
        for child in ast.iter_child_nodes(node):
            visit(child, scope)
    visit(ast.parse(source), [])
    out += [f"allow-list entry {k} matches no copy site (remove it)" for k in sorted(set(allowed) - used)]
    return out


def _flock(fh) -> bool:
    """An exclusive lock on `fh`; False when it cannot be taken (no fcntl, or a filesystem without flock) —
    a caller whose next step is DESTRUCTIVE must refuse it then (`_swap_into_place`)."""
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return True
    except (ImportError, OSError):
        return False


# ───────────────────────────────── the one credential: AcrPull by workload identity ───────

def acr_registry_config(registry: str, env: dict, post: Callable[[str, dict], dict], dest_dir: str) -> Optional[str]:
    """A docker config.json authorising ORAS to PULL from `registry`, from the host's Azure
    WORKLOAD IDENTITY (the ServiceAccount's federated token, AcrPull only) — the hosts' ONE
    credential, read-only. Returns None when no workload identity is projected (an anonymous pull).

    federated token (AZURE_FEDERATED_TOKEN_FILE) → Entra access token (client-credentials with a
    client assertion) → ACR refresh token (`/oauth2/exchange`) → `identitytoken` in a config file
    written into the HOST's private directory. The token file is mounted into the host container
    only and is never bound into a sandbox; the config is deleted after the pull."""
    token_file = env.get("AZURE_FEDERATED_TOKEN_FILE", "")
    client = env.get("AZURE_CLIENT_ID", "")
    tenant = env.get("AZURE_TENANT_ID", "")
    if not token_file:
        return None
    if not client or not tenant:
        raise Refusal("a workload-identity token is projected but AZURE_CLIENT_ID / AZURE_TENANT_ID are not — "
                      "the ServiceAccount's azure.workload.identity/client-id annotation is missing")
    try:
        with open(token_file) as fh:
            assertion = fh.read().strip()
    except OSError as e:
        raise Refusal(f"the workload-identity token {token_file} is unreadable: {e}")
    authority = (env.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com/").rstrip("/")
    aad = post(f"{authority}/{tenant}/oauth2/v2.0/token", {
        "grant_type": "client_credentials", "client_id": client,
        "scope": "https://management.azure.com/.default",   # the audience ACR's /oauth2/exchange accepts
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion})
    access = aad.get("access_token") if isinstance(aad, dict) else None
    if not access:
        raise Refusal("Entra refused the workload-identity exchange (no access_token) — is the federated "
                      "credential registered for this ServiceAccount?")
    acr = post(f"https://{registry}/oauth2/exchange", {
        "grant_type": "access_token", "service": registry, "tenant": tenant, "access_token": access})
    refresh = acr.get("refresh_token") if isinstance(acr, dict) else None
    if not refresh:
        raise Refusal(f"{registry} refused the token exchange (no refresh_token) — does the identity hold AcrPull?")
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    path = os.path.join(dest_dir, "config.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"auths": {registry: {"identitytoken": refresh}}}, fh)
    return path


def _registry_get(url: str, key: str) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, str(e)


def _post_form(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": str(e)}


def _git_run(args: list[str], env: dict) -> tuple[int, str]:
    # No hook and no fsmonitor ever run from a checkout's own .git: every host git step runs OUTSIDE the sandbox, and a
    # run's tree is layered on a deployment tree a repository command (`warmBuild`) wrote (review of Deploy#183).
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
                       env={**env, "GIT_CONFIG_NOSYSTEM": "1"}, capture_output=True, text=True, timeout=900)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _run_capture_stdout(argv: list[str], timeout: float = 1800,
                        cancel: Optional[threading.Event] = None) -> tuple[int, str]:
    """A HOST helper (ci-artifact-store.py): no credential but the Azure workload identity's.
    `timeout` is the caller's remaining RUN budget; past it the helper is killed (TimeoutExpired). `cancel` set (the run
    was abandoned — a sibling stage met the 409) kills it at once and raises Lost: never a store call waited out."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                            start_new_session=True,
                            env={k: v for k, v in os.environ.items() if not SECRET_ENV.search(k) or k.startswith("AZURE_")})
    end = time.monotonic() + max(1.0, timeout)
    while True:
        try:
            out, err = proc.communicate(timeout=max(0.01, min(1.0, end - time.monotonic())))
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                _kill(proc)
                raise Lost(f"{os.path.basename(argv[1]) if len(argv) > 1 else argv[0]}: killed — the run was abandoned")
            if time.monotonic() >= end:
                _kill(proc)
                raise subprocess.TimeoutExpired(argv, timeout)
    return proc.returncode, (out or "") + ("\n" + err if proc.returncode else "")


def _run_capture(argv: list[str]) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                       timeout=120)
    return r.returncode, (r.stderr or "") + (r.stdout or "")      # stdout LAST: callers read its final line


class Host:
    def __init__(self, env: dict, api: Api, *, log: Callable[[str], None] = None,
                 pull: Callable[[list[str]], int] = None, checkout: Callable = None,
                 sandbox=None, leftovers: Callable[[], list[int]] = None, reap: Callable[[], int] = None,
                 registry_get: Callable[[str, str], tuple[int, object]] = None,
                 fetch_runner: Callable[[list[str], dict], int] = None,
                 store_runner: Callable[..., tuple[int, str]] = None,
                 git_runner: Callable[[list[str], dict], tuple[int, str]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 capacity: Callable[[], "tuple[float, Optional[int]] | str"] = None,
                 nuget_feeds: Optional[NugetFeeds] = None) -> None:
        self.env = env
        # the build toolchain by CONTENT, once per process (the warm key and the module fingerprints read it)
        if TOOLCHAIN_ENV not in self.env:
            self.env[TOOLCHAIN_ENV] = build_toolchain()
        # Where a package promoted into the warm NuGet cache is checked against its source feed (promote_nuget).
        self.nuget_feeds = nuget_feeds or NugetFeeds(trusted=nuget_trusted_sources(env))
        self.api = api
        self.log = log or (lambda s: print(f"build-run: {s}", flush=True))
        self.pull = pull or (lambda cmd: subprocess.run(cmd).returncode)
        self.checkout = checkout or default_checkout
        self.cache = env.get("MW_BUILD_CACHE", "/cache")
        self.work = env.get("MW_BUILD_WORK", "/work")
        # NEVER the cache root: what a stage may read of the shared cache is bound per stage
        # (stage_readonly) — its own repository's mirror and the dependency artifacts it declares.
        # The staged image roots are bound read-only, `--ro-bind-try` (absent on a set that names no
        # such image): /core-tests is where a set's mw-core-tests image is staged, and core's shard and
        # doc-gate stages run `python3 /core-tests/core-tests.py …` from it.
        self.sandbox = sandbox or Sandbox(env.get("BWRAP", "bwrap"), sandbox_readonly_roots(env), outer_ids(env))
        self.run_holds = contextlib.ExitStack()
        self.run_platform_lower: Optional[str] = None   # the deployment's warm platform tree this run holds (§7.3b step 2)
        self.cold_tree: Optional[tuple] = None
        self.legacy_retired = False                  # §7.3's <cache>/shared removed by this process's first idle pass
        self.run_mount: Optional[Callable[[str, str], None]] = None   # the run overlay's mount (injectable for the self-test)
        self.leftovers = leftovers or pid_namespace_leftovers
        self.reap = reap or reap_children
        self.registry_get = registry_get or _registry_get
        self.store_runner = store_runner or _run_capture_stdout
        self.git_runner = git_runner or _git_run
        self.fetch_runner = fetch_runner or (lambda argv, env: subprocess.run(argv, env=env, timeout=600).returncode)
        self.sleep = sleep
        self.clock = clock
        self.stopping = False
        # stale-set: claiming no work, but RUNNING (Memex#885) — only SIGTERM ends the host.
        self.drained = False
        self.host = env.get("MW_BUILD_HOST") or socket.gethostname()
        self.channel = env.get("MW_BUILD_CHANNEL", "").strip()
        self.version = env.get("MW_BUILD_VERSION", "").strip()
        self.platform_version = env.get("MW_BUILD_PLATFORM_VERSION", "").strip()
        # The claim scope every call names: a versioned server claims, reports and scales by its VERSION.
        self.scope = {"version": self.version} if self.version else {"channel": self.channel}
        # A versioned server PROVES the set it runs on every claim: the three images of its v/<version> set, as the chart
        # renders them. The server drains a host whose images are not the set the version names now, and records the set
        # on the claim, so a run's green is a test of exactly those images (Plugins#4221).
        self.set_identity = {"testerDigest": env.get("MW_BUILD_TESTER_DIGEST", "").strip(),
                             "portalDigest": env.get("MW_BUILD_PORTAL_DIGEST", "").strip(),
                             "runnerDigest": env.get("MW_BUILD_RUNNER_DIGEST", "").strip()} if self.version else {}
        # The optional fifth part of a set: sent exactly when the chart renders it, so the claim proves the whole token.
        if self.version and env.get("MW_BUILD_CORE_TESTS_DIGEST", "").strip():
            self.set_identity["coreTestsDigest"] = env["MW_BUILD_CORE_TESTS_DIGEST"].strip()
        # A CHANNEL host presents its set too, so a channel declared to SERVE another (a leased Mac's `mac` → `latest`,
        # Plugins#5104) can prove it runs exactly the served channel's set — the server serves a host nothing unless every
        # digest matches (BuildHostQueue.ServesTheSet). Only well-shaped digests are sent: the server refuses a whole claim
        # that carries a malformed one, and a channel host's own claim does not depend on them.
        if not self.version:
            for key, name in (("testerDigest", "MW_BUILD_TESTER_DIGEST"), ("portalDigest", "MW_BUILD_PORTAL_DIGEST"),
                              ("runnerDigest", "MW_BUILD_RUNNER_DIGEST"), ("coreTestsDigest", "MW_BUILD_CORE_TESTS_DIGEST")):
                value = env.get(name, "").strip()
                if DIGEST.match(value):
                    self.set_identity[key] = value
        self.lane = self.version or self.channel
        self.identity = env.get("MW_BUILD_FRAMEWORK_IDENTITY", "").strip()
        if not self.identity and env.get("MW_BUILD_IDENTITY_FILE"):
            try:
                with open(env["MW_BUILD_IDENTITY_FILE"]) as fh:
                    self.identity = (fh.read().split() or [""])[0]
            except OSError:
                self.identity = ""
        self.ready_file = env.get("MW_BUILD_READY_FILE", "/tmp/build-host-ready")
        try:
            self.run_deadline = float(env.get("MW_BUILD_RUN_DEADLINE_SECONDS", str(DEFAULT_RUN_DEADLINE)) or DEFAULT_RUN_DEADLINE)
        except ValueError:
            self.run_deadline = float(DEFAULT_RUN_DEADLINE)
        # Fan-out: how many of a `parallel` run's stages may be in flight at once (checked against the cgroup at start).
        self.stage_slots_text = (env.get("MW_BUILD_STAGE_SLOTS", "1") or "1").strip()
        try:
            self.stage_slots = max(1, int(self.stage_slots_text))
        except ValueError:
            self.stage_slots = 1
        self.capacity = capacity or cgroup_capacity
        self.plugins_digest: Optional[str] = None
        self.plugins_dir: Optional[str] = None
        self.plugins_known = False
        self.held: list[EventSink] = []
        # `held` is appended to from every concurrent stage's thread (deliver) and drained by the scheduler.
        self.held_lock = threading.RLock()
        self.artifacts_dir = ""
        self.run_env: dict = {}
        self.plugins_key: Optional[str] = None
        # The module hand-over signal the layer was staged at (`/hosts/inputs` → `modules.key`), and whether
        # the persistent layer record was consulted yet (once per process).
        self.modules_seen: Optional[str] = None
        self.layer_record_read = False
        self.modules_signal_said: Optional[str] = None
        self.heartbeat: Optional[Heartbeat] = None
        self.run_sinks: dict[str, EventSink] = {}
        self.claim_id = ""
        self.probe: Optional[str] = None
        self.run_scope = {"channel": self.channel}
        self.workspace_line = ""
        # repositories whose warm main workspace the host keeps (§7.2), learned from the runs it took
        self.warm_repos: set = set()
        self.warm_thread: Optional[threading.Thread] = None
        self.warm_cancel = threading.Event()
        self.warm_stop_seconds = 120.0
        # repo -> (tip, key, monotonic time) of an advancement that FAILED: retried after WARM_RETRY_SECONDS, not every idle pass
        self.warm_failed: dict = {}
        self.module_broker: Optional[ModuleHubBroker] = None
        # NO NETWORK DURING A RUN (Plugins Hosting/OneBuildTree.md): `offline` runs every stage in its own network
        # namespace and restores only from the prepared cache; `open` (the default until the preparation covers a run's
        # whole closure) keeps today's network and says what a run still had to fetch (MWPREP001)
        self.stage_network = (env.get(STAGE_NETWORK_ENV) or "open").strip().lower()
        self.stage_offline = self.stage_network == "offline"
        # what ONE run may write on the node's disk (MW_BUILD_RUN_DISK_GIB), and the share of the disk the kubelet's
        # eviction threshold needs kept free (MW_BUILD_DISK_HEADROOM_PERCENT): a claim is taken only when both fit
        self.run_disk = disk_gib(env.get(RUN_DISK_ENV), RUN_DISK_DEFAULT_GIB)
        self.disk_headroom = disk_percent(env.get(DISK_HEADROOM_ENV), DISK_HEADROOM_DEFAULT_PERCENT)
        self.disk_stat: Callable[[str], "tuple[int, int]"] = _disk_free
        self.disk_said: Optional[str] = None

    def start_module_broker(self, repo: str, sha: str, claim: dict, trunk: bool, checkout: str, workdir: str) -> None:
        """§7.4 wiring, the host's half: when the host is enabled for it and the DEFAULT BRANCH tip declares `moduleHub`,
        fingerprint every declared module from the pristine tree and serve the stages' requests for the run. Never raises:
        without a broker a stage builds as it always did (said)."""
        self.module_broker = None
        if not module_hub_enabled(self.env):
            return
        try:
            mirror = os.path.join(self.cache, "git", f"{repo}.git")
            genv = {**{k: self.env[k] for k in ("PATH", "LANG") if self.env.get(k)}, "HOME": workdir, "GIT_TERMINAL_PROMPT": "0"}
            rc, out = self.git_runner(["--git-dir", mirror, "rev-parse", "--verify", "HEAD^{commit}"], genv)
            tip = out.strip().splitlines()[-1].strip().lower() if rc == 0 and out.strip() else ""
            rc, doc = self.git_runner(["--git-dir", mirror, "show", f"{tip}:.github/build-stages.json"], genv) if SHA.match(tip) else (1, "")
            decl = validate_module_hub(json.loads(doc)) if rc == 0 else None
            if decl is None:
                self.log("module hub: the default branch declares no moduleHub — the stages build as before")
                return
            # the run's own declaration WIDENS the trusted one (a new dependency's directory), never narrows it
            try:
                with open(os.path.join(checkout, ".github", "build-stages.json")) as fh:
                    candidate = validate_module_hub(json.load(fh))
            except (OSError, ValueError, Refusal):
                candidate = None
            decl = widen_module_hub(decl, candidate)

            def ls_tree(paths: list) -> str:
                rc_, out_ = self.git_runner(["-C", checkout, "ls-tree", "-r", "HEAD", "--", *paths], genv)
                if rc_ != 0:
                    raise Refusal(f"git ls-tree failed (exit {rc_}): {out_.strip()[-200:]}")
                return out_
            # the BUILD IDENTITY's parts (platform set, framework identity, compiler) — never the runner's code, so a Deploy
            # merge keeps every fingerprint and the store answers Hits across rolls and across hosts
            toolchain = f"{self.platform_version}|{self.identity}|{self.env.get(TOOLCHAIN_ENV, '')}"
            fps = module_fingerprints(decl, ls_tree, toolchain)
            order = 0
            if trunk:
                rc, out = self.git_runner(["--git-dir", mirror, "rev-list", "--count", "--first-parent", sha], genv)
                order = int(out.strip().splitlines()[-1]) if rc == 0 and out.strip().splitlines()[-1].isdigit() else 0
                if order <= 0:
                    self.log("module hub: the trunk commit's merge order could not be read — no broker for this run")
                    return
            pr = pull_request_number(claim.get("refs") or [])
            if not trunk and pr is None:
                self.log("module hub: a non-trunk run without a pull request ref — no broker for this run")
                return
            root = os.path.join(workdir, "module-hub")
            self.module_broker = ModuleHubBroker(self.api, repo, sha, self.host, root, fps, trunk, order, pr, self.log,
                                                 slice_cache=self.slice_cache_root(),
                                                 build_tag={"platformVersion": self.platform_version, "frameworkIdentity": self.identity})
            self.module_broker.start()
            self.log(f"module hub: {len(fps)} module(s) fingerprinted from the pristine tree; lane "
                     f"{'main (order ' + str(order) + ')' if trunk else 'pull request #' + str(pr)}")
        except (Refusal, ValueError, OSError, subprocess.SubprocessError) as e:
            self.module_broker = None
            self.log(f"module hub: not started ({type(e).__name__}: {str(e)[:200]}) — the stages build as before")

    def stop_module_broker(self) -> None:
        broker, self.module_broker = self.module_broker, None
        if broker is not None:
            broker.stop("the run ended before this module was handed over")
            self.bound_slices()

    def disk_refusal(self) -> Optional[str]:
        """Why this host must not claim now, or None: the filesystem under the work volume or the cache (each measured —
        an emptyDir and a hostPath/local-path claim share the node's disk on a Mac) would keep less than the eviction
        headroom free once one run's footprint is written. Never raises: an unreadable filesystem claims (said once)."""
        for what, path in (("the work volume", self.work), ("the cache", self.cache)):
            try:
                total, free = self.disk_stat(path)
            except OSError:
                continue
            if total <= 0:
                continue
            keep = total * self.disk_headroom / 100.0
            if free - self.run_disk < keep:
                return (f"{what} ({path}) has {free / (1 << 30):.1f} GiB free of {total / (1 << 30):.1f} GiB; a run needs "
                        f"{self.run_disk / (1 << 30):.0f} GiB ({RUN_DISK_ENV}) and {self.disk_headroom:g} % must stay free "
                        f"for the kubelet's eviction threshold ({DISK_HEADROOM_ENV}) — retried every idle pass")
        return None

    def slice_cache_root(self) -> str:
        return os.path.join(self.cache, "module-slices")

    def bound_slices(self) -> None:
        """The slice cache back under its bound (`bound_slice_cache`), after every run that handed slices over and on every
        idle pass. Never raises: a bound that cannot be applied is said, the run's verdict is not touched."""
        try:
            bound_slice_cache(self.slice_cache_root(), slice_cache_cap(self.env), self.log)
        except Exception as e:      # the bound must never take a run or the idle loop down
            self.log(f"slice cache: not bounded this pass ({type(e).__name__}: {str(e)[:160]})")

    def preflight(self) -> None:
        missing = []
        if not api_url_acceptable(self.api.base):
            missing.append("BUILD_API (the operations instance's /api/builds: https, or http to a "
                           "cluster-local host — the worker token rides on it)")
        if not self.api.token:
            missing.append("the worker token (BUILD_TOKEN_FILE, the mounted Hosting:Builds:WorkerToken)")
        if self.version and self.channel:
            missing.append("exactly ONE of MW_BUILD_CHANNEL and MW_BUILD_VERSION (a versioned server claims by version only)")
        elif self.version:
            if not VERSION.match(self.version):
                missing.append(f"MW_BUILD_VERSION as MAJOR.MINOR.PATCH[-pre] (got {self.version!r})")
            elif self.platform_version and self.platform_version != self.version:
                missing.append(f"MW_BUILD_PLATFORM_VERSION equal to MW_BUILD_VERSION ({self.platform_version!r} != {self.version!r}) "
                               "— a versioned server runs exactly the set its version names")
            for name, value in (("MW_BUILD_TESTER_DIGEST", self.set_identity.get("testerDigest", "")),
                                ("MW_BUILD_PORTAL_DIGEST", self.set_identity.get("portalDigest", "")),
                                ("MW_BUILD_RUNNER_DIGEST", self.set_identity.get("runnerDigest", "")),
                                *((("MW_BUILD_CORE_TESTS_DIGEST", self.set_identity["coreTestsDigest"]),)
                                  if "coreTestsDigest" in self.set_identity else ())):
                if not DIGEST.match(value):
                    missing.append(f"{name} as a sha256:<64 hex> digest — a versioned server proves its set on every claim")
        elif not CHANNEL.match(self.channel):
            missing.append("MW_BUILD_CHANNEL (latest | stable) or MW_BUILD_VERSION")
        if not self.platform_version:
            missing.append("MW_BUILD_PLATFORM_VERSION (the set this host runs)")
        if self.stage_network not in STAGE_NETWORKS:
            missing.append(f"{STAGE_NETWORK_ENV} as open or offline (got {self.stage_network!r})")
        if not self.identity:
            missing.append("MW_BUILD_FRAMEWORK_IDENTITY or a readable MW_BUILD_IDENTITY_FILE")
        if self.env.get("MW_BUILD_COMPILE_REFS"):
            why = compile_refs_refusal(self.env["MW_BUILD_COMPILE_REFS"])
            if why:
                missing.append(why)
        why_slots = stage_slots_refusal(self.stage_slots_text, self.capacity())
        if why_slots:
            missing.append(why_slots)
        if not (MIN_RUN_DEADLINE <= self.run_deadline <= MAX_RUN_DEADLINE):
            missing.append(f"MW_BUILD_RUN_DEADLINE_SECONDS within [{MIN_RUN_DEADLINE}, {MAX_RUN_DEADLINE}] — the RUN budget "
                           f"(the contract's budgetMinutes is 10..720), which must cover the stages a repository declares")
        if missing:
            raise Refusal("the host cannot start — provide: " + "; ".join(missing))

    # -- the namespace must be empty before anything is claimed -----------------------------
    def assert_clean(self) -> None:
        self.reap()
        left = self.leftovers()
        if not left:
            return
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        self.sleep(1.0)
        self.reap()
        left = self.leftovers()
        if left:
            raise Fatal(f"processes of a finished stage survive in another pid namespace ({left[:10]}) — "
                        f"the host will not claim again; the container is replaced")

    # -- step 1: the plugins layer, refreshed BEFORE any claim -------------------------------
    def refresh_inputs(self) -> bool:
        if self.env.get("MW_REGISTRY_URL"):
            return self.refresh_bundles()
        return self.refresh_seal()

    def modules_signal(self) -> Optional[str]:
        """The module hand-over signal: `/hosts/inputs` → `modules.key` (MeshWeaver.Plugins
        Hosting/StandingBuildHosts §2). It MOVES when a module is handed over to the registry (the
        registry's module-published broadcast, reconciled on the operations instance), so the index is
        read when something was handed over — not on every loop turn. None = no signal (unreadable inputs,
        a null `modules`): the caller then reads the index every turn, as before the signal existed."""
        status, body = self.api.call("GET", "/hosts/inputs", query=self.scope)
        modules = body.get("modules") if status == 200 and isinstance(body, dict) else None
        key = modules.get("key") if isinstance(modules, dict) else None
        key = key if isinstance(key, str) and key.strip() else None
        said = key and "signal" or f"none (HTTP {status})"
        if said != self.modules_signal_said:
            self.modules_signal_said = said
            self.log("the operations instance reports the module hand-over signal — the bundle index is read when it moves"
                     if key else f"no module hand-over signal ({said}) — reading the bundle index every turn")
        return key

    def refresh_bundles(self) -> bool:
        """H8: the plugins layer from the REGISTRY's bundle index — what an instance on this framework
        identity would install — read with the host's read-only `mwi_` instance key (a file in the host
        container, never bound into a sandbox). The index is read when the module HAND-OVER signal moved
        (`modules_signal`), when the staged layer is not intact on the cache (install-on-miss), or on every
        turn while there is no signal. The installed layer is recorded on the shared cache, so the next
        host starts with it installed."""
        if not self.layer_record_read:
            self.layer_record_read = True
            rec = read_layer_record(self.cache, self.identity) if self.identity else None
            if rec:
                self.plugins_dir, self.plugins_key, self.plugins_digest, self.plugins_known = rec["dir"], rec["key"], None, True
                self.modules_seen = rec.get("modulesKey") if isinstance(rec.get("modulesKey"), str) else None
                self.log(f"plugins layer already installed on the cache: {rec['key'][:19]}…")
        signal = self.modules_signal()
        if signal is not None and signal == self.modules_seen and self.plugins_key and layer_intact(self.plugins_dir, self.cache):
            return True                     # nothing handed over since the layer was staged
        url = self.env["MW_REGISTRY_URL"].rstrip("/")
        key_file = self.env.get("MW_REGISTRY_KEY_FILE", "/run/registry-key/key")
        try:
            with open(key_file) as fh:
                instance_key = fh.read().strip()
        except OSError as e:
            self.log(f"the registry instance key {key_file} is unreadable ({e}) — NOT claiming")
            return False
        status, index = self.registry_get(f"{url}/api/plugins/bundles/index.json", instance_key)
        if status != 200 or not isinstance(index, dict):
            self.log(f"the registry's bundle index is unreadable (HTTP {status}) — NOT claiming this cycle")
            return False
        # The index states the framework it serves (`frameworkMvid`, the identity a bundle is built
        # against). MISSING is a refusal as much as DIFFERENT: an index that cannot say which
        # framework its bundles are for cannot be staged for this host's.
        served_for = index.get("frameworkMvid") or index.get("frameworkIdentity") or index.get("identity")
        if not served_for:
            self.log("the registry's bundle index names no framework identity (frameworkMvid) — NOT claiming")
            return False
        if served_for != self.identity:
            self.log(f"the registry serves bundles for identity {served_for}, this host runs {self.identity} — NOT claiming")
            return False
        try:
            lines = bundle_lines(index)
            key = plugins_key_of(lines)
            if key != self.plugins_key or not layer_intact(self.plugins_dir, self.cache):
                d = stage_bundle_set(lines, self.cache, lambda pkg, ver, out: self.module_fetch(url, instance_key, pkg, ver, out))
                self.log(f"plugins layer staged from the registry index: {key[:19]}… ({len(lines)} bundle(s))")
                self.plugins_dir = d
        except Refusal as r:
            self.log(f"{r} — NOT claiming this cycle")
            return False
        self.plugins_key, self.plugins_digest, self.plugins_known = key, None, True
        self.modules_seen = signal
        try:
            write_layer_record(self.cache, self.identity, key, self.plugins_dir, signal)
        except OSError as e:
            self.log(f"could not record the installed plugins layer on the cache ({e}) — the next host reads the index")
        return True

    def module_fetch(self, registry: str, instance_key: str, package: str, version: str, out: str) -> int:
        """core's `meshweaver-plugin-build module-fetch` — the one implementation that validates a
        bundle's paths before writing. The key reaches it through the environment only (`--key-env`),
        never argv; it is a HOST helper, never run inside a stage's sandbox."""
        cmd = (self.env.get("MW_PLUGIN_BUILD") or "dotnet /opt/build-host/plugin-build/MeshWeaver.Plugin.Build.dll").split()
        env = {k: self.env[k] for k in ("PATH", "HOME", "DOTNET_ROOT", "SSL_CERT_FILE", "SSL_CERT_DIR") if self.env.get(k)}
        env.update({"MW_REGISTRY_INSTANCE_KEY": instance_key, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"})
        return self.fetch_runner([*cmd, "module-fetch", package, "--registry", registry,
                                  "--key-env", "MW_REGISTRY_INSTANCE_KEY", "--version", version, "--out", out], env)

    def refresh_seal(self) -> bool:
        # The claim scope: a versioned server asks by its version (`?version=`), a channel host by its channel.
        status, body = self.api.call("GET", "/hosts/inputs", query=self.scope)
        if status != 200 or not isinstance(body, dict):
            self.log(f"hosts/inputs unreadable (HTTP {status}) — NOT claiming this cycle: a run must "
                     f"record the plugins layer that compiled it")
            return False
        plugins = body.get("plugins")
        if plugins is None:
            self.plugins_digest, self.plugins_dir, self.plugins_known = None, None, True
            return True
        if not isinstance(plugins, dict):
            self.log("hosts/inputs answered a plugins value that is not an object — NOT claiming")
            return False
        digest = str(plugins.get("digest") or "")
        if digest == self.plugins_digest and self.plugins_dir and self.plugins_known:
            return True
        try:
            d = stage_plugins(plugins, self.cache, self.env, self.pull)
        except Refusal as r:
            self.log(f"{r} — NOT claiming this cycle")
            return False
        if self.plugins_digest != digest:
            self.log(f"plugins layer staged: {digest} (version {plugins.get('version')}, sha "
                     f"{str(plugins.get('sha') or '')[:12]})")
        self.plugins_digest, self.plugins_dir, self.plugins_known = digest, d, True
        return True

    def mark_ready(self) -> None:
        try:
            with open(self.ready_file, "w") as fh:
                fh.write(f"{self.platform_version} {self.plugins_digest or '-'}\n")
        except OSError as e:
            self.log(f"could not write the readiness file {self.ready_file}: {e}")

    # -- step 2: claim ------------------------------------------------------------------------
    def claim(self) -> Optional[dict]:
        """`POST /claim` with a FRESH `claimId` per claim attempt. A network error (or a 5xx) is
        retried with the SAME claimId: the instance then answers the assignment it already made, so a
        lost answer never strands a run claimed for a host that does not know it. A host keeps no
        claim across a restart — after one it claims afresh, never resumes an old claim.

        409 answers, by `error`:
          * `stale-claim-id` — that id is SPENT (its reservation names a run no longer held under this
            host and id) and nothing was handed out: mint a FRESH id and claim again, ONCE — never a
            retry of the spent id, never a loop on it;
          * `stale-set` — this host's platform set is no longer its channel's: DRAIN — claim no work, and
            IDLE; never exit (Memex#885). The answer reads the LIVE record, and a record change can sit
            unapplied (an approval lane) with NO replacement replica: a host that exited then was restarted
            by its Deployment, drained again and exited again — CrashLoopBackOff and no build host at all.
            The replacement replica, when there is one, ends this host with SIGTERM; until then it asks
            again each idle period and resumes if the instance answers its set current."""
        for fresh in range(2):
            body = {"host": self.host, **self.scope, **self.set_identity, "platformVersion": self.platform_version,
                    "frameworkIdentity": self.identity, "pluginsDigest": self.plugins_digest,
                    "pluginsKey": self.plugins_key, "claimId": str(uuid.uuid4())}
            self.claim_id = body["claimId"]
            delay = 2.0
            status, answer = 0, None
            for attempt in range(4):
                status, answer = self.api.call("POST", "/claim", body)
                if status == 204 or (status == 200 and isinstance(answer, dict)):
                    if self.drained:
                        self.log(f"the instance answers this host's set {self.platform_version} current again — claiming")
                        self.drained = False
                    return answer if status == 200 else None
                if status != 0 and status < 500:
                    break
                self.log(f"claim {body['claimId']} answered HTTP {status} — retrying with the SAME claimId")
                self.sleep(delay)
                delay *= 2
            error = str(answer.get("error") or "") if status == 409 and isinstance(answer, dict) else ""
            if error.startswith("stale-claim-id"):
                if fresh == 0:
                    self.log(f"claim id {body['claimId']} is spent (stale-claim-id) — claiming again with a FRESH id")
                    continue
                self.log("a fresh claim id was answered stale-claim-id too — not claiming again this cycle")
                return None
            if error.startswith("stale-set"):
                if not self.drained:
                    self.log(f"the instance answers stale-set: this host's set {self.platform_version} is no longer "
                             f"{'version' if self.version else 'channel'} {self.lane}'s — DRAINED: claiming no work and "
                             f"idling until a replica of the new set replaces this one (SIGTERM); never exiting")
                self.drained = True
                return None
            self.log(f"claim answered HTTP {status} — retrying after a pause")
            return None
        return None

    def cycle(self) -> str:
        """One loop turn: 'stopping', 'no-inputs', 'idle', 'drained', 'ran' or 'lost'. Raises Fatal."""
        if self.stopping:
            return "stopping"
        # the host's OWN warm sandbox is not a leftover: the survivor check runs while no warm build is in flight, and again
        # (below) after a claim has stopped one and before its run starts
        if not (self.warm_thread and self.warm_thread.is_alive()):
            self.assert_clean()
        if not self.refresh_inputs():
            return "no-inputs"
        self.mark_ready()
        if self.stopping:
            return "stopping"
        # DISK FIRST: a run that starts on a node without room for it is evicted mid-run for DiskPressure (moench and eiger
        # 2026-11-10: 17 and 18 build pods), which reads as a lost run, never as "the node was full"
        tight = self.disk_refusal()
        if tight is not None:
            if tight != self.disk_said:
                self.log(f"disk: claiming nothing — {tight}")
                self.disk_said = tight
            self.bound_slices()
            return "idle"
        if self.disk_said is not None:
            self.log("disk: room again — claiming")
            self.disk_said = None
        claim = self.claim()
        if claim is None:
            if not self.drained and warm_enabled(self.env) and not (self.warm_thread and self.warm_thread.is_alive()):
                # warming runs BESIDE the claim loop: the host keeps asking for work, and a claim stops it (stop_warming)
                self.warm_cancel.clear()
                self.warm_thread = threading.Thread(target=self.warm_main_when_idle, daemon=True)
                self.warm_thread.start()
            return "drained" if self.drained else "idle"
        self.stop_warming()
        self.assert_clean()      # the full survivor check, now that no warm sandbox is in flight
        return self.run(claim)

    def stop_warming(self) -> None:
        """A claim (or SIGTERM) needs the host: stop the warm build in flight and wait for its sandbox to be gone, so no
        run ever reads a half-built deployment tree. FAIL-CLOSED: a warm worker still alive after the bound (a git step or the
        platform-source copy that does not observe the cancel) keeps the cancel SET and ends this host — never a run that
        reads a tree the worker can still change."""
        if self.warm_thread and self.warm_thread.is_alive():
            self.warm_cancel.set()
            self.warm_thread.join(timeout=self.warm_stop_seconds)
            if self.warm_thread.is_alive():
                raise Fatal(f"the warm build did not stop within {self.warm_stop_seconds:g} s of a claim — this host claims nothing more (the claim is "
                            "released by the lease), and Kubernetes replaces the container")
        self.warm_cancel.clear()

    def deployment(self, key: Optional[str] = None) -> str:
        """This host's ONE tree directory (Plugins Hosting/OneBuildTree.md): its channel — or `versioned` for a versioned
        server, whose version is a property of the set, not of the tree — never the build identity (`key` is accepted and
        ignored: a new identity rebuilds this tree in place)."""
        return deployment_name(self.channel or "versioned")

    def warm_main_when_idle(self) -> None:
        """§7.3b, the WRITER's half: while nothing is claimable, advance each known repository's deployment tree to the
        default branch's tip IN PLACE and run the TIP's declared `warmBuild` in it, in the sandbox, once per (tip, key).
        Never raises: warming is optional, so a failure is said, the tree is left "not built", and the next idle cycle
        retries after a pause."""
        key = warm_key(self.env, self.identity)
        if not self.legacy_retired:
            retire_legacy_shared(self.cache, self.log)     # §7.3's generations: removed once per process, before warming
            self.legacy_retired = True
        self.bound_slices()                                # every idle pass: the slice cache under its bound
        # every idle pass: the one-time migration — the lane's LEGACY keyed deployments (a tree per set and image), once unused and quiet
        retire_legacy_deployments(self.cache, self.lane, self.deployment(key), self.log,
                                 _git_runner(self.env, self.work, None, time.monotonic() + 120))
        repos = set(self.warm_repos)
        # every repository this deployment already holds a tree of is kept warm by whichever replica is idle, not only by
        # one that ran it since its own start (an unreadable listing only narrows this pass)
        try:
            root = os.path.join(self.cache, DEPLOY_DIR, self.deployment(key))
            repos |= {r for r in os.listdir(root) if REPO.match(r) and not r.startswith(".")}
        except OSError:
            pass
        for repo in sorted(repos):
            if self.stopping or self.warm_cancel.is_set():
                return                                     # a claim needs the host: no further repository is started
            self._warm_deployment(repo, key)

    def _warm_deployment(self, repo: str, key: str) -> str:
        """Advance and build `repo`'s deployment tree as its ONE writer. Returns "built", "skipped" (already warm, cooling
        down, no tip, or another writer / a reader holds the tree), "failed" or "stopped"."""
        dt = DeploymentTree(self.cache, self.deployment(key), repo)
        tip = self._mirror_tip(repo)
        if not tip:
            return "skipped"                               # the master names no tip: nothing to warm
        st = dt.state()
        if st is not None and st.get("key") == key and st.get("tip") == tip and st.get("built") in (tip, "none"):
            return "skipped"
        failed = self.warm_failed.get(repo)
        if failed is not None and failed[:2] == (tip, key) and time.monotonic() - failed[2] < WARM_RETRY_SECONDS:
            return "skipped"                               # cooling down: a failing tip is not rebuilt every pass
        try:
            with dt.writer() as w:
                if w is None:
                    return "skipped"                       # another writer, or a run reading the tree: never changed under it
                return self._warm_one(repo, key, w, tip)
        except (Refusal, subprocess.SubprocessError, OSError, ValueError) as e:
            self.warm_failed[repo] = (tip, key, time.monotonic())
            self.log(f"deployment tree: {repo} not warmed ({type(e).__name__}: {str(e)[:200]}) — retried when idle")
            return "failed"

    def _mirror_tip(self, repo: str) -> str:
        mirror = master_path(self.cache, repo)
        genv = {**{k: self.env[k] for k in ("PATH", "LANG") if self.env.get(k)}, "HOME": self.work, "GIT_TERMINAL_PROMPT": "0"}
        try:
            rc, out = self.git_runner(["--git-dir", mirror, "rev-parse", "--verify", "HEAD^{commit}"], genv)
        except (OSError, subprocess.SubprocessError):
            return ""
        tip = out.strip().splitlines()[-1].strip().lower() if rc == 0 and out.strip() else ""
        return tip if SHA.match(tip) else ""

    def _warm_one(self, repo: str, key: str, dt: DeploymentTree, tip: str) -> str:
        """Holding `dt`'s writer lock: move the tree to `tip` in place and run the tip's warmBuild in it, then the manifest
        and the state (`publish`) before the lock is released. Returns "built" (the tip's build, or a sources-only tree),
        "failed" or "stopped" — either of the last two leaves the tree "not built", so every reader goes cold."""
        scratch = tempfile.mkdtemp(prefix=".warm-", dir=self.work)
        try:
            g = _git_runner(self.env, scratch, None, time.monotonic() + 600)
            self.log(advance_deployment_tree(dt, tip, key, g))
            try:
                with open(os.path.join(dt.tree, ".github", "build-stages.json")) as fh:
                    decl = validate_warm_build(json.load(fh))
            except (OSError, ValueError, Refusal) as e:
                self.log(f"deployment tree: the tip {tip[:12]} declares no usable warmBuild ({e}) — sources only")
                decl = None
            if decl is None:
                dt.publish(tip, key)
                self.warm_failed.pop(repo, None)
                return "built"
            # the image's PREPARED inputs, resolved for THIS build: `run_env` is the run's, and it is empty while idle
            prepared = prepared_env()
            extra = {**self.platform_env(), **prepared, "MW_BUILD_REPO": repo}
            extra.pop("MW_PLATFORM_SOURCE", None)
            out_dir = os.path.join(scratch, "out")
            os.makedirs(out_dir)
            warm_nuget_dir = os.path.join(scratch, "nuget")
            os.makedirs(warm_nuget_dir, exist_ok=True)
            extra["NUGET_PACKAGES"] = SANDBOX_NUGET
            # a pending WHOLE rebuild stays recorded until `publish`: an interrupted one is redone, never resumed incrementally
            whole = (dt.state() or {}).get("whole") is True
            dt.write_state({"key": key, "tip": tip, "built": "building", **({"whole": True} if whole else {})})
            started = self.clock()
            platform_src = self.env.get("MW_PLATFORM_SOURCE", "") or prepared.get("MW_PLATFORM_SOURCE", "")
            platform_built: Optional[str] = None
            with contextlib.ExitStack() as views:
                warm_binds = []
                warm_ro = [dt.mirror, os.path.join(self.cache, "nuget")]
                if platform_src and os.path.isdir(platform_src):
                    # The deployment's PLATFORM TREE (§7.3b step 2): core's worktree at the set's commit, built IN PLACE and
                    # kept — so every stage's platform view starts from core's warm obj/ and bin/ — appearing AT the
                    # prepared tree's own path, the path a run's stages are handed, so obj/ records the same platform paths
                    # in the warm build as in a run. Nothing is copied; a tree that cannot be made falls back to a
                    # throwaway view of the image's tree (said), which a stage then rebuilds.
                    extra["MW_PLATFORM_SOURCE"] = platform_src
                    pc = source_commit(platform_src)
                    try:
                        if pc is None:
                            raise Refusal("the image's platform source names no .source-commit")
                        base = self.env.get("MW_BUILD_GIT_BASE", "https://github.com/Systemorph").rstrip("/")
                        pmirror = master_path(self.cache, PLATFORM_REPO)
                        _update_mirror(PLATFORM_REPO, f"{base}/{PLATFORM_REPO}.git", os.path.dirname(pmirror), pmirror, [pc], g, self.log)
                        self.log(advance_platform_tree(dt, pc, g, whole=whole))
                        warm_binds.append((dt.platform, platform_src))
                        warm_ro.append(pmirror)              # the platform tree's .git names the platform master's worktree admin
                        platform_built = pc
                    except (Refusal, subprocess.SubprocessError, OSError) as e:
                        self.log(f"platform tree: not kept ({str(e)[:200]}) — this warm build builds core in a throwaway view, "
                                 f"and every stage rebuilds what it needs of core")
                        psrc = views.enter_context(platform_source_view(platform_src, os.path.join(scratch, "platform"), self.env, self.log))
                        warm_binds.append((psrc, platform_src))
                rc, text = self._sandboxed_capture(decl["command"], dt.tree, out_dir, scratch, extra,
                                                   warm_ro, [], decl["timeoutMinutes"] * 60,
                                                   cancel=self.warm_cancel, mount=SANDBOX_CHECKOUT,
                                                   binds=[(warm_nuget_dir, SANDBOX_NUGET), *warm_binds])
            if rc == 0:
                dt.publish(tip, key, platform_built)       # manifests, then state — still under the writer lock
                self.warm_failed.pop(repo, None)
                self.log(f"deployment tree: built main@{tip[:12]} with the tip's warmBuild in {int(self.clock() - started)} s "
                         f"({dt.tree}; integrity manifest written"
                         f"{f'; platform tree at {platform_built[:12]} built in place' if platform_built else ''})")
                return "built"
            dt.unbuild(key, tip)
            # the cooldown covers a failed BUILD too: an unchanged failing tip is not rebuilt every pass
            self.warm_failed[repo] = (tip, key, time.monotonic())
            self.log(f"deployment tree: the tip's warmBuild failed (exit {rc}) — not built (runs go cold), retried when idle: {text.strip()[-300:]}")
            return "failed"
        except Lost:
            # a claim (or SIGTERM) needs the host: the half-built tree is left NOT BUILT, never read by a run
            dt.unbuild(key, tip)
            self.log(f"deployment tree for {repo}: the warm build was stopped for a claim — not built, retried when idle")
            return "stopped"
        except (Refusal, subprocess.SubprocessError, OSError, ValueError) as e:
            try:
                dt.unbuild(key, tip)
            except OSError:
                pass
            self.warm_failed[repo] = (tip, key, time.monotonic())
            self.log(f"deployment tree for {repo}: not warmed ({type(e).__name__}: {str(e)[:200]}) — not built, retried when idle")
            return "failed"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def seed_run_tree(self, repo: str, sha: str, refs: list, dest: str, token: Optional[str], deadline: Optional[float]) -> str:
        """A run's tree (§7.3b): its deployment tree as the read-only LOWER of an overlay — the tree held by a SHARED lock in
        `self.run_holds` until run() has unmounted it — or, as the ONE fallback, a COLD worktree off the master, said.
        Nothing is ever copied. Returns the plan line, `bytes copied = N` measured."""
        key = warm_key(self.env, self.identity)
        dt = DeploymentTree(self.cache, self.deployment(key), repo)
        why = None
        if not run_overlay_enabled(self.env):
            why = ("this host cannot overlay (no user + mount namespace for the runner)" if str(self.env.get(RUN_OVERLAY_ENV, "")).lower() == "true"
                   else "the run overlay is not on for this host (buildHosts.runOverlay)")
        else:
            with contextlib.ExitStack() as hold:
                held = hold.enter_context(dt.reader(key))
                if held is None:
                    why = dt.last_refusal
                else:
                    lower, tip = held
                    self.run_platform_lower = dt.platform_lower    # held by the same shared lock, for the stages' views
                    started = time.monotonic()
                    try:
                        overlay_run_tree(repo, sha, refs, lower, tip, dest, token, self.env, self.log, deadline=deadline,
                                         mount=self.run_mount)
                        self.run_holds.enter_context(hold.pop_all())   # held until run() released the overlay
                        n = copied_bytes(dest + ".upper", lower)
                        return (f"workspace: overlay of the deployment tree main@{tip[:12]} (built by the tip's warmBuild), "
                                f"moved to {sha[:12]} in {time.monotonic() - started:.1f} s — the run writes only its delta "
                                f"(obj/ and bin/ kept); bytes copied = {n}")
                    except (Refusal, OSError) as e:
                        why = f"the overlay failed ({str(e)[:160]})"
                        release_run_tree(dest)
                        for d in (dest, dest + ".upper", dest + ".ovlwork"):
                            shutil.rmtree(d, ignore_errors=True)
        self.log(f"COLD WORKTREE: {repo}@{sha[:12]} — {why}; this run takes a cold tree off the master "
                 f"(its own .git, objects borrowed; no warm obj/ or bin/; nothing copied)")
        g = _git_runner(self.env, os.path.dirname(dest), token, deadline)
        base = self.env.get("MW_BUILD_GIT_BASE", "https://github.com/Systemorph").rstrip("/")
        mirror = master_path(self.cache, repo)
        wanted = [sha] + [r for r in (refs or []) if isinstance(r, str) and r]
        _update_mirror(repo, f"{base}/{repo}.git", os.path.dirname(mirror), mirror, wanted, g, self.log)
        cold_run_tree(self.cache, repo, sha, dest, base, g)
        self.cold_tree = None                            # its own .git, inside the workdir: gone with it, nothing registered
        return f"workspace: COLD worktree of the master at {sha[:12]} ({why}); bytes copied = {copied_bytes(dest, None)}"

    # -- step 3/4: run ----------------------------------------------------------------------
    def run(self, claim: dict) -> str:
        repo = str(claim.get("repo") or "")
        sha = str(claim.get("sha") or "").lower()
        path = str(claim.get("path") or "")
        publish = claim.get("publish") is True
        trunk = claim.get("isTrunk") is True
        if not REPO.match(repo) or not SHA.match(sha):
            self.log(f"claim {path!r} carries an unusable repo/sha ({repo!r}, {sha!r}) — not running it")
            return "idle"
        started = self.clock()
        self.sweep_workspaces()
        workdir = tempfile.mkdtemp(prefix=f"{repo}-{sha[:12]}-", dir=self.work)
        self.run_platform_lower = None                   # set by seed_run_tree when the deployment's platform tree is warm
        # the run's tree is always its OWN (an overlay of its deployment tree, or a cold worktree — §7.3b), gone with the workdir
        checkout_dir = os.path.join(workdir, "src")
        outputs_dir = os.path.join(workdir, "out")
        self.artifacts_dir = os.path.join(workdir, "artifacts")
        stages: list[dict] = []
        self.run_sinks: dict[str, EventSink] = {}       # seq restarts at 1 for every assignment
        probe_id = claim.get("probe")
        self.probe = str(probe_id) if isinstance(probe_id, str) and probe_id.strip() else None
        # Run-scoped calls (events, heartbeat, token) name the CLAIMED RUN, which the server finds by
        # (repo, sha, channel): the claim answer's channel — a versioned server's own scope is its
        # version, used only for claiming, the queue and its inputs.
        claimed = claim.get("channel")
        self.run_scope = {"channel": claimed.strip()} if isinstance(claimed, str) and claimed.strip() else {"channel": self.channel}
        if claim.get("claimId"):
            self.claim_id = str(claim["claimId"])
        self.run_env = prepared_env()
        self.run_holds = contextlib.ExitStack()        # what the run holds until its tree is released (the deployment tree's shared lock)
        self.cold_tree = None                           # (repo, path) of a cold worktree this run took off the master
        with Heartbeat() as hb:
            self.heartbeat = hb
            outcome = "lost"
            try:
                outcome = self._run(claim, repo, sha, path, publish, trunk, started, workdir, checkout_dir,
                                    outputs_dir, stages)
                return outcome
            finally:
                self.stop_module_broker()
                self.heartbeat = None
                self.run_env = {}
                # A run refused before any stage never created its artifacts directory: nothing was
                # handed over, so there is no receipt to keep the workspace for.
                if (outcome == "undelivered" and os.path.isdir(self.artifacts_dir)
                        and os.listdir(self.artifacts_dir)):
                    # H9: the workspace goes only after every receipt was ACKNOWLEDGED. It is kept
                    # (and named) for the instance's re-queue, and swept once older than two deadlines.
                    self.log(f"{path}: receipts not acknowledged — keeping the workspace {workdir}")
                    release_run_tree(checkout_dir)
                else:
                    if self.cold_tree is not None:
                        # the cold worktree is removed AND pruned under the master lock: nothing stays registered
                        remove_cold_worktree(self.cache, self.cold_tree[0], self.cold_tree[1],
                                             _git_runner(self.env, workdir, None, None))
                    remove_workspace(workdir)
                self.cold_tree = None
                # the overlay is gone: only now may the deployment tree beneath it be advanced by its writer
                self.run_holds.close()

    def _sink_for(self, repo: str, sha: str, stage: str) -> EventSink:
        """ONE sink per stage route for the whole run, so its batch numbers are one sequence."""
        sink = self.run_sinks.get(stage)
        if sink is None:
            sink = self.run_sinks[stage] = self.sink(repo, sha, stage)
        if self.heartbeat is not None:
            self.heartbeat.target = sink
        return sink

    def _run(self, claim, repo, sha, path, publish, trunk, started, workdir, checkout_dir, outputs_dir, stages) -> str:
        artifacts_dir = self.artifacts_dir
        try:
            fallback = [c.get("name") if isinstance(c, dict) else c for c in (claim.get("stages") or [])]
            last_stage = next((bare_stage(s) for s in fallback if isinstance(s, str) and s), "verdict")
            # The lease is kept from the first second — a first clone into an empty mirror can take
            # longer than the instance's lease, and the checkout posts nothing else.
            self._sink_for(repo, sha, last_stage)
            self.log(f"claimed {path} ({repo}@{sha[:12]}, {'version' if self.version else 'channel'} {self.lane}, trunk={trunk}, publish={publish})")
            # Under the lease (the heartbeat beats the sink registered above): the mode migration of folders an
            # older runner rebuilt walks the whole warm cache once, before warming, preparing or any stage.
            repair_nuget_cache(os.path.join(self.cache, "nuget"), self.log)
            token = self.installation_token(claim)
            try:
                # The checkout is bounded by the run's deadline: a stalled clone or fetch ends the run
                # RED inside the pod's grace period, never holds the claim indefinitely.
                co_deadline = time.monotonic() + max(1.0, self.run_deadline - (self.clock() - started))
                if warm_enabled(self.env) and self.checkout is default_checkout:
                    self.warm_repos.add(repo)
                    # §7.3b: the deployment tree as the read-only lower of an overlay (held for the WHOLE run, released in
                    # run() after the unmount), or a cold worktree off the master — never a copy
                    self.workspace_line = self.seed_run_tree(repo, sha, claim.get("refs") or [], checkout_dir, token, co_deadline)
                    self.log(self.workspace_line)
                else:
                    self.workspace_line = "workspace: clean (a fresh clone per run — MW_BUILD_WARM_WORKSPACE is not set)"
                    self.checkout(repo, sha, claim.get("refs") or [], checkout_dir, token, self.env, self.log, deadline=co_deadline)
                # ONE merged tree per attempt (policy suites-test-fresh-merge): a pull-request run's
                # head is merged onto ONE base — the claim's `mergeBaseSha`, or main's tip resolved
                # ONCE from the mirror when the claim names none — and every stage runs on that tree.
                # The STAGE DECLARATION is read from the MERGED tree, so a command, dependency or
                # timeout the merge brings in is the one that runs. Trunk and probe runs are not
                # merged. The base is named in the plan; the instance refuses a PR success whose plan
                # names another sha, or none.
                merge_base = None
                probe = claim.get("isProbe") is True or str(claim.get("kind") or "").lower() == "probe"
                # a probe that NAMES a pull request is measured on the tree that pull request's own run builds: merged once
                # onto the same base (its narrowing reads HEAD^2...HEAD, its module hub the pull-request lane); a plain
                # probe is not merged
                if not trunk and (not probe or pull_request_number(claim.get("refs") or []) is not None):
                    merge_base = self.merge_onto_base(repo, checkout_dir, claim.get("mergeBaseSha"), token)
                declared = load_stages(os.path.join(checkout_dir, ".github", "build-stages.json"))
                prepared_inputs = load_prepared(os.path.join(checkout_dir, ".github", "build-stages.json"))
                parallel = load_parallel(os.path.join(checkout_dir, ".github", "build-stages.json"))
                stages = select_stages(declared, claim.get("stages"))
                # the module-hub broker (§7.4 wiring): fingerprints from the PRISTINE tree, before any stage runs
                self.start_module_broker(repo, sha, claim, trunk, checkout_dir, workdir)
                if probe and claim.get("requestedStages"):
                    # A PROBE runs EXACTLY the stages it asked for (`requestedStages`; empty = every
                    # declared stage) and plans only those; one the repository does not declare is red.
                    stages = select_stages(declared, claim.get("requestedStages"))
                elif not probe and trunk and claim.get("servedStages"):
                    # a TRUNK run is never served in part: its verdict publishes, so every declared stage runs
                    self.log("served stages ignored on a trunk run: every declared stage runs")
                elif not probe and isinstance(claim.get("servedStages"), list) and claim.get("servedStages"):
                    # A SERVED run (another channel's, taken by this serving host): only the stages this host's channel
                    # is declared to run of it (control's Hosting:Builds:ServesStages). A declared name the repository
                    # does not have is skipped — the declaration is the operator's, the stages the repository's — but a
                    # list that matches NO declared stage (a renamed stage, a typo) is red, never a green run of nothing.
                    wanted = [n for n in claim["servedStages"] if isinstance(n, str)]
                    names = [st.get("name") for st in declared]
                    matched = [n for n in wanted if n in names]
                    if not matched:
                        raise Refusal(f"served stages {', '.join(wanted) or '(none named)'} match none of the stages the "
                                      f"repository declares ({', '.join(str(n) for n in names)})")
                    stages = select_stages(declared, matched)
                    self.log(f"served run: this host runs {', '.join(st['name'] for st in stages) or 'no stage'} of it "
                             f"(declared for the serving channel: {', '.join(wanted)})")
            except Refusal as r:
                sink = self._sink_for(repo, sha, last_stage)
                sink.add([{"kind": "line", "text": f"run refused before any stage: {r}"},
                          {"kind": "stage-finished", "state": "failed", "summary": str(r)[:MAX_LINE]},
                          {"kind": "run-finished", "succeeded": False, "error": str(r)[:MAX_LINE]}])
                return "ran" if sink.flush(attempts=12) else "undelivered"
            # The PLAN, host-emitted and never a child's: the instance refuses a host-run success
            # unless every planned stage reported success.
            plan = self._sink_for(repo, sha, stages[0]["name"])
            # The run's declared execution budget: the instance sets the attempt's deadline to claim
            # time + budget + 15 min. The host enforces the same number (`budget`, below).
            warm = self.nuget_warm_plan(repo, workdir)
            # Fan-out: the repository declares its stages safe to run concurrently (`parallel`), the host declares how many
            # may (`MW_BUILD_STAGE_SLOTS`); either one absent is today's serial run.
            slots = self.stage_slots if parallel else 1
            fanout = slots > 1
            budget = run_budget_seconds(stages, self.run_deadline - (self.clock() - started), prepared_inputs, trunk,
                                        warm["timeoutMinutes"] if warm["state"] == "warm" else 0.0, slots=slots)
            plan_event = {"kind": "plan", "stages": [st["name"] for st in stages], "budgetMinutes": budget_minutes(budget)}
            if merge_base:
                plan_event["mergeBase"] = merge_base
            plan.add([plan_event])
            if self.workspace_line:
                # how the tree was seeded (§7.2): every verdict can be traced to the state it started from
                plan.add([{"kind": "line", "text": self.workspace_line}])
            if fanout:
                plan.add([{"kind": "line", "text": f"fan-out: up to {slots} stage(s) in flight at once (the repository "
                                                   f"declares `parallel`; a build stage runs alone)"}])
            elif parallel:
                plan.add([{"kind": "line", "text": "serial: the repository declares `parallel`, but this host has ONE "
                                                   "stage slot (MW_BUILD_STAGE_SLOTS)"}])
            self.deliver(plan)
            os.makedirs(outputs_dir, exist_ok=True)
            os.makedirs(artifacts_dir, exist_ok=True)
            # MW_BUILD_PUBLISH is INFORMATIONAL: a host holds no publication credential, so a stage
            # cannot publish whatever it says; publication is the publish lane's, on the aggregate check `build`.
            extra = {
                "MW_BUILD_PUBLISH": "true" if publish else "false",
                "MW_BUILD_CHANNEL": self.channel, "MW_BUILD_VERSION": self.version, "MW_BUILD_REPO": repo, "MW_BUILD_SHA": sha,
                "MW_BUILD_RUN_PATH": path, "MW_BUILD_IS_TRUNK": "true" if trunk else "false",
                "MW_PLUGINS_DIR": self.plugins_dir or "", "MW_PLUGINS_DIGEST": self.plugins_digest or "",
                "MW_BUILD_CACHE_OUTPUTS": outputs_root(self.cache, repo), "MW_BUILD_ARTIFACTS": artifacts_dir,
                "MW_BUILD_OUTPUTS": outputs_dir,
                "MW_PLUGINS_KEY": self.effective_plugins_key(),
                **self.platform_env(),
            }
            # The pull request's LIVE labels, as the admission's change set read them and the claim carries them: a
            # host holds no credential to read them, so a stage classifies with these or with none (said by the stage).
            labels = claim_labels(claim)
            if labels is not None:
                extra["MW_BUILD_LABELS"] = labels
            # The base this attempt's head was merged onto, handed to every stage: when the head already CONTAINS it
            # (a settle pull request cut from main's tip) git answers "Already up to date" and makes NO merge commit, so a
            # selector that reads `HEAD^2` finds nothing and would select the FULL set. With the base named it narrows to
            # `<base>...HEAD` in both shapes. UNSET on a run that was not merged (trunk, a plain probe).
            if merge_base:
                extra["MW_BUILD_MERGE_BASE"] = merge_base
            # A stage is skipped only when a stage it runs AFTER failed (`after`); its independent siblings still
            # run. `failed` holds every red stage — ran red or was skipped — so a skip propagates to the skipped
            # stage's own dependants. `why` is a RUN-WIDE stop (a prepared input, the run deadline) that ends every
            # remaining stage. `failed_at` is the first red stage (the run's log line); any red stage reds the run.
            failed_at = None
            failed: set = set()
            ran_red: list = []
            why = ""
            run_end = self.clock() + budget
            prepared_ro: list[str] = []
            # NUGET_FALLBACK_PACKAGES names <cache>/nuget for EVERY stage that needs `nuget`, prepared inputs or not;
            # NuGet refuses a fallback folder that does not exist, so a fresh host creates it before any stage runs
            # (else the first trunk build — the one that warms it — could never restore).
            os.makedirs(os.path.join(self.cache, "nuget"), exist_ok=True)
            self.warm_nuget(repo, workdir, warm, self._sink_for(repo, sha, stages[0]["name"]), run_end)
            if prepared_inputs:
                psink = self._sink_for(repo, sha, stages[0]["name"])
                try:
                    penv, prepared_ro = self.prepare_inputs(repo, checkout_dir, workdir, trunk, prepared_inputs, psink, run_end)
                    extra.update(penv)
                except Refusal as r:
                    failed_at, why = stages[0]["name"], f"a prepared input could not be provided: {r}"
                    failed.add(failed_at)
                    psink.add([{"kind": "line", "text": why[:MAX_LINE]}])
                    self.deliver(psink)
            # THE SCHEDULER (fan-out, `next_stages`): one slot unless the repository declares `parallel` and the host
            # has more. Each started stage runs in its own thread (its own sandbox, sink and — with fan-out — its own
            # artifacts directory); everything AFTER a stage (the restored-package report, the promotions, the
            # quarantine, the leftover check) runs here, on the scheduler's thread, one stage at a time.
            selected = {s["name"] for s in stages}
            # The PIN: with fan-out a stage also waits for every stage the budget's all-caps schedule finished before it
            # started, so the declared budget bounds the run whatever the stages' real durations (`cap_schedule`).
            pin = cap_schedule(stages, slots)[1] if fanout else None
            finished: set = set()
            pending = list(stages)
            inflight: dict = {}            # name → (thread, stage, nuget_before, art_before, artifacts dir)
            done: "queue.Queue[tuple[str, bool, Optional[BaseException]]]" = queue.Queue()
            cancel = threading.Event()
            lost: Optional[BaseException] = None
            # The stages' OWN package folder lives in the run's workspace (not the sandbox's tmpfs), so the host can SEE
            # what a run restored: a package missing from the warm cache is a server that was not prepared for it —
            # said, and on a trunk build promoted, so the next run restores nothing.
            nuget_dir = os.path.join(workdir, "nuget")
            os.makedirs(nuget_dir, exist_ok=True)

            def stage_artifacts(name: str) -> str:
                # With fan-out every stage writes and hands over ITS OWN directory: a concurrent sibling can neither
                # see nor contribute to it, so a red stage's files can never be receipted under a green sibling.
                return os.path.join(artifacts_dir, name) if fanout else artifacts_dir

            def not_run(st: dict, reason: str) -> None:
                nonlocal failed_at
                pending.remove(st)
                failed.add(st["name"])
                finished.add(st["name"])
                failed_at = failed_at or st["name"]
                sink = self._sink_for(repo, sha, st["name"])
                sink.add([{"kind": "stage-finished", "state": "failed", "summary": f"not run — {reason}"}])
                self.deliver(sink)

            def start(st: dict) -> None:
                sink = self._sink_for(repo, sha, st["name"])
                self.drain_held(attempts=2)
                art = stage_artifacts(st["name"])
                os.makedirs(art, exist_ok=True)
                nuget_before = nuget_packages(nuget_dir)
                art_before = artifact_snapshot(art)
                remaining = run_end - self.clock()
                spec = dict(st, timeoutMinutes=min(st["timeoutMinutes"], remaining / 60))
                # the run's package folder is bound at SANDBOX_NUGET: the path restore records is the warm build's too
                stage_env = {**extra, "MW_BUILD_STAGE": st["name"], "NUGET_PACKAGES": SANDBOX_NUGET, "MW_BUILD_ARTIFACTS": art,
                             **({"MW_BUILD_MODULE_HUB": self.module_broker.io, "MW_BUILD_MODULE_HUB_OUT": self.module_broker.out}
                                if self.module_broker else {})}
                readonly = self.stage_readonly(repo, st) + prepared_ro
                if self.module_broker:
                    readonly = readonly + [self.module_broker.out]      # the host's answers: the stage reads, never writes

                def work(spec=spec, sink=sink, art=art, stage_env=stage_env, readonly=readonly, name=st["name"],
                         art_before=art_before) -> None:
                    try:
                        with contextlib.ExitStack() as pviews:
                            ok = self.run_stage(spec, checkout_dir, outputs_dir, sink, stage_env,
                                            readonly=readonly, writable=[art, *([self.module_broker.io] if self.module_broker else [])],
                                            binds=[(nuget_dir, SANDBOX_NUGET),
                                                   *self.stage_platform_binds(stage_env, workdir, name, pviews)],
                                            hand_over=lambda: self.hand_over_artifacts(repo, sha, name, art, deadline=run_end,
                                                                                       cancel=cancel),
                                            cancel=cancel,
                                            hand_over_failed=lambda: self.hand_over_failed_stage(
                                                repo, sha, name, art, art_before, deadline=run_end, cancel=cancel))
                        done.put((name, ok, None))
                    except BaseException as e:     # Lost, or a host defect: the scheduler decides, never a dead thread
                        # The run is abandoned the moment a worker KNOWS it — not when the scheduler reads this from the
                        # queue: a sibling's success queued ahead of it must not get its freed slot refilled first.
                        cancel.set()
                        done.put((name, False, e))

                t = threading.Thread(target=work, name=f"stage-{st['name']}", daemon=True)
                inflight[st["name"]] = (t, st, nuget_before, art_before, art)
                pending.remove(st)
                t.start()

            def after_stage(name: str, ok: bool) -> None:
                nonlocal failed_at
                _t, st, nuget_before, art_before, art = inflight.pop(name)
                finished.add(name)
                sink = self._sink_for(repo, sha, name)
                if not inflight:
                    # Between stages only: while a sibling runs, its sandbox IS a process in another pid namespace.
                    self.assert_clean()
                restored = sorted(nuget_packages(nuget_dir) - nuget_before)
                if restored:
                    # OFFLINE, a package restored per run is a hole in the preparation and the stage's verdict says so
                    # (an ERROR); open, it is said as a warning while the preparation is completed
                    sink.add([{"kind": "diagnostic", "severity": "error" if self.stage_offline else "warning", "code": "MWPREP001",
                               "message": f"{len(restored)} NuGet package(s) restored PER RUN — the server's warm cache "
                                          f"did not hold them: {', '.join(restored[:10])}"
                                          + (" — the run is OFFLINE (MW_BUILD_STAGE_NETWORK=offline): the preparation (the "
                                             "warm restore of the default branch's tip) must fetch them" if self.stage_offline
                                             else "" if trunk else " (a trunk build promotes them)")},
                              {"kind": "line", "text": f"prepared server: {len(restored)} package(s) restored per run"}])
                    self.deliver(sink)
                # The folder accumulates what the prepared builds and earlier stages restored too (already present
                # before this stage, so not in `restored`): promote ALL of it after every successful trunk build —
                # promote_nuget keeps a package the warm cache already holds. A build stage runs ALONE, so nothing
                # writes the folder while it is promoted.
                if ok and trunk and st["kind"] == "build" and nuget_packages(nuget_dir):
                    np_, nk, nr = promote_nuget(nuget_dir, self.cache, self.log, self.nuget_feeds,
                                                budget=run_end - self.clock())
                    sink.add([{"kind": "line", "text": f"warm NuGet cache: {np_} package(s) promoted, {nk} already present"
                                                       + (f", {len(nr)} NOT promoted (unverified)" if nr else "")}])
                    self.deliver(sink)
                if not ok:
                    failed.add(name)
                    ran_red.append(name)
                    # A red stage's artifacts never reach a GREEN hand-over. Its own hand-over already sent a COPY of them,
                    # every receipt marked failed (hand_over_failed_stage). Without fan-out the directory is shared and a
                    # later sibling's hand-over walks all of it, so what the red stage added or rewrote is quarantined
                    # beside the run, out of that walk; with fan-out the stage's own directory goes whole.
                    quarantined = quarantine_artifacts(art, art_before, os.path.join(workdir, "failed-artifacts", name))
                    if quarantined:
                        sink.add([{"kind": "line", "text": (f"artifacts: {len(quarantined)} entry(ies) left by the red stage "
                                                            f"quarantined, never handed over as a green stage's: "
                                                            f"{', '.join(quarantined[:5])}")[:MAX_LINE]}])
                        self.deliver(sink)
                    failed_at = failed_at or name
                elif trunk and st["kind"] == "build" and ran_red:
                    # The outputs folder is shared by every stage of the run, and a stage that ran red may have
                    # left a PARTIAL output under a valid key (cache_key_problem verifies the key, never that the
                    # output is complete). Once any stage ran red, nothing more from this run enters the cache.
                    sink.add([{"kind": "line", "text": f"cache: outputs NOT promoted — stage {ran_red[0]} ran red "
                                                       f"earlier in this run and may have left partial outputs"}])
                    self.deliver(sink)
                elif trunk and st["kind"] == "build":
                    # Only COMPILATION is reused across runs — never a test verdict — and only a key
                    # the host can RECOMPUTE from the inputs the output names (cache_key_problem).
                    promoted, kept, refused = promote_outputs(outputs_dir, self.cache, self.log,
                                                              verify=lambda d: self.cache_key_problem(d, checkout_dir),
                                                              repo=repo)
                    if promoted or kept or refused:
                        sink.add([{"kind": "line", "text": f"cache: {promoted} output(s) promoted, {kept} already "
                                                           f"present" + (f", NOT promoted (not a content key): "
                                                                         f"{refused[:5]}" if refused else "")}])
                        self.deliver(sink)

            try:
                # Once the run is lost nothing more starts: the loop drains only what is in flight. `cancel` is the
                # signal — a worker sets it the moment it meets the 409, before its exception reaches this queue.
                while (pending and lost is None and not cancel.is_set()) or inflight:
                    if lost is None and not cancel.is_set():
                        if not why and run_end - self.clock() <= 0:
                            why = f"the run deadline ({budget} s) passed"
                        if why:
                            for st in list(pending):
                                not_run(st, why)
                        while pending and not cancel.is_set():
                            blocked, ready = next_stages(pending, selected, finished, failed,
                                                         {n: v[1]["kind"] for n, v in inflight.items()}, slots, pin)
                            for st, dep in blocked:
                                not_run(st, f"stage {dep}, which it runs after, failed first")
                            for st in ready:
                                if cancel.is_set():
                                    break
                                start(st)
                            if not blocked and not ready:
                                break
                        if pending and not inflight and not cancel.is_set():
                            # Unreachable: every dependency is declared earlier, so with nothing in flight the first
                            # pending stage is ready or blocked. A scheduler that stalls is a host defect — said, red.
                            raise Fatal(f"the stage scheduler made no progress with {[s['name'] for s in pending]} pending")
                    if not inflight:
                        continue
                    try:
                        name, ok, err = done.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    inflight[name][0].join()
                    if err is not None:
                        inflight.pop(name)
                        if lost is None:
                            lost = err
                            cancel.set()      # every sibling kills its sandbox and stops posting for this run
                        continue
                    after_stage(name, ok)
            except BaseException:
                cancel.set()
                for t, *_ in list(inflight.values()):
                    t.join()
                raise
            if lost is not None:
                self.assert_clean()
                raise lost
            last_stage = stages[-1]["name"]
            # run-finished is the LAST word: never before every stage-finished was acknowledged —
            # a run verdict over a missing stage verdict is a contradiction the instance cannot heal.
            for _ in range(10):
                if self.drain_held(attempts=6):
                    break
                self.sleep(30.0)
            if self.held:
                self.log(f"{path}: {len(self.held)} stage verdict(s) could not be delivered — NOT posting run-finished; "
                         f"the instance's adoption re-queues the run")
                self.held = []
                return "undelivered"
            sink = self._sink_for(repo, sha, last_stage)
            error = "; ".join(([f"stage {ran_red[0]} failed" if len(ran_red) == 1
                                else f"stages {', '.join(ran_red)} failed"] if ran_red else []) + ([why] if why else []))
            sink.add([{"kind": "run-finished", "succeeded": failed_at is None,
                       "error": None if failed_at is None else (error or f"stage {failed_at} failed")}])
            if not sink.flush(attempts=12):
                self.log(f"{path}: run-finished could not be delivered — the instance's adoption re-queues the run")
                return "undelivered"
            self.log(f"{path}: {'succeeded' if failed_at is None else 'failed at ' + failed_at} in "
                     f"{_hms(self.clock() - started)}")
            return "ran"
        except Lost as e:
            # The abandoned run's undelivered verdicts go with it: they belong to a claim this host no
            # longer holds, so retrying them before the NEXT run's posts would only draw 409s again
            # and abandon that run too — every later claim, until a restart.
            self.held = []
            self.log(f"{path}: {e} — abandoning the run")
            return "lost"

    def sink(self, repo: str, sha: str, stage: str) -> EventSink:
        return EventSink(self.api, self.host, repo, sha, stage, self.run_scope, self.log,
                         sleep=self.sleep, clock=self.clock, claim_id=self.claim_id, probe=self.probe)

    def installation_token(self, claim: dict) -> Optional[str]:
        """A one-hour installation token for the CHECKOUT only (`POST /token`, Build service §1.3).
        It stays in the host process; no stage ever sees it. None ⇒ clone anonymously."""
        status, body = self.api.call("POST", "/token", {"host": self.host, "path": claim.get("path"),
                                                        "repo": claim.get("repo"), "sha": claim.get("sha")},
                                     {**self.run_scope, **({"probe": self.probe} if self.probe else {})})
        if status == 200 and isinstance(body, dict) and body.get("token"):
            return str(body["token"])
        self.log(f"no installation token for {claim.get('repo')} (HTTP {status}) — cloning anonymously")
        return None

    def stage_refset(self) -> Optional[str]:
        """The composed gate host (the portal's /app with the tester beside it) staged into the
        shared cache keyed by BOTH digests — `<cache>/refsets/<sha256(tester|portal)>/host` — so a
        reference set is never reused across a change of either image. Rebinds MW_BUILD_GATE_HOST
        (and the sandbox's read-only bind) to the staged copy. None when there is nothing to stage."""
        src = self.env.get("MW_BUILD_GATE_HOST", "")
        tester, portal = self.env.get("MW_BUILD_TESTER_DIGEST", ""), self.env.get("MW_BUILD_PORTAL_DIGEST", "")
        if not src or not os.path.isdir(src):
            return None
        if not DIGEST.match(tester) or not DIGEST.match(portal):
            raise Fatal("the reference set cannot be keyed: MW_BUILD_TESTER_DIGEST / MW_BUILD_PORTAL_DIGEST are not both sha256 digests")
        key = refset_key(tester, portal)
        root = os.path.join(self.cache, "refsets")
        final = os.path.join(root, key)
        if not os.path.isfile(os.path.join(final, ".complete")):
            os.makedirs(root, exist_ok=True)
            tmp = tempfile.mkdtemp(prefix=".refset-", dir=root)
            shutil.copytree(src, os.path.join(tmp, "host"), symlinks=True)
            with open(os.path.join(tmp, ".complete"), "w") as fh:
                fh.write(f"tester {tester}\nportal {portal}\n")
            try:
                os.rename(tmp, final)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)
        staged = os.path.join(final, "host")
        self.env["MW_BUILD_GATE_HOST"] = staged
        self.env["MW_BUILD_REFSET_KEY"] = key
        if isinstance(self.sandbox, Sandbox):
            self.sandbox.readonly = tuple(p for p in self.sandbox.readonly if p != src) + (staged,)
        return staged

    def merge_onto_base(self, repo: str, checkout: str, claimed: object, token: Optional[str]) -> str:
        """Merge the checked-out head onto ONE base and return that base's sha. `claimed` is the
        claim's `mergeBaseSha`: used VERBATIM (fetched if absent) when given; when null, main's tip
        is resolved ONCE from the repository's mirror (its HEAD — the default branch, fetched just
        before the checkout). The merge happens once, in the run's checkout, so every stage tests the
        same merged tree; a conflict is a Refusal naming the base."""
        genv = {**{k: self.env[k] for k in ("PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR") if self.env.get(k)},
                "HOME": os.path.dirname(checkout), **git_auth_env(token),
                "GIT_AUTHOR_NAME": "build host", "GIT_AUTHOR_EMAIL": "build-host@invalid",
                "GIT_COMMITTER_NAME": "build host", "GIT_COMMITTER_EMAIL": "build-host@invalid"}
        if claimed is not None:
            base = str(claimed).strip().lower()
            if not SHA.match(base):
                raise Refusal(f"the claim's mergeBaseSha {claimed!r} is not a full commit")
        else:
            mirror = os.path.join(self.cache, "git", f"{repo}.git")
            rc, out = self.git_runner(["--git-dir", mirror, "rev-parse", "--verify", "HEAD^{commit}"], genv)
            base = out.strip().splitlines()[-1].strip().lower() if rc == 0 and out.strip() else ""
            if not SHA.match(base):
                raise Refusal(f"main's tip could not be resolved from the mirror (exit {rc}): {out.strip()[-160:]}")
        rc, out = self.git_runner(["-C", checkout, "fetch", "--quiet", "origin", base], genv)
        if rc != 0:
            raise Refusal(f"the merge base {base[:12]} could not be fetched (exit {rc}): {out.strip()[-160:]}")
        rc, out = self.git_runner(["-C", checkout, "merge", "--no-ff", "--no-edit", "-m",
                                   f"build host: merge onto {base}", base], genv)
        if rc != 0:
            self.git_runner(["-C", checkout, "merge", "--abort"], genv)
            raise Refusal(f"the head does not merge onto {base[:12]} (exit {rc}) — a conflict is red, never a test of the unmerged head: {out.strip()[-200:]}")
        if re.search(r"up[ -]to[ -]date", out, re.I):   # "Already up to date." — older git hyphenates it
            self.log(f"the head already contains {base[:12]} — no merge commit; every stage sees the base as MW_BUILD_MERGE_BASE")
        else:
            self.log(f"merged the head onto {base[:12]} — every stage of this attempt tests that tree")
        return base

    def sweep_workspaces(self, now: Optional[float] = None) -> list[str]:
        """Remove kept workspaces older than two run deadlines (their receipts were never
        acknowledged; the instance re-queued the run long since). Returns what was removed."""
        now = time.time() if now is None else now
        gone = []
        try:
            names = os.listdir(self.work)
        except OSError:
            return gone
        for n in names:
            full = os.path.join(self.work, n)
            try:
                if os.path.isdir(full) and not os.path.islink(full) and now - os.path.getmtime(full) > 2 * self.run_deadline:
                    remove_workspace(full)
                    gone.append(n)
            except OSError:
                continue
        cache = getattr(self, "cache", None)
        if gone and cache:
            # a kept run that took a COLD worktree is gone now: prune its registration from the master, under the master lock
            git_root = os.path.join(cache, "git")
            try:
                masters = [m[:-4] for m in os.listdir(git_root) if m.endswith(".git") and REPO.match(m[:-4])]
            except OSError:
                masters = []
            g = _git_runner(getattr(self, "env", {}) or {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, self.work, None, None)
            for repo in masters:
                try:
                    with master_lock(git_root, repo):
                        g("--git-dir", master_path(cache, repo), "worktree", "prune")
                except (Refusal, OSError):
                    continue
        return gone

    def nuget_warm_plan(self, repo: str, workdir: str) -> dict:
        """What the warm NuGet cache needs for this repository's DEFAULT BRANCH tip, read from the host's mirror only (the
        run's own tree is never consulted): `state` is `warm` (the tip declares a restore whose key the cache does not
        hold yet — `tip`, `key`, `command`, `timeoutMinutes`, `marker`), `held` (it does) or `none` (nothing to do), with
        `text` saying which. Never raises: a mirror that cannot answer — a nonzero exit, a git that times out, an I/O error —
        is said and the run proceeds unwarmed (the cache is optional; a run's verdict never hangs on it)."""
        try:
            return self._nuget_warm_plan(repo, workdir)
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            return {"state": "none", "text": f"warm NuGet cache: the mirror could not be read ({type(e).__name__}: {str(e)[:160]}) "
                                             "— not warmed this run"}

    def _nuget_warm_plan(self, repo: str, workdir: str) -> dict:
        mirror = os.path.join(self.cache, "git", f"{repo}.git")
        genv = {**{k: self.env[k] for k in ("PATH", "LANG") if self.env.get(k)}, "HOME": workdir, "GIT_TERMINAL_PROMPT": "0"}
        rc, out = self.git_runner(["--git-dir", mirror, "rev-parse", "--verify", "HEAD^{commit}"], genv)
        tip = out.strip().splitlines()[-1].strip().lower() if rc == 0 and out.strip() else ""
        if not SHA.match(tip):
            return {"state": "none", "text": f"warm NuGet cache: the default branch's tip could not be resolved from the mirror "
                                             f"(exit {rc}) — not warmed this run"}
        rc, doc = self.git_runner(["--git-dir", mirror, "show", f"{tip}:.github/build-stages.json"], genv)
        try:
            decl = validate_restore(json.loads(doc)) if rc == 0 else None
        except (ValueError, Refusal) as e:
            return {"state": "none", "text": f"warm NuGet cache: the default branch's tip {tip[:12]} declares no usable `restore` ({e})"}
        if decl is None:
            return {"state": "none", "text": f"warm NuGet cache: the default branch's tip {tip[:12]} declares no `restore` — "
                                             "the cache is filled only by trunk builds"}
        rc, listing = self.git_runner(["--git-dir", mirror, "ls-tree", "-r", "--full-tree", tip], genv)
        if rc != 0:
            return {"state": "none", "text": f"warm NuGet cache: the default branch's tip {tip[:12]} could not be listed (exit {rc})"}
        key = nuget_warm_key(listing, decl["command"], self.platform_version)
        marker = nuget_warm_marker(self.cache, repo, key)
        if os.path.isfile(marker):
            return {"state": "held", "text": f"warm NuGet cache: holds the default branch's restore (tip {tip[:12]}, key {key[:12]}) "
                                             "— nothing restored for the server"}
        return {"state": "warm", "tip": tip, "key": key, "marker": marker, **decl}

    def platform_env(self) -> dict:
        """The INSTALLED platform's facts a repository-declared command on this host is handed: the set's identity (what
        a stage needs to compute the cross-run cache keys the host will recompute), the platform's canonical checkers and
        the prepared host's facts (`prepared_env`: core's source at the set's commit, the chart, the tester /app, the
        PostgreSQL snapshot). Every stage gets them, and so does the default branch's warm restore (`warm_nuget`) — a
        repository whose projects reference the platform cannot restore without them. The reference set itself
        (MeshWeaverRefs, MW_BUILD_COMPILE_REFS) reaches both through the allow-list (ENV_ALLOW) and the sandbox's
        read-only roots (sandbox_readonly_roots)."""
        return {
            "MW_BUILD_PLATFORM_VERSION": self.platform_version,
            "MW_BUILD_FRAMEWORK_IDENTITY": self.identity,
            "MW_BUILD_TESTER_DIGEST": self.env.get("MW_BUILD_TESTER_DIGEST", ""),
            "MW_BUILD_PORTAL_DIGEST": self.env.get("MW_BUILD_PORTAL_DIGEST", ""),
            "MW_BUILD_PLATFORM_REF": self.env.get("MW_BUILD_PLATFORM_REF", ""),
            "MW_BUILD_REFSET_KEY": self.env.get("MW_BUILD_REFSET_KEY", ""),
            **platform_scripts_env(os.path.dirname(os.path.abspath(__file__))),
            **self.run_env,
        }

    def stage_platform_binds(self, stage_env: dict, workdir: str, name: str, views: contextlib.ExitStack) -> list:
        """A run stage's OWN writable view of the platform source (§7.3b step 2), bound AT the path MW_PLATFORM_SOURCE names
        — the image's prepared tree — so a stage builds core where it lies, with the same absolute paths the warm build
        recorded in obj/. Its lower is the deployment's warm PLATFORM TREE when this run holds one (core's outputs carried),
        else the image's tree; an overlay, or a cold worktree of the platform master — never a copy. Each stage its own
        view (stages run in parallel and each writes core's obj/), removed when the stage ends (`views`). A view that
        cannot be made binds nothing: the stage sees the read-only tree and says so itself, never a copy."""
        target = stage_env.get("MW_PLATFORM_SOURCE", "")
        if not target or not os.path.isdir(target):
            return []
        lower = self.run_platform_lower or target
        try:
            view = views.enter_context(platform_source_view(lower, os.path.join(workdir, "platform", name), self.env, self.log,
                                                            create_master=False))
        except (Refusal, subprocess.SubprocessError, OSError) as e:
            self.log(f"stage {name}: no writable platform view ({str(e)[:200]}) — the stage sees the image's read-only tree")
            return []
        self.log(f"stage {name}: platform source writable at {target} — a view of "
                 f"{'the deployment platform tree (core warm)' if lower != target else 'the image tree (core cold)'}; nothing copied")
        return [(view, target)]

    def warm_nuget(self, repo: str, workdir: str, plan: dict, sink: "EventSink", run_end: float) -> None:
        """Fill `<cache>/nuget` from the DEFAULT BRANCH's tip per `plan` (`nuget_warm_plan`): the tip's OWN restore
        command, on a host-made checkout of the tip, handed the installed platform as a stage is (`platform_env`, the
        reference set) and its OWN writable view of the platform source (`platform_source_view`, as a prepared
        input's build gets one — MeshWeaver.Plugins' projects reference the platform, so a bare checkout cannot restore
        them), writing into a fresh host-owned folder; every package it fetched is
        verified and rebuilt into the cache (`promote_nuget`), and the key is marked once it restored cleanly. A failure
        is SAID (MWPREP003) and never reds the run — its stages still restore what they lack, per run."""
        if plan.get("state") != "warm":
            sink.add([{"kind": "line", "text": plan.get("text", "")[:MAX_LINE]}])
            self.deliver(sink)
            return

        def warn(msg: str) -> None:
            sink.add([{"kind": "diagnostic", "severity": "warning", "code": "MWPREP003", "message": msg[:MAX_LINE]}])
            self.deliver(sink)
        # Every host-side failure at this boundary (a git that times out, an I/O error) is SAID and the run goes on:
        # warming is optional, so it must never end the worker without a run verdict.
        try:
            tip, tree = self.default_branch_tree(repo, workdir)
        except (Refusal, subprocess.SubprocessError, OSError, ValueError) as r:
            warn(f"the warm NuGet cache was not filled: {r}")
            return
        if tip != plan["tip"]:
            warn(f"the warm NuGet cache was not filled: the default branch moved ({plan['tip'][:12]} → {tip[:12]}) while it was planned")
            return
        left = run_end - self.clock()
        if left <= 0:
            warn("the warm NuGet cache was not filled: the run deadline passed first")
            return
        # A FRESH folder, written only by this restore: nothing a stage or a per-run prepared build restored can reach it.
        fetched = os.path.join(workdir, "warm-nuget")
        # The restore gets what a stage gets of the installed platform (`platform_env`), and — like a prepared input's
        # build — its OWN writable VIEW of the platform source (`platform_source_view`: an overlay, or a cold worktree of the
        # platform's master — never a copy): a repository whose projects reference the platform
        # (MeshWeaver.Plugins: -p:MeshWeaverRoot="$MW_PLATFORM_SOURCE/") restores those projects too, and restore writes
        # their obj/ into that tree. A host whose image carries no platform source hands none (said by the restore).
        platform_src = self.env.get("MW_PLATFORM_SOURCE", "")
        psrc = os.path.join(workdir, "warm-platform")
        views = contextlib.ExitStack()
        try:
            shutil.rmtree(fetched, ignore_errors=True)
            os.makedirs(fetched)
            out = os.path.join(workdir, "warm-out")
            os.makedirs(out, exist_ok=True)
            started = self.clock()
            extra = {**self.platform_env(), "MW_BUILD_REPO": repo, "NUGET_PACKAGES": fetched}
            extra.pop("MW_PLATFORM_SOURCE", None)
            writable = [fetched]
            if platform_src and os.path.isdir(platform_src):
                extra["MW_PLATFORM_SOURCE"] = views.enter_context(platform_source_view(platform_src, psrc, self.env, self.log))
                writable.append(psrc)
            rc, text = self._sandboxed_capture(plan["command"], tree, out, workdir, extra,
                                               [os.path.join(self.cache, "git", f"{repo}.git"), os.path.join(self.cache, "nuget")],
                                               writable, min(left, plan["timeoutMinutes"] * 60))
            # Every package fetched came from the TIP's own sources by the TIP's own command, so even a failed restore's
            # complete packages may be promoted; each is verified against what NuGet recorded AND against the hash its
            # source feed publishes, and rebuilt from its archive.
            np_, nk, nr = promote_nuget(fetched, self.cache, self.log, self.nuget_feeds, budget=run_end - self.clock())
        except (subprocess.SubprocessError, OSError, Refusal) as e:
            warn(f"the warm NuGet cache was not filled: a host-side failure while warming ({type(e).__name__}: {str(e)[:200]})")
            return
        finally:
            shutil.rmtree(fetched, ignore_errors=True)
            views.close()                                  # the platform view: unmounted or its cold worktree removed + pruned
        secs = int(self.clock() - started)
        if rc != 0 or nr:
            warn(f"the warm NuGet cache was not filled from the default branch's tip {tip[:12]} (restore exit {rc}"
                 + (f", {len(nr)} package(s) failed verification: {nr[:5]}" if nr else "") + f"): {text.strip()[-400:]}")
            return
        try:
            os.makedirs(os.path.dirname(plan["marker"]), exist_ok=True)
            write_marker(os.path.dirname(plan["marker"]), os.path.basename(plan["marker"]), f"{tip} {plan['key']}")
        except OSError as e:
            warn(f"the warm NuGet cache was filled but its key could not be marked ({e})")
            return
        sink.add([{"kind": "line", "text": f"warm NuGet cache: filled from the default branch's tip {tip[:12]} (key {plan['key'][:12]}, "
                                           f"{secs} s): {np_} package(s) promoted, {nk} already present — later runs restore nothing"}])
        self.deliver(sink)

    def _sandboxed_capture(self, command: str, tree: str, out: str, workdir: str, extra: dict, readonly: list,
                           writable: list, timeout: float, cancel: Optional[threading.Event] = None,
                           mount: Optional[str] = None, binds: Iterable[tuple[str, str]] = ()) -> tuple[int, str]:
        """Run a host-chosen, repository-declared command in the sandbox on `tree`. Its output goes to host-owned FILES
        (outside the sandbox's writable binds) and only a bounded tail of each is read back, so a noisy command can never
        exhaust the host's memory and end the worker without a verdict. Returns (exit code, stderr tail + stdout tail).
        `cancel` set (the heartbeat met the 409 — the run is not this host's claim any more) kills the sandbox's whole
        tree at once and raises Lost: a build for a run another host now owns is never waited out (Deploy#49)."""
        argv = self.sandbox.argv(command, tree, out, sandbox_env(self.env, extra, self.cache),
                                 readonly=readonly, writable=writable, mount=mount, binds=binds)
        logs = tempfile.mkdtemp(prefix=".prep-log-", dir=workdir)
        try:
            with open(os.path.join(logs, "out"), "w+b") as so, open(os.path.join(logs, "err"), "w+b") as se:
                try:
                    proc = subprocess.Popen(argv, cwd=tree, env={"PATH": self.env.get("PATH", "/usr/bin:/bin")},
                                            stdout=so, stderr=se, stdin=subprocess.DEVNULL, start_new_session=True)
                except OSError as e:
                    return 127, str(e)
                end = time.monotonic() + max(1.0, timeout)
                while True:
                    try:
                        proc.wait(timeout=max(0.01, min(1.0, end - time.monotonic())))
                        break
                    except subprocess.TimeoutExpired:
                        if cancel is not None and cancel.is_set():
                            _kill(proc)
                            raise Lost("a prepared input's command was killed — the run is not this host's claim any more")
                        if time.monotonic() >= end:
                            _kill(proc)
                            return 124, "timed out"
                # stdout LAST: a key command's key is its final line
                return proc.returncode, file_tail(se, PREPARED_OUTPUT_TAIL) + file_tail(so, PREPARED_OUTPUT_TAIL)
        finally:
            shutil.rmtree(logs, ignore_errors=True)

    def default_branch_tree(self, repo: str, workdir: str) -> tuple[str, str]:
        """The repository's DEFAULT BRANCH tip as a host-made tree: the mirror's HEAD (the default branch, fetched just
        before this run's checkout) cloned `--shared` from the LOCAL mirror and checked out detached — no network, no
        credential, and none of the run's own content. Returns (tip sha, tree). Raises Refusal, naming why."""
        mirror = os.path.join(self.cache, "git", f"{repo}.git")
        tree = os.path.join(workdir, "default-branch")
        genv = {**{k: self.env[k] for k in ("PATH", "LANG") if self.env.get(k)}, "HOME": workdir, "GIT_TERMINAL_PROMPT": "0"}
        rc, out = self.git_runner(["--git-dir", mirror, "rev-parse", "--verify", "HEAD^{commit}"], genv)
        tip = out.strip().splitlines()[-1].strip().lower() if rc == 0 and out.strip() else ""
        if not SHA.match(tip):
            raise Refusal(f"the default branch's tip could not be resolved from the mirror (exit {rc}): {out.strip()[-160:]}")
        shutil.rmtree(tree, ignore_errors=True)
        for args in (["clone", "--quiet", "--shared", "--no-checkout", mirror, tree],
                     ["-C", tree, "checkout", "--quiet", "--detach", tip]):
            rc, out = self.git_runner(args, genv)
            if rc != 0:
                raise Refusal(f"the default branch's tip {tip[:12]} could not be checked out (exit {rc}): {out.strip()[-160:]}")
        return tip, tree

    def prepare_inputs(self, repo: str, checkout: str, workdir: str, trunk: bool, inputs: list[dict],
                       sink: "EventSink", run_end: float) -> tuple[dict, list[str]]:
        """Provide every PREPARED input the repository declares (see PREPARED_ENV): the installed copy for this
        run's source key, or — once per (platform, key) — a build of it, installed for every later run. "Once" is
        the INSTALL: two runs that miss the same key concurrently may each build it; the first rename wins and the
        other's copy is dropped (`install`), so the cost of a race is a duplicate build, never a mixed copy.
        Returns (the stage environment additions, the read-only binds). Raises Refusal, naming the input.

        What is INSTALLED is only ever built from DEFAULT-BRANCH content, with the default branch's own key and build
        commands: a trunk run's tree, or — for any other run whose key is not installed — the default branch's tip,
        checked out by the host from the mirror. A pull request whose sources key the same as that tip (it did not
        change them: its tree is merged onto the tip) therefore installs the tip's build and reads it, so the server is
        prepared for a new platform (or a new main key) by the FIRST run that needs it, trunk run or not. A pull request
        that changed the sources builds them for that run only (MWPREP002): its bytes never reach the shared copy,
        because its own commands never build what is installed — a PR that LIES about its key fools only itself."""
        env_add: dict = {}
        ro: list[str] = []
        mirror = os.path.join(self.cache, "git", f"{repo}.git")
        platform_src = self.env.get("MW_PLATFORM_SOURCE", "")
        # NUGET_FALLBACK_PACKAGES names <cache>/nuget; NuGet refuses a fallback folder that does not exist, and
        # --ro-bind-try would silently skip it — so a fresh host creates it before the first preparation.
        os.makedirs(os.path.join(self.cache, "nuget"), exist_ok=True)

        # The claim's loss, as the heartbeat sees it: a key or build command polls it and is killed on it (Deploy#49).
        lost = self.heartbeat.lost if self.heartbeat is not None else None

        def sandboxed(command: str, tree: str, out: str, extra: dict, readonly: list, writable: list, timeout: float) -> tuple[int, str]:
            if lost is not None and lost.is_set():
                raise Lost("the run is not this host's claim any more — preparing nothing more for it")
            return self._sandboxed_capture(command, tree, out, workdir, extra, readonly, writable, timeout, cancel=lost)

        base_extra = {"MW_BUILD_PLATFORM_VERSION": self.platform_version, "MW_BUILD_REPO": repo}

        def source_key(p: dict, tree: str, label: str) -> str:
            left = run_end - self.clock()
            if left <= 0:
                raise Refusal(f"{p['name']}: the run deadline passed before it was prepared")
            keydir = os.path.join(workdir, "prepared-key", label, p["name"])
            os.makedirs(keydir, exist_ok=True)
            rc, text = sandboxed(p["key"], tree, keydir, base_extra, [mirror], [], min(left, float(PREPARED_KEY_SECONDS)))
            key = (text.strip().splitlines() or [""])[-1].strip() if rc == 0 else ""
            if rc != 0 or not SOURCE_KEY.match(key):
                whose = "the default branch's" if label == "default" else "its"
                raise Refusal(f"{p['name']}: {whose} key command did not answer a source key (exit {rc}): {text.strip()[-300:]}")
            return key

        def build(p: dict, tree: str, label: str, key: str) -> tuple[str, int]:
            name = p["name"]
            if not platform_src or not os.path.isdir(platform_src):
                raise Refusal(f"{name} (key {key[:12]}) is not installed and this host has no platform source to build it "
                              f"with (MW_PLATFORM_SOURCE is {'unset' if not platform_src else 'not a directory: ' + platform_src})")
            out = os.path.join(workdir, "prepared", label, name)
            shutil.rmtree(out, ignore_errors=True)
            os.makedirs(out)
            # Everything a build may WRITE is per LABEL: a run-content ("run") build of one input must never leave
            # bytes a later default-branch ("default") build of another input reads — a shared package folder would
            # let a pull request's command plant packages into what the default branch's build installs for every run.
            nuget = os.path.join(workdir, "prepared-nuget", label)
            os.makedirs(nuget, exist_ok=True)
            # The build gets its OWN writable VIEW of the platform source (`platform_source_view`: an overlay of the
            # image's read-only tree, or a cold worktree of the platform's master — never a copy): a `dotnet publish` that
            # references platform projects writes their obj/ and bin/ into that tree, and the image's tree is read-only.
            left = run_end - self.clock()
            if left <= 0:
                raise Refusal(f"{name}: the run deadline passed before it was built")
            started = self.clock()
            with platform_source_view(platform_src, os.path.join(workdir, "prepared-platform", label, name),
                                      self.env, self.log) as psrc:
                rc, text = sandboxed(p["build"], tree, out, {**base_extra, "MW_PREPARE_OUT": out, "MW_PLATFORM_SOURCE": psrc,
                                                             "NUGET_PACKAGES": nuget},
                                     [mirror, os.path.join(self.cache, "nuget")], [nuget, psrc],
                                     min(left, p["timeoutMinutes"] * 60))
            # The build controls `out` (it may leave it unreadable): every host-side I/O on it is a Refusal NAMING
            # the input, never an exception that leaves the run without a verdict.
            try:
                wrote = [n for n in os.listdir(out) if not n.startswith(".")]
            except OSError as e:
                raise Refusal(f"{name}: its output cannot be read ({e})")
            if rc != 0 or not wrote:
                where = "from the default branch " if label == "default" else ""
                raise Refusal(f"{name}: building it {where}failed (exit {rc}{', it wrote nothing' if rc == 0 else ''}): {text.strip()[-600:]}")
            # Host-side marker writes never follow what the build left: the build controls `out`, so a
            # `.source-key` (or, in the copy, a `.complete`) it planted as a symlink would redirect the write.
            try:
                write_marker(out, ".source-key", key)
            except OSError as e:
                raise Refusal(f"{name}: its output cannot be marked ({e})")
            return out, int(self.clock() - started)

        def install(name: str, out: str, store: str, key: str) -> None:
            """Atomic and keyed: the copy is completed beside the store and renamed into place; when a concurrent
            installer of the same (platform, key) won the rename, this copy is dropped and theirs is read."""
            os.makedirs(os.path.dirname(store), exist_ok=True)
            tmp = tempfile.mkdtemp(prefix=".prep-", dir=os.path.dirname(store))
            try:
                shutil.copytree(out, os.path.join(tmp, "p"), symlinks=True)
                write_marker(os.path.join(tmp, "p"), ".complete", key)
                os.rename(os.path.join(tmp, "p"), store)
            except (OSError, shutil.Error):
                if not os.path.isfile(os.path.join(store, ".complete")):
                    raise Refusal(f"{name}: built, but could not be installed at {store}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        base: Optional[tuple[str, str, dict]] = None       # (tip, tree, the tip's declared inputs by name), made once
        for p in inputs:
            name = p["name"]
            key = source_key(p, checkout, "run")
            store = prepared_store(self.cache, repo, name, self.platform_version, key)
            if os.path.isfile(os.path.join(store, ".complete")):
                env_add[p["env"]] = store
                ro.append(store)
                sink.add([{"kind": "line", "text": f"prepared {name}: installed on this server (key {key[:12]}) — nothing prepared for this run"}])
                continue
            if trunk:
                out, secs = build(p, checkout, "run", key)
                install(name, out, store, key)
                env_add[p["env"]] = store
                ro.append(store)
                sink.add([{"kind": "line", "text": f"prepared {name}: main moved its sources — built once ({secs} s) and "
                                                   f"installed on this server (key {key[:12]}); later runs prepare nothing"}])
                continue
            # Not a trunk run and not installed: the server is prepared from the DEFAULT BRANCH's tip — its tree, its
            # key command, its build command — never from this run's content.
            if base is None:
                tip, tree = self.default_branch_tree(repo, workdir)
                try:
                    declared_there = {q["name"]: q for q in load_prepared(os.path.join(tree, ".github", "build-stages.json"))}
                except Refusal as r:
                    sink.add([{"kind": "line", "text": f"prepared inputs: the default branch's tip {tip[:12]} declares none usable ({r})"[:MAX_LINE]}])
                    declared_there = {}
                base = (tip, tree, declared_there)
            tip, tree, declared_there = base
            bp = declared_there.get(name)
            bkey = source_key(bp, tree, "default") if bp else None
            if bkey == key:
                out, secs = build(bp, tree, "default", bkey)
                install(name, out, store, bkey)
                env_add[p["env"]] = store
                ro.append(store)
                sink.add([{"kind": "line", "text": f"prepared {name}: not installed for platform {self.platform_version} — built once "
                                                   f"({secs} s) from the default branch's tip {tip[:12]} (key {key[:12]}) and installed "
                                                   "on this server; later runs prepare nothing"}])
                continue
            out, secs = build(p, checkout, "run", key)
            env_add[p["env"]] = out
            ro.append(out)
            theirs = f"key {bkey[:12]}" if bkey else "which does not declare it"
            sink.add([{"kind": "diagnostic", "severity": "warning", "code": "MWPREP002",
                       "message": f"prepared input {name} built PER RUN ({secs} s): this change's sources (key {key[:12]}) "
                                  f"differ from the default branch's tip {tip[:12]} ({theirs}); never installed"}])
        self.deliver(sink)
        return env_add, ro

    def stage_readonly(self, repo: str, st: dict) -> list[str]:
        """What of the SHARED cache this stage may read, read-only: its own repository's git mirror
        (the checkout's alternates point there) and what the stage DECLARES (`needs`) — never the
        cache root, never another repository's mirror or outputs."""
        ro = [os.path.join(self.cache, "git", f"{repo}.git")]
        needs = st.get("needs") or []
        if "plugins" in needs and self.plugins_dir:
            ro.append(self.plugins_dir)
            for name in sorted(os.listdir(self.plugins_dir)) if os.path.isdir(self.plugins_dir) else []:
                link = os.path.join(self.plugins_dir, name)
                if os.path.islink(link):
                    ro.append(os.path.realpath(link))     # a plugin set's links point into /cache/bundles
        if "outputs" in needs:
            ro.append(outputs_root(self.cache, repo))
        # The warm NuGet cache, for EVERY stage: sandbox_env hands each one NUGET_FALLBACK_PACKAGES=<cache>/nuget, and
        # the build stage's project.assets.json records it as a package folder, so a test stage that restores nothing
        # still resolves its assets through it. Binding it only for `needs: ["nuget"]` left the folder named but
        # absent in every other stage — MSB4018 ResolvePackageAssets "Unable to find fallback package folder".
        ro.append(os.path.join(self.cache, "nuget"))
        return ro

    def run_stage(self, st: dict, checkout: str, outputs: str, sink: EventSink, extra: dict,
                  readonly: Iterable[str] = (), writable: Iterable[str] = (), binds: Iterable[tuple[str, str]] = (),
                  hand_over: Callable[[], tuple[list[dict], Optional[str]]] = None,
                  cancel: Optional[threading.Event] = None,
                  hand_over_failed: Callable[[], tuple[list[dict], Optional[str]]] = None) -> bool:
        """Run ONE stage in a fresh sandboxed process tree; post its events; return success. `cancel` (fan-out): set
        when a sibling stage found the run is no longer this host's — the stage is killed and Lost raised.
        `hand_over_failed`: what a RED stage still hands over — everything it added or rewrote (red_stage_artifacts),
        every receipt marked failed; its receipts are posted before the failed verdict, and a problem with them is named
        but never turns red green."""
        name, started = st["name"], self.clock()

        def abandoned() -> None:
            if cancel is not None and cancel.is_set():
                raise Lost(f"stage {name}: abandoned — a sibling stage found the run is not this host's claim any more")

        # Never START a sandbox for a run already abandoned (a sibling may have met the 409 since this stage was picked).
        abandoned()
        # the stage sees its checkout at SANDBOX_CHECKOUT, so that is the prefix its diagnostics carry
        parser = OutputParser(checkout, name, inside=SANDBOX_CHECKOUT)
        sink.add([{"kind": "line", "text": f"[00:00:00] {st['kind']}/{name} on build-{self.lane} "
                                           f"({self.platform_version}, plugins {(self.plugins_digest or '-')[:19]})"}])
        try:
            # OFFLINE (MW_BUILD_STAGE_NETWORK=offline): no network namespace beyond loopback, and NuGet's ONLY source is the
            # prepared package folder — a package the preparation did not fetch is NU1101 naming it, never a download
            stage_extra = {**extra, **offline_restore_env(self.cache)} if self.stage_offline else extra
            argv = self.sandbox.argv(st["command"], checkout, outputs, sandbox_env(self.env, stage_extra, self.cache),
                                     readonly=readonly, writable=writable, mount=SANDBOX_CHECKOUT, binds=binds,
                                     **({"network": False} if self.stage_offline else {}))
            proc = subprocess.Popen(argv, cwd=checkout, env={"PATH": self.env.get("PATH", "/usr/bin:/bin")},
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, text=True, errors="replace", bufsize=1)
        except (OSError, Refusal) as e:
            sink.add([{"kind": "stage-finished", "state": "failed", "summary": f"could not start: {e}"}])
            sink.flush(attempts=6)
            return False
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def reader() -> None:
            assert proc.stdout is not None
            for ln in proc.stdout:
                lines.put(ln)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        deadline = started + st["timeoutMinutes"] * 60
        timed_out = False

        try:
            while True:
                abandoned()
                try:
                    ln = lines.get(timeout=1.0)
                except queue.Empty:
                    if self.clock() > deadline and not timed_out:
                        timed_out = True
                        _kill(proc)
                    sink.add([])
                    continue
                if ln is None:
                    break
                sink.add(parser.feed(ln))
                if self.clock() > deadline and not timed_out:
                    timed_out = True
                    _kill(proc)
            # EOF is NOT exit: a stage can close its stdout and keep running. The deadline holds
            # until the process tree is gone, and the events keep flowing meanwhile.
            while True:
                try:
                    rc = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    abandoned()
                    sink.add([])
                    if self.clock() > deadline and not timed_out:
                        timed_out = True
                        _kill(proc)
        except Lost:
            _kill(proc)
            raise
        except Exception as e:
            # Nothing a stage does may take the host down without a verdict: kill, reap, report.
            _kill(proc)
            sink.add([{"kind": "stage-finished", "state": "failed",
                       "summary": f"the host failed while running the stage ({type(e).__name__}: {str(e)[:200]})"}])
            self.deliver(sink)
            return False
        events = parser.finish()
        ok = rc == 0 and not timed_out
        if not ok and parser.errors == 0 and parser.tests["failed"] == 0:
            # nothing structured explains the failure — the tail is the evidence
            events += [{"kind": "line", "text": "│ " + t} for t in parser.tail if t.strip()]
        note = f"timed out after {st['timeoutMinutes']:g} min" if timed_out else ""
        # An abandoned run hands nothing over and posts no verdict: the hand-over itself stops at its next step too.
        abandoned()
        if ok and hand_over is not None:
            # H9: every artifact the stage produced is in the shared CI store, its stored hash re-read,
            # BEFORE the stage can succeed — a receipt the instance holds is a byte the store holds.
            receipts, problem = hand_over()
            events += receipts
            if problem:
                ok = False
                note = (note + "; " if note else "") + f"artifact hand-over failed: {problem}"
        elif not ok and hand_over_failed is not None:
            # The evidence of WHY it is red outlives the sandbox (MeshWeaver.Deploy#52), every receipt marked failed.
            receipts, problem = hand_over_failed()
            events += receipts
            if receipts:
                events.append({"kind": "line", "text": f"red stage: {len(receipts)} file(s) handed over, marked failed"})
            if problem:
                note = (note + "; " if note else "") + f"red-stage hand-over failed: {problem}"
        abandoned()
        events.append({"kind": "stage-finished", "state": "succeeded" if ok else "failed",
                       "summary": parser.summary(rc, self.clock() - started, note)})
        sink.add(events)
        self.deliver(sink)
        return ok

    def effective_plugins_key(self) -> str:
        """The plugins layer's identity in a cache key, in EITHER mode: the registry index's key
        (H8), else the sealed publication's digest — so a changed plugins layer is always a new key."""
        return self.plugins_key or self.plugins_digest or ""

    def cache_key_problem(self, output_dir: str, checkout: str) -> Optional[str]:
        """None when the output's directory name IS the key the host recomputes from the inputs its
        `.cache-key.json` names; otherwise the reason it is not promoted."""
        try:
            with open(os.path.join(output_dir, CACHE_KEY_FILE)) as fh:
                spec = json.load(fh)
        except (OSError, ValueError) as e:
            return f"no readable {CACHE_KEY_FILE} ({type(e).__name__})"
        env = dict(self.env, MW_BUILD_FRAMEWORK_IDENTITY=self.identity, MW_PLUGINS_KEY=self.effective_plugins_key())
        try:
            key = cache_key_of(checkout, spec, env)
        except Refusal as r:
            return str(r)
        return None if key == os.path.basename(output_dir.rstrip("/")) else f"the recomputed key is {key[:16]}…, not the directory's name"

    def hand_over_artifacts(self, repo: str, sha: str, stage: str, artifacts_dir: str,
                            deadline: Optional[float] = None,
                            cancel: Optional[threading.Event] = None,
                            stage_state: str = STAGE_SUCCEEDED) -> tuple[list[dict], Optional[str]]:
        """H9: upload every file a stage left under `$MW_BUILD_ARTIFACTS/<unit>/` (a module bundle,
        its publication record, the gate bake) to the shared CI artifact store with core's
        `ci-artifact-store.py` (content-addressed, SHA-256), READ IT BACK and re-hash it, and only
        then return its receipt `{"kind":"artifact","unit","key","sha256","bytes","stageState"}` — `stage_state` the
        outcome of the stage handing it over (STAGE_SUCCEEDED, or STAGE_FAILED for a red stage's evidence). Returns
        (receipts, the first problem or None); a stage with artifacts and no store is a problem.
        The hand-over is INSIDE the run budget: every store call gets the time left until `deadline`
        (the run's absolute end, on self.clock) and no more, so a stage finishing near the limit
        cannot hold its claim past the follower's window or the pod's grace; when the budget runs
        out the hand-over is a problem and the stage fails. `cancel` (fan-out) set — a sibling met the 409 — raises Lost
        before the next store call and kills the one in flight: an abandoned run uploads and receipts nothing more."""
        files = []
        for root, _dirs, names in os.walk(artifacts_dir):
            for n in sorted(names):
                full = os.path.join(root, n)
                if os.path.islink(full) or not os.path.isfile(full):
                    return [], f"{os.path.relpath(full, artifacts_dir)} is not a regular file"
                files.append(full)
        if not files:
            return [], None
        store_url = self.env.get("MW_BUILD_ARTIFACT_STORE", "").strip()
        if not store_url:
            return [], "the stage produced artifacts but no artifact store is configured (MW_BUILD_ARTIFACT_STORE)"

        def left() -> float:
            return float("inf") if deadline is None else deadline - self.clock()

        if store_url == API_STORE_SPEC:
            return self.hand_over_through_api(repo, sha, stage, artifacts_dir, files, left, cancel, stage_state)
        if store_url.startswith(API_STORE_SPEC):
            return [], f"MW_BUILD_ARTIFACT_STORE {store_url!r} is not a store: the API store is exactly {API_STORE_SPEC!r}"
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci-artifact-store.py")
        receipts = []

        def store(argv: list[str]) -> tuple[int, str]:
            abandoned(cancel, stage)
            budget = left()
            if budget <= 0:
                raise subprocess.TimeoutExpired(argv, 0)
            return self.store_runner(argv, timeout=min(1800.0, budget), cancel=cancel)

        for full in sorted(files):
            unit = artifact_unit(os.path.relpath(full, artifacts_dir).replace(os.sep, "/"))
            if "/" not in unit:
                return receipts, f"{unit} is not under a unit directory ($MW_BUILD_ARTIFACTS/<unit>/<file>)"
            digest, size = _sha256_file(full), os.path.getsize(full)
            # The DIGEST is in the key: a later attempt, probe or platform-set build of the same
            # commit/channel/stage that produces OTHER bytes writes a different object, so an earlier
            # acknowledged receipt's object is never replaced under it.
            key = f"build-hosts/{repo}/{sha}/{self.lane}/{stage}/{digest}/{unit}"
            try:
                rc, out = store([sys.executable, script, "put", "--store", store_url, "--key", key, "--file", full])
            except subprocess.TimeoutExpired:
                return receipts, f"the run deadline passed during the hand-over of {unit} (put)"
            locator = next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith("locator=")), "")
            if rc != 0 or not locator:
                return receipts, f"ci-artifact-store put {unit} failed (exit {rc}): {out.strip()[-200:]}"
            if not locator.endswith(f"#sha256={digest}"):
                return receipts, f"the store's locator for {unit} names another hash ({locator[-80:]})"
            readback = tempfile.mkdtemp(prefix=".readback-", dir=self.work)
            try:
                back = os.path.join(readback, "f")
                try:
                    rc, out = store([sys.executable, script, "get", "--store", store_url, "--locator", locator, "--out", back])
                except subprocess.TimeoutExpired:
                    return receipts, f"the run deadline passed during the hand-over of {unit} (read-back)"
                if rc != 0 or not os.path.isfile(back) or _sha256_file(back) != digest:
                    return receipts, f"the stored {unit} did not read back as sha256 {digest[:12]}… (exit {rc}): {out.strip()[-160:]}"
            finally:
                shutil.rmtree(readback, ignore_errors=True)
            receipts.append({"kind": "artifact", "unit": unit, "key": locator, "sha256": digest, "bytes": size,
                             "stageState": stage_state})
        return receipts, None

    def hand_over_failed_stage(self, repo: str, sha: str, stage: str, artifacts_dir: str, before: dict,
                               deadline: Optional[float] = None,
                               cancel: Optional[threading.Event] = None) -> tuple[list[dict], Optional[str]]:
        """A RED stage's artifacts — everything it added or rewrote — through the SAME hand-over a green stage uses (store,
        read-back, receipt), every receipt marked STAGE_FAILED, from a private copy so the quarantine still moves the
        originals and no later sibling ever receipts them under its own name."""
        def proceed() -> None:
            abandoned(cancel, stage)    # Lost: an abandoned run stages, uploads and receipts nothing more
            if deadline is not None and deadline - self.clock() <= 0:
                raise _DeadlinePassed()

        staged = None
        try:
            proceed()
            # Inside the try: on a full work volume this very mkdtemp raises ENOSPC, and that must become this note,
            # never an exception out of run_stage that leaves the stage without a verdict.
            staged = tempfile.mkdtemp(prefix=".red-stage-", dir=self.work)
            if not red_stage_artifacts(artifacts_dir, before, staged, proceed):
                return [], None
            return self.hand_over_artifacts(repo, sha, stage, staged, deadline=deadline, cancel=cancel,
                                            stage_state=STAGE_FAILED)
        except _DeadlinePassed:
            return [], "the run deadline passed while the red stage's artifacts were staged"
        except OSError as e:
            return [], f"the red stage's artifacts could not be staged ({type(e).__name__}: {str(e)[:160]})"
        finally:
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)

    def hand_over_through_api(self, repo: str, sha: str, stage: str, artifacts_dir: str, files: list[str],
                              left: Callable[[], float],
                              cancel: Optional[threading.Event] = None,
                              stage_state: str = STAGE_SUCCEEDED) -> tuple[list[dict], Optional[str]]:
        """The API store: PUT each artifact to `{api}/{repo}/{sha}/artifacts/{sha256}` as the run's holder (host + claim id),
        and take the receipt FROM THE SERVER'S ANSWER — the instance hashed what it stored, so a receipt is a byte it holds.
        The answer must name this unit, this sha256, this size and a locator attesting the digest, or the stage fails. A
        409 means the run is no longer this host's (Lost). Retries only a transport failure or a 5xx, bounded by
        API_UPLOAD_ATTEMPTS and the run budget; every other answer fails the stage at once, naming it."""
        receipts = []
        for full in sorted(files):
            unit = artifact_unit(os.path.relpath(full, artifacts_dir).replace(os.sep, "/"))
            if "/" not in unit:
                return receipts, f"{unit} is not under a unit directory ($MW_BUILD_ARTIFACTS/<unit>/<file>)"
            digest, size = _sha256_file(full), os.path.getsize(full)
            query = {**self.run_scope, "stage": stage, "unit": unit, "host": self.host, "claimId": self.claim_id,
                     "stageState": stage_state, **({"probe": self.probe} if self.probe else {})}
            path = artifact_path(repo, sha, digest)
            status, answer, pause = 0, None, 2.0
            for attempt in range(1, API_UPLOAD_ATTEMPTS + 1):
                abandoned(cancel, stage)
                budget = left()
                if budget <= 0:
                    return receipts, f"the run deadline passed during the hand-over of {unit} (upload)"
                status, answer = self.api.upload(path, query, full, size, timeout=min(1800.0, budget), cancel=cancel)
                if 200 <= status < 300 or status == 409 or not (status == 0 or status >= 500):
                    break
                if attempt < API_UPLOAD_ATTEMPTS:
                    self.log(f"upload of {unit} → {status or 'no answer'} ({_answer_text(answer)}); "
                             f"retrying ({attempt}/{API_UPLOAD_ATTEMPTS})")
                    self.sleep(max(0.0, min(pause, left())))
                    pause = min(pause * 2, 60.0)
            if status == 409:
                raise Lost(f"{path}: the run is not this host's claim any more (409 on the artifact upload: {_answer_text(answer)})")
            if not (200 <= status < 300):
                tries = f" after {API_UPLOAD_ATTEMPTS} attempts" if status == 0 or status >= 500 else ""
                return receipts, f"the build API did not store {unit} ({status or 'no answer'}{tries}): {_answer_text(answer)}"
            problem = api_receipt_problem(answer, unit, digest, size)
            if problem:
                return receipts, f"the build API's answer for {unit} {problem}"
            receipts.append({"kind": "artifact", "unit": unit, "key": answer["key"], "sha256": digest, "bytes": size,
                             "stageState": stage_state})
        return receipts, None

    # -- terminal events are never lost -----------------------------------------------------
    def deliver(self, sink: EventSink) -> bool:
        """Flush a sink; one that could not be emptied (the API is down) is HELD, with its
        stage-finished, and retried before every later post — nothing is discarded with it."""
        if sink.flush(attempts=6):
            return True
        with self.held_lock:
            if sink not in self.held:
                self.held.append(sink)
        return False

    def drain_held(self, attempts: int = 6) -> bool:
        with self.held_lock:
            still = []
            for sink in self.held:
                if not sink.flush(attempts=attempts):
                    still.append(sink)
            self.held = still
            return not still

    def loop(self) -> int:
        idle = float(self.env.get("MW_BUILD_IDLE_SECONDS", "15") or 15)
        while not self.stopping:
            what = self.cycle()
            if what in ("idle", "no-inputs", "drained"):
                self.sleep(idle if what == "idle" else max(idle, 60 if what == "drained" else 30))
        self.log("stopping: no further claims (the run in hand, if any, has finished)")
        return 0


class _DeadlinePassed(Exception):
    """The run's deadline passed while a red stage's artifacts were being staged."""


def abandoned(cancel: Optional[threading.Event], stage: str) -> None:
    """Raise Lost when the run was abandoned (`cancel` set: a sibling stage met the 409)."""
    if cancel is not None and cancel.is_set():
        raise Lost(f"stage {stage}: abandoned during the artifact hand-over — the run is not this host's claim any more")


# A throwaway self-signed key and certificate (CN=localhost) for the self-test's local HTTPS server ONLY — it protects
# nothing and is trusted by nothing; the test's client skips verification.
_SELFTEST_TLS_PEM = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg7OlL4C3Ub9FiEweL
ynk1oc5efgNlmyblhh+7MjIoTTmhRANCAASHr3IYuC94xquFX+ix2VsuPH+rfhVU
Aehp5XrBb4TXo2FBAumRcQ6ui5yR2qGJ29i9KA3Z88eS1MRShDYujZIM
-----END PRIVATE KEY-----
-----BEGIN CERTIFICATE-----
MIIBfzCCASWgAwIBAgIUK0qzuB9UXvL4tWBQVet/dLTB/rkwCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJbG9jYWxob3N0MCAXDTI2MTAyOTA3MDI0N1oYDzIxMjYxMDA1
MDcwMjQ3WjAUMRIwEAYDVQQDDAlsb2NhbGhvc3QwWTATBgcqhkjOPQIBBggqhkjO
PQMBBwNCAASHr3IYuC94xquFX+ix2VsuPH+rfhVUAehp5XrBb4TXo2FBAumRcQ6u
i5yR2qGJ29i9KA3Z88eS1MRShDYujZIMo1MwUTAdBgNVHQ4EFgQUCbhWWbd/1DPK
s6XJ6XeB2DYiPAQwHwYDVR0jBBgwFoAUCbhWWbd/1DPKs6XJ6XeB2DYiPAQwDwYD
VR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNIADBFAiABRYhUwwZ3OfgdgUStxR1C
KnKFMEemuUbbTwqk4CAxdQIhAOxOF6W3fGwFURNaCzgZxdXvmt3WwFb5EEhTLvT5
QZUn
-----END CERTIFICATE-----
"""


def _answer_text(answer: object) -> str:
    """A refusal's reason as the build API states it (`{"error": …}`), else the raw answer — bounded."""
    if isinstance(answer, dict) and isinstance(answer.get("error"), str):
        return answer["error"][:200]
    return str(answer)[:200]


def api_receipt_problem(answer: object, unit: str, digest: str, size: int) -> Optional[str]:
    """None when the build API's answer is the receipt of exactly this artifact — this unit, this sha256, this size, and a
    locator under the API store attesting the digest; otherwise what is wrong with it."""
    if not isinstance(answer, dict):
        return "is not a receipt"
    if answer.get("unit") != unit:
        return f"names another unit ({str(answer.get('unit'))[:80]})"
    if answer.get("sha256") != digest:
        return f"names another hash ({str(answer.get('sha256'))[:80]})"
    if answer.get("bytes") != size:
        return f"names another size ({answer.get('bytes')} bytes, not {size})"
    key = answer.get("key")
    if not (isinstance(key, str) and key.startswith(API_STORE_SPEC + "/") and key.endswith(f"#sha256={digest}") and len(key) <= 512):
        return f"carries no locator attesting sha256 {digest[:12]}… ({str(key)[:80]})"
    return None


def _kill(proc: subprocess.Popen) -> None:
    """End the stage's whole tree: bwrap is the session leader, and --die-with-parent plus the PID
    namespace take everything under it down with it."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    for _ in range(100):
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--host", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--self-test", action="store_true")
    g.add_argument("--cache-key", metavar="SPEC.json",
                   help="print the cross-run cache key of the output SPEC describes (a stage's "
                        ".cache-key.json), computed exactly as the host recomputes it; cwd = the checkout")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.cache_key:
        try:
            with (sys.stdin if a.cache_key == "-" else open(a.cache_key)) as fh:
                print(cache_key_of(os.getcwd(), json.load(fh), dict(os.environ)))
            return 0
        except (Refusal, OSError, ValueError) as e:
            print(f"build-run: {e}", file=sys.stderr)
            return 1
    env = dict(os.environ)
    if a.host or a.once:
        enter_run_namespace(argv, env, lambda m: print(f"build-run: {m}", flush=True))
    try:
        host = Host(env, Api(env.get("BUILD_API", ""), read_worker_token(env)))
        host.preflight()
        host.sandbox.probe(_run_capture)
        boundary_check(host.sandbox, env, _run_capture)
        host.stage_refset()
        scoping_check(host, _run_capture)
    except (Refusal, Fatal) as r:
        print(f"build-run: {r}", file=sys.stderr, flush=True)
        return 1

    def stop(_sig, _frm) -> None:
        # The old replica of a roll (or a scale-in): stop claiming, finish the run in hand, exit — and stop a warm build.
        host.stopping = True
        host.warm_cancel.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if a.once:
            print(f"build-run: {host.cycle()}", flush=True)
            return 0
        return host.loop()
    except Fatal as f:
        print(f"build-run: {f}", file=sys.stderr, flush=True)
        return 2


# ───────────────────────────────── self-test ──────────────────────────────────────────────

class _FakeApi(Api):
    def __init__(self, script: dict) -> None:
        super().__init__("https://control.example.com/api/builds", "worker")
        self.script = script
        self.calls: list[tuple] = []

    def call(self, method, path, body=None, query=None):
        self.calls.append((method, path, json.loads(json.dumps(body)) if body is not None else None,
                           dict(query or {})))
        for (m, p), answers in self.script.items():
            if m == method and path.startswith(p):
                if callable(answers):
                    return answers(path, body)
                return answers.pop(0) if len(answers) > 1 else answers[0]
        return 404, None

    def download(self, path, dest, timeout):
        self.calls.append(("GET-BYTES", path, None, {}))
        for (m, p), answers in self.script.items():
            if m == "GET-BYTES" and path.startswith(p):
                status, data = answers(path) if callable(answers) else answers[0]
                if status == 200:
                    with open(dest, "wb") as fh:
                        fh.write(data)
                return status, "" if status == 200 else "refused"
        return 404, "not found"

    def upload(self, path, query, file_path, size, timeout, cancel=None):
        abandoned(cancel, "fake-upload")
        with open(file_path, "rb") as fh:
            data = fh.read()
        self.calls.append(("PUT", path, None, dict(query or {})))
        for (m, p), answers in self.script.items():
            if m == "PUT" and path.startswith(p):
                if callable(answers):
                    return answers(path, dict(query or {}), data, size, timeout)
                return answers.pop(0) if len(answers) > 1 else answers[0]
        return 404, None


class _EnvSandbox:
    """The self-test's stand-in for bwrap on a machine without it: `env -i` gives the same
    clear-env + allow-list semantics, so what a stage SEES is still what the host chose."""

    last_readonly: list = []
    last_mount: Optional[str] = None
    last_binds: list = []

    def argv(self, command, checkout, outputs, env, readonly=(), writable=(), mount=None, binds=()):
        _EnvSandbox.last_readonly = list(readonly)
        _EnvSandbox.last_binds = list(binds)
        # no mount namespace here: a bound in-sandbox path in the env is the host path it is bound from
        for host_path, in_path in binds:
            env = {k: (host_path + v[len(in_path):] if v == in_path or v.startswith(in_path + "/") else v) for k, v in env.items()}
        _EnvSandbox.last_mount = mount
        return ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in sorted(env.items())], "/bin/bash", "-c", command]


def self_test() -> int:
    failures: list[str] = []
    # the self-test's hosts run in temporary directories on whatever disk the test machine has: the disk guard reads no
    # filesystem here (total 0 = unknown, never blocks); its own cases inject the readings they test
    globals()["_disk_free"] = lambda path: (0, 0)

    def check(name: str, cond: bool, detail: object = "") -> None:
        if not cond:
            failures.append(f"{name}: {detail}")

    # ── 0. the claim's labels → MW_BUILD_LABELS (None = unset: the stage says it was not told) ─────────
    check("labels/sorted, de-duplicated, trimmed", claim_labels({"labels": ["tests-before-review", " full-run", "full-run"]}) == "full-run,tests-before-review")
    check("labels/an empty list is known-none ('')", claim_labels({"labels": []}) == "")
    union = [f"l{i}" for i in range(160)] + ["full-run"]
    check("labels/an accumulated union beyond one PR's 100 is handed whole, full-run included",
          (claim_labels({"labels": union}) or "").split(",").count("full-run") == 1 and len(claim_labels({"labels": union}).split(",")) == 161)
    check("labels/counted in code points, as GitHub and the instance count them", claim_labels({"labels": ["\U0001F680" * 26]}) == "\U0001F680" * 26)
    for bad in ({}, {"labels": None}, {"labels": "full-run"}, {"labels": [1]}, {"labels": ["a,b"]}, {"labels": [""]},
                {"labels": ["x" * 51]}, {"labels": ["a\nb"]}, {"labels": ["full-\x7frun"]}, {"labels": ["full-run\x85"]}):
        check(f"labels/{str(bad)[:30]} is unknown (None), never a partial set", claim_labels(bad) is None, claim_labels(bad))

    # ── 1. compiler diagnostics ─────────────────────────────────────────────────────────────
    p = OutputParser("/work/run1/src", "compile-check")
    pin = OutputParser("/work/run1/src", "compile-check", inside=SANDBOX_CHECKOUT)
    check("parse/a diagnostic under the in-sandbox checkout names the repository file",
          pin.rel(SANDBOX_CHECKOUT + "/Hosting/Build/Source/BuildApi.cs") == "Hosting/Build/Source/BuildApi.cs"
          and pin.rel("/work/run1/src/Hosting/x.cs") == "Hosting/x.cs")
    ev = p.feed("/work/run1/src/Hosting/Build/Source/BuildApi.cs(41,13): error CS0246: The type or "
                "namespace name 'X' could not be found [/work/run1/src/Hosting/Hosting.csproj]\n")
    check("msbuild/error", ev == [{"kind": "diagnostic", "severity": "error", "file": "Hosting/Build/Source/BuildApi.cs",
                                   "line": 41, "code": "CS0246",
                                   "message": "The type or namespace name 'X' could not be found"}], ev)
    ev = p.feed("src/A.cs(7): warning CS8602: Dereference of a possibly null reference.")
    check("msbuild/warning no column", ev and ev[0]["severity"] == "warning" and ev[0]["line"] == 7
          and ev[0]["file"] == "src/A.cs", ev)
    again = p.feed("/work/run1/src/Hosting/Build/Source/BuildApi.cs(41,13): error CS0246: The type or "
                   "namespace name 'X' could not be found [/work/run1/src/Hosting/Hosting.csproj]")
    check("msbuild/dedup of the closing summary", again == [] and p.errors == 1, again)
    ev = p.feed("::error file=Store/Plugin/Source/Gate.cs,line=12::CS1061 'Foo' does not contain 'Bar'")
    check("wfcmd/diagnostic", ev and ev[0]["code"] == "CS1061" and ev[0]["line"] == 12
          and ev[0]["file"] == "Store/Plugin/Source/Gate.cs" and ev[0]["message"].startswith("'Foo'"), ev)
    check("chatter/dropped", p.feed("  Restored /work/run1/src/x.csproj (in 1.2 s).") == [])
    check("chatter/kept in tail", any("Restored" in t for t in p.tail), p.tail)
    check("summary/counts", "2 error(s)" in p.summary(1, 75) and "1 warning(s)" in p.summary(1, 75)
          and "1m15s" in p.summary(1, 75), p.summary(1, 75))

    # ── 2. test results ─────────────────────────────────────────────────────────────────────
    t = OutputParser("/work/run2/src", "portal-hosts")
    out: list[dict] = []
    for ln in [
        "Test run for /work/run2/src/bin/MeshWeaver.Hosting.Test.dll (.NETCoreApp,Version=v10.0)",
        "  Passed MeshWeaver.Hosting.Test.QueueTest.Claims [12 ms]",
        "  Failed MeshWeaver.Hosting.Test.QueueTest.Orders [1 s]",
        "  Error Message:",
        "   Assert.Equal() Failure: Values differ",
        "  Stack Trace:",
        "     at Xunit.Assert.Equal(Int32 expected, Int32 actual) in /_/src/Assert.cs:line 9",
        "     at MeshWeaver.Hosting.Test.QueueTest.Orders() in /work/run2/src/test/QueueTest.cs:line 42",
        "  Skipped MeshWeaver.Hosting.Test.QueueTest.Later [< 1 ms]",
        "Failed!  - Failed:     1, Passed:     1, Skipped:     1, Total:     3",
    ]:
        out += t.feed(ln)
    out += t.finish()
    tests = [e for e in out if e["kind"] == "test-finished"]
    check("vstest/three results", [e["outcome"] for e in tests] == ["passed", "failed", "skipped"], tests)
    f = next((e for e in tests if e["outcome"] == "failed"), {})
    check("vstest/suite from the dll", f.get("suite") == "MeshWeaver.Hosting.Test", f)
    check("vstest/failure message", f.get("message") == "Assert.Equal() Failure: Values differ", f)
    check("vstest/failure source line is the repo's, not the framework's",
          f.get("file") == "test/QueueTest.cs" and f.get("line") == 42, f)
    check("vstest/duration", tests[0]["durationMs"] == 12 and f.get("durationMs") == 1000, tests)
    check("vstest/summary not a result", t.tests == {"passed": 1, "failed": 1, "skipped": 1}, t.tests)
    # Two LANES interleaved (Deploy#47): lane 1's failure message arrives AFTER lane 2 started its own suite,
    # and lane 2's results arrive while lane 1's failure is still pending. Each result keeps its own lane's
    # suite and its own failure detail.
    il = OutputParser("/work/run2/src", "portal-hosts")
    io_: list[dict] = []
    for ln in [
        "::mw-lane::1::Test run for /work/run2/src/bin/Alpha.Test.dll (.NETCoreApp,Version=v10.0)",
        "::mw-lane::1::  Failed Alpha.Test.Suite.Breaks [1 s]",
        "::mw-lane::2::Test run for /work/run2/src/bin/Beta.Test.dll (.NETCoreApp,Version=v10.0)",
        "::mw-lane::1::  Error Message:",
        "::mw-lane::2::  Passed Beta.Test.Suite.Holds [3 ms]",
        "::mw-lane::1::   alpha went wrong",
        "::mw-lane::2::  Failed Beta.Test.Suite.Falls [2 s]",
        "::mw-lane::2::  Error Message:",
        "::mw-lane::1::  Stack Trace:",
        "::mw-lane::2::   beta went wrong",
        "::mw-lane::1::     at Alpha.Test.Suite.Breaks() in /work/run2/src/test/Alpha.cs:line 7",
        "::mw-lane::2::  Stack Trace:",
        "::mw-lane::2::     at Beta.Test.Suite.Falls() in /work/run2/src/test/Beta.cs:line 9",
        "::mw-lane::1::  Passed Alpha.Test.Suite.Holds [1 ms]",
    ]:
        io_ += il.feed(ln)
    io_ += il.finish()
    it = {e["test"]: e for e in io_ if e["kind"] == "test-finished"}
    check("lanes/every interleaved result is reported once", sorted(it) == sorted(
        ["Alpha.Test.Suite.Breaks", "Alpha.Test.Suite.Holds", "Beta.Test.Suite.Holds", "Beta.Test.Suite.Falls"])
        and len([e for e in io_ if e["kind"] == "test-finished"]) == 4, io_)
    check("lanes/each result keeps its OWN lane's suite",
          [it.get(k, {}).get("suite") for k in ("Alpha.Test.Suite.Breaks", "Alpha.Test.Suite.Holds")] == ["Alpha.Test", "Alpha.Test"]
          and [it.get(k, {}).get("suite") for k in ("Beta.Test.Suite.Holds", "Beta.Test.Suite.Falls")] == ["Beta.Test", "Beta.Test"], it)
    check("lanes/each failure keeps its OWN message and source line",
          it.get("Alpha.Test.Suite.Breaks", {}).get("message") == "alpha went wrong"
          and it["Alpha.Test.Suite.Breaks"].get("file") == "test/Alpha.cs" and it["Alpha.Test.Suite.Breaks"].get("line") == 7
          and it.get("Beta.Test.Suite.Falls", {}).get("message") == "beta went wrong"
          and it["Beta.Test.Suite.Falls"].get("file") == "test/Beta.cs" and it["Beta.Test.Suite.Falls"].get("line") == 9, it)
    check("lanes/the stage's counters span every lane", il.tests == {"passed": 2, "failed": 2, "skipped": 0}, il.tests)
    # Negative control: the SAME interleaving without lane tags is what misattributed — lane 2's `Test run for`
    # moves lane 1's suite, so lane 1's later result lands in lane 2's suite.
    un = OutputParser("/work/run2/src", "portal-hosts")
    uo: list[dict] = []
    for ln in ["Test run for /work/run2/src/bin/Alpha.Test.dll", "  Failed Alpha.Test.Suite.Breaks [1 s]",
               "Test run for /work/run2/src/bin/Beta.Test.dll", "  Error Message:", "   alpha went wrong",
               "  Passed Alpha.Test.Suite.Holds [1 ms]"]:
        uo += un.feed(ln)
    uo += un.finish()
    ut = {e["test"]: e for e in uo if e["kind"] == "test-finished"}
    check("lanes/negative control: untagged interleaving loses lane 1's failure message and suite",
          ut.get("Alpha.Test.Suite.Breaks", {}).get("message") != "alpha went wrong"
          and ut.get("Alpha.Test.Suite.Holds", {}).get("suite") == "Beta.Test", ut)
    # A stage's own untagged lines between lane lines keep the stage's own state.
    mx = OutputParser("/w", "portal-hosts")
    mo = mx.feed("::mw-lane::a::Test run for /w/bin/A.Test.dll") + mx.feed("  Passed Own.Thing [1 ms]") \
        + mx.feed("::mw-lane::a::  Passed A.Test.X [1 ms]") + mx.finish()
    check("lanes/an untagged line is the stage's own, never a lane's",
          [(e["test"], e["suite"]) for e in mo if e["kind"] == "test-finished"] == [("Own.Thing", "Own"), ("A.Test.X", "A.Test")], mo)
    x = OutputParser("/w", "tests")
    xo = x.feed("    MeshWeaver.Store.Test.GateTest.Refuses [FAIL]") + x.feed("      Expected refusal, got grant") + x.finish()
    check("xunit/fail with message", xo and xo[0]["outcome"] == "failed"
          and xo[0]["message"] == "Expected refusal, got grant" and xo[0]["suite"] == "MeshWeaver.Store.Test.GateTest", xo)
    s = OutputParser("/w", "modules")
    check("mw-event/unit", s.feed('::mw-event::{"kind":"unit-finished","unit":"Store","outcome":"cached","durationMs":3}')
          == [{"kind": "unit-finished", "unit": "Store", "outcome": "cached", "durationMs": 3}])
    refused = s.feed('::mw-event::{"kind":"stage-finished","state":"succeeded"}')
    check("mw-event/a child cannot declare its stage green",
          [e["kind"] for e in refused] == ["line"] and "only the host decides" in refused[0]["text"], refused)
    refused = s.feed('::mw-event::{"kind":"run-finished","succeeded":true}')
    check("mw-event/a child cannot declare the run green", [e["kind"] for e in refused] == ["line"], refused)
    for bad in ('{not json}', '[1]', '{"kind":"diagnostic","line":"oops"}', '{"kind":"diagnostic","line":-1}',
                '{"kind":"diagnostic","severity":"fatal"}', '{"kind":"test-finished","outcome":"maybe"}',
                '{"kind":"count","unitsTotal":true}', '{"kind":"unit-finished","unit":"A","outcome":"won"}',
                '{"kind":"nope"}', '{"kind":"line"}'):
        got = s.feed("::mw-event::" + bad)
        check(f"mw-event/malformed {bad} is dropped and said, never raised",
              len(got) == 1 and got[0]["kind"] == "line" and "dropped" in got[0]["text"], got)
    ok_diag = s.feed('::mw-event::{"kind":"diagnostic","severity":"warning","file":"/w/a.cs","line":3,"code":"CS1","message":"m"}')
    check("mw-event/a well-formed diagnostic passes, typed", ok_diag and ok_diag[0]["line"] == 3 and ok_diag[0]["file"] == "a.cs", ok_diag)

    # ── 3. stage declaration ───────────────────────────────────────────────────────────────
    good = {"stages": [{"name": "compile-check", "kind": "build", "command": "true"},
                       {"name": "gate", "kind": "test", "command": "true", "timeoutMinutes": 30}]}
    check("stages/valid", [x["name"] for x in validate_stages(good)] == ["compile-check", "gate"])
    for bad in ({"stages": []}, {"stages": [{"name": "build/x", "kind": "build", "command": "t"}]},
                {"stages": [{"name": "a", "kind": "deploy", "command": "t"}]},
                {"stages": [{"name": "a", "kind": "build", "command": ""}]},
                {"stages": [{"name": "a", "kind": "build", "command": "t", "timeoutMinutes": 90}]},
                {"stages": [{"name": "a", "kind": "build", "command": "t"}] * 2}):
        try:
            validate_stages(bad)
            check(f"stages/refuses {bad}", False, "accepted")
        except Refusal:
            pass
    d = validate_stages(good)
    check("stages/no `after`: a stage runs after every earlier BUILD stage",
          [x["after"] for x in d] == [[], ["compile-check"]], [x["after"] for x in d])
    deps = validate_stages({"stages": [{"name": "build", "kind": "build", "command": "t"},
                                       {"name": "s0", "kind": "test", "command": "t"},
                                       {"name": "s1", "kind": "test", "command": "t"},
                                       {"name": "doc", "kind": "test", "command": "t", "after": ["build", "s0"]},
                                       {"name": "lone", "kind": "test", "command": "t", "after": []}]})
    check("stages/sibling test stages do NOT run after each other (only after the build)",
          [x["after"] for x in deps] == [[], ["build"], ["build"], ["build", "s0"], []], [x["after"] for x in deps])
    for bad in ({"stages": [{"name": "a", "kind": "build", "command": "t", "after": ["b"]},
                            {"name": "b", "kind": "test", "command": "t"}]},
                {"stages": [{"name": "a", "kind": "build", "command": "t", "after": ["a"]}]},
                {"stages": [{"name": "a", "kind": "build", "command": "t"},
                            {"name": "b", "kind": "test", "command": "t", "after": "a"}]},
                {"stages": [{"name": "a", "kind": "build", "command": "t"},
                            {"name": "b", "kind": "test", "command": "t", "after": ["a", "a"]}]},
                {"stages": [{"name": "a", "kind": "build", "command": "t"},
                            {"name": "b", "kind": "test", "command": "t", "after": ["nope"]}]}):
        try:
            validate_stages(bad)
            check(f"stages/refuses `after` {bad}", False, "accepted")
        except Refusal:
            pass
    check("stages/claim selects", [x["name"] for x in select_stages(d, ["build/gate"])] == ["gate"])
    check("stages/a stable stage check selects its stage", [x["name"] for x in select_stages(d, ["build-stable/gate"])] == ["gate"])
    try:
        select_stages(d, ["portal-hosts"])
        check("stages/undeclared claim is red", False, "accepted")
    except Refusal:
        pass

    # ── 4. the sandbox: the bwrap argv, the env allow-list, the token file ─────────────────
    sb = Sandbox("/usr/bin/bwrap", ["/cache", "/host"])
    av = sb.argv("make test", "/work/r/src", "/work/r/out", {"PATH": "/usr/bin", "MW_BUILD_PUBLISH": "false"})
    joined = " ".join(av)
    for flag in ("--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--clearenv",
                 "--new-session"):
        check(f"bwrap/{flag}", flag in av, av)
    check("bwrap/fresh /proc", "--proc /proc" in joined, joined)
    check("bwrap/caches READ-ONLY", "--ro-bind-try /cache /cache" in joined and "--bind /cache" not in joined, joined)
    check("bwrap/writable: only checkout, outputs, scratch",
          [av[i + 1] for i, a in enumerate(av) if a == "--bind"] == ["/work/r/src", "/work/r/out"]
          and "--tmpfs /scratch" in joined, joined)
    check("bwrap/env only via --setenv", av.count("--setenv") == 2 and av.index("--clearenv") < av.index("--setenv"), av)
    # THE PREPARED HOST: each fact is handed only when the image carries it (never an empty value).
    with tempfile.TemporaryDirectory() as pr:
        check("prepared/nothing installed: nothing handed", prepared_env(pr, os.path.join(pr, "tester")) == {}, pr)
        os.makedirs(os.path.join(pr, "platform-src"))
        open(os.path.join(pr, "platform-src", "Directory.Build.props"), "w").close()
        check("prepared/core source without its commit marker is NOT handed", "MW_PLATFORM_SOURCE" not in prepared_env(pr, pr), pr)
        open(os.path.join(pr, "platform-src", ".source-commit"), "w").write("a" * 40)
        os.makedirs(os.path.join(pr, "deploy-src", "deploy", "helm"))
        open(os.path.join(pr, "deploy-src", "deploy", "helm", "Chart.yaml"), "w").close()
        os.makedirs(os.path.join(pr, "tester"))
        open(os.path.join(pr, "tester", "mw-plugin-test.dll"), "w").close()
        pe = prepared_env(pr, os.path.join(pr, "tester"))
        check("prepared/all three handed", pe == {"MW_PLATFORM_SOURCE": os.path.join(pr, "platform-src"),
                                                    "MW_DEPLOY_SOURCE": os.path.join(pr, "deploy-src"),
                                                    "MW_BUILD_TESTER_APP": os.path.join(pr, "tester")}, pe)
        check("prepared/reaches a stage through the allow-list", sandbox_env({"PATH": "/usr/bin"}, pe, "/cache").get("MW_DEPLOY_SOURCE") == pe["MW_DEPLOY_SOURCE"], pe)
        # THE PREPARED TEST CLUSTER: handed only with its ready marker AND its starter.
        os.makedirs(os.path.join(pr, "pg-snapshot"))
        check("prepared/a snapshot without its ready marker is NOT handed", "MW_TEST_PG_SNAPSHOT" not in prepared_env(pr, pr), pr)
        open(os.path.join(pr, "pg-snapshot", "ready"), "w").write("template=mw_test_template\n")
        check("prepared/a snapshot without its starter is NOT handed", "MW_TEST_PG_SNAPSHOT" not in prepared_env(pr, pr), pr)
        open(os.path.join(pr, "host-postgres.sh"), "w").close()
        pe = prepared_env(pr, pr)
        check("prepared/the snapshot and its starter are handed together",
              pe.get("MW_TEST_PG_SNAPSHOT") == os.path.join(pr, "pg-snapshot") and pe.get("MW_TEST_PG_SERVER") == os.path.join(pr, "host-postgres.sh"), pe)
    with tempfile.TemporaryDirectory() as ps:
        check("platform scripts/absent: no MW_PLATFORM_SCRIPTS at all (never an empty value)", platform_scripts_env(ps) == {}, ps)
        for n in PLATFORM_SCRIPTS:
            open(os.path.join(ps, n), "w").close()
        check("platform scripts/present: MW_PLATFORM_SCRIPTS names the runner's directory", platform_scripts_env(ps) == {"MW_PLATFORM_SCRIPTS": ps}, ps)
        pe = sandbox_env({"PATH": "/usr/bin"}, platform_scripts_env(ps), "/cache")
        check("platform scripts/reach the stage through the allow-list", pe.get("MW_PLATFORM_SCRIPTS") == ps, pe)
    check("bwrap/command last", av[-4:] == ["--", "/bin/bash", "-c", "make test"], av[-4:])
    check("bwrap/no network namespace by default (an OPEN host: builds fetch packages)", "--unshare-net" not in av and "--unshare-all" not in av)
    oav = Sandbox("bwrap", []).argv("make test", "/w/s", "/w/o", {"PATH": "/bin"}, network=False)
    check("offline/a stage with network=False gets its OWN network namespace (loopback only), before the command",
          "--unshare-net" in oav and oav.index("--unshare-net") < oav.index("--"), oav[:12])
    check("offline/NuGet's ONLY source is the prepared package folder (RestoreSources replaces every NuGet.Config source)",
          offline_restore_env("/cache") == {"RestoreSources": "/cache/nuget"}
          and sandbox_env({"PATH": "/bin"}, offline_restore_env("/cache"), "/cache")["RestoreSources"] == "/cache/nuget")
    ho_ = Host({STAGE_NETWORK_ENV: "offline"}, None, log=lambda s: None)
    hn_ = Host({}, None, log=lambda s: None)
    check("offline/the switch: `offline` is offline, unset is open", ho_.stage_offline and not hn_.stage_offline and hn_.stage_network == "open")
    # the DISK guard: a claim is taken only when one run's footprint still leaves the eviction headroom free
    hd_ = Host({RUN_DISK_ENV: "20", DISK_HEADROOM_ENV: "15"}, None, log=lambda s: None)
    G_ = 1 << 30
    hd_.disk_stat = lambda p_: (100 * G_, 40 * G_)
    check("disk/room: 40 GiB free of 100, a 20 GiB run, 15 % kept → claims", hd_.disk_refusal() is None)
    hd_.disk_stat = lambda p_: (100 * G_, 30 * G_)
    why_d = hd_.disk_refusal() or ""
    check("disk/no room: 30 GiB free of 100 → claims nothing, naming the free space, the run's need and the headroom",
          "30.0 GiB free of 100.0 GiB" in why_d and RUN_DISK_ENV in why_d and "15 %" in why_d, why_d)
    hd_.disk_stat = lambda p_: (_ for _ in ()).throw(OSError("gone"))
    check("disk/an unreadable filesystem never blocks a claim", hd_.disk_refusal() is None)
    hz_ = Host({RUN_DISK_ENV: "0", DISK_HEADROOM_ENV: "0"}, None, log=lambda s: None)
    hz_.disk_stat = lambda p_: (100 * G_, 1)
    check("disk/0 and 0 turn the guard off", hz_.disk_refusal() is None)
    hu_ = Host({}, None, log=lambda s: None)
    hu_.disk_stat = lambda p_: (100 * G_, 30 * G_)
    check("disk/unset: the chart's defaults hold (20 GiB, 15 %) — a host on an older chart copy is guarded too",
          hu_.disk_refusal() is not None and hu_.run_disk == 20 * G_ and hu_.disk_headroom == 15.0)
    check("disk/values: whole and fractional GiB, 0 allowed, junk and out-of-range → the default",
          disk_gib("2.5", 0) == int(2.5 * G_) and disk_gib("0", 7) == 0 and disk_gib("x", 7) == 7 * G_ and disk_gib("-1", 7) == 7 * G_
          and disk_gib("nan", 7) == 7 * G_ and disk_percent("95", 15) == 15 and disk_percent("10", 15) == 10)
    # The host's OWN sandbox binds every staged image root read-only — /core-tests included: core's
    # shard and doc-gate stages run `python3 /core-tests/core-tests.py …` (core .github/build-stages.json).
    hav = " ".join(Host({}, None, log=lambda s: None).sandbox.argv("true", "/w/s", "/w/o", {}))
    long_unit = "diagnostics/portal-hosts/test-logs/src__MeshWeaver.Fleet.Control.Test__bin__Release__net10.0__test-logs__" + "A" * 300 + ".log"
    short_unit = artifact_unit(long_unit)
    check("artifact/a unit too long for the store's receipt is shortened deterministically, its directory and extension kept",
          len(short_unit) <= ARTIFACT_UNIT_MAX and short_unit.startswith("diagnostics/portal-hosts/test-logs/") and short_unit.endswith(".log")
          and artifact_unit(long_unit) == short_unit and artifact_unit(long_unit + "x") != short_unit, short_unit)
    deep = "diagnostics/" + "/".join(["d" * 40] * 8) + "/x.log"
    check("artifact/a unit whose DIRECTORY is too long still fits, its top directory and extension kept",
          len(artifact_unit(deep)) <= ARTIFACT_UNIT_MAX and artifact_unit(deep).startswith("diagnostics/~")
          and artifact_unit(deep).endswith(".log") and artifact_unit(deep) != artifact_unit(deep + "x"), artifact_unit(deep))
    check("artifact/a unit that fits is never renamed (consumers read gate-log/gate.log by name)",
          artifact_unit("gate-log/gate.log") == "gate-log/gate.log")
    mav = Host({}, None, log=lambda s: None).sandbox.argv("true", "/w/run-7/src", "/w/o", {}, mount=SANDBOX_CHECKOUT)
    check("bwrap/a run's checkout appears at ONE fixed path inside the sandbox (the same for every run and the warm build)",
          mav[mav.index("/w/run-7/src") + 1] == SANDBOX_CHECKOUT and mav[mav.index("--chdir") + 1] == SANDBOX_CHECKOUT
          and mav.count("/w/run-7/src") == 1, mav)
    check("bwrap/without a mount the checkout keeps its own path (prepare and probe sandboxes)",
          "--bind /w/s /w/s" in hav and "--chdir /w/s" in hav, hav)
    bav = Host({}, None, log=lambda s: None).sandbox.argv("true", "/w/s", "/w/o", {}, mount=SANDBOX_CHECKOUT,
                                                         binds=[("/w/run-7/nuget", SANDBOX_NUGET)])
    check("bwrap/the package folder is bound WRITABLE at its fixed in-sandbox path",
          " ".join(bav).count(f"--bind /w/run-7/nuget {SANDBOX_NUGET}") == 1, bav)
    for root in ("/portal", "/tester", "/tools", "/core-tests"):
        check(f"bwrap/the staged image root {root} is visible READ-ONLY in a stage",
              f"--ro-bind-try {root} {root}" in hav and f"--bind {root} " not in hav, hav)
    se = sandbox_env({"PATH": "/bin", "BUILD_TOKEN_FILE": "/run/build-worker/token", "AZURE_CLIENT_SECRET": "s",
                      "BUILD_API": "https://x", "KUBERNETES_SERVICE_HOST": "10.0.0.1", "MeshWeaverRefs": "/portal/app"},
                     {"MW_BUILD_PUBLISH": "false"}, "/cache")
    check("env/allow-list only", set(se) == {"PATH", "MeshWeaverRefs", "HOME", "TMPDIR", "NUGET_PACKAGES",
                                             "NUGET_FALLBACK_PACKAGES", "MW_BUILD_PUBLISH"}, sorted(se))
    be = sandbox_env({"PATH": "/bin", "MESHWEAVER_CHROMIUM_PATH": "/usr/bin/chromium", "CHROME_BIN": "/usr/bin/chromium",
                      "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"}, {}, "/cache")
    check("env/the image's browser reaches a stage (core's shards export MESHWEAVER_CHROMIUM_PATH)",
          be.get("MESHWEAVER_CHROMIUM_PATH") == "/usr/bin/chromium" and be.get("CHROME_BIN") == "/usr/bin/chromium"
          and be.get("PLAYWRIGHT_BROWSERS_PATH") == "/opt/ms-playwright", sorted(be))
    try:
        sandbox_env({}, {"GITHUB_TOKEN": "t"}, "/cache")
        check("env/credential-shaped extra refused", False, "accepted")
    except Refusal:
        pass
    probe_calls: list[list[str]] = []

    def probe_fails(argv):
        probe_calls.append(argv)
        return 1, "bwrap: No permissions to creating new namespace, likely because the kernel does not allow non-privileged user namespaces"
    try:
        sb.probe(probe_fails)
        check("probe/no namespaces is FATAL at startup", False, "accepted")
    except Fatal as e:
        check("probe/names why", "No permissions" in str(e) and "unsandboxed" in str(e), e)
    check("probe/uses the same sandbox argv", probe_calls and probe_calls[0][0] == "/usr/bin/bwrap"
          and "--unshare-pid" in probe_calls[0], probe_calls)
    sb.probe(lambda argv: (0, ""))      # a node that allows them starts

    # the boundary self-check, with injected results
    sbx = Sandbox("/usr/bin/bwrap", [])
    seen_argv: list[list[str]] = []

    def boundary_run(results):
        def run(argv):
            seen_argv.append(argv)
            return 0, "bwrap noise\n" + json.dumps(results)
        return run
    blocked = {n: "blocked (URLError)" for n, _, _ in BOUNDARY_PROBES}
    boundary_check(sbx, {}, boundary_run(blocked))
    check("boundary/probed from INSIDE a sandbox", seen_argv and seen_argv[0][0] == "/usr/bin/bwrap"
          and "--unshare-pid" in seen_argv[0] and "169.254.169.254" in seen_argv[0][-1], seen_argv[:1])
    check("boundary/the metadata probe sends Metadata: true", '"Metadata": "true"' in seen_argv[0][-1])
    for name, _, _ in BOUNDARY_PROBES:
        for answer in ("answered", "answered (HTTP 403)"):
            try:
                boundary_check(sbx, {}, boundary_run(dict(blocked, **{name: answer})))
                check(f"boundary/{name} {answer} refuses to start", False, "started")
            except Fatal as e:
                check(f"boundary/the refusal names {name}", name in str(e), e)
    for broken in ((1, "python3: not found"), (0, "not json"), (0, json.dumps({"only": "one"}))):
        try:
            boundary_check(sbx, {}, lambda argv, b=broken: b)
            check(f"boundary/an unmeasured boundary {broken} refuses to start", False, "started")
        except Fatal:
            pass
    seen_argv.clear()
    boundary_check(sbx, {"MW_BUILD_BOUNDARY_PRIVATE_URL": "http://10.1.2.3/"}, boundary_run(blocked))
    check("boundary/the private probe address is configurable", "10.1.2.3" in seen_argv[0][-1], seen_argv[0][-1][-200:])

    tmp = tempfile.mkdtemp(prefix="build-run-selftest-")
    try:
        tf = os.path.join(tmp, "token")
        with open(tf, "w") as fh:
            fh.write("worker-secret\n")
        check("token/from the file", read_worker_token({"BUILD_TOKEN_FILE": tf}) == "worker-secret")
        for bad_env in ({"BUILD_TOKEN": "x", "BUILD_TOKEN_FILE": tf}, {"BUILD_TOKEN_FILE": os.path.join(tmp, "absent")}):
            try:
                read_worker_token(bad_env)
                check(f"token/refuses {sorted(bad_env)}", False, "accepted")
            except Refusal:
                pass

        # namespace leftovers: a fake /proc with one process in a foreign pid namespace
        fake = os.path.join(tmp, "proc")
        for pid, ns in (("self", "pid:[1]"), ("10", "pid:[1]"), ("11", "pid:[2]")):
            os.makedirs(os.path.join(fake, pid, "ns"))
            os.symlink(ns, os.path.join(fake, pid, "ns", "pid"))
        check("leftovers/foreign namespace found", pid_namespace_leftovers(fake) == [11], pid_namespace_leftovers(fake))
        # A ZOMBIE in a foreign namespace has exited: it is not a survivor (the user-namespace pool,
        # 2026-10-27 — the startup probe's bwrap, State Z, PPid 1, made every start refuse to claim).
        os.makedirs(os.path.join(fake, "12", "ns"))
        os.symlink("pid:[3]", os.path.join(fake, "12", "ns", "pid"))
        with open(os.path.join(fake, "12", "status"), "w") as fh:
            fh.write("Name:\tbwrap\nState:\tZ (zombie)\nPPid:\t1\n")
        with open(os.path.join(fake, "11", "status"), "w") as fh:
            fh.write("Name:\tpython3\nState:\tS (sleeping)\nPPid:\t1\n")
        check("leftovers/a zombie is not a survivor, a sleeping process is", pid_namespace_leftovers(fake) == [11],
              pid_namespace_leftovers(fake))
        with open(os.path.join(fake, "12", "status"), "wb") as fh:
            fh.write(b"Name:\tbw\xff\xferap\nState:\tZ (zombie)\nPPid:\t1\n")
        check("leftovers/a non-UTF-8 task name neither raises nor hides a zombie", pid_namespace_leftovers(fake) == [11],
              pid_namespace_leftovers(fake))
        with open(os.path.join(fake, "11", "status"), "w", newline="") as fh:
            fh.write("Name:\t\rState:\tZ\r\nState:\tS (sleeping)\nPPid:\t1\n")
        check("leftovers/a task name forging a carriage-return State line does not hide a live survivor",
              pid_namespace_leftovers(fake) == [11], pid_namespace_leftovers(fake))
        # reap_children really reaps: a child that exited is collected without blocking.
        child = subprocess.Popen(["true"])
        # Wait for the child to EXIT without consuming its status (Popen waits only for exec), so the
        # reaper is what collects it — never a fixed sleep that a loaded runner can outrun (review of #7287).
        if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            reaped = reap_children()
        else:  # no waitid (macOS): retry the reaper until the child has exited, bounded
            reaped = 0
            for _ in range(200):
                reaped += reap_children()
                if reaped:
                    break
                time.sleep(0.05)
        child.returncode = 0  # reaped behind Popen's back; keep its finalizer quiet
        gone = True
        try:
            os.kill(child.pid, 0)
            gone = False
        except OSError:
            pass
        check("leftovers/reap_children collects an exited child", reaped >= 1 and gone, f"reaped={reaped} gone={gone}")
        # assert_clean reaps BEFORE it decides, and again after the kill.
        calls: list[str] = []
        hz = Host({"MW_BUILD_CHANNEL": "latest"}, _FakeApi({}), log=lambda s: None, sandbox=_EnvSandbox(),
                  leftovers=lambda: (calls.append("look"), [])[1], reap=lambda: (calls.append("reap"), 0)[1],
                  sleep=lambda s: None)
        hz.assert_clean()
        check("leftovers/assert_clean reaps before looking", calls[:2] == ["reap", "look"], calls)
        hl = Host({"MW_BUILD_CHANNEL": "latest"}, _FakeApi({}), log=lambda s: None, sandbox=_EnvSandbox(),
                  leftovers=lambda: [999999999], sleep=lambda s: None)
        try:
            hl.cycle()
            check("leftovers/host refuses to claim", False, "claimed")
        except Fatal:
            check("leftovers/no claim was attempted", not any(c[1] == "/claim" for c in hl.api.calls), hl.api.calls)

        # ── 5. cache promotion: trunk only, content keys only, links not followed ──────────
        outd = os.path.join(tmp, "o")
        key = "ab" * 16
        os.makedirs(os.path.join(outd, key))
        with open(os.path.join(outd, key, "X.dll"), "w") as fh:
            fh.write("x")
        os.makedirs(os.path.join(outd, "not-a-key"))
        os.symlink("/etc", os.path.join(outd, "cd" * 16))
        cache = os.path.join(tmp, "cache")
        pr, kp, rf = promote_outputs(outd, cache, lambda s: None)
        check("promote/one promoted", pr == 1 and os.path.isfile(os.path.join(cache, "outputs", key, "X.dll")), (pr, kp, rf))
        check("promote/non-key and symlink refused", sorted(rf) == sorted(["not-a-key", "cd" * 16]), rf)
        pr2, kp2, _ = promote_outputs(outd, cache, lambda s: None)
        check("promote/same key kept", pr2 == 0 and kp2 == 1, (pr2, kp2))

        # ── 5b. the git mirror: clone --mirror on first use, fetch before every checkout ───
        calls: list[tuple] = []

        def fake_git(*args):
            calls.append(args)
            if args[0] == "clone" and args[1] == "--mirror":
                os.makedirs(args[-1])
                open(os.path.join(args[-1], "HEAD"), "w").close()
        genv = {"MW_BUILD_CACHE": os.path.join(tmp, "gitcache"), "PATH": "/usr/bin:/bin"}
        default_checkout("MeshWeaver.Plugins", "d" * 40, ["refs/pull/7/head"], os.path.join(tmp, "r1", "src"),
                         None, genv, lambda s: None, git=fake_git)
        mirror = os.path.join(tmp, "gitcache", "git", "MeshWeaver.Plugins.git")
        first = [c[:2] for c in calls]
        check("mirror/first use clones --mirror, then fetches the wanted refs, then a --reference clone",
              first[0] == ("clone", "--mirror") and ("-C", mirror) in first
              and any(c[0] == "clone" and "--reference" in c and mirror in c for c in calls), calls)
        check("mirror/the PR ref and the sha are fetched into the mirror",
              any(c[:3] == ("-C", mirror, "fetch") and "refs/pull/7/head" in c and "d" * 40 in c for c in calls), calls)
        calls.clear()
        mlog: list[str] = []
        default_checkout("MeshWeaver.Plugins", "d" * 40, [], os.path.join(tmp, "r2", "src"),
                         None, genv, mlog.append, git=fake_git)
        check("mirror/later use fetches --prune, never re-clones the mirror",
              not any(c[:2] == ("clone", "--mirror") for c in calls)
              and any(c[:4] == ("-C", mirror, "fetch", "--quiet") and "--prune" in c for c in calls), calls)
        check("mirror/later use SAYS it fetched an existing mirror, and never that it created one",
              any(m.startswith(f"git mirror fetched (existing): {mirror} in ") for m in mlog)
              and not any(m.startswith("git mirror created:") for m in mlog), mlog)
        check("mirror/the checkout is detached at the sha", calls[-1][-2:] == ("--detach", "d" * 40), calls[-1])

        # ── 5a''. NOTHING IS EVER COPIED (§7.3b): the gate reads this file's own source ─────────────────────────────────
        own_src = open(os.path.abspath(__file__), encoding="utf-8").read()
        nc = no_copy_violations(own_src)
        check("no-copy/build-run.py copies no source or build tree anywhere (cp, rsync, tar, shutil.copytree/copyfile/copy/copy2)",
              nc == [], nc)
        check("no-copy/every allow-listed copy site is named with a reason", all(isinstance(v, str) and len(v) > 40 for v in NO_COPY_ALLOWED.values()))
        # NEGATIVE CONTROLS: a planted copy of each kind is caught — in a new function, and in an allow-listed one under
        # another name; an allow-list entry whose site is gone is caught too
        plants = {
            "shutil.copytree": "def _planted(a, b):\n    shutil.copytree(a, b)\n",
            "shutil.copyfile": "def _planted(a, b):\n    shutil.copyfile(a, b)\n",
            "shutil.copy2": "def _planted(a, b):\n    shutil.copy2(a, b)\n",
            "cp argv": "def _planted(a, b):\n    subprocess.run([\"cp\", \"-a\", a, b])\n",
            "cp argv in a loop": "def _planted(a, b):\n    for argv in ([\"cp\", \"-a\", \"--reflink=auto\", a, b],):\n        subprocess.run(argv)\n",
            "rsync argv": "def _planted(a, b):\n    subprocess.Popen([\"rsync\", \"-a\", a, b])\n",
            "tar argv": "def _planted(a, b):\n    subprocess.run([\"/usr/bin/tar\", \"-cf\", \"-\", a])\n",
            "a shell cp": "def _planted(a, b):\n    subprocess.run(f\"cp -a {a} {b}\", shell=True)\n",
            "a shell tar pipe": "def _planted(a, b):\n    subprocess.run([\"sh\", \"-c\", f\"tar -C {a} -cf - . | tar -C {b} -xf -\"])\n",
        }
        for name_, code_ in plants.items():
            got_ = no_copy_violations(own_src + "\n\n" + code_)
            check(f"no-copy/negative control: a planted {name_} FAILS the gate, named by its function", any("_planted" in v for v in got_), got_)
        check("no-copy/negative control: an allow-list entry that matches no site fails the gate",
              any("Nowhere.at_all" in v for v in no_copy_violations(own_src, dict(NO_COPY_ALLOWED, **{"Nowhere.at_all": "x" * 50}))))
        check("no-copy/negative control: the same copy outside its allow-listed function fails",
              no_copy_violations(own_src, {k: v for k, v in NO_COPY_ALLOWED.items() if k != "promote_outputs"}) != [])
        check("no-copy/the self-test's own fixtures are not the product (self_test is skipped)",
              no_copy_violations("def self_test():\n    shutil.copytree('a', 'b')\n", {}) == [])
        # bytes copied is MEASURED: a run layer holding a duplicate of the lower's file counts it, git's own .git and a changed
        # file do not
        bc = os.path.join(tmp, "bytes-copied")
        os.makedirs(os.path.join(bc, "lower", "s")); os.makedirs(os.path.join(bc, "up", "s")); os.makedirs(os.path.join(bc, "up", ".git"))
        open(os.path.join(bc, "lower", "s", "a.cs"), "w").write("same-bytes")
        open(os.path.join(bc, "lower", "s", "b.cs"), "w").write("old")
        open(os.path.join(bc, "up", "s", "b.cs"), "w").write("new")
        open(os.path.join(bc, "up", ".git", "index"), "w").write("x" * 99)
        check("no-copy/a layer with only changed files and git's own .git copied 0 bytes", copied_bytes(os.path.join(bc, "up"), os.path.join(bc, "lower")) == 0)
        open(os.path.join(bc, "up", "s", "a.cs"), "w").write("same-bytes")
        check("no-copy/negative control: a duplicate of the lower's file in the run's layer IS counted (10 bytes)",
              copied_bytes(os.path.join(bc, "up"), os.path.join(bc, "lower")) == 10)
        check("no-copy/no lower: nothing copied from one", copied_bytes(os.path.join(bc, "up"), None) == 0)

        # ── the RUN OVERLAY: a run's tree is its deployment tree (read-only lower) + its own upper ──────────────
        ns_log, execd = [], []
        def _exec(*a):
            execd.append(a)
        ov_env = {"MW_BUILD_RUN_OVERLAY": "true"}
        enter_run_namespace(["--host"], {}, ns_log.append, which=lambda n: "/usr/bin/unshare", probe=lambda c: 0, execvpe=_exec)
        check("overlay/not asked for: the runner is not re-executed", execd == [], execd)
        enter_run_namespace(["--host"], dict(ov_env, MW_BUILD_IN_NS="1"), ns_log.append, which=lambda n: "/usr/bin/unshare",
                            probe=lambda c: 0, execvpe=_exec)
        check("overlay/already in its namespace: never re-executed again", execd == [], execd)
        enter_run_namespace(["--host"], ov_env, ns_log.append, which=lambda n: None, probe=lambda c: 0, execvpe=_exec)
        check("overlay/no unshare: not re-executed, and said", execd == [] and "no `unshare`" in ns_log[-1], ns_log)
        enter_run_namespace(["--host"], ov_env, ns_log.append, which=lambda n: "/usr/bin/unshare", probe=lambda c: 1, execvpe=_exec)
        check("overlay/a host that cannot mount an overlay in a user namespace: not re-executed, and said (runs take a cold worktree)",
              execd == [] and "cannot mount an overlay" in ns_log[-1], ns_log)
        probed = []
        enter_run_namespace(["--host"], ov_env, ns_log.append, which=lambda n: "/usr/bin/unshare",
                            probe=lambda c: (probed.append(c), 0)[1], execvpe=_exec)
        check("overlay/the probe is a REAL overlay mount inside `unshare -Urm` (a write through it, the lower untouched)",
              probed and probed[0][:4] == ["/usr/bin/unshare", "-Urm", "--propagation", "private"]
              and "mount -t overlay" in probed[0][6] and "umount" in probed[0][6], probed)
        check("overlay/the probe mounts on the run's OWN filesystems — lower on the cache, upper on the work root — never mktemp's /tmp",
              probed and probed[0][-2:] == ["/cache", "/work"] and "mktemp -d -p \"$1\"" in probed[0][6]
              and "mktemp -d -p \"$2\"" in probed[0][6] and "mktemp -d)" not in probed[0][6], probed)
        probed.clear()
        enter_run_namespace(["--host"], dict(ov_env, MW_BUILD_CACHE="/c", MW_BUILD_WORK="/w"), ns_log.append,
                            which=lambda n: "/usr/bin/unshare", probe=lambda c: (probed.append(c), 0)[1], execvpe=lambda *a: None)
        check("overlay/the probe follows MW_BUILD_CACHE / MW_BUILD_WORK", probed and probed[0][-2:] == ["/c", "/w"], probed)
        check("overlay/the probe's cleanup is silent, so a failure's last stderr line is the CAUSE, not cleanup noise",
              "2>/dev/null; done; exit $rc" in _OVERLAY_PROBE and ".overlay-probe.XXXXXX" in _OVERLAY_PROBE)
        sw = os.path.join(tmp, "probe-sweep")
        for n, age in ((".overlay-probe.old", 3600), (".overlay-probe.young", 30), ("keep.old", 3600)):
            os.makedirs(os.path.join(sw, n, "u", "x"))
            t0 = time.time() - age
            os.utime(os.path.join(sw, n), (t0, t0))
        os.chmod(os.path.join(sw, ".overlay-probe.old", "u", "x"), 0o000)
        swept = sweep_stale_overlay_probes([sw, os.path.join(tmp, "no-such-root")])
        check("overlay/a stale probe dir (killed by its timeout) is swept, even with an unreadable child; a young one (another "
              "pod's probe in flight) and anything else are left; a missing root is no error",
              swept == [os.path.join(sw, ".overlay-probe.old")] and sorted(os.listdir(sw)) == [".overlay-probe.young", "keep.old"],
              (swept, os.listdir(sw)))
        check("overlay/a capable host re-executes this runner under unshare -Urm with the outer ids and the marker",
              len(execd) == 1 and execd[0][1][:4] == ["/usr/bin/unshare", "-Urm", "--propagation", "private"]
              and execd[0][1][-1] == "--host" and execd[0][2].get("MW_BUILD_IN_NS") == "1"
              and execd[0][2].get("MW_BUILD_OUTER_UID") == str(os.getuid()), execd)
        check("overlay/enabled only with the marker AND the warm workspace (there is no deployment tree without it)",
              run_overlay_enabled({"MW_BUILD_RUN_OVERLAY": "true", "MW_BUILD_IN_NS": "1", "MW_BUILD_WARM_WORKSPACE": "true"})
              and not run_overlay_enabled({"MW_BUILD_RUN_OVERLAY": "true", "MW_BUILD_WARM_WORKSPACE": "true"})
              and not run_overlay_enabled({"MW_BUILD_RUN_OVERLAY": "true", "MW_BUILD_IN_NS": "1"}))
        ids_av = Sandbox("bwrap", [], ("1654", "1654")).argv("true", "/w/s", "/w/o", {})
        plain_av = Sandbox("bwrap", []).argv("true", "/w/s", "/w/o", {})
        check("overlay/in the runner's namespace a stage keeps the pod's uid/gid (--uid/--gid right after --unshare-user)",
              ids_av[ids_av.index("--unshare-user") + 1:ids_av.index("--unshare-user") + 5] == ["--uid", "1654", "--gid", "1654"], ids_av)
        check("overlay/outside it the sandbox argv is unchanged (negative control)", "--uid" not in plain_av, plain_av)
        check("overlay/outer ids only inside the namespace", outer_ids({"MW_BUILD_IN_NS": "1", "MW_BUILD_OUTER_UID": "1654", "MW_BUILD_OUTER_GID": "1654"})
              == ("1654", "1654") and outer_ids({"MW_BUILD_OUTER_UID": "1654", "MW_BUILD_OUTER_GID": "1654"}) is None)
        # the mount itself, its refusal, and the release — with a recording runner
        calls = []
        def _sh_ok(cmd):
            calls.append(cmd)
            return 0, ""
        low = os.path.join(tmp, "ovl-lower")
        os.makedirs(low, exist_ok=True)
        ovl_dest = os.path.join(tmp, "ovl-run", "src")
        os.makedirs(os.path.dirname(ovl_dest), exist_ok=True)
        _overlay_tree(low, ovl_dest, _sh_ok)
        check("overlay/the run's tree is mounted with the deployment tree as the read-only lower and the run's own upper",
              calls and calls[0][:4] == ["mount", "-t", "overlay", "overlay"]
              and calls[0][5] == f"lowerdir={low},upperdir={ovl_dest}.upper,workdir={ovl_dest}.ovlwork" and calls[0][6] == ovl_dest, calls)
        refused = None
        try:
            _overlay_tree(low, os.path.join(tmp, "ovl-run2", "src"), lambda c: (1, "permission denied"))
        except Refusal as e:
            refused = str(e)
        check("overlay/a refused mount is a Refusal naming why (the caller takes a cold worktree instead)", refused and "permission denied" in refused, refused)
        for bad in ("/a,b", "/a:b"):
            try:
                _overlay_tree(bad, os.path.join(tmp, "ovl-run3", "src"), _sh_ok)
                bad_ok = False
            except Refusal:
                bad_ok = True
            check(f"overlay/a lower path overlayfs cannot parse ({bad!r}) is refused, never mounted wrong", bad_ok)
        um = []
        wk = os.path.join(tmp, "ovl-rel", "src.ovlwork", "work")
        os.makedirs(wk, exist_ok=True)
        os.chmod(wk, 0)
        release_run_tree(os.path.join(tmp, "ovl-rel", "src"), lambda c: (um.append(c), (0, ""))[1], ismount=lambda p: True)
        check("overlay/release unmounts the run's tree", um == [["umount", os.path.join(tmp, "ovl-rel", "src")]], um)
        remove_workspace(os.path.join(tmp, "ovl-rel"), release=lambda d: release_run_tree(d, lambda c: (0, ""), ismount=lambda p: False))
        check("overlay/the kernel's mode-000 work directory is made removable and the workspace is gone",
              not os.path.exists(os.path.join(tmp, "ovl-rel")))
        # the REAL kernel path where this host can show it: an unprivileged overlay in a user+mount namespace
        # (on a real filesystem: overlayfs takes no upper on overlayfs, and a container's /tmp often IS one — a host runs
        # its trees on /work and /cache; MW_BUILD_SELFTEST_REAL_DIR points the case there)
        real_root = os.environ.get("MW_BUILD_SELFTEST_REAL_DIR") or tmp
        on_overlay = subprocess.run(["findmnt", "-no", "FSTYPE", "-T", real_root], capture_output=True, text=True).stdout.strip() == "overlay" \
            if shutil.which("findmnt") else False
        if sys.platform.startswith("linux") and on_overlay:
            print(f"build-run self-test: overlay/REAL skipped — {real_root} is itself on overlayfs (set MW_BUILD_SELFTEST_REAL_DIR)")
        elif sys.platform.startswith("linux") and shutil.which("unshare") and subprocess.run(
                ["unshare", "-Urm", "true"], capture_output=True).returncode == 0:
            real = tempfile.mkdtemp(prefix="ovl-real-", dir=real_root)
            os.makedirs(os.path.join(real, "lower", "sub"))
            open(os.path.join(real, "lower", "sub", "a.txt"), "w").write("shared")
            open(os.path.join(real, "lower", "b.txt"), "w").write("keep")
            script = (f"mkdir -p {real}/run/src && "
                      f"mount -t overlay overlay -o lowerdir={real}/lower,upperdir={real}/run/src.upper,workdir={real}/run/src.ovlwork {real}/run/src && "
                      f"echo run > {real}/run/src/sub/a.txt && echo new > {real}/run/src/c.txt && rm {real}/run/src/b.txt && umount {real}/run/src")
            os.makedirs(os.path.join(real, "run", "src.upper"))
            os.makedirs(os.path.join(real, "run", "src.ovlwork"))
            rr = subprocess.run(["unshare", "-Urm", "sh", "-c", script], capture_output=True, text=True)
            check("overlay/REAL: the run's writes land in its upper and the shared lower is untouched",
                  rr.returncode == 0 and open(os.path.join(real, "lower", "sub", "a.txt")).read() == "shared"
                  and os.path.exists(os.path.join(real, "lower", "b.txt"))
                  and open(os.path.join(real, "run", "src.upper", "sub", "a.txt")).read().strip() == "run"
                  and open(os.path.join(real, "run", "src.upper", "c.txt")).read().strip() == "new", rr.stderr[-300:])
            remove_workspace(os.path.join(real, "run"), release=lambda d: release_run_tree(d, ismount=lambda p: False))
            check("overlay/REAL: the workspace (upper, kernel work dir) is removed", not os.path.exists(os.path.join(real, "run")))
            shutil.rmtree(real, ignore_errors=True)


        # ── TREE INTEGRITY: a reader uses a deployment tree only while it is exactly what its writer hashed ─────────────
        def _hashed(name: str) -> "tuple[str, str]":
            t_ = os.path.join(tmp, f"integ-{name}", "tree")
            os.makedirs(os.path.join(t_, "src", "obj"), exist_ok=True)
            for i_ in range(5):
                open(os.path.join(t_, "src", f"f{i_}.cs"), "w").write(f"class C{i_} {{}}")
            open(os.path.join(t_, "src", "obj", "out.dll"), "wb").write(b"\0" * 4096)
            os.symlink("f0.cs", os.path.join(t_, "src", "link.cs"))
            m_ = os.path.join(tmp, f"integ-{name}", "manifest.json")
            with open(m_, "w") as fh:
                json.dump(tree_manifest(t_, "d/R@" + "1" * 40), fh)
            return t_, m_
        t0, m0 = _hashed("clean")
        check("integrity/an untampered tree verifies, every file's stat and (here) every file's content",
              verify_tree(t0, m0, "d/R@" + "1" * 40, sample=1000) is None)
        tampers = {
            "content": lambda t_, m_: open(os.path.join(t_, "src", "f1.cs"), "w").write("class Evil {}"),
            "content-mtime-restored": lambda t_, m_: (lambda pth, st0: (open(pth, "w").write("class C2 {X}"),
                                                                         os.utime(pth, ns=(st0.st_atime_ns, st0.st_mtime_ns))))(
                os.path.join(t_, "src", "f2.cs"), os.stat(os.path.join(t_, "src", "f2.cs"))),
            "added": lambda t_, m_: open(os.path.join(t_, "src", "obj", "planted.dll"), "wb").write(b"x"),
            "removed": lambda t_, m_: os.remove(os.path.join(t_, "src", "f3.cs")),
            "relinked": lambda t_, m_: (os.remove(os.path.join(t_, "src", "link.cs")), os.symlink("/etc/passwd", os.path.join(t_, "src", "link.cs"))),
            "mode": lambda t_, m_: os.chmod(os.path.join(t_, "src", "f4.cs"), 0o777),
            "manifest-forged": lambda t_, m_: (lambda mm: (mm["entries"].pop(), open(m_, "w").write(json.dumps(mm))))(json.load(open(m_))),
            "manifest-removed": lambda t_, m_: os.remove(m_),
        }
        expect = {"content": "changed", "content-mtime-restored": "changed", "added": "added", "removed": "removed",
                  "relinked": "changed", "mode": "changed", "manifest-forged": "digest", "manifest-removed": "no integrity manifest"}
        for name_, tamper in tampers.items():
            t_, m_ = _hashed(name_)
            time.sleep(0.05)                                  # a later write gets a later ctime even at coarse granularity
            tamper(t_, m_)
            why_ = verify_tree(t_, m_, "d/R@" + "1" * 40, sample=1000)
            check(f"integrity/a tree {name_} after it was built is refused ({expect[name_]})", why_ and expect[name_] in why_, why_)
        t5, m5p = _hashed("sample")
        m5 = json.load(open(m5p))
        for e_ in m5["entries"]:
            if e_[0].endswith("f0.cs"):
                e_[6] = "0" * 64
        m5["digest"] = hashlib.sha256(json.dumps(m5["entries"], separators=(",", ":")).encode()).hexdigest()
        open(m5p, "w").write(json.dumps(m5))
        check("integrity/a content hash that no longer matches is refused when sampled",
              "no longer hashes" in (verify_tree(t5, m5p, "d/R@" + "1" * 40, sample=1000) or ""))
        check("integrity/another tip's (or tree's) manifest is refused",
              "not" in (verify_tree(t0, m0, "d/R@" + "2" * 40, sample=0) or ""))
        check("deploy/the ONE tree is named by the lane alone — no key, so a platform update starts no second tree — and is a "
              "path overlayfs can parse",
              deployment_name("stable") == "stable-tree" and deployment_name(None) == "default-tree"
              and not any(c in deployment_name("3.1,x:y\\z") for c in ",:\\"))

        plat_commit, plat_base = "0" * 40, "file:///nonexistent"      # the platform's master, set below where git exists
        # ── 5b'. THE MASTER AND THE DEPLOYMENT TREE (§7.3b), against REAL git: a local origin, the real master path ─────
        if shutil.which("git"):
            wroot = os.path.join(tmp, "warm-git")
            origin = os.path.join(wroot, "origin", "R.git")
            seed = os.path.join(wroot, "seed")
            os.makedirs(seed)
            genv_g = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": wroot, "GIT_CONFIG_NOSYSTEM": "1",
                      "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

            def sh(*a, cwd=seed):
                return subprocess.run(list(a), cwd=cwd, env=genv_g, check=True, capture_output=True, text=True).stdout.strip()
            sh("git", "init", "-q", "-b", "main")
            open(os.path.join(seed, "a.cs"), "w").write("v1")
            open(os.path.join(seed, "b.cs"), "w").write("unchanged")
            sh("git", "add", "-A"); sh("git", "commit", "-qm", "one")
            c1 = sh("git", "rev-parse", "HEAD")
            open(os.path.join(seed, "a.cs"), "w").write("v2")
            sh("git", "commit", "-qam", "two")
            c2 = sh("git", "rev-parse", "HEAD")
            os.makedirs(os.path.dirname(origin))
            sh("git", "clone", "-q", "--bare", seed, origin, cwd=wroot)
            wenv = {"PATH": genv_g["PATH"], "MW_BUILD_GIT_BASE": "file://" + os.path.dirname(origin),
                    "MW_BUILD_CACHE": os.path.join(wroot, "cache")}
            log0 = lambda m: None
            gw = _git_runner(wenv, wroot, None, None)
            mirror_w = master_path(wenv["MW_BUILD_CACHE"], "R")
            _update_mirror("R", wenv["MW_BUILD_GIT_BASE"] + "/R.git", os.path.dirname(mirror_w), mirror_w, [c2], gw, log0)

            def worktrees() -> str:
                return subprocess.run(["git", "--git-dir", mirror_w, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout

            def wt_count(p_: str, gd: str = "") -> int:
                lines_ = subprocess.run(["git", "--git-dir", gd or mirror_w, "worktree", "list", "--porcelain"],
                                        capture_output=True, text=True).stdout.splitlines()
                return sum(1 for l_ in lines_ if l_ in (f"worktree {p_}", f"worktree {os.path.realpath(p_)}"))

            dt = DeploymentTree(wenv["MW_BUILD_CACHE"], "stable-k1", "R")
            with dt.writer() as w1:
                check("deploy/the first writer holds the tree", w1 is not None)
                with dt.writer() as w2:
                    check("deploy/a SECOND writer cannot advance the tree while the first holds it (single writer)", w2 is None)
                line_a = advance_deployment_tree(w1, c1, "k1", gw)
                check("deploy/the writer ADDS the tree from nothing: a `git worktree add --detach` of the master at the tip",
                      line_a.startswith(f"deployment tree: added at main@{c1[:12]}") and open(os.path.join(dt.tree, "a.cs")).read() == "v1"
                      and wt_count(dt.tree) == 1 and os.path.isfile(os.path.join(dt.tree, ".git")), (line_a, worktrees()))
                check("deploy/state and admin live OUTSIDE the tree (host-private)", dt.admin() is not None
                      and not os.path.commonpath([dt.host, dt.tree]) == dt.tree and (dt.state() or {}).get("built") is None)
                with dt.reader("k1") as r_nb:
                    check("deploy/while the writer holds it no reader gets the tree (the lock is busy)",
                          r_nb is None and "locked" in (dt.last_refusal or ""), dt.last_refusal)
                # the warm build (stood in for): obj/ and bin/, then the manifest and the state before the lock is released
                os.makedirs(os.path.join(dt.tree, "obj")); os.makedirs(os.path.join(dt.tree, "bin"))
                open(os.path.join(dt.tree, "obj", "state.cache"), "w").write("main")
                open(os.path.join(dt.tree, "bin", "R.dll"), "w").write("built")
                w1.publish(c1, "k1")
            check("deploy/the integrity manifest is written beside the tree, never inside it",
                  os.path.isfile(dt.manifest_path) and not os.path.commonpath([dt.manifest_path, dt.tree]) == dt.tree)
            with dt.reader("k1") as held:
                check("deploy/a reader VERIFIES and gets the built tree and its tip", held == (dt.tree, c1), dt.last_refusal)
                with dt.writer() as w_r:
                    check("deploy/the writer NEVER advances while a reader holds the tree", w_r is None)
            with dt.reader("k2") as other_k:
                check("deploy/a reader of another build identity gets nothing (the tree was built by another compiler), said",
                      other_k is None and "another build identity" in (dt.last_refusal or ""), dt.last_refusal)
            # A PLATFORM UPDATE (a new build identity) rebuilds the ONE tree WHOLE, in place: same directory, same worktree,
            # obj/ and bin/ of every project removed; the pending whole rebuild survives an interruption until `publish`
            dt_w = DeploymentTree(wenv["MW_BUILD_CACHE"], "whole-tree", "R")
            with dt_w.writer() as ww:
                advance_deployment_tree(ww, c1, "id-A", gw)
                check("whole/a NEW tree is due a whole build (nothing tells what built it)", (dt_w.state() or {}).get("whole") is True)
                os.makedirs(os.path.join(dt_w.tree, "src", "obj")); os.makedirs(os.path.join(dt_w.tree, "src", "bin"))
                open(os.path.join(dt_w.tree, "src", "obj", "project.assets.json"), "w").write("A")
                open(os.path.join(dt_w.tree, "src", "bin", "M.dll"), "w").write("built by A")
                ww.publish(c1, "id-A")
                check("whole/publish clears the pending whole rebuild", "whole" not in (dt_w.state() or {}), dt_w.state())
                line_same = advance_deployment_tree(ww, c2, "id-A", gw)
                check("whole/the SAME identity keeps obj/ and bin/ (incremental)", "WHOLE" not in line_same
                      and os.path.isfile(os.path.join(dt_w.tree, "src", "bin", "M.dll")), line_same)
                ww.publish(c2, "id-A")
                ino_tree = os.stat(dt_w.tree).st_ino
                line_new = advance_deployment_tree(ww, c2, "id-B", gw)
                check("whole/a NEW identity (a platform update) rebuilds WHOLE in the same tree: obj/ and bin/ removed, said",
                      "WHOLE REBUILD" in line_new and os.stat(dt_w.tree).st_ino == ino_tree and wt_count(dt_w.tree) == 1
                      and not os.path.exists(os.path.join(dt_w.tree, "src", "obj")) and not os.path.exists(os.path.join(dt_w.tree, "src", "bin"))
                      and open(os.path.join(dt_w.tree, "a.cs")).read() == "v2", line_new)
                # interrupted (a claim stopped the build): the next advance with the SAME new identity is still WHOLE
                os.makedirs(os.path.join(dt_w.tree, "src", "obj"))
                open(os.path.join(dt_w.tree, "src", "obj", "half.dll"), "w").write("half-built by B")
                ww.unbuild("id-B", c2)
                check("whole/an interrupted whole rebuild stays pending (unbuild keeps it)", (dt_w.state() or {}).get("whole") is True)
                line_again = advance_deployment_tree(ww, c2, "id-B", gw)
                check("whole/an interrupted whole rebuild is REDONE, never resumed incrementally",
                      "WHOLE REBUILD" in line_again and not os.path.exists(os.path.join(dt_w.tree, "src", "obj")), line_again)
                ww.publish(c2, "id-B")
            check("whole/whole_rebuild_due: lost state, another key or a pending flag — and nothing else",
                  whole_rebuild_due(None, "k") and whole_rebuild_due({"key": "j"}, "k") and whole_rebuild_due({"key": "k", "whole": True}, "k")
                  and not whole_rebuild_due({"key": "k", "built": "x"}, "k"))
            # advancing from an OLDER tip, in place: only changed files are rewritten, unchanged ones keep their mtime and
            # inode, obj/ and bin/ are kept, anything else untracked is cleaned
            old_t = 1_000_000_000
            os.utime(os.path.join(dt.tree, "b.cs"), (old_t, old_t))
            ino_b = os.stat(os.path.join(dt.tree, "b.cs")).st_ino
            open(os.path.join(dt.tree, "stray.txt"), "w").write("x")
            with dt.writer() as w3:
                line_m = advance_deployment_tree(w3, c2, "k1", gw)
                w3.publish(c2, "k1")
            check("deploy/the writer MOVES the tree in place (checkout --detach -f), saying so",
                  line_m.startswith(f"deployment tree: moved to main@{c2[:12]} in place") and open(os.path.join(dt.tree, "a.cs")).read() == "v2", line_m)
            check("deploy/an unchanged file keeps its mtime and inode (never rewritten: MSBuild stays incremental)",
                  int(os.stat(os.path.join(dt.tree, "b.cs")).st_mtime) == old_t and os.stat(os.path.join(dt.tree, "b.cs")).st_ino == ino_b,
                  os.stat(os.path.join(dt.tree, "b.cs")).st_mtime)
            check("deploy/obj/ and bin/ are kept, any other untracked file is cleaned",
                  open(os.path.join(dt.tree, "obj", "state.cache")).read() == "main" and os.path.isfile(os.path.join(dt.tree, "bin", "R.dll"))
                  and not os.path.exists(os.path.join(dt.tree, "stray.txt")))
            check("deploy/the tree is still ONE registered worktree (moved, never re-added)", wt_count(dt.tree) == 1)
            # a tracked file the warmBuild modified is restored; a planted .git (hooks, filters, merge drivers) never runs
            open(os.path.join(dt.tree, "b.cs"), "w").write("modified by warmBuild")
            os.remove(os.path.join(dt.tree, ".git"))
            planted = os.path.join(dt.tree, ".git")
            os.makedirs(os.path.join(planted, "hooks"))
            marker_h = os.path.join(wroot, "hook-ran")
            with open(os.path.join(planted, "hooks", "post-checkout"), "w") as fh:
                fh.write(f"#!/bin/sh\ntouch {marker_h}\n")
            os.chmod(os.path.join(planted, "hooks", "post-checkout"), 0o755)
            with open(os.path.join(planted, "config"), "w") as fh:
                fh.write(f"[filter \"evil\"]\n\tsmudge = touch {marker_h}-filter; cat\n\tclean = cat\n")
            with open(os.path.join(dt.tree, ".gitattributes"), "w") as fh:
                fh.write("* filter=evil\n")
            with dt.writer() as w4:
                advance_deployment_tree(w4, c1, "k1", gw)
            check("deploy/a tracked file the warmBuild modified is restored to the commit", open(os.path.join(dt.tree, "b.cs")).read() == "unchanged")
            check("deploy/a planted .git is replaced by the host's link before any git step; no hook or filter ran",
                  os.path.isfile(os.path.join(dt.tree, ".git")) and open(os.path.join(dt.tree, ".git")).read().strip() == f"gitdir: {dt.admin()}"
                  and not any(os.path.exists(marker_h + x) for x in ("", "-filter")))
            with dt.reader("k1") as r_nb2:
                check("deploy/an advance that was not followed by a build leaves the tree NOT BUILT (readers go cold)",
                      r_nb2 is None and "not built" in (dt.last_refusal or ""), dt.last_refusal)
            with dt.writer() as w5:
                w5.publish(c1, "k1")
            time.sleep(0.05)
            open(os.path.join(dt.tree, "obj", "state.cache"), "a").write("POISON")
            with dt.reader("k1") as r_t:
                check("deploy/a tree changed after its build is refused by the integrity check, said",
                      r_t is None and "integrity check refused" in (dt.last_refusal or ""), dt.last_refusal)
            # the master lock serialises worktree add/remove: a cold add waits while another holder has the lock
            cold_a = os.path.join(wroot, "work", "cold-a", "src")
            done_a = threading.Event()
            with master_lock(os.path.dirname(mirror_w), "R"):
                th_ = threading.Thread(target=lambda: (cold_worktree(wenv["MW_BUILD_CACHE"], "R", c1, cold_a, gw), done_a.set()), daemon=True)
                th_.start()
                time.sleep(0.5)
                check("deploy/the master lock serialises `worktree add`: it waits while another holder has the lock",
                      not done_a.is_set() and not os.path.exists(cold_a))
            th_.join(30)
            check("deploy/...and proceeds once the lock is released: a COLD worktree off the master at the sha",
                  done_a.is_set() and open(os.path.join(cold_a, "a.cs")).read() == "v1" and wt_count(cold_a) == 1)
            done_r = threading.Event()
            with master_lock(os.path.dirname(mirror_w), "R"):
                th_ = threading.Thread(target=lambda: (remove_cold_worktree(wenv["MW_BUILD_CACHE"], "R", cold_a, gw), done_r.set()), daemon=True)
                th_.start()
                time.sleep(0.5)
                check("deploy/the master lock serialises `worktree remove`: it waits while another holder has the lock",
                      not done_r.is_set() and os.path.isdir(cold_a))
            th_.join(30)
            check("deploy/a cold worktree is removed AND pruned (nothing stays registered)",
                  done_r.is_set() and not os.path.exists(cold_a) and wt_count(cold_a) == 0, worktrees())
            # a cold worktree whose directory is already gone (a swept workspace) is pruned from the master
            cold_b = os.path.join(wroot, "work", "cold-b", "src")
            cold_worktree(wenv["MW_BUILD_CACHE"], "R", c2, cold_b, gw)
            shutil.rmtree(os.path.dirname(cold_b))
            remove_cold_worktree(wenv["MW_BUILD_CACHE"], "R", cold_b, gw)
            check("deploy/a cold worktree whose directory is already gone is pruned", wt_count(cold_b) == 0, worktrees())
            # the platform source as a writable VIEW: a cold worktree of the platform's master at the image tree's
            # .source-commit where this runner cannot overlay — and NOT A COPY; gone afterwards
            porigin = os.path.join(os.path.dirname(origin), "MeshWeaver.git")
            pseed = os.path.join(wroot, "pseed"); os.makedirs(pseed)
            sh("git", "init", "-q", "-b", "main", cwd=pseed)
            open(os.path.join(pseed, "Directory.Build.props"), "w").write("<Project/>")
            sh("git", "add", "-A", cwd=pseed); sh("git", "commit", "-qm", "platform", cwd=pseed)
            pc = sh("git", "rev-parse", "HEAD", cwd=pseed)
            sh("git", "clone", "-q", "--bare", pseed, porigin, cwd=wroot)
            plat_commit, plat_base = pc, "file://" + os.path.dirname(origin)
            image_src = os.path.join(wroot, "image-platform-src"); os.makedirs(image_src)
            open(os.path.join(image_src, "Directory.Build.props"), "w").write("<Project/>")
            open(os.path.join(image_src, ".source-commit"), "w").write(pc + "\n")
            pv_log: list = []
            pview = os.path.join(wroot, "work", "pv", "platform")
            with platform_source_view(image_src, pview, wenv, pv_log.append, can_overlay=False) as pv:
                open(os.path.join(pv, "obj.txt"), "w").write("restore output")
                check("platform-view/without an overlay: a cold worktree of the platform's master at .source-commit, said",
                      pv == pview and open(os.path.join(pv, "Directory.Build.props")).read() == "<Project/>"
                      and any("COLD worktree" in m and pc[:12] in m for m in pv_log)
                      and wt_count(pview, master_path(wenv["MW_BUILD_CACHE"], "MeshWeaver")) == 1, pv_log)
            check("platform-view/the image's tree is never written and the view is gone (removed + pruned) afterwards",
                  not os.path.exists(os.path.join(image_src, "obj.txt")) and not os.path.exists(pview)
                  and wt_count(pview, master_path(wenv["MW_BUILD_CACHE"], "MeshWeaver")) == 0)
            pv_mounts: list = []
            with platform_source_view(image_src, os.path.join(wroot, "work", "pv2", "platform"), wenv, pv_log.append, can_overlay=True,
                                      mount=lambda lo, de: (pv_mounts.append((lo, de)), os.makedirs(de, exist_ok=True))) as pv2:
                check("platform-view/inside the runner's namespace: an OVERLAY with the image's tree as the read-only lower",
                      pv_mounts == [(image_src, pv2)], pv_mounts)
            # the IMAGE's tree as it is on a host: owned by an id the runner's namespace does not map — an overlay of it mounts
            # but cannot be written (eiger, 2026-11-09: every prepared input and warm restore "Access … is denied"), so it
            # takes the cold worktree even though the runner can overlay; an OWNED lower still overlays
            real_lw = globals()["lower_is_writable_through"]
            globals()["lower_is_writable_through"] = lambda lo: lo != image_src
            try:
                pv_mounts.clear(); said_own: list = []
                with platform_source_view(image_src, os.path.join(wroot, "work", "pv4", "platform"), wenv, said_own.append,
                                          can_overlay=True, mount=lambda lo, de: pv_mounts.append((lo, de))) as pv4:
                    open(os.path.join(pv4, "obj.txt"), "w").write("restore output")
                    check("platform-view/a lower this runner does not OWN is never overlaid (it could not be written): a cold "
                          "worktree instead, said with both uids",
                          pv_mounts == [] and any("not owned by this runner" in m for m in said_own)
                          and any("COLD worktree" in m for m in said_own)
                          and wt_count(pv4, master_path(wenv["MW_BUILD_CACHE"], "MeshWeaver")) == 1, (pv_mounts, said_own))
                owned = os.path.join(wroot, "owned-lower"); os.makedirs(owned)
                open(os.path.join(owned, ".source-commit"), "w").write(pc + "\n")
                with platform_source_view(owned, os.path.join(wroot, "work", "pv5", "platform"), wenv, log0, can_overlay=True,
                                          mount=lambda lo, de: (pv_mounts.append((lo, de)), os.makedirs(de, exist_ok=True))) as pv5:
                    check("platform-view/...while an OWNED lower (the deployment's platform tree) still overlays",
                          pv_mounts == [(owned, pv5)], pv_mounts)
            finally:
                globals()["lower_is_writable_through"] = real_lw
            said_missing: list = []
            real_lw2 = globals()["lower_is_writable_through"]
            globals()["lower_is_writable_through"] = lambda lo: False
            try:
                try:
                    with platform_source_view(os.path.join(wroot, "gone-lower"), os.path.join(wroot, "work", "pv7", "platform"), wenv,
                                              said_missing.append, can_overlay=True, mount=lambda lo, de: None):
                        pass
                except Refusal:
                    pass
            finally:
                globals()["lower_is_writable_through"] = real_lw2
            check("platform-view/a MISSING lower is said (owner unreadable) and refused, never an exception from the log line",
                  any("owner unreadable: FileNotFoundError" in m for m in said_missing), said_missing)
            check("platform-view/ownership is read from the lower itself: this process's own directory is writable-through, a "
                  "missing one is not", lower_is_writable_through(image_src) and not lower_is_writable_through(os.path.join(wroot, "nope")))
            empty_cache = os.path.join(wroot, "empty-cache"); os.makedirs(empty_cache)
            try:
                with platform_source_view(image_src, os.path.join(wroot, "work", "pv6", "platform"), dict(wenv, MW_BUILD_CACHE=empty_cache),
                                          log0, can_overlay=False, create_master=False):
                    pass
                check("platform-view/a STAGE's view never clones the platform master: none on the host is a Refusal", False, "yielded")
            except Refusal as e:
                check("platform-view/a STAGE's view never clones the platform master: none on the host is a Refusal",
                      "does not clone" in str(e) and not os.path.exists(master_path(empty_cache, "MeshWeaver")), str(e))
            no_commit = os.path.join(wroot, "image-no-commit"); os.makedirs(no_commit)
            try:
                with platform_source_view(no_commit, os.path.join(wroot, "work", "pv3", "platform"), wenv, log0, can_overlay=False):
                    pass
                check("platform-view/neither an overlay nor a .source-commit is a Refusal, never a copy", False, "yielded")
            except Refusal as e:
                check("platform-view/neither an overlay nor a .source-commit is a Refusal, never a copy", "without a copy" in str(e), str(e))
            check("warm/the warmBuild declaration is validated",
                  validate_warm_build({"stages": []}) is None
                  and validate_warm_build({"warmBuild": {"command": "dotnet build", "timeoutMinutes": 20}}) == {"command": "dotnet build", "timeoutMinutes": 20.0})
            for bad_w in ({"warmBuild": {}}, {"warmBuild": {"command": "x", "timeoutMinutes": 0}}, {"warmBuild": {"command": "x", "timeoutMinutes": 61}}):
                try:
                    validate_warm_build(bad_w)
                    check(f"warm/{bad_w} is refused", False, "accepted")
                except Refusal:
                    pass
            check("warm/the BUILD IDENTITY rides on the platform set, the framework identity, the tester/portal images and the compiler",
                  warm_key({"MW_BUILD_PLATFORM_VERSION": "3.1.1"}, "c003") != warm_key({"MW_BUILD_PLATFORM_VERSION": "3.1.2"}, "c003")
                  and warm_key({}, "a") != warm_key({}, "b")
                  and warm_key({"MW_BUILD_TESTER_DIGEST": "t"}, "a") != warm_key({}, "a")
                  and warm_key({TOOLCHAIN_ENV: "sdk=10.0.403"}, "a") != warm_key({TOOLCHAIN_ENV: "sdk=10.0.404"}, "a"))
            check("warm/the RUNNER IMAGE DIGEST keys nothing: a rebuilt runner with the same compiler keeps the tree's obj/ and bin/",
                  warm_key({"MW_BUILD_RUNNER_DIGEST": "sha256:" + "a" * 64, TOOLCHAIN_ENV: "x"}, "a")
                  == warm_key({"MW_BUILD_RUNNER_DIGEST": "sha256:" + "b" * 64, TOOLCHAIN_ENV: "x"}, "a"))
            # the compiler part: the SDKs and the architecture; NOT the runner's code or its helpers (a Deploy merge moves those)
            tc_dn = os.path.join(wroot, "tc-dotnet")
            os.makedirs(os.path.join(tc_dn, "sdk", "10.0.403"))
            open(os.path.join(tc_dn, "dotnet"), "w").close()
            tc_which = lambda n: os.path.join(tc_dn, "dotnet") if n == "dotnet" else None
            tc0 = build_toolchain(tc_which)
            check("toolchain/it names the layout, the architecture and the SDKs — and nothing of the runner (no code, no helpers)",
                  f"layout={WARM_LAYOUT}" in tc0 and "arch=" in tc0 and "sdk=10.0.403" in tc0
                  and "code=" not in tc0 and "helpers=" not in tc0, tc0)
            real_wk = globals()["WARM_KEEP"]
            globals()["WARM_KEEP"] = real_wk + ("planted/",)
            check("toolchain/a change to the runner's build code (a Deploy merge) leaves it unchanged — no whole rebuild",
                  build_toolchain(tc_which) == tc0)
            globals()["WARM_KEEP"] = real_wk
            os.makedirs(os.path.join(tc_dn, "sdk", "10.0.404"))
            check("toolchain/another .NET SDK in the image changes it (a new compiler)", "sdk=10.0.403,10.0.404" in build_toolchain(tc_which))
            no_root = build_toolchain(lambda n: None)
            check("toolchain/no dotnet is NAMED in the value (never an exception, never the empty digest)", "sdk=no-dotnet" in no_root, no_root)
            check("warm/opt-in only", warm_enabled({"MW_BUILD_WARM_WORKSPACE": "true"}) and not warm_enabled({}))
            check("warm/sharedTree stays a switch, only with the warm workspace", shared_tree_enabled({"MW_BUILD_WARM_WORKSPACE": "true", "MW_BUILD_SHARED_TREE": "true"})
                  and not shared_tree_enabled({"MW_BUILD_SHARED_TREE": "true"}) and not shared_tree_enabled({"MW_BUILD_WARM_WORKSPACE": "true"}))
            import types as _types
            old_run = os.path.join(wroot, "sweep-work", "R-old-run", "src")
            cold_worktree(wenv["MW_BUILD_CACHE"], "R", c1, old_run, gw)
            os.utime(os.path.dirname(old_run), (0, 0))
            swept = Host.sweep_workspaces(_types.SimpleNamespace(work=os.path.join(wroot, "sweep-work"), run_deadline=60.0,
                                                                 cache=wenv["MW_BUILD_CACHE"], env=wenv))
            check("warm/the workspace sweep removes an old kept run's workspace AND prunes its cold worktree from the master",
                  swept == ["R-old-run"] and wt_count(old_run) == 0, (swept, worktrees()))

        # ── 5b''. the module-hub broker (§7.4 wiring): identity is the HOST's, the stage only names a module ─────────
        mh_doc = {"moduleHub": {"modules": {"A": {"paths": ["src/A"]}, "B": {"paths": ["src/B"], "deps": ["A"]}},
                                "recipe": ["src/Directory.Build.props"]}}
        mh = validate_module_hub(mh_doc)
        check("modhub/the declaration is read, leaves first", module_order(mh["modules"]) == ["A", "B"], mh)
        for bad_mh, why_mh in (({"moduleHub": {"modules": {"A": {"paths": ["x"], "deps": ["Z"]}}}}, "not declared"),
                               ({"moduleHub": {"modules": {"A": {"paths": ["x"], "deps": ["B"]}, "B": {"paths": ["y"], "deps": ["A"]}}}}, "cycle"),
                               ({"moduleHub": {"modules": {"../A": {"paths": ["x"]}}}}, "safe path segment"),
                               ({"moduleHub": {"modules": {"A": {"paths": ["../x"]}}}}, "relative paths"),
                               ({"moduleHub": {"modules": {"A": {"paths": ["x"]}}, "recipe": ["/tmp/recipe"]}}, "relative paths")):
            try:
                validate_module_hub(bad_mh)
                check(f"modhub/{why_mh} is refused", False, "accepted")
            except Refusal as e:
                check(f"modhub/{why_mh} is refused, naming it", why_mh in str(e), str(e))
        check("modhub/no declaration is no broker", validate_module_hub({"stages": []}) is None)
        wid = widen_module_hub(mh, validate_module_hub({"moduleHub": {"modules": {"A": {"paths": ["src/NewDep"]}, "Evil": {"paths": ["x"]}},
                                                                     "recipe": ["src/Directory.PlatformRefs.targets"]}}))
        check("modhub/the run's declaration WIDENS the trusted one (a new dependency's paths, a recipe file) and never narrows it",
              wid["modules"]["A"]["paths"] == ["src/A", "src/NewDep"] and wid["modules"]["B"]["paths"] == ["src/B"]
              and "Evil" not in wid["modules"] and set(wid["recipe"]) == {"src/Directory.Build.props", "src/Directory.PlatformRefs.targets"}, wid)
        trees = {"src/A": "100644 blob a1 src/A/a.cs", "src/B": "100644 blob b1 src/B/b.cs", "src/Directory.Build.props": "100644 blob r1 src/Directory.Build.props"}
        fp1 = module_fingerprints(mh, lambda ps: "\n".join(trees[p] for p in ps), "tc")
        trees["src/A"] = "100644 blob a2 src/A/a.cs"
        fp2 = module_fingerprints(mh, lambda ps: "\n".join(trees[p] for p in ps), "tc")
        check("modhub/a change in A moves A's fingerprint AND its dependent B's (a dependency's fingerprint is an input)",
              fp1["A"] != fp2["A"] and fp1["B"] != fp2["B"])
        trees["src/B"] = "100644 blob b2 src/B/b.cs"
        fp3 = module_fingerprints(mh, lambda ps: "\n".join(trees[p] for p in ps), "tc")
        check("modhub/a change in B moves only B", fp3["A"] == fp2["A"] and fp3["B"] != fp2["B"])
        trees["src/Directory.Build.props"] = "100644 blob r2 src/Directory.Build.props"
        fp4 = module_fingerprints(mh, lambda ps: "\n".join(trees[p] for p in ps), "tc")
        check("modhub/a recipe change moves every module", fp4["A"] != fp3["A"] and fp4["B"] != fp3["B"])
        check("modhub/the toolchain moves every module", module_fingerprints(mh, lambda ps: "\n".join(trees[p] for p in ps), "tc2")["A"] != fp4["A"])
        mh_opt = validate_module_hub({"moduleHub": {"modules": {"A": {"paths": ["src/A"], "options": ["accept=targets lib"]},
                                                              "B": {"paths": ["src/B"], "deps": ["A"]}}, "recipe": ["src/Directory.Build.props"]}})
        check("modhub/a build option (the catalog's accept) moves the module and its dependents",
              module_fingerprints(mh_opt, lambda ps: "\n".join(trees[p] for p in ps), "tc")["A"] != fp4["A"]
              and module_fingerprints(mh_opt, lambda ps: "\n".join(trees[p] for p in ps), "tc")["B"] != fp4["B"])
        # the output tar: equal content → equal bytes, whatever the mtimes; a link is refused; extraction stays inside
        o1, o2 = os.path.join(tmp, "mh-o1"), os.path.join(tmp, "mh-o2")
        for d_, t_ in ((o1, 1000), (o2, 2000)):
            os.makedirs(os.path.join(d_, "lib"))
            for n_, c_ in (("lib/A.dll", b"IL"), ("A.closure.txt", b"x")):
                with open(os.path.join(d_, n_), "wb") as fh:
                    fh.write(c_)
                os.utime(os.path.join(d_, n_), (t_, t_))
        h1 = deterministic_tar(o1, os.path.join(tmp, "mh1.tar"))
        h2 = deterministic_tar(o2, os.path.join(tmp, "mh2.tar"))
        check("modhub/the output tar is deterministic (mtimes do not matter)", h1 == h2, (h1, h2))
        g1 = deterministic_tar(o1, os.path.join(tmp, "mh1.tgz"), compress=True)
        time.sleep(1.1)                                      # a gzip header carrying the clock would now differ
        g2 = deterministic_tar(o2, os.path.join(tmp, "mh2.tgz"), compress=True)
        check("modhub/the COMPRESSED output is deterministic too (no clock, no name in the gzip header)", g1 == g2 and g1 != h1, (g1, g2))
        safe_extract(os.path.join(tmp, "mh1.tgz"), os.path.join(tmp, "mh-xz"))
        check("modhub/a compressed output extracts as packed", open(os.path.join(tmp, "mh-xz", "lib", "A.dll"), "rb").read() == b"IL")
        big = os.path.join(tmp, "mh-big")
        os.makedirs(big)
        with open(os.path.join(big, "A.dll"), "wb") as fh:
            fh.write((b"MZ" + bytes(range(256)) * 8) * 4096)       # assembly-like: repetitive metadata and IL
        deterministic_tar(big, os.path.join(tmp, "mh-big.tar"))
        deterministic_tar(big, os.path.join(tmp, "mh-big.tgz"), compress=True)
        check("modhub/compression shrinks an output (what keeps the larger modules under the ingress body limit)",
              os.path.getsize(os.path.join(tmp, "mh-big.tgz")) * 3 < os.path.getsize(os.path.join(tmp, "mh-big.tar")),
              (os.path.getsize(os.path.join(tmp, "mh-big.tgz")), os.path.getsize(os.path.join(tmp, "mh-big.tar"))))
        safe_extract(os.path.join(tmp, "mh1.tar"), os.path.join(tmp, "mh-x"))
        check("modhub/the output extracts as packed", open(os.path.join(tmp, "mh-x", "lib", "A.dll"), "rb").read() == b"IL")
        import tarfile as _tf
        evil = os.path.join(tmp, "mh-evil.tar")
        with _tf.open(evil, "w") as t_:
            ti_ = _tf.TarInfo("../escape.txt"); ti_.size = 1
            import io as _io
            t_.addfile(ti_, _io.BytesIO(b"x"))
        try:
            safe_extract(evil, os.path.join(tmp, "mh-evil"))
            check("modhub/an entry outside the destination is refused", False, "extracted")
        except Refusal:
            pass
        os.symlink("/etc/passwd", os.path.join(o1, "link"))
        try:
            deterministic_tar(o1, os.path.join(tmp, "mh3.tar"))
            check("modhub/a symbolic link in an output is refused", False, "packed")
        except Refusal:
            pass

        def broker_with(script_, trunk_=False, order_=0, pr_=42):
            root_ = tempfile.mkdtemp(prefix="mh-root-", dir=tmp)
            api_ = _FakeApi(script_)
            return ModuleHubBroker(api_, "MeshWeaver.Plugins", "a" * 40, "build-host-mac-6d5f7c9b8-x2k4q", root_, fp4, trunk_, order_,
                                   None if trunk_ else pr_, lambda m: None, sleep=lambda s_: None), api_
        uploaded = {}

        def put_(path_, query_, data_, size_, timeout_):
            uploaded[path_] = data_
            return 201, {"sha256": path_.rsplit("/", 1)[-1]}
        br, api_g = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Wait", "step": {"seq": 3}}), (200, {"verdict": "Grant", "step": {"seq": 4}})],
                                 ("PUT", "/modules/MeshWeaver.Plugins/B/outputs/"): put_,
                                 ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True, "why": "published"})})
        with open(os.path.join(br.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B", "fingerprint": "sha256:" + "f" * 64, "lane": "Main"}, fh)   # what a stage may NOT decide
        br.poll_once()
        check("modhub/a Wait does not hold the broker: no answer yet, the module is asked again later",
              not os.path.exists(os.path.join(br.out, "resp", "B.json")) and "B" in br.pending)
        br.pending["B"] = 0                                   # its retry is due
        br.poll_once()
        resp_ = json.load(open(os.path.join(br.out, "resp", "B.json")))
        reqs_ = [c[2] for c in api_g.calls if c[1].endswith("/request")]
        check("modhub/Wait is re-asked until the hub grants", len(reqs_) == 2 and resp_["verdict"] == "Grant" and resp_["seq"] == 4, (reqs_, resp_))
        check("modhub/the HOST's fingerprint, lane and holder are sent — never the stage's",
              all(r_["fingerprint"] == fp4["B"] and r_["lane"] == "PullRequest" and r_["pullRequest"] == 42 and r_["requester"] == "build-host-mac-6d5f7c9b8-x2k4q"
                  and r_["commit"] == "a" * 40 and "order" not in r_ for r_ in reqs_), reqs_)
        os.makedirs(os.path.join(br.io, "build", "B"), exist_ok=True)
        with open(os.path.join(br.io, "build", "B", "B.dll"), "wb") as fh:
            fh.write(b"built B")
        with open(os.path.join(br.io, "done", "B.json"), "w") as fh:
            json.dump({"succeeded": True}, fh)
        br.build_tag = {"platformVersion": "3.1.11700", "frameworkIdentity": "c3e1"}
        br.poll_once()
        ack_ = json.load(open(os.path.join(br.out, "ack", "B.json")))
        check("modhub/the completion TAGS the stored output with the platform set and framework identity it was built against "
              "(its key stays the content hash)",
              [(c[2] or {}).get("platformVersion") for c in api_g.calls if c[1].endswith("/complete")] == ["3.1.11700"]
              and [(c[2] or {}).get("frameworkIdentity") for c in api_g.calls if c[1].endswith("/complete")] == ["c3e1"])
        comp_ = [c[2] for c in api_g.calls if c[1].endswith("/complete")]
        put_path = next(iter(uploaded))
        check("modhub/a granted build's output is uploaded BY ITS HASH, then the step completed with the granted seq",
              ack_["accepted"] and comp_ and comp_[0]["seq"] == 4 and comp_[0]["fingerprint"] == fp4["B"] and comp_[0]["holder"] == br.host
              and comp_[0]["outputHash"] == "sha256:" + put_path.rsplit("/", 1)[-1]
              and hashlib.sha256(uploaded[put_path]).hexdigest() == put_path.rsplit("/", 1)[-1], (ack_, comp_))
        puts_ = [c[3] for c in api_g.calls if c[0] == "PUT"]
        check("modhub/the output upload names its LEASE (holder, granted seq, fingerprint) — control accepts it only from the live holder",
              puts_ == [{"holder": br.host, "seq": "4", "fingerprint": fp4["B"]}], puts_)
        check("modhub/the broker hands the output over gzip-compressed", uploaded[put_path][:2] == b"\x1f\x8b", uploaded[put_path][:4])
        # an upload the ingress refuses (a bare 413, no body): the stage's warning names THAT, not only the hub's verdict
        br413, _ = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 9}})],
                                ("PUT", "/modules/MeshWeaver.Plugins/B/outputs/"): lambda p_, q_, d_, s_, t_: (413, None),
                                ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True, "why": "failed — the head stays at the last good step"})})
        with open(os.path.join(br413.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        br413.poll_once()
        os.makedirs(os.path.join(br413.io, "build", "B"), exist_ok=True)
        with open(os.path.join(br413.io, "build", "B", "B.dll"), "wb") as fh:
            fh.write(b"built B")
        with open(os.path.join(br413.io, "done", "B.json"), "w") as fh:
            json.dump({"succeeded": True}, fh)
        br413.poll_once()
        ack413 = json.load(open(os.path.join(br413.out, "ack", "B.json")))
        check("modhub/a refused hand-over names its OWN reason in the ack (the 413), beside the hub's verdict",
              ack413["succeeded"] is False and "upload was refused (413" in (ack413.get("why") or "")
              and "the hub recorded: failed" in ack413["why"], ack413)
        out_bytes = uploaded[put_path]
        out_hex = put_path.rsplit("/", 1)[-1]
        br2, api_h = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Hit", "step": {"seq": 4, "outputHash": "sha256:" + out_hex}})],
                                  ("GET-BYTES", "/modules/MeshWeaver.Plugins/B/outputs/" + out_hex): [(200, out_bytes)]}, trunk_=True, order_=77)
        with open(os.path.join(br2.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        br2.poll_once()
        resp2 = json.load(open(os.path.join(br2.out, "resp", "B.json")))
        check("modhub/a Hit is fetched, verified and extracted where the stage reads it",
              resp2["verdict"] == "Hit" and open(os.path.join(resp2["outputDir"], "B.dll"), "rb").read() == b"built B", resp2)
        sc = os.path.join(tmp, "mh-slices")
        brc, api_c = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Hit", "step": {"seq": 4, "outputHash": "sha256:" + out_hex}})],
                                  ("GET-BYTES", "/modules/MeshWeaver.Plugins/B/outputs/" + out_hex): [(200, out_bytes)]})
        brc.slice_cache = sc
        for round_ in (1, 2):
            shutil.rmtree(os.path.join(brc.out, "resp"), ignore_errors=True); os.makedirs(os.path.join(brc.out, "resp"))
            brc.answered.discard("req:B")
            with open(os.path.join(brc.io, "req", "B.json"), "w") as fh:
                json.dump({"module": "B"}, fh)
            brc.poll_once()
        downloads = [c for c in api_c.calls if c[0] == "GET-BYTES"]
        resp_c = json.load(open(os.path.join(brc.out, "resp", "B.json")))
        check("modhub/a slice already in this host's cache is used with NO download (the second Hit)",
              len(downloads) == 1 and resp_c.get("source") == "from this host's slice cache"
              and open(os.path.join(resp_c["outputDir"], "B.dll"), "rb").read() == b"built B"
              and os.path.isfile(os.path.join(sc, "MeshWeaver.Plugins", "B", out_hex, ".complete")), (downloads, resp_c))
        check("slices/a put records the entry's size beside it (`.bytes`), and the stage's copy carries no cache bookkeeping",
              int(open(os.path.join(sc, "MeshWeaver.Plugins", "B", out_hex, ".bytes")).read()) == len(b"built B")
              and not os.path.exists(os.path.join(resp_c["outputDir"], ".bytes"))
              and not os.path.exists(os.path.join(resp_c["outputDir"], ".complete")))
        # THE BOUND: least recently used first, never an entry in use or used recently, down to the low-water mark
        lru = os.path.join(tmp, "lru-slices")
        t_now = time.time()
        for i_, (age_, kib_) in enumerate([(9000, 400), (8000, 400), (7000, 400), (100, 400)]):
            e_ = os.path.join(lru, "R", f"M{i_}", f"{i_:064x}")
            os.makedirs(e_)
            open(os.path.join(e_, "M.dll"), "wb").write(b"x" * (kib_ * 1024))
            open(os.path.join(e_, ".complete"), "w").write("h")
            os.utime(os.path.join(e_, ".complete"), (t_now - age_, t_now - age_))
        os.makedirs(os.path.join(lru, "R", "M0", "f" * 64 + ".staging-1-2"))
        os.utime(os.path.join(lru, "R", "M0", "f" * 64 + ".staging-1-2"), (t_now - 2 * 86400, t_now - 2 * 86400))
        import fcntl as _fc
        held_ = open(os.path.join(lru, "R", "M1", f"{1:064x}.lock"), "a+")
        _fc.flock(held_.fileno(), _fc.LOCK_SH)               # a reader copying M1 right now
        lru_log: list = []
        ev_, after_ = bound_slice_cache(lru, 1000 * 1024, lru_log.append, now=t_now)
        held_.close()
        left_ = sorted(os.listdir(os.path.join(lru, "R", m_)) for m_ in ("M0", "M1", "M2", "M3"))
        check("slices/over the bound the LEAST RECENTLY USED go first; an entry a reader holds and one used within the minimum "
              "age are kept; it stops at the low-water mark; the line names the bound",
              ev_ == 2 and not os.path.exists(os.path.join(lru, "R", "M0", f"{0:064x}")) and not os.path.exists(os.path.join(lru, "R", "M2", f"{2:064x}"))
              and os.path.isdir(os.path.join(lru, "R", "M1", f"{1:064x}")) and os.path.isdir(os.path.join(lru, "R", "M3", f"{3:064x}"))
              and 800 * 1024 <= after_ <= 800 * 1024 + 64 and any(SLICE_CACHE_MAX_ENV in m_ and "evicted the 2 least recently used" in m_ for m_ in lru_log),
              (ev_, after_, left_, lru_log))
        check("slices/a staging directory a killed process left behind (over a day old) is removed",
              not os.path.exists(os.path.join(lru, "R", "M0", "f" * 64 + ".staging-1-2")))
        check("slices/an entry with no recorded size is measured once and recorded",
              os.path.isfile(os.path.join(lru, "R", "M3", f"{3:064x}", ".bytes")))
        check("slices/under the bound nothing is evicted", bound_slice_cache(lru, 10 << 20, lambda m_: None, now=t_now) == (0, after_))
        check("slices/a second evictor on the same cache skips (one at a time)",
              (lambda g_: (bound_slice_cache(lru, 1, lambda m_: None, now=t_now), g_.close())[0])(
                  _try_lock(os.path.join(lru, ".bound.lock"), _fc.LOCK_EX | _fc.LOCK_NB)) == (0, -1))
        check("slices/the bound is MW_BUILD_SLICE_CACHE_MAX_GIB, else the default — never unbounded",
              slice_cache_cap({SLICE_CACHE_MAX_ENV: "2"}) == 2 << 30 and slice_cache_cap({}) == int(SLICE_CACHE_DEFAULT_GIB * (1 << 30))
              and all(slice_cache_cap({SLICE_CACHE_MAX_ENV: v_}) == int(SLICE_CACHE_DEFAULT_GIB * (1 << 30)) for v_ in ("0", "-3", "x", "nan", "inf")))
        # an evicted slice is fetched again from the store by its hash (it is a cache of the store, never the only copy)
        shutil.rmtree(os.path.join(sc, "MeshWeaver.Plugins", "B", out_hex))
        brc.answered.discard("req:B")
        shutil.rmtree(os.path.join(brc.out, "resp"), ignore_errors=True); os.makedirs(os.path.join(brc.out, "resp"))
        api_c.script[("GET-BYTES", "/modules/MeshWeaver.Plugins/B/outputs/" + out_hex)] = [(200, out_bytes)]
        api_c.script[("POST", "/modules/MeshWeaver.Plugins/B/request")] = [(200, {"verdict": "Hit", "step": {"seq": 4, "outputHash": "sha256:" + out_hex}})]
        with open(os.path.join(brc.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        brc.poll_once()
        resp_e = json.load(open(os.path.join(brc.out, "resp", "B.json")))
        check("slices/an EVICTED slice is filled again from the store by its hash (a cold host fills before it builds)",
              resp_e.get("source") == "fetched from the hub's store" and open(os.path.join(resp_e["outputDir"], "B.dll"), "rb").read() == b"built B", resp_e)
        brl, api_l = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 9}, "leaseUntil": "2026-11-08T10:00:00Z"})],
                                  ("PUT", "/modules/MeshWeaver.Plugins/B/outputs/"): put_,
                                  ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True})})
        brl.utcnow = lambda: datetime.datetime(2026, 11, 8, 9, 59, 50, tzinfo=datetime.timezone.utc)   # 10 s before the end
        with open(os.path.join(brl.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        brl.poll_once()
        os.makedirs(os.path.join(brl.io, "build", "B"), exist_ok=True)
        open(os.path.join(brl.io, "build", "B", "B.dll"), "wb").write(b"late")
        with open(os.path.join(brl.io, "done", "B.json"), "w") as fh:
            json.dump({"succeeded": True}, fh)
        brl.poll_once()
        ack_l = json.load(open(os.path.join(brl.out, "ack", "B.json")))
        check("modhub/too close to the server's lease end, NOTHING is handed over (no upload, no completion)",
              ack_l["accepted"] is False and "lease ended" in ack_l["why"]
              and not any(c[1].endswith("/complete") or c[0] == "PUT" for c in api_l.calls), (ack_l, api_l.calls))
        # 🚨 the stage cannot redirect the host: a LINKED output root, or a link inside it, is refused and nothing is uploaded
        secret_dir = os.path.join(tmp, "host-secret"); os.makedirs(secret_dir, exist_ok=True)
        open(os.path.join(secret_dir, "token"), "w").write("worker-token")
        for shape in ("root-link", "inner-link"):
            bs, api_s = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 21}})],
                                     ("PUT", "/modules/MeshWeaver.Plugins/B/outputs/"): put_,
                                     ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True})})
            with open(os.path.join(bs.io, "req", "B.json"), "w") as fh:
                json.dump({"module": "B"}, fh)
            bs.poll_once()
            out_b = os.path.join(bs.io, "build", "B")
            shutil.rmtree(out_b, ignore_errors=True)
            if shape == "root-link":
                os.symlink(secret_dir, out_b)
            else:
                os.makedirs(out_b); open(os.path.join(out_b, "B.dll"), "w").write("x")
                os.symlink(os.path.join(secret_dir, "token"), os.path.join(out_b, "leak.txt"))
            with open(os.path.join(bs.io, "done", "B.json"), "w") as fh:
                json.dump({"succeeded": True}, fh)
            uploads_before = sum(1 for c in api_s.calls if c[0] == "PUT")
            bs.poll_once()
            comp_s = [c[2] for c in api_s.calls if c[1].endswith("/complete")]
            check(f"modhub/a {shape} in the stage's output is refused — nothing is packed or uploaded, the step fails",
                  sum(1 for c in api_s.calls if c[0] == "PUT") == uploads_before and comp_s and comp_s[-1]["succeeded"] is False
                  and ("link" in comp_s[-1]["error"] or "not a plain directory" in comp_s[-1]["error"]), comp_s)
            bs.stop("test end")
        # a FIFO planted where the host reads (a request, an output file) never blocks the broker: it is refused at once
        bf, api_f = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 61}})],
                                 ("PUT", "/modules/MeshWeaver.Plugins/B/outputs/"): put_,
                                 ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True})})
        os.mkfifo(os.path.join(bf.io, "req", "X.json"))
        t0_ = time.monotonic()
        bf.poll_once()
        check("modhub/a FIFO planted as a request never blocks the broker", time.monotonic() - t0_ < 5 and not os.path.exists(os.path.join(bf.out, "resp", "X.json")))
        with open(os.path.join(bf.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        bf.poll_once()
        os.makedirs(os.path.join(bf.io, "build", "B"), exist_ok=True)
        os.mkfifo(os.path.join(bf.io, "build", "B", "B.dll"))
        with open(os.path.join(bf.io, "done", "B.json"), "w") as fh:
            json.dump({"succeeded": True}, fh)
        t0_ = time.monotonic()
        bf.poll_once()
        comp_f = [c[2] for c in api_f.calls if c[1].endswith("/complete")]
        check("modhub/a FIFO planted in an output never blocks the broker and fails the hand-over",
              time.monotonic() - t0_ < 5 and comp_f and comp_f[-1]["succeeded"] is False, comp_f)
        bf.stop("test end")
        # a done file still being written is not consumed: it is read again once it is a complete JSON object
        bp, api_p = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 31}})],
                                 ("PUT", "/modules/MeshWeaver.Plugins/B/outputs/"): put_,
                                 ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True})})
        with open(os.path.join(bp.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        bp.poll_once()
        os.makedirs(os.path.join(bp.io, "build", "B"), exist_ok=True)
        open(os.path.join(bp.io, "build", "B", "B.dll"), "wb").write(b"B")
        open(os.path.join(bp.io, "done", "B.json"), "w").write('{"succee')
        bp.poll_once()
        check("modhub/a partial done file is not consumed (no completion sent)", not any(c[1].endswith("/complete") for c in api_p.calls))
        open(os.path.join(bp.io, "done", "B.json"), "w").write('{"succeeded": true}')
        bp.poll_once()
        check("modhub/...and once complete it is processed (and the grant settles on the server's answer)",
              any(c[1].endswith("/complete") and c[2]["succeeded"] for c in api_p.calls) and "B" not in bp.granted)
        bp.stop("test end")
        # a Wait on one module never holds another module's completion
        bw, api_w = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 41}})],
                                 ("POST", "/modules/MeshWeaver.Plugins/A/request"): [(200, {"verdict": "Wait", "step": {"seq": 1}})],
                                 ("PUT", "/modules/MeshWeaver.Plugins/B/outputs/"): put_,
                                 ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True})})
        with open(os.path.join(bw.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        bw.poll_once()
        with open(os.path.join(bw.io, "req", "A.json"), "w") as fh:
            json.dump({"module": "A"}, fh)
        os.makedirs(os.path.join(bw.io, "build", "B"), exist_ok=True)
        open(os.path.join(bw.io, "build", "B", "B.dll"), "wb").write(b"B")
        with open(os.path.join(bw.io, "done", "B.json"), "w") as fh:
            json.dump({"succeeded": True}, fh)
        bw.poll_once()
        check("modhub/a module waiting on its hub does not hold another module's completion",
              os.path.exists(os.path.join(bw.out, "ack", "B.json")) and "A" in bw.pending)
        bw.stop("test end")
        # teardown releases a grant the server never acknowledged
        bt, api_t2 = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 51}})],
                                  ("POST", "/modules/MeshWeaver.Plugins/B/complete"): [(0, "connection reset"), (200, {"accepted": True})]})
        with open(os.path.join(bt.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        bt.poll_once()
        with open(os.path.join(bt.io, "done", "B.json"), "w") as fh:
            json.dump({"succeeded": False, "error": "compile error"}, fh)
        bt.poll_once()
        check("modhub/a completion the server did not answer keeps the grant", "B" in bt.granted)
        bt.stop("the run ended")
        check("modhub/...and teardown releases it", "B" not in bt.granted
              and sum(1 for c in api_t2.calls if c[1].endswith("/complete")) == 2)
        check("modhub/the pull request is read from a string ref too", pull_request_number(["refs/pull/7/head"]) == 7
              and pull_request_number([{"kind": "PullRequest", "number": 9}]) == 9 and pull_request_number(["refs/heads/main"]) is None)
        check("modhub/the operations instance's claim answer names the kind by NUMBER (BuildRefKind.PullRequest = 1)",
              pull_request_number([{"kind": 1, "name": "probe/x", "number": 5107, "baseRef": "main"}]) == 5107
              and pull_request_number([{"kind": 0, "name": "main"}]) is None and pull_request_number([{"kind": True, "number": 3}]) is None
              and pull_request_number([{"kind": 1, "number": True}]) is None)
        removed = widen_module_hub(validate_module_hub({"moduleHub": {"modules": {"A": {"paths": ["src/A"], "options": ["opt-a", "opt-b"]}}}}),
                                   validate_module_hub({"moduleHub": {"modules": {"A": {"paths": ["src/A"], "options": ["opt-a"]}}}}))
        same = widen_module_hub(validate_module_hub({"moduleHub": {"modules": {"A": {"paths": ["src/A"], "options": ["opt-a", "opt-b"]}}}}),
                                validate_module_hub({"moduleHub": {"modules": {"A": {"paths": ["src/A"], "options": ["opt-a", "opt-b"]}}}}))
        check("modhub/a REMOVED option moves the key (the candidate's options enter it separately)",
              module_fingerprints(removed, lambda ps: "x", "tc")["A"] != module_fingerprints(same, lambda ps: "x", "tc")["A"])
        check("modhub/a TRUNK run asks on main with its merge order",
              [c[2] for c in api_h.calls if c[1].endswith("/request")][0].get("order") == 77
              and [c[2] for c in api_h.calls if c[1].endswith("/request")][0].get("lane") == "Main")
        br3, api_t = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Hit", "step": {"seq": 4, "outputHash": "sha256:" + out_hex}})],
                                  ("GET-BYTES", "/modules/MeshWeaver.Plugins/B/outputs/"): [(200, b"tampered")]})
        with open(os.path.join(br3.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        br3.poll_once()
        check("modhub/fetched bytes that do not hash to the step's output are refused",
              json.load(open(os.path.join(br3.out, "resp", "B.json")))["verdict"] == "Failed")
        with open(os.path.join(br3.io, "req", "Nope.json"), "w") as fh:
            json.dump({}, fh)
        br3.poll_once()
        check("modhub/a module the default branch does not declare is refused, never asked",
              json.load(open(os.path.join(br3.out, "resp", "Nope.json")))["verdict"] == "Refused"
              and not any("/Nope/" in c[1] for c in api_t.calls))
        with open(os.path.join(br3.io, "done", "B.json"), "w") as fh:
            json.dump({"succeeded": True}, fh)
        br3.poll_once()
        check("modhub/a completion without this host's grant is refused locally", json.load(open(os.path.join(br3.out, "ack", "B.json")))["accepted"] is False)
        brr, api_r = broker_with({("POST", "/modules/MeshWeaver.Plugins/B/request"): [(200, {"verdict": "Grant", "step": {"seq": 11}})],
                                  ("POST", "/modules/MeshWeaver.Plugins/B/complete"): lambda p_, b_: (200, {"accepted": True})})
        with open(os.path.join(brr.io, "req", "B.json"), "w") as fh:
            json.dump({"module": "B"}, fh)
        brr.poll_once()
        brr.release_open_grants("the run ended")
        rel = [c[2] for c in api_r.calls if c[1].endswith("/complete")]
        check("modhub/at the run's end every open grant is completed as FAILED (no lease waits to lapse)",
              rel and rel[0]["seq"] == 11 and rel[0]["succeeded"] is False and rel[0]["holder"] == brr.host, rel)

        # ── 5c. the ONE credential: AcrPull by workload identity, host-only ──────────────
        fed = os.path.join(tmp, "azure-identity-token")
        with open(fed, "w") as fh:
            fh.write("federated-jwt")
        posts: list[tuple] = []

        def fake_post(url, form):
            posts.append((url, dict(form)))
            if "login.microsoftonline.com" in url:
                return {"access_token": "aad-token"}
            return {"refresh_token": "acr-refresh"}
        wi = {"AZURE_FEDERATED_TOKEN_FILE": fed, "AZURE_CLIENT_ID": "cid", "AZURE_TENANT_ID": "tid"}
        cfg = acr_registry_config("cr.example.com", wi, fake_post, os.path.join(tmp, "auth"))
        check("wi/the federated token is the client assertion",
              posts[0][1].get("client_assertion") == "federated-jwt" and posts[0][0].endswith("/tid/oauth2/v2.0/token"), posts)
        check("wi/the Entra token is exchanged at the registry",
              posts[1][0] == "https://cr.example.com/oauth2/exchange" and posts[1][1]["access_token"] == "aad-token", posts)
        check("wi/the config carries the refresh token, mode 0600",
              json.load(open(cfg)) == {"auths": {"cr.example.com": {"identitytoken": "acr-refresh"}}}
              and (os.stat(cfg).st_mode & 0o777) == 0o600, cfg)
        check("wi/no projected token ⇒ anonymous", acr_registry_config("cr.example.com", {}, fake_post, tmp) is None)
        try:
            acr_registry_config("cr.example.com", wi, lambda u, f: {}, os.path.join(tmp, "auth2"))
            check("wi/a refused exchange is a Refusal", False, "accepted")
        except Refusal:
            pass
        check("wi/the federated token never reaches a sandbox",
              "AZURE_FEDERATED_TOKEN_FILE" not in sandbox_env(dict(wi, PATH="/bin"), {}, "/cache")
              and "AZURE_CLIENT_ID" not in sandbox_env(dict(wi, PATH="/bin"), {}, "/cache"))
        seen_cfg: list[str] = []

        def pull_with_cfg(cmd):
            if "--registry-config" in cmd:
                p_ = cmd[cmd.index("--registry-config") + 1]
                seen_cfg.append(p_)
                check("wi/the config exists during the pull", os.path.isfile(p_), p_)
            os.makedirs(cmd[4], exist_ok=True)
            return 0
        stage_plugins({"digest": "sha256:" + "9" * 64}, os.path.join(tmp, "wicache"),
                      dict(wi, MW_PLUGINS_REGISTRY="cr.example.com"), pull_with_cfg, fake_post)
        check("wi/the pull carries the config, and it is deleted after", seen_cfg and not os.path.exists(seen_cfg[0]), seen_cfg)

        # ── 5d. cache scoping: a stage is bound its own mirror + what it declares, never a root ──
        sc_env = {"MW_BUILD_CHANNEL": "latest", "MW_BUILD_PLATFORM_VERSION": "3.1.10412", "PATH": "/usr/bin:/bin",
                  "MW_BUILD_FRAMEWORK_IDENTITY": "c3e1", "MW_BUILD_CACHE": os.path.join(tmp, "sc-cache"), "MW_BUILD_WORK": tmp}
        hs_ = Host(sc_env, _FakeApi({}), log=lambda m: None, sandbox=Sandbox("/usr/bin/bwrap", ["/portal"]),
                   leftovers=lambda: [], sleep=lambda x: None)
        hs_.plugins_dir = os.path.join(tmp, "sc-cache", "plugin-sets", "k")
        ro = hs_.stage_readonly("MeshWeaver.Plugins", {"needs": []})
        check("scope/no needs: the repository's own mirror and the warm NuGet cache (every stage is handed it as its "
              "fallback folder, and a build's project.assets.json names it)",
              ro == [os.path.join(tmp, "sc-cache", "git", "MeshWeaver.Plugins.git"), os.path.join(tmp, "sc-cache", "nuget")], ro)
        check("scope/the fallback folder every stage is handed is one it is bound",
              sandbox_env({}, {}, os.path.join(tmp, "sc-cache"))["NUGET_FALLBACK_PACKAGES"] in ro)
        ro = hs_.stage_readonly("MeshWeaver.Plugins", {"needs": ["plugins", "outputs", "nuget"]})
        check("scope/declared needs are bound, repository-scoped",
              os.path.join(tmp, "sc-cache", "outputs", "MeshWeaver.Plugins") in ro and hs_.plugins_dir in ro
              and os.path.join(tmp, "sc-cache", "nuget") in ro, ro)
        check("scope/never the cache root, never another repository",
              os.path.join(tmp, "sc-cache") not in ro and not any("MeshWeaver.Education" in p for p in ro), ro)
        av = hs_.sandbox.argv("x", "/w/s", "/w/o", {"PATH": "/bin"}, readonly=ro, writable=["/w/a"])
        check("scope/the binds are read-only, the artifacts dir writable",
              all(f"--ro-bind-try {p} {p}" in " ".join(av) for p in ro) and "--bind /w/a /w/a" in " ".join(av)
              and f"--bind {os.path.join(tmp, 'sc-cache')}" not in " ".join(av), av)
        try:
            validate_stages({"stages": [{"name": "a", "kind": "build", "command": "t", "needs": ["everything"]}]})
            check("scope/an undeclarable need is refused", False, "accepted")
        except Refusal:
            pass
        scripts_seen: list[str] = []

        def sc_run(rc_, out_):
            def run(argv):
                scripts_seen.append(argv[-1])
                return rc_, out_
            return run
        scoping_check(hs_, sc_run(0, "scoped"))
        check("scope/the self-check runs in a sandbox bound like a public stage, reading a private entry",
              scripts_seen and ".boundary-private" in scripts_seen[0] and ".boundary-root-canary" in scripts_seen[0], scripts_seen[:1])
        for rc_, why in ((4, "PRIVATE repository's mirror"), (5, "compile outputs"), (6, "cache ROOT"), (3, "own mirror"), (1, "")):
            try:
                scoping_check(hs_, sc_run(rc_, "nope"))
                check(f"scope/self-check exit {rc_} refuses to start", False, "started")
            except Fatal as e:
                check(f"scope/the refusal names it ({rc_})", why in str(e), e)
        # the reference set is keyed by BOTH digests
        rs_src = os.path.join(tmp, "composed")
        os.makedirs(rs_src)
        open(os.path.join(rs_src, "mw-plugin-test.dll"), "w").close()
        rs_env = dict(sc_env, MW_BUILD_GATE_HOST=rs_src, MW_BUILD_TESTER_DIGEST="sha256:" + "1" * 64,
                      MW_BUILD_PORTAL_DIGEST="sha256:" + "2" * 64)
        hr = Host(rs_env, _FakeApi({}), log=lambda m: None, sandbox=Sandbox("bwrap", [rs_src]), leftovers=lambda: [])
        staged = hr.stage_refset()
        check("refset/keyed by both digests", refset_key("sha256:" + "1" * 64, "sha256:" + "2" * 64) in staged
              and refset_key("sha256:" + "1" * 64, "sha256:" + "2" * 64) != refset_key("sha256:" + "1" * 64, "sha256:" + "3" * 64)
              and refset_key("sha256:" + "1" * 64, "sha256:" + "2" * 64) != refset_key("sha256:" + "4" * 64, "sha256:" + "2" * 64), staged)
        check("refset/the gate host and the bind move to the staged copy",
              hr.env["MW_BUILD_GATE_HOST"] == staged and staged in hr.sandbox.readonly and rs_src not in hr.sandbox.readonly
              and os.path.isfile(os.path.join(staged, "mw-plugin-test.dll")), (hr.env["MW_BUILD_GATE_HOST"], hr.sandbox.readonly))
        try:
            Host(dict(rs_env, MW_BUILD_PORTAL_DIGEST=""), _FakeApi({}), log=lambda m: None, sandbox=Sandbox("bwrap", []),
                 leftovers=lambda: []).stage_refset()
            check("refset/one digest alone cannot key it", False, "keyed")
        except Fatal:
            pass

        # ── 6. the plugins layer is refreshed BEFORE the claim ─────────────────────────────
        order: list[str] = []
        d1 = "sha256:" + "1" * 64
        d2 = "sha256:" + "2" * 64
        inputs = [(200, {"plugins": {"digest": d1, "identity": "c3e1", "version": "1.0.1", "sha": "a" * 40}}),
                  (200, {"plugins": {"digest": d1, "identity": "c3e1", "version": "1.0.1", "sha": "a" * 40}}),
                  (200, {"plugins": {"digest": d2, "identity": "c3e1", "version": "1.0.2", "sha": "b" * 40}})]

        def on_inputs(path, body):
            order.append("inputs")
            return inputs.pop(0)

        def on_claim(path, body):
            order.append(f"claim:{body['pluginsDigest']}")
            return 204, None

        def pull(cmd):
            order.append(f"pull:{cmd[2].rsplit('@', 1)[1]}")
            os.makedirs(cmd[4], exist_ok=True)
            with open(os.path.join(cmd[4], "MeshWeaver.AI.zip"), "w") as fh:
                fh.write("x")
            return 0

        env = {"MW_BUILD_CHANNEL": "latest", "MW_BUILD_PLATFORM_VERSION": "3.1.10412", "PATH": "/usr/bin:/bin",
               "MW_BUILD_FRAMEWORK_IDENTITY": "c3e1", "MW_BUILD_CACHE": cache, "MW_BUILD_WORK": tmp,
               "MW_BUILD_READY_FILE": os.path.join(tmp, "ready"), "MW_PLUGINS_REGISTRY": "cr.example.com",
               "MW_BUILD_HOST": "build-latest-0"}

        # THE SOURCE FEED's stand-in — never the network. promote_nuget compares every package with the hash its feed
        # publishes (service index → registration leaf → catalog leaf); this answers from `feed_hashes`
        # ("id/version" → base64 SHA-512, or None for a catalog entry that publishes no hash) and 404s anything else.
        feed_hashes: dict = {}

        def stand_in_feed(url, timeout):
            if not (0 < timeout <= NUGET_FEED_TIMEOUT_SECONDS):
                raise ValueError(f"unbounded feed request ({timeout})")
            if url == "https://api.nuget.org/v3/index.json":
                return json.dumps({"resources": [{"@id": "https://feed.test/v3/search", "@type": "SearchQueryService"},
                                                 {"@id": "https://feed.test/reg/", "@type": "RegistrationsBaseUrl/3.6.0"}]}).encode()
            m_ = re.match(r"^https://feed\.test/(reg|cat)/([^/]+)/([^/]+)\.json$", url)
            key_ = f"{m_[2]}/{m_[3]}" if m_ else None
            if key_ not in feed_hashes:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            if m_[1] == "reg":
                return json.dumps({"@id": url, "catalogEntry": f"https://feed.test/cat/{m_[2]}/{m_[3]}.json"}).encode()
            leaf_ = {"id": m_[2].upper(), "version": m_[3]}
            if feed_hashes[key_] is not None:
                leaf_.update(packageHash=feed_hashes[key_], packageHashAlgorithm="SHA512")
            return json.dumps(leaf_).encode()

        def host(api, **kw):
            kw.setdefault("pull", pull)
            kw.setdefault("nuget_feeds", NugetFeeds(stand_in_feed))
            # a pull-request run merges onto a base: a fake git that resolves and merges cleanly
            kw.setdefault("git_runner", lambda args, env_: (0, "e" * 40 + "\n" if "rev-parse" in args else ""))
            return Host(env if "env" not in kw else kw.pop("env"), api, log=lambda s: None, sandbox=_EnvSandbox(),
                        leftovers=lambda: [], sleep=lambda s: None, **kw)

        # ── the deployment tree's WRITER and its READERS (§7.3b), on a real host loop over real git: nothing claimable → the host
        #    advances the deployment tree to main's tip IN PLACE and runs the TIP's declared warmBuild in the sandbox, once per
        #    tip; a run then overlays it, verified, or takes a cold worktree off the master — never a copy ─────────────────
        if shutil.which("git"):
            os.makedirs(os.path.join(seed, ".github"), exist_ok=True)
            with open(os.path.join(seed, ".github", "build-stages.json"), "w") as fh:
                json.dump({"stages": [], "warmBuild": {"command": "mkdir -p src/obj && echo built-by-tip > src/obj/warm.txt && pwd > src/obj/where.txt",
                                                       "timeoutMinutes": 5}}, fh)
            sh("git", "add", "-A"); sh("git", "commit", "-qm", "declare warmBuild")
            c3 = sh("git", "rev-parse", "HEAD")
            sh("git", "--git-dir", origin, "fetch", "-q", seed, "+main:main", cwd=wroot)
            # the RUNS keep the master fetched (with their token); the idle path only reads it
            sh("git", "--git-dir", mirror_w, "fetch", "-q", "--prune", "origin", cwd=wroot)
            idle_work = os.path.join(wroot, "idle-work")
            os.makedirs(idle_work)
            ienv = dict(env, MW_BUILD_WARM_WORKSPACE="true", MW_BUILD_RUN_OVERLAY="true", MW_BUILD_IN_NS="1", MW_BUILD_WORK=idle_work,
                        MW_BUILD_CACHE=wenv["MW_BUILD_CACHE"], MW_BUILD_GIT_BASE=wenv["MW_BUILD_GIT_BASE"])
            # the platform source is the fixture's (its .source-commit is on the local platform master): a runner image's own
            # prepared tree names a commit no local origin holds
            ienv["MW_PLATFORM_SOURCE"] = image_src
            # what a §7.3 host left on its cache: a copied generation (with a kernel overlay work dir, mode 000)
            legacy_gen = os.path.join(wenv["MW_BUILD_CACHE"], "shared", "R", "gens", "00000000000000000001-1")
            os.makedirs(os.path.join(legacy_gen, "w", "work"))
            open(os.path.join(legacy_gen, "a.cs"), "w").write("old")
            os.chmod(os.path.join(legacy_gen, "w", "work"), 0)
            hi = host(_FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})] * 3, ("POST", "/claim"): [(204, None)] * 3}),
                      env=ienv, git_runner=_git_run)
            idle_log: list = []
            hi.log = idle_log.append
            hi.warm_repos.add("R")
            hi.cycle()
            if hi.warm_thread:
                hi.warm_thread.join(60)
            idt = DeploymentTree(wenv["MW_BUILD_CACHE"], hi.deployment(), "R")
            st_i = idt.state() or {}
            check("warm-idle/the warm build sees its tree at the SAME in-sandbox path a run's stages see theirs",
                  _EnvSandbox.last_mount == SANDBOX_CHECKOUT, _EnvSandbox.last_mount)
            check("warm-idle/the warm build's package folder is bound at the SAME in-sandbox path a run's stages restore into",
                  _EnvSandbox.last_binds and _EnvSandbox.last_binds[0][1] == SANDBOX_NUGET, _EnvSandbox.last_binds)
            check("warm-idle/with nothing claimable the host builds main's tip with the TIP's warmBuild IN the deployment tree "
                  "(a worktree of the master), then writes its manifest",
                  st_i.get("tip") == c3 and st_i.get("built") == c3 and os.path.isfile(idt.manifest_path)
                  and open(os.path.join(idt.tree, "src", "obj", "warm.txt")).read().strip() == "built-by-tip"
                  and os.path.realpath(open(os.path.join(idt.tree, "src", "obj", "where.txt")).read().strip()) == os.path.realpath(idt.tree), st_i)
            check("warm-idle/the first idle pass REMOVES the §7.3 generations left on the cache (no 18 GiB beside the new trees)",
                  not os.path.exists(legacy_gen) and hi.legacy_retired
                  and any(m.startswith("legacy shared tree:") and "removed" in m for m in idle_log), (legacy_gen, idle_log[:3]))
            check("warm-idle/the ONE tree is the host's channel alone, never its build identity", os.path.basename(idt.root) == deployment_name(hi.channel)
                  and (idt.state() or {}).get("key") == warm_key(hi.env, hi.identity))
            hi.cycle()
            if hi.warm_thread:
                hi.warm_thread.join(60)
            check("warm-idle/a tip already built is not built again (and its manifest still verifies)",
                  verify_tree(idt.tree, idt.manifest_path, idt.ident(c3), sample=1000) is None)

            # a READER: the overlay, emulated (the overlay's VIEW as hard links — nothing copied — and the lower's .git hidden,
            # exactly as _overlay_tree hides it)
            mounted: list = []

            def link_mount(lower, dest):
                mounted.append((lower, dest))
                for d_, dirs_, files_ in os.walk(lower):
                    rel_ = os.path.relpath(d_, lower)
                    if rel_ == ".":
                        dirs_[:] = [x for x in dirs_ if x != ".git"]
                    os.makedirs(os.path.join(dest, rel_), exist_ok=True)
                    for f_ in files_:
                        if rel_ == "." and f_ == ".git":
                            continue
                        os.link(os.path.join(d_, f_), os.path.join(dest, rel_, f_))
            seen_log: list = []
            hi.log = seen_log.append
            hi.run_mount = link_mount
            hi.run_holds = contextlib.ExitStack()
            hi.cold_tree = None
            rd1 = os.path.join(idle_work, "run-ovl", "src")
            line1 = hi.seed_run_tree("R", c1, [], rd1, None, None)
            check("reader/a run VERIFIES and overlays the built deployment tree, moved to its own sha, saying so with bytes copied = 0",
                  line1.startswith(f"workspace: overlay of the deployment tree main@{c3[:12]} (built by the tip's warmBuild), moved to {c1[:12]}")
                  and line1.endswith("bytes copied = 0") and mounted == [(idt.tree, rd1)], line1)
            check("reader/the run's tree is exactly its commit, with main's obj/ carried for MSBuild",
                  open(os.path.join(rd1, "a.cs")).read() == "v1" and not os.path.exists(os.path.join(rd1, ".github"))
                  and open(os.path.join(rd1, "src", "obj", "warm.txt")).read().strip() == "built-by-tip")
            check("reader/the deployment tree beneath is untouched", open(os.path.join(idt.tree, "a.cs")).read() == "v2"
                  and os.path.isfile(os.path.join(idt.tree, ".github", "build-stages.json")))
            check("reader/the run's .git is made by git (init, alternates to the master) — nothing copied, not even the index",
                  os.path.isdir(os.path.join(rd1, ".git"))
                  and open(os.path.join(rd1, ".git", "objects", "info", "alternates")).read().strip() == os.path.join(mirror_w, "objects")
                  and not os.path.samefile(os.path.join(rd1, ".git", "index"), os.path.join(idt.admin(), "index")))
            with idt.writer() as w_held:
                check("reader/the run holds the tree for its WHOLE life: the writer cannot advance it meanwhile", w_held is None)
            hi.run_holds.close()
            with idt.writer() as w_free:
                check("reader/once the run released it, the writer can advance it again", w_free is not None)
            # integrity refused → COLD worktree, said
            time.sleep(0.05)
            open(os.path.join(idt.tree, "src", "obj", "warm.txt"), "a").write("POISON")
            hi.run_holds = contextlib.ExitStack()
            rd2 = os.path.join(idle_work, "run-integ", "src")
            line2 = hi.seed_run_tree("R", c1, [], rd2, None, None)
            check("reader/a tree that fails its integrity check is never mounted: a COLD worktree off the master, said",
                  line2.startswith(f"workspace: COLD worktree of the master at {c1[:12]}") and "integrity check refused" in line2
                  and line2.endswith("bytes copied = 0") and any(m.startswith("COLD WORKTREE") for m in seen_log)
                  and len(mounted) == 1 and open(os.path.join(rd2, "a.cs")).read() == "v1"
                  and hi.cold_tree is None and wt_count(rd2) == 0, (line2, seen_log[-2:]))
            # the cold tree has its OWN .git: a ref the run writes lands there, never in the master (read-only to a stage)
            mref = subprocess.run(["git", "-C", rd2, "update-ref", "refs/remotes/origin/mw-host-modules-base", c1], capture_output=True, text=True)
            check("reader/the cold tree is its OWN repository (objects borrowed from the master): HEAD at the run's sha, a ref "
                  "a stage writes lands in it and never in the master, nothing registered on the master",
                  mref.returncode == 0 and os.path.isdir(os.path.join(rd2, ".git"))
                  and subprocess.run(["git", "-C", rd2, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() == c1
                  and subprocess.run(["git", "--git-dir", master_path(wenv["MW_BUILD_CACHE"], "R"), "rev-parse", "--verify", "-q",
                                      "refs/remotes/origin/mw-host-modules-base"], capture_output=True).returncode != 0
                  and "alternates" in os.listdir(os.path.join(rd2, ".git", "objects", "info")), mref.stderr)
            shutil.rmtree(rd2, ignore_errors=True)
            hi.run_holds.close()
            check("reader/the cold tree leaves nothing registered on the master", wt_count(rd2) == 0)
            mcfg = lambda k_: subprocess.run(["git", "--git-dir", master_path(wenv["MW_BUILD_CACHE"], "R"), "config", "--get", k_],
                                             capture_output=True, text=True).stdout.strip()
            check("reader/a master is never auto-gc'd (runs borrow its objects through alternates with no ref it knows)",
                  mcfg("gc.auto") == "0" and mcfg("maintenance.auto") == "false", (mcfg("gc.auto"), mcfg("maintenance.auto")))
            # the writer rebuilds it (not built → advance in place → warmBuild → manifest)
            idt.unbuild(warm_key(hi.env, hi.identity), c3)
            check("warm-idle/a tree marked not built is rebuilt by the writer", hi._warm_deployment("R", warm_key(hi.env, hi.identity)) == "built"
                  and verify_tree(idt.tree, idt.manifest_path, idt.ident(c3), sample=1000) is None)
            # lock busy → COLD worktree, said
            hi.run_holds = contextlib.ExitStack()
            hi.cold_tree = None
            rd3 = os.path.join(idle_work, "run-busy", "src")
            with idt.writer() as w_busy:
                line3 = hi.seed_run_tree("R", c1, [], rd3, None, None)
            check("reader/a tree its writer is advancing (lock busy) is never mounted: a COLD worktree, said",
                  line3.startswith("workspace: COLD worktree") and "locked" in line3 and len(mounted) == 1 and hi.cold_tree is None, line3)
            shutil.rmtree(rd3, ignore_errors=True)
            hi.run_holds.close()
            # a host that cannot overlay → COLD worktree, said
            hi.env.pop("MW_BUILD_IN_NS")
            hi.run_holds = contextlib.ExitStack()
            rd4 = os.path.join(idle_work, "run-noovl", "src")
            line4 = hi.seed_run_tree("R", c1, [], rd4, None, None)
            check("reader/a host that cannot overlay takes a COLD worktree off the master, said (never a copy)",
                  line4.startswith("workspace: COLD worktree") and "cannot overlay" in line4 and len(mounted) == 1, line4)
            shutil.rmtree(rd4, ignore_errors=True)
            hi.run_holds.close()
            hi.env["MW_BUILD_IN_NS"] = "1"
            # an overlay refused by the kernel → COLD worktree, said, and the shared lock released at once
            hi.run_holds = contextlib.ExitStack()
            hi.run_mount = lambda lo, de: (_ for _ in ()).throw(Refusal("the overlay mount was refused (permission denied)"))
            rd5 = os.path.join(idle_work, "run-refused", "src")
            line5 = hi.seed_run_tree("R", c1, [], rd5, None, None)
            with idt.writer() as w_after:
                check("reader/a refused overlay falls back to a COLD worktree, said, and holds no lock on the tree",
                      line5.startswith("workspace: COLD worktree") and "permission denied" in line5 and w_after is not None, line5)
            remove_cold_worktree(wenv["MW_BUILD_CACHE"], "R", rd5, gw)
            hi.run_holds.close()
            hi.run_mount = link_mount

            # a SECOND host on the same cache cannot write the tree while the first holds it (one writer per deployment)
            hb = host(_FakeApi({}), env=dict(ienv), git_runner=_git_run)
            hb_builds: list = []
            hb._sandboxed_capture = lambda *a_, **k_: (hb_builds.append(1), (0, ""))[1]
            idt.unbuild(warm_key(hb.env, hb.identity), c3)
            with idt.writer() as w_a:
                out_b = hb._warm_deployment("R", warm_key(hb.env, hb.identity))
            check("warm-idle/another host is NOT a writer while one holds the tree: it neither builds nor waits",
                  out_b == "skipped" and hb_builds == [], (out_b, hb_builds))
            check("warm-idle/once free, that host is the writer", hb._warm_deployment("R", warm_key(hb.env, hb.identity)) == "built" and hb_builds == [1])

            # an advancement that FAILS leaves the tree NOT BUILT and the tip cooled down, never marked handled
            fail_work = os.path.join(wroot, "idle-fail")
            os.makedirs(fail_work)
            hf = host(_FakeApi({}), env=dict(ienv, MW_BUILD_WORK=fail_work, MW_BUILD_CHANNEL="failing"), git_runner=_git_run)
            hf.warm_repos.add("R")
            adv_calls: list = []
            real_adv = globals()["advance_deployment_tree"]

            def failing_adv(dt_, tip_, key_, git_):
                adv_calls.append(tip_)
                dt_.unbuild(key_, tip_)
                raise Refusal("git worktree add failed (exit 128)")
            globals()["advance_deployment_tree"] = failing_adv
            fdt = DeploymentTree(wenv["MW_BUILD_CACHE"], hf.deployment(), "R")
            try:
                hf.warm_main_when_idle()
                check("warm-idle/a FAILED advancement leaves the tree not built and the tip cooled down",
                      (fdt.state() or {}).get("built") is None and len(adv_calls) == 1 and hf.warm_failed.get("R", ("",))[0] == c3,
                      (fdt.state(), adv_calls))
                hf.warm_main_when_idle()
                check("warm-idle/the failed tip is retried after a pause, not on every idle pass", len(adv_calls) == 1, adv_calls)
                hf.warm_failed["R"] = (c3, warm_key(hf.env, hf.identity), time.monotonic() - WARM_RETRY_SECONDS - 1)
                hf.warm_main_when_idle()
                check("warm-idle/after the pause the failed tip is tried again", len(adv_calls) == 2, adv_calls)
            finally:
                globals()["advance_deployment_tree"] = real_adv
            # a failing warmBuild (advancement fine): not built (readers go cold), cooled down, not rebuilt every pass
            hf.warm_failed.clear()
            builds: list = []
            hf._sandboxed_capture = lambda *a_, **k_: (builds.append(1), (1, "boom"))[1]
            hf.warm_main_when_idle()
            hf.warm_main_when_idle()
            with fdt.reader(warm_key(hf.env, hf.identity)) as r_f:
                check("warm-idle/a FAILED warmBuild leaves the tree NOT BUILT (readers go cold), and is not rebuilt on the next pass",
                      r_f is None and "not built" in (fdt.last_refusal or "") and len(builds) == 1
                      and hf.warm_failed.get("R", ("",))[0] == c3 and not os.path.exists(fdt.manifest_path), (builds, fdt.last_refusal))
            hf._sandboxed_capture = lambda *a_, **k_: (builds.append(1), (0, ""))[1]
            hf.warm_failed["R"] = (c3, warm_key(hf.env, hf.identity), time.monotonic() - WARM_RETRY_SECONDS - 1)
            hf.warm_main_when_idle()
            check("warm-idle/a successful build after the pause clears the failure",
                  len(builds) == 2 and "R" not in hf.warm_failed and (fdt.state() or {}).get("built") == c3, (builds, hf.warm_failed))
            # the warm build's platform source: a writable VIEW (here a cold worktree of the platform's master — this host
            # cannot overlay), bound at the PREPARED path and named by it — the path a run's stages see; never a copy
            seen_b: list = []
            hf.env["MW_PLATFORM_SOURCE"] = image_src
            hf.env.pop("MW_BUILD_IN_NS")
            hf.warm_failed.clear()
            fdt.unbuild(warm_key(hf.env, hf.identity), c3)

            def capture_view(*a_, **k_):
                binds_ = list(k_.get("binds") or [])
                seen_b.append((dict(a_[4]), binds_, [os.path.isfile(os.path.join(b_[0], "Directory.Build.props")) for b_ in binds_]))
                return 0, ""
            hf._sandboxed_capture = capture_view
            hf.warm_main_when_idle()
            check("warm-idle/the deployment's PLATFORM TREE is bound at the PREPARED path and named by it, populated while the "
                  "build runs — core's worktree at the set's commit, under the deployment, never a copy",
                  seen_b and seen_b[0][0].get("MW_PLATFORM_SOURCE") == image_src
                  and any(b[1] == image_src and b[0] == fdt.platform and ok_ for b, ok_ in zip(seen_b[0][1], seen_b[0][2]))
                  and any(b[1] == SANDBOX_NUGET for b in seen_b[0][1]), seen_b)
            psc = source_commit(image_src)
            check("warm-idle/...and it is KEPT for the stages: a registered worktree of the platform master at the image's "
                  ".source-commit, named in the state and covered by its own integrity manifest",
                  psc is not None and os.path.isdir(fdt.platform)
                  and wt_count(fdt.platform, master_path(wenv["MW_BUILD_CACHE"], "MeshWeaver")) == 1
                  and (fdt.state() or {}).get("platform") == psc and os.path.isfile(fdt.platform_manifest_path)
                  and open(os.path.join(fdt.platform, ".source-commit")).read().strip() == psc,
                  (psc, fdt.state(), os.path.exists(fdt.platform_manifest_path)))
            with fdt.reader(warm_key(hf.env, hf.identity)) as r_p:
                check("warm-idle/a reader of the built tree is handed the verified platform tree as its stages' lower",
                      r_p is not None and fdt.platform_lower == fdt.platform, (r_p, fdt.platform_lower, fdt.last_refusal))
            # a tampered platform tree: the reader still holds the repo tree, but the stages fall back to the image's tree
            with open(os.path.join(fdt.platform, "Directory.Build.props"), "a") as fh:
                fh.write("<!-- planted -->")
            with fdt.reader(warm_key(hf.env, hf.identity)) as r_t2:
                check("warm-idle/a platform tree failing its integrity check is NOT handed to the stages (the image's tree is), "
                      "and the repo tree is still read",
                      r_t2 is not None and fdt.platform_lower is None and "platform tree" in (fdt.last_refusal or ""),
                      (r_t2, fdt.platform_lower, fdt.last_refusal))
            # the next warm build moves the platform tree IN PLACE (no second worktree, obj/ kept)
            os.makedirs(os.path.join(fdt.platform, "src", "obj"), exist_ok=True)
            open(os.path.join(fdt.platform, "src", "obj", "kept.txt"), "w").write("warm")
            fdt.unbuild(warm_key(hf.env, hf.identity), c3)
            seen_b.clear()
            hf.warm_main_when_idle()
            check("warm-idle/a second warm build moves the platform tree in place: still ONE registered worktree, obj/ kept, the "
                  "planted change reset",
                  seen_b and wt_count(fdt.platform, master_path(wenv["MW_BUILD_CACHE"], "MeshWeaver")) == 1
                  and os.path.isfile(os.path.join(fdt.platform, "src", "obj", "kept.txt"))
                  and "planted" not in open(os.path.join(fdt.platform, "Directory.Build.props")).read(), seen_b)

            # a LINK where the platform tree belongs (planted, or left by a crash) is removed, never followed nor fatal
            shutil.rmtree(fdt.platform)
            os.remove(os.path.join(fdt.host, "platform-admin"))
            os.symlink(wroot, fdt.platform)
            fdt.unbuild(warm_key(hf.env, hf.identity), c3)
            seen_b.clear()
            hf.warm_main_when_idle()
            check("warm-idle/a symlink at the platform tree's path is removed (not followed) and the tree is re-added",
                  seen_b and not os.path.islink(fdt.platform) and os.path.isdir(fdt.platform) and os.path.isdir(wroot)
                  and any(b[0] == fdt.platform for b in seen_b[0][1]), seen_b)

            # STALE DEPLOYMENTS: a set change gives a new key; the lane's other keys are retired once unused and quiet
            rc_ = os.path.join(wroot, "retire-cache"); rdep = os.path.join(rc_, DEPLOY_DIR)
            for nm in ("mac-currentcurrent00", "mac-0123456789abcdef", "mac-youngyoungyoung1", "mac-busybusybusybusy", "latest-oldoldoldoldold1", "mac-oldish-but-not-a-key"):
                os.makedirs(os.path.join(rdep, nm, ".locks"), exist_ok=True)
                open(os.path.join(rdep, nm, ".locks", "R.lock"), "w").close()
            # a REAL worktree of a master inside the stale deployment: it must be deregistered, not left as a dangling entry
            rmirror = master_path(rc_, "R"); os.makedirs(os.path.dirname(rmirror), exist_ok=True)
            sh("git", "clone", "-q", "--bare", seed, rmirror, cwd=wroot)
            sh("git", "--git-dir", rmirror, "worktree", "add", "--detach", "-q", os.path.join(rdep, "mac-0123456789abcdef", "R"), "HEAD", cwd=wroot)
            pmirror_r = master_path(rc_, PLATFORM_REPO)
            sh("git", "clone", "-q", "--bare", seed, pmirror_r, cwd=wroot)
            sh("git", "--git-dir", pmirror_r, "worktree", "add", "--detach", "-q", os.path.join(rdep, "mac-0123456789abcdef", ".platform", "R"), "HEAD", cwd=wroot)
            old_t = time.time() - STALE_DEPLOYMENT_SECONDS - 60
            for nm in ("mac-0123456789abcdef", "mac-busybusybusybusy", "latest-oldoldoldoldold1", "mac-oldish-but-not-a-key"):
                for d_, ds_, fs_ in os.walk(os.path.join(rdep, nm)):
                    for x_ in ds_ + fs_ + [""]:
                        try:
                            os.utime(os.path.join(d_, x_) if x_ else d_, (old_t, old_t), follow_symlinks=False)
                        except OSError:
                            pass
            import fcntl as _fcntl
            busy_fh = open(os.path.join(rdep, "mac-busybusybusybusy", ".locks", "R.lock"))
            _fcntl.flock(busy_fh.fileno(), _fcntl.LOCK_SH)         # a run still reading the old key's tree
            r_log: list = []
            gone_ = retire_legacy_deployments(rc_, "mac", "mac-currentcurrent00", r_log.append,
                                             lambda *a_: subprocess.run(["git", *a_], check=True, capture_output=True))
            busy_fh.close()
            check("retire/the lane's OTHER key, unused and quiet, is retired — directory gone and its worktree deregistered",
                  gone_ == [os.path.join(rdep, "mac-0123456789abcdef")] and not os.path.exists(os.path.join(rdep, "mac-0123456789abcdef"))
                  and "mac-0123456789abcdef" not in subprocess.run(["git", "--git-dir", rmirror, "worktree", "list"], capture_output=True, text=True).stdout,
                  (gone_, r_log))
            check("retire/the CURRENT key, a recently touched old key (a draining replica), a key whose lock is held (a run "
                  "reading it) and another lane's deployment are all kept",
                  all(os.path.isdir(os.path.join(rdep, n_)) for n_ in ("mac-currentcurrent00", "mac-youngyoungyoung1", "mac-busybusybusybusy", "latest-oldoldoldoldold1"))
                  and any("still in use" in m for m in r_log), (os.listdir(rdep), r_log))
            check("retire/the stale deployment's PLATFORM tree is deregistered from the platform master too",
                  "0123456789abcdef" not in subprocess.run(["git", "--git-dir", pmirror_r, "worktree", "list"], capture_output=True, text=True).stdout)
            check("retire/a directory that is not exactly `<lane>-<16-char key>` is never touched (a longer lane sharing the prefix)",
                  os.path.isdir(os.path.join(rdep, "mac-oldish-but-not-a-key")))
            check("retire/the log names the MEASURED age, not the threshold",
                  any("retired (untouched " in m and f"untouched {int(STALE_DEPLOYMENT_SECONDS)} s" not in m for m in r_log), r_log)
            check("retire/a cache with no deploy directory is no error", retire_legacy_deployments(os.path.join(wroot, "nope"), "mac", "x", log0, lambda *a_: None) == [])

            # a run STAGE's platform view (§7.3b step 2): its own, bound AT the MW_PLATFORM_SOURCE path, lower = the warm platform
            # tree the run holds, else the image's tree; a view that cannot be made binds nothing (the stage sees read-only)
            real_psv = globals()["platform_source_view"]
            psv_calls: list = []

            @contextlib.contextmanager
            def fake_psv(lower_, dest_, env_, log_, **kw_):
                psv_calls.append((lower_, dest_))
                os.makedirs(dest_, exist_ok=True)
                yield dest_
                psv_calls.append(("released", dest_))

            globals()["platform_source_view"] = fake_psv
            try:
                swd = os.path.join(wroot, "stage-views")
                with contextlib.ExitStack() as sv:
                    hf.run_platform_lower = None
                    check("stage-view/no platform source named: nothing bound",
                          hf.stage_platform_binds({}, swd, "gate", sv) == [] and psv_calls == [], psv_calls)
                    b_cold = hf.stage_platform_binds({"MW_PLATFORM_SOURCE": image_src}, swd, "gate", sv)
                    check("stage-view/no warm platform tree: a view of the IMAGE's tree, bound at the image path, per stage",
                          b_cold == [(os.path.join(swd, "platform", "gate"), image_src)] and psv_calls[-1][0] == image_src, (b_cold, psv_calls))
                    hf.run_platform_lower = fdt.platform
                    b_warm = hf.stage_platform_binds({"MW_PLATFORM_SOURCE": image_src}, swd, "portal-hosts", sv)
                    check("stage-view/the run holds the warm platform tree: it is the view's LOWER, still bound at the image path",
                          b_warm == [(os.path.join(swd, "platform", "portal-hosts"), image_src)] and psv_calls[-1][0] == fdt.platform,
                          (b_warm, psv_calls))
                check("stage-view/each view is released when its stage ends",
                      [c for c in psv_calls if c[0] == "released"] and len([c for c in psv_calls if c[0] == "released"]) == 2, psv_calls)

                @contextlib.contextmanager
                def refusing_psv(*a_, **k_):
                    raise Refusal("no overlay and no .source-commit")
                    yield  # pragma: no cover
                globals()["platform_source_view"] = refusing_psv
                said: list = []
                real_log = hf.log
                hf.log = said.append
                with contextlib.ExitStack() as sv2:
                    b_none = hf.stage_platform_binds({"MW_PLATFORM_SOURCE": image_src}, swd, "modules", sv2)
                hf.log = real_log
                check("stage-view/a view that cannot be made binds NOTHING and says so (never a copy, never a host defect)",
                      b_none == [] and any("no writable platform view" in m for m in said), (b_none, said))
            finally:
                globals()["platform_source_view"] = real_psv
                hf.run_platform_lower = None

            # a claim STOPS a warm build in flight (its sandbox killed) and leaves the tree NOT BUILT — never waits it out
            with open(os.path.join(seed, ".github", "build-stages.json"), "w") as fh:
                json.dump({"stages": [], "warmBuild": {"command": "sleep 30", "timeoutMinutes": 5}}, fh)
            sh("git", "commit", "-qam", "slow warmBuild")
            sh("git", "--git-dir", origin, "fetch", "-q", seed, "+main:main", cwd=wroot)
            sh("git", "--git-dir", mirror_w, "fetch", "-q", "--prune", "origin", cwd=wroot)
            hi.cycle()
            started_w = time.monotonic()
            for _ in range(200):
                if (idt.state() or {}).get("built") == "building":
                    break
                time.sleep(0.05)
            hi.stop_warming()
            with idt.reader(warm_key(hi.env, hi.identity)) as r_s:
                check("warm-idle/a claim stops a warm build in flight at once; the half-built tree is NOT BUILT, never read",
                      time.monotonic() - started_w < 20 and not (hi.warm_thread and hi.warm_thread.is_alive())
                      and (idt.state() or {}).get("built") is None and r_s is None,
                      (time.monotonic() - started_w, idt.state()))
            # restore a quick warmBuild for what follows
            with open(os.path.join(seed, ".github", "build-stages.json"), "w") as fh:
                json.dump({"stages": [{"name": "s", "kind": "build", "command": "true"}],
                           "warmBuild": {"command": "mkdir -p src/obj && echo built-by-tip > src/obj/warm.txt", "timeoutMinutes": 5}}, fh)
            sh("git", "commit", "-qam", "quick warmBuild, one stage")
            c5 = sh("git", "rev-parse", "HEAD")
            sh("git", "--git-dir", origin, "fetch", "-q", seed, "+main:main", cwd=wroot)
            sh("git", "--git-dir", mirror_w, "fetch", "-q", "--prune", "origin", cwd=wroot)
            hook_repo = os.path.join(idle_work, "hooked")
            sh("git", "clone", "-q", seed, hook_repo, cwd=wroot)
            with open(os.path.join(hook_repo, ".git", "hooks", "post-checkout"), "w") as fh:
                fh.write(f"#!/bin/sh\ntouch {os.path.join(wroot, 'merge-hook-ran')}\n")
            os.chmod(os.path.join(hook_repo, ".git", "hooks", "post-checkout"), 0o755)
            _git_run(["-C", hook_repo, "checkout", "-q", "--detach", c1], {"PATH": genv_g["PATH"], "HOME": wroot})
            check("warm/no hook in a run tree's .git runs on ANY host git step (the merge included)",
                  not os.path.exists(os.path.join(wroot, "merge-hook-ran")))
            stuck_release = threading.Event()
            hs = host(_FakeApi({}), env=dict(env, MW_BUILD_WARM_WORKSPACE="true"))
            hs.warm_stop_seconds = 0.2
            hs.warm_thread = threading.Thread(target=lambda: stuck_release.wait(30), daemon=True)
            hs.warm_thread.start()
            try:
                hs.stop_warming()
                check("warm-idle/a warm worker that does not stop is FATAL (fail-closed), never handed to a run", False, "returned")
            except Fatal as e:
                check("warm-idle/a warm worker that does not stop is FATAL and keeps the cancel set",
                      "did not stop" in str(e) and hs.warm_cancel.is_set(), str(e))
            finally:
                stuck_release.set()

        h = host(_FakeApi({("GET", "/hosts/inputs"): on_inputs, ("POST", "/claim"): on_claim}))
        h.preflight()
        for i in range(3):
            check(f"order/cycle {i} idle", h.cycle() == "idle")
        check("order/refresh before claim, pull only on a new digest", order == [
            "inputs", f"pull:{d1}", f"claim:{d1}",
            "inputs", f"claim:{d1}",
            "inputs", f"pull:{d2}", f"claim:{d2}"], order)
        check("order/content-addressed", os.path.isfile(os.path.join(cache, "plugins", "2" * 64, ".complete")))
        check("order/readiness written once staged", os.path.isfile(os.path.join(tmp, "ready")))
        api2 = _FakeApi({("GET", "/hosts/inputs"): [(503, None)], ("POST", "/claim"): on_claim})
        check("order/unreadable inputs → no claim", host(api2).cycle() == "no-inputs"
              and not any(c[1] == "/claim" for c in api2.calls), api2.calls)
        api3 = _FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": {"digest": "sha256:" + "3" * 64}})],
                         ("POST", "/claim"): on_claim})
        check("order/failed pull → no claim", host(api3, pull=lambda cmd: 1).cycle() == "no-inputs"
              and not any(c[1] == "/claim" for c in api3.calls), api3.calls)
        api4 = _FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})], ("POST", "/claim"): on_claim})
        check("order/no publication registered → claim with null", host(api4).cycle() == "idle"
              and api4.calls[-1][2]["pluginsDigest"] is None, api4.calls)

        # ── 6b. H8: the plugins layer from the registry's bundle index ─────────────────────
        kf = os.path.join(tmp, "mwi")
        with open(kf, "w") as fh:
            fh.write("mwi_secret\n")
        idx = {"frameworkMvid": "c3e1", "bundles": [
            {"plugin": "Store", "version": "1.4.2", "bundleHash": "aa"}, {"plugin": "AI", "version": "1.36.5"}]}
        fetched: list[list[str]] = []

        def fetch_runner(argv, env_):
            fetched.append(argv)
            check("h8/the instance key reaches module-fetch through the ENVIRONMENT only",
                  "mwi_secret" not in " ".join(argv) and env_.get("MW_REGISTRY_INSTANCE_KEY") == "mwi_secret"
                  and argv[argv.index("--key-env") + 1] == "MW_REGISTRY_INSTANCE_KEY", argv)
            out = argv[argv.index("--out") + 1]
            os.makedirs(os.path.join(out, argv[argv.index("module-fetch") + 1]))
            return 0
        claims_seen: list[dict] = []
        h8env = dict(env, MW_REGISTRY_URL="https://portal.example.com", MW_REGISTRY_KEY_FILE=kf,
                     MW_BUILD_CACHE=os.path.join(tmp, "h8cache"))
        h8 = host(_FakeApi({("POST", "/claim"): lambda p_, b: (claims_seen.append(b), (204, None))[1]}), env=h8env,
                  registry_get=lambda u, k: (200, idx) if u.endswith("/api/plugins/bundles/index.json") and k == "mwi_secret" else (401, None),
                  fetch_runner=fetch_runner)
        check("h8/a cycle stages the layer and claims", h8.cycle() == "idle")
        want_key = plugins_key_of(["AI@1.36.5", "Store@1.4.2@aa"])
        check("h8/the key is the digest of the sorted package@version[@hash] list, presented as pluginsKey",
              claims_seen and claims_seen[-1]["pluginsKey"] == want_key, claims_seen[-1:])
        check("h8/every bundle fetched once", sorted(a[a.index("module-fetch") + 1] for a in fetched) == ["AI", "Store"], fetched)
        check("h8/the set links every package", sorted(os.listdir(h8.plugins_dir)) == [".complete", "AI", "Store"], h8.plugins_dir)
        fetched.clear()
        h8.cycle()
        check("h8/an unchanged index fetches nothing", fetched == [], fetched)
        idx["bundles"][1] = {"plugin": "AI", "version": "1.36.6"}
        h8.cycle()
        check("h8/a changed bundle is the only fetch", [a[a.index("module-fetch") + 1] for a in fetched] == ["AI"], fetched)
        check("h8/the new key is presented", claims_seen[-1]["pluginsKey"] == plugins_key_of(["AI@1.36.6", "Store@1.4.2@aa"]))
        n_claims = len(claims_seen)
        idx["bundles"].append({"plugin": "Edu", "version": "2.0.0"})
        h8.fetch_runner = lambda argv, env_: 3
        check("h8/a failed module-fetch means no claim", h8.cycle() == "no-inputs" and len(claims_seen) == n_claims)
        h8w = host(_FakeApi({("POST", "/claim"): lambda p_, b: (claims_seen.append(b), (204, None))[1]}), env=h8env,
                   registry_get=lambda u, k: (200, dict(idx, frameworkMvid="c9e9")), fetch_runner=fetch_runner)
        check("h8/an index for another framework identity means no claim", h8w.cycle() == "no-inputs")
        h8m = host(_FakeApi({("POST", "/claim"): lambda p_, b: (204, None)}), env=h8env,
                   registry_get=lambda u, k: (200, {"bundles": idx["bundles"]}), fetch_runner=fetch_runner)
        check("h8/an index naming NO framework means no claim", h8m.cycle() == "no-inputs")
        h8k = host(_FakeApi({}), env=dict(h8env, MW_REGISTRY_KEY_FILE=os.path.join(tmp, "absent")), fetch_runner=fetch_runner)
        check("h8/no instance key means no claim", h8k.cycle() == "no-inputs")
        for bad in ({}, {"bundles": [{"plugin": "AI"}]}, {"bundles": [{"plugin": "../x", "version": "1"}]}, {"bundles": ["AI"]}):
            try:
                bundle_lines(bad)
                check(f"h8/refuses the index {bad}", False, "keyed")
            except Refusal:
                pass
        check("h8/the instance key never reaches a sandbox",
              not any("REGISTRY" in k for k in sandbox_env(dict(h8env, MW_REGISTRY_INSTANCE_KEY="x"), {}, "/cache")))

        # ── 6c. the plugins layer follows the module HAND-OVER signal, and stays installed ─────
        sig = {"key": "sha256:" + "1" * 64}
        index_reads: list[str] = []
        idx2 = {"frameworkMvid": "c3e1", "bundles": [{"plugin": "AI", "version": "1.40.1"}, {"plugin": "Store", "version": "1.36.20"}]}

        def reg_get(u, k):
            index_reads.append(u)
            return (200, json.loads(json.dumps(idx2)))
        fetched.clear()
        hoenv = dict(h8env, MW_BUILD_CACHE=os.path.join(tmp, "hocache"))
        claims2: list[dict] = []
        ho_api = _FakeApi({("GET", "/hosts/inputs"): lambda p_, b: (200, {"plugins": None, "platformVersion": "3.1.10412", "modules": dict(sig)}),
                           ("POST", "/claim"): lambda p_, b: (claims2.append(b), (204, None))[1]})
        ho = host(ho_api, env=hoenv, registry_get=reg_get, fetch_runner=fetch_runner)
        check("handover/first turn installs the layer", ho.cycle() == "idle" and len(index_reads) == 1
              and sorted(a[a.index("module-fetch") + 1] for a in fetched) == ["AI", "Store"], (index_reads, fetched))
        for _ in range(3):
            ho.cycle()
        check("handover/IDLE turns with an unchanged signal read NO index and fetch nothing", len(index_reads) == 1 and len(fetched) == 2, index_reads)
        check("handover/idle turns still claim on the installed layer", claims2[-1]["pluginsKey"] == plugins_key_of(["AI@1.40.1", "Store@1.36.20"]), claims2[-1:])
        # A module is handed over: the registry now serves AI 1.40.2 and the signal moved — the IDLE host installs it on
        # its next turn, before any run is claimed on it.
        idx2["bundles"][0] = {"plugin": "AI", "version": "1.40.2"}
        sig["key"] = "sha256:" + "2" * 64
        fetched.clear()
        ho.cycle()
        check("handover/a moved signal reads the index once and fetches ONLY the handed-over module",
              len(index_reads) == 2 and [a[a.index("module-fetch") + 1] for a in fetched] == ["AI"], (index_reads, fetched))
        check("handover/the next claim presents the new layer", claims2[-1]["pluginsKey"] == plugins_key_of(["AI@1.40.2", "Store@1.36.20"]))
        # Negative control: the registry moved but the signal did NOT — the host must not have read it (proves the
        # skip is the signal's doing, not an accident of the fake).
        idx2["bundles"][1] = {"plugin": "Store", "version": "1.36.21"}
        ho.cycle()
        check("handover/negative control: no signal move, no index read", len(index_reads) == 2, index_reads)
        # Persistent install: a NEW host on the same cache (a restart, a new replica) starts installed — no fetch, no
        # index read while the signal is the one the layer was staged at.
        fetched.clear()
        ho2 = host(_FakeApi({("GET", "/hosts/inputs"): lambda p_, b: (200, {"modules": dict(sig)}),
                             ("POST", "/claim"): lambda p_, b: (claims2.append(b), (204, None))[1]}),
                   env=hoenv, registry_get=reg_get, fetch_runner=fetch_runner)
        check("handover/the next host finds the layer installed (no index read, no fetch)",
              ho2.cycle() == "idle" and len(index_reads) == 2 and fetched == []
              and claims2[-1]["pluginsKey"] == plugins_key_of(["AI@1.40.2", "Store@1.36.20"]), (index_reads, fetched))
        # Install-on-miss: the layer was evicted from the cache — reinstalled whatever the signal says.
        evicted = os.path.join(hoenv["MW_BUILD_CACHE"], "bundles", hashlib.sha256(b"AI@1.40.2").hexdigest()[:32])
        check("handover/the fixture names the installed AI bundle", os.path.isfile(os.path.join(evicted, ".complete")), evicted)
        shutil.rmtree(evicted)
        ho2.cycle()
        check("handover/install-on-miss: a layer no longer intact is installed again from the index", len(index_reads) == 3, index_reads)
        # The layer must be intact PER PLUGIN, not per marker: each damage below leaves every `.complete` it can in place.
        hc = hoenv["MW_BUILD_CACHE"]
        check("handover/the reinstalled layer is intact", layer_intact(ho2.plugins_dir, hc), ho2.plugins_dir)
        os.unlink(os.path.join(ho2.plugins_dir, "Store"))
        check("handover/a set missing a package LINK is not intact", not layer_intact(ho2.plugins_dir, hc))
        ho2.cycle()
        check("handover/a removed package link is repaired (index read, link back)", len(index_reads) == 4
              and layer_intact(ho2.plugins_dir, hc) and os.path.islink(os.path.join(ho2.plugins_dir, "Store")), index_reads)
        store_bundle = os.path.dirname(os.path.realpath(os.path.join(ho2.plugins_dir, "Store")))
        shutil.rmtree(os.path.join(store_bundle, "Store"))
        check("handover/a bundle whose package directory is gone (its .complete kept) is not intact",
              os.path.isfile(os.path.join(store_bundle, ".complete")) and not layer_intact(ho2.plugins_dir, hc))
        fetched.clear()
        ho2.cycle()
        check("handover/the missing package directory is fetched again and swapped into place",
              [a[a.index("module-fetch") + 1] for a in fetched] == ["Store"] and layer_intact(ho2.plugins_dir, hc), fetched)
        os.unlink(os.path.join(ho2.plugins_dir, ".complete"))
        ho2.cycle()
        check("handover/a populated set that lost its .complete is rebuilt, not returned unchanged",
              len(index_reads) == 6 and layer_intact(ho2.plugins_dir, hc), index_reads)
        with open(os.path.join(ho2.plugins_dir, ".complete"), "a") as fh_:
            fh_.write("Ghost@1.0.0\n")
        check("handover/a manifest naming a plugin the set does not hold is not intact", not layer_intact(ho2.plugins_dir, hc))
        ho2.cycle()
        check("handover/...and is repaired from the index", layer_intact(ho2.plugins_dir, hc))
        os.symlink(store_bundle, os.path.join(ho2.plugins_dir, "Stray"))
        check("handover/a set holding a plugin its manifest does not name is not intact", not layer_intact(ho2.plugins_dir, hc))
        ho2.cycle()
        check("handover/...and is rebuilt without it", layer_intact(ho2.plugins_dir, hc) and not os.path.lexists(os.path.join(ho2.plugins_dir, "Stray")))
        # The EMPTY set (an index with `bundles: []`) is a legitimate layer; an emptied manifest of a non-empty set is not.
        ec = os.path.join(tmp, "emptycache")
        empty_dir = stage_bundle_set([], ec, lambda pkg, ver, out: 1)
        check("handover/an index with no bundles stages a valid EMPTY layer (the host claims, never stuck in no-inputs)",
              layer_intact(empty_dir, ec) and os.path.basename(empty_dir) == plugins_key_of([]).split(":", 1)[1], empty_dir)
        with open(os.path.join(ho2.plugins_dir, ".complete"), "w") as fh_:
            fh_.write("\n")
        check("handover/an EMPTIED manifest of a non-empty set is not intact", not layer_intact(ho2.plugins_dir, hc))
        ho2.cycle()
        check("handover/...and is repaired from the index", layer_intact(ho2.plugins_dir, hc))
        # Two replicas fetch the same missing bundle: the slower one must NOT move the faster one's intact install
        # aside (a running sandbox may bind-mount it) — it discards its own fetch. Same for the set.
        rc_ = os.path.join(tmp, "racecache")
        race_line = "Race@1.0.0"
        race_final = os.path.join(rc_, "bundles", hashlib.sha256(race_line.encode()).hexdigest()[:32])

        def racing_fetch(pkg, ver, out):
            os.makedirs(os.path.join(out, pkg))
            os.makedirs(os.path.join(race_final, pkg))              # the faster replica installs meanwhile
            with open(os.path.join(race_final, pkg, "in-use.dll"), "w") as fh_:
                fh_.write("mounted")
            with open(os.path.join(race_final, ".complete"), "w") as fh_:
                fh_.write(race_line + "\n")
            return 0
        race_ino = None
        race_set = stage_bundle_set([race_line], rc_, racing_fetch)
        race_ino = os.stat(os.path.join(race_final, "Race")).st_ino
        check("handover/a concurrent replica's intact bundle is kept in place (its files survive, no tmp left behind)",
              os.path.isfile(os.path.join(race_final, "Race", "in-use.dll")) and layer_intact(race_set, rc_)
              and not [n for n in os.listdir(os.path.join(rc_, "bundles")) if n.startswith((".fetch-", ".stale-"))],
              os.listdir(os.path.join(rc_, "bundles")))
        set_ino = os.stat(race_set).st_ino
        dup = tempfile.mkdtemp(prefix=".set-", dir=os.path.dirname(race_set))
        _swap_into_place(dup, race_set, lambda d: layer_intact(d, rc_))
        check("handover/publishing over an INTACT set discards the new copy and leaves the installed one",
              os.stat(race_set).st_ino == set_ino and not os.path.exists(dup)
              and os.stat(os.path.join(race_final, "Race")).st_ino == race_ino)
        # Negative control: over a DAMAGED entry the swap still replaces it.
        os.unlink(os.path.join(race_set, ".complete"))
        dup = tempfile.mkdtemp(prefix=".set-", dir=os.path.dirname(race_set))
        os.symlink(os.path.join(race_final, "Race"), os.path.join(dup, "Race"))
        with open(os.path.join(dup, ".complete"), "w") as fh_:
            fh_.write(race_line + "\n")
        displaced_before = {n for n in os.listdir(os.path.dirname(race_set)) if n.startswith(".stale-")}
        _swap_into_place(dup, race_set, lambda d: layer_intact(d, rc_))
        check("handover/negative control: a DAMAGED set is still replaced", layer_intact(race_set, rc_) and os.stat(race_set).st_ino != set_ino)
        # Deploy#46: the displaced entry is KEPT — a sandbox on another replica may still bind-mount it — and its
        # files are intact; it is reclaimed only once older than the longest run.
        displaced = [os.path.join(os.path.dirname(race_set), n) for n in os.listdir(os.path.dirname(race_set))
                     if n.startswith(".stale-") and n not in displaced_before]
        check("handover/a displaced entry is kept, its files intact, for a reader that may still mount it",
              len(displaced) == 1 and os.path.islink(os.path.join(displaced[0], "Race"))
              and os.stat(displaced[0]).st_ino == set_ino, displaced)
        check("handover/a freshly displaced entry is not swept", len(displaced) == 1
              and sweep_displaced(os.path.dirname(race_set)) == [] and os.path.isdir(displaced[0]), displaced)
        check("handover/a displaced entry older than the longest run IS swept",
              len(displaced) == 1
              and sweep_displaced(os.path.dirname(race_set), now=time.time() + STALE_RECLAIM_SECONDS + 1) == displaced
              and not os.path.exists(displaced[0]) and layer_intact(race_set, rc_), displaced)
        # A sweep under ANOTHER entry's lock may scan the parent between the rename and anything after it: an old
        # entry displaced at that instant must already carry its displacement time, never its old mtime.
        os.unlink(os.path.join(race_set, ".complete"))
        old_t = time.time() - STALE_RECLAIM_SECONDS - 3600
        os.utime(race_set, (old_t, old_t))
        dup = tempfile.mkdtemp(prefix=".set-", dir=os.path.dirname(race_set))
        os.symlink(os.path.join(race_final, "Race"), os.path.join(dup, "Race"))
        with open(os.path.join(dup, ".complete"), "w") as fh_:
            fh_.write(race_line + "\n")
        swept_mid: list = []
        real_rename = os.rename

        def rename_then_sweep(src, dst):
            real_rename(src, dst)
            if src == race_set and os.path.basename(dst).startswith(".stale-"):
                swept_mid.extend(sweep_displaced(os.path.dirname(race_set)))
        os.rename = rename_then_sweep
        try:
            _swap_into_place(dup, race_set, lambda d: layer_intact(d, rc_))
        finally:
            os.rename = real_rename
        check("handover/a sweep that runs the instant an OLD entry is displaced does not delete it",
              swept_mid == [] and len([n for n in os.listdir(os.path.dirname(race_set)) if n.startswith(".stale-")]) == 1,
              (swept_mid, os.listdir(os.path.dirname(race_set))))
        check("handover/the reclaim grace outlives every run a host may be configured for",
              STALE_RECLAIM_SECONDS > MAX_RUN_DEADLINE)
        # Without exclusion (the lock cannot be taken) a damaged entry is NEVER moved aside: Refusal, tmp discarded,
        # the damaged entry's files left as they are. An absent entry is still installed (a rename is not destructive).
        os.unlink(os.path.join(race_set, ".complete"))
        nolock_ino = os.stat(race_set).st_ino
        dup = tempfile.mkdtemp(prefix=".set-", dir=os.path.dirname(race_set))
        try:
            _swap_into_place(dup, race_set, lambda d: layer_intact(d, rc_), lock_fn=lambda fh: False)
            nolock_refused = False
        except Refusal:
            nolock_refused = True
        check("handover/a damaged entry is NOT replaced when its lock cannot be taken (Refusal, never a blind swap)",
              nolock_refused and os.stat(race_set).st_ino == nolock_ino and not os.path.exists(dup)
              and os.path.islink(os.path.join(race_set, "Race")), (nolock_refused, os.listdir(os.path.dirname(race_set))))
        absent = os.path.join(os.path.dirname(race_set), "absent-entry")
        dup = tempfile.mkdtemp(prefix=".set-", dir=os.path.dirname(race_set))
        _swap_into_place(dup, absent, lambda d: False, lock_fn=lambda fh: False)
        check("handover/...while an ABSENT entry is still installed without the lock", os.path.isdir(absent) and not os.path.exists(dup))
        # No signal (an older operations instance, an unreadable ledger): the index is read every turn, as before.
        nosig = host(_FakeApi({("GET", "/hosts/inputs"): lambda p_, b: (200, {"modules": None}),
                               ("POST", "/claim"): lambda p_, b: (204, None)}), env=hoenv, registry_get=reg_get, fetch_runner=fetch_runner)
        before = len(index_reads)
        nosig.cycle()
        nosig.cycle()
        check("handover/no signal: the index is read on EVERY turn (never 'nothing handed over')", len(index_reads) == before + 2, index_reads)
        check("handover/a record for another identity is never adopted", read_layer_record(hoenv["MW_BUILD_CACHE"], "other-identity") is None)

        # ── 7. whole runs ─────────────────────────────────────────────────────────────────
        posted: list[tuple] = []

        def on_events(path, body):
            posted.append((path, body["events"]))
            return 202, None

        def checkout_with(stages_doc):
            def co(repo, sha, refs, dest, token, env_, log, **_):
                os.makedirs(os.path.join(dest, ".github"))
                with open(os.path.join(dest, ".github", "build-stages.json"), "w") as fh:
                    json.dump(stages_doc, fh)
            return co

        claim = {"path": "Hosting/Builds/MeshWeaver.Plugins/" + "c" * 40 + "-stable",
                 "repo": "MeshWeaver.Plugins", "sha": "c" * 40, "refs": [], "isTrunk": False,
                 "publish": False, "stages": []}
        env_s = dict(env, MW_BUILD_CHANNEL="stable", BUILD_TOKEN_FILE=tf, AZURE_CLIENT_SECRET="leak")

        def api_ev(*extra):
            return _FakeApi({("POST", "/token"): [(200, {"token": "run-token"})],
                             ("POST", "/MeshWeaver.Plugins/"): on_events, **dict(extra)})

        # fail-fast + diagnostics + channel on every POST
        a5 = api_ev()
        h5 = host(a5, env=env_s, checkout=checkout_with({"stages": [
            {"name": "compile-check", "kind": "build",
             "command": "echo \"$PWD/Hosting/A.cs(3,1): error CS0246: missing X\"; exit 1"},
            {"name": "gate", "kind": "test", "command": f"echo ran > {tmp}/ran2"}]}))
        check("run/ran", h5.run(claim) == "ran")
        evpaths = [c for c in a5.calls if c[1].endswith("/events")]
        check("run/channel on every event POST", evpaths and all(c[3].get("channel") == "stable" for c in evpaths), evpaths)
        check("run/host named", all(c[2]["host"] == "build-latest-0" for c in evpaths), evpaths)
        allev = [(pth, e) for pth, evs in posted for e in evs]
        diag = [e for _, e in allev if e["kind"] == "diagnostic"]
        check("run/diagnostic relative to the checkout", diag and diag[0]["file"] == "Hosting/A.cs"
              and diag[0]["code"] == "CS0246", diag)
        fin = [(pth, e) for pth, e in allev if e["kind"] == "stage-finished"]
        check("run/compile-check failed", any("/compile-check/" in pth and e["state"] == "failed" for pth, e in fin), fin)
        check("run/gate not run and said so", any("/gate/" in pth and e["state"] == "failed"
                                                  and "not run" in e["summary"] for pth, e in fin), fin)
        check("run/fail-fast: the second stage never started", not os.path.exists(os.path.join(tmp, "ran2")))
        # §7.3b end to end: a REAL run with the warm workspace on — its plan names how the tree was made and `bytes copied = 0`;
        # an overlay holds the deployment tree for the whole run and lets go after; a cold worktree is removed and pruned
        if shutil.which("git"):
            rw = os.path.join(tmp, "run-73b")
            renv = dict(env_s, MW_BUILD_WARM_WORKSPACE="true", MW_BUILD_RUN_OVERLAY="true", MW_BUILD_WORK=os.path.join(rw, "work"),
                        MW_BUILD_CACHE=os.path.join(wroot, "cache"), MW_BUILD_GIT_BASE="file://" + os.path.dirname(origin))
            renv["MW_PLATFORM_SOURCE"] = image_src
            os.makedirs(renv["MW_BUILD_WORK"])
            rclaim = {"path": f"Hosting/Builds/R/{c5}-stable", "repo": "R", "sha": c5, "refs": [], "isTrunk": True,
                      "publish": False, "stages": []}

            def rv73():
                return [e for _, evs in posted for e in evs if e["kind"] == "run-finished"][-1:]

            def plan_lines():
                return [e.get("text", "") for _, evs in posted for e in evs if e.get("kind") == "line" and "workspace:" in e.get("text", "")]
            posted.clear()
            rlog: list = []
            hr = host(api_ev((("POST", "/R/"), on_events)), env=dict(renv))
            hr.log = rlog.append
            check("run-73b/a cold run (no namespace to overlay in) is green", hr.run(rclaim) == "ran" and rv73()
                  and rv73()[0]["succeeded"] is True, rv73())
            check("run-73b/its plan line names the COLD worktree off the master, and states bytes copied = 0",
                  any(l_.startswith(f"workspace: COLD worktree of the master at {c5[:12]}") and l_.endswith("bytes copied = 0")
                      for l_ in plan_lines()) and any(m.startswith("COLD WORKTREE") for m in rlog), (plan_lines(), rlog[-3:]))
            check("run-73b/the cold worktree is removed AND pruned when the run ends (nothing stays registered on the master)",
                  os.listdir(renv["MW_BUILD_WORK"]) == [] and f"{os.sep}work{os.sep}R-" not in worktrees(), worktrees())
            # the overlay path: the host's own deployment tree, built by its writer, then a run through the (emulated) overlay
            renv_o = dict(renv, MW_BUILD_IN_NS="1")
            ho = host(api_ev((("POST", "/R/"), on_events)), env=dict(renv_o), git_runner=_git_run)
            ho._sandboxed_capture = hi._sandboxed_capture
            check("run-73b/the host's writer builds its deployment tree", ho._warm_deployment("R", warm_key(ho.env, ho.identity)) == "built")
            odt = DeploymentTree(renv["MW_BUILD_CACHE"], ho.deployment(), "R")
            held_during: list = []

            def mount_and_probe(lower, dest):
                link_mount(lower, dest)
                with odt.writer() as w_:
                    held_during.append(w_ is None)
            ho.run_mount = mount_and_probe
            posted.clear()
            check("run-73b/an overlay run is green", ho.run(rclaim) == "ran" and rv73()[0]["succeeded"] is True, rv73())
            check("run-73b/its plan line names the overlay of the deployment tree and states bytes copied = 0",
                  any(l_.startswith(f"workspace: overlay of the deployment tree main@{c5[:12]}") and l_.endswith("bytes copied = 0")
                      for l_ in plan_lines()), plan_lines())
            with odt.writer() as w_end:
                check("run-73b/the run held the tree while mounted and released it after the unmount",
                      held_during == [True] and w_end is not None and os.listdir(renv["MW_BUILD_WORK"]) == [], (held_during, os.listdir(renv["MW_BUILD_WORK"])))
        rf_ = [e for _, e in allev if e["kind"] == "run-finished"]
        check("run/run-finished false, once", len(rf_) == 1 and rf_[0]["succeeded"] is False, rf_)

        # A red SIBLING hides nothing: build green, s0 red → s1 (after the build only) still RUNS, doc (after s0)
        # is not run and says which stage it waited on, tail (after doc) is not run either (a skip propagates),
        # lone (`after: []`) runs. The run stays red, naming the stage that ran red.
        posted.clear()
        marks = os.path.join(tmp, "sib")
        os.makedirs(marks)
        h5s = host(api_ev(), env=env_s, checkout=checkout_with({"stages": [
            {"name": "build", "kind": "build", "command": f"touch {marks}/build"},
            {"name": "s0", "kind": "test", "command": f"touch {marks}/s0; exit 1"},
            {"name": "s1", "kind": "test", "command": f"touch {marks}/s1"},
            {"name": "doc", "kind": "test", "command": f"touch {marks}/doc", "after": ["s0"]},
            {"name": "tail", "kind": "test", "command": f"touch {marks}/tail", "after": ["doc"]},
            {"name": "lone", "kind": "test", "command": f"touch {marks}/lone", "after": []}]}))
        check("run/siblings ran", h5s.run(claim) == "ran")
        check("run/an independent stage after a red sibling RUNS (one red shard never hides the others)",
              sorted(os.listdir(marks)) == ["build", "lone", "s0", "s1"], sorted(os.listdir(marks)))
        allev = [(pth, e) for pth, evs in posted for e in evs]
        fin = {pth.split("/")[-2]: e for pth, e in allev if e["kind"] == "stage-finished"}
        check("run/the independent sibling is GREEN", fin.get("s1", {}).get("state") == "succeeded", fin)
        check("run/a dependant of the red stage is not run and names it",
              fin.get("doc", {}).get("state") == "failed" and "not run" in fin["doc"]["summary"]
              and "s0" in fin["doc"]["summary"], fin)
        check("run/a skip propagates to the skipped stage's own dependants",
              fin.get("tail", {}).get("state") == "failed" and "doc" in fin["tail"]["summary"], fin)
        rf_ = [e for _, e in allev if e["kind"] == "run-finished"]
        check("run/a red sibling keeps the RUN red, naming the stage that ran red",
              len(rf_) == 1 and rf_[0]["succeeded"] is False and rf_[0]["error"] == "stage s0 failed", rf_)
        # Negative control: the same declaration with the BUILD red runs nothing that runs after it — only `lone`.
        posted.clear()
        shutil.rmtree(marks)
        os.makedirs(marks)
        h5n = host(api_ev(), env=env_s, checkout=checkout_with({"stages": [
            {"name": "build", "kind": "build", "command": f"touch {marks}/build; exit 1"},
            {"name": "s0", "kind": "test", "command": f"touch {marks}/s0"},
            {"name": "s1", "kind": "test", "command": f"touch {marks}/s1"},
            {"name": "lone", "kind": "test", "command": f"touch {marks}/lone", "after": []}]}))
        h5n.run(claim)
        check("run/negative control: a red BUILD skips every stage that runs after it",
              sorted(os.listdir(marks)) == ["build", "lone"], sorted(os.listdir(marks)))
        fin = {pth.split("/")[-2]: e for pth, evs in posted for e in evs if e["kind"] == "stage-finished"}
        check("run/negative control: the shards are not run, naming the build",
              all(fin.get(n, {}).get("state") == "failed" and "build" in fin[n]["summary"] for n in ("s0", "s1")), fin)

        # what a stage SEES: fresh process per stage, publish from the claim, nothing credential-shaped
        seen = os.path.join(tmp, "seen")
        dump = f"echo $$ ${{MW_BUILD_PUBLISH}} ${{AZURE_CLIENT_SECRET:-none}} ${{BUILD_TOKEN_FILE:-none}} ${{GITHUB_TOKEN:-none}} >> {seen}"
        posted.clear()
        key2 = "ef" * 16
        h6 = host(api_ev(), env=env_s, checkout=checkout_with({"stages": [
            {"name": "a", "kind": "build", "command": dump + f"; mkdir -p $MW_BUILD_OUTPUTS/{key2} && echo y > $MW_BUILD_OUTPUTS/{key2}/Y.dll"},
            {"name": "b", "kind": "test", "command": dump}]}))
        h6.run(dict(claim, publish=True))
        rows = [r.split() for r in open(seen).read().splitlines()]
        check("run/a fresh process per stage", len(rows) == 2 and rows[0][0] != rows[1][0], rows)
        check("run/publish from the claim", all(r[1] == "true" for r in rows), rows)
        check("run/no credential reaches a stage", all(r[2:] == ["none", "none", "none"] for r in rows), rows)
        # the claim's labels reach every stage as MW_BUILD_LABELS; a claim without them leaves it UNSET (not told)
        posted_h6 = list(posted)
        for lbl, want in ((["full-run", "tests-before-review"], "full-run,tests-before-review"), ([], ""), (None, "unset")):
            seen_l = os.path.join(tmp, "seen-labels")
            if os.path.exists(seen_l):
                os.remove(seen_l)
            hl = host(api_ev(), env=env_s, checkout=checkout_with({"stages": [
                {"name": "a", "kind": "test", "command": f"echo \"[${{MW_BUILD_LABELS-unset}}]\" >> {seen_l}"}]}))
            hl.run(dict(claim) if lbl is None else dict(claim, labels=lbl))
            got = open(seen_l).read().strip() if os.path.exists(seen_l) else "<stage did not run>"
            check(f"run/claim labels {lbl!r} reach the stage as MW_BUILD_LABELS={want!r}", got == f"[{want}]", got)
            check(f"run/the stage ran with its checkout mounted at {SANDBOX_CHECKOUT} ({lbl!r})", _EnvSandbox.last_mount == SANDBOX_CHECKOUT,
                  _EnvSandbox.last_mount)
            check(f"run/the stage's package folder is the run's own, bound at {SANDBOX_NUGET} ({lbl!r})",
                  len(_EnvSandbox.last_binds) == 1 and _EnvSandbox.last_binds[0][1] == SANDBOX_NUGET
                  and _EnvSandbox.last_binds[0][0].endswith("/nuget"), _EnvSandbox.last_binds)
        posted[:] = posted_h6   # the checks below read h6's own events
        rf_ = [e for _, evs in posted for e in evs if e["kind"] == "run-finished"]
        check("run/green run-finished", rf_ == [{"kind": "run-finished", "succeeded": True, "error": None}], rf_)
        check("run/a PR run's outputs never enter the shared cache",
              not os.path.exists(os.path.join(cache, "outputs", "MeshWeaver.Plugins", key2)))
        keyed_env = dict(env_s, MW_BUILD_TESTER_DIGEST="sha256:" + "7" * 64, MW_BUILD_PORTAL_DIGEST="sha256:" + "8" * 64)
        me = os.path.abspath(__file__)
        keyed = ("echo 'public class A {}' > A.cs; "
                 "printf '{\"kind\":\"nodetype\",\"sources\":[\"A.cs\"],\"options\":\"--warnings-as-errors\"}' > spec.json; "
                 f"k=$(python3 {me} --cache-key spec.json) && mkdir -p $MW_BUILD_OUTPUTS/$k && cp spec.json $MW_BUILD_OUTPUTS/$k/.cache-key.json "
                 "&& echo y > $MW_BUILD_OUTPUTS/$k/A.dll && echo $k > ../KEY")
        h6t = host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [
            {"name": "a", "kind": "build", "command": keyed + f" && cp ../KEY {tmp}/KEY"},
            {"name": "t", "kind": "test", "command": f"mkdir -p $MW_BUILD_OUTPUTS/{'ab' * 20} && touch $MW_BUILD_OUTPUTS/{'ab' * 20}/verdict"}]}))
        h6t.run(dict(claim, isTrunk=True))
        k1 = open(os.path.join(tmp, "KEY")).read().strip() if os.path.exists(os.path.join(tmp, "KEY")) else ""
        check("run/a trunk BUILD stage's output is promoted under the key the host recomputes",
              k1 and os.path.isfile(os.path.join(cache, "outputs", "MeshWeaver.Plugins", k1, "A.dll")), k1)
        check("run/a TEST stage's output is never promoted (no test verdict is reused)",
              not os.path.exists(os.path.join(cache, "outputs", "MeshWeaver.Plugins", "ab" * 20)))
        # a stage asserting a key the inputs do not produce is refused
        lie = "f" * 64
        h6l = host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [
            {"name": "a", "kind": "build", "command": "echo x > A.cs; "
             "printf '{\"kind\":\"nodetype\",\"sources\":[\"A.cs\"]}' > s.json; "
             f"mkdir -p $MW_BUILD_OUTPUTS/{lie} && cp s.json $MW_BUILD_OUTPUTS/{lie}/.cache-key.json"}]}))
        h6l.run(dict(claim, isTrunk=True))
        check("run/an output whose name is not its recomputed key is refused", not os.path.exists(os.path.join(cache, "outputs", "MeshWeaver.Plugins", lie)))
        # a red BUILD stage's partial output is never promoted by an independent build that runs after it
        keyed_b = keyed.replace("public class A {}", "public class B {}").replace("> ../KEY", "> ../KEYB")
        partial = [{"name": "bad", "kind": "build", "command": keyed_b + f" && cp ../KEYB {tmp}/KEYB; exit 1"},
                   {"name": "good", "kind": "build", "command": "true", "after": []}]
        posted.clear()
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": partial})).run(dict(claim, isTrunk=True))
        kb = open(os.path.join(tmp, "KEYB")).read().strip() if os.path.exists(os.path.join(tmp, "KEYB")) else ""
        fin = {pth.split("/")[-2]: e for pth, evs in posted for e in evs if e["kind"] == "stage-finished"}
        check("run/the independent build after a red build RUNS", fin.get("good", {}).get("state") == "succeeded", fin)
        check("run/a red build's (partial) output is NOT promoted by a later independent build",
              kb and not os.path.exists(os.path.join(cache, "outputs", "MeshWeaver.Plugins", kb)), kb)
        # Negative control: the same declaration with `bad` GREEN promotes that very output.
        partial[0] = dict(partial[0], command=keyed_b + f" && cp ../KEYB {tmp}/KEYB")
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": partial})).run(dict(claim, isTrunk=True))
        check("run/negative control: the same output from a GREEN build is promoted",
              kb and os.path.isfile(os.path.join(cache, "outputs", "MeshWeaver.Plugins", kb, "A.dll")), kb)

        # ── 7b. a PREPARED server: no stage prepares per run; what a run had to restore warms the server ──
        for cmd_, why_ in (("python3 .github/scripts/resolve-platform.py --passed-on-main", "resolve-platform"),
                           ("python3 scripts/fetch-refs.py --digest x", "fetch-refs"),
                           ("python3 scripts/platform-mount.py", "platform-mount"),
                           ("dotnet /opt/x.dll module-fetch AI --registry r", "module-fetch"),
                           ("docker pull postgres:17", "image pull"), ("oras pull cr/x@sha256:1", "image pull"),
                           ("docker image pull x", "image pull"), ("bash scripts/prepull-testcontainers-images.sh", "pre-pull"),
                           ("set -e; dotnet restore src/X.csproj", "dotnet restore"), ("nuget install X", "NuGet install"),
                           ("apt-get -y install jq", "system package"), ("python3 -m pip install pyyaml", "pip"),
                           ("npm ci", "npm"), ("python3 -m pip --no-cache-dir install pyyaml", "pip"),
                           ("pip3 --disable-pip-version-check install x", "pip"), ("npm --prefix ./app ci", "npm"),
                           ("npm --prefix=./app install", "npm"), ("dotnet --diagnostics restore X.sln", "dotnet restore"),
                           ("docker --context remote pull x", "image pull"), ("apk add --no-cache git", "apk add"),
                           ("apk --no-cache add git", "apk add")):
            try:
                validate_stages({"stages": [{"name": "x", "kind": "build", "command": cmd_}]})
                check(f"prepared/refuses a stage that prepares per run: {cmd_}", False, "accepted")
            except Refusal as r:
                check(f"prepared/the refusal names what to move into the server ({why_})", why_ in str(r) and "PREPARED" in str(r), str(r))
        # negative controls: what the fleet's stages run today is NOT a prepare step
        for cmd_ in ("python3 scripts/canonical-compile-check.py --refs \"$refs\"; python3 scripts/check-scripts.py --refs \"$refs\"",
                     "\"$dn\" \"$tester\" compile . --app \"$app\" --allow plugin-gate.allow",
                     "python3 /core-tests/core-tests.py shard 3", "dotnet build -c Release --no-restore src/X.csproj",
                     "echo restored-by-the-server; grep -c pull log.txt", "npm --prefix ./app run build",
                     "dotnet --info; dotnet build --no-restore -c Release X.csproj", "python3 -m pip --version"):
            check(f"prepared/accepts a prepared stage: {cmd_[:40]}", per_run_prepare_steps(cmd_) == [], per_run_prepare_steps(cmd_))
        # A REAL package fixture (a zip with a nuspec, a lib and OPC plumbing), restored by the stage from a folder the
        # self-test writes: `good` (unsigned), `signed` (a `.signature.p7s` entry appended — its content hash is the
        # UNSIGNED package's, not the archive's), `forged` (metadata naming another hash) and `tamper` (good hashes,
        # but the extracted DLL altered and an extra file planted after NuGet extracted it).
        def fixture_zip(signed_: bool, encrypted_: bool = False) -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for n_, b_ in (("Demo.Pkg.nuspec", b"<package/>"), ("lib/net10.0/Demo.dll", b"the-real-dll"),
                               ("[Content_Types].xml", b"<Types/>"), ("_rels/.rels", b"<Relationships/>")):
                    z.writestr(zipfile.ZipInfo(n_, (2020, 1, 1, 0, 0, 0)), b_)
                if signed_:
                    z.writestr(zipfile.ZipInfo(NUGET_SIGNATURE, (2020, 1, 1, 0, 0, 0)), b"pkcs7-signature")
            if not encrypted_:
                return buf.getvalue()
            # zipfile resets flag_bits on write, so set the ENCRYPTED bit (0x1) in the bytes: on the DLL's local header
            # (flags at +6) and its central-directory record (flags at +8) — zipfile then refuses to read it.
            raw_ = bytearray(buf.getvalue())
            for sig_, off_ in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
                at_ = raw_.find(sig_)
                while at_ >= 0:
                    nl_ = struct.unpack_from("<H", raw_, at_ + (26 if off_ == 6 else 28))[0]
                    name_at_ = at_ + (30 if off_ == 6 else 46)
                    if bytes(raw_[name_at_:name_at_ + nl_]).endswith(b".dll"):
                        raw_[at_ + off_] |= 0x1
                    at_ = raw_.find(sig_, at_ + 4)
            return bytes(raw_)

        def b64sha(b_: bytes) -> str:
            return base64.b64encode(hashlib.sha512(b_).digest()).decode()
        unsigned_, signed_zip, encrypted_zip = fixture_zip(False), fixture_zip(True), fixture_zip(False, True)
        # CORRUPT DEFLATE: the DLL entry's compressed bytes are garbage (valid zip structure, so its hashes still
        # match what the fixture records) — extraction raises zlib.error, which must be a refusal, never a crash.
        _cz = io.BytesIO()
        with zipfile.ZipFile(_cz, "w", zipfile.ZIP_DEFLATED) as z:
            for n_, b_ in (("Demo.Pkg.nuspec", b"<package/>"), ("lib/net10.0/Demo.dll", b"the-real-dll" * 64),
                           ("[Content_Types].xml", b"<Types/>"), ("_rels/.rels", b"<Relationships/>")):
                z.writestr(zipfile.ZipInfo(n_, (2020, 1, 1, 0, 0, 0)), b_, compress_type=zipfile.ZIP_DEFLATED)
        corrupt_zip = bytearray(_cz.getvalue())
        _at = corrupt_zip.find(b"PK\x03\x04", corrupt_zip.find(b"lib/net10.0/Demo.dll") - 30)
        _data = _at + 30 + struct.unpack_from("<H", corrupt_zip, _at + 26)[0] + struct.unpack_from("<H", corrupt_zip, _at + 28)[0]
        corrupt_zip[_data:_data + 4] = b"\xff\xff\xff\xff"
        corrupt_zip = bytes(corrupt_zip)
        check("nuget/a signed package's content hash is the UNSIGNED package's, not its archive hash",
              nupkg_content_hash(signed_zip) == b64sha(unsigned_) != b64sha(signed_zip), nupkg_content_hash(signed_zip))
        check("nuget/an unsigned package's content hash is its archive hash", nupkg_content_hash(unsigned_) == b64sha(unsigned_))
        check("nuget/not a zip: no content hash (never promoted)", nupkg_content_hash(b"nupkg-bytes") is None)
        eocd_ = signed_zip[signed_zip.rfind(b"PK\x05\x06"):]
        truncated_ = b"PK\x01\x02" + b"\0" * 10 + eocd_[:8] + struct.pack("<HHII", 1, 1, 14, 0) + eocd_[20:]
        check("nuget/a TRUNCATED central-directory record: no content hash, never an exception", nupkg_content_hash(truncated_) is None)
        nulld_ = os.path.join(tmp, "pkg-null", "demo.pkg", "1.0.0")
        os.makedirs(nulld_)
        open(os.path.join(nulld_, "demo.pkg.1.0.0.nupkg"), "wb").write(truncated_)
        open(os.path.join(nulld_, "demo.pkg.1.0.0.nupkg.sha512"), "w").write(b64sha(truncated_))
        open(os.path.join(nulld_, ".nupkg.metadata"), "w").write(json.dumps({"version": 2, "contentHash": None}))
        check("nuget/negative control: a null contentHash over an unreadable archive is refused, not matched",
              nuget_package_problem(nulld_, "demo.pkg", "1.0.0") is not None, nuget_package_problem(nulld_, "demo.pkg", "1.0.0"))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(unsigned_)     # what the stand-in feed publishes for the fixture package

        # ── PROVENANCE (MeshWeaver.Plugins#4493): a package is promoted only when its archive is the one its SOURCE FEED
        # publishes. A FORGED package is self-consistent (its .sha512 and contentHash match its own bytes) — every local
        # check passes — under the real id/version; only the feed's published hash tells it apart.
        def restored_pkg(root_, bytes_, source_="https://api.nuget.org/v3/index.json"):
            d_ = os.path.join(root_, "demo.pkg", "1.0.0")
            shutil.rmtree(root_, ignore_errors=True)
            os.makedirs(d_)
            open(os.path.join(d_, "demo.pkg.1.0.0.nupkg"), "wb").write(bytes_)
            open(os.path.join(d_, "demo.pkg.1.0.0.nupkg.sha512"), "w").write(b64sha(bytes_))
            meta_ = {"version": 2, "contentHash": nupkg_content_hash(bytes_)}
            if source_ is not None:
                meta_["source"] = source_
            json.dump(meta_, open(os.path.join(d_, ".nupkg.metadata"), "w"))
            return root_
        _fz = io.BytesIO()
        with zipfile.ZipFile(_fz, "w", zipfile.ZIP_DEFLATED) as z:
            for n_, b_ in (("Demo.Pkg.nuspec", b"<package/>"), ("lib/net10.0/Demo.dll", b"NOT-the-published-dll"),
                           ("[Content_Types].xml", b"<Types/>"), ("_rels/.rels", b"<Relationships/>")):
                z.writestr(zipfile.ZipInfo(n_, (2020, 1, 1, 0, 0, 0)), b_)
        forged_zip = _fz.getvalue()
        forged_dir = restored_pkg(os.path.join(tmp, "prov-forged"), forged_zip)
        check("provenance/the forged package passes every LOCAL check (the case has a subject)",
              nuget_package_problem(os.path.join(forged_dir, "demo.pkg", "1.0.0"), "demo.pkg", "1.0.0") is None)
        feeds_ = NugetFeeds(stand_in_feed)

        def promote_into(name_, restored_, feeds__=None):
            cache_ = os.path.join(tmp, f"prov-cache-{name_}")
            shutil.rmtree(cache_, ignore_errors=True)
            said_: list = []
            res_ = promote_nuget(restored_, cache_, said_.append, feeds__ or feeds_)
            return res_, nuget_packages(os.path.join(cache_, "nuget")), said_
        (pf_, kf_, rf_), inf_, saidf_ = promote_into("forged", forged_dir)
        check("provenance/a FORGED package (self-consistent, not the archive its feed publishes) is REFUSED, by name, "
              "and is NOT in the shared cache",
              pf_ == 0 and inf_ == set() and len(rf_) == 1 and rf_[0].startswith("demo.pkg/1.0.0")
              and "not the one its source feed publishes" in rf_[0] and any("demo.pkg/1.0.0" in m_ for m_ in saidf_),
              (pf_, inf_, rf_, saidf_))
        (pg_, _, rg_), ing_, _ = promote_into("good", restored_pkg(os.path.join(tmp, "prov-good"), unsigned_))
        check("provenance/a package whose archive IS the one its feed publishes is promoted",
              pg_ == 1 and ing_ == {"demo.pkg/1.0.0"} and rg_ == [], (pg_, ing_, rg_))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(signed_zip)
        (ps_, _, rs_), ins_, _ = promote_into("signed", restored_pkg(os.path.join(tmp, "prov-signed"), signed_zip))
        check("provenance/a SIGNED package is compared by its ARCHIVE hash (the feed's packageHash covers the signature) "
              "— promoted", ps_ == 1 and ins_ == {"demo.pkg/1.0.0"} and rs_ == [], (ps_, ins_, rs_))
        feed_hashes["demo.pkg/1.0.0"] = nupkg_content_hash(signed_zip)
        (_, _, rsc_), insc_, _ = promote_into("signed-content", restored_pkg(os.path.join(tmp, "prov-signed-c"), signed_zip))
        check("provenance/negative control: a feed hash equal to the signed package's CONTENT hash (not its archive) "
              "is not a match — refused", insc_ == set() and len(rsc_) == 1, rsc_)
        # UNVERIFIABLE: fail closed — refused by name, never promoted on a missing answer.
        good_dir = restored_pkg(os.path.join(tmp, "prov-unv"), unsigned_)
        feed_hashes["demo.pkg/1.0.0"] = None
        (_, _, rn_), inn_, _ = promote_into("nohash", good_dir)
        check("provenance/a feed that publishes NO hash: refused, not in the shared cache",
              inn_ == set() and len(rn_) == 1 and "publishes no SHA512" in rn_[0], rn_)
        del feed_hashes["demo.pkg/1.0.0"]
        (_, _, r404_), in404_, _ = promote_into("404", good_dir)
        check("provenance/a feed that cannot be asked (404): refused, not in the shared cache",
              in404_ == set() and len(r404_) == 1 and "could not be asked" in r404_[0], r404_)
        feed_hashes["demo.pkg/1.0.0"] = b64sha(unsigned_)

        def failing_get(url, timeout):
            raise socket.timeout("timed out")
        (_, _, rto_), into_, _ = promote_into("timeout", good_dir, NugetFeeds(failing_get))
        check("provenance/a feed that times out: refused, not in the shared cache",
              into_ == set() and len(rto_) == 1 and "could not be asked" in rto_[0], rto_)

        def slow_get(url, timeout):
            time.sleep(0.5)
            return stand_in_feed(url, timeout)
        slow_ = NugetFeeds(slow_get).published_hashes([("demo.pkg/1.0.0", "https://api.nuget.org/v3/index.json",
                                                        "demo.pkg", "1.0.0")], budget=0.05)
        check("provenance/the feed is asked within a BOUNDED budget: an answer that comes later is a refusal",
              isinstance(slow_.get("demo.pkg/1.0.0"), ValueError) and "did not answer" in str(slow_["demo.pkg/1.0.0"]), slow_)
        for label_, src_ in (("a local-folder source", "/home/x/dist/packages"), ("no recorded source", None),
                             ("a plain-http feed", "http://api.nuget.org/v3/index.json"),
                             ("a v2 feed", "https://www.nuget.org/api/v2/")):
            (_, _, rl_), inl_, _ = promote_into("src", restored_pkg(os.path.join(tmp, "prov-src"), unsigned_, src_))
            check(f"provenance/{label_}: unverifiable, refused, not in the shared cache",
                  inl_ == set() and len(rl_) == 1 and "not a NuGet v3 feed this host can verify" in rl_[0], rl_)
        held_ = os.path.join(tmp, "prov-held")
        promote_nuget(good_dir, held_, lambda m: None, feeds_)
        check("provenance/a package the cache already holds is KEPT without asking the feed (an id/version is immutable)",
              promote_nuget(good_dir, held_, lambda m: None, NugetFeeds(failing_get))[:2] == (0, 1))

        # ── EXTRACTION PARITY (MeshWeaver.Plugins#4478): a rebuilt folder is the folder NuGet would have written. Two
        # host-only reds came from it: Grpc.Tools' protoc kept no execute bit (MSB6003 "Permission denied" from the warm
        # cache), and Nerdbank.MessagePack 1.2.4 — whose archive names `analyzers/cs//…dll` — was refused outright.
        _tz = io.BytesIO()
        with zipfile.ZipFile(_tz, "w", zipfile.ZIP_DEFLATED) as z:
            for n_, b_ in (("Demo.Pkg.nuspec", b"<package/>"), ("tools/linux_x64/protoc", b"#!/bin/sh\necho ran\n"),
                           ("analyzers/cs//Demo.CodeFixes.dll", b"codefix-dll"),
                           ("[Content_Types].xml", b"<Types/>"), ("_rels/.rels", b"<Relationships/>")):
                z.writestr(zipfile.ZipInfo(n_, (2020, 1, 1, 0, 0, 0)), b_)
        tools_zip = _tz.getvalue()
        feed_hashes["demo.pkg/1.0.0"] = b64sha(tools_zip)
        (pt_, _, rt_), int_, _ = promote_into("tools", restored_pkg(os.path.join(tmp, "prov-tools"), tools_zip))
        tdir_ = os.path.join(tmp, "prov-cache-tools", "nuget", "demo.pkg", "1.0.0")
        protoc_ = os.path.join(tdir_, "tools", "linux_x64", "protoc")
        check("extraction/an entry with an EMPTY segment (`analyzers/cs//X.dll`) is promoted at the path NuGet writes",
              pt_ == 1 and rt_ == [] and os.path.isfile(os.path.join(tdir_, "analyzers", "cs", "Demo.CodeFixes.dll")),
              (pt_, rt_))
        check("extraction/an extracted file keeps NuGet's EXECUTE bit (0766 & ~umask), so protoc runs from the cache",
              os.path.isfile(protoc_) and os.stat(protoc_).st_mode & 0o100 != 0
              and subprocess.run([protoc_], capture_output=True, text=True).stdout.strip() == "ran",
              oct(os.stat(protoc_).st_mode) if os.path.isfile(protoc_) else "absent")
        check("extraction/a rebuilt folder carries the materialized mark", os.path.isfile(os.path.join(tdir_, NUGET_MATERIALIZED_MARK)))
        for bad_ in ("a/../b.dll", "./b.dll", "/abs.dll", "a\\b.dll", "//", "%2e%2e/b.dll"):
            try:
                nuget_entry_parts(bad_)
                check(f"extraction/negative control: {bad_!r} would leave the package folder — refused", False, "accepted")
            except ValueError:
                pass
        check("extraction/empty segments collapse, nothing else changes", nuget_entry_parts("a//b///c.dll") == ["a", "b", "c.dll"])
        # REPAIR: a folder an OLDER runner rebuilt (no mark, files 0644) is kept, and its modes are repaired — the
        # bytes are not touched, and a second pass changes nothing.
        os.remove(os.path.join(tdir_, NUGET_MATERIALIZED_MARK))
        os.chmod(protoc_, 0o644)
        said_rep_: list = []
        rep_ = promote_nuget(restore_dir_ := os.path.join(tmp, "prov-tools"), os.path.join(tmp, "prov-cache-tools"),
                             said_rep_.append, NugetFeeds(failing_get))
        check("extraction/an older runner's folder is KEPT (the feed is not asked) and its modes are repaired",
              rep_[:2] == (0, 1) and os.stat(protoc_).st_mode & 0o100 != 0
              and os.path.isfile(os.path.join(tdir_, NUGET_MATERIALIZED_MARK))
              and any("modes repaired" in m_ for m_ in said_rep_), (rep_, oct(os.stat(protoc_).st_mode), said_rep_))
        said_rep2_: list = []
        promote_nuget(restore_dir_, os.path.join(tmp, "prov-cache-tools"), said_rep2_.append, NugetFeeds(failing_get))
        check("extraction/a marked folder is not repaired again", not any("modes" in m_ for m_ in said_rep2_), said_rep2_)
        feed_hashes["demo.pkg/1.0.0"] = b64sha(unsigned_)
        try:
            feed_get("http://api.nuget.org/v3/index.json", 1)
            check("provenance/the real fetch refuses a non-https URL before any request", False, "fetched")
        except ValueError:
            pass
        # A REDIRECT to plain http is refused before it is followed; one to https is followed.
        rd_ = HttpsOnlyRedirect()
        rq_ = urllib.request.Request("https://api.nuget.org/v3/index.json")
        try:
            rd_.redirect_request(rq_, None, 302, "Found", {}, "http://api.nuget.org/v3/index.json")
            check("provenance/a redirect from https to http is refused", False, "followed")
        except ValueError:
            pass
        check("provenance/a redirect to another https URL is followed",
              rd_.redirect_request(rq_, None, 302, "Found", {}, "https://api.nuget.org/v3-flat/index.json") is not None)
        check("provenance/the real fetch's opener refuses http redirects (HttpsOnlyRedirect is installed)",
              any(isinstance(h_, HttpsOnlyRedirect) for h_ in feed_opener().handlers))
        # A body that keeps TRICKLING is cut at the absolute deadline (the socket timeout bounds each read only).
        clock_ = [0.0]

        class Trickle:
            def read(self, n_):
                clock_[0] += 1.0
                return b"x"
        try:
            read_bounded(Trickle(), 5.0, NUGET_FEED_MAX_BYTES, clock=lambda: clock_[0])
            check("provenance/a trickling answer is cut at its absolute deadline", False, "read to the end")
        except TimeoutError:
            check("provenance/a trickling answer is cut at its absolute deadline", clock_[0] <= 6.0, clock_[0])
        # TRUST is the host's, never the package's: a stage that rewrites BOTH the archive and `source` to a feed it
        # controls (an https v3 feed that publishes the forged archive's hash) is refused.
        def rogue_feed(url, timeout):
            if url == "https://rogue.test/v3/index.json":
                return json.dumps({"resources": [{"@id": "https://feed.test/reg/", "@type": "RegistrationsBaseUrl/3.6.0"}]}).encode()
            return stand_in_feed(url, timeout)
        feed_hashes["demo.pkg/1.0.0"] = b64sha(forged_zip)
        rogue_dir = restored_pkg(os.path.join(tmp, "prov-rogue"), forged_zip, "https://rogue.test/v3/index.json")
        (pr_, _, rr_), inr_, _ = promote_into("rogue", rogue_dir, NugetFeeds(rogue_feed))
        check("provenance/a forged archive whose `source` names a feed the STAGE controls is REFUSED (untrusted source)",
              pr_ == 0 and inr_ == set() and len(rr_) == 1 and "not a feed this host trusts" in rr_[0], (pr_, inr_, rr_))
        (pt_, _, _), int_, _ = promote_into("rogue-trusted", rogue_dir,
                                            NugetFeeds(rogue_feed, trusted=("https://rogue.test/v3/index.json",)))
        check("provenance/negative control: the same feed, trusted by the HOST's configuration, vouches",
              pt_ == 1 and int_ == {"demo.pkg/1.0.0"}, (pt_, int_))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(unsigned_)
        check("provenance/the trusted sources come from the host's env, nuget.org's when unset",
              nuget_trusted_sources({}) == NUGET_DEFAULT_TRUSTED_SOURCES
              and nuget_trusted_sources({NUGET_TRUSTED_SOURCES_ENV: "https://a.test/v3/index.json, https://b.test/index.json"})
              == ("https://a.test/v3/index.json", "https://b.test/index.json"))
        check("provenance/a trusted source matches case-insensitively in host, with or without a trailing slash",
              _source_key("https://API.nuget.org/v3/index.json/") == _source_key("https://api.nuget.org/v3/index.json"))
        # The RUN's remaining time bounds the verification: with none left the feed is never asked and nothing promoted.
        asked_: list = []

        def counting_get(url, timeout):
            asked_.append(url)
            return stand_in_feed(url, timeout)
        (pb_, _, rb_), inb_, _ = (promote_nuget(good_dir, os.path.join(tmp, "prov-cache-nobudget"), lambda m: None,
                                                NugetFeeds(counting_get), budget=0),
                                  nuget_packages(os.path.join(tmp, "prov-cache-nobudget", "nuget")), None)
        check("provenance/no run time left: the feed is not asked, the package is refused (retried by a later run)",
              pb_ == 0 and inb_ == set() and asked_ == [] and len(rb_) == 1 and "no time left" in rb_[0], (pb_, inb_, asked_, rb_))

        def pkg_fixture(kind_: str) -> str:
            root_ = os.path.join(tmp, f"pkg-{kind_}")
            d_ = os.path.join(root_, "demo.pkg", "1.0.0")
            shutil.rmtree(root_, ignore_errors=True)
            os.makedirs(os.path.join(d_, "lib", "net10.0"))
            b_ = (signed_zip if kind_ == "signed" else encrypted_zip if kind_ == "encrypted"
                  else corrupt_zip if kind_ == "corrupt" else unsigned_)
            open(os.path.join(d_, "demo.pkg.1.0.0.nupkg"), "wb").write(b_)
            open(os.path.join(d_, "demo.pkg.1.0.0.nupkg.sha512"), "w").write(b64sha(b_))
            open(os.path.join(d_, "demo.pkg.nuspec"), "wb").write(b"<package/>")
            open(os.path.join(d_, "lib", "net10.0", "Demo.dll"), "wb").write(b"POISONED" if kind_ == "tamper" else b"the-real-dll")
            if kind_ == "tamper":
                open(os.path.join(d_, "lib", "net10.0", "Extra.targets"), "w").write("<Project/>")
            json.dump({"version": 2, "contentHash": "forged" if kind_ == "forged" else nupkg_content_hash(b_),
                       "source": "https://api.nuget.org/v3/index.json"},
                      open(os.path.join(d_, ".nupkg.metadata"), "w"))
            return f"cp -R {root_}/. \"$NUGET_PACKAGES\"/"
        warm = os.path.join(cache, "nuget", "demo.pkg", "1.0.0")
        posted.clear()
        hpr = host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": pkg_fixture("good")}]}))
        hpr.run(claim)
        warns = [e for _, evs in posted for e in evs if e["kind"] == "diagnostic" and e.get("code") == "MWPREP001"]
        check("prepared/a package restored per run is SAID (a warning naming it)", len(warns) == 1 and "demo.pkg/1.0.0" in warns[0]["message"], warns)
        check("prepared/a PR run's restore never enters the warm cache", not os.path.exists(warm))
        posted.clear()
        htr = host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": pkg_fixture("good")}]}))
        htr.run(dict(claim, isTrunk=True))
        check("prepared/a trunk build's verified restore is promoted into the warm cache",
              os.path.isfile(os.path.join(warm, ".nupkg.metadata")) and nuget_packages(os.path.join(cache, "nuget")) == {"demo.pkg/1.0.0"}
              and sorted(os.listdir(warm)) == [NUGET_MATERIALIZED_MARK, ".nupkg.metadata", "demo.pkg.1.0.0.nupkg", "demo.pkg.1.0.0.nupkg.sha512", "demo.pkg.nuspec", "lib"],
              sorted(os.listdir(warm)) if os.path.isdir(warm) else None)
        shutil.rmtree(os.path.join(cache, "nuget"))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(signed_zip)     # the feed serves the SIGNED archive
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": pkg_fixture("signed")}]})).run(dict(claim, isTrunk=True))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(unsigned_)
        check("prepared/a SIGNED package (content hash != archive hash) is promoted, its signature extracted as NuGet does",
              os.path.isfile(os.path.join(warm, ".nupkg.metadata")) and os.path.isfile(os.path.join(warm, NUGET_SIGNATURE)))
        shutil.rmtree(os.path.join(cache, "nuget"))
        # The feed publishes EACH fixture's own archive hash, so provenance PASSES and the refusal under test is the
        # materialisation's (RuntimeError / zlib.error) — never a provenance mismatch standing in for it.
        for kind_, zip_ in (("encrypted", encrypted_zip), ("corrupt", corrupt_zip)):
            feed_hashes["demo.pkg/1.0.0"] = b64sha(zip_)
            (pm_, _, rm_), inm_, _ = promote_into(f"mat-{kind_}", restored_pkg(os.path.join(tmp, f"prov-mat-{kind_}"), zip_))
            check(f"provenance/the {kind_} fixture passes provenance and is refused by MATERIALISATION",
                  pm_ == 0 and inm_ == set() and len(rm_) == 1 and "could not be moved into place" in rm_[0], rm_)
        # An entry zipfile cannot OPEN (the encrypted one) must not leak the output descriptor (review of #109): the
        # host is long-running and refuses such a package on every run.
        enc_src_ = os.path.join(restored_pkg(os.path.join(tmp, "fd-enc"), encrypted_zip), "demo.pkg", "1.0.0")
        fds_before_ = len(os.listdir("/dev/fd"))
        for i_ in range(20):
            try:
                materialize_nuget_package(enc_src_, "demo.pkg", "1.0.0", os.path.join(tmp, f"fd-enc-out-{i_}"))
            except RuntimeError:
                pass
        check("extraction/an entry that cannot be opened closes the output descriptor (no fd leak over 20 refusals)",
              len(os.listdir("/dev/fd")) == fds_before_, (fds_before_, len(os.listdir("/dev/fd"))))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(encrypted_zip)
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": pkg_fixture("encrypted")}]})).run(dict(claim, isTrunk=True))
        check("prepared/negative control: an ENCRYPTED package (valid hashes, unreadable entry) is refused, never a crash",
              not os.path.exists(warm))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(corrupt_zip)
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": pkg_fixture("corrupt")}]})).run(dict(claim, isTrunk=True))
        check("prepared/negative control: a CORRUPT-DEFLATE package (valid hashes) is refused, never a crash", not os.path.exists(warm))
        feed_hashes["demo.pkg/1.0.0"] = b64sha(unsigned_)
        shutil.rmtree(os.path.join(cache, "nuget"), ignore_errors=True)
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]})).run(dict(claim, isTrunk=True))
        check("prepared/a fresh cache gets <cache>/nuget before any stage runs, with NO prepared inputs declared",
              os.path.isdir(os.path.join(cache, "nuget")))
        big_ = tempfile.TemporaryFile()
        big_.write(b"x" * (3 * PREPARED_OUTPUT_TAIL) + b"\nKEY")
        check("prepared/a command's output is read back as a BOUNDED tail, its final line intact",
              len(file_tail(big_, PREPARED_OUTPUT_TAIL)) == PREPARED_OUTPUT_TAIL and file_tail(big_, PREPARED_OUTPUT_TAIL).endswith("\nKEY"))
        big_.close()
        htf = host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": pkg_fixture("forged")}]}))
        htf.run(dict(claim, isTrunk=True))
        check("prepared/negative control: a package whose hash is not what NuGet recorded is NOT promoted", not os.path.exists(warm))
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": pkg_fixture("tamper")}]})).run(dict(claim, isTrunk=True))
        check("prepared/negative control: an ALTERED extracted file never reaches the warm cache — it is rebuilt from the verified archive",
              os.path.isfile(os.path.join(warm, "lib", "net10.0", "Demo.dll"))
              and open(os.path.join(warm, "lib", "net10.0", "Demo.dll"), "rb").read() == b"the-real-dll"
              and not os.path.exists(os.path.join(warm, "lib", "net10.0", "Extra.targets")))
        shutil.rmtree(os.path.join(cache, "nuget"))
        # what an EARLIER stage restored (present before the build stage started) is promoted by the build stage too
        host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [
            {"name": "t", "kind": "test", "command": pkg_fixture("good")},
            {"name": "a", "kind": "build", "command": "true"}]})).run(dict(claim, isTrunk=True))
        check("prepared/a later trunk build promotes what an earlier stage restored", os.path.isfile(os.path.join(warm, ".nupkg.metadata")))
        shutil.rmtree(os.path.join(cache, "nuget"))
        w_dir = os.path.join(tmp, "marker")
        os.makedirs(w_dir)
        victim = os.path.join(tmp, "victim")
        open(victim, "w").write("host file")
        os.symlink(victim, os.path.join(w_dir, ".source-key"))
        write_marker(w_dir, ".source-key", "k")
        check("prepared-inputs/a marker planted as a symlink is REPLACED, never written through",
              open(victim).read() == "host file" and not os.path.islink(os.path.join(w_dir, ".source-key"))
              and open(os.path.join(w_dir, ".source-key")).read() == "k\n")
        posted.clear()
        hnone = host(api_ev(), env=keyed_env, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]}))
        hnone.run(dict(claim, isTrunk=True))
        check("prepared/a run that restored nothing says nothing", not [e for _, evs in posted for e in evs if e.get("code") == "MWPREP001"])

        # ── 7b'. the fallback folder every stage is handed EXISTS in every stage, and the build and test stages agree ──
        # The defect: a build stage restored with NUGET_FALLBACK_PACKAGES=<cache>/nuget, its project.assets.json named the
        # folder, and a test stage that did not declare `needs: ["nuget"]` was not bound it → MSB4018 ResolvePackageAssets
        # "Unable to find fallback package folder". The build writes what NuGet would record; the test stage resolves it.
        fresh_nuget = os.path.join(tmp, "fresh-nuget-cache")

        def run_verdict():
            return [e for _, evs in posted for e in evs if e["kind"] == "run-finished"][-1:]
        assets_ = os.path.join(tmp, "assets-fallback")
        seen_ro: list = []

        class _RecordingSandbox(_EnvSandbox):
            def argv(self, command, checkout, outputs, env, readonly=(), writable=(), mount=None, binds=()):
                seen_ro.append((env.get("MW_BUILD_STAGE"), list(readonly), env.get("NUGET_FALLBACK_PACKAGES")))
                return super().argv(command, checkout, outputs, env, readonly, writable, mount, binds)
        posted.clear()
        h_fb = Host(dict(keyed_env, MW_BUILD_CACHE=fresh_nuget), api_ev(), log=lambda s: None, sandbox=_RecordingSandbox(),
                    leftovers=lambda: [], sleep=lambda s: None, pull=pull,
                    git_runner=lambda args, env_: (0, "e" * 40 + "\n" if "rev-parse" in args else ""),
                    checkout=checkout_with({"stages": [
                        {"name": "build", "kind": "build", "needs": ["nuget"],
                         "command": f"echo \"$NUGET_FALLBACK_PACKAGES\" > {assets_}"},
                        {"name": "test-shard-0", "kind": "test", "command": f"test -d \"$(cat {assets_})\""}]}))
        h_fb.run(claim)
        fb_dir = os.path.join(fresh_nuget, "nuget")
        check("fallback/an ABSENT warm cache: the folder every stage names exists before the first stage, and the test "
              "stage resolves what the build recorded", run_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}]
              and os.path.isdir(fb_dir), run_verdict())
        check("fallback/the build and EVERY test stage are bound the folder they are handed — `needs` declared or not",
              len(seen_ro) == 2 and all(fb_ == fb_dir and fb_dir in ro_ for _, ro_, fb_ in seen_ro), seen_ro)
        av_fb = Sandbox("bwrap", []).argv("x", "/w/s", "/w/o", sandbox_env({}, {}, fresh_nuget),
                                          readonly=h_fb.stage_readonly("MeshWeaver.Plugins", {"needs": []}))
        check("fallback/the bwrap argv of a stage that declares no needs binds the fallback folder read-only",
              f"--ro-bind-try {fb_dir} {fb_dir}" in " ".join(av_fb) and f"--bind {fb_dir} " not in " ".join(av_fb), av_fb)

        # ── 7b''. the warm NuGet cache is filled from the DEFAULT BRANCH's tip, without a trunk run, once per key ──
        warm_cache = os.path.join(tmp, "warm-cache")
        warm_runs = os.path.join(tmp, "warm-restores")
        pkg_good = pkg_fixture("good")
        # the run's stage behaves as NuGet does: it restores a package into its own folder only when the fallback lacks it
        nuget_like = ("if [ ! -f \"$NUGET_FALLBACK_PACKAGES/demo.pkg/1.0.0/.nupkg.metadata\" ]; then " + pkg_good + "; fi")
        pr_doc = {"stages": [{"name": "build", "kind": "build", "command": nuget_like},
                             {"name": "test-shard-0", "kind": "test", "command": "true"}]}

        def tip_git(tip_doc, listing="100644 blob " + "a" * 40 + "\tsrc/A/A.csproj\n", tip_="d" * 40, log_=None):
            def g(args, env_):
                if log_ is not None:
                    log_.append(args)
                if "rev-parse" in args:
                    return 0, tip_ + "\n"
                if "show" in args:
                    return (0, json.dumps(tip_doc)) if tip_doc is not None else (128, "fatal: path does not exist")
                if "ls-tree" in args:
                    return 0, listing
                if args[0] == "clone" and "--shared" in args:
                    os.makedirs(os.path.join(args[-1], ".github"))
                return 0, ""
            return g
        tip_restore = {"stages": pr_doc["stages"],
                       "restore": {"command": f"echo restored >> {warm_runs}; " + pkg_good, "timeoutMinutes": 10}}
        wenv = dict(keyed_env, MW_BUILD_CACHE=warm_cache)

        def n_warm():
            return len(open(warm_runs).read().splitlines()) if os.path.exists(warm_runs) else 0

        def codes(c_):
            return [e for _, evs in posted for e in evs if e.get("code") == c_]
        posted.clear()
        host(api_ev(), env=wenv, git_runner=tip_git(tip_restore), checkout=checkout_with(pr_doc)).run(claim)
        warm_pkg = os.path.join(warm_cache, "nuget", "demo.pkg", "1.0.0")
        check("warm/an EMPTY cache, NO trunk run: the first PR run fills it from the default branch's tip, once",
              n_warm() == 1 and os.path.isfile(os.path.join(warm_pkg, ".nupkg.metadata"))
              and nuget_packages(os.path.join(warm_cache, "nuget")) == {"demo.pkg/1.0.0"}, (n_warm(), run_verdict()))
        check("warm/... before its stages ran, so even that run restores nothing per run", not codes("MWPREP001"), codes("MWPREP001"))
        def marks(c_):
            d_ = os.path.join(c_, "nuget-warm", "MeshWeaver.Plugins")
            return len(os.listdir(d_)) if os.path.isdir(d_) else 0
        check("warm/... and the key is marked", marks(warm_cache) == 1, marks(warm_cache))
        check("warm/the run is green", run_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}], run_verdict())
        posted.clear()
        host(api_ev(), env=wenv, git_runner=tip_git(tip_restore), checkout=checkout_with(pr_doc)).run(claim)
        lines_w = [e.get("text", "") for _, evs in posted for e in evs if e["kind"] == "line"]
        check("warm/the SECOND run restores nothing — neither the server nor the run",
              n_warm() == 1 and not codes("MWPREP001") and any("holds the default branch's restore" in t_ for t_ in lines_w),
              (n_warm(), codes("MWPREP001"), lines_w))
        # MODE MIGRATION (review of #109): a package an OLDER runner rebuilt lives ONLY in the cache — the stage reads it
        # in place from the fallback folder, so it never reaches `restored`, and the held warm key skips promotion. The
        # run still repairs its modes before any stage (a 0644 protoc stays unexecutable otherwise).
        legacy_ = os.path.join(warm_pkg, "lib", "net10.0", "Demo.dll")
        os.remove(os.path.join(warm_pkg, NUGET_MATERIALIZED_MARK))
        os.chmod(legacy_, 0o644)
        posted.clear()
        host(api_ev(), env=wenv, git_runner=tip_git(tip_restore), checkout=checkout_with(pr_doc)).run(claim)
        check("warm/a cache-only package an older runner rebuilt (held key, nothing restored) has its modes repaired "
              "and is marked before the stages run",
              n_warm() == 1 and os.stat(legacy_).st_mode & 0o100 != 0
              and os.path.isfile(os.path.join(warm_pkg, NUGET_MATERIALIZED_MARK)), (n_warm(), oct(os.stat(legacy_).st_mode)))
        # ... and it runs UNDER THE LEASE (review of #109): a slow migration of a large cache must be beaten for. The
        # stand-in migration records what renews the lease while it runs — the run's heartbeat thread alive, with the
        # run's sink as its target; that such a heartbeat renews the lease every period is what the lease/ cases prove,
        # so however long the migration takes, the claim is kept.
        seen_lease_: list = []
        h_lease_ = host(api_ev(), env=wenv, git_runner=tip_git(tip_restore), checkout=checkout_with(pr_doc))
        real_repair_ = globals()["repair_nuget_cache"]

        def slow_repair_(root_, log_):
            hb_ = h_lease_.heartbeat
            seen_lease_.append(hb_ is not None and hb_.thread.is_alive() and hb_.target is not None and not hb_.stop.is_set())
            return real_repair_(root_, log_)
        globals()["repair_nuget_cache"] = slow_repair_
        try:
            h_lease_.run(claim)
        finally:
            globals()["repair_nuget_cache"] = real_repair_
        check("warm/the cache migration runs once per run, inside the lease: the heartbeat is alive and targets the "
              "run's sink while it runs", seen_lease_ == [True], seen_lease_)
        posted.clear()
        host(api_ev(), env=wenv, git_runner=tip_git(tip_restore, listing="100644 blob " + "b" * 40 + "\tsrc/A/A.csproj\n"),
             checkout=checkout_with(pr_doc)).run(claim)
        check("warm/main changed a project file: a new key, restored once more (what the cache holds is kept)",
              n_warm() == 2 and marks(warm_cache) == 2, (n_warm(), marks(warm_cache)))
        check("warm/the key: a project file's blob, the command and the platform change it; a source file does not",
              nuget_warm_key("100644 blob " + "a" * 40 + "\tsrc/A/A.cs\n", "c", "p") == nuget_warm_key("", "c", "p")
              and nuget_warm_key("100644 blob " + "a" * 40 + "\tDirectory.Packages.props\n", "c", "p") != nuget_warm_key("", "c", "p")
              and nuget_warm_key("100644 blob " + "a" * 40 + "\tNuGet.Config\n", "c", "p") != nuget_warm_key("", "c", "p")
              and nuget_warm_key("", "c", "p") != nuget_warm_key("", "c2", "p") != nuget_warm_key("", "c", "p2"))
        # a failed tip restore is SAID and never reds the run; nothing is marked, so the next run tries again
        posted.clear()
        failing_tip = dict(tip_restore, restore={"command": f"echo restored >> {warm_runs}; exit 3"})
        host(api_ev(), env=dict(wenv, MW_BUILD_CACHE=os.path.join(tmp, "warm-fail")), git_runner=tip_git(failing_tip),
             checkout=checkout_with(pr_doc)).run(claim)
        check("warm/a failed tip restore is SAID (MWPREP003), marks nothing, and the run still restores per run, green",
              len(codes("MWPREP003")) == 1 and "exit 3" in codes("MWPREP003")[0]["message"]
              and not os.path.exists(os.path.join(tmp, "warm-fail", "nuget-warm")) and codes("MWPREP001")
              and run_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}], (codes("MWPREP003"), run_verdict()))
        posted.clear()
        host(api_ev(), env=dict(wenv, MW_BUILD_CACHE=os.path.join(tmp, "warm-none")), git_runner=tip_git(None),
             checkout=checkout_with(pr_doc)).run(claim)
        check("warm/a tip that declares no `restore`: said, nothing restored for the server",
              n_warm() == 3 and any("declares no `restore`" in e.get("text", "") for _, evs in posted for e in evs), n_warm())
        check("warm/the budget reserves the tip's restore only when it will run",
              run_budget_seconds([{"timeoutMinutes": 10}], 10 ** 6, warm_minutes=5) == 15 * 60 + RUN_MARGIN_SECONDS
              and run_budget_seconds([{"timeoutMinutes": 10}], 10 ** 6) == 10 * 60 + RUN_MARGIN_SECONDS)
        # NEGATIVE CONTROLS (the trust boundary): a PR's own `restore` declaration is never executed, and neither its
        # per-run restore nor a package source its tree adds ever writes the shared cache.
        # The PR's package is VALID (it would pass verification), so only the trust boundary keeps it out of the cache.
        poison_ = os.path.join(tmp, "poison-pkg")
        shutil.rmtree(poison_, ignore_errors=True)
        shutil.copytree(os.path.join(tmp, "pkg-good", "demo.pkg", "1.0.0"), os.path.join(poison_, "evil.pkg", "6.6.6"))
        for ext_ in ("nupkg", "nupkg.sha512"):
            os.rename(os.path.join(poison_, "evil.pkg", "6.6.6", f"demo.pkg.1.0.0.{ext_}"),
                      os.path.join(poison_, "evil.pkg", "6.6.6", f"evil.pkg.6.6.6.{ext_}"))
        check("warm/negative control: the PR's package would pass verification (the case has a subject)",
              nuget_package_problem(os.path.join(poison_, "evil.pkg", "6.6.6"), "evil.pkg", "6.6.6") is None)
        pr_ran = os.path.join(tmp, "pr-restore-ran")
        evil_pr = {"stages": [{"name": "build", "kind": "build", "command": f"cp -R {poison_}/. \"$NUGET_PACKAGES\"/"}],
                   "restore": {"command": f"echo ran > {pr_ran}; cp -R {poison_}/. \"$NUGET_PACKAGES\"/"}}
        neg_cache = os.path.join(tmp, "warm-neg")
        posted.clear()
        host(api_ev(), env=dict(wenv, MW_BUILD_CACHE=neg_cache), git_runner=tip_git(None), checkout=checkout_with(evil_pr)).run(claim)
        check("warm/negative control: the PR's per-run restore DID fetch its package (the case has a subject)",
              any("evil.pkg/6.6.6" in w_["message"] for w_ in codes("MWPREP001")), codes("MWPREP001"))
        check("warm/negative control: a PR's own `restore` declaration is never executed", not os.path.exists(pr_ran))
        check("warm/negative control: ... and a PR's restore never writes the shared cache",
              nuget_packages(os.path.join(neg_cache, "nuget")) == set() and not os.path.exists(os.path.join(neg_cache, "nuget-warm")))
        posted.clear()
        host(api_ev(), env=dict(wenv, MW_BUILD_CACHE=neg_cache), git_runner=tip_git(tip_restore), checkout=checkout_with(evil_pr)).run(claim)
        check("warm/negative control: with the tip declaring a restore, what is installed is the TIP's closure, never the PR's",
              nuget_packages(os.path.join(neg_cache, "nuget")) == {"demo.pkg/1.0.0"} and not os.path.exists(pr_ran),
              nuget_packages(os.path.join(neg_cache, "nuget")))
        # A host-side failure while warming (a git that times out, an I/O error) is SAID and the run still gets its verdict.
        def raising_git(stage_, exc_):
            inner_ = tip_git(tip_restore)

            def g(args, env_):
                if stage_ in args:
                    raise exc_
                return inner_(args, env_)
            return g
        for name_, git_ in (("planning", raising_git("show", subprocess.TimeoutExpired(["git", "show"], 900))),
                            ("checkout", raising_git("--shared", subprocess.TimeoutExpired(["git", "clone"], 900))),
                            ("checkout-io", raising_git("--detach", OSError(5, "Input/output error")))):
            posted.clear()
            raise_cache_ = os.path.join(tmp, f"warm-raise-{name_}")
            outcome_ = host(api_ev(), env=dict(wenv, MW_BUILD_CACHE=raise_cache_), git_runner=git_,
                            checkout=checkout_with(pr_doc)).run(claim)
            said_ = codes("MWPREP003") if name_ != "planning" else \
                [e for _, evs in posted for e in evs if "could not be read" in e.get("text", "")]
            check(f"warm/a host-side failure in warm {name_} never ends the worker without a verdict: said, and the run is green",
                  outcome_ == "ran" and run_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}]
                  and len(said_) == 1 and not os.path.exists(os.path.join(raise_cache_, "nuget-warm")),
                  (outcome_, run_verdict(), said_))
        wlog: list = []
        posted.clear()
        host(api_ev(), env=dict(wenv, MW_BUILD_CACHE=os.path.join(tmp, "warm-src")), git_runner=tip_git(tip_restore, log_=wlog),
             checkout=checkout_with(evil_pr)).run(claim)
        check("warm/the declaration and the key are read from the MIRROR at the tip, never from the run's tree",
              any("show" in a_ and a_[-1] == "d" * 40 + ":.github/build-stages.json" for a_ in wlog)
              and any("ls-tree" in a_ and a_[-1] == "d" * 40 for a_ in wlog), wlog)
        # A repository whose projects REFERENCE THE PLATFORM (MeshWeaver.Plugins: -p:MeshWeaverRoot="$MW_PLATFORM_SOURCE/"
        # -p:MeshWeaverRefs="$MeshWeaverRefs") restores only with the platform's source and the reference set — and restore
        # writes obj/ into the platform's projects, so the source must be a WRITABLE copy, never the image's read-only tree.
        wp_src = os.path.join(tmp, "warm-platform-src")
        wp_refs = os.path.join(tmp, "warm-platform-refs")
        wp_crefs = os.path.join(tmp, "warm-platform-crefs")
        for d_ in (wp_src, wp_refs, wp_crefs):
            os.makedirs(d_, exist_ok=True)
        open(os.path.join(wp_src, "Directory.Build.props"), "w").write("<Project/>")
        wp_seen = os.path.join(tmp, "warm-platform-seen")
        plugins_like = {"stages": pr_doc["stages"], "restore": {"timeoutMinutes": 10, "command": (
            f"set -e; printf '%s\\n' \"${{MW_PLATFORM_SOURCE:-}}\" \"${{MeshWeaverRefs:-}}\" \"${{MW_BUILD_COMPILE_REFS:-}}\" "
            f"\"${{MW_BUILD_TESTER_DIGEST:-}}\" > {wp_seen}; "
            "test -f \"$MW_PLATFORM_SOURCE/Directory.Build.props\"; test -d \"$MeshWeaverRefs\"; test -d \"$MW_BUILD_COMPILE_REFS\"; "
            "mkdir -p \"$MW_PLATFORM_SOURCE/src/obj\"; echo assets > \"$MW_PLATFORM_SOURCE/src/obj/project.assets.json\"; "
            + pkg_good)}}
        # the image's platform tree names its commit; this runner cannot overlay it here, so its writable VIEW is a cold
        # worktree of the platform's master (the local origin of the §7.3b fixtures) — never a copy
        open(os.path.join(wp_src, ".source-commit"), "w").write(plat_commit + "\n")
        wp_env = dict(wenv, MW_PLATFORM_SOURCE=wp_src, MeshWeaverRefs=wp_refs, MW_BUILD_COMPILE_REFS=wp_crefs, MW_BUILD_GIT_BASE=plat_base)
        wp_cache = os.path.join(tmp, "warm-platform-cache")
        posted.clear()
        host(api_ev(), env=dict(wp_env, MW_BUILD_CACHE=wp_cache), git_runner=tip_git(plugins_like),
             checkout=checkout_with(pr_doc)).run(claim)
        seen_wp = open(wp_seen).read().split("\n") if os.path.exists(wp_seen) else []
        check("warm/a restore whose projects reference the platform gets the platform source and the reference set: it "
              "restores, its packages are promoted and the key is marked",
              not codes("MWPREP003") and nuget_packages(os.path.join(wp_cache, "nuget")) == {"demo.pkg/1.0.0"}
              and marks(wp_cache) == 1 and seen_wp[1:4] == [wp_refs, wp_crefs, keyed_env["MW_BUILD_TESTER_DIGEST"]],
              (codes("MWPREP003"), seen_wp))
        check("warm/... the platform source it gets is a WRITABLE VIEW (never a copy): restore's obj/ never lands in the "
              "image's tree, and the view is gone afterwards",
              bool(seen_wp) and seen_wp[0] not in ("", wp_src) and not os.path.exists(os.path.join(wp_src, "src"))
              and not os.path.exists(seen_wp[0]), seen_wp)
        # NEGATIVE CONTROL: the same restore on a host whose image carries no platform source cannot restore — so the
        # case above has a subject — and that is SAID (MWPREP003), marks nothing, and never reds the run.
        posted.clear()
        no_src_cache = os.path.join(tmp, "warm-platform-none")
        host(api_ev(), env=dict({k: v for k, v in wp_env.items() if k != "MW_PLATFORM_SOURCE"}, MW_BUILD_CACHE=no_src_cache),
             git_runner=tip_git(plugins_like), checkout=checkout_with(pr_doc)).run(claim)
        check("warm/negative control: without the platform source the same restore fails, said (MWPREP003), nothing marked",
              len(codes("MWPREP003")) == 1 and marks(no_src_cache) == 0
              and run_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}], (codes("MWPREP003"), run_verdict()))

        # ── 7c. PREPARED INPUTS: built from the repository's own sources ONCE per (platform, key), installed persistently ──
        for bad_ in ({"prepared": {}}, {"prepared": [{"name": "shelf", "env": "MW_SECRET", "key": "k", "build": "b"}]},
                     {"prepared": [{"name": "Shelf", "env": "MW_BUILD_MODULE_LIBS", "key": "k", "build": "b"}]},
                     {"prepared": [{"name": "a", "env": "MW_BUILD_MODULE_LIBS", "key": "k", "build": "b"},
                                   {"name": "b", "env": "MW_BUILD_MODULE_LIBS", "key": "k", "build": "b"}]},
                     {"prepared": [{"name": "shelf", "env": "MW_BUILD_MODULE_LIBS", "key": "k"}]},
                     {"prepared": [{"name": "shelf", "env": "MW_BUILD_MODULE_LIBS", "key": "k", "build": "b", "timeoutMinutes": 90}]}):
            try:
                validate_prepared(bad_)
                check(f"prepared-inputs/refuses the declaration {bad_}", False, "accepted")
            except Refusal:
                pass
        check("prepared-inputs/both names reach a stage through the allow-list",
              set(PREPARED_ENV) <= set(sandbox_env({k: "/x" for k in PREPARED_ENV}, {}, "/cache")))
        psrc = os.path.join(tmp, "platform-src")
        os.makedirs(psrc, exist_ok=True)
        builds = os.path.join(tmp, "prep-builds")
        seen_in = os.path.join(tmp, "prep-seen")
        k1, k2 = "1" * 64, "2" * 64

        def prep_doc(key):
            return {"prepared": [{"name": "shelf", "env": "MW_BUILD_MODULE_LIBS", "key": f"echo noise; echo {key}",
                                  "build": f"echo built >> {builds}; echo dll > \"$MW_PREPARE_OUT/Shelf.dll\"; "
                                           "test -d \"$MW_PLATFORM_SOURCE\" && mkdir -p \"$MW_PLATFORM_SOURCE/src/obj\""}],
                    "stages": [{"name": "a", "kind": "build",
                                "command": f"cat \"$MW_BUILD_MODULE_LIBS/.source-key\" > {seen_in}; echo \"$MW_BUILD_MODULE_LIBS\" >> {seen_in}; "
                                           "test -f \"$MW_BUILD_MODULE_LIBS/Shelf.dll\""}]}
        open(os.path.join(psrc, ".source-commit"), "w").write(plat_commit + "\n")
        penv = dict(keyed_env, MW_PLATFORM_SOURCE=psrc, MW_BUILD_GIT_BASE=plat_base)

        def n_builds():
            return len(open(builds).read().splitlines()) if os.path.exists(builds) else 0

        def last_verdict():
            return [e for _, evs in posted for e in evs if e["kind"] == "run-finished"][-1:]
        store1 = prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], k1)
        posted.clear()
        host(api_ev(), env=penv, checkout=checkout_with(prep_doc(k1))).run(dict(claim, isTrunk=True))
        seen_ = open(seen_in).read().split()
        check("prepared-inputs/main moved: a TRUNK run builds it ONCE and installs it persistently",
              n_builds() == 1 and os.path.isfile(os.path.join(store1, ".complete")) and seen_ == [k1, store1], (n_builds(), seen_))
        check("prepared-inputs/the build writes obj/ into its OWN view of the platform source (never a copy), never the image's",
              not os.path.exists(os.path.join(psrc, "src")))
        check("prepared-inputs/the run is green on the installed input", last_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}], last_verdict())
        posted.clear()
        host(api_ev(), env=penv, checkout=checkout_with(prep_doc(k1))).run(claim)
        check("prepared-inputs/the next run (a PR, same sources) prepares NOTHING and reads the installed copy",
              n_builds() == 1 and open(seen_in).read().split() == [k1, store1], (n_builds(), open(seen_in).read()))
        posted.clear()
        host(api_ev(), env=dict(keyed_env), checkout=checkout_with(prep_doc(k1))).run(claim)
        check("prepared-inputs/an installed input needs no platform source", n_builds() == 1
              and last_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}], last_verdict())
        posted.clear()
        host(api_ev(), env=penv, checkout=checkout_with(prep_doc(k2))).run(claim)
        warn2 = [e for _, evs in posted for e in evs if e.get("code") == "MWPREP002"]
        check("prepared-inputs/a PR that changes its sources builds it for THIS run only, said",
              n_builds() == 2 and len(warn2) == 1 and open(seen_in).read().split()[0] == k2, (n_builds(), warn2))
        check("prepared-inputs/negative control: a PR never installs into the shared store",
              not os.path.exists(prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], k2)))
        check("prepared-inputs/another platform is another install",
              prepared_store(cache, "r", "shelf", "3.1.1", k1) != prepared_store(cache, "r", "shelf", "3.1.2", k1))
        posted.clear()
        host(api_ev(), env=dict(keyed_env), checkout=checkout_with(prep_doc(k2))).run(dict(claim, isTrunk=True))
        rf2 = last_verdict()
        check("prepared-inputs/not installed and no platform source: RED, naming it — never a stage run against nothing",
              rf2 and rf2[0]["succeeded"] is False and "MW_PLATFORM_SOURCE" in (rf2[0]["error"] or ""), rf2)
        posted.clear()
        host(api_ev(), env=penv, checkout=checkout_with(dict(prep_doc(k1), prepared=[dict(prep_doc(k1)["prepared"][0], key="echo not-a-key")]))).run(claim)
        rf3 = last_verdict()
        check("prepared-inputs/a key command that answers no key is RED", rf3 and rf3[0]["succeeded"] is False and "source key" in (rf3[0]["error"] or ""), rf3)
        posted.clear()
        host(api_ev(), env=penv, checkout=checkout_with(dict(prep_doc(k1), prepared=[dict(prep_doc(k1)["prepared"][0], key=f"echo {k1}; echo a-diagnostic >&2")]))).run(claim)
        check("prepared-inputs/a key command that also writes stderr still answers its stdout key (installed, nothing built)",
              n_builds() == 2 and last_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}], (n_builds(), last_verdict()))
        fresh_ = os.path.join(tmp, "fresh-cache")
        posted.clear()
        host(api_ev(), env=dict(penv, MW_BUILD_CACHE=fresh_), checkout=checkout_with(prep_doc(k2))).run(claim)
        check("prepared-inputs/a FRESH host creates the NuGet fallback folder before the first preparation",
              os.path.isdir(os.path.join(fresh_, "nuget")) and n_builds() == 3, (n_builds(), last_verdict()))
        posted.clear()
        host(api_ev(), env=penv, checkout=checkout_with(dict(prep_doc("4" * 64), prepared=[dict(prep_doc("4" * 64)["prepared"][0],
             build="echo dll > \"$MW_PREPARE_OUT/Shelf.dll\"; chmod 000 \"$MW_PREPARE_OUT\"")]))).run(dict(claim, isTrunk=True))
        rf5 = last_verdict()
        for r_, ds_, _ in os.walk(tmp):
            for d_ in ds_:
                if not os.access(os.path.join(r_, d_), os.R_OK | os.X_OK):
                    os.chmod(os.path.join(r_, d_), 0o700)
        # Root reads past file modes, so as root the build CANNOT leave its output unreadable and this case has no
        # subject: it is red there, naming why, never a vacuous pass (the image runs this self-test as uid 1654).
        check("prepared-inputs/a build that leaves its output UNREADABLE is a RED verdict naming it, never a crash",
              os.geteuid() != 0 and rf5 and rf5[0]["succeeded"] is False and "shelf" in (rf5[0]["error"] or "") and "cannot be" in (rf5[0]["error"] or ""),
              "run this self-test as an unprivileged uid: root reads a chmod-000 directory" if os.geteuid() == 0 else rf5)
        posted.clear()
        host(api_ev(), env=penv, checkout=checkout_with(dict(prep_doc("3" * 64), prepared=[dict(prep_doc("3" * 64)["prepared"][0], build="exit 7")]))).run(dict(claim, isTrunk=True))
        rf4 = last_verdict()
        check("prepared-inputs/a failed build is RED and installs nothing", rf4 and rf4[0]["succeeded"] is False
              and not os.path.exists(prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], "3" * 64)), rf4)
        # ── the server is prepared WITHOUT a trunk run: the default branch's tip is built by the host, once ──
        # Trunk runs need not reach the host at all, so a missing (platform, key) is installed by the FIRST run that needs
        # it — from the default branch's tip (its tree, its key and build commands), never from the run's own content.
        def default_doc(key_, declare=True):
            d_ = prep_doc(key_)
            if not declare:
                return {"stages": d_["stages"]}
            # the default branch's build writes the TREE it ran in, so what was installed names where it was built
            return dict(d_, prepared=[dict(d_["prepared"][0], build=f"echo built >> {builds}; cp tree \"$MW_PREPARE_OUT/Shelf.dll\"")])

        def git_with_default(doc_, tip_="d" * 40, resolves=True):
            def g(args, env_):
                if "rev-parse" in args:
                    return (0, tip_ + "\n") if resolves else (128, "fatal: no HEAD")
                if args[0] == "clone" and "--shared" in args:
                    dest_ = args[-1]
                    os.makedirs(os.path.join(dest_, ".github"))
                    with open(os.path.join(dest_, ".github", "build-stages.json"), "w") as fh:
                        json.dump(doc_, fh)
                    with open(os.path.join(dest_, "tree"), "w") as fh:
                        fh.write("default-branch")
                return 0, ""
            return g

        def shelf_of(store_):
            return open(os.path.join(store_, "Shelf.dll")).read().strip() if os.path.isfile(os.path.join(store_, "Shelf.dll")) else None
        k5, k6, k7 = "5" * 64, "6" * 64, "7" * 64
        store5 = prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], k5)
        before_ = n_builds()
        posted.clear()
        host(api_ev(), env=penv, git_runner=git_with_default(default_doc(k5)), checkout=checkout_with(prep_doc(k5))).run(claim)
        lines_ = [e.get("text", "") for _, evs in posted for e in evs if e["kind"] == "line"]
        check("prepared-inputs/missing for this platform, NO trunk run: the first PR run prepares it ONCE and installs it",
              n_builds() == before_ + 1 and os.path.isfile(os.path.join(store5, ".complete"))
              and open(seen_in).read().split() == [k5, store5]
              and any("default branch's tip dddddddddddd" in t_ for t_ in lines_), (n_builds() - before_, lines_))
        check("prepared-inputs/what is installed was built from the DEFAULT BRANCH's tree with ITS command, never the PR's",
              shelf_of(store5) == "default-branch", shelf_of(store5))
        check("prepared-inputs/the run is green on the copy it installed", last_verdict() == [{"kind": "run-finished", "succeeded": True, "error": None}], last_verdict())
        check("prepared-inputs/a PR run that installs from the default branch says no MWPREP002",
              not [e for _, evs in posted for e in evs if e.get("code") == "MWPREP002"])
        posted.clear()
        host(api_ev(), env=penv, git_runner=git_with_default(default_doc(k5)), checkout=checkout_with(prep_doc(k5))).run(claim)
        check("prepared-inputs/the second run reads it — no rebuild", n_builds() == before_ + 1
              and open(seen_in).read().split() == [k5, store5], n_builds() - before_)
        # negative control: a PR that CHANGED the sources (its key differs from the tip's) builds per run, said, and
        # writes NEITHER key's shared copy — at most one build per run, and never one of the PR's bytes installed
        posted.clear()
        host(api_ev(), env=penv, git_runner=git_with_default(default_doc(k7)), checkout=checkout_with(prep_doc(k6))).run(claim)
        warn6 = [e for _, evs in posted for e in evs if e.get("code") == "MWPREP002"]
        check("prepared-inputs/negative control: a PR changing the sources builds PER RUN, said, naming the tip's key",
              n_builds() == before_ + 2 and len(warn6) == 1 and k7[:12] in warn6[0]["message"]
              and open(seen_in).read().split()[0] == k6, (n_builds() - before_, warn6))
        check("prepared-inputs/negative control: ... and writes NO shared copy, neither its own key nor the tip's",
              not os.path.exists(prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], k6))
              and not os.path.exists(prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], k7)))
        # negative control: a PR whose key command LIES (answers the tip's key over changed sources) never installs
        # its own bytes — what it reads is the default branch's build
        k8 = "8" * 64
        store8 = prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], k8)
        lying_ = dict(prep_doc(k8), prepared=[dict(prep_doc(k8)["prepared"][0], build=f"echo built >> {builds}; echo forged > \"$MW_PREPARE_OUT/Shelf.dll\"")])
        posted.clear()
        host(api_ev(), env=penv, git_runner=git_with_default(default_doc(k8)), checkout=checkout_with(lying_)).run(claim)
        check("prepared-inputs/negative control: a PR that lies about its key never installs its own bytes",
              shelf_of(store8) == "default-branch", shelf_of(store8))
        # a tip that does not declare the input: per run, said — nothing installed
        k9 = "9" * 64
        posted.clear()
        host(api_ev(), env=penv, git_runner=git_with_default(default_doc(k9, declare=False)), checkout=checkout_with(prep_doc(k9))).run(claim)
        check("prepared-inputs/a tip that does not declare the input: the PR builds per run, nothing installed",
              [e for _, evs in posted for e in evs if e.get("code") == "MWPREP002"]
              and not os.path.exists(prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], k9)))
        # TWO inputs, the PR's FIRST built per run: its command writes a package into the folder it is handed. The
        # default branch's build of the SECOND input must not see it — what it installs serves every later run.
        kp, kq = "b" * 64, "c" * 64
        storeq = prepared_store(cache, "MeshWeaver.Plugins", "shelf", keyed_env["MW_BUILD_PLATFORM_VERSION"], kq)
        planted_, read_ = os.path.join(tmp, "prep-planted"), os.path.join(tmp, "prep-default-nuget")
        evil_ = {"name": "evil", "env": "MW_BUILD_EXTENSIONS", "key": f"echo {kp}",
                 "build": f"mkdir -p \"$NUGET_PACKAGES/poison\"; echo \"$NUGET_PACKAGES\" > {planted_}; "
                          "echo x > \"$MW_PREPARE_OUT/E.dll\""}
        two_pr_ = dict(prep_doc(kq), prepared=[evil_, prep_doc(kq)["prepared"][0]])
        tip_doc_ = dict(default_doc(kq), prepared=[dict(default_doc(kq)["prepared"][0],
                        build=f"echo \"$NUGET_PACKAGES\" > {read_}; cp tree \"$MW_PREPARE_OUT/Shelf.dll\"; "
                              "if [ -e \"$NUGET_PACKAGES/poison\" ]; then echo poisoned >> \"$MW_PREPARE_OUT/Shelf.dll\"; fi")])
        posted.clear()
        host(api_ev(), env=penv, git_runner=git_with_default(tip_doc_), checkout=checkout_with(two_pr_)).run(claim)
        planted_at = open(planted_).read().strip() if os.path.exists(planted_) else None
        read_at = open(read_).read().strip() if os.path.exists(read_) else None
        check("prepared-inputs/negative control: the PR's per-run build DID plant a package (the case has a subject)",
              planted_at is not None and read_at is not None, (planted_at, read_at, last_verdict()))
        check("prepared-inputs/a default-branch build never reads a package folder a PR command wrote, and installs clean",
              planted_at != read_at and shelf_of(storeq) == "default-branch", (planted_at, read_at, shelf_of(storeq)))
        # the default branch cannot be resolved: RED, naming it — never a silent per-run build that installs nothing
        posted.clear()
        host(api_ev(), env=penv, git_runner=git_with_default(default_doc(k9), resolves=False),
             checkout=checkout_with(dict(prep_doc("a" * 64)))).run(dict(claim, mergeBaseSha="e" * 40))
        rfd = last_verdict()
        check("prepared-inputs/an unresolvable default branch is RED, naming it",
              rfd and rfd[0]["succeeded"] is False and "default branch" in (rfd[0]["error"] or ""), rfd)
        # the key covers sources, reference set, options and compiler
        kd = os.path.join(tmp, "keydir")
        os.makedirs(kd)
        with open(os.path.join(kd, "A.cs"), "w") as fh:
            fh.write("a")
        kenv = {"MW_BUILD_FRAMEWORK_IDENTITY": "c3e1", "MW_BUILD_PORTAL_DIGEST": "sha256:" + "8" * 64,
                "MW_BUILD_TESTER_DIGEST": "sha256:" + "7" * 64, "MW_PLUGINS_KEY": "p1"}
        spec = {"kind": "nodetype", "sources": ["A.cs"], "options": "--warnings-as-errors"}
        base_key = nodetype_cache_key(kd, spec, kenv)
        for what, sp, en in (("options", dict(spec, options=""), kenv), ("plugins key", spec, dict(kenv, MW_PLUGINS_KEY="p2")),
                             ("compiler", spec, dict(kenv, MW_BUILD_TESTER_DIGEST="sha256:" + "6" * 64)),
                             ("identity", spec, dict(kenv, MW_BUILD_FRAMEWORK_IDENTITY="c3e2"))):
            check(f"key/changes with the {what}", nodetype_cache_key(kd, sp, en) != base_key)
        with open(os.path.join(kd, "A.cs"), "w") as fh:
            fh.write("b")
        check("key/changes with a source", nodetype_cache_key(kd, spec, kenv) != base_key)
        for bad in ({"kind": "nodetype", "sources": ["../x"]}, {"kind": "nodetype", "sources": ["/etc/passwd"]},
                    {"kind": "nodetype", "sources": []}, {"kind": "elsewhere"}):
            try:
                cache_key_of(kd, bad, kenv)
                check(f"key/refuses {bad}", False, "keyed")
            except Refusal:
                pass
        check("key/a module key is core's module-build-key.py, run with the HOST's digests",
              module_cache_key(kd, {"kind": "module", "entry": {"package": "AI"}}, kenv,
                               run=lambda a: (0, "c" * 64 + "\n") if a[a.index("--tester-digest") + 1] == kenv["MW_BUILD_TESTER_DIGEST"]
                               and "module-build-key.py" in a[1] else (1, "wrong")) == "c" * 64)

        # 409 abandons; no stages declared is red; a stage past its timeout is killed and red
        a7 = _FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): [(409, None)]})
        check("run/409 abandons the run", host(a7, env=env_s, checkout=checkout_with(
            {"stages": [{"name": "a", "kind": "build", "command": "true"}]})).run(claim) == "lost")
        check("run/409 posts nothing after it", sum(1 for c in a7.calls if c[1].endswith("/events")) == 1, a7.calls)
        posted.clear()

        def empty_checkout(repo, sha, refs, dest, token, env_, log, **_):
            os.makedirs(dest)
        host(api_ev(), env=env_s, checkout=empty_checkout).run(dict(claim, stages=["build/modules"]))
        rf_ = [e for _, evs in posted for e in evs if e["kind"] == "run-finished"]
        check("run/undeclared stages are red", rf_ and rf_[0]["succeeded"] is False
              and "declares no build stages" in rf_[0]["error"], rf_)
        check("run/refusal posted to the claimed stage", posted and "/stages/modules/events" in posted[0][0], posted)
        posted.clear()
        t0 = time.monotonic()
        host(api_ev(), env=env_s, checkout=checkout_with({"stages": [
            {"name": "slow", "kind": "test", "command": "sleep 30", "timeoutMinutes": 0.02}]})).run(claim)
        fin = [e for _, evs in posted for e in evs if e["kind"] == "stage-finished"]
        check("run/timeout kills and reds", fin and fin[0]["state"] == "failed" and "timed out" in fin[0]["summary"]
              and time.monotonic() - t0 < 20, (fin, time.monotonic() - t0))
        posted.clear()
        host(api_ev(), env=dict(env_s, MW_BUILD_RUN_DEADLINE_SECONDS="0"), checkout=checkout_with({"stages": [
            {"name": "a", "kind": "build", "command": "true"}]})).run(claim)
        rf_ = [e for _, evs in posted for e in evs if e["kind"] == "run-finished"]
        check("run/past the run deadline is red", rf_ and rf_[0]["succeeded"] is False and "deadline" in rf_[0]["error"], rf_)
        # a stage that CLOSES its output and keeps running is still bound by its timeout
        posted.clear()
        t0 = time.monotonic()
        host(api_ev(), env=env_s, checkout=checkout_with({"stages": [
            {"name": "quiet", "kind": "test", "command": "exec >/dev/null 2>&1; sleep 30", "timeoutMinutes": 0.02}]})).run(claim)
        fin = [e for _, evs in posted for e in evs if e["kind"] == "stage-finished"]
        check("run/EOF is not exit: a silent stage past its timeout is killed and red",
              fin and fin[0]["state"] == "failed" and "timed out" in fin[0]["summary"] and time.monotonic() - t0 < 20,
              (fin, time.monotonic() - t0))

        # an API outage across stage-finished: held, retried, delivered BEFORE run-finished
        order_ev: list[str] = []
        outage = {"left": 9}

        def flaky(path, body):
            kinds = [e["kind"] for e in body["events"]]
            if "stage-finished" in kinds and outage["left"] > 0:
                outage["left"] -= 1
                return 503, None
            order_ev.extend(k for k in kinds if k in ("stage-finished", "run-finished"))
            return 202, None
        hf = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): flaky}), env=env_s,
                  checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]}))
        check("events/an outage over stage-finished is ridden out", hf.run(claim) == "ran", order_ev)
        check("events/stage-finished is delivered before run-finished", order_ev == ["stage-finished", "run-finished"], order_ev)
        down = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): [(503, None)]}), env=env_s,
                    checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]}))
        outcome = down.run(claim)
        sent_kinds = {e.get("kind") for c in down.api.calls if c[1].endswith("/events") for e in c[2]["events"]}
        check("events/a lasting outage posts NO run-finished over a missing stage verdict",
              outcome == "undelivered" and "run-finished" not in sent_kinds,
              (outcome, sent_kinds))
        # a run refused before any stage, with the API down, keeps the host alive: no artifacts
        # directory was ever made, so the workspace cleanup must not list one
        refused_down = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): [(503, None)]}),
                            env=env_s, checkout=empty_checkout)
        try:
            outcome = refused_down.run(claim)
        except OSError as e:
            outcome = f"raised {e!r}"
        check("events/a refusal during an outage is undelivered, never a crash", outcome == "undelivered", outcome)
        # a 409 while verdicts are held drops them: the next claim is not abandoned by the old run's 409s
        lost_left = {"n": 0}

        def held_then_lost(path, body):
            if "stage-finished" in [e["kind"] for e in body["events"]]:
                lost_left["n"] += 1
                return (503, None) if lost_left["n"] <= 6 else (409, None)
            return 202, None
        hl = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): held_then_lost}),
                  env=env_s, checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]}))
        check("events/a 409 over a held verdict abandons the run", hl.run(claim) == "lost")
        check("events/a lost run leaves no held verdict for the next claim", hl.held == [], hl.held)
        # successful batches never consume the failure budget: 1,000 events drain through 5 batches
        big = EventSink(_FakeApi({("POST", "/r/"): [(202, None)]}), "h", "r", "a" * 40, "s", "latest", lambda m: None,
                        sleep=lambda x: None)
        big.buf = [{"kind": "line", "text": str(i)} for i in range(1000)]
        check("events/many successful batches drain with a budget of one", big.flush(attempts=1) and not big.buf)

        # the checkout is bounded by the run's deadline
        try:
            default_checkout("R", "e" * 40, [], os.path.join(tmp, "late", "src"), None,
                             {"MW_BUILD_CACHE": os.path.join(tmp, "late-cache"), "PATH": "/usr/bin:/bin"},
                             lambda m: None, deadline=time.monotonic() - 1)
            check("checkout/a passed deadline is a Refusal", False, "checked out")
        except Refusal as e:
            check("checkout/names the deadline", "deadline" in str(e), e)
        # H9: artifacts go to the store, read back, and only then are their receipts posted
        store_dir = os.path.join(tmp, "store")
        os.makedirs(store_dir)
        store_calls: list[list[str]] = []
        store_timeouts: list = []

        def fake_store(argv, timeout=None, cancel=None):
            store_calls.append(argv)
            store_timeouts.append(timeout)
            verb = argv[2]
            if verb == "put":
                src = argv[argv.index("--file") + 1]
                h = _sha256_file(src)
                shutil.copy(src, os.path.join(store_dir, h))
                return 0, f"locator={argv[argv.index('--store') + 1]}#sha256={h}\n"
            if verb == "get":
                loc = argv[argv.index("--locator") + 1]
                shutil.copy(os.path.join(store_dir, loc.rsplit("=", 1)[1]), argv[argv.index("--out") + 1])
                return 0, ""
            return 1, "?"
        art_cmd = "mkdir -p $MW_BUILD_ARTIFACTS/Store && echo bundle > $MW_BUILD_ARTIFACTS/Store/Store-1.4.2.zip && echo '{}' > $MW_BUILD_ARTIFACTS/Store/Store.publication.json"
        posted.clear()
        ha = host(api_ev(), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=fake_store,
                  checkout=checkout_with({"stages": [{"name": "modules", "kind": "build", "command": art_cmd}]}))
        check("h9/a run with artifacts", ha.run(claim) == "ran")
        evs = [e for _, es in posted for e in es]
        arts = [e for e in evs if e["kind"] == "artifact"]
        check("h9/one receipt per artifact, with unit, key, sha256 and bytes",
              sorted(a["unit"] for a in arts) == ["Store/Store-1.4.2.zip", "Store/Store.publication.json"]
              and all(a["key"].endswith("#sha256=" + a["sha256"]) and a["bytes"] > 0 for a in arts), arts)
        # NEGATIVE CONTROL for MeshWeaver.Deploy#52: a GREEN stage's receipts are exactly as before, marked succeeded.
        check("h9/a green stage's receipts are marked succeeded", arts and all(a.get("stageState") == "succeeded" for a in arts), arts)
        kinds_order = [e["kind"] for e in evs if e["kind"] in ("artifact", "stage-finished")]
        check("h9/receipts precede the stage's verdict", kinds_order[-1] == "stage-finished" and kinds_order.count("artifact") == 2, kinds_order)
        check("h9/every upload was read back", [c[2] for c in store_calls] == ["put", "get", "put", "get"], [c[2] for c in store_calls])
        fin = [e for e in evs if e["kind"] == "stage-finished"]
        check("h9/the stage succeeded", fin and fin[-1]["state"] == "succeeded", fin)
        check("h9/every store call is bounded by the run budget left",
              store_timeouts and all(t is not None and 0 < t <= min(1800, ha.run_deadline) for t in store_timeouts), store_timeouts)
        # the hand-over is INSIDE the run budget: a store that outlasts it, or a budget already spent, fails the stage
        def slow_store(argv, timeout=None, cancel=None):
            raise subprocess.TimeoutExpired(argv, timeout)
        posted.clear()
        host(api_ev(), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=slow_store,
             checkout=checkout_with({"stages": [{"name": "modules", "kind": "build", "command": art_cmd}]})).run(claim)
        fin = [e for _, es in posted for e in es if e["kind"] == "stage-finished"]
        check("h9/a store call cut by the run deadline fails the stage, naming it", fin and fin[-1]["state"] == "failed"
              and "run deadline passed during the hand-over" in fin[-1]["summary"], fin)
        store_calls.clear()
        hd = host(api_ev(), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=fake_store)
        spent_dir = os.path.join(tmp, "spent-artifacts", "Store")
        os.makedirs(spent_dir)
        open(os.path.join(spent_dir, "x.zip"), "w").write("x")
        _r, problem = hd.hand_over_artifacts("R", "f" * 40, "modules", os.path.dirname(spent_dir), deadline=hd.clock() - 1)
        check("h9/a spent budget makes no store call and is a problem", not store_calls and problem and "run deadline" in problem, (store_calls, problem))
        # a store that reads back other bytes, and a missing store, fail the stage
        def lying_store(argv, timeout=None, cancel=None):
            if argv[2] == "get":
                open(argv[argv.index("--out") + 1], "w").write("other")
                return 0, ""
            return fake_store(argv, timeout)
        posted.clear()
        host(api_ev(), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=lying_store,
             checkout=checkout_with({"stages": [{"name": "modules", "kind": "build", "command": art_cmd}]})).run(claim)
        fin = [e for _, es in posted for e in es if e["kind"] == "stage-finished"]
        check("h9/a read-back that does not match fails the stage", fin and fin[-1]["state"] == "failed"
              and "did not read back" in fin[-1]["summary"], fin)
        posted.clear()
        host(api_ev(), env=env_s, store_runner=fake_store,
             checkout=checkout_with({"stages": [{"name": "modules", "kind": "build", "command": art_cmd}]})).run(claim)
        fin = [e for _, es in posted for e in es if e["kind"] == "stage-finished"]
        check("h9/artifacts with no store fail the stage", fin and fin[-1]["state"] == "failed" and "no artifact store" in fin[-1]["summary"], fin)
        # A red stage that rewrites an EXISTING artifact in place, same size, and restores its mtime (cp -p, touch -d,
        # timestamp normalisation) is still seen as a rewrite: ctime is part of the identity and no process can set it.
        qroot = tempfile.mkdtemp(prefix="h9q-")
        try:
            art = os.path.join(qroot, "a", "Keep.zip")
            os.makedirs(os.path.dirname(art))
            for n_, body in (("Keep.zip", b"good"), ("Same.zip", b"same")):
                with open(os.path.join(qroot, "a", n_), "wb") as fh:
                    fh.write(body)
            before_q = artifact_snapshot(qroot)
            st0 = os.stat(art)
            time.sleep(0.05)   # past the kernel's coarse timestamp tick, so the rewrite's ctime is a later one
            with open(art, "r+b") as fh:
                fh.write(b"evil")
            os.utime(art, ns=(st0.st_atime_ns, st0.st_mtime_ns))
            st1 = os.stat(art)
            check("h9/negative control: size, mtime and inode alone cannot see the restored-mtime rewrite",
                  (st1.st_size, st1.st_mtime_ns, st1.st_ino) == (st0.st_size, st0.st_mtime_ns, st0.st_ino), (st0, st1))
            moved_q = quarantine_artifacts(qroot, before_q, os.path.join(qroot, "..", os.path.basename(qroot) + "-q"))
            check("h9/a same-size rewrite with its mtime restored is quarantined; the untouched sibling stays",
                  moved_q == [os.path.join("a", "Keep.zip")] and os.path.exists(os.path.join(qroot, "a", "Same.zip")),
                  moved_q)
        finally:
            shutil.rmtree(qroot, ignore_errors=True)
            shutil.rmtree(qroot + "-q", ignore_errors=True)
        # A RED stage's artifacts never reach a later independent sibling's hand-over: the shared directory is walked
        # whole by every green stage, so without the quarantine `bad`'s unfinished bundle would be receipted as `good`'s,
        # and its dangling symlink would turn `good` red.
        for leftover, label in (("echo half > $MW_BUILD_ARTIFACTS/Bad/Bad-1.0.0.zip", "an unfinished file"),
                                ("ln -s /nonexistent $MW_BUILD_ARTIFACTS/Bad/dangling", "a dangling symlink")):
            sib = [{"name": "good0", "kind": "build", "command": art_cmd},
                   {"name": "bad", "kind": "build", "command": f"mkdir -p $MW_BUILD_ARTIFACTS/Bad && {leftover}; exit 1",
                    "after": []},
                   {"name": "good", "kind": "build", "command": "mkdir -p $MW_BUILD_ARTIFACTS/Good && echo ok > "
                                                                "$MW_BUILD_ARTIFACTS/Good/Good.json", "after": []}]
            for quarantine in (True, False):
                posted.clear()
                hq = host(api_ev(), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=fake_store,
                          checkout=checkout_with({"stages": sib}))
                real_q = quarantine_artifacts
                if not quarantine:   # NEGATIVE CONTROL: the same run with the quarantine disabled
                    globals()["quarantine_artifacts"] = lambda root, before, dest: []
                try:
                    hq.run(claim)
                finally:
                    globals()["quarantine_artifacts"] = real_q
                by = {}
                for pth, es in posted:
                    for e in es:
                        by.setdefault(pth.split("/")[-2], []).append(e)
                good_units = sorted(e["unit"] for e in by.get("good", []) if e["kind"] == "artifact")
                good_fin = [e for e in by.get("good", []) if e["kind"] == "stage-finished"]
                clean = (good_fin and good_fin[-1]["state"] == "succeeded"
                         and not [u for u in good_units if u.startswith("Bad/")] and "Good/Good.json" in good_units
                         and "Store/Store-1.4.2.zip" in good_units)
                if quarantine:
                    check(f"h9/{label} left by a red stage is not handed over by a later independent sibling, which stays green",
                          clean, (good_fin, good_units))
                    check(f"h9/{label}: the red stage says what it quarantined",
                          any(e["kind"] == "line" and "quarantined" in e["text"] and e["text"].endswith(": Bad")
                              for e in by.get("bad", [])), by.get("bad"))
                else:
                    check(f"h9/negative control: without the quarantine {label} reaches the sibling's hand-over",
                          not clean, (good_fin, good_units))
        # A RED stage's ARTIFACTS outlive its sandbox (MeshWeaver.Deploy#52, after the diagnostics of #4499): everything it
        # added — its diagnostics unit, its results, even an unfinished bundle — is handed over and receipted before its
        # failed verdict, EVERY receipt marked failed (so no reader publishes or ratchets it), the originals are still
        # quarantined, and a dangling symlink among them is skipped rather than failing the hand-over.
        red_cmd = ("mkdir -p $MW_BUILD_ARTIFACTS/diagnostics/bad $MW_BUILD_ARTIFACTS/Bad && echo crash > "
                   "$MW_BUILD_ARTIFACTS/diagnostics/bad/suite.log && echo half > $MW_BUILD_ARTIFACTS/Bad/Bad-1.0.0.zip && "
                   "ln -s /nonexistent $MW_BUILD_ARTIFACTS/diagnostics/bad/dangling; exit 1")
        for wired in (True, False):
            posted.clear()
            hd = host(api_ev(), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=fake_store,
                      checkout=checkout_with({"stages": [{"name": "bad", "kind": "test", "command": red_cmd}]}))
            if not wired:   # NEGATIVE CONTROL: the runner without the red-stage hand-over — a red stage receipts nothing
                hd.hand_over_failed_stage = lambda *a, **k: ([], None)
            hd.run(claim)
            evs = [e for pth, es in posted if pth.split("/")[-2] == "bad" for e in es]
            red_arts = [e for e in evs if e["kind"] == "artifact"]
            units = sorted(e["unit"] for e in red_arts)
            fin = [e for e in evs if e["kind"] == "stage-finished"]
            if wired:
                check("h9/a red stage hands over every file it added (diagnostics AND its other units), and stays red",
                      units == ["Bad/Bad-1.0.0.zip", "diagnostics/bad/suite.log"] and fin and fin[-1]["state"] == "failed",
                      (units, fin))
                check("h9/every receipt of a red stage arrives marked failed",
                      red_arts and all(e.get("stageState") == "failed" for e in red_arts), red_arts)
                check("h9/the red stage's receipts precede its failed verdict",
                      [e["kind"] for e in evs if e["kind"] in ("artifact", "stage-finished")][-1] == "stage-finished", evs)
                check("h9/the red stage says how many files it handed over, marked failed",
                      any(e["kind"] == "line" and e["text"] == "red stage: 2 file(s) handed over, marked failed" for e in evs), evs)
            else:
                check("h9/negative control: without the red-stage hand-over a red stage receipts nothing", units == [], units)
        # The API store carries the marker on the UPLOAD too, so the receipt the instance records says failed.
        red_puts: list[dict] = []

        def red_put(path, query, data, size, timeout):
            red_puts.append(dict(query))
            digest = path.rsplit("/", 1)[1]
            return 201, {"stage": query["stage"], "unit": query["unit"], "sha256": digest, "bytes": size,
                         "stageState": query.get("stageState"), "existed": False,
                         "key": f"api:/build-hosts/R/{'c' * 40}/stable/{query['stage']}/{digest}/{query['unit']}#sha256={digest}"}
        posted.clear()
        hr = host(api_ev(((("PUT", "/MeshWeaver.Plugins/"), red_put))), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="api:"),
                  store_runner=fake_store, checkout=checkout_with({"stages": [{"name": "bad", "kind": "test", "command": red_cmd}]}))
        hr.run(dict(claim, claimId="claim-red"))
        evs = [e for pth, es in posted if pth.split("/")[-2] == "bad" for e in es]
        check("h9-api/a red stage's uploads name stageState=failed, and its receipts say failed",
              len(red_puts) == 2 and all(q.get("stageState") == "failed" for q in red_puts)
              and all(e.get("stageState") == "failed" for e in evs if e["kind"] == "artifact")
              and [e for e in evs if e["kind"] == "stage-finished"][-1]["state"] == "failed", (red_puts, evs))
        droot = tempfile.mkdtemp(prefix="h9d-")
        try:
            os.makedirs(os.path.join(droot, "diagnostics", "x"))
            os.makedirs(os.path.join(droot, "Mod"))
            for rel_, body in (("diagnostics/x/old.log", b"old"), ("Mod/Mod.zip", b"zip")):
                with open(os.path.join(droot, rel_), "wb") as fh:
                    fh.write(body)
            before_d = artifact_snapshot(droot)
            with open(os.path.join(droot, "diagnostics", "x", "new.trx"), "wb") as fh:
                fh.write(b"trx")
            with open(os.path.join(droot, "Mod", "Mod.zip"), "wb") as fh:
                fh.write(b"rewritten")
            got_d = red_stage_artifacts(droot, before_d, droot + "-d")
            check("h9/red stage: what it ADDED or REWROTE is staged (a new trx, a rewritten bundle) — never an older file",
                  got_d == ["Mod/Mod.zip", "diagnostics/x/new.trx"]
                  and os.path.isfile(os.path.join(droot, "diagnostics", "x", "new.trx")), got_d)
        finally:
            shutil.rmtree(droot, ignore_errors=True)
            shutil.rmtree(droot + "-d", ignore_errors=True)
        # The staging itself can fail or must stop, and neither may escape run_stage without a verdict: a work volume
        # that refuses the staging directory (ENOSPC there, a missing directory here) is the diagnostics note; an
        # abandoned run raises Lost before anything is copied; an expired run copies nothing and says so.
        sroot = tempfile.mkdtemp(prefix="h9s-")
        try:
            os.makedirs(os.path.join(sroot, "art", "diagnostics", "x"))
            with open(os.path.join(sroot, "art", "diagnostics", "x", "dump.bin"), "wb") as fh:
                fh.write(b"dump")
            hs = host(api_ev(), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=fake_store)
            hs.work = os.path.join(sroot, "no-such-work-volume")
            try:
                r_, prob_ = hs.hand_over_failed_stage("r", "s", "bad", os.path.join(sroot, "art"), {})
                staged_ok = r_ == [] and bool(prob_) and "could not be staged" in prob_
            except Exception as e:      # the defect: the staging error escaped
                staged_ok, prob_ = False, repr(e)
            check("h9/red stage: a staging directory the work volume refuses is the note, never an escaped exception",
                  staged_ok, prob_)
            hs.work = sroot
            gone = threading.Event()
            gone.set()
            try:
                hs.hand_over_failed_stage("r", "s", "bad", os.path.join(sroot, "art"), {}, cancel=gone)
                lost_ok = False
            except Lost:
                lost_ok = True
            check("h9/red stage: an abandoned run stages nothing (Lost before the first copy)",
                  lost_ok and not [d for d in os.listdir(sroot) if d.startswith(".red-stage-")], os.listdir(sroot))
            r_, prob_ = hs.hand_over_failed_stage("r", "s", "bad", os.path.join(sroot, "art"), {},
                                                  deadline=hs.clock() - 1)
            check("h9/red stage: an expired run copies nothing and names the deadline",
                  r_ == [] and bool(prob_) and "deadline" in prob_
                  and not [d for d in os.listdir(sroot) if d.startswith(".red-stage-")], (r_, prob_))
            # A dump larger than two chunks, stopped by the check after the first chunk: only that chunk is written.
            # An unbounded read followed by one check would write the whole file before it could stop.
            big_root = os.path.join(sroot, "big")
            os.makedirs(os.path.join(big_root, "diagnostics", "x"))
            with open(os.path.join(big_root, "diagnostics", "x", "core.dump"), "wb") as fh:
                fh.truncate(2 * RED_STAGE_COPY_CHUNK + 1)
            calls = []

            class _Stop(Exception):
                pass

            def stop_after_first_chunk():
                calls.append(1)
                if len(calls) == 2:     # call 1 precedes the file, call 2 follows its first chunk
                    raise _Stop()
            stopped = False
            try:
                red_stage_artifacts(big_root, {}, os.path.join(sroot, "copy"), stop_after_first_chunk)
            except _Stop:
                stopped = True
            copied_target = os.path.join(sroot, "copy", "diagnostics", "x", "core.dump")
            written = os.path.getsize(copied_target) if os.path.isfile(copied_target) else -1
            check("h9/red stage: the copy re-checks before each file and between its chunks, and stops mid-file",
                  stopped and calls == [1, 1] and written == RED_STAGE_COPY_CHUNK, (stopped, calls, written))
        finally:
            shutil.rmtree(sroot, ignore_errors=True)
        # ── 7d. FAN-OUT: a `parallel` run's independent stages run CONCURRENTLY, within the host's declared slots ──
        # The scheduling decision itself, pure: a dependant waits for its dependency, a build runs ALONE, slots bound it.
        fs = [{"name": "build", "kind": "build", "after": []}, {"name": "s0", "kind": "test", "after": ["build"]},
              {"name": "s1", "kind": "test", "after": ["build"]}, {"name": "s2", "kind": "test", "after": ["build"]},
              {"name": "doc", "kind": "test", "after": ["s0"]}]
        sel = {x["name"] for x in fs}
        _, st_ = next_stages(fs, sel, set(), set(), {}, 8)
        check("fanout/only the build starts first (everything else runs after it)", [x["name"] for x in st_] == ["build"], st_)
        _, st_ = next_stages(fs[1:], sel, {"build"}, set(), {}, 2)
        check("fanout/independent siblings start together, bounded by the slots",
              [x["name"] for x in st_] == ["s0", "s1"], st_)
        _, st_ = next_stages(fs[2:], sel, {"build"}, set(), {"s0": "test"}, 8)
        check("fanout/a dependant waits while its dependency is in flight; its independent siblings start",
              [x["name"] for x in st_] == ["s1", "s2"], st_)
        _, st_ = next_stages([fs[4]], sel, {"build", "s0"}, set(), {"s1": "test"}, 8)
        check("fanout/a dependant starts once its dependency finished green", [x["name"] for x in st_] == ["doc"], st_)
        bl, st_ = next_stages([fs[4]], sel, {"build", "s0"}, {"s0"}, {}, 8)
        check("fanout/a dependant of a red stage is blocked, naming it", not st_ and bl and bl[0][1] == "s0", (bl, st_))
        late_build = [{"name": "t", "kind": "test", "after": []}, {"name": "b2", "kind": "build", "after": []},
                      {"name": "t2", "kind": "test", "after": []}]
        _, st_ = next_stages(late_build, {"t", "b2", "t2"}, set(), set(), {}, 8)
        check("fanout/a build waiting for the stages in flight to drain holds back everything declared after it",
              [x["name"] for x in st_] == ["t"], st_)
        _, st_ = next_stages(late_build[2:], {"t", "b2", "t2"}, {"t"}, set(), {"b2": "build"}, 8)
        check("fanout/nothing starts while a build is in flight", st_ == [], st_)
        _, st_ = next_stages([{"name": "x", "kind": "test", "after": ["not-selected"]}], {"x"}, set(), set(), {}, 8)
        check("fanout/a dependency the claim did not select blocks nothing", [x["name"] for x in st_] == ["x"], st_)
        # The budget is the schedule the host runs: core's declaration (build 25 + 8 shards x 20 + doc-gate 25) is 210 min
        # serially — past a 120-minute deadline — and 70 min on 8 slots (build, then the 8 shards, then doc-gate).
        core = ([{"name": "build", "kind": "build", "timeoutMinutes": 25, "after": []}]
                + [{"name": f"test-shard-{i}", "kind": "test", "timeoutMinutes": 20, "after": ["build"]} for i in range(8)]
                + [{"name": "doc-gate", "kind": "test", "timeoutMinutes": 25, "after": ["build"]}])
        check("fanout/one slot: the caps' sum (today's serial budget)", planned_seconds(core, 1) == 210 * 60, planned_seconds(core, 1))
        check("fanout/8 slots: the critical path through the slots", planned_seconds(core, 8) == 70 * 60, planned_seconds(core, 8))
        check("fanout/10 slots: build, then every test stage at once", planned_seconds(core, 10) == 50 * 60, planned_seconds(core, 10))
        check("fanout/the declared budget follows the schedule",
              run_budget_seconds(core, 10 ** 6, slots=8) == 70 * 60 + RUN_MARGIN_SECONDS
              and run_budget_seconds(core, 10 ** 6) == 210 * 60 + RUN_MARGIN_SECONDS)
        # The budget BOUNDS the run: a greedy schedule is not monotone in its durations (Graham's anomaly). Two slots,
        # a/b/c/d/e capped 20/20/15/45/30 min, c and d after b: all caps take 65 min; but if only `a` finishes at 5 min,
        # unpinned, `e` takes the freed slot, `d` waits for `c`, and the run takes 80 min — cut by its own deadline with
        # every stage inside its cap. Pinned to the all-caps schedule it can never exceed it.
        anomaly = [{"name": "a", "kind": "test", "timeoutMinutes": 20, "after": []},
                   {"name": "b", "kind": "test", "timeoutMinutes": 20, "after": []},
                   {"name": "c", "kind": "test", "timeoutMinutes": 15, "after": ["b"]},
                   {"name": "d", "kind": "test", "timeoutMinutes": 45, "after": ["b"]},
                   {"name": "e", "kind": "test", "timeoutMinutes": 30, "after": []}]
        early_a = lambda st: 5 * 60 if st["name"] == "a" else _cap_seconds(st)
        planned_a, pin_a = cap_schedule(anomaly, 2)
        check("fanout/budget: the anomaly's all-caps schedule is 65 min", planned_a == 65 * 60, planned_a)
        check("fanout/budget: negative control — UNPINNED, `a` finishing early takes the run to 80 min, past the budget",
              simulate_schedule(anomaly, 2, early_a)[0] == 80 * 60, simulate_schedule(anomaly, 2, early_a)[0])
        check("fanout/budget: PINNED, `a` finishing early cannot push the run past the budget",
              simulate_schedule(anomaly, 2, early_a, pin_a)[0] <= planned_a, simulate_schedule(anomaly, 2, early_a, pin_a)[0])
        # …and for ANY declaration and ANY durations within the caps (a seeded sweep, so the case is reproducible): builds
        # anywhere, explicit and default `after`, 2-4 slots. The unpinned sweep must find an overrun (or it proves nothing).
        rnd = random.Random(69)
        worst_pinned, unpinned_over = 0.0, 0
        for _ in range(4000):
            decl: list = []
            for i in range(rnd.randint(2, 8)):
                kind = "build" if rnd.random() < 0.2 else "test"
                st = {"name": f"x{i}", "kind": kind, "timeoutMinutes": rnd.randint(1, 45)}
                earlier = [d["name"] for d in decl]
                st["after"] = (rnd.sample(earlier, rnd.randint(0, min(2, len(earlier)))) if rnd.random() < 0.7
                               else [d["name"] for d in decl if d["kind"] == "build"])
                decl.append(st)
            sl = rnd.randint(2, 4)
            planned_r, pin_r = cap_schedule(decl, sl)
            # half the stages take their full cap, the rest finish early — the shape an anomaly needs
            real = {d["name"]: (1.0 if rnd.random() < 0.5 else rnd.uniform(0.01, 1.0)) * _cap_seconds(d) for d in decl}
            pinned_r = simulate_schedule(decl, sl, lambda st: real[st["name"]], pin_r)[0]
            worst_pinned = max(worst_pinned, pinned_r - planned_r)
            unpinned_over += simulate_schedule(decl, sl, lambda st: real[st["name"]])[0] > planned_r + 1e-6
        check("fanout/budget: pinned, no declaration and no durations within the caps outlast the budget",
              worst_pinned <= 1e-6, worst_pinned)
        check("fanout/budget: negative control — the unpinned sweep does find runs past the budget", unpinned_over > 0, unpinned_over)
        # The host's slots are DECLARED and checked against the cgroup it runs in — refused at start, never clamped.
        gib = 1024 ** 3
        check("fanout/8 slots fit the chart's default host (15 CPU, 52 GiB)", stage_slots_refusal("8", (15.0, 52 * gib)) is None)
        check("fanout/one slot needs no capacity check", stage_slots_refusal("1", (0.5, gib)) is None)
        for txt, cap, what in (("8", (6.0, 12 * gib), "CPU"), ("8", (15.0, 12 * gib), "GiB"), ("x", (15.0, None), "whole number"),
                               ("0", (15.0, None), "within"), ("33", (64.0, None), "within")):
            why = stage_slots_refusal(txt, cap)
            check(f"fanout/MW_BUILD_STAGE_SLOTS={txt} on {cap} is refused, naming {what}", bool(why) and what in why, why)
        hs = host(_FakeApi({}), env=dict(env_s, MW_BUILD_STAGE_SLOTS="8"), capacity=lambda: (6.0, 12 * gib))
        try:
            hs.preflight()
            check("fanout/a host declaring more slots than its cgroup holds refuses to start", False, "started")
        except Refusal as e:
            check("fanout/the start refusal names the slots", "MW_BUILD_STAGE_SLOTS" in str(e), e)
        cg = os.path.join(tmp, "cgroup")
        os.makedirs(cg)
        with open(os.path.join(cg, "cgroup.controllers"), "w") as fh:
            fh.write("cpu memory pids\n")
        with open(os.path.join(cg, "cpu.max"), "w") as fh:
            fh.write("1500000 100000\n")
        with open(os.path.join(cg, "memory.max"), "w") as fh:
            fh.write(str(52 * gib) + "\n")
        check("fanout/the cgroup v2 quota and memory limit are read", cgroup_capacity(cg) == (min(15.0, float(os.cpu_count() or 1)), 52 * gib),
              cgroup_capacity(cg))
        with open(os.path.join(cg, "memory.max"), "w") as fh:
            fh.write("max\n")
        check("fanout/an unbounded memory.max is no bound", cgroup_capacity(cg)[1] is None, cgroup_capacity(cg))
        os.remove(os.path.join(cg, "memory.max"))
        why = stage_slots_refusal("8", cgroup_capacity(cg))
        check("fanout/a cgroup v2 host without a readable memory.max refuses 8 slots", bool(why) and "memory.max unreadable" in why, why)
        # cgroup v1: the limits are READ, never taken for the machine's (a 2-CPU, 4 GiB container on any node refuses 8 slots).
        v1 = os.path.join(tmp, "cgroup-v1")
        os.makedirs(os.path.join(v1, "cpu"))
        os.makedirs(os.path.join(v1, "memory"))
        for rel, val in (("cpu/cpu.cfs_quota_us", "200000"), ("cpu/cpu.cfs_period_us", "100000"),
                         ("memory/memory.limit_in_bytes", str(4 * gib))):
            with open(os.path.join(v1, rel), "w") as fh:
                fh.write(val + "\n")
        check("fanout/the cgroup v1 quota and memory limit are read", cgroup_capacity(v1) == (min(2.0, float(os.cpu_count() or 1)), 4 * gib),
              cgroup_capacity(v1))
        why = stage_slots_refusal("8", cgroup_capacity(v1))
        check("fanout/a 2-CPU, 4 GiB cgroup v1 host refuses 8 slots", bool(why) and "MW_BUILD_STAGE_SLOTS 8" in why, why)
        for rel, val in (("cpu/cpu.cfs_quota_us", "-1"), ("memory/memory.limit_in_bytes", "9223372036854771712")):
            with open(os.path.join(v1, rel), "w") as fh:
                fh.write(val + "\n")
        check("fanout/an unlimited cgroup v1 is the machine's CPUs and no memory bound",
              cgroup_capacity(v1) == (float(os.cpu_count() or 1), None), cgroup_capacity(v1))
        # Neither layout readable: unmeasured, never unrestricted — more than one slot is refused, one slot still runs.
        empty = os.path.join(tmp, "cgroup-none")
        os.makedirs(empty)
        why = stage_slots_refusal("8", cgroup_capacity(empty))
        check("fanout/a host whose cgroup limits cannot be read refuses 8 slots", bool(why) and "cannot be measured" in why, why)
        check("fanout/a host whose cgroup limits cannot be read still runs one slot", stage_slots_refusal("1", cgroup_capacity(empty)) is None)
        hs = host(_FakeApi({}), env=dict(env_s, MW_BUILD_STAGE_SLOTS="8"), capacity=lambda: cgroup_capacity(empty))
        try:
            hs.preflight()
            check("fanout/a host with an unmeasurable cgroup and 8 slots refuses to start", False, "started")
        except Refusal as ex:
            check("fanout/the unmeasured start refusal names the slots", "cannot be measured" in str(ex), ex)
        for bad in ({"parallel": "yes", "stages": []}, {"parallel": 1, "stages": []}):
            pf = os.path.join(tmp, "parallel.json")
            with open(pf, "w") as fh:
                json.dump(bad, fh)
            try:
                load_parallel(pf)
                check(f"fanout/`parallel` {bad['parallel']!r} is refused", False, "accepted")
            except Refusal:
                pass

        # OVERLAP, proven with a rendezvous and no clock: s0 writes the FIFO s1 reads and reads the one s1 writes. Opening
        # a FIFO blocks until its other end is open, so neither stage can finish unless BOTH are running at once. The
        # NEGATIVE CONTROL is the same declaration without `parallel` — serially, s0 waits on a reader that never comes
        # and is killed at its (3 s) cap, red, and so is s1.
        def rendezvous(parallel_: bool, slots_: str) -> tuple[dict, list]:
            fifo = tempfile.mkdtemp(prefix="fifo-", dir=tmp)
            for f in ("ab", "ba"):
                os.mkfifo(os.path.join(fifo, f))
            marks_ = os.path.join(fifo, "m")
            os.makedirs(marks_)
            posted.clear()
            busy: list = []

            def leftovers_seen() -> list:
                # what the host's between-stages leftover check would have seen: a sibling stage still in flight
                busy.append(sorted(t.name for t in threading.enumerate() if t.name.startswith("stage-") and t.is_alive()))
                return []
            doc = {"stages": [
                {"name": "build", "kind": "build", "command": f"touch {marks_}/build"},
                {"name": "s0", "kind": "test", "timeoutMinutes": 0.05,
                 "command": f"echo a > {fifo}/ab && read x < {fifo}/ba && touch {marks_}/s0 && mkdir -p $MW_BUILD_ARTIFACTS/S0 && echo 0 > $MW_BUILD_ARTIFACTS/S0/s0.txt"},
                {"name": "s1", "kind": "test", "timeoutMinutes": 0.05,
                 "command": f"read x < {fifo}/ab && echo b > {fifo}/ba && touch {marks_}/s1 && mkdir -p $MW_BUILD_ARTIFACTS/S1 && echo 1 > $MW_BUILD_ARTIFACTS/S1/s1.txt"},
                {"name": "doc", "kind": "test", "after": ["s0"], "command": f"test -e {marks_}/s0 && touch {marks_}/doc"}]}
            if parallel_:
                doc["parallel"] = True
            hp = host(api_ev(), env=dict(env_s, MW_BUILD_STAGE_SLOTS=slots_, MW_BUILD_ARTIFACT_STORE="file:" + store_dir),
                      store_runner=fake_store, checkout=checkout_with(doc))
            hp.leftovers = leftovers_seen
            outcome = hp.run(claim)
            by_ = {}
            for pth, es in posted:
                for e in es:
                    by_.setdefault(pth.split("/")[-2], []).append(e)
            return {"outcome": outcome, "by": by_, "marks": sorted(os.listdir(marks_))}, busy

        def state(r: dict, n: str) -> str:
            fin_ = [e for e in r["by"].get(n, []) if e["kind"] == "stage-finished"]
            return fin_[-1]["state"] if fin_ else "absent"

        par, busy = rendezvous(True, "4")
        check("fanout/two independent stages are IN FLIGHT AT ONCE (the rendezvous completes)",
              state(par, "s0") == "succeeded" and state(par, "s1") == "succeeded", par["by"])
        check("fanout/the dependant runs only after its dependency finished green",
              state(par, "doc") == "succeeded" and par["marks"] == ["build", "doc", "s0", "s1"], par)
        check("fanout/the run is green", any(e["kind"] == "run-finished" and e["succeeded"] for es in par["by"].values() for e in es), par["by"])
        check("fanout/the plan says it fans out", any(e["kind"] == "line" and e["text"].startswith("fan-out: up to 4")
                                                      for e in par["by"].get("build", [])), par["by"].get("build"))
        check("fanout/the leftover check runs only while NO stage is in flight", busy and all(b == [] for b in busy), busy)
        units = {n: sorted(e["unit"] for e in par["by"].get(n, []) if e["kind"] == "artifact") for n in ("s0", "s1")}
        check("fanout/each concurrent stage hands over ITS OWN artifacts, never a sibling's",
              units == {"s0": ["S0/s0.txt"], "s1": ["S1/s1.txt"]}, units)
        ser, _ = rendezvous(False, "4")
        check("fanout/negative control: without `parallel` the same stages run serially — the rendezvous never completes",
              state(ser, "s0") == "failed" and state(ser, "s1") == "failed", ser["by"])
        check("fanout/negative control: the dependant of the red stage is not run",
              state(ser, "doc") == "failed" and any("not run" in e.get("summary", "") for e in ser["by"].get("doc", [])), ser["by"].get("doc"))
        one, _ = rendezvous(True, "1")
        check("fanout/negative control: `parallel` on a ONE-slot host runs serially too",
              state(one, "s0") == "failed" and state(one, "s1") == "failed"
              and any(e["kind"] == "line" and e["text"].startswith("serial:") for e in one["by"].get("build", [])), one["by"])

        # A RED stage in flight beside a green one: its artifacts are handed over MARKED FAILED (MeshWeaver.Deploy#52) and
        # then quarantined whole, and they never reach the green sibling's hand-over (each stage has its own directory) —
        # the sibling stays green and its receipts stay marked succeeded.
        fifo = tempfile.mkdtemp(prefix="fifo-", dir=tmp)
        for f in ("ab", "ba"):
            os.mkfifo(os.path.join(fifo, f))
        posted.clear()
        hq = host(api_ev(), env=dict(env_s, MW_BUILD_STAGE_SLOTS="2", MW_BUILD_ARTIFACT_STORE="file:" + store_dir),
                  store_runner=fake_store, checkout=checkout_with({"parallel": True, "stages": [
                      {"name": "bad", "kind": "test", "after": [], "timeoutMinutes": 0.05,
                       "command": f"mkdir -p $MW_BUILD_ARTIFACTS/Bad && echo half > $MW_BUILD_ARTIFACTS/Bad/Bad-1.0.0.zip "
                                  f"&& ln -s /nonexistent $MW_BUILD_ARTIFACTS/Bad/dangling && echo a > {fifo}/ab && read x < {fifo}/ba; exit 1"},
                      {"name": "good", "kind": "test", "after": [], "timeoutMinutes": 0.05,
                       "command": f"read x < {fifo}/ab && mkdir -p $MW_BUILD_ARTIFACTS/Good && echo ok > $MW_BUILD_ARTIFACTS/Good/Good.json "
                                  f"&& echo b > {fifo}/ba"}]}))
        check("fanout/a red and a green stage in flight together", hq.run(claim) == "ran")
        by_q = {}
        for pth, es in posted:
            for e in es:
                by_q.setdefault(pth.split("/")[-2], []).append(e)
        gfin = [e for e in by_q.get("good", []) if e["kind"] == "stage-finished"]
        gunits = sorted(e["unit"] for e in by_q.get("good", []) if e["kind"] == "artifact")
        check("fanout/the green sibling stays green and hands over only its own artifacts, marked succeeded",
              gfin and gfin[-1]["state"] == "succeeded" and gunits == ["Good/Good.json"]
              and all(e.get("stageState") == "succeeded" for e in by_q.get("good", []) if e["kind"] == "artifact"), (gfin, gunits))
        bad_arts = [e for e in by_q.get("bad", []) if e["kind"] == "artifact"]
        check("fanout/the red stage hands over its own files marked failed (never its dangling symlink) and says what it quarantined",
              [e["unit"] for e in bad_arts] == ["Bad/Bad-1.0.0.zip"] and all(e.get("stageState") == "failed" for e in bad_arts)
              and any(e["kind"] == "line" and "quarantined" in e["text"] for e in by_q.get("bad", [])), by_q.get("bad"))

        # LOST: a 409 on one stage's route abandons the RUN — its siblings in flight are killed, not waited out.
        fifo = tempfile.mkdtemp(prefix="fifo-", dir=tmp)
        os.mkfifo(os.path.join(fifo, "never"))

        def on_events_409(path, body):
            if "/stages/s0/" in path:
                return 409, None
            posted.append((path, body["events"]))
            return 202, None
        posted.clear()
        t0 = time.monotonic()
        hl = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): on_events_409}),
                  env=dict(env_s, MW_BUILD_STAGE_SLOTS="2"), checkout=checkout_with({"parallel": True, "stages": [
                      {"name": "build", "kind": "build", "command": "true"},
                      {"name": "s0", "kind": "test", "command": "echo going"},
                      {"name": "s1", "kind": "test", "timeoutMinutes": 5, "command": f"read x < {fifo}/never"}]}))
        check("fanout/a 409 on one stage abandons the run", hl.run(claim) == "lost")
        check("fanout/its sibling in flight is killed, not waited out (cap 5 min)", time.monotonic() - t0 < 60, time.monotonic() - t0)
        check("fanout/no verdict is posted for the abandoned run",
              not [e for _, es in posted for e in es if e["kind"] == "run-finished"], posted)
        # The same 409 on a SERIAL run with stages still pending: nothing more starts, and the run ends "lost" at once
        # (the scheduler drains only what is in flight — it once spun forever on the pending stages it could not start).
        marks = tempfile.mkdtemp(prefix="lost-", dir=tmp)
        posted.clear()
        hl1 = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): on_events_409}),
                   env=env_s, checkout=checkout_with({"stages": [
                       {"name": "build", "kind": "build", "command": "true"},
                       {"name": "s0", "kind": "test", "command": "echo going"},
                       {"name": "s1", "kind": "test", "command": f"touch {marks}/s1"}]}))
        done_l: list = []
        tl = threading.Thread(target=lambda: done_l.append(hl1.run(claim)), daemon=True)
        tl.start()
        tl.join(60)
        check("fanout/a 409 on a serial run with stages pending ends the run lost", done_l == ["lost"], done_l)
        check("fanout/…and starts no further stage", os.listdir(marks) == [], os.listdir(marks))

        # The 409 ends the run the moment a WORKER meets it, not when the scheduler dequeues it: completions queued as
        # [s0 succeeded, s1 Lost] must not see s0's freed slot refilled. Forced with no clock: the scheduler's own clock
        # read, once s0's worker has ENDED (its success queued), releases s1 through a FIFO and joins s1's worker — so the
        # 409 is met and queued behind s0's success before the scheduler looks at its queue again.
        fifo = tempfile.mkdtemp(prefix="fifo-", dir=tmp)
        os.mkfifo(os.path.join(fifo, "go"))
        sched_thread = threading.current_thread()
        started_stages: list = []
        fired: list = []

        class _RecordingSandbox(_EnvSandbox):
            def argv(self, command, checkout, outputs, env, readonly=(), writable=(), mount=None, binds=()):
                started_stages.append(env.get("MW_BUILD_STAGE"))
                return super().argv(command, checkout, outputs, env, readonly, writable, mount, binds)

        def staged_clock() -> float:
            if (threading.current_thread() is sched_thread and not fired and "s0" in started_stages
                    and not any(t.name == "stage-s0" for t in threading.enumerate())):
                fired.append(True)
                with open(os.path.join(fifo, "go"), "w") as fh:
                    fh.write("go\n")
                s1t = next((t for t in threading.enumerate() if t.name == "stage-s1"), None)
                if s1t is not None:
                    s1t.join(60)
            return time.monotonic()

        def on_events_409_s1(path, body):
            if "/stages/s1/" in path:
                return 409, None
            posted.append((path, body["events"]))
            return 202, None
        posted.clear()
        hr = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): on_events_409_s1}),
                  env=dict(env_s, MW_BUILD_STAGE_SLOTS="2"), clock=staged_clock, checkout=checkout_with({"parallel": True, "stages": [
                      {"name": "s0", "kind": "test", "after": [], "timeoutMinutes": 1, "command": "true"},
                      {"name": "s1", "kind": "test", "after": [], "timeoutMinutes": 5, "command": f"read x < {fifo}/go && echo hi"},
                      {"name": "s2", "kind": "test", "after": [], "timeoutMinutes": 1, "command": "true"}]}))
        hr.sandbox = _RecordingSandbox()
        check("fanout/a 409 met behind a queued success ends the run lost", hr.run(claim) == "lost")
        check("fanout/the race was forced (s1 released only after s0's worker ended)", fired == [True], fired)
        check("fanout/…and no stage starts once a worker has met the 409, even into a freed slot",
              sorted(started_stages) == ["s0", "s1"], started_stages)

        # A sibling BLOCKED in the artifact store when the 409 arrives is cut loose, not waited out: the store call is
        # killed (cancel reaches it), and the abandoned stage hands over and receipts nothing, posting no verdict.
        fifo = tempfile.mkdtemp(prefix="fifo-", dir=tmp)
        os.mkfifo(os.path.join(fifo, "go"))
        store_cancels: list = []

        def blocking_store(argv, timeout=None, cancel=None):
            store_cancels.append(cancel is not None)
            if len(store_cancels) == 1:
                with open(os.path.join(fifo, "go"), "w") as fh:     # releases s1, whose verdict meets the 409
                    fh.write("go\n")
            # the real runner (`_run_capture_stdout`) kills its helper when `cancel` is set; without it, a slow store
            if (cancel or threading.Event()).wait(30):
                raise Lost("store helper killed — the run was abandoned")
            return fake_store(argv, timeout)
        posted.clear()
        t0 = time.monotonic()
        hb_ = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): on_events_409_s1}),
                   env=dict(env_s, MW_BUILD_STAGE_SLOTS="2", MW_BUILD_ARTIFACT_STORE="file:" + store_dir),
                   store_runner=blocking_store, checkout=checkout_with({"parallel": True, "stages": [
                       {"name": "s0", "kind": "test", "after": [],
                        "command": "mkdir -p $MW_BUILD_ARTIFACTS/U && echo 0 > $MW_BUILD_ARTIFACTS/U/u.txt"},
                       {"name": "s1", "kind": "test", "after": [], "command": f"read x < {fifo}/go && echo hi"}]}))
        check("fanout/a 409 while a sibling is in the store ends the run lost", hb_.run(claim) == "lost")
        check("fanout/the store call is told the run was abandoned", store_cancels[:1] == [True], store_cancels)
        check("fanout/…and no further store call follows it", len(store_cancels) == 1, store_cancels)
        check("fanout/…and is cut loose at once, not waited out", time.monotonic() - t0 < 20, time.monotonic() - t0)
        s0_posted = [e for pth, es in posted if "/stages/s0/" in pth for e in es]
        check("fanout/the abandoned sibling posts no receipt and no verdict",
              not [e for e in s0_posted if e["kind"] in ("artifact", "stage-finished")], s0_posted)
        # The real store runner kills its helper on `cancel` (a helper that would run 30 s), and an upload's body stops
        # streaming at its next block.
        gone = threading.Event()
        gone.set()
        t0 = time.monotonic()
        try:
            _run_capture_stdout(["/bin/sleep", "30"], 60, gone)
            check("fanout/the store helper of an abandoned run is killed", False, "it ran to the end")
        except Lost:
            check("fanout/the store helper of an abandoned run is killed at once", time.monotonic() - t0 < 15, time.monotonic() - t0)
        check("fanout/the store helper still answers when not cancelled",
              _run_capture_stdout(["/bin/echo", "locator=x"], 60, threading.Event()) == (0, "locator=x\n"))
        small = os.path.join(tmp, "abandoned-upload.bin")
        with open(small, "wb") as fh:
            fh.write(b"bytes")
        try:
            Api("http://127.0.0.1:9/api/builds", "t").upload("/x", {}, small, 5, timeout=5, cancel=gone)
            check("fanout/an abandoned upload stops streaming", False, "sent on")
        except Lost:
            pass

        # H9, the API store (`MW_BUILD_ARTIFACT_STORE=api:`): each artifact is PUT to the build API as the run's holder and
        # its receipt is the SERVER's answer; nothing touches ci-artifact-store.py.
        api_env = dict(env_s, MW_BUILD_ARTIFACT_STORE="api:")
        api_store: dict[str, bytes] = {}
        api_puts: list[tuple] = []

        def api_put(answers=None, lie=None):
            """A fake build API upload route: verifies the body against the digest in the path, stores it, answers like
            the instance (201 new / 200 existed). `answers` scripts statuses first; `lie` rewrites a field of the answer."""
            scripted = list(answers or [])

            def on_put(path, query, data, size, timeout):
                api_puts.append((path, query, len(data), size, timeout))
                if scripted:
                    return scripted.pop(0)
                digest = path.rsplit("/", 1)[1]
                if hashlib.sha256(data).hexdigest() != digest or len(data) != size:
                    return 422, {"error": "the body does not hash to its name"}
                existed = digest in api_store
                api_store[digest] = data
                answer = {"stage": query["stage"], "unit": query["unit"], "sha256": digest, "bytes": size, "existed": existed,
                          "key": f"api:/build-hosts/R/{'c' * 40}/stable/{query['stage']}/{digest}/{query['unit']}#sha256={digest}"}
                if lie:
                    answer.update(lie)
                return (200 if existed else 201), answer
            return on_put

        def api_run(on_put, **kw):
            posted.clear()
            api_puts.clear()
            store_calls.clear()
            api = api_ev(((("PUT", "/MeshWeaver.Plugins/"), on_put)))
            hx = host(api, env=api_env, store_runner=fake_store,
                      checkout=checkout_with({"stages": [{"name": "modules", "kind": "build", "command": art_cmd}]}), **kw)
            outcome = hx.run(dict(claim, claimId="claim-api"))
            evs_ = [e for _, es in posted for e in es]
            return outcome, evs_, [e for e in evs_ if e["kind"] == "stage-finished"]

        outcome, evs, fin = api_run(api_put())
        arts = [e for e in evs if e["kind"] == "artifact"]
        check("h9-api/a run with artifacts succeeds", outcome == "ran" and fin and fin[-1]["state"] == "succeeded", (outcome, fin))
        check("h9-api/one PUT per artifact, nothing through ci-artifact-store.py", len(api_puts) == 2 and not store_calls, (api_puts, store_calls))
        check("h9-api/each PUT names the run's holder, stage, unit and channel",
              all(q.get("host") and q.get("claimId") == "claim-api" and q.get("stage") == "modules" and q.get("channel") == "stable"
                  and q.get("unit") in ("Store/Store-1.4.2.zip", "Store/Store.publication.json") for _p, q, *_ in api_puts), api_puts)
        check("h9-api/each PUT carries the whole file and declares its size", all(n == sz and n > 0 for _p, _q, n, sz, _t in api_puts), api_puts)
        check("h9-api/the receipts are the server's answers (api:/ locators attesting the digest)",
              sorted(a["unit"] for a in arts) == ["Store/Store-1.4.2.zip", "Store/Store.publication.json"]
              and all(a["key"].startswith("api:/build-hosts/") and a["key"].endswith("#sha256=" + a["sha256"]) for a in arts), arts)
        check("h9-api/every upload is bounded by the run budget left", all(t is not None and 0 < t <= min(1800, ha.run_deadline) for *_x, t in api_puts), api_puts)
        # a duplicate upload is idempotent at the server: the receipt is the same
        outcome, evs, fin = api_run(api_put())
        check("h9-api/a re-run of the same bytes is answered 'existed' and still receipted", outcome == "ran" and fin[-1]["state"] == "succeeded"
              and len([e for e in evs if e["kind"] == "artifact"]) == 2, fin)
        # bounded retries on a transport failure or a 5xx, then the stage passes
        outcome, evs, fin = api_run(api_put(answers=[(503, {"error": "busy"}), (0, "connection reset")]))
        check("h9-api/a 503 and a transport failure are retried, then stored", outcome == "ran" and fin[-1]["state"] == "succeeded"
              and len(api_puts) == 4, (len(api_puts), fin))
        # ... and a 5xx that never ends fails the stage after exactly API_UPLOAD_ATTEMPTS — red, named, never a pass
        outcome, evs, fin = api_run(api_put(answers=[(503, {"error": "the run is being written concurrently"})] * 50))
        check("h9-api/a persistent 5xx fails the stage after the bounded attempts, naming it",
              fin and fin[-1]["state"] == "failed" and f"after {API_UPLOAD_ATTEMPTS} attempts" in fin[-1]["summary"]
              and len(api_puts) == API_UPLOAD_ATTEMPTS and not [e for e in evs if e["kind"] == "artifact"], (len(api_puts), fin))
        # a refusal is NOT retried: 422 (hash), 401 (token), 413 (size) fail the stage at once, naming the server's reason
        for code, why in ((422, "the body hashes to sha256 x, not y"), (401, "the build-host worker token is required"), (413, "an artifact is at most 1 bytes")):
            outcome, evs, fin = api_run(api_put(answers=[(code, {"error": why})] * 5))
            check(f"h9-api/a {code} fails the stage at once, naming the reason", fin and fin[-1]["state"] == "failed"
                  and f"({code})" in fin[-1]["summary"] and why[:20] in fin[-1]["summary"] and len(api_puts) == 1, (len(api_puts), fin))
        # NEGATIVE CONTROLS: a server answer that is not the receipt of THIS artifact fails the stage
        for field, lie in (("hash", {"sha256": "0" * 64}), ("size", {"bytes": 1}), ("unit", {"unit": "Other/x.zip"}),
                           ("locator", {"key": "file:/ci-artifacts/x#sha256=" + "0" * 64})):
            outcome, evs, fin = api_run(api_put(lie=lie))
            check(f"h9-api/an answer naming another {field} fails the stage", fin and fin[-1]["state"] == "failed"
                  and "build API's answer" in fin[-1]["summary"] and not [e for e in evs if e["kind"] == "artifact"], fin)
        # a 409 means the run is not this host's any more: the run is lost, nothing more is posted for it
        outcome, evs, fin = api_run(api_put(answers=[(409, {"error": "not-yours: the run is not held by this host"})]))
        check("h9-api/a 409 on the upload abandons the run", outcome == "lost" and len(api_puts) == 1, (outcome, api_puts))
        # a spent budget makes no upload and is a problem
        api_puts.clear()
        hs = host(_FakeApi({("PUT", "/R/"): api_put()}), env=api_env)
        _r, problem = hs.hand_over_artifacts("R", "f" * 40, "modules", os.path.dirname(spent_dir), deadline=hs.clock() - 1)
        check("h9-api/a spent budget makes no upload and is a problem", not api_puts and problem and "run deadline" in problem, (api_puts, problem))
        _r, problem = host(_FakeApi({}), env=dict(env_s, MW_BUILD_ARTIFACT_STORE="api:/elsewhere")).hand_over_artifacts(
            "R", "f" * 40, "modules", os.path.dirname(spent_dir))
        check("h9-api/an api: spec that is not exactly 'api:' is refused, never guessed", problem and "not a store" in problem, problem)
        # the TRANSPORT: Api.upload over real HTTP to a local server — the bytes, the length and the bearer arrive intact
        import http.server
        got: dict = {}

        class _Upload(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                n = int(self.headers.get("Content-Length", "-1"))
                got.update(path=self.path, auth=self.headers.get("Authorization"), length=n,
                           type=self.headers.get("Content-Type"), body=self.rfile.read(n) if n >= 0 else b"")
                payload = json.dumps({"ok": True}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upload)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            blob = os.path.join(tmp, "transport.bin")
            data = os.urandom(300_000)
            with open(blob, "wb") as fh:
                fh.write(data)
            real = Api(f"http://127.0.0.1:{server.server_address[1]}/api/builds", "the-worker-token")
            status, answer = real.upload("/R/" + "f" * 40 + "/artifacts/" + hashlib.sha256(data).hexdigest(),
                                         {"channel": "latest", "unit": "u/f"}, blob, len(data), timeout=30)
            check("h9-api/transport: the PUT is answered and its JSON read", status == 201 and answer == {"ok": True}, (status, answer))
            check("h9-api/transport: the bytes arrive byte-identical with their length and the bearer",
                  got.get("body") == data and got.get("length") == len(data) and got.get("auth") == "Bearer the-worker-token"
                  and got.get("type") == "application/octet-stream" and "unit=u%2Ff" in got.get("path", ""), {k: v for k, v in got.items() if k != "body"})
        finally:
            server.shutdown()
            server.server_close()
        status, answer = Api("http://127.0.0.1:9/api/builds", "t").upload("/x", {}, blob, 1, timeout=5)
        check("h9-api/transport: an unreachable API is status 0, never a pass", status == 0, (status, answer))

        # Deploy#64: the build API refuses an oversize artifact on its DECLARED length, BEFORE it reads the body, and
        # closes. The host must report THAT answer (413 + reason), never a broken pipe it then retries as transport.
        class _EarlyRefusal(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                payload = json.dumps({"error": "artifact-too-large: 41943040 bytes, the limit is 1024"}).encode()
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                self.close_connection = True          # the body is never read

            def log_message(self, *a):
                pass

        big = os.path.join(tmp, "oversize.bin")
        with open(big, "wb") as fh:
            fh.write(b"\0" * (40 * 1024 * 1024))     # far past every socket buffer: the send MUST meet the close
        early = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EarlyRefusal)
        threading.Thread(target=early.serve_forever, daemon=True).start()
        try:
            status, answer = Api(f"http://127.0.0.1:{early.server_address[1]}/api/builds", "t").upload(
                "/x", {}, big, os.path.getsize(big), timeout=30)
            check("h9-api/transport: an answer sent BEFORE the body was read (413) is reported, never a broken pipe",
                  status == 413 and _answer_text(answer).startswith("artifact-too-large"), (status, answer))
        finally:
            early.shutdown()
            early.server_close()

        # Deploy#63: `timeout` bounds the WHOLE exchange. A server that answers a trickle — every byte well inside any
        # per-operation timeout — must not keep the upload past its budget, and a late success is not accepted.
        class _Trickle(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                n = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(n)
                payload = json.dumps({"ok": True, "pad": "x" * 40}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    for b_ in payload:
                        self.wfile.write(bytes([b_]))
                        self.wfile.flush()
                        time.sleep(0.1)               # ~6 s in all, 0.1 s between bytes
                except OSError:
                    pass                              # the client gave up — what the budget is for

            def log_message(self, *a):
                pass

        slow = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
        threading.Thread(target=slow.serve_forever, daemon=True).start()
        try:
            t0 = time.monotonic()
            status, answer = Api(f"http://127.0.0.1:{slow.server_address[1]}/api/builds", "t").upload(
                "/x", {}, blob, len(data), timeout=1.5)
            spent = time.monotonic() - t0
            check("h9-api/transport: a trickling answer ends at the ABSOLUTE budget as a transport failure, never a late success",
                  status == 0 and "budget" in str(answer) and spent < 3.0, (status, answer, round(spent, 2)))
        finally:
            slow.shutdown()
            slow.server_close()

        # a response closed before its Content-Length arrived (IncompleteRead) is status 0 on BOTH paths — a
        # transport failure the bounded retry handles — never an exception escaping before the verdict is posted
        class _Truncated(http.server.BaseHTTPRequestHandler):
            def _cut(self):
                n = int(self.headers.get("Content-Length", "0"))
                if n:
                    self.rfile.read(n)
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"ok":')
                self.wfile.flush()
                self.close_connection = True

            do_PUT = _cut
            do_POST = _cut

            def log_message(self, *a):
                pass

        cut = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Truncated)
        threading.Thread(target=cut.serve_forever, daemon=True).start()
        try:
            api = Api(f"http://127.0.0.1:{cut.server_address[1]}/api/builds", "t")
            status, answer = api.upload("/x", {}, blob, 1, timeout=10)
            check("h9-api/transport: a truncated upload answer is status 0, never an escaping IncompleteRead", status == 0, (status, answer))
            status, answer = api.call("POST", "/y", {"a": 1})
            check("h9-api/transport: a truncated call answer is status 0, never an escaping IncompleteRead", status == 0, (status, answer))
        finally:
            cut.shutdown()
            cut.server_close()

        # a REFUSAL closed before its Content-Length arrived keeps its status: a 413/401 fails at once and is never
        # retried as a transport failure (the same rule `call` keeps on its HTTPError path)
        class _TruncatedRefusal(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"error":"artifact-too')
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *a):
                pass

        cutr = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TruncatedRefusal)
        threading.Thread(target=cutr.serve_forever, daemon=True).start()
        try:
            status, answer = Api(f"http://127.0.0.1:{cutr.server_address[1]}/api/builds", "t").upload(
                "/x", {}, blob, 1, timeout=10)
            check("h9-api/transport: a truncated REFUSAL keeps its status (413), never status 0 retried as transport",
                  status == 413, (status, answer))
        finally:
            cutr.shutdown()
            cutr.server_close()

        # an IPv6 literal with no explicit port keeps its address (http.client would read `::1` as host `:` port 1)
        check("h9-api/transport: an IPv6 API with no port connects to that address on the scheme's port",
              upload_endpoint(urllib.parse.urlsplit("https://[::1]/api/builds")) == ("::1", 443)
              and upload_endpoint(urllib.parse.urlsplit("http://[fd00::5]/api/builds")) == ("fd00::5", 80)
              and upload_endpoint(urllib.parse.urlsplit("http://api:8080/x")) == ("api", 8080))

        # a complete REFUSAL whose body is not UTF-8 keeps its status, never status 0 retried as transport
        class _BinaryRefusal(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = b"\xff\xfe\x00refused"
                self.send_response(413)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        binr = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BinaryRefusal)
        threading.Thread(target=binr.serve_forever, daemon=True).start()
        try:
            status, answer = Api(f"http://127.0.0.1:{binr.server_address[1]}/api/builds", "t").upload(
                "/x", {}, blob, 1, timeout=10)
            check("h9-api/transport: a refusal with a non-UTF-8 body keeps its status (413)", status == 413, (status, answer))
        finally:
            binr.shutdown()
            binr.server_close()

        # HTTPS: a TLS 1.3 server sends its session tickets right after the handshake, which makes the socket READABLE
        # before any HTTP answer exists. The upload must keep sending through that, not stop the body and wait for an
        # answer the server will only send once the body arrived — and an early refusal over TLS is still read.
        tls_dir = tempfile.mkdtemp(prefix="build-run-tls-", dir=tmp)
        with open(os.path.join(tls_dir, "test.pem"), "w") as fh:
            fh.write(_SELFTEST_TLS_PEM)
        srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv_ctx.load_cert_chain(os.path.join(tls_dir, "test.pem"))

        class _TlsServer(http.server.ThreadingHTTPServer):
            def get_request(self):
                c, a = super().get_request()
                return srv_ctx.wrap_socket(c, server_side=True), a

        tls_got: dict = {}

        class _TlsUpload(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                if self.path.startswith("/api/builds/refuse"):
                    payload = json.dumps({"error": "artifact-too-large: over TLS"}).encode()
                    self.send_response(413)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                    self.close_connection = True      # the body is never USED — only discarded, below
                    # A LINGERING close, as a real server makes one (Kestrel, nginx): the answer is sent BEFORE the
                    # body is read, then the body is read and thrown away until the client hangs up. An ABORTIVE close
                    # here — close() with the body still unread — makes the kernel answer with a RST that DISCARDS this
                    # server's own still-unsent answer (Linux tcp_close, data_was_unread), so the client met an EOF
                    # with no 413 at all: no client can read an answer that was never transmitted, and the check went
                    # red on runners whenever the answer was still queued (Deploy main, 2026-10-29: `(0, 'EOF occurred
                    # in violation of protocol')`). Bounded by the declared length and by the upload's own budget.
                    self.connection.settimeout(30)
                    left_ = int(self.headers.get("Content-Length", "0"))
                    try:
                        while left_ > 0:
                            chunk = self.rfile.read1(min(left_, 1 << 16))
                            if not chunk:
                                break
                            left_ -= len(chunk)
                    except OSError:
                        pass                          # the client read the answer and hung up: the drain is over
                    return
                n = int(self.headers.get("Content-Length", "0"))
                tls_got["body"] = self.rfile.read(n)
                payload = json.dumps({"ok": True}).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        def _trusting(*a, **k):
            c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            c.check_hostname = False
            c.verify_mode = ssl.CERT_NONE
            return c

        tls = _TlsServer(("127.0.0.1", 0), _TlsUpload)
        threading.Thread(target=tls.serve_forever, daemon=True).start()
        default_https = ssl._create_default_https_context
        ssl._create_default_https_context = _trusting
        try:
            tls_api = Api(f"https://127.0.0.1:{tls.server_address[1]}/api/builds", "t")
            t0 = time.monotonic()
            status, answer = tls_api.upload("/ok", {}, blob, len(data), timeout=10)
            check("h9-api/transport: an HTTPS upload sends its whole body through the TLS session tickets and reads the answer",
                  status == 201 and answer == {"ok": True} and tls_got.get("body") == data and time.monotonic() - t0 < 5,
                  (status, answer, len(tls_got.get("body") or b""), round(time.monotonic() - t0, 2)))
            status, answer = tls_api.upload("/refuse", {}, big, os.path.getsize(big), timeout=30)
            check("h9-api/transport: an HTTPS answer sent BEFORE the body was read (413) is reported",
                  status == 413 and _answer_text(answer).startswith("artifact-too-large"), (status, answer))
        finally:
            ssl._create_default_https_context = default_https
            tls.shutdown()
            tls.server_close()

        # the workspace stays until every receipt was acknowledged
        hk = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): [(503, None)]}),
                  env=dict(env_s, MW_BUILD_ARTIFACT_STORE="file:" + store_dir), store_runner=fake_store,
                  checkout=checkout_with({"stages": [{"name": "modules", "kind": "build", "command": art_cmd}]}))
        before = set(os.listdir(tmp))
        check("h9/unacknowledged receipts", hk.run(claim) == "undelivered")
        kept = [n for n in set(os.listdir(tmp)) - before if n.startswith("MeshWeaver.Plugins-")]
        check("h9/the workspace is kept while receipts are unacknowledged", len(kept) == 1, kept)
        old = time.time() + 3 * hk.run_deadline
        check("h9/a kept workspace is swept after two deadlines", kept and kept[0] in hk.sweep_workspaces(now=old), kept)

        # the PLAN: host-emitted first, naming every stage it will run; seq numbers per stage
        posted.clear()
        seen_seq: list[tuple] = []

        def rec(path, body):
            seen_seq.append((path.split("/stages/")[1].split("/")[0], body["seq"], [e["kind"] for e in body["events"]]))
            posted.append((path, body["events"]))
            return 202, None
        hp = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): rec}), env=env_s,
                  checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"},
                                                     {"name": "b", "kind": "test", "command": "true"}]}))
        hp.run(claim)
        kinds = [e for _, evs in posted for e in evs if e["kind"] == "plan"]
        check("plan/one plan, naming every stage the host will run (and, for a PR run, its merge base)",
              [{k: v for k, v in p_.items() if k != "budgetMinutes"} for p_ in kinds]
              == [{"kind": "plan", "stages": ["a", "b"], "mergeBase": "e" * 40}], kinds)
        # the declared budget: the stages' caps + margin, within the deadline, in whole minutes 10..720
        check("budget/the plan declares budgetMinutes", kinds and kinds[0].get("budgetMinutes") == budget_minutes(
            run_budget_seconds([{"timeoutMinutes": 45}, {"timeoutMinutes": 45}], hp.run_deadline)), kinds)
        check("budget/two 40+45-minute stages fit the default deadline",
              run_budget_seconds([{"timeoutMinutes": 40}, {"timeoutMinutes": 45}], DEFAULT_RUN_DEADLINE) == 85 * 60 + RUN_MARGIN_SECONDS)
        check("budget/a declared prepared input's build cap and key allowance are inside the budget",
              run_budget_seconds([{"timeoutMinutes": 1}], DEFAULT_RUN_DEADLINE, [{"timeoutMinutes": 20}])
              == 60 + RUN_MARGIN_SECONDS + 20 * 60 + 2 * PREPARED_KEY_SECONDS)
        check("budget/capped by the deadline", run_budget_seconds([{"timeoutMinutes": 45}] * 4, 2100) == 2100)
        check("budget/a NON-trunk run reserves the largest prepared cap: the default branch's declaration may be the one built",
              run_budget_seconds([{"timeoutMinutes": 1}], DEFAULT_RUN_DEADLINE, [{"timeoutMinutes": 1}], trunk=False)
              == 60 + RUN_MARGIN_SECONDS + DEFAULT_STAGE_TIMEOUT_MINUTES * 60 + 2 * PREPARED_KEY_SECONDS)
        check("budget/negative control: a trunk run builds its OWN declaration and reserves its own cap",
              run_budget_seconds([{"timeoutMinutes": 1}], DEFAULT_RUN_DEADLINE, [{"timeoutMinutes": 1}], trunk=True)
              == 60 + RUN_MARGIN_SECONDS + 60 + 2 * PREPARED_KEY_SECONDS)
        check("budget/2100 s declares 35 minutes", budget_minutes(2100) == 35)
        check("budget/minutes round up and stay in 10..720",
              budget_minutes(61) == 10 and budget_minutes(661) == 12 and budget_minutes(10 ** 6) == 720)
        firsts = [k for _, _, ks in seen_seq for k in ks if k != "line"]
        check("plan/the plan precedes every stage verdict", firsts and firsts[0] == "plan", firsts)
        per_stage: dict = {}
        for st_, sq, _ in seen_seq:
            per_stage.setdefault(st_, []).append(sq)
        check("seq/each stage's batches are numbered 1, 2, …", all(v == list(range(1, len(v) + 1)) for v in per_stage.values()), per_stage)
        # a retried batch keeps its number
        tries: list[int] = []
        answers = [503, 202]
        sq = EventSink(_FakeApi({("POST", "/r/"): lambda p, b: (tries.append(b["seq"]) or answers.pop(0), None)}),
                       "h", "r", "a" * 40, "s", "latest", lambda m: None, sleep=lambda x: None)
        sq.add([{"kind": "line", "text": "x"}])
        sq.flush(attempts=3)
        check("seq/a retried batch keeps its number", tries == [1, 1] and sq.seq == 1, tries)
        # a LOST acknowledgement: the retry re-sends the SAME payload under the SAME seq; events added
        # meanwhile go under the NEXT seq
        sent: list[tuple] = []
        replies = [503, 202, 202]
        lk = EventSink(_FakeApi({("POST", "/r/"): lambda p_, b: (sent.append((b["seq"], json.dumps(b["events"]))) or replies.pop(0), None)}),
                       "h", "r", "a" * 40, "s", "latest", lambda m: None, sleep=lambda x: None)
        lk.buf = [{"kind": "line", "text": "first"}]
        lk.flush(attempts=1)                       # the server committed it; the answer was lost (503)
        lk.add([{"kind": "stage-finished", "state": "succeeded", "summary": "s"}])
        lk.flush(attempts=3)
        check("seq/a retried batch is byte-identical under its seq", sent[0] == sent[1] and sent[0][0] == 1, sent)
        check("seq/events added meanwhile go under the next seq", len(sent) == 3 and sent[2][0] == 2
              and "stage-finished" in sent[2][1] and "stage-finished" not in sent[1][1], sent)
        # the heartbeat: an empty batch once HEARTBEAT_SECONDS passed with nothing posted
        now = {"t": 0.0}
        hbp: list = []
        hs = EventSink(_FakeApi({("POST", "/r/"): lambda p, b: (hbp.append(b), (202, None))[1]}),
                       "h", "r", "a" * 40, "s", "latest", lambda m: None, sleep=lambda x: None, clock=lambda: now["t"])
        hs.beat()
        check("lease/no heartbeat before the period", hbp == [], hbp)
        now["t"] = HEARTBEAT_SECONDS + 1
        hs.add([])
        check("lease/an empty batch is the heartbeat", hbp == [{"host": "h", "claimId": "", "seq": 1, "events": []}], hbp)
        check("lease/the heartbeat period is under a minute", HEARTBEAT_SECONDS < 60)
        hbt = Heartbeat(period=0.05)
        hbt.target = hs
        now["t"] = 2 * HEARTBEAT_SECONDS + 2
        with hbt:
            time.sleep(0.3)
        check("lease/the run's heartbeat thread beats the current sink", len(hbp) >= 2, hbp)
        check("lease/a healthy heartbeat names no loss", not hbt.lost.is_set())

        # Deploy#49: a beat that meets the 409 SETS the heartbeat's loss — a transient fault does not.
        class _Beats:
            def __init__(self, fault):
                self.fault, self.n = fault, 0

            def beat(self):
                self.n += 1
                raise self.fault
        gone_beat = _Beats(Lost("409"))
        hbl = Heartbeat(period=0.02)
        hbl.target = gone_beat
        with hbl:
            hbl.lost.wait(2)
        check("lease/a 409 on a beat sets the heartbeat's loss", hbl.lost.is_set() and gone_beat.n == 1, gone_beat.n)
        flaky = _Beats(OSError("reset"))
        hbf = Heartbeat(period=0.02)
        hbf.target = flaky
        with hbf:
            time.sleep(0.2)
        check("lease/negative control: a transient fault on a beat is NOT a loss (it is retried)",
              not hbf.lost.is_set() and flaky.n >= 3, flaky.n)
        # …and a sandboxed preparation command is KILLED on the loss, never waited out.
        with tempfile.TemporaryDirectory() as ptmp:
            hp = Host({"MW_BUILD_CHANNEL": "latest", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}, _FakeApi({}),
                      log=lambda s: None, sandbox=_EnvSandbox(), leftovers=lambda: [], sleep=lambda s: None)
            gone = threading.Event()
            threading.Timer(0.3, gone.set).start()
            t0 = time.monotonic()
            try:
                hp._sandboxed_capture("exec sleep 30", ptmp, ptmp, ptmp, {}, [], [], 60, cancel=gone)
                killed = False
            except Lost:
                killed = True
            check("prepare/a key or build command is killed when the claim is lost (Lost, in seconds, not its timeout)",
                  killed and time.monotonic() - t0 < 10, (killed, time.monotonic() - t0))
            check("prepare/negative control: a command with no loss runs to its own exit",
                  hp._sandboxed_capture("echo prepared", ptmp, ptmp, ptmp, {}, [], [], 60, cancel=threading.Event())
                  == (0, "prepared\n"))
            check("prepare/a command past its timeout is still ended as before (124)",
                  hp._sandboxed_capture("exec sleep 30", ptmp, ptmp, ptmp, {}, [], [], 1)[0] == 124)
        # claimId: fresh per claim attempt, the SAME one on a network-error retry
        ids: list[str] = []
        ans = [(0, "reset"), (503, None), (204, None), (204, None)]

        def on_claim_id(path, body):
            ids.append(body["claimId"])
            return ans.pop(0)
        hc = host(_FakeApi({("POST", "/claim"): on_claim_id}))
        hc.claim()
        check("claim/a network error retries the SAME claimId", len(ids) == 3 and len(set(ids)) == 1, ids)
        hc.claim()
        check("claim/a new claim attempt carries a fresh claimId", len(ids) == 4 and ids[3] != ids[0], ids)
        check("claim/the claimId is a uuid", all(len(i) == 36 and i.count("-") == 4 for i in ids), ids)
        # ONE merged tree per attempt: null → main's tip resolved ONCE and reused; non-null → verbatim;
        # trunk → no merge
        def merge_host(gitlog, tip="e" * 40, merge_rc=0, merge_out="", command="true"):
            def git(args, env_):
                # The warm NuGet cache's plan reads the mirror too (rev-parse → show → ls-tree); those reads are not the
                # merge's, so they are left out of the log these cases assert on — the rev-parse it opened with included.
                if "show" in args or "ls-tree" in args:
                    if "show" in args and gitlog and "rev-parse" in gitlog[-1]:
                        gitlog.pop()
                    return 0, ""
                gitlog.append(args)
                if "rev-parse" in args:
                    return 0, tip + "\n"
                if "merge" in args and "--abort" not in args:
                    return merge_rc, "CONFLICT" if merge_rc else merge_out
                return 0, ""
            posted.clear()
            return host(api_ev(), env=env_s, git_runner=git, checkout=checkout_with({"stages": [
                {"name": "a", "kind": "build", "command": "true"}, {"name": "b", "kind": "test", "command": command}]}))
        plans = lambda: [e for _, es in posted for e in es if e["kind"] == "plan"]
        gl: list = []
        merge_host(gl).run(dict(claim, isTrunk=False, mergeBaseSha=None))
        check("merge/null: main's tip resolved ONCE", sum(1 for a in gl if "rev-parse" in a) == 1, gl)
        check("merge/null: merged ONCE for every stage", sum(1 for a in gl if "merge" in a) == 1, gl)
        check("merge/null: the plan names the resolved base", plans() and plans()[0].get("mergeBase") == "e" * 40, plans())
        gl = []
        merge_host(gl).run(dict(claim, isTrunk=False, mergeBaseSha="F" * 40))
        check("merge/non-null: used VERBATIM, never re-resolved", not any("rev-parse" in a for a in gl)
              and any(a[-1] == "f" * 40 and "fetch" in a for a in gl) and plans()[0].get("mergeBase") == "f" * 40, (gl, plans()))
        gl = []
        merge_host(gl).run(dict(claim, isTrunk=True, mergeBaseSha=None))
        check("merge/trunk: no merge, no mergeBase", gl == [] and "mergeBase" not in plans()[0], (gl, plans()))
        gl = []
        merge_host(gl).run(dict(claim, isTrunk=False, kind="probe", mergeBaseSha=None))
        check("merge/probe: no merge, no mergeBase", gl == [] and "mergeBase" not in plans()[0], (gl, plans()))
        gl = []
        merge_host(gl).run(dict(claim, isTrunk=False, isProbe=True, mergeBaseSha=None))
        check("merge/isProbe: no merge, no mergeBase", gl == [] and "mergeBase" not in plans()[0], (gl, plans()))
        gl = []
        merge_host(gl).run(dict(claim, isTrunk=False, isProbe=True, mergeBaseSha=None,
                                refs=[{"kind": "PullRequest", "name": "probe/p-1", "number": 5105, "baseRef": "main"}]))
        check("merge/a probe NAMING a pull request is merged once onto main's tip, and its plan names the base",
              sum(1 for a in gl if "merge" in a) == 1 and plans() and plans()[0].get("mergeBase") == "e" * 40, (gl, plans()))
        gl = []
        merge_host(gl).run(dict(claim, isTrunk=False, isProbe=True, mergeBaseSha=None,
                                refs=[{"kind": 1, "name": "probe/p-2", "number": 5107, "baseRef": "main"}]))
        check("merge/a PR-named probe whose kind arrives as the NUMBER the claim answer writes is merged too",
              sum(1 for a in gl if "merge" in a) == 1 and plans()[0].get("mergeBase") == "e" * 40, (gl, plans()))
        # a probe run's id rides on every events POST (and the token request); a normal run's never
        pq: list = []
        hpq = host(_FakeApi({("POST", "/token"): lambda p_, b: (pq.append(("token", None)), (404, None))[1],
                             ("POST", "/MeshWeaver.Plugins/"): lambda p_, b: (202, None)}), env=env_s,
                   checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]}))
        hpq.run(dict(claim, isTrunk=False, isProbe=True, probe="p-123"))
        evq = [c[3] for c in hpq.api.calls if c[1].endswith("/events")]
        check("probe/every events POST carries &probe=<id>", evq and all(q.get("probe") == "p-123" and q.get("channel") == "stable" for q in evq), evq)
        check("probe/the token request carries it too", any(c[1] == "/token" and c[3].get("probe") == "p-123" for c in hpq.api.calls), hpq.api.calls[:2])
        hnq = host(_FakeApi({("POST", "/token"): [(404, None)], ("POST", "/MeshWeaver.Plugins/"): [(202, None)]}), env=env_s,
                   checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]}))
        hnq.run(dict(claim, probe=None))
        check("probe/a normal run carries no probe", all("probe" not in c[3] for c in hnq.api.calls if c[1].endswith("/events")), hnq.api.calls[:3])
        gl = []
        merge_host(gl, merge_rc=1).run(dict(claim, isTrunk=False, mergeBaseSha=None))
        rf = [e for _, es in posted for e in es if e["kind"] == "run-finished"]
        check("merge/a conflict is red, never a test of the unmerged head",
              rf and rf[0]["succeeded"] is False and "does not merge" in rf[0]["error"] and not plans(), (rf, plans()))
        gl = []
        merge_host(gl).run(dict(claim, isTrunk=False, mergeBaseSha="not-a-sha"))
        rf = [e for _, es in posted for e in es if e["kind"] == "run-finished"]
        check("merge/a malformed mergeBaseSha is refused", rf and "not a full commit" in rf[0]["error"], rf)
        # the merged base reaches every stage as MW_BUILD_MERGE_BASE — with a merge commit AND when the head already
        # contained the base (git: "Already up to date", no commit), so a selector narrows in both; UNSET when unmerged
        seen_mb = os.path.join(tmp, "seen-merge-base")
        for label, kw, run_kw, want in (
                ("a merge commit", {}, {"isTrunk": False, "mergeBaseSha": None}, "e" * 40),
                ("an already-up-to-date head", {"merge_out": "Already up to date.\n"},
                 {"isTrunk": False, "mergeBaseSha": "D" * 40}, "d" * 40),
                ("a trunk run", {}, {"isTrunk": True, "mergeBaseSha": None}, "unset"),
                ("a plain probe", {}, {"isTrunk": False, "isProbe": True, "mergeBaseSha": None}, "unset")):
            if os.path.exists(seen_mb):
                os.remove(seen_mb)
            gl = []
            merge_host(gl, command=f"echo \"[${{MW_BUILD_MERGE_BASE-unset}}]\" >> {seen_mb}", **kw).run(dict(claim, **run_kw))
            got = open(seen_mb).read().strip() if os.path.exists(seen_mb) else "<stage did not run>"
            check(f"merge/{label}: the stage sees MW_BUILD_MERGE_BASE={want[:12]}", got == f"[{want}]", (got, gl))

        # every batch carries the assignment's claimId; a stale-claim 409 ends the run; 503 is retried
        bodies: list[dict] = []
        hci = host(_FakeApi({("POST", "/claim"): [(200, dict(claim, stages=[]))], ("POST", "/token"): [(404, None)],
                             ("POST", "/MeshWeaver.Plugins/"): lambda p_, b: (bodies.append(b), (202, None))[1]}), env=env_s,
                   checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": "true"}]}))
        got = hci.claim()
        hci.run(got)
        check("claimId/every batch carries the assignment's claimId",
              bodies and all(b.get("claimId") == hci.claim_id and len(hci.claim_id) == 36 for b in bodies), bodies[:2])
        stale = host(_FakeApi({("POST", "/token"): [(404, None)],
                               ("POST", "/MeshWeaver.Plugins/"): [(409, {"error": "stale-claim"})]}), env=env_s,
                     checkout=checkout_with({"stages": [{"name": "a", "kind": "build", "command": f"sleep 30; echo ran > {tmp}/stale"}]}))
        t1 = time.monotonic()
        check("claimId/a stale-claim 409 abandons the run and kills the child",
              stale.run(dict(claim, claimId="old")) == "lost" and time.monotonic() - t1 < 20
              and not os.path.exists(os.path.join(tmp, "stale")), time.monotonic() - t1)
        tries503: list[int] = []
        ans503 = [503, 503, 202]
        s503 = EventSink(_FakeApi({("POST", "/r/"): lambda p_, b: (tries503.append(b["seq"]) or ans503.pop(0), None)}),
                         "h", "r", "a" * 40, "s", "latest", lambda m: None, sleep=lambda x: None, claim_id="c")
        s503.add([{"kind": "line", "text": "x"}])
        check("503/retried with the same batch and seq, never a failure", s503.flush(attempts=6) and tries503 == [1, 1, 1], tries503)
        check("sandbox/TMPDIR is the stage's tmpfs scratch", sandbox_env({}, {}, "/cache")["TMPDIR"] == f"{SCRATCH}/tmp"
              and f"--tmpfs {SCRATCH}" in " ".join(Sandbox("bwrap", []).argv("x", "/w/s", "/w/o", {"PATH": "/bin"})))
        # 409 stale-claim-id: a FRESH id, once; 409 stale-set: drain, no new claim
        sid: list[str] = []
        sans = [(409, {"error": "stale-claim-id"}), (204, None)]
        hsc = host(_FakeApi({("POST", "/claim"): lambda p_, b: (sid.append(b["claimId"]), sans.pop(0))[1]}))
        hsc.claim()
        check("claim/stale-claim-id → the next request carries a DIFFERENT claimId", len(sid) == 2 and sid[0] != sid[1], sid)
        sid.clear()
        sans2 = [(409, {"error": "stale-claim-id"}), (409, {"error": "stale-claim-id"}), (204, None)]
        hsc2 = host(_FakeApi({("POST", "/claim"): lambda p_, b: (sid.append(b["claimId"]), sans2.pop(0))[1]}))
        check("claim/stale-claim-id twice → once more only, never a loop", hsc2.claim() is None and len(sid) == 2 and not hsc2.stopping, sid)
        sid.clear()
        hss = host(_FakeApi({("POST", "/claim"): lambda p_, b: (sid.append(b["claimId"]), (409, {"error": "stale-set"}))[1]}))
        check("claim/stale-set → drained, no work claimed", hss.claim() is None and len(sid) == 1 and hss.drained, sid)
        # Memex#885: the answer reads the LIVE record, which can change long before a replacement replica exists.
        # A drained host that EXITS is restarted by its Deployment and drains again — CrashLoopBackOff, no host.
        check("claim/stale-set NEVER stops the host (no exit, no CrashLoopBackOff on an unapplied record)",
              not hss.stopping, hss.stopping)
        sid.clear()
        back = [(409, {"error": "stale-set"}), (204, None)]
        hsb = host(_FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})] * 3,
                             ("POST", "/claim"): lambda p_, b: (sid.append(b["claimId"]), back.pop(0))[1]}))
        first, second = hsb.cycle(), hsb.cycle()
        check("claim/a drained host keeps running and asks again; its set current again → it claims again",
              first == "drained" and second == "idle" and not hsb.drained and not hsb.stopping and len(sid) == 2,
              (first, second, sid))
        slept: list[float] = []
        hsl = host(_FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})] * 3,
                             ("POST", "/claim"): [(409, {"error": "stale-set"})] * 3}))

        def stop_after_two(_s):
            slept.append(_s)
            if len(slept) >= 2:
                hsl.stopping = True        # what the SIGTERM handler does
        hsl.sleep = stop_after_two
        check("claim/a drained host's loop idles until SIGTERM, then exits 0",
              hsl.loop() == 0 and len(slept) == 2 and all(x >= 60 for x in slept), slept)
        # the plugins layer is in the cache key in EITHER mode
        hk1 = host(_FakeApi({}))
        hk1.plugins_digest, hk1.plugins_key = "sha256:" + "a" * 64, None
        check("key/the seal's digest keys the plugins layer when there is no index key", hk1.effective_plugins_key() == "sha256:" + "a" * 64)
        hk1.plugins_key = "sha256:" + "b" * 64
        check("key/the index key wins when present", hk1.effective_plugins_key() == "sha256:" + "b" * 64)
        # a refusal's JSON body reaches claim() through the real transport
        import http.server
        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.dumps({"error": "stale-set"}).encode()
                self.send_response(409); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        st_, body_ = Api(f"http://localhost:{srv.server_address[1]}/api/builds", "t").call("POST", "/claim", {})
        srv.server_close()
        check("api/a 409's JSON body is kept beside its status", st_ == 409 and body_ == {"error": "stale-set"}, (st_, body_))
        # the stage declaration is read from the MERGED tree
        def merging_git(args, env_):
            if "rev-parse" in args:
                return 0, "e" * 40 + "\n"
            if "merge" in args and "--abort" not in args:
                gs = os.path.join(args[1], ".github", "build-stages.json")
                json.dump({"stages": [{"name": "merged", "kind": "build", "command": "true"}]}, open(gs, "w"))
            return 0, ""
        posted.clear()
        hmt = host(api_ev(), env=env_s, git_runner=merging_git, checkout=checkout_with({"stages": [
            {"name": "head", "kind": "build", "command": "true"}]}))
        hmt.run(dict(claim, isTrunk=False, mergeBaseSha=None))
        pl = [e for _, es in posted for e in es if e["kind"] == "plan"]
        check("merge/the stages run are the MERGED tree's declaration", pl and pl[0]["stages"] == ["merged"], pl)
        # the run budget
        check("budget/the default deadline covers the declared stages and stays in range",
              MIN_RUN_DEADLINE <= DEFAULT_RUN_DEADLINE <= MAX_RUN_DEADLINE)
        for bad_deadline in ("60", str(721 * 60)):
            try:
                host(_FakeApi({}), env=dict(env, MW_BUILD_RUN_DEADLINE_SECONDS=bad_deadline)).preflight()
                check(f"budget/a run deadline of {bad_deadline} s is refused", False, "started")
            except Refusal as e:
                check("budget/the refusal names the RUN budget", "RUN budget" in str(e), e)
        # a VERSIONED build server (Doc/Architecture/DeployTestSwitchLatest §3): it claims, reports and asks for its
        # token by VERSION, never by channel — and exactly one of the two is set.
        env_v = {k: v for k, v in dict(env, MW_BUILD_VERSION="3.1.10412", BUILD_TOKEN_FILE=tf,
                                       MW_BUILD_TESTER_DIGEST="sha256:" + "a" * 64, MW_BUILD_PORTAL_DIGEST="sha256:" + "b" * 64,
                                       MW_BUILD_RUNNER_DIGEST="sha256:" + "c" * 64).items() if k != "MW_BUILD_CHANNEL"}
        host(_FakeApi({}), env=env_v).preflight()
        for bad_env, why in ((dict(env_v, MW_BUILD_CHANNEL="latest"), "exactly ONE"),
                             (dict(env_v, MW_BUILD_PLATFORM_VERSION="3.1.10500"), "equal to MW_BUILD_VERSION"),
                             (dict(env_v, MW_BUILD_VERSION="3.1", MW_BUILD_PLATFORM_VERSION="3.1"), "MAJOR.MINOR.PATCH"),
                             (dict(env_v, MW_BUILD_RUNNER_DIGEST=""), "MW_BUILD_RUNNER_DIGEST as a sha256"),
                             (dict(env_v, MW_BUILD_TESTER_DIGEST="sha256:short"), "MW_BUILD_TESTER_DIGEST as a sha256"),
                             (dict(env_v, MW_BUILD_STAGE_NETWORK="none"), "MW_BUILD_STAGE_NETWORK as open or offline")):
            try:
                host(_FakeApi({}), env=bad_env).preflight()
                check(f"version/refused: {why}", False, "started")
            except Refusal as e:
                check(f"version/refused: {why}", why in str(e), e)
        # THE COMPILE REFERENCE SET: the host does not start on a set core's compile-check.py would refuse.
        # NEGATIVE CONTROL: the staging the first probe met (the portal's /app alone) is refused by name.
        cr = tempfile.mkdtemp(prefix="compile-refs-")
        try:
            open(os.path.join(cr, "MeshWeaver.Messaging.Hub.dll"), "wb").close()
            try:
                host(_FakeApi({}), env=dict(env_v, MW_BUILD_COMPILE_REFS=cr)).preflight()
                check("compile refs/the portal's /app alone (the old staging) is refused", False, "started")
            except Refusal as e:
                check("compile refs/the portal's /app alone (the old staging) is refused",
                      "NO shared frameworks" in str(e) and "MW_BUILD_COMPILE_REFS" in str(e), e)
            fx = os.path.join(cr, "shared-frameworks", "Microsoft.NETCore.App", "10.0.0")
            os.makedirs(fx)
            open(os.path.join(fx, "System.Private.CoreLib.dll"), "wb").close()
            check("compile refs/NEGATIVE CONTROL: a set without System.Reactive.dll is refused (core's checker refuses it too)",
                  "System.Reactive.dll" in (compile_refs_refusal(cr) or ""), compile_refs_refusal(cr))
            # Deploy#58: core's compile-check excludes sdk-generators/ and analyzers/ BEFORE it looks for Rx, so an Rx
            # that lives only there is refused there (exit 4) — the preflight must refuse it too, never pass it on.
            for tooling in sorted(COMPILE_REFS_TOOLING_DIRS):
                os.makedirs(os.path.join(cr, tooling, "dotnet", "cs"), exist_ok=True)
                open(os.path.join(cr, tooling, "dotnet", "cs", "System.Reactive.dll"), "wb").close()
                check(f"compile refs/an Rx only under {tooling}/ is refused, as core's compile-check refuses it",
                      "System.Reactive.dll" in (compile_refs_refusal(cr) or ""), compile_refs_refusal(cr))
                shutil.rmtree(os.path.join(cr, tooling))
            os.makedirs(os.path.join(fx, "analyzers"))
            os.rename(os.path.join(fx, "System.Private.CoreLib.dll"), os.path.join(fx, "analyzers", "System.Private.CoreLib.dll"))
            open(os.path.join(cr, "System.Reactive.dll"), "wb").close()
            check("compile refs/a CoreLib only under analyzers/ is refused",
                  "NO shared frameworks" in (compile_refs_refusal(cr) or ""), compile_refs_refusal(cr))
            os.rename(os.path.join(fx, "analyzers", "System.Private.CoreLib.dll"), os.path.join(fx, "System.Private.CoreLib.dll"))
            os.rmdir(os.path.join(fx, "analyzers"))
            os.remove(os.path.join(cr, "System.Reactive.dll"))
            # Deploy#58: a set bound read-only over the stages' writable scratch would hide it after the host started.
            for over in (f"{SCRATCH}/tmp", f"{SCRATCH}/home/refs", f"{SCRATCH}/nuget", SCRATCH):
                check(f"compile refs/a set at {over} (over the writable scratch) is refused before the host starts",
                      "writable" in (compile_refs_refusal(over) or ""), compile_refs_refusal(over))
            check("compile refs/a set beside the scratch dirs is not an overlap",
                  "writable" not in (compile_refs_refusal(f"{SCRATCH}/refs-x") or ""), compile_refs_refusal(f"{SCRATCH}/refs-x"))
            open(os.path.join(cr, "System.Reactive.dll"), "wb").close()
            host(_FakeApi({}), env=dict(env_v, MW_BUILD_COMPILE_REFS=cr)).preflight()
            check("compile refs/the configured directory is bound read-only into every stage's sandbox",
                  cr in sandbox_readonly_roots({"MW_BUILD_COMPILE_REFS": cr})
                  and f"--ro-bind-try {cr} {cr}" in " ".join(Sandbox("bwrap", sandbox_readonly_roots({"MW_BUILD_COMPILE_REFS": cr})).argv("true", "/w", "/o", {})),
                  sandbox_readonly_roots({"MW_BUILD_COMPILE_REFS": cr}))
            check("compile refs/the prepared set (/app + shared-frameworks) starts", compile_refs_refusal(cr) is None)
            # VISIBILITY, not just the argument: a root under /tmp or /scratch is bound AFTER the tmpfs that would
            # otherwise cover it (binds first, then a tmpfs, hid the root while the argument check still passed).
            for root in ("/tmp/refs-x", f"{SCRATCH}/refs-x", cr):
                av = Sandbox("bwrap", [root]).argv("true", "/w", "/o", {})
                bind_at = max(i for i in range(len(av) - 2) if av[i:i + 3] == ["--ro-bind-try", root, root])
                tmpfs_at = [i for i in range(len(av) - 1) if av[i] == "--tmpfs"]
                check(f"compile refs/a read-only root at {root} is bound AFTER every tmpfs (visible in the sandbox)",
                      all(t < bind_at for t in tmpfs_at), av)
            os.remove(os.path.join(cr, "MeshWeaver.Messaging.Hub.dll"))
            check("compile refs/a set without the portal's MeshWeaver assemblies is refused",
                  "no MeshWeaver" in (compile_refs_refusal(cr) or ""), compile_refs_refusal(cr))
            check("compile refs/a missing directory is refused",
                  "not a directory" in (compile_refs_refusal(os.path.join(cr, "absent")) or ""))
            check("compile refs/handed to a stage through the allow-list",
                  sandbox_env({"PATH": "/bin", "MW_BUILD_COMPILE_REFS": cr}, {}, "/cache").get("MW_BUILD_COMPILE_REFS") == cr)
        finally:
            shutil.rmtree(cr, ignore_errors=True)
        claims_v: list[dict] = []
        hv_claim = host(_FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})],
                                  ("POST", "/claim"): lambda p_, b: (claims_v.append(b), (204, None))[1]}), env=env_v)
        hv_claim.cycle()
        check("version/the claim names the version and no channel",
              claims_v and claims_v[0].get("version") == "3.1.10412" and "channel" not in claims_v[0], claims_v)
        iq = [c[3] for c in hv_claim.api.calls if c[1] == "/hosts/inputs"]
        check("version/the inputs request names the version and no channel", iq and iq[0] == {"version": "3.1.10412"}, iq)
        check("version/the claim proves the host's set: its tester, portal and runner digests",
              claims_v and claims_v[0].get("testerDigest") == "sha256:" + "a" * 64 and claims_v[0].get("portalDigest") == "sha256:" + "b" * 64
              and claims_v[0].get("runnerDigest") == "sha256:" + "c" * 64, claims_v)
        claims_c: list[dict] = []
        hc = host(_FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})],
                            ("POST", "/claim"): lambda p_, b: (claims_c.append(b), (204, None))[1]}))
        hc.cycle()
        check("version/a CHANNEL host without set digests in its env sends none", claims_c and "testerDigest" not in claims_c[0], claims_c)
        claims_cs: list[dict] = []
        env_cs = {k: v for k, v in env_v.items() if k != "MW_BUILD_VERSION"}
        env_cs.update({"MW_BUILD_CHANNEL": "mac", "MW_BUILD_CORE_TESTS_DIGEST": "not-a-digest"})
        host(_FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})],
                       ("POST", "/claim"): lambda p_, b: (claims_cs.append(b), (204, None))[1]}), env=env_cs).cycle()
        check("version/a CHANNEL host presents its tester, portal and runner digests (a serving channel proves the served set)",
              claims_cs and claims_cs[0].get("channel") == "mac" and "version" not in claims_cs[0]
              and claims_cs[0].get("testerDigest") == "sha256:" + "a" * 64 and claims_cs[0].get("portalDigest") == "sha256:" + "b" * 64
              and claims_cs[0].get("runnerDigest") == "sha256:" + "c" * 64, claims_cs)
        check("version/a channel host drops a malformed digest instead of sending it (the server would refuse the claim)",
              claims_cs and "coreTestsDigest" not in claims_cs[0], claims_cs)
        ic = [c[3] for c in hc.api.calls if c[1] == "/hosts/inputs"]
        check("version/a channel host's inputs request names its channel", ic and set(ic[0]) == {"channel"}, ic)
        check("version/no core-tests image in the env → none in the claim", claims_v and "coreTestsDigest" not in claims_v[0], claims_v)
        claims_t: list[dict] = []
        host(_FakeApi({("GET", "/hosts/inputs"): [(200, {"plugins": None})],
                       ("POST", "/claim"): lambda p_, b: (claims_t.append(b), (204, None))[1]}),
             env=dict(env_v, MW_BUILD_CORE_TESTS_DIGEST="sha256:" + "d" * 64)).cycle()
        check("version/a set's core-tests image is proved on the claim too",
              claims_t and claims_t[0].get("coreTestsDigest") == "sha256:" + "d" * 64, claims_t)
        try:
            host(_FakeApi({}), env=dict(env_v, MW_BUILD_CORE_TESTS_DIGEST="nope")).preflight()
            check("version/refused: a malformed core-tests digest", False, "started")
        except Refusal as e:
            check("version/refused: a malformed core-tests digest", "MW_BUILD_CORE_TESTS_DIGEST" in str(e), e)
        posted.clear()
        av = api_ev()
        hv = host(av, env=env_v, checkout=checkout_with({"stages": [{"name": "s", "kind": "build", "command": "true"}]}))
        check("version/ran", hv.run(dict(claim, channel="stable")) == "ran")
        vq = [c[3] for c in av.calls if c[1].endswith("/events") or c[1] == "/token"]
        check("version/every event POST and the token request name the CLAIMED run's channel (the server finds the run by it)",
              vq and all(q.get("channel") == "stable" and "version" not in q for q in vq), vq)
        # a probe runs EXACTLY its requestedStages and plans only those
        posted.clear()
        hrq = host(api_ev(), env=env_s, checkout=checkout_with({"stages": [
            {"name": "a", "kind": "build", "command": f"echo a >> {tmp}/rq"}, {"name": "b", "kind": "test", "command": f"echo b >> {tmp}/rq"}]}))
        hrq.run(dict(claim, isProbe=True, probe="p-9", requestedStages=["b"]))
        pl = [e for _, es in posted for e in es if e["kind"] == "plan"]
        check("probe/requestedStages: only those stages are planned", pl and pl[0]["stages"] == ["b"], pl)
        check("probe/requestedStages: only those stages run", open(os.path.join(tmp, "rq")).read().split() == ["b"])
        posted.clear()
        hrq.run(dict(claim, isProbe=True, probe="p-9", requestedStages=[]))
        pl = [e for _, es in posted for e in es if e["kind"] == "plan"]
        check("probe/empty requestedStages = every declared stage", pl and pl[0]["stages"] == ["a", "b"], pl)
        posted.clear()
        hrq.run(dict(claim, isProbe=True, probe="p-9", requestedStages=["nope"]))
        rf = [e for _, es in posted for e in es if e["kind"] == "run-finished"]
        check("probe/a requested stage the repository does not declare is red", rf and rf[0]["succeeded"] is False, rf)
        # a SERVED run runs exactly the stages declared for the serving channel; an undeclared name is skipped, never red
        posted.clear()
        open(os.path.join(tmp, "rq"), "w").close()
        hrq.run(dict(claim, isTrunk=False, servedStages=["a", "not-a-stage"]))
        pl = [e for _, es in posted for e in es if e["kind"] == "plan"]
        rf = [e for _, es in posted for e in es if e["kind"] == "run-finished"]
        check("served/only the declared served stages are planned and run, an unknown name skipped",
              pl and pl[0]["stages"] == ["a"] and open(os.path.join(tmp, "rq")).read().split() == ["a"]
              and rf and rf[0]["succeeded"] is True, (pl, rf))
        posted.clear()
        open(os.path.join(tmp, "rq"), "w").close()
        hrq.run(dict(claim, isTrunk=False, servedStages=[]))
        pl = [e for _, es in posted for e in es if e["kind"] == "plan"]
        check("served/no servedStages = every declared stage", pl and pl[0]["stages"] == ["a", "b"], pl)
        posted.clear()
        open(os.path.join(tmp, "rq"), "w").close()
        hrq.run(dict(claim, isTrunk=False, servedStages=["renamed", "typo"]))
        rf = [e for _, es in posted for e in es if e["kind"] == "run-finished"]
        check("served/a list matching NO declared stage is red and runs nothing (never a green run of zero stages)",
              rf and rf[0]["succeeded"] is False and "match none" in (rf[0].get("error") or "")
              and open(os.path.join(tmp, "rq")).read().split() == [], rf)
        posted.clear()
        open(os.path.join(tmp, "rq"), "w").close()
        hrq.run(dict(claim, isTrunk=True, servedStages=["a"]))
        pl = [e for _, es in posted for e in es if e["kind"] == "plan"]
        check("served/a trunk run ignores servedStages: every declared stage is planned", pl and pl[0]["stages"] == ["a", "b"], pl)
        plan_from_child = OutputParser("/w", "x").feed('::mw-event::{"kind":"plan","stages":["x"]}')
        check("plan/a child cannot emit the plan", plan_from_child and "refused" in plan_from_child[0]["text"], plan_from_child)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"SELF-TEST FAILED: {f}")
        return 1
    print("build-run self-test: all cases passed (diagnostics, dedup, vstest + xunit results, structured events, "
          "stage declaration, the bwrap argv, the env allow-list, the token file, the startup sandbox probe, "
          "pid-namespace leftovers, one merged tree per PR attempt, the network-boundary self-check, trunk-only BUILD-stage cache promotion under recomputed keys, the plan, batch seq, the lease heartbeat, the claimId, the git mirror, the module-hub broker (host-computed fingerprints from a default-branch declaration, host-decided lane, hash-verified hand-over both ways), the warm workspace (main-only, advanced by the host from the tip; every run an incremental COPY it can never write back; wiped on a key change; a fresh clone on failure; never swept), cache scoping per stage, the NuGet fallback folder bound for every stage, the warm NuGet cache filled from the default branch's tip once per key (and never from a PR), the refset key, the H9 artifact hand-over (a file: store and the build API), the workload-identity pull, the registry bundle index (H8), plugins refresh BEFORE claim, fresh process per "
          "stage, fail-fast, channel + publish, 409, undeclared stages, stage timeout, run deadline)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
