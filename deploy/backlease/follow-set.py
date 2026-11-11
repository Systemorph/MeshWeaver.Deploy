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

THE CHART AND THE MAC'S VALUES FOLLOW TOO (MeshWeaver.Plugins Hosting/OneBuildTree §7, "the follower re-applies only the
set"). Before, a chart or values change reached a Mac only on a re-attach. Now each pass also reads this repository's
`main` (public; `--source-repo`/`--source-ref`). When its newest commit is GREEN (the chart gate's REQUIRED_CHECKS succeeded; a red check OUTSIDE the gate is named, never a hold — 2026-11-11: an unrelated publish job red for hours held both Macs off the chart that fixed their readiness) and
changed `deploy/helm` or `deploy/backlease` (their git tree ids), the pass stages that commit's chart, `build-host-mac.values.yaml`
and this script beside the agent (the next pass runs the new script), then applies them: a FULL `helm upgrade` — the
chart's defaults, the Mac values, the estate values first-attach set (ESTATE_KEYS, read back from the release) and the set —
never `--reuse-values`, which keeps the old values and ignores a new chart's defaults. A red or pending commit, or one
GitHub cannot answer, changes nothing, said on every pass while it lasts. GitHub is asked ONE question per pass while
main's head stays the same commit (the trees and the verdict are kept in `head.json`), so two Macs behind one address stay
far under the unauthenticated limit (60 an hour).

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
from xml.sax.saxutils import escape as xml_escape

LABEL = "com.memex.local.buildhost-follow"
VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
FIELDS = ("platformVersion", "testerDigest", "portalDigest", "runnerDigest")
# The estate values first-attach.sh sets with --set-string beside the values files (the self-test reads them out of the
# script, so a new one there cannot be forgotten here): kept from the release on a full upgrade.
ESTATE_KEYS = ("apiUrl", "testerRepository", "portalRepository", "runnerRepository", "pluginsRegistry", "registry")
SOURCE_REPO = "Systemorph/MeshWeaver.Deploy"
SOURCE_REF = "main"
# The checks that GATE the chart and this follower: chart-gate renders the Mac values and runs this self-test, the
# chart guards check the chart against the platform. A commit is followed only when BOTH ran and succeeded and no check on
# it failed; a check still running elsewhere (the macOS installer, the runner image) does not hold it.
REQUIRED_CHECKS = ("Chart invariants", "Chart guards (vs platform)")
RED = ("failure", "timed_out", "startup_failure", "action_required", "cancelled")
VALUES_FILE = "build-host-mac.values.yaml"
SHA = re.compile(r"^[0-9a-f]{40}$")
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


def download_to(url: str, dest: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
            shutil.copyfileobj(r, fh)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError):
        return 0


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
    if doc.get("channel") != follow:
        return None, f"control answered channel {doc.get('channel')!r}'s set, not {follow!r}'s"
    s = {k: doc.get(k) for k in FIELDS}
    if not (isinstance(s["platformVersion"], str) and VERSION.match(s["platformVersion"])):
        return None, f"channel {follow}'s version {s['platformVersion']!r} is not a platform version"
    for k in FIELDS[1:]:
        if not (isinstance(s[k], str) and DIGEST.match(s[k])):
            return None, f"channel {follow}'s {k} {s[k]!r} is not sha256:<64 hex>"
    return s, "ok"


def deployed_set(release: str, ns: str, ctx: str, channel: str, sh: Runner, values_out: Optional[dict] = None
                 ) -> "tuple[Optional[dict], str]":
    """The set the release runs now ({} when it names none); None when the release cannot be read or is mid-operation.
    `values_out` receives the release's user-supplied values (for the estate keys of a full upgrade)."""
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
    if values_out is not None:
        values_out.update(values)
    ch = ((values.get("buildHosts") or {}).get(channel) or {})
    return {k: ch.get(k) for k in FIELDS}, "ok"


def source_head(get: Fetcher, repo: str = SOURCE_REPO, ref: str = SOURCE_REF, cache: Optional[str] = None
                ) -> "tuple[Optional[dict], str]":
    """The newest commit of `ref` with the git tree ids of `deploy/helm` and `deploy/backlease`, when it is GREEN; (None,
    why) otherwise. Read from GitHub's public API, unauthenticated. `cache` (a JSON file) keeps the head's trees and its
    green verdict, so an unchanged head costs ONE call per pass."""
    api = f"https://api.github.com/repos/{repo}"
    status, body = get(f"{api}/commits/{ref}")
    try:
        doc = json.loads(body) if status == 200 else None
        sha, tree = doc["sha"], doc["commit"]["tree"]["sha"]
    except (ValueError, TypeError, KeyError):
        return None, f"GitHub answered {status or 'nothing'} for {repo}@{ref}"
    if not (SHA.match(sha) and SHA.match(tree)):
        return None, f"{repo}@{ref} names no commit"
    seen = read_json(cache) if cache else {}
    if seen.get("sha") == sha and seen.get("green") is True and seen.get("helm") and seen.get("backlease"):
        return {"sha": sha, "helm": seen["helm"], "backlease": seen["backlease"]}, "ok"
    ids: dict = {}
    if seen.get("sha") == sha and seen.get("helm") and seen.get("backlease"):
        ids = {"helm": seen["helm"], "backlease": seen["backlease"]}
    else:
        for path, want in (("", ("deploy",)), ("deploy", ("helm", "backlease"))):
            status, body = get(f"{api}/git/trees/{tree if not path else ids['deploy']}")
            try:
                entries = {e["path"]: e["sha"] for e in json.loads(body)["tree"] if e.get("type") == "tree"} if status == 200 else {}
            except (ValueError, TypeError, KeyError):
                entries = {}
            for w in want:
                if w not in entries:
                    return None, f"{repo}@{sha[:12]} has no {('deploy/' if path else '') + w}"
                ids[w] = entries[w]
    status, body = get(f"{api}/commits/{sha}/check-runs?per_page=100")
    try:
        runs = json.loads(body)["check_runs"] if status == 200 else None
    except (ValueError, TypeError, KeyError):
        runs = None
    if runs is None:
        return None, f"the checks of {repo}@{sha[:12]} cannot be read ({status})"
    red = [r.get("name") for r in runs if r.get("status") == "completed" and r.get("conclusion") in RED]
    by_name: dict = {}
    for r in runs:
        by_name.setdefault(r.get("name"), []).append(r)
    missing = [n for n in REQUIRED_CHECKS
               if not any(r.get("status") == "completed" and r.get("conclusion") == "success" for r in by_name.get(n, []))]
    # Only the GATE decides: the checks that render the chart and run this follower's self-test. A red elsewhere (a
    # publish job, an image lane) says nothing about the chart and must not hold the hosts on an older one.
    gate_red = [n for n in red if n in REQUIRED_CHECKS]
    other_red = [n for n in red if n not in REQUIRED_CHECKS]
    green = not gate_red and not missing
    if cache:
        try:
            write_json(cache, {"sha": sha, "helm": ids["helm"], "backlease": ids["backlease"], "green": green})
        except OSError:
            pass
    if not green:
        return None, (f"{repo}@{sha[:12]} is not green yet — "
                      + (f"{len(gate_red)} gate check(s) red ({', '.join(map(str, gate_red[:3]))})" if gate_red else
                         f"the gate has not passed ({', '.join(missing)})"))
    src = {"sha": sha, "helm": ids["helm"], "backlease": ids["backlease"]}
    if other_red:
        src["outsideRed"] = sorted(set(map(str, other_red)))   # named on the pass line, never a hold
    return src, "ok"


def source_key(src: Optional[dict]) -> Optional[str]:
    return f"{src['helm']}/{src['backlease']}" if src else None


def read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: str, doc: dict) -> None:
    tmp = path + ".part"
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def stage_source(repo: str, src: dict, agent: str, download: "Callable[[str, str], int]") -> Optional[str]:
    """Stage the chart, the Mac values and this script of commit `src['sha']` beside the agent (`<agent>/helm`,
    `<agent>/build-host-mac.values.yaml`, `<agent>/follow-set.py`), each replaced atomically; why it failed, or None.
    Only regular files under those three paths are taken from the archive, none through a link or `..`."""
    import tarfile
    work = tempfile.mkdtemp(prefix=".source-", dir=agent)
    try:
        tgz = os.path.join(work, "src.tar.gz")
        status = download(f"https://codeload.github.com/{repo}/tar.gz/{src['sha']}", tgz)
        if status != 200:
            return f"the archive of {repo}@{src['sha'][:12]} could not be downloaded ({status})"
        out = os.path.join(work, "x")
        wanted = {"helm": "deploy/helm/", "values": f"deploy/backlease/{VALUES_FILE}", "script": "deploy/backlease/follow-set.py"}
        got = set()
        with tarfile.open(tgz, "r:gz") as tar:
            for m in tar.getmembers():
                parts = m.name.split("/", 1)
                rel = parts[1] if len(parts) == 2 else ""
                if ".." in rel.split("/") or not (rel.startswith(wanted["helm"]) or rel in (wanted["values"], wanted["script"])):
                    continue
                if m.isdir():
                    continue
                if not m.isfile():
                    return f"the archive carries a non-file at {rel} — refused"
                dest = os.path.join(out, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with tar.extractfile(m) as src_fh, open(dest, "wb") as fh:
                    shutil.copyfileobj(src_fh, fh)
                got.add("helm" if rel.startswith(wanted["helm"]) else "values" if rel == wanted["values"] else "script")
        if not os.path.isfile(os.path.join(out, "deploy", "helm", "Chart.yaml")) or got != {"helm", "values", "script"}:
            return f"the archive of {repo}@{src['sha'][:12]} lacks the chart, the Mac values or the follower"
        chart = os.path.join(agent, "helm")
        shutil.rmtree(chart + ".old", ignore_errors=True)
        if os.path.isdir(chart):
            os.replace(chart, chart + ".old")
        os.replace(os.path.join(out, "deploy", "helm"), chart)
        shutil.rmtree(chart + ".old", ignore_errors=True)
        os.replace(os.path.join(out, wanted["values"]), os.path.join(agent, VALUES_FILE))
        script = os.path.join(agent, "follow-set.py")
        os.replace(os.path.join(out, wanted["script"]), script)
        os.chmod(script, 0o755)
        return None
    except (OSError, tarfile.TarError, ValueError) as e:
        return f"staging {repo}@{src['sha'][:12]} failed ({type(e).__name__}: {str(e)[:160]})"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def estate_values(values: dict) -> dict:
    """The estate values (ESTATE_KEYS) of the release's user-supplied values, as a values document."""
    bh = values.get("buildHosts") or {}
    return {"buildHosts": {k: bh[k] for k in ESTATE_KEYS if k in bh}}


SIZING_FILE = "sizing.yaml"


def sizing_file(agent: Optional[str], chart: str) -> Optional[str]:
    """THIS MAC'S SIZING (`memex-local buildhost apply` writes `<agent>/sizing.yaml`: runners, resources, stage slots, the
    cache budget from its build disk), when present and the chart can read it (a chart that predates
    `buildHosts.cache.sizeEnforced` would refuse a budget above the claim) — else None. Without it, every full upgrade
    put the Mac back to the values file's defaults (2026-11-11)."""
    if not agent:
        return None
    f = os.path.join(agent, SIZING_FILE)
    tpl = os.path.join(chart, "templates", "build-host", "_build-hosts.tpl")
    try:
        return f if os.path.isfile(f) and "sizeEnforced" in open(tpl).read() else None
    except OSError:
        return None


def full_upgrade_cmd(release: str, ns: str, ctx: str, chart: str, values_file: str, estate_file: str, channel: str,
                     s: dict, sizing: Optional[str] = None) -> list:
    """A FULL upgrade: the chart's defaults, the Mac values, the estate values, this Mac's sizing, then the set — never
    --reuse-values."""
    cmd = ["helm", "upgrade", release, chart, "--kube-context", ctx, "-n", ns,
           "-f", os.path.join(chart, "values.yaml"), "-f", values_file, "-f", estate_file]
    if sizing:
        cmd += ["-f", sizing]
    for k in FIELDS:
        cmd += ["--set-string", f"buildHosts.{channel}.{k}={s[k]}"]
    return cmd + ["--wait=false"]


def upgrade_cmd(release: str, ns: str, ctx: str, chart: str, channel: str, s: dict) -> list:
    cmd = ["helm", "upgrade", release, chart, "--kube-context", ctx, "-n", ns, "--reuse-values"]
    for k in FIELDS:
        cmd += ["--set-string", f"buildHosts.{channel}.{k}={s[k]}"]
    return cmd + ["--wait=false"]


def follow_once(api: str, follow: str, release: str, ns: str, ctx: str, chart: str, channel: str,
                get: Fetcher, sh: Runner, dry_run: bool = False, agent: Optional[str] = None,
                download: "Optional[Callable[[str, str], int]]" = None, source_repo: str = SOURCE_REPO,
                source_ref: str = SOURCE_REF) -> "tuple[str, int]":
    """One pass: (the line it prints, exit code). Exit 0 for current/applied/nothing-to-follow, 1 for a failed apply.
    With `agent` (the agent's directory) and `download`, the chart and the Mac values follow `source_ref` too."""
    want, why = desired_set(api, follow, get)
    if want is None:
        return f"follow-set: nothing applied — {why}", 0
    values: dict = {}
    have, why = deployed_set(release, ns, ctx, channel, sh, values)
    if have is None:
        return f"follow-set: nothing applied — {why}", 0
    notes = []
    source_changed = False
    if agent is not None and download is not None:
        src, swhy = source_head(get, source_repo, source_ref, cache=os.path.join(agent, "head.json"))
        staged = read_json(os.path.join(agent, "source.json"))
        if src is not None and src.get("outsideRed"):
            out = src["outsideRed"]
            notes.append(f"{len(out)} check(s) outside the chart gate red ({', '.join(out[:3])}) — not a hold")
        if src is None:
            notes.append(f"chart/values held — {swhy}")
        elif source_key(src) != staged.get("key"):
            if dry_run:
                notes.append(f"would stage the chart and Mac values of {source_repo}@{src['sha'][:12]}")
            else:
                err = stage_source(source_repo, src, agent, download)
                if err is not None:
                    return f"follow-set: nothing applied — {err}", 1
                write_json(os.path.join(agent, "source.json"), {"key": source_key(src), "sha": src["sha"]})
                staged = {"key": source_key(src), "sha": src["sha"]}
                notes.append(f"staged the chart and Mac values of {source_repo}@{src['sha'][:12]}")
        applied = read_json(os.path.join(agent, "applied.json"))
        source_changed = bool(staged.get("key")) and staged.get("key") != applied.get("key")
    note = f" ({'; '.join(notes)})" if notes else ""
    if have == want and not source_changed:
        return f"follow-set: current — {release} runs {follow}'s set {want['platformVersion']}{note}", 0
    if not os.path.isfile(os.path.join(chart, "Chart.yaml")):
        return f"follow-set: nothing applied — no chart at {chart}", 0
    line = (f"{have.get('platformVersion') or 'none'} → {want['platformVersion']} (runner {want['runnerDigest'][7:19]})"
            + (" with the chart and Mac values of " + str(read_json(os.path.join(agent, "source.json")).get("sha", ""))[:12]
               if source_changed and agent else ""))
    if dry_run:
        return f"follow-set: would apply {line}{note}", 0
    values_file = os.path.join(agent, VALUES_FILE) if agent else ""
    if agent and os.path.isfile(values_file):
        estate = os.path.join(agent, "estate-values.json")
        write_json(estate, estate_values(values))
        rc, out = sh(full_upgrade_cmd(release, ns, ctx, chart, values_file, estate, channel, want, sizing_file(agent, chart)))
    else:
        rc, out = sh(upgrade_cmd(release, ns, ctx, chart, channel, want))
    if rc != 0:
        tail = out.strip().splitlines()[-1][:300] if out.strip() else ""
        return f"follow-set: FAILED to apply {line} (helm exit {rc}): {tail}{note}", 1
    if agent:
        write_json(os.path.join(agent, "applied.json"), read_json(os.path.join(agent, "source.json")))
    return f"follow-set: applied {line} — the runners drain their run in hand, then the new set claims{note}", 0


def agent_dir(profile: str) -> str:
    return os.path.join(HOME, "buildhost-follow", profile)


def plist(script: str, chart: str, a) -> str:
    args = [sys.executable or "/usr/bin/python3", script, "--once", "--profile", a.profile, "--channel", a.channel,
            "--follow", a.follow, "--api", a.api, "--chart", chart]
    items = "".join(f"<string>{xml_escape(x)}</string>" for x in args)
    log = xml_escape(os.path.join(agent_dir(a.profile), "follow.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{xml_escape(LABEL + "." + a.profile)}</string>
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
    # the Mac values beside the chart, so a pass can apply a FULL upgrade (chart defaults + Mac values + estate + set)
    values_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), VALUES_FILE)
    if os.path.isfile(values_src):
        shutil.copyfile(values_src, os.path.join(d, VALUES_FILE))
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

        h = Helm(old)
        line, rc = go(control({**good, "channel": "qa"}), h)
        check("an answer naming ANOTHER channel's set is never applied", h.upgrades() == [] and "not 'latest'" in line, line)

        seen = []
        follow_once("https://ops.example/api/builds/", "latest", "r", "n", "c", chart, "mac",
                    lambda url: (seen.append(url), (200, json.dumps(good)))[1], Helm(old))
        check("the set is read from control's complete-set route", seen == ["https://ops.example/api/builds/channels/latest/set"], seen)

        a = argparse.Namespace(profile="build", follow="latest", api="https://ops.example/api/builds", interval=300, channel="mac")
        p = plist("/x/follow-set.py", "/x/helm", a)
        check("the agent runs one pass every interval with the attach's api and chart",
              f"<string>{LABEL}.build</string>" in p and "<integer>300</integer>" in p and "<string>--once</string>" in p
              and "<string>https://ops.example/api/builds</string>" in p and "<string>/x/helm</string>" in p, p)
        a2 = argparse.Namespace(profile="build", follow="latest", api="https://ops.example/api/builds?a=1&b=<2>", interval=300, channel="lab")
        p2 = plist("/x/follow-set.py", "/x/helm", a2)
        check("the agent targets the channel it was installed for (--channel reaches the agent's arguments)",
              "<string>--channel</string><string>lab</string>" in p2, p2)
        check("plist values are XML-escaped (an & or < in --api never yields a plist launchd rejects)",
              "?a=1&amp;b=&lt;2&gt;" in p2 and "&b=<2>" not in p2, p2)
        import xml.dom.minidom
        try:
            xml.dom.minidom.parseString(p2.split("\n", 1)[1])
            parsed = True
        except Exception as e:  # noqa: BLE001 — any parse failure is the finding
            parsed = repr(e)
        check("the escaped plist is well-formed XML", parsed is True, parsed)

    # ── the chart and the Mac values follow the source repository's green main ──
    import io
    import tarfile
    SHA_C, TREE, DEP, HELM, BL = "1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40

    def github(checks: list, helm: str = HELM, status: int = 200, sha: str = SHA_C) -> Fetcher:
        def get(url: str) -> "tuple[int, str]":
            if url.endswith("/channels/latest/set"):
                return 200, json.dumps(good)
            if url.endswith(f"/commits/{SOURCE_REF}"):
                return status, json.dumps({"sha": sha, "commit": {"tree": {"sha": TREE}}})
            if url.endswith(f"/git/trees/{TREE}"):
                return 200, json.dumps({"tree": [{"path": "deploy", "type": "tree", "sha": DEP}]})
            if url.endswith(f"/git/trees/{DEP}"):
                return 200, json.dumps({"tree": [{"path": "helm", "type": "tree", "sha": helm}, {"path": "backlease", "type": "tree", "sha": BL}]})
            if "/check-runs" in url:
                return 200, json.dumps({"check_runs": checks})
            return 404, ""
        return get

    green = [{"name": n, "status": "completed", "conclusion": "success"} for n in REQUIRED_CHECKS] + [
        {"name": "b", "status": "completed", "conclusion": "skipped"}, {"name": "macOS pkg", "status": "in_progress"}]
    src, why = source_head(github(green))
    check("source: a green main names its commit and the two tree ids", src == {"sha": SHA_C, "helm": HELM, "backlease": BL}, (src, why))
    check("source: a check still running OUTSIDE the gate does not hold a commit whose gate passed", source_head(github(green))[0] is not None)
    for name, checks in (("a red GATE check", [{"name": REQUIRED_CHECKS[0], "status": "completed", "conclusion": "failure"},
                                                {"name": REQUIRED_CHECKS[1], "status": "completed", "conclusion": "success"}]),
                         ("a cancelled GATE check", [{"name": REQUIRED_CHECKS[0], "status": "completed", "conclusion": "success"},
                                                     {"name": REQUIRED_CHECKS[1], "status": "completed", "conclusion": "cancelled"}]),
                         ("the gate still running", [{"name": REQUIRED_CHECKS[0], "status": "in_progress"},
                                                     {"name": REQUIRED_CHECKS[1], "status": "completed", "conclusion": "success"}]),
                         ("the gate skipped (never ran)", [{"name": n, "status": "completed", "conclusion": "skipped"} for n in REQUIRED_CHECKS]),
                         ("only trivial checks", [{"name": "b", "status": "completed", "conclusion": "success"}]), ("no check at all", [])):
        src, why = source_head(github(checks))
        check(f"source: {name} holds the chart (nothing staged)", src is None and "not green" in why, why)
    src, why = source_head(github(green + [{"name": "Package seed: publish", "status": "completed", "conclusion": "failure"},
                                           {"name": "x", "status": "completed", "conclusion": "cancelled"}]))
    check("source: a red check OUTSIDE the gate (a publish job) does not hold the chart, and is named as data",
          src is not None and src.get("outsideRed") == ["Package seed: publish", "x"] and why == "ok", (src, why))
    src, why = source_head(github(green, status=403))
    check("source: GitHub refusing (a rate limit) holds it, said", src is None and "403" in why, why)
    with tempfile.TemporaryDirectory() as hc:
        calls: list = []
        gh_ = github(green)
        counting = lambda url: (calls.append(url), gh_(url))[1]
        cache_f = os.path.join(hc, "head.json")
        first = source_head(counting, cache=cache_f)[0]
        n_first = len(calls)
        again = source_head(counting, cache=cache_f)[0]
        check("source: an UNCHANGED green head costs ONE call on the next pass (trees and verdict kept)",
              first == again and n_first == 4 and len(calls) - n_first == 1, (n_first, calls[n_first:]))

    def tarball(files: dict) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in files.items():
                ti = tarfile.TarInfo(f"MeshWeaver.Deploy-{SHA_C}/{name}")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
        return buf.getvalue()
    full = {"deploy/helm/Chart.yaml": b"name: x", "deploy/helm/values.yaml": b"buildHosts: {}",
            "deploy/helm/templates/a.yaml": b"kind: X", f"deploy/backlease/{VALUES_FILE}": b"buildHosts: {rolloutStrategy: Recreate}",
            "deploy/backlease/follow-set.py": b"# new", "deploy/other/secret.txt": b"not taken"}

    def dl(data: bytes, status: int = 200):
        def do(url: str, dest: str) -> int:
            with open(dest, "wb") as fh:
                fh.write(data)
            return status
        return do

    with tempfile.TemporaryDirectory() as ag:
        os.makedirs(os.path.join(ag, "helm"))
        open(os.path.join(ag, "helm", "Chart.yaml"), "w").write("old")
        err = stage_source(SOURCE_REPO, {"sha": SHA_C, "helm": HELM, "backlease": BL}, ag, dl(tarball(full)))
        check("stage: the chart, the Mac values and the follower are staged, nothing else",
              err is None and open(os.path.join(ag, "helm", "Chart.yaml")).read() == "name: x"
              and os.path.isfile(os.path.join(ag, "helm", "templates", "a.yaml"))
              and "Recreate" in open(os.path.join(ag, VALUES_FILE)).read()
              and open(os.path.join(ag, "follow-set.py")).read() == "# new" and not os.path.exists(os.path.join(ag, "deploy")), err)
        bad = dict(full)
        bad.pop("deploy/backlease/follow-set.py")
        err2 = stage_source(SOURCE_REPO, {"sha": SHA_C, "helm": HELM, "backlease": BL}, ag, dl(tarball(bad)))
        check("stage: an archive missing a part stages nothing", err2 is not None and "lacks" in err2, err2)

    with tempfile.TemporaryDirectory() as ag:
        chart_a = os.path.join(ag, "helm")
        os.makedirs(chart_a)
        open(os.path.join(chart_a, "Chart.yaml"), "w").write("old")
        cur = {k: good[k] for k in FIELDS}

        class HelmV(Helm):
            def __call__(self, cmd: list) -> "tuple[int, str]":
                if cmd[:3] == ["helm", "get", "values"]:
                    self.calls.append(cmd)
                    return 0, json.dumps({"buildHosts": {"mac": self.values, "apiUrl": "https://ops.example/api/builds",
                                                         "runnerRepository": "cr.example/mw-build-host", "rolloutStrategy": "Recreate",
                                                         "registry": {"url": "https://reg.example"}}})
                return super().__call__(cmd)
        hv = HelmV(cur)
        line, rc = follow_once("https://ops.example/api/builds", "latest", "build-host-mac", "build-mac", "colima-build",
                               chart_a, "mac", github(green), hv, agent=ag, download=dl(tarball(full)))
        up = hv.upgrades()
        check("values: a host ON its set whose chart/values changed on a green main is upgraded — and a FRESH install (no applied.json) counts as changed, so it applies the current chart once",
              rc == 0 and len(up) == 1 and "with the chart and Mac values of 111111111111" in line, (line, hv.calls))
        check("values: the upgrade is FULL — chart defaults, the Mac values, the estate values, then the set — never --reuse-values",
              up and "--reuse-values" not in up[0] and up[0][up[0].index("-f") + 1] == os.path.join(chart_a, "values.yaml")
              and os.path.join(ag, VALUES_FILE) in up[0] and os.path.join(ag, "estate-values.json") in up[0]
              and f"buildHosts.mac.runnerDigest={R}" in up[0], up)
        est = read_json(os.path.join(ag, "estate-values.json"))
        check("values: the estate values are kept from the release, and nothing else of it (a stale rolloutStrategy is not)",
              est == {"buildHosts": {"apiUrl": "https://ops.example/api/builds", "runnerRepository": "cr.example/mw-build-host",
                                     "registry": {"url": "https://reg.example"}}}, est)
        hv2 = HelmV(cur)
        line2, rc2 = follow_once("https://ops.example/api/builds", "latest", "build-host-mac", "build-mac", "colima-build",
                                 chart_a, "mac", github(green), hv2, agent=ag, download=dl(tarball(full)))
        check("values: the same green commit again changes nothing (applied is recorded)", hv2.upgrades() == [] and "current" in line2, line2)
        hv3 = HelmV(cur)
        line3, _ = follow_once("https://ops.example/api/builds", "latest", "build-host-mac", "build-mac", "colima-build",
                               chart_a, "mac", github(green, helm="9" * 40, sha="6" * 40), hv3, agent=ag, download=dl(tarball(full)), dry_run=True)
        check("values: --dry-run stages and applies nothing", hv3.upgrades() == [] and "would" in line3, line3)
        hv4 = HelmV(cur)
        line4, _ = follow_once("https://ops.example/api/builds", "latest", "build-host-mac", "build-mac", "colima-build",
                               chart_a, "mac", github([{"name": REQUIRED_CHECKS[0], "status": "completed", "conclusion": "failure"},
                                                        {"name": REQUIRED_CHECKS[1], "status": "completed", "conclusion": "success"}], helm="9" * 40, sha="7" * 40),
                               hv4, agent=ag, download=dl(tarball(full)))
        hv5 = HelmV(cur)
        line5, _ = follow_once("https://ops.example/api/builds", "latest", "build-host-mac", "build-mac", "colima-build",
                               chart_a, "mac", github(green + [{"name": "Package seed: publish", "status": "completed", "conclusion": "failure"}],
                                                      helm="8" * 40, sha="5" * 40), hv5, agent=ag, download=dl(tarball(full)))
        check("values: a red check OUTSIDE the gate does not hold — the changed chart is staged AND applied, the red named on the line",
              len(hv5.upgrades()) == 1 and "applied" in line5 and "Package seed: publish" in line5 and "not a hold" in line5, line5)
        check("values: a main whose chart GATE is red is never followed (the host stays as it is, said)", hv4.upgrades() == [] and "held" in line4, line4)
    with tempfile.TemporaryDirectory() as sz:
        ch = os.path.join(sz, "helm"); os.makedirs(os.path.join(ch, "templates", "build-host"))
        tpl = os.path.join(ch, "templates", "build-host", "_build-hosts.tpl")
        open(tpl, "w").write("old chart")
        open(os.path.join(sz, SIZING_FILE), "w").write("buildHosts: {cache: {maxGi: 80}}")
        check("sizing: a chart that cannot read sizeEnforced gets NO sizing file (it would refuse a budget above the claim)",
              sizing_file(sz, ch) is None)
        open(tpl, "w").write("... cache.sizeEnforced ...")
        cmd = full_upgrade_cmd("r", "n", "c", ch, "v.yaml", "e.json", "mac", good, sizing_file(sz, ch))
        check("sizing: this Mac's sizing.yaml is layered AFTER the Mac and estate values, BEFORE the set",
              cmd[cmd.index("e.json") + 1:cmd.index("e.json") + 3] == ["-f", os.path.join(sz, SIZING_FILE)]
              and cmd.index(os.path.join(sz, SIZING_FILE)) < cmd.index("--set-string"), cmd)
        os.remove(os.path.join(sz, SIZING_FILE))
        check("sizing: no sizing.yaml (a Mac never sized) → the Mac values alone", sizing_file(sz, ch) is None)
    # first-attach's estate --set-string keys are exactly what a full upgrade keeps
    attach = os.path.join(os.path.dirname(os.path.abspath(__file__)), "first-attach.sh")
    if os.path.isfile(attach):
        keys = set(re.findall(r"--set-string buildHosts\.([A-Za-z]+)[.=]", open(attach).read())) - {"mac"}
        check("estate: every buildHosts key first-attach.sh sets is kept on a full upgrade (ESTATE_KEYS)",
              keys and keys <= set(ESTATE_KEYS), (sorted(keys), ESTATE_KEYS))

    if fails:
        for f in fails:
            print(f"SELF-TEST FAILED: {f}")
        return 1
    print("follow-set self-test: all cases passed (behind → upgraded with exactly the four set values and --reuse-values, "
          "pinned context; current → nothing; unreachable/503/incomplete/unnamed/malformed → nothing (fail closed); "
          "another channel's answer refused; pending release never overlapped; failed upgrade red; dry-run; no chart; the complete-set route; "
          "the agent plist names its channel and escapes every value; the chart and Mac values follow a GREEN main only, staged "
          "whole and applied by a FULL upgrade keeping exactly the estate values)")
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
    ap.add_argument("--source-repo", default=SOURCE_REPO, help="where the chart and the Mac values are followed from (public)")
    ap.add_argument("--source-ref", default=SOURCE_REF)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.install:
        return install(a, run)
    if a.uninstall:
        return uninstall(a, run)
    agent = agent_dir(a.profile)
    chart = a.chart or os.path.join(agent, "helm")
    os.makedirs(agent_dir(a.profile), exist_ok=True)
    # one pass at a time: launchd never overlaps a pass, but a hand-run one could
    with open(os.path.join(agent_dir(a.profile), ".lock"), "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("follow-set: nothing applied — another pass is running")
            return 0
        line, rc = follow_once(a.api, a.follow, f"build-host-{a.channel}", f"build-{a.channel}", f"colima-{a.profile}",
                               chart, a.channel, fetch, run, dry_run=a.dry_run,
                               agent=agent if os.path.realpath(chart) == os.path.realpath(os.path.join(agent, "helm")) else None,
                               download=download_to,
                               source_repo=a.source_repo, source_ref=a.source_ref)
    print(line, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
