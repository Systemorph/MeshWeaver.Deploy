#!/usr/bin/env python3
"""memex-local instances — further private instances on this Mac, and the executor that runs them for its GUI.

A Mac's FIRST instance is its local control instance (MeshWeaver.Plugins Hosting/MacStudioFleet §3/§3a). Further
private instances are created FROM it: each is its own helm release `memex-<name>` of the same chart, in its own
namespace `memex-<name>`, with its own Postgres, its own data volume and its own key-protection master key.

Run by `memex-local instances …`, which computes the Mac's context (machine id, private-network name, chart, the
registry, the layer files) and passes it in the environment — this program never re-derives what memex-local knows.

THE BRIDGE (schema 1) — how the first instance's GUI reaches this program. The first instance mounts
~/.memex-local/instances read-write at /host-instances. Its Hosting module writes `jobs/<jobId>.json`; the launchd
agent `com.memex.local.instances` (`memex-local instances agent run`) claims a job by renaming it into
`running/`, runs it, and keeps `results/<jobId>.json` current until a terminal status; every 30 s (and after each
job) it rewrites `inventory.json`. The portal deletes a result after recording it. A job that is not valid JSON or
fails validation gets a `Refused` result naming why — never a silent drop. Contract: deploy/homebrew/README.md.

🚨 Per-instance STATE (the spec, the generated layers, the secrets) lives in ~/.memex-local/instance-state/<name>/,
OUTSIDE the mounted bridge: Colima mounts the Mac's home with virtiofs, where every uid inside the VM reads and
writes as the Mac user (measured on a Mac Studio, 2026-11-08: `nobody` in the VM wrote a file in a 0755 directory).
A 0600 secrets file inside the mount would therefore be readable by the first instance's pod.

Nothing here builds a shell command from a string: every external program is called with an argument LIST.
"""
import base64
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

SCHEMA = 1
OPERATIONS = ("create", "start", "stop", "update", "remove")
TERMINAL = ("Succeeded", "Failed", "Refused")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,22}[a-z0-9]$")
RESERVED = {"memex", "control", "build", "e2e", "www", "api", "default", "monitoring", "ingress-nginx", "kube-system"}
PACKAGE_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._*-]+$")
MAX_PACKAGES = 40
JOB_ID_RE = re.compile(r"^[a-z0-9-]{1,100}$")
HOST_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)+$")
OWNER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,99}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
REQUESTED_BY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,199}$")
DEFAULT_RESOURCES = {"cpu": 2, "memoryGi": 6, "storageGi": 20}
HOSTING_PACKAGE = "Plugins/Hosting"
LOG_LINES = 200


class Refused(Exception):
    """The request cannot be run as asked (validation, capacity, confirmation). Nothing was changed."""


class Failed(Exception):
    """A step failed after the request was accepted."""


# ── context (from memex-local) ────────────────────────────────────────────────────────────────────────────────
def env(key, default=""):
    return os.environ.get(key, default)


LOCAL_HOME = env("MEMEX_LOCAL_HOME") or os.path.expanduser("~/.memex-local")
BRIDGE = os.path.join(LOCAL_HOME, "instances")
STATE = os.path.join(LOCAL_HOME, "instance-state")
CERT_DIR = os.path.join(LOCAL_HOME, "certs")
KUBE_CONTEXT = env("MEMEX_KUBE_CONTEXT", "colima")
FIRST_NAMESPACE = env("MEMEX_NAMESPACE", "memex")
STORAGE_CLASS = env("MEMEX_INSTANCES_STORAGE_CLASS", "local-path")
MAGIC_DNS = env("MEMEX_INSTANCES_RESOLVER", "100.100.100.100")
FALLBACK_PORTAL_IMAGE = "ghcr.io/systemorph/memex-portal-ai:latest"
FALLBACK_MIGRATION_IMAGE = "ghcr.io/systemorph/memex-migration:latest"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def machine():
    return env("MEMEX_INSTANCES_MACHINE") or "mac"


def namespace(name):
    return "memex-" + name


def state_dir(name):
    return os.path.join(STATE, name)


def write_json_atomic(path, data, mode=0o644):
    """tmp + rename. The bridge directory is writable by the first instance's pod, so nothing it could plant is
    followed: the directory must not be a symlink, and the temp file — an unpredictable name — is created with
    O_EXCL|O_NOFOLLOW (a planted file or symlink fails the write instead of redirecting it). The final rename
    replaces whatever sits at `path` (a symlink included) without following it."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    if os.path.islink(d):
        raise Failed("%s is a symbolic link — refusing to write through it" % d)
    tmp = os.path.join(d, ".%s.tmp-%s" % (os.path.basename(path), secrets.token_hex(8)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def has_control_chars(s):
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


# ── validation ────────────────────────────────────────────────────────────────────────────────────────────────
def check_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise Refused("name %r is not ^[a-z][a-z0-9-]{1,22}[a-z0-9]$ (3–24 lower-case letters, digits, dashes)" % (name,))
    if name in RESERVED or name.startswith("kube-"):
        raise Refused("name %r is reserved (%s, kube-*)" % (name, ", ".join(sorted(RESERVED))))
    if "--" in name:
        raise Refused("name %r has a double dash" % name)
    return name


def number(value, what, lo, hi, integer):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused("%s must be a number (got %r)" % (what, value))
    if integer and float(value) != int(value):
        raise Refused("%s must be a whole number (got %r)" % (what, value))
    if integer:
        value = int(value)
    elif round(float(value) * 10) != float(value) * 10:
        raise Refused("%s takes at most one decimal (got %r)" % (what, value))
    if not lo <= value <= hi:
        raise Refused("%s must be between %s and %s (got %s)" % (what, lo, hi, value))
    return int(value) if float(value) == int(value) else float(value)


def check_spec(spec, operation, stored=None):
    """The normalised spec. `stored` is the instance's current spec (update): what update may not change is checked
    against it, and what a partial update leaves out is kept."""
    if not isinstance(spec, dict):
        raise Refused("spec must be an object")
    base = dict(stored or {})
    out = {}
    if operation == "create":
        host = spec.get("hostName")
        if not isinstance(host, str) or not HOST_RE.match(host):
            raise Refused("spec.hostName %r is not a lower-case DNS name" % (host,))
        out["hostName"] = host
        owner = spec.get("owner")
        if not isinstance(owner, dict):
            raise Refused("spec.owner {id, email} is required — an instance never comes up without its administrator")
        oid, mail = owner.get("id"), owner.get("email")
        if not isinstance(oid, str) or not OWNER_ID_RE.match(oid):
            raise Refused("spec.owner.id %r is not a user id" % (oid,))
        if not isinstance(mail, str) or not EMAIL_RE.match(mail):
            raise Refused("spec.owner.email %r is not an e-mail address" % (mail,))
        out["owner"] = {"id": oid, "email": mail}
    else:
        if not base.get("hostName") or not base.get("owner"):
            raise Refused("no instance to update — create it first")
        for key in ("hostName", "owner", "name"):
            if key in spec and spec[key] not in (None, base.get(key)):
                raise Refused("update never changes %s (it is %r)" % (key, base.get(key)))
        out["hostName"] = base["hostName"]
        out["owner"] = base["owner"]
    dn = spec.get("displayName", base.get("displayName", ""))
    if dn is None:
        dn = ""
    if not isinstance(dn, str) or len(dn) > 80 or has_control_chars(dn):
        raise Refused("spec.displayName must be at most 80 characters with no control characters")
    out["displayName"] = dn.strip()
    res = spec.get("resources")
    res = res if isinstance(res, dict) else {}
    cur = dict(DEFAULT_RESOURCES)
    cur.update(base.get("resources") or {})
    out["resources"] = {
        "cpu": number(res.get("cpu", cur["cpu"]), "spec.resources.cpu", 0.5, 64, False),
        "memoryGi": number(res.get("memoryGi", cur["memoryGi"]), "spec.resources.memoryGi", 2, 256, True),
        "storageGi": number(res.get("storageGi", cur["storageGi"]), "spec.resources.storageGi", 5, 2048, True),
    }
    if operation == "update" and "storageGi" in res and out["resources"]["storageGi"] != cur["storageGi"]:
        raise Refused("update never changes spec.resources.storageGi (the data volume is %s Gi; local volumes do not resize)" % cur["storageGi"])
    pk = spec.get("packages", base.get("packages", [HOSTING_PACKAGE]))
    if not isinstance(pk, list):
        raise Refused("spec.packages must be a list")
    if len(pk) > MAX_PACKAGES:
        raise Refused("spec.packages names %d packages — at most %d" % (len(pk), MAX_PACKAGES))
    seen = []
    for p in pk:
        if not isinstance(p, str) or not PACKAGE_RE.match(p):
            raise Refused("spec.packages: %r is not <Source>/<Package>" % (p,))
        if p not in seen:
            seen.append(p)
    out["packages"] = seen
    ai = spec.get("ai")
    ai = ai if isinstance(ai, dict) else {}
    bai = base.get("ai") or {}
    model = ai.get("chatModel", bai.get("chatModel", ""))
    model = "" if model is None else model
    if not isinstance(model, str) or (model and not MODEL_RE.match(model)):
        raise Refused("spec.ai.chatModel %r is not a model name" % (model,))
    emb = ai.get("embeddings", bai.get("embeddings", True))
    if not isinstance(emb, bool):
        raise Refused("spec.ai.embeddings must be true or false")
    out["ai"] = {"chatModel": model, "embeddings": emb}
    return out


def check_job(job, file_stem):
    if not isinstance(job, dict):
        raise Refused("the job is not a JSON object")
    if job.get("schema") != SCHEMA:
        raise Refused("schema %r is not %d" % (job.get("schema"), SCHEMA))
    jid = job.get("jobId")
    if not isinstance(jid, str) or not JOB_ID_RE.match(jid):
        raise Refused("jobId %r is not ^[a-z0-9-]{1,100}$" % (jid,))
    if jid != file_stem:
        raise Refused("jobId %r does not match its file name %r" % (jid, file_stem))
    op = job.get("operation")
    if op not in OPERATIONS:
        raise Refused("operation %r is not one of %s" % (op, "|".join(OPERATIONS)))
    check_name(job.get("name"))
    rb = job.get("requestedBy")
    if not isinstance(rb, str) or not REQUESTED_BY_RE.match(rb):
        raise Refused("requestedBy %r is not a user id" % (rb,))
    if op == "remove" and job.get("confirm") != job["name"]:
        raise Refused("remove needs confirm equal to the name %r — it deletes the instance's database and data" % job["name"])
    if op in ("create", "update") and not isinstance(job.get("spec"), dict):
        raise Refused("%s needs a spec" % op)
    return job


# ── progress (a terminal, or a result file) ───────────────────────────────────────────────────────────────────
class Progress:
    def __init__(self, job=None, result_path=None):
        self.job = job
        self.path = result_path
        self.redact = set()
        self.result = {
            "schema": SCHEMA, "jobId": (job or {}).get("jobId"), "operation": (job or {}).get("operation"),
            "name": (job or {}).get("name"), "status": "Running", "message": "", "startedAt": now(),
            "updatedAt": now(), "finishedAt": None, "steps": [], "log": [], "url": None, "actionsForOwner": [],
        }

    def scrub(self, text):
        for s in self.redact:
            if s and len(s) >= 6:
                text = text.replace(s, "<redacted>")
        return text

    def save(self):
        if not self.path:
            return
        self.result["updatedAt"] = now()
        self.result["log"] = self.result["log"][-LOG_LINES:]
        write_json_atomic(self.path, self.result)

    def step(self, name, status, detail=""):
        detail = self.scrub(detail)
        for s in self.result["steps"]:
            if s["name"] == name:
                s["status"], s["detail"] = status, detail
                break
        else:
            self.result["steps"].append({"name": name, "status": status, "detail": detail})
        if not self.path:
            mark = {"Done": " ok ", "Running": "==> ", "Failed": "err ", "Skipped": "skip", "Pending": "    "}.get(status, "    ")
            print("%s %s%s" % (mark, name, (" — " + detail) if detail else ""), flush=True)
        self.save()

    def log(self, line):
        line = self.scrub(line.rstrip())
        if not line:
            return
        self.result["log"].append(line[:500])
        if not self.path:
            print("    " + line, flush=True)
        else:
            self.save()

    def action(self, text):
        if text not in self.result["actionsForOwner"]:
            self.result["actionsForOwner"].append(text)
        if not self.path:
            print("  owner: " + text, flush=True)
        self.save()

    def finish(self, status, message):
        self.result["status"], self.result["message"] = status, self.scrub(message)[:500]
        self.result["finishedAt"] = now()
        for s in self.result["steps"]:
            if s["status"] == "Running":
                s["status"] = "Failed" if status != "Succeeded" else "Done"
        if not self.path:
            print(("%s: %s" % (status, self.result["message"])), flush=True)
        self.save()


# ── external programs (argument lists only) ───────────────────────────────────────────────────────────────────
def run(args, progress=None, timeout=120, input_text=None, check=True, quiet=False):
    try:
        p = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise Failed("%s is not installed" % args[0])
    except subprocess.TimeoutExpired:
        raise Failed("%s did not finish within %d s" % (" ".join(args[:3]), timeout))
    if progress and not quiet:
        for line in (p.stdout + p.stderr).splitlines()[-40:]:
            progress.log(line)
    if check and p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["exit %d" % p.returncode]
        raise Failed("%s %s failed: %s" % (os.path.basename(args[0]), " ".join(args[1:3]), tail[0][:300]))
    return p


def kubectl(*args):
    return ["kubectl", "--context", KUBE_CONTEXT] + list(args)


def kget_json(*args):
    p = run(kubectl(*args) + ["-o", "json"], check=False, quiet=True, timeout=30)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


# ── the Mac's capacity ────────────────────────────────────────────────────────────────────────────────────────
def quantity_cpu(v):
    if not v:
        return 0.0
    v = str(v)
    if v.endswith("m"):
        return float(v[:-1]) / 1000
    return float(v)


UNITS = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "k": 1e3}


def quantity_gi(v):
    if not v:
        return 0.0
    v = str(v)
    for u in ("Ki", "Mi", "Gi", "Ti", "K", "M", "G", "T", "k"):
        if v.endswith(u):
            return float(v[: -len(u)]) * UNITS[u] / 2 ** 30
    if v.endswith("m"):
        return float(v[:-1]) / 1000 / 2 ** 30
    return float(v) / 2 ** 30


def capacity():
    """The VM's allocatable CPU/memory and what running pods REQUEST (requests, not use — what the scheduler
    counts). None when the cluster cannot be read."""
    nodes = kget_json("get", "nodes")
    pods = kget_json("get", "pods", "-A")
    if not nodes or pods is None or not nodes.get("items"):
        return None
    cpu = mem = 0.0
    for n in nodes["items"]:
        a = n.get("status", {}).get("allocatable", {})
        cpu += quantity_cpu(a.get("cpu"))
        mem += quantity_gi(a.get("memory"))
    rc = rm = 0.0
    for p in pods.get("items", []):
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for c in p.get("spec", {}).get("containers", []):
            r = (c.get("resources") or {}).get("requests") or {}
            rc += quantity_cpu(r.get("cpu"))
            rm += quantity_gi(r.get("memory"))
    return {"vmCpu": round(cpu, 2), "vmMemoryGi": round(mem, 1), "requestedCpu": round(rc, 2), "requestedMemoryGi": round(rm, 1)}


def check_capacity(cpu, mem_gi, progress, what):
    cap = capacity()
    if cap is None:
        raise Failed("the cluster cannot be read (kubectl --context %s) — is the stack up? (memex-local status)" % KUBE_CONTEXT)
    free_cpu = cap["vmCpu"] - cap["requestedCpu"]
    free_mem = cap["vmMemoryGi"] - cap["requestedMemoryGi"]
    detail = "VM %s CPU / %s GiB; requested %s CPU / %s GiB; free %.2f CPU / %.1f GiB; %s needs %s CPU / %s GiB" % (
        cap["vmCpu"], cap["vmMemoryGi"], cap["requestedCpu"], cap["requestedMemoryGi"], free_cpu, free_mem, what, cpu, mem_gi)
    if cpu > free_cpu + 1e-9 or mem_gi > free_mem + 1e-9:
        progress.step("Capacity", "Failed", detail)
        raise Refused("not enough room in the Colima VM: " + detail)
    progress.step("Capacity", "Done", detail)


# ── the instance's record ─────────────────────────────────────────────────────────────────────────────────────
def load_spec(name):
    path = os.path.join(state_dir(name), "spec.json")
    if not os.path.exists(path):
        return None
    return read_json(path)


def save_spec(name, spec):
    write_json_atomic(os.path.join(state_dir(name), "spec.json"), spec, 0o600)


def instance_names():
    if not os.path.isdir(STATE):
        return []
    return sorted(n for n in os.listdir(STATE) if NAME_RE.match(n) and os.path.exists(os.path.join(STATE, n, "spec.json")))


def first_instance_config():
    """Non-secret keys of the first instance's EFFECTIVE configuration (its rendered ConfigMap — every layer
    folded in), and its sign-in secret read in memory only."""
    cm = kget_json("-n", FIRST_NAMESPACE, "get", "configmap", "memex-portal-config") or {}
    data = cm.get("data") or {}
    sec = kget_json("-n", FIRST_NAMESPACE, "get", "secret", "memex-portal-secrets") or {}
    raw = (sec.get("data") or {}).get("Authentication__Microsoft__ClientSecret", "")
    try:
        client_secret = base64.b64decode(raw).decode("utf-8") if raw else ""
    except (ValueError, UnicodeDecodeError):
        client_secret = ""
    return data, client_secret


def first_instance_images():
    portal = migration = ""
    dep = kget_json("-n", FIRST_NAMESPACE, "get", "deployment", "memex-portal-deployment")
    for c in ((dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []):
        if c.get("name") == "memex-portal" or "memex-portal-ai" in c.get("image", ""):
            portal = c.get("image", "")
            break
    jobs = kget_json("-n", FIRST_NAMESPACE, "get", "jobs", "-l", "app.kubernetes.io/component=memex-migration")
    items = sorted((jobs or {}).get("items") or [], key=lambda j: j.get("metadata", {}).get("creationTimestamp", ""))
    if items:
        cs = items[-1].get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
        if cs:
            migration = cs[0].get("image", "")
    return portal or FALLBACK_PORTAL_IMAGE, migration or FALLBACK_MIGRATION_IMAGE


def in_namespace(url, name):
    """A first-instance service URL (ollama.memex.svc…) pointed at the same service in this instance's namespace."""
    return (url or "").replace(".%s.svc.cluster.local" % FIRST_NAMESPACE, ".%s.svc.cluster.local" % namespace(name))


INHERITED_KEYS = ("Authentication__Provider", "OpenAICompatible__DataResidency")
EMBEDDING_KEYS = ("Embedding__Provider", "Embedding__Endpoint", "Embedding__Model")
SIGNIN_KEYS = ("Authentication__Microsoft__ClientId", "Authentication__Microsoft__TenantId")


def build_layers(name, spec, first_cfg, client_secret, portal_image, migration_image, pg_password, master_key):
    """(values, secrets) — the two per-instance layers, as data (written as JSON, which helm reads as YAML).
    The back-lease, fleet, build-host and publish layers are deliberately ABSENT: they belong to the Mac's first
    instance (its heartbeat, its keys, its build host, its public names)."""
    host = spec["hostName"]
    portal_cfg = {
        "Portal__InstanceName": spec["displayName"] or name,
        "PluginCatalog__InstallPreInstalledPackages": "true",
        "Auth__GlobalAdmins__0": spec["owner"]["id"],
        "Instance__Owner__Id": spec["owner"]["id"],
        "Instance__Owner__Email": spec["owner"]["email"],
        "OpenAICompatible__Endpoint": "http://ollama.%s.svc.cluster.local:11434/v1" % namespace(name),
        "Features__Onboarding__InvitationOnly": "true",
        "Authentication__EnableDevLogin": "false",
        "Authentication__DevAdminUsers": "",
    }
    # Hosting first (the fleet-host template's package), then the spec's — one exact, ordered set.
    packages = [HOSTING_PACKAGE] + [p for p in spec["packages"] if p != HOSTING_PACKAGE]
    for i, p in enumerate(packages):
        portal_cfg["PluginCatalog__InstallByDefault__%d" % i] = p
    for k in INHERITED_KEYS:
        if first_cfg.get(k):
            portal_cfg[k] = first_cfg[k]
    if first_cfg.get(SIGNIN_KEYS[0]):
        for k in SIGNIN_KEYS:
            if first_cfg.get(k):
                portal_cfg[k] = first_cfg[k]
    migration_cfg = {}
    if spec["ai"]["embeddings"]:
        for k in EMBEDDING_KEYS:
            if first_cfg.get(k):
                v = in_namespace(first_cfg[k], name) if k == "Embedding__Endpoint" else first_cfg[k]
                portal_cfg[k] = v
                migration_cfg[k] = v
    model = spec["ai"]["chatModel"]
    if model:
        portal_cfg["OpenAICompatible__Models__0"] = model
        portal_cfg["AI__Tiers__chat"] = "Provider/OpenAICompatible/" + model
        # the Mac's measured window belongs to ITS model; another model's stays undeclared (blank = not rendered)
        portal_cfg["OpenAICompatible__ContextWindowTokens__0__Model"] = ""
        portal_cfg["OpenAICompatible__ContextWindowTokens__0__Tokens"] = ""
    cpu = spec["resources"]["cpu"]
    mem = "%dGi" % spec["resources"]["memoryGi"]
    values = {
        "portal": {"image": portal_image},
        "migration": {"image": migration_image},
        "persistDataVolume": False,
        "persistence": {"data": {"claimName": "memex-data", "create": True, "size": "%dGi" % spec["resources"]["storageGi"],
                                 "storageClass": STORAGE_CLASS, "accessMode": "ReadWriteOnce"}},
        "resources": {"portal": {"requests": {"cpu": str(cpu), "memory": mem}, "limits": {"cpu": str(cpu), "memory": mem}}},
        "pluginCatalog": {"registryUrl": env("MEMEX_INSTANCES_REGISTRY_URL"), "instanceId": "%s-%s" % (machine(), name),
                          "homeUrl": "https://" + host, "installByDefault": []},
        "ingress": {"enabled": True, "host": host, "additionalHosts": [], "tlsSecret": "%s-tls" % namespace(name),
                    "clusterIssuer": "none"},
        "config": {"memex_portal": portal_cfg},
    }
    if migration_cfg:
        values["config"]["memex_migration"] = migration_cfg
    conn = "Host=memex-postgres-service;Port=5432;Database=memex;Username=postgres;Password=" + pg_password
    portal_secrets = {"memex_postgres_password": pg_password, "ConnectionStrings__memex": conn,
                      "Ai__KeyProtection__MasterKey": master_key, "OpenAICompatible__ApiKey": "ollama"}
    if client_secret and first_cfg.get(SIGNIN_KEYS[0]):
        portal_secrets["Authentication__Microsoft__ClientSecret"] = client_secret
    secret_values = {"secrets": {
        "memex_postgres": {"memex_postgres_password": pg_password},
        "memex_migration": {"memex_postgres_password": pg_password, "ConnectionStrings__memex": conn},
        "memex_portal": portal_secrets,
    }}
    return values, secret_values


def instance_secrets(name):
    """The instance's own generated secrets, created ONCE (0600, outside the bridge) and never printed."""
    path = os.path.join(state_dir(name), "secrets.json")
    if os.path.exists(path):
        s = read_json(path)
    else:
        s = {"postgresPassword": secrets.token_hex(24), "masterKey": base64.b64encode(secrets.token_bytes(32)).decode()}
        write_json_atomic(path, s, 0o600)
    return s


def layer_files():
    """The shared layers, in order — the same files the first instance is deployed with (not its overlay)."""
    share = env("MEMEX_LOCAL_SHARE")
    chart = env("MEMEX_INSTANCES_CHART")
    files = [os.path.join(chart, "values.yaml"), os.path.join(share, "values.local.defaults.yaml")]
    for f in (env("MEMEX_INSTANCES_LLM_AUTO_FILE"), env("MEMEX_INSTANCES_LLM_FILE")):
        if f and os.path.exists(f):
            files.append(f)
    files += [os.path.join(share, "values.local.registry-mode.yaml"), os.path.join(share, "values.local.fleet-host.yaml")]
    return files


def helm_args(name):
    d = state_dir(name)
    args = ["helm", "upgrade", "--install", namespace(name), env("MEMEX_INSTANCES_CHART"),
            "--kube-context", KUBE_CONTEXT, "-n", namespace(name), "--create-namespace"]
    for f in layer_files():
        args += ["-f", f]
    args += ["-f", os.path.join(d, "values.json"), "-f", os.path.join(d, "secret-values.json")]
    if env("MEMEX_INSTANCES_HELM_FORCE_CONFLICTS", "1") == "1":
        args.append("--force-conflicts")
    return args


def signin_urls(host):
    return ["https://%s/signin-microsoft" % host, "https://%s/auth/callback/Microsoft" % host, "https://%s/auth/ea/callback" % host]


# ── certificates (the Mac's `memex-local cert` configuration) ─────────────────────────────────────────────────
def cert_config():
    path = os.path.join(CERT_DIR, "dns01.env")
    if not os.path.exists(path) or not os.path.exists(os.path.join(CERT_DIR, "dns01.secret")):
        return None
    cfg = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k] = v.strip('"')
    return cfg


def cert_file(host, ext):
    return os.path.join(CERT_DIR, "lego", "certificates", "%s.%s" % (host, ext))


def cert_expiry(host):
    crt = cert_file(host, "crt")
    if not os.path.exists(crt):
        return None
    p = run(["openssl", "x509", "-in", crt, "-noout", "-enddate"], check=False, quiet=True, timeout=10)
    m = re.search(r"notAfter=(.+)", p.stdout or "")
    if not m:
        return None
    try:
        t = datetime.datetime.strptime(m.group(1).strip().replace("  ", " "), "%b %d %H:%M:%S %Y %Z")
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def issue_certificate(name, host, progress):
    """Obtain (or renew within 30 days) the instance's certificate and install it as its TLS Secret. Skipped,
    with the missing step said, when this Mac has no certificate configuration."""
    cfg = cert_config()
    if cfg is None:
        progress.step("Certificate", "Skipped", "no certificate configuration on this Mac — the Ingress serves ingress-nginx's default certificate")
        progress.action("Configure certificates on this Mac (memex-local cert config … < secret), then: memex-local instances update %s — the certificate for %s is issued then" % (name, host))
        return False
    zone = cfg.get("AZURE_ZONE_NAME", "")
    if not zone or not host.endswith("." + zone):
        progress.step("Certificate", "Skipped", "%s is not inside the DNS zone %s this Mac may write challenge records in" % (host, zone or "<none>"))
        progress.action("Choose a host name inside %s, or configure a certificate identity for its zone" % (zone or "the certificate zone"))
        return False
    if not shutil.which("lego"):
        progress.step("Certificate", "Skipped", "lego is not installed (brew install lego)")
        progress.action("Install lego (brew install lego), then: memex-local instances update %s" % name)
        return False
    progress.step("Certificate", "Running", "Let's Encrypt over DNS-01 in %s" % zone)
    lego_env = dict(os.environ)
    lego_env.update({"AZURE_AUTH_METHOD": "env", "AZURE_CLIENT_SECRET_FILE": os.path.join(CERT_DIR, "dns01.secret")})
    for k in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "AZURE_ZONE_NAME"):
        lego_env[k] = cfg.get(k, "")
    args = ["lego", "run", "--accept-tos", "--email", cfg.get("LEGO_EMAIL", ""), "--dns", "azuredns", "--domains", host,
            "--key-type", "EC256", "--renew-days", "30", "--path", os.path.join(CERT_DIR, "lego")]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=600, env=lego_env)
    except subprocess.TimeoutExpired:
        raise Failed("lego did not finish within 600 s")
    for line in (p.stdout + p.stderr).splitlines()[-20:]:
        progress.log(line)
    if p.returncode != 0:
        raise Failed("lego could not obtain or renew the certificate for %s (its output is in the log)" % host)
    install_tls_secret(name, host, progress)
    progress.step("Certificate", "Done", "valid until %s (renewed daily by the certificate agent, within 30 days of expiry)" % (cert_expiry(host) or "?"))
    return True


def install_tls_secret(name, host, progress):
    crt, key = cert_file(host, "crt"), cert_file(host, "key")
    if not (os.path.exists(crt) and os.path.exists(key)):
        return False
    y = run(kubectl("-n", namespace(name), "create", "secret", "tls", "%s-tls" % namespace(name), "--cert=" + crt,
                    "--key=" + key, "--dry-run=client", "-o", "yaml"), quiet=True)
    run(kubectl("-n", namespace(name), "apply", "-f", "-"), progress, input_text=y.stdout, quiet=True)
    return True


# ── the lifecycle ─────────────────────────────────────────────────────────────────────────────────────────────
def name_resolves(host):
    if not shutil.which("dig"):
        return False
    p = run(["dig", "+short", "+time=2", "+tries=1", "@" + MAGIC_DNS, host, "A"], check=False, quiet=True, timeout=10)
    return any(re.match(r"^\d+\.\d+\.\d+\.\d+$", l.strip()) for l in (p.stdout or "").splitlines())


def owner_actions(name, spec, first_cfg, progress):
    host = spec["hostName"]
    cid = first_cfg.get(SIGNIN_KEYS[0], "")
    if cid:
        for url in signin_urls(host):
            progress.action("Register the redirect URI %s on the sign-in app %s" % (url, cid))
    else:
        progress.action("This Mac's first instance has no Microsoft sign-in configured, so %s has none either and nobody can sign in — configure it (memex-local internal on …), then: memex-local instances update %s" % (host, name))
    if not name_resolves(host):
        ip = env("MEMEX_INSTANCES_NETWORK_IP")
        progress.action("Declare the private-network name %s → %s (Headscale dns.extraRecords on the control server's chart values)" % (host, ip or "<this Mac's network address: tailscale ip -4>"))


def wait_ready(name, progress, timeout=900):
    progress.step("Rollout", "Running", "waiting for the portal (first boot installs and compiles its packages)")
    p = run(kubectl("-n", namespace(name), "rollout", "status", "deployment/memex-portal-deployment", "--timeout=%ds" % timeout),
            progress, timeout=timeout + 30, check=False)
    if p.returncode != 0:
        logs = run(kubectl("-n", namespace(name), "logs", "deployment/memex-portal-deployment", "--tail=30"), check=False, quiet=True)
        for line in (logs.stdout or "").splitlines():
            progress.log(line)
        raise Failed("the portal of %s did not become ready within %d s (its last log lines are in the log)" % (name, timeout))
    progress.step("Rollout", "Done", "portal ready")


def health(name):
    """(health, detail) — the instance's own /health, read THROUGH THE CLUSTER (the API server's service proxy),
    never through the private network."""
    t0 = time.time()
    p = run(kubectl("get", "--raw", "/api/v1/namespaces/%s/services/memex-portal-service:8080/proxy/health" % namespace(name)),
            check=False, quiet=True, timeout=20)
    ms = int((time.time() - t0) * 1000)
    if p.returncode == 0:
        first = (p.stdout.strip().split() or ["?"])[0]
        verdict = "Healthy" if first == "Healthy" else "Degraded"
        return verdict, "GET /health %s in %d ms" % (first, ms)
    err = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or [""]
    e = err[0]
    if re.search(r"no endpoints|connection refused|not found|i/o timeout|dial tcp", e, re.I):
        return "Unreachable", "GET /health: %s" % e[:200]
    return "Degraded", "GET /health: %s" % e[:200]


def deploy(name, spec, progress):
    first_cfg, client_secret = first_instance_config()
    if not first_cfg:
        raise Failed("the first instance's configuration (namespace %s) cannot be read — is it up? (memex-local status)" % FIRST_NAMESPACE)
    progress.redact.add(client_secret)
    if not env("MEMEX_INSTANCES_REGISTRY_URL"):
        raise Refused("this Mac is not in registry mode — an instance registers at the same registry as the first instance (memex-local registry <url>)")
    portal_image, migration_image = first_instance_images()
    s = instance_secrets(name)
    progress.redact.update({s["postgresPassword"], s["masterKey"]})
    values, secret_values = build_layers(name, spec, first_cfg, client_secret, portal_image, migration_image,
                                         s["postgresPassword"], s["masterKey"])
    d = state_dir(name)
    write_json_atomic(os.path.join(d, "values.json"), values, 0o600)
    write_json_atomic(os.path.join(d, "secret-values.json"), secret_values, 0o600)
    progress.step("Layers", "Done", "image %s; packages %s; owner %s" % (portal_image, ", ".join(
        v for k, v in sorted(values["config"]["memex_portal"].items()) if k.startswith("PluginCatalog__InstallByDefault__")), spec["owner"]["id"]))
    y = run(kubectl("create", "namespace", namespace(name), "--dry-run=client", "-o", "yaml"), quiet=True)
    run(kubectl("apply", "-f", "-"), progress, input_text=y.stdout, quiet=True)
    issue_certificate(name, spec["hostName"], progress)
    progress.step("Deploy", "Running", "helm upgrade --install %s (namespace %s)" % (namespace(name), namespace(name)))
    run(helm_args(name), progress, timeout=600)
    progress.step("Deploy", "Done", "release %s" % namespace(name))
    return first_cfg


def scale(name, replicas, progress):
    ns = namespace(name)
    order = (("statefulset", "memex-postgres-statefulset"), ("deployment", "memex-portal-deployment"))
    for kind, obj in (order if replicas else tuple(reversed(order))):
        run(kubectl("-n", ns, "scale", "%s/%s" % (kind, obj), "--replicas=%d" % replicas), progress)


def op_create(name, spec, progress):
    progress.step("Validate", "Running")
    check_name(name)
    spec = check_spec(spec, "create")
    if load_spec(name) or kget_json("get", "namespace", namespace(name)):
        raise Refused("an instance %r already exists on this Mac — use update, start or stop (or remove it first)" % name)
    reserved_hosts = {"memex.localhost", env("MEMEX_INSTANCES_NETWORK_NAME")}
    for other in instance_names():
        reserved_hosts.add((load_spec(other) or {}).get("hostName"))
    if spec["hostName"] in reserved_hosts:
        raise Refused("hostName %s is already served on this Mac" % spec["hostName"])
    progress.step("Validate", "Done", "%s at https://%s" % (name, spec["hostName"]))
    check_capacity(spec["resources"]["cpu"], spec["resources"]["memoryGi"], progress, name)
    spec.update({"name": name, "createdAt": now(), "machine": machine()})
    os.makedirs(state_dir(name), mode=0o700, exist_ok=True)
    save_spec(name, spec)
    progress.result["url"] = "https://" + spec["hostName"]
    first_cfg = deploy(name, spec, progress)
    wait_ready(name, progress)
    h, detail = health(name)
    progress.step("Health", "Done" if h in ("Healthy", "Degraded") else "Failed", detail)
    owner_actions(name, spec, first_cfg, progress)
    progress.step("Name", "Done" if name_resolves(spec["hostName"]) else "Skipped",
                  "%s %s via %s" % (spec["hostName"], "resolves" if name_resolves(spec["hostName"]) else "does not resolve yet", MAGIC_DNS))
    return "created %s at https://%s (%s)" % (name, spec["hostName"], h)


def require(name):
    check_name(name)
    spec = load_spec(name)
    if not spec:
        raise Refused("no instance %r on this Mac (memex-local instances list)" % name)
    return spec


def op_update(name, change, progress):
    progress.step("Validate", "Running")
    stored = require(name)
    spec = check_spec(change or {}, "update", stored)
    spec.update({k: stored[k] for k in ("name", "createdAt", "machine") if k in stored})
    stopped = bool(stored.get("stopped"))
    progress.step("Validate", "Done")
    if not stopped:
        cur = stored.get("resources") or DEFAULT_RESOURCES
        dc, dm = spec["resources"]["cpu"] - cur["cpu"], spec["resources"]["memoryGi"] - cur["memoryGi"]
        if dc > 0 or dm > 0:
            check_capacity(max(dc, 0), max(dm, 0), progress, "the resize of " + name)
        else:
            progress.step("Capacity", "Skipped", "no more CPU or memory than before")
    spec["stopped"] = stopped
    save_spec(name, spec)
    progress.result["url"] = "https://" + spec["hostName"]
    first_cfg = deploy(name, spec, progress)
    if stopped:
        scale(name, 0, progress)
        progress.step("Rollout", "Skipped", "the instance is stopped — the new configuration applies at the next start")
        return "updated %s (stopped; applies at start)" % name
    # :latest under IfNotPresent: the same tag may name new bytes the first instance already runs — a fresh pod takes them.
    run(kubectl("-n", namespace(name), "rollout", "restart", "deployment/memex-portal-deployment"), progress)
    wait_ready(name, progress)
    h, detail = health(name)
    progress.step("Health", "Done" if h in ("Healthy", "Degraded") else "Failed", detail)
    owner_actions(name, spec, first_cfg, progress)
    return "updated %s (%s)" % (name, h)


def op_start(name, progress):
    progress.step("Validate", "Running")
    spec = require(name)
    progress.step("Validate", "Done")
    check_capacity(spec["resources"]["cpu"], spec["resources"]["memoryGi"], progress, name)
    scale(name, 1, progress)
    spec["stopped"] = False
    save_spec(name, spec)
    progress.result["url"] = "https://" + spec["hostName"]
    wait_ready(name, progress, timeout=600)
    h, detail = health(name)
    progress.step("Health", "Done" if h in ("Healthy", "Degraded") else "Failed", detail)
    return "started %s (%s)" % (name, h)


def op_stop(name, progress):
    progress.step("Validate", "Running")
    spec = require(name)
    progress.step("Validate", "Done")
    progress.step("Stop", "Running", "scaling the portal and its database to 0 — data kept")
    scale(name, 0, progress)
    spec["stopped"] = True
    save_spec(name, spec)
    progress.step("Stop", "Done", "stopped; data kept (memex-local instances start %s)" % name)
    return "stopped %s (data kept)" % name


def op_remove(name, confirm, progress):
    progress.step("Validate", "Running")
    spec = require(name)
    if confirm != name:
        raise Refused("remove needs --confirm %s — it deletes the instance's database and data" % name)
    progress.step("Validate", "Done")
    ns = namespace(name)
    open(os.path.join(state_dir(name), "removing"), "w").close()
    progress.step("Uninstall", "Running", "helm uninstall %s" % ns)
    run(["helm", "uninstall", ns, "--kube-context", KUBE_CONTEXT, "-n", ns, "--ignore-not-found"], progress, timeout=300, check=False)
    progress.step("Uninstall", "Done")
    progress.step("Data", "Running", "deleting namespace %s with its database and data volumes" % ns)
    run(kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=true", "--timeout=300s"), progress, timeout=330)
    # local-path deletes a volume's directory when its claim goes; any volume left Released is deleted here.
    pvs = kget_json("get", "pv") or {}
    for pv in pvs.get("items", []):
        ref = pv.get("spec", {}).get("claimRef") or {}
        if ref.get("namespace") == ns:
            run(kubectl("delete", "pv", pv["metadata"]["name"], "--ignore-not-found"), progress, check=False)
    progress.step("Data", "Done", "namespace, database and data volumes deleted")
    host = spec["hostName"]
    for ext in ("crt", "key", "issuer.crt", "json"):
        try:
            os.remove(cert_file(host, ext))
        except OSError:
            pass
    shutil.rmtree(state_dir(name), ignore_errors=True)
    first_cfg, _ = first_instance_config()
    cid = first_cfg.get(SIGNIN_KEYS[0], "")
    if cid:
        for url in signin_urls(host):
            progress.action("Remove the redirect URI %s from the sign-in app %s" % (url, cid))
    progress.action("Remove the private-network name %s (Headscale dns.extraRecords)" % host)
    progress.action("The registry claim %s-%s on %s is NOT released (the registry has no unregister API) — re-creating %s on this Mac needs a registry administrator to release it"
                    % (spec.get("machine") or machine(), name, env("MEMEX_INSTANCES_REGISTRY_URL") or "the registry", name))
    return "removed %s (database and data deleted; registry claim kept)" % name


def execute(job, progress):
    """Run one validated operation; the terminal status is written by the caller."""
    op, name = job["operation"], job["name"]
    if op == "create":
        return op_create(name, job["spec"], progress)
    if op == "update":
        return op_update(name, job.get("spec") or {}, progress)
    if op == "start":
        return op_start(name, progress)
    if op == "stop":
        return op_stop(name, progress)
    return op_remove(name, job.get("confirm"), progress)


def run_job(job, progress):
    try:
        msg = execute(job, progress)
        progress.finish("Succeeded", msg)
        return 0
    except Refused as e:
        progress.finish("Refused", str(e))
        return 3
    except Failed as e:
        progress.finish("Failed", str(e))
        return 1
    except Exception as e:  # noqa: BLE001 — a crash is still a Failed result, never a silent drop
        progress.finish("Failed", "%s: %s" % (type(e).__name__, e))
        return 1


# ── the inventory ─────────────────────────────────────────────────────────────────────────────────────────────
def registered_as(name):
    cache = os.path.join(state_dir(name), "registered-as")
    if os.path.exists(cache):
        with open(cache) as f:
            return f.read().strip() or None
    p = run(kubectl("-n", namespace(name), "exec", "memex-postgres-statefulset-0", "--", "psql", "-U", "postgres", "-d", "memex",
                    "-tAc", "SELECT content->>'instanceId' FROM admin.mesh_nodes WHERE node_type='PluginRegistryCredential' LIMIT 1"),
            check=False, quiet=True, timeout=20)
    v = (p.stdout or "").strip() if p.returncode == 0 else ""
    if v and re.match(r"^[A-Za-z0-9._-]{1,200}$", v):
        with open(cache, "w") as f:
            f.write(v)
        return v
    return None


def describe(name):
    spec = load_spec(name) or {}
    ns = namespace(name)
    host = spec.get("hostName", "")
    item = {"name": name, "namespace": ns, "release": ns, "hostName": host, "url": "https://" + host if host else None,
            "state": "Failed", "ready": "0/0", "health": "Unknown", "healthDetail": "", "nameResolves": name_resolves(host) if host else False,
            "certificateExpires": cert_expiry(host) if host else None, "image": None, "owner": spec.get("owner"),
            "resources": spec.get("resources"), "packages": spec.get("packages", []), "createdAt": spec.get("createdAt"),
            "registeredAs": None}
    if os.path.exists(os.path.join(state_dir(name), "removing")):
        item["state"] = "Removing"
        return item
    dep = kget_json("-n", ns, "get", "deployment", "memex-portal-deployment")
    if not dep:
        item["healthDetail"] = "no portal deployment in %s" % ns
        return item
    want = dep.get("spec", {}).get("replicas", 0) or 0
    ready = dep.get("status", {}).get("readyReplicas", 0) or 0
    item["ready"] = "%d/%d" % (ready, want)
    cs = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    item["image"] = next((c.get("image") for c in cs if c.get("name") == "memex-portal"), cs[0].get("image") if cs else None)
    if want == 0:
        item["state"], item["healthDetail"] = "Stopped", "scaled to 0 (data kept)"
        return item
    pods = kget_json("-n", ns, "get", "pods", "-l", "app.kubernetes.io/component=memex-portal") or {}
    crash = any((st.get("state", {}).get("waiting") or {}).get("reason") in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull")
                for p in pods.get("items", []) for st in (p.get("status", {}).get("containerStatuses") or []))
    item["state"] = "Running" if ready >= 1 else ("Failed" if crash else "Starting")
    item["health"], item["healthDetail"] = health(name)
    if ready >= 1:
        item["registeredAs"] = registered_as(name)
    return item


def inventory():
    return {"schema": SCHEMA, "sampledAt": now(), "machine": machine(), "networkName": env("MEMEX_INSTANCES_NETWORK_NAME"),
            "agentVersion": env("MEMEX_INSTANCES_AGENT_VERSION", "0.3.0"), "capacity": capacity(),
            "instances": [describe(n) for n in instance_names()]}


def write_inventory():
    write_json_atomic(os.path.join(BRIDGE, "inventory.json"), inventory())


# ── the agent: claim, run, report ─────────────────────────────────────────────────────────────────────────────
def bridge(*parts):
    return os.path.join(BRIDGE, *parts)


def say(msg):
    print("%s %s" % (now(), msg), flush=True)


def terminal_result(job_id):
    path = bridge("results", job_id + ".json")
    try:
        return read_json(path).get("status") in TERMINAL
    except (OSError, ValueError, AttributeError):
        return False


def refuse_file(job_id, op, name, why):
    p = Progress({"jobId": job_id, "operation": op, "name": name}, bridge("results", job_id + ".json"))
    p.step("Validate", "Failed", why)
    p.finish("Refused", why)
    say("refused %s: %s" % (job_id, why))


def claim_next():
    """The oldest job, claimed (renamed into running/) — or None. Invalid jobs are answered Refused here."""
    try:
        names = [n for n in os.listdir(bridge("jobs")) if n.endswith(".json") and not n.startswith(".")]
    except OSError:
        return None
    names.sort(key=lambda n: (os.path.getmtime(bridge("jobs", n)) if os.path.exists(bridge("jobs", n)) else 0, n))
    for n in names:
        stem = n[:-5]
        src, dst = bridge("jobs", n), bridge("running", n)
        if not JOB_ID_RE.match(stem):
            safe = "invalid-" + re.sub(r"[^a-z0-9-]", "-", stem.lower())[:80].strip("-")
            try:
                os.remove(src)
            except OSError:
                continue
            refuse_file(safe, None, None, "job file %r: its name is not ^[a-z0-9-]{1,100}$.json" % n)
            continue
        try:
            os.replace(src, dst)
        except OSError:
            continue  # another claimer won, or the portal is still renaming
        if os.path.islink(dst) or not os.path.isfile(dst):
            os.remove(dst)
            refuse_file(stem, None, None, "the job is not a regular file (a symbolic link is never followed)")
            continue
        if terminal_result(stem):
            os.remove(dst)
            say("job %s already has a terminal result — not re-run" % stem)
            continue
        try:
            job = read_json(dst)
        except (OSError, ValueError) as e:
            os.remove(dst)
            refuse_file(stem, None, None, "the job file is not valid JSON (%s)" % e)
            continue
        try:
            check_job(job, stem)
        except Refused as e:
            os.remove(dst)
            refuse_file(stem, job.get("operation") if isinstance(job, dict) and job.get("operation") in OPERATIONS else None,
                        job.get("name") if isinstance(job, dict) and isinstance(job.get("name"), str) and NAME_RE.match(job.get("name") or "") else None, str(e))
            continue
        return stem
    return None


def start_child(job_id):
    say("running %s" % job_id)
    return subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "exec-job", job_id], stdin=subprocess.DEVNULL)


def finalize(job_id, code):
    running = bridge("running", job_id + ".json")
    if not terminal_result(job_id):
        try:
            job = read_json(running)
        except (OSError, ValueError):
            job = {"jobId": job_id}
        p = Progress(job, bridge("results", job_id + ".json"))
        try:
            p.result.update({k: v for k, v in read_json(bridge("results", job_id + ".json")).items() if k in ("steps", "log", "startedAt", "url", "actionsForOwner")})
        except (OSError, ValueError):
            pass
        p.finish("Failed", "the executor's job process exited (code %s) without a result" % code)
    try:
        os.remove(running)
    except OSError:
        pass
    say("finished %s (exit %s)" % (job_id, code))


def recover():
    """A job left in running/ by an agent that died mid-job: its process is gone, so it ends Failed — never re-run
    behind the requester's back."""
    try:
        names = [n for n in os.listdir(bridge("running")) if n.endswith(".json")]
    except OSError:
        return
    for n in names:
        stem = n[:-5]
        if JOB_ID_RE.match(stem):
            if terminal_result(stem):
                os.remove(bridge("running", n))
            else:
                finalize(stem, "lost — the executor restarted mid-job; re-submit")


def agent(once):
    for d in ("jobs", "running", "results"):
        os.makedirs(bridge(d), exist_ok=True)
        if os.path.islink(bridge(d)):
            say("%s is a symbolic link — the executor refuses to run on a bridge it does not own" % bridge(d))
            return 1
    if once:
        # every waiting job, one after the other, then one inventory
        recover()
        while True:
            jid = claim_next()
            if not jid:
                break
            child = start_child(jid)
            finalize(jid, child.wait())
        write_inventory()
        return 0
    me = os.path.abspath(__file__)
    stamp = os.path.getmtime(me)
    recover()
    child, child_id = None, None
    last_jobs = last_inv = 0.0
    while True:
        t = time.time()
        if child is not None and child.poll() is not None:
            finalize(child_id, child.returncode)
            child, child_id = None, None
            last_inv = 0.0
        if child is None and t - last_jobs >= 5:
            last_jobs = t
            jid = claim_next()
            if jid:
                child, child_id = start_child(jid), jid
        if t - last_inv >= 30:
            last_inv = t
            try:
                write_inventory()
            except Exception as e:  # noqa: BLE001 — the loop outlives a bad sample
                say("inventory not written: %s: %s" % (type(e).__name__, e))
        if child is None and (not os.path.exists(me) or os.path.getmtime(me) != stamp):
            say("the executor was upgraded — exiting so launchd starts the new one")
            return 0
        time.sleep(1)


def exec_job(job_id):
    if not JOB_ID_RE.match(job_id):
        return 2
    job = read_json(bridge("running", job_id + ".json"))
    p = Progress(job, bridge("results", job_id + ".json"))
    p.save()
    try:
        check_job(job, job_id)
    except Refused as e:
        p.finish("Refused", str(e))
        return 3
    code = run_job(job, p)
    try:
        os.remove(bridge("running", job_id + ".json"))
    except OSError:
        pass
    return code


# ── the command line ──────────────────────────────────────────────────────────────────────────────────────────
USAGE = """usage: memex-local instances <command>
  create <name> --host <name>.<machine>.<domain> --owner-id <id> --owner-email <e-mail>
         [--display-name <text>] [--cpu 2] [--memory-gi 6] [--storage-gi 20] [--package <Source>/<Package>]…
         [--chat-model <model>] [--no-embeddings]
  update <name> [--display-name …] [--cpu …] [--memory-gi …] [--package …]… [--chat-model …] [--embeddings|--no-embeddings]
  start <name> | stop <name> | remove <name> --confirm <name>
  list [--json]
  agent {on|off|status|run|run-once}
  renew-certs"""


def parse_flags(argv, allowed):
    spec, packages = {}, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a not in allowed:
            raise Refused("unknown flag %r" % a)
        if a in ("--no-embeddings", "--embeddings"):
            spec.setdefault("ai", {})["embeddings"] = a == "--embeddings"
            i += 1
            continue
        if i + 1 >= len(argv):
            raise Refused("%s needs a value" % a)
        v = argv[i + 1]
        i += 2
        if a == "--host":
            spec["hostName"] = v
        elif a == "--owner-id":
            spec.setdefault("owner", {})["id"] = v
        elif a == "--owner-email":
            spec.setdefault("owner", {})["email"] = v
        elif a == "--display-name":
            spec["displayName"] = v
        elif a in ("--cpu", "--memory-gi", "--storage-gi"):
            key = {"--cpu": "cpu", "--memory-gi": "memoryGi", "--storage-gi": "storageGi"}[a]
            try:
                spec.setdefault("resources", {})[key] = float(v) if "." in v else int(v)
            except ValueError:
                raise Refused("%s needs a number (got %r)" % (a, v))
        elif a == "--package":
            packages = (packages or []) + [v]
        elif a == "--chat-model":
            spec.setdefault("ai", {})["chatModel"] = v
        elif a == "--confirm":
            spec["confirm"] = v
    if packages is not None:
        spec["packages"] = packages
    return spec


def cli(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "exec-job" and rest:
        return exec_job(rest[0])
    if cmd == "agent-run":
        return agent(once=bool(rest and rest[0] == "--once"))
    if cmd == "inventory":
        if rest and rest[0] == "--write":
            write_inventory()
            return 0
        print(json.dumps(inventory(), indent=2, ensure_ascii=False))
        return 0
    if cmd == "list":
        inv = inventory()
        if rest == ["--json"]:
            print(json.dumps(inv, indent=2, ensure_ascii=False))
            return 0
        if rest:
            print(USAGE, file=sys.stderr)
            return 2
        cap = inv["capacity"] or {}
        print("machine %s  network %s  VM %s CPU / %s GiB, requested %s CPU / %s GiB" % (
            inv["machine"], inv["networkName"] or "-", cap.get("vmCpu", "?"), cap.get("vmMemoryGi", "?"),
            cap.get("requestedCpu", "?"), cap.get("requestedMemoryGi", "?")))
        if not inv["instances"]:
            print("no further instances on this Mac (memex-local instances create …)")
        for i in inv["instances"]:
            print("  %-24s %-9s %-5s %-11s %s  %s" % (i["name"], i["state"], i["ready"], i["health"], i["url"] or "-", i["healthDetail"]))
        return 0
    if cmd == "renew-certs":
        p = Progress()
        for n in instance_names():
            spec = load_spec(n)
            try:
                issue_certificate(n, spec["hostName"], p)
            except Failed as e:
                print("err  %s: %s" % (n, e), file=sys.stderr)
        return 0
    if cmd not in OPERATIONS:
        print(USAGE, file=sys.stderr)
        return 2
    if not rest or rest[0].startswith("-"):
        print("%s: the instance name comes first\n%s" % (cmd, USAGE), file=sys.stderr)
        return 2
    name, flags = rest[0], rest[1:]
    p = Progress()
    try:
        allowed = {
            "create": {"--host", "--owner-id", "--owner-email", "--display-name", "--cpu", "--memory-gi", "--storage-gi", "--package", "--chat-model", "--no-embeddings", "--embeddings"},
            "update": {"--display-name", "--cpu", "--memory-gi", "--storage-gi", "--package", "--chat-model", "--no-embeddings", "--embeddings"},
            "remove": {"--confirm"}, "start": set(), "stop": set(),
        }[cmd]
        spec = parse_flags(flags, allowed)
    except Refused as e:
        p.finish("Refused", str(e))
        return 3
    job = {"operation": cmd, "name": name, "spec": spec, "confirm": spec.pop("confirm", None)}
    return run_job(job, p)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
