#!/usr/bin/env python3
"""bench-host.py — the Mac half of control's two bench operations (MeshWeaver.Plugins Hosting/BackLease §5.2a).

Control hands a fleet Mac a governed host command over the signed /api/fleet/self-status answer; `memex-local` runs it
through THIS program and reports the outcome signed with the Mac's own heartbeat key. Two FIXED operations, never a
command line:

  harness-sync <commit>   install devtools/issue-replay of MeshWeaver.Plugins at that 40-hex commit of main into
                          ~/bench/harness@<sha12>/, verify it, and atomically repoint ~/bench/harness-current — only while
                          no runner works from the current harness (else exit 75 = DEFERRED, the next tick asks again).
                          The source is control's harness-token route (a short-lived contents:read App token for this one
                          repository, minted only for a Mac that holds the command) and GitHub's REST API: every file is
                          checked against the commit's tree (its git blob id). Nothing ever touches ~/bench/harness or
                          ~/bench/harness-v6, and the token is never written anywhere.
  bench <verb> <job.json> start | stop | status a DECLARED job (the declaration control validated, re-validated here):
                          a benchmark / gen job runs under a supervisor (bench-share.sh semantics: each model in turn,
                          a dead runner restarted, ~/bench/jobs/<name>.stop drains it) from the harness-current it
                          RESOLVED ONCE at start; a solution-paths job runs solution_paths.py --root <root> --all, then
                          --flush, to the end.

Exit codes: 0 Done, 1 Failed, 75 Deferred. The last line on stdout is the one-line detail. Python 3.9, standard library.
"""
import base64, hashlib, hmac, json, os, re, shutil, signal, subprocess, sys, time, urllib.error, urllib.request

DEFERRED = 75
BENCH = os.path.abspath(os.path.expanduser(os.environ.get("MEMEX_BENCH_HOME", "~/bench")))
JOBS = os.path.join(BENCH, "jobs")
CURRENT = os.path.join(BENCH, "harness-current")
GITHUB = os.environ.get("MEMEX_GITHUB_API", "https://api.github.com").rstrip("/")
PY = os.environ.get("BENCH_PYTHON", "/usr/bin/python3")
POLL = int(os.environ.get("BENCH_JOB_POLL", "300"))
REPOS = ("Systemorph/MeshWeaver.Plugins",)
HARNESS_PATH = "devtools/issue-replay"
DEFAULT_ARGS = ["--backend", "ollama", "--max-steps", "120", "--wall", "7200", "--num-ctx", "262144", "--context-chars", "800000"]

COMMIT = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
DEPLOYMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
ROOT = re.compile(r"~/bench/[a-z0-9][a-z0-9._-]{0,63}")
TASKS = re.compile(r"~/bench/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json")
ONLY = re.compile(r"T[0-9]{3,5}(,T[0-9]{3,5}){0,499}")
KINDS = ("benchmark", "gen", "solution-paths")


def _int(lo, hi):
    return lambda v: bool(re.fullmatch(r"0|[1-9][0-9]{0,7}", v)) and lo <= int(v) <= hi


# the SAME allow-list as control's MacBenchJobs.AllowedArgs — a flag outside it is refused, never passed through
ALLOWED = {
    "backend": lambda v: v in ("ollama", "openai", "jobs"),
    "max-steps": _int(1, 1000), "wall": _int(60, 28800), "num-ctx": _int(1024, 1048576),
    "context-chars": _int(1000, 4000000), "max-tokens": _int(256, 65536), "finish-checks": _int(0, 5),
    "tool-guide": lambda v: v in ("0", "1"),
    "think": lambda v: v in ("true", "false", "low", "medium", "high"),
    "base": lambda v: bool(re.fullmatch(r"http://(localhost|127\.0\.0\.1):[0-9]{2,5}", v)),
}


def done(detail):
    print(one_line(detail)); return 0


def failed(detail):
    print(one_line(detail)); return 1


def one_line(s):
    s = re.sub(r"\s+", " ", "".join(" " if ord(c) < 32 else c for c in str(s))).strip()
    return s[:600]


def bench_path(tilde):
    """~/bench/<rest> → the absolute path under BENCH (the shapes above already refused anything else)."""
    return os.path.join(BENCH, tilde[len("~/bench/"):])


# ── the declaration ─────────────────────────────────────────────────────────────────────────────────────────────────────

def validate(job):
    """The job as control handed it, re-validated field by field; (job, None) or (None, why)."""
    if not isinstance(job, dict):
        return None, "the bench job is no object"
    name, kind, dep, root = job.get("name"), job.get("kind"), job.get("deployment"), job.get("root")
    if not (isinstance(name, str) and NAME.fullmatch(name)):
        return None, "the job name %r has another shape" % (name,)
    if kind not in KINDS:
        return None, "kind %r is not one of %s" % (kind, ", ".join(KINDS))
    if not (isinstance(dep, str) and DEPLOYMENT.fullmatch(dep)):
        return None, "the job names no Mac"
    if not (isinstance(root, str) and ROOT.fullmatch(root) and ".." not in root):
        return None, "root %r is not ~/bench/<segment>" % (root,)
    models = job.get("models") or []
    if not isinstance(models, list) or any(not (isinstance(m, str) and MODEL.fullmatch(m) and ".." not in m) for m in models):
        return None, "a model name has another shape"
    tasks, only = job.get("tasks"), job.get("only") or "all"
    if tasks is not None and not (isinstance(tasks, str) and TASKS.fullmatch(tasks)):
        return None, "tasks %r is not ~/bench/<file>.json" % (tasks,)
    if not (isinstance(only, str) and (only == "all" or ONLY.fullmatch(only))):
        return None, "only %r is neither all nor a list of task ids" % (only,)
    args = job.get("args") or {}
    if not isinstance(args, dict):
        return None, "args is no object"
    for k, v in args.items():
        if k not in ALLOWED:
            return None, "argument %r is not on the allow-list" % (k,)
        if not (isinstance(v, str) and ALLOWED[k](v)):
            return None, "argument %r = %r is outside its shape" % (k, v)
    am, ar = job.get("afterModel"), job.get("afterRoot")
    if (am is None) != (ar is None):
        return None, "afterModel and afterRoot go together"
    if am is not None and not (isinstance(am, str) and MODEL.fullmatch(am) and isinstance(ar, str) and ROOT.fullmatch(ar) and ".." not in ar + am):
        return None, "after %r under %r has another shape" % (am, ar)
    if kind == "solution-paths":
        if models or args or tasks is not None or only != "all" or am is not None:
            return None, "a solution-paths job takes only its root"
    elif not 1 <= len(models) <= 8:
        return None, "a %s names 1-8 models" % kind
    return {"name": name, "kind": kind, "deployment": dep, "root": root, "models": models, "tasks": tasks, "only": only,
            "args": {k: args[k] for k in sorted(args)}, "push": kind == "benchmark" and job.get("push") is not False,
            "afterModel": am, "afterRoot": ar}, None


def safe(model):
    return model.replace("/", "_").replace(":", "_")


def model_log(root, model):
    return bench_path(root) + "-" + safe(model) + ".log"


def runner_argv(job, model):
    """runner.py's argv for one model — every value from the validated declaration, nothing through a shell."""
    argv = [PY, "runner.py", "--models", model, "--root", bench_path(job["root"])]
    if job["tasks"]:
        argv += ["--tasks", bench_path(job["tasks"])]
    if job["only"] != "all":
        argv += ["--only", job["only"]]
    if not job["push"]:
        argv += ["--no-push"]
    agent = [x for k, v in job["args"].items() for x in ("--" + k, v)] or list(DEFAULT_ARGS)
    caffeinate = os.environ.get("BENCH_CAFFEINATE", "/usr/bin/caffeinate")
    return ([caffeinate, "-is"] if caffeinate and os.path.exists(caffeinate) else []) + argv + ["--"] + agent


def job_files(name):
    return {k: os.path.join(JOBS, "%s.%s" % (name, k)) for k in ("json", "pid", "log", "stop")}


def alive(pid):
    try:
        os.kill(pid, 0); return True
    except (OSError, ValueError):
        return False


def read_pid(name):
    try:
        rec = json.load(open(job_files(name)["pid"]))
        return rec if isinstance(rec, dict) and isinstance(rec.get("pid"), int) and alive(rec["pid"]) else None
    except (OSError, ValueError):
        return None


def last_line(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 4096))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
            return lines[-1] if lines else "(empty)"
    except OSError:
        return "(no log yet)"


def say(log, text):
    with open(log, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + text + "\n")


# ── bench verbs ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def bench(verb, raw, mac):
    try:
        job, why = validate(json.loads(raw))
    except ValueError:
        job, why = None, "the bench job is no JSON"
    if job is None:
        return failed("bench job refused, nothing was run: " + why)
    # the declaration is bound to ONE machine: a job naming another Mac runs nothing here, whoever addressed it
    if job["deployment"].lower() != (mac or "").strip().lower():
        return failed("bench job %s is declared for %r, not this Mac (%r) — nothing was run" % (job["name"], job["deployment"], mac))
    files = job_files(job["name"])
    running = read_pid(job["name"])
    if verb == "status":
        if running:
            return done("bench job %s: RUNNING (pid %d since %s, from %s); last log line: %s"
                        % (job["name"], running["pid"], running.get("startedAt", "?"), running.get("harness", "?"), last_line(files["log"])))
        return done("bench job %s: not running; last log line: %s" % (job["name"], last_line(files["log"])))
    if verb == "stop":
        os.makedirs(JOBS, exist_ok=True)
        open(files["stop"], "a").close()
        return done("bench job %s: stop file written (%s) — %s" % (job["name"], files["stop"],
                    "the runner drains after its current task" if running else "nothing was running"))
    if verb != "start":
        return failed("unknown bench verb %r — this memex-local runs start, stop and status; nothing was run" % (verb,))
    harness = os.path.realpath(CURRENT)
    if not os.path.islink(CURRENT) or not os.path.isfile(os.path.join(harness, "runner.py")):
        return failed("no ~/bench/harness-current with a harness in it — a harness sync comes first; nothing was started")
    if job["kind"] == "solution-paths":
        return solution_paths(job, harness)
    if running:
        return failed("bench job %s is already running (pid %d) — stop it first; nothing was started" % (job["name"], running["pid"]))
    os.makedirs(JOBS, exist_ok=True)
    with open(files["json"] + ".tmp", "w") as f:
        json.dump(job, f, indent=1)
    os.replace(files["json"] + ".tmp", files["json"])
    for stale in (files["stop"],):
        if os.path.exists(stale):
            os.remove(stale)
    log = open(files["log"], "a")
    child = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "supervise", files["json"], harness],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    with open(files["pid"] + ".tmp", "w") as f:
        json.dump({"pid": child.pid, "harness": harness, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    os.replace(files["pid"] + ".tmp", files["pid"])
    return done("bench job %s started (pid %d): %s of %s under %s from %s" % (
        job["name"], child.pid, job["kind"], " ".join(job["models"]), job["root"], os.path.basename(harness)))


def solution_paths(job, harness):
    root = bench_path(job["root"])
    for step in ("--all", "--flush"):
        try:
            p = subprocess.run([PY, "solution_paths.py", "--root", root, step], cwd=harness, capture_output=True, text=True,
                               timeout=int(os.environ.get("BENCH_SP_TIMEOUT", "3600")))
        except subprocess.TimeoutExpired:
            return failed("solution_paths.py %s under %s did not finish in time" % (step, job["root"]))
        if p.returncode != 0:
            return failed("solution_paths.py %s under %s FAILED (rc %d): %s" % (step, job["root"], p.returncode,
                          ((p.stderr or p.stdout).strip().splitlines() or ["(no output)"])[-1]))
        tail = (p.stdout.strip().splitlines() or ["(no output)"])[-1]
    return done("solution paths of %s distilled and flushed from %s: %s" % (job["root"], os.path.basename(harness), tail))


def supervise(jobfile, harness):
    """bench-share.sh semantics, from ONE resolved harness: wait for the stage before, then each model in turn until its
    log says 'all tasks done', a dead runner restarted after 30 s, the stop file drains it."""
    job, why = validate(json.load(open(jobfile)))
    if job is None:
        print("refused: " + why); return 1
    files = job_files(job["name"])
    log = files["log"]
    stop = lambda: os.path.exists(files["stop"])
    say(log, "%s up: %d model(s) from %s" % (job["name"], len(job["models"]), harness))
    if job["afterModel"]:
        alog = model_log(job["afterRoot"], job["afterModel"])
        say(log, "waiting for %s to finish (%s)" % (job["afterModel"], alog))
        while not stop() and not logged_done(alog, job["afterModel"]):
            time.sleep(POLL)
    for m in job["models"]:
        mlog = model_log(job["root"], m)
        failures = 0
        while not stop() and not logged_done(mlog, m):
            if not ollama_up(log):
                time.sleep(POLL); continue
            if not has_model(m):
                say(log, "waiting: %s is not in this Mac's Ollama" % m); time.sleep(POLL); continue
            say(log, "runner start: %s" % m)
            with open(mlog, "a") as out:
                rc = subprocess.call(runner_argv(job, m), cwd=harness, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
            # a runner that keeps dying without finishing backs off — 30 s after the first failure, doubling, at most 30 min
            # between starts — so a persistent failure (a contract drift, a broken model) is not restarted every 30 s
            # forever; status shows it. The wait is sliced, so a stop file or a finished log is noticed within seconds.
            failures = failures + 1 if rc != 0 else 0
            wait = min(30 * (2 ** min(failures - 1, 6)), 1800) if failures else 30
            say(log, "runner exit: %s rc=%d%s" % (m, rc, " — failure %d in a row, next start in %d s" % (failures, wait) if failures else ""))
            nap(wait, lambda: stop() or logged_done(mlog, m))
        subprocess.call(["ollama", "stop", m], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        say(log, "done: %s" % m)
    say(log, "%s %s" % (job["name"], "stopped" if stop() else "finished"))
    try:
        os.remove(files["pid"])
    except OSError:
        pass
    return 0


def nap(seconds, wake):
    """Sleeps up to `seconds` in short slices, returning early once `wake()` is true (a stop file, a finished log)."""
    end = time.time() + seconds
    while time.time() < end and not wake():
        time.sleep(max(0.05, min(NAP_SLICE, end - time.time())))


NAP_SLICE = float(os.environ.get("BENCH_NAP_SLICE", "5"))


def logged_done(path, model):
    try:
        return any((model + " all tasks done") in line for line in open(path, errors="replace"))
    except OSError:
        return False


def ollama_up(log):
    if subprocess.call(["ollama", "list"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_FLASH_ATTENTION="1", OLLAMA_KV_CACHE_TYPE="q8_0")
    subprocess.Popen(["ollama", "serve"], env=env, stdin=subprocess.DEVNULL, stdout=open(os.path.join(BENCH, "ollama-serve.log"), "a"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    say(log, "ollama serve started")
    time.sleep(min(10, POLL))
    return subprocess.call(["ollama", "list"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def has_model(model):
    tagged = model if ":" in model else model + ":latest"
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return tagged in [l.split()[0] for l in out.splitlines()[1:] if l.split()]


# ── harness sync ────────────────────────────────────────────────────────────────────────────────────────────────────────

def processes():
    """(pid, cwd, command) of every process that runs a harness script. MEMEX_BENCH_FAKE_PROCS (tests) replaces the reading."""
    fake = os.environ.get("MEMEX_BENCH_FAKE_PROCS")
    if fake is not None:
        rows = []
        for line in open(fake) if os.path.exists(fake) else []:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3:
                rows.append((int(parts[0]), parts[1], parts[2]))
        return rows
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not re.search(r"(runner|agent|solution_paths)\.py", cmd) or not pid.isdigit():
            continue
        cwd = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
        rows.append((int(pid), next((l[1:] for l in cwd.splitlines() if l.startswith("n")), ""), cmd))
    return rows


def busy_with(target):
    """Why the harness at `target` is in use (a runner, an agent or a supervised job working from it), or None."""
    target = os.path.realpath(target)
    for pid, cwd, cmd in processes():
        real = os.path.realpath(cwd) if cwd else ""
        if real == target or real.startswith(target + os.sep):
            return "pid %d (%s) works from %s" % (pid, cmd.split()[1] if len(cmd.split()) > 1 else cmd, os.path.basename(target))
    for f in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        if f.endswith(".pid"):
            rec = read_pid(f[:-4])
            if rec and os.path.realpath(rec.get("harness", "")) == target:
                return "bench job %s (pid %d) runs from %s" % (f[:-4], rec["pid"], os.path.basename(target))
    return None


def heartbeat_key():
    for path in (os.environ.get("MEMEX_FLEET_FILE"), os.environ.get("MEMEX_OVERLAY_FILE")):
        if path and os.path.isfile(path):
            for line in open(path):
                m = re.match(r'\s*Hosting__ModuleReportSecret:\s*"?([^"\s]+)"?', line)
                if m:
                    return m.group(1)
    return None


def api(url, token, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": accept,
                                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "memex-local-harness-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def harness_sync(control, deployment, command_id, commit):
    if not COMMIT.fullmatch(commit or ""):
        return failed("no valid commit in the command (40 lower-case hex) — nothing was run")
    target = os.path.join(BENCH, "harness@" + commit[:12])
    current = os.path.realpath(CURRENT) if os.path.islink(CURRENT) else None
    if os.path.exists(CURRENT) and not os.path.islink(CURRENT):
        return failed("%s is not a symlink — it is left exactly as it is; nothing was installed" % CURRENT)
    if current == os.path.realpath(target) and os.path.isfile(os.path.join(target, ".memex-harness.json")):
        return done("harness@%s is already ~/bench/harness-current — nothing changed" % commit[:12])
    # 🚨 never change code mid-run: a runner, an agent or a job working from the CURRENT harness defers the repoint
    if current and (why := busy_with(current)):
        print(one_line("DEFERRED: %s — harness-current stays %s; asked again at the next tick" % (why, os.path.basename(current))))
        return DEFERRED
    if not os.path.isfile(os.path.join(target, ".memex-harness.json")):
        why = install(control, deployment, command_id, commit, target)
        if why:
            return failed(why)
    # the repoint: a fresh symlink renamed over the old one — rename(2) is atomic, so a reader sees the old or the new
    tmp = os.path.join(BENCH, ".harness-current.%d" % os.getpid())
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.basename(target), tmp)
    os.replace(tmp, CURRENT)
    return done("harness-current -> harness@%s (Systemorph/MeshWeaver.Plugins@%s, verified)%s" % (
        commit[:12], commit, "" if not current else "; was " + os.path.basename(current)))


def install(control, deployment, command_id, commit, target):
    """Fetches and verifies the harness into a temporary directory, then renames it to `target`. Returns why not, or None."""
    key = heartbeat_key()
    if not key:
        return "no heartbeat key on this Mac (fleet.yaml / the overlay) — control cannot be asked for the harness"
    body = json.dumps({"deployment": deployment, "commandId": command_id, "ts": int(time.time())}, separators=(",", ":")).encode()
    sig = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(control.rstrip("/") + "/api/fleet/harness-token", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig, "User-Agent": "memex-local-harness-sync"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            grant = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return "control refused the harness token (HTTP %d) — %s" % (e.code, "its image predates the route" if e.code == 404 else "nothing was installed")
    except Exception as e:  # noqa: BLE001 — a network failure is a Failed report, never a crash
        return "control could not be asked for the harness token (%s)" % str(e)[:160]
    token, repo, granted = grant.get("token"), grant.get("repository"), grant.get("commit")
    if not isinstance(token, str) or repo not in REPOS or granted != commit or grant.get("path") != HARNESS_PATH:
        return "control's harness grant does not name %s@%s/%s — nothing was installed" % (REPOS[0], commit, HARNESS_PATH)
    tmp = target + ".tmp.%d" % os.getpid()
    try:
        cmp = api("%s/repos/%s/compare/%s...main" % (GITHUB, repo, commit), token)
        if cmp.get("status") not in ("ahead", "identical"):
            return "commit %s is not on %s main (compare: %s) — nothing was installed" % (commit, repo, cmp.get("status"))
        os.makedirs(tmp)
        count = fetch_dir(repo, commit, HARNESS_PATH, tmp, token)
    except urllib.error.HTTPError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return "GitHub answered HTTP %d while fetching %s@%s — nothing was installed" % (e.code, repo, commit[:12])
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return "the harness could not be fetched (%s) — nothing was installed" % str(e)[:200]
    finally:
        token = None  # held for the download only; never written anywhere
    why = verify(tmp)
    if why:
        shutil.rmtree(tmp, ignore_errors=True)
        return why
    shutil.rmtree(os.path.join(tmp, "__pycache__"), ignore_errors=True)
    with open(os.path.join(tmp, ".memex-harness.json"), "w") as f:
        json.dump({"repository": repo, "commit": commit, "path": HARNESS_PATH, "files": count,
                   "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "command": command_id}, f, indent=1)
    os.replace(tmp, target)
    return None


def verify(tmp):
    """Why a fetched harness may not be installed, or None: its scripts are there, every .py compiles, its own tests pass."""
    for need in ("runner.py", "agent.py", "push.py"):
        if not os.path.isfile(os.path.join(tmp, need)):
            return "the fetched harness has no %s — nothing was installed" % need
    bad = [f for f in os.listdir(tmp) if f.endswith(".py")
           and subprocess.call([PY, "-m", "py_compile", os.path.join(tmp, f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0]
    if bad:
        return "the fetched harness does not compile (%s) — nothing was installed" % ", ".join(sorted(bad))
    if os.path.isfile(os.path.join(tmp, "test_harness.py")) and os.environ.get("MEMEX_BENCH_SKIP_TESTS") != "1":
        try:
            t = subprocess.run([PY, "-m", "unittest", "-q", "test_harness"], cwd=tmp, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            return "the fetched harness's own tests did not finish in 30 min — nothing was installed"
        if t.returncode != 0:
            return "the fetched harness's own tests FAILED (%s) — nothing was installed" % ((t.stderr.strip().splitlines() or ["?"])[-1])
    return None


def fetch_dir(repo, commit, path, into, token):
    """Every file under `path` at `commit`, each checked against its git blob id. Returns the file count."""
    count = 0
    for entry in api("%s/repos/%s/contents/%s?ref=%s" % (GITHUB, repo, path, commit), token):
        name = entry.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) or name in (".", ".."):
            raise ValueError("an entry named %r" % name)
        if entry.get("type") == "dir":
            os.makedirs(os.path.join(into, name))
            count += fetch_dir(repo, commit, path + "/" + name, os.path.join(into, name), token)
        elif entry.get("type") == "file":
            sha = entry.get("sha", "")
            blob = api("%s/repos/%s/git/blobs/%s" % (GITHUB, repo, sha), token)
            data = base64.b64decode(blob.get("content", ""))
            if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != sha:
                raise ValueError("%s/%s does not match its blob id" % (path, name))
            with open(os.path.join(into, name), "wb") as f:
                f.write(data)
            if name.endswith((".sh", ".py")):
                os.chmod(os.path.join(into, name), 0o755)
            count += 1
        else:
            raise ValueError("%s/%s is a %s, not a file or a directory" % (path, name, entry.get("type")))
    return count


def main(argv):
    if len(argv) >= 1 and argv[0] == "harness-sync" and len(argv) == 5:
        return harness_sync(*argv[1:])
    if len(argv) == 4 and argv[0] == "bench":
        return bench(argv[1], argv[2], argv[3])
    if len(argv) == 3 and argv[0] == "supervise":
        return supervise(argv[1], argv[2])
    if len(argv) == 2 and argv[0] == "busy":
        why = busy_with(argv[1]); print(why or "idle"); return 0 if why else 1
    print("usage: bench-host.py harness-sync <control> <deployment> <commandId> <commit> | bench <start|stop|status> <job.json>")
    return 2


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main(sys.argv[1:]))
