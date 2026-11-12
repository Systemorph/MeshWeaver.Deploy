#!/usr/bin/env python3
"""Intune objects as code: reconcile the Mac Studio fleet's Intune objects against a DECLARED spec.

    intune-reconcile.py --check  --spec <fleet.spec.json>   read-only; one line per object, exit 2 on drift
    intune-reconcile.py --apply  --spec <fleet.spec.json>   create / patch / assign to match the spec
    intune-reconcile.py --sync   --spec <fleet.spec.json>   only: ask Intune to sync every Mac the spec assigns to
    intune-reconcile.py --validate --spec <file>            the spec's shape only, no Graph call
    intune-reconcile.py --self-test                         against an in-memory fake Graph

What the spec declares (deploy/macos/intune/README.md → "Intune objects as code"): the fleet's groups and the
Macs in them (by serial number), the bootstrap shell script (content pinned to a commit + sha256), the
`com.memex.local` custom profile and its keys, the trusted-root certificate of the fleet's local CA, the
night-window DDM update policy, the compliance policies the Studios must be excluded from / given, every
include and exclude assignment, and the application permissions the app identity must hold (verified only).

The rules the reconcile keeps:
  * --check never writes. Every line says what --apply WOULD do for it.
  * --apply never deletes and never removes: an assignment, a group member or an object the spec does not
    name is reported ("kept") and left alone. Assignments are written as the UNION of live and declared.
  * An object marked "hold" is reported as drift and never written, not even by --apply: the owner's go
    lifts the hold in the spec (a reviewed change), not a flag on the command line.
  * A secret is never printed: the token is used, never echoed; ids are shortened to 8 characters.
  * After an --apply that CHANGED a script, profile, certificate or settings policy, every Mac that object
    reaches (its include groups' members, minus its exclude groups') is asked to sync (managedDevices/{id}/
    syncDevice, one line per Mac): the Intune agent otherwise re-runs a CACHED script until its next full sync,
    up to 8 hours later (measured 2026-11-12). Needs DeviceManagementManagedDevices.PrivilegedOperations.All.

Exit codes: 0 in sync (PENDING lines allowed) · 2 drift (check) or drift left after apply (held) · 1 error.
Python standard library only.
"""

import argparse
import base64
import copy
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.microsoft.com"
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SPECIAL_TARGETS = {
    "allDevices": "#microsoft.graph.allDevicesAssignmentTarget",
    "allLicensedUsers": "#microsoft.graph.allLicensedUsersAssignmentTarget",
}
INCLUDE_T = "#microsoft.graph.groupAssignmentTarget"
EXCLUDE_T = "#microsoft.graph.exclusionGroupAssignmentTarget"
# The participation keys (MeshWeaver.Plugins onboarding contract §5). Lists are written as ONE comma-separated
# string, the form memex-local's --accept-jobs / --job-channels / --token-models already take.
PARTICIPATION_BOOL = ("AcceptJobs", "AcceptTokens")
PARTICIPATION_LIST = ("AcceptJobsFrom", "AcceptJobsChannels", "AcceptJobsRepos", "AcceptTokensFrom", "AcceptTokensModels")
PARTICIPATION_STR = ("AcceptTokensResidency",)
# Keys kept as a real plist ARRAY (never comma-joined: an SSH key's comment may hold a comma). The bootstrap reads
# OperatorSshKeys element by element (fleet-bootstrap.sh operator_ssh_keys).
ARRAY_KEYS = ("OperatorSshKeys",)
# One OpenSSH public key line, as the bootstrap accepts it (fleet-bootstrap.sh ssh_key_core): an accepted type, the
# base64 blob, an optional comment — no options prefix, no private key material.
SSH_PUBKEY = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|ssh-rsa) ([A-Za-z0-9+/]+={0,3})( [^\x00-\x1f\x7f]*)?\Z")


def ssh_pubkey_problem(line):
    """Why `line` is not one accepted OpenSSH public key, or None."""
    m = SSH_PUBKEY.match(line) if isinstance(line, str) else None
    if not m:
        return "not one single-line ssh-ed25519 / ecdsa-sha2-* / ssh-rsa public key without options"
    try:
        blob = base64.b64decode(m.group(2), validate=True)
    except Exception:  # noqa: BLE001 — malformed base64 is a refusal, not a crash
        return "its key blob is not base64"
    n = int.from_bytes(blob[:4], "big") if len(blob) >= 4 else -1
    if blob[4:4 + n].decode("latin-1") != m.group(1):
        return f"its key blob is not a {m.group(1)} key"
    return None


class GraphError(Exception):
    def __init__(self, status, method, path, message):
        super().__init__(f"{method} {path} → HTTP {status}: {message}")
        self.status = status


def short(i):
    return (i or "")[:8] + ("…" if i and len(i) > 8 else "")


# ── Graph ───────────────────────────────────────────────────────────────────────────────────────────────────
class Graph:
    """A minimal Graph client. The token never leaves this object and is never printed."""

    def __init__(self, token, base=GRAPH):
        self._token = token
        self.base = base

    def call(self, method, path, body=None):
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        if data is None and method == "POST":
            data = b""   # an action with no body (syncDevice): Graph answers 411 without an explicit Content-Length: 0
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._token)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        elif method == "POST":
            req.add_header("Content-Length", "0")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001 — the body is diagnostic only
                msg = ""
            raise GraphError(e.code, method, path.split("?")[0], " ".join(msg.split())[:200]) from None

    def get(self, path):
        return self.call("GET", path)

    def list(self, path):
        out, nxt = [], path
        while nxt:
            page = self.call("GET", nxt)
            out.extend(page.get("value", []))
            nxt = page.get("@odata.nextLink")
        return out

    def post(self, path, body):
        return self.call("POST", path, body)

    def patch(self, path, body):
        return self.call("PATCH", path, body)

    def put(self, path, body):
        return self.call("PUT", path, body)


def token_claims(token):
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:  # noqa: BLE001
        return {}


def acquire_token(tenant):
    """App credentials from the environment (secret or federated), else the Azure CLI. Never printed."""
    tid = os.environ.get("INTUNE_TENANT_ID") or tenant
    cid = os.environ.get("INTUNE_CLIENT_ID")
    form = None
    if cid and tid and os.environ.get("INTUNE_CLIENT_SECRET"):
        form = {"client_secret": os.environ["INTUNE_CLIENT_SECRET"]}
    elif cid and tid and os.environ.get("AZURE_FEDERATED_TOKEN_FILE"):
        with open(os.environ["AZURE_FEDERATED_TOKEN_FILE"]) as f:
            form = {"client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": f.read().strip()}
    if form is not None:
        form.update({"client_id": cid, "scope": f"{GRAPH}/.default", "grant_type": "client_credentials"})
        req = urllib.request.Request(f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token",
                                     data=urllib.parse.urlencode(form).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["access_token"], "app credentials from the environment"
    cmd = ["az", "account", "get-access-token", "--resource", GRAPH, "--query", "accessToken", "-o", "tsv"]
    if tid:
        cmd += ["--tenant", tid]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit("no Graph credential: set INTUNE_CLIENT_ID + INTUNE_TENANT_ID with INTUNE_CLIENT_SECRET or "
                         "AZURE_FEDERATED_TOKEN_FILE, or sign in with `az login` (" + r.stderr.strip()[:160] + ")")
    return r.stdout.strip(), "the Azure CLI sign-in"


def fetch_source(src):
    url = f"https://raw.githubusercontent.com/{src['repo']}/{src['commit']}/{src['path']}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


# ── the spec ────────────────────────────────────────────────────────────────────────────────────────────────
def validate(spec, spec_dir):
    """Every problem in the spec's SHAPE, as a list of strings (empty = valid)."""
    p = []
    groups = spec.get("groups") or {}
    if not isinstance(groups, dict) or not groups:
        p.append("groups: declare at least one group")
    for k, g in groups.items():
        if not g.get("displayName"):
            p.append(f"groups.{k}: displayName is required")
        if g.get("id") and not GUID.match(g["id"]):
            p.append(f"groups.{k}: id is not a GUID")
        for d in g.get("devices", []):
            if not d.get("serial") or not d.get("name"):
                p.append(f"groups.{k}.devices: every device names serial and name")

    def targets(where, a):
        for kind in ("include", "exclude"):
            for t in (a or {}).get(kind, []):
                if t in SPECIAL_TARGETS:
                    if kind == "exclude":
                        p.append(f"{where}: '{t}' cannot be an exclusion")
                elif t not in groups:
                    p.append(f"{where}: assignment names an undeclared group '{t}'")
        for t in (a or {}).get("remove", []):
            if t not in SPECIAL_TARGETS and t not in groups:
                p.append(f"{where}: assign.remove names an undeclared group '{t}'")
            if t in (a or {}).get("include", []):
                p.append(f"{where}: '{t}' is both included and removed")
        if not (a or {}).get("include"):
            p.append(f"{where}: assign.include is empty (an unassigned object reaches no Mac)")

    for s in spec.get("scripts", []):
        w = f"scripts.{s.get('key')}"
        src = s.get("source") or {}
        for f in ("key", "displayName", "fileName", "runAsAccount", "executionFrequency"):
            if not s.get(f):
                p.append(f"{w}: {f} is required")
        if not HEX40.match(src.get("commit", "")):
            p.append(f"{w}: source.commit must be a full 40-hex commit (a pin, never a branch)")
        if not HEX64.match(src.get("sha256", "")):
            p.append(f"{w}: source.sha256 must be 64 lower-hex")
        if not src.get("repo") or not src.get("path"):
            p.append(f"{w}: source.repo and source.path are required")
        targets(w, s.get("assign"))
    for c in spec.get("customProfiles", []):
        w = f"customProfiles.{c.get('key')}"
        pl = c.get("plist") or {}
        for f in ("payloadType", "identifier", "uuid", "innerIdentifier", "innerUuid", "displayName"):
            if not pl.get(f):
                p.append(f"{w}: plist.{f} is required")
        keys = c.get("keys") or {}
        if pl.get("payloadType") == "com.memex.local":
            for k in ("ControlUrl", "RegistryUrl", "LoginServer", "OwnerId", "OwnerEmail"):
                if not keys.get(k):
                    p.append(f"{w}: keys.{k} is required for com.memex.local")
            for k in PARTICIPATION_BOOL:
                if not isinstance(keys.get(k), bool):
                    p.append(f"{w}: keys.{k} must be an explicit true/false (the bootstrap fails loudly without it)")
            for k in PARTICIPATION_LIST:
                if k in keys and not (isinstance(keys[k], list) and all(isinstance(x, str) and x and "," not in x for x in keys[k])):
                    p.append(f"{w}: keys.{k} must be a list of non-empty strings without commas")
            if keys.get("AcceptJobs") is True and not keys.get("AcceptJobsFrom"):
                p.append(f"{w}: AcceptJobs is true but AcceptJobsFrom names no instance")
            if keys.get("AcceptTokens") is True and not keys.get("AcceptTokensFrom"):
                p.append(f"{w}: AcceptTokens is true but AcceptTokensFrom names no instance")
            if keys.get("AcceptTokens") is True and not keys.get("AcceptTokensModels"):
                p.append(f"{w}: AcceptTokens is true but AcceptTokensModels names no model (the bootstrap refuses that)")
            cas = [t.get("sha256") for t in spec.get("trustedRootCertificates", []) if t.get("sha256")]
            if keys.get("FleetCaSha256") and keys["FleetCaSha256"] not in cas:
                p.append(f"{w}: keys.FleetCaSha256 names no declared trustedRootCertificates sha256 (memex-local ca adopt "
                         "would look for a certificate the profile never delivers)")
            if cas and not keys.get("FleetCaSha256"):
                p.append(f"{w}: a fleet CA is declared but keys.FleetCaSha256 is absent (memex-local ca adopt reads it)")
            if "AcceptTokensResidency" in keys and not isinstance(keys["AcceptTokensResidency"], str):
                p.append(f"{w}: keys.AcceptTokensResidency must be a string")
            if "OperatorSshKeys" in keys:
                ks = keys["OperatorSshKeys"]
                if not isinstance(ks, list) or not ks:
                    p.append(f"{w}: keys.OperatorSshKeys must be a non-empty list of OpenSSH public keys (omit it to leave SSH alone)")
                else:
                    for i, k in enumerate(ks):
                        why = ssh_pubkey_problem(k)
                        if why:
                            p.append(f"{w}: keys.OperatorSshKeys[{i}] is refused — {why} (the bootstrap refuses it too)")
        targets(w, c.get("assign"))
    for t in spec.get("trustedRootCertificates", []):
        w = f"trustedRootCertificates.{t.get('key')}"
        if not t.get("certFile") or not t.get("displayName"):
            p.append(f"{w}: certFile and displayName are required")
        if t.get("sha256") and not HEX64.match(t["sha256"]):
            p.append(f"{w}: sha256 must be 64 lower-hex (or empty until the CA is minted)")
        targets(w, t.get("assign"))
    for s in spec.get("settingsPolicies", []):
        w = f"settingsPolicies.{s.get('key')}"
        cr = s.get("create")
        if cr is not None:
            cgroups = cr.get("groups") if isinstance(cr, dict) else None   # never `groups`: targets() reads the spec's
            if not isinstance(cgroups, dict) or not cgroups or not all(isinstance(v, list) and v for v in cgroups.values()):
                p.append(f"settingsPolicies.{s.get('key')}: create.groups must map each group definition id to its child setting ids")
            else:
                for sid in {x for v in cgroups.values() for x in v}:
                    if sid not in (s.get("settings") or {}):
                        p.append(f"settingsPolicies.{s.get('key')}: create.groups names {sid}, which settings does not declare")
        if not s.get("name") or not isinstance(s.get("settings"), dict):
            p.append(f"{w}: name and a settings map are required")
        targets(w, s.get("assign"))
    for c in spec.get("compliancePolicies", []):
        w = f"compliancePolicies.{c.get('key')}"
        if not c.get("displayName"):
            p.append(f"{w}: displayName is required")
        if not c.get("id") and not c.get("properties"):
            p.append(f"{w}: an undeclared-id policy needs properties to create it from")
        targets(w, c.get("assign"))
    app = spec.get("appIdentity") or {}
    if not isinstance(app.get("requiredRoles"), list) or not app.get("requiredRoles"):
        p.append("appIdentity.requiredRoles: declare the Graph application permissions the reconcile needs")
    return p


def build_plist(c):
    """The custom profile's mobileconfig, built from the spec. Lists become ONE comma-separated string, except the
    ARRAY_KEYS, which stay a plist array."""
    pl = c["plist"]
    inner = {"PayloadType": pl["payloadType"], "PayloadVersion": 1, "PayloadIdentifier": pl["innerIdentifier"],
             "PayloadUUID": pl["innerUuid"], "PayloadDisplayName": pl["displayName"]}
    for k, v in c.get("keys", {}).items():
        inner[k] = ",".join(v) if isinstance(v, list) and k not in ARRAY_KEYS else v
    return {"PayloadContent": [inner], "PayloadDisplayName": pl["displayName"], "PayloadIdentifier": pl["identifier"],
            "PayloadOrganization": pl.get("organization", ""), "PayloadRemovalDisallowed": False,
            "PayloadScope": "System", "PayloadType": "Configuration", "PayloadUUID": pl["uuid"], "PayloadVersion": 1}


def cert_der(path):
    raw = open(path, "rb").read()
    if b"-----BEGIN CERTIFICATE-----" in raw:
        body = raw.split(b"-----BEGIN CERTIFICATE-----")[1].split(b"-----END CERTIFICATE-----")[0]
        return base64.b64decode(b"".join(body.split()))
    return raw


def flatten_settings(o, out=None):
    """settingDefinitionId → value for every simple and choice setting in a settings-catalog tree."""
    out = {} if out is None else out
    if isinstance(o, dict):
        if "settingDefinitionId" in o:
            if isinstance(o.get("simpleSettingValue"), dict):
                out[o["settingDefinitionId"]] = o["simpleSettingValue"].get("value")
            elif isinstance(o.get("choiceSettingValue"), dict):
                out[o["settingDefinitionId"]] = o["choiceSettingValue"].get("value")
        for v in o.values():
            flatten_settings(v, out)
    elif isinstance(o, list):
        for v in o:
            flatten_settings(v, out)
    return out


def flatten_settings_values(o):
    """Every simple/choice value in a tree, in order (the test reads a value's TYPE, which a flat dict compare hides)."""
    return list(flatten_settings(o).values())


def rewrite_settings(o, want):
    if isinstance(o, dict):
        d = o.get("settingDefinitionId")
        if d in want:
            if isinstance(o.get("simpleSettingValue"), dict):
                o["simpleSettingValue"]["value"] = want[d]
            elif isinstance(o.get("choiceSettingValue"), dict):
                o["choiceSettingValue"]["value"] = want[d]
        for v in o.values():
            rewrite_settings(v, want)
    elif isinstance(o, list):
        for v in o:
            rewrite_settings(v, want)


def strip_ids(o):
    if isinstance(o, dict):
        return {k: strip_ids(v) for k, v in o.items() if k not in ("id", "@odata.context") and not k.endswith("@odata.context")}
    if isinstance(o, list):
        return [strip_ids(v) for v in o]
    return o


# ── the reconcile ───────────────────────────────────────────────────────────────────────────────────────────
class Reconcile:
    def __init__(self, spec, spec_dir, graph, apply=False, fetch=fetch_source, token=None, out=print, sync_only=False):
        self.spec, self.dir, self.g, self.apply, self.fetch, self.out = spec, spec_dir, graph, apply, fetch, out
        self.token = token
        self.sync_only = sync_only
        self.drift = self.held = self.errors = self.pending = 0
        self.gid = {}
        self.changed = []   # the delivered objects (script / profile / certificate / policy) an --apply changed
        self.new_members = []   # (name, managed device id) of Macs this --apply added to a group

    # one line per finding ─────────────────────────────────────────────────────────────────────────────────
    def line(self, status, what, detail, action=""):
        self.out(f"{status:<8}{what}: {detail}" + (f" → apply: {action}" if action else ""))

    def finding(self, obj, what, detail, action, do):
        """A drift. In --apply, `do()` makes it so — unless the object is held."""
        hold = obj.get("hold") if isinstance(obj, dict) else None
        if hold:
            self.held += 1
            self.line("HELD", what, detail, f"held, nothing written ({hold}); would: {action}")
            return
        if not self.apply:
            self.drift += 1
            self.line("DRIFT", what, detail, action)
            return
        try:
            do()
            self.line("APPLIED", what, detail, action)
            if self.delivered(obj) and not any(obj is c for c in self.changed):
                self.changed.append(obj)
        except GraphError as e:
            self.errors += 1
            self.line("ERROR", what, f"{detail}; {action} failed: {e}")

    # assignments ──────────────────────────────────────────────────────────────────────────────────────────
    def desired_targets(self, assign):
        want = set()
        for t in (assign or {}).get("include", []):
            want.add((SPECIAL_TARGETS[t], None) if t in SPECIAL_TARGETS else (INCLUDE_T, self.gid.get(t)))
        for t in (assign or {}).get("exclude", []):
            want.add((EXCLUDE_T, self.gid.get(t)))
        return want

    @staticmethod
    def live_targets(assignments):
        return {(a["target"]["@odata.type"], a["target"].get("groupId")) for a in assignments}

    def label(self, t):
        kind, gid = t
        name = next((k for k, v in self.gid.items() if v and v == gid), short(gid))
        return {INCLUDE_T: f"include {name}", EXCLUDE_T: f"exclude {name}"}.get(kind, kind.split(".")[-1].replace("AssignmentTarget", ""))

    def removal_targets(self, assign):
        out = set()
        for t in (assign or {}).get("remove", []):
            out.add((SPECIAL_TARGETS[t], None) if t in SPECIAL_TARGETS else (INCLUDE_T, self.gid.get(t)))
        return out

    def reconcile_assignments(self, obj, what, live, assign_path, body_key, odata_item=None):
        """Writes the UNION of live and declared targets. The only target ever dropped is one the spec names under
        `assign.remove` (a declared removal, reviewed like any other line). A kept live target is written back
        verbatim, assignment filter included."""
        want = self.desired_targets(obj.get("assign"))
        drop = self.removal_targets(obj.get("assign"))
        if any(g is None and k in (INCLUDE_T, EXCLUDE_T) for k, g in want):
            self.line("PENDING", what, "assignments wait for a group that does not exist yet")
            self.pending += 1
            return
        have = self.live_targets(live)
        missing, extra, removing = want - have, have - want - drop, have & drop
        if extra:
            self.line("INFO", what, "assignment not in the spec, kept: " + ", ".join(sorted(self.label(t) for t in extra)))
        if not missing and not removing:
            self.line("OK", what, "assignments " + ", ".join(sorted(self.label(t) for t in want)))
            return
        items = []
        for a in live:
            if (a["target"]["@odata.type"], a["target"].get("groupId")) in drop:
                continue
            item = {"target": {k: v for k, v in a["target"].items() if v is not None}}
            if odata_item:
                item["@odata.type"] = odata_item
            items.append(item)
        for kind, gid in sorted(missing, key=lambda t: (t[0], t[1] or "")):
            target = {"@odata.type": kind}
            if gid:
                target["groupId"] = gid
            item = {"target": target}
            if odata_item:
                item["@odata.type"] = odata_item
            items.append(item)
        detail = "; ".join(x for x in (
            ("missing assignment " + ", ".join(sorted(self.label(t) for t in missing))) if missing else "",
            ("declared removal of " + ", ".join(sorted(self.label(t) for t in removing))) if removing else "") if x)
        self.finding(obj, what, detail,
                     f"POST {assign_path} with the live targets" + (" minus the declared removal" if removing else "")
                     + (" plus the missing ones" if missing else ""),
                     lambda: self.g.post(assign_path, {body_key: items}))

    # groups and devices ──────────────────────────────────────────────────────────────────────────────────
    def groups(self):
        for key, g in self.spec["groups"].items():
            what = f"group {key} \"{g['displayName']}\""
            live = None
            if g.get("id"):
                try:
                    live = self.g.get(f"v1.0/groups/{g['id']}?$select=id,displayName,groupTypes,securityEnabled")
                except GraphError as e:
                    if e.status != 404:
                        raise
            if live is None:
                found = self.g.list("v1.0/groups?$filter=" + urllib.parse.quote(f"displayName eq '{g['displayName']}'")
                                    + "&$select=id,displayName,groupTypes")
                if found:
                    self.gid[key] = found[0]["id"]
                    self.name_match(what, g, found)
                    continue
                body = {"displayName": g["displayName"], "description": g.get("description", ""), "mailEnabled": False,
                        "securityEnabled": True, "mailNickname": re.sub(r"[^A-Za-z0-9]", "", g["displayName"])[:56] or key}
                self.finding(g, what, "absent", "POST v1.0/groups (an assigned security group; the app identity "
                             "needs Group.Create for this — else a person creates it)",
                             lambda: self.gid.__setitem__(key, self.g.post("v1.0/groups", body)["id"]))
                continue
            self.gid[key] = live["id"]
            if "DynamicMembership" in (live.get("groupTypes") or []):
                self.errors += 1
                self.line("ERROR", what, "is a DYNAMIC group; the spec declares an assigned group with declared devices")
            elif live.get("displayName") != g["displayName"]:
                self.finding(g, what, f"displayName live \"{live.get('displayName')}\"",
                             f"PATCH v1.0/groups/{short(live['id'])} displayName",
                             lambda: self.g.patch(f"v1.0/groups/{live['id']}", {"displayName": g["displayName"]}))
            else:
                self.line("OK", what, short(live["id"]))

    def devices(self):
        for key, g in self.spec["groups"].items():
            if not g.get("devices") and not g.get("reportMembers"):
                continue
            gid = self.gid.get(key)
            what_g = f"members of {key}"
            if not gid:
                self.pending += 1
                self.line("PENDING", what_g, "the group does not exist yet")
                continue
            members = {m["id"]: m for m in self.g.list(f"v1.0/groups/{gid}/members?$select=id,displayName,deviceId")}
            declared = set()
            for d in g.get("devices", []):
                what = f"device {d['name']} in {key}"
                serial = d["serial"].replace("'", "")
                md = self.g.list("v1.0/deviceManagement/managedDevices?$filter="
                                 + urllib.parse.quote(f"serialNumber eq '{serial}'")
                                 + "&$select=id,deviceName,serialNumber,azureADDeviceId,operatingSystem")
                md = [m for m in md if (m.get("operatingSystem") or "macOS") == "macOS"]
                if not md:
                    self.pending += 1
                    self.line("PENDING", what, "not enrolled in Intune yet (no managed device with this serial)")
                    continue
                if len(md) > 1:
                    self.errors += 1
                    self.line("ERROR", what, f"{len(md)} managed devices carry this serial; retire the stale one first")
                    continue
                aad = md[0].get("azureADDeviceId")
                if not aad or aad == "00000000-0000-0000-0000-000000000000":
                    self.pending += 1
                    self.line("PENDING", what, "enrolled, but not registered in the directory yet")
                    continue
                dev = self.g.list("v1.0/devices?$filter=" + urllib.parse.quote(f"deviceId eq '{aad}'") + "&$select=id,displayName,deviceId")
                if not dev:
                    self.pending += 1
                    self.line("PENDING", what, f"directory device {short(aad)} not readable yet")
                    continue
                oid = dev[0]["id"]
                declared.add(oid)
                if oid in members:
                    self.line("OK", what, f"member (managed {short(md[0]['id'])}, directory {short(oid)})")
                    continue
                def add(oid=oid, mid=md[0]["id"], name=d.get("name") or d["serial"]):
                    self.g.post(f"v1.0/groups/{gid}/members/$ref", {"@odata.id": f"{GRAPH}/v1.0/directoryObjects/{oid}"})
                    # a Mac that just joined the group gets its first bootstrap NOW, not at the agent's next check-in
                    self.new_members.append((name, mid))
                self.finding(g, what, "not a member",
                             f"POST v1.0/groups/{short(gid)}/members/$ref directory object {short(oid)}", add)
            for mid, m in members.items():
                if mid not in declared:
                    self.line("INFO", what_g, f"member {m.get('displayName') or short(mid)} is not declared, kept")

    # scripts ─────────────────────────────────────────────────────────────────────────────────────────────
    def scripts(self):
        for s in self.spec.get("scripts", []):
            what = f"script {s['key']} \"{s['displayName']}\""
            meta = {k: s[k] for k in ("displayName", "description", "fileName", "runAsAccount", "executionFrequency",
                                      "retryCount", "blockExecutionNotifications") if k in s}
            live = self.find("beta/deviceManagement/deviceShellScripts", s, "displayName")
            if live is None:
                def create():
                    content = self.source(s)
                    body = dict(meta, **{"@odata.type": "#microsoft.graph.deviceShellScript",
                                         "scriptContent": base64.b64encode(content).decode()})
                    s["_live"] = self.g.post("beta/deviceManagement/deviceShellScripts", body)
                self.finding(s, what, "absent", "POST beta/deviceManagement/deviceShellScripts (content from "
                             f"{s['source']['repo']}@{s['source']['commit'][:12]})", create)
                live = s.get("_live")
                if live is None:
                    continue
            else:
                # The script's assignments are only readable EXPANDED (…/assignments has no GET route — measured).
                full = self.g.get(f"beta/deviceManagement/deviceShellScripts/{live['id']}?$expand=assignments")
                live["assignments"] = full.get("assignments", [])
                live_sha = hashlib.sha256(base64.b64decode(full.get("scriptContent") or "")).hexdigest()
                diffs = [f"{k} live={full.get(k)!r}" for k, v in meta.items() if full.get(k) != v]
                if live_sha != s["source"]["sha256"]:
                    diffs.append(f"content sha256 live={live_sha[:12]} spec={s['source']['sha256'][:12]}")
                if diffs:
                    def patch(full=full, live_sha=live_sha):
                        body = dict(meta, **{"@odata.type": "#microsoft.graph.deviceShellScript"})
                        if live_sha != s["source"]["sha256"]:
                            body["scriptContent"] = base64.b64encode(self.source(s)).decode()
                        self.g.patch(f"beta/deviceManagement/deviceShellScripts/{full['id']}", body)
                    self.finding(s, what, "; ".join(diffs), f"PATCH deviceShellScripts/{short(live['id'])}"
                                 + (f" with the content of {s['source']['repo']}@{s['source']['commit'][:12]}" if live_sha != s["source"]["sha256"] else ""),
                                 patch)
                else:
                    self.line("OK", what, f"{short(live['id'])}, content {live_sha[:12]} = {s['source']['path']}@{s['source']['commit'][:12]}")
            self.reconcile_assignments(s, what, live.get("assignments", []), f"beta/deviceManagement/deviceShellScripts/{live['id']}/assign",
                                       "deviceManagementScriptAssignments")

    def source(self, s):
        content = self.fetch(s["source"])
        got = hashlib.sha256(content).hexdigest()
        if got != s["source"]["sha256"]:
            raise GraphError(0, "FETCH", s["source"]["path"], f"the pinned source hashes to {got[:12]}, the spec says "
                             f"{s['source']['sha256'][:12]} — refusing to upload it")
        return content

    def name_match(self, what, obj, found):
        """Found by exact name. With no id declared that is the match (INFO: record it); a declared id that does
        not exist is a wrong spec (DRIFT); two objects of one name are ambiguous (ERROR, the first is not used)."""
        if len(found) > 1:
            self.errors += 1
            self.line("ERROR", what, f"{len(found)} objects carry this name; declare the id of the right one")
        elif obj.get("id"):
            self.drift += 1
            self.line("DRIFT", what, f"the spec's id {short(obj['id'])} does not exist; one of this name is "
                      f"{short(found[0]['id'])}", "nothing — correct the id in the spec")
        else:
            self.line("INFO", what, f"matched by name as {short(found[0]['id'])}; record the id in the spec")

    def find(self, collection, obj, name_field):
        """The live object by its declared id, else by exact name (reported: record the id)."""
        if obj.get("id"):
            try:
                return self.g.get(f"{collection}/{obj['id']}")
            except GraphError as e:
                if e.status != 404:
                    raise
        name = obj.get(name_field) or obj.get("displayName") or obj.get("name")
        found = [x for x in self.g.list(collection) if x.get(name_field) == name]
        if found:
            self.name_match(f"{collection.split('/')[-1]} \"{name}\"", obj, found)
            return found[0]
        return None

    # custom profiles ──────────────────────────────────────────────────────────────────────────────────────
    def custom_profiles(self):
        for c in self.spec.get("customProfiles", []):
            what = f"profile {c['key']} \"{c['displayName']}\""
            want_plist = build_plist(c)
            payload = base64.b64encode(plistlib.dumps(want_plist)).decode()
            meta = {k: c[k] for k in ("displayName", "description", "payloadName", "payloadFileName", "deploymentChannel") if k in c}
            body = dict(meta, **{"@odata.type": "#microsoft.graph.macOSCustomConfiguration", "payload": payload})
            live = self.find("beta/deviceManagement/deviceConfigurations", c, "displayName")
            if live is None:
                self.finding(c, what, "absent", "POST beta/deviceManagement/deviceConfigurations (macOSCustomConfiguration)",
                             lambda: c.__setitem__("_live", self.g.post("beta/deviceManagement/deviceConfigurations", body)))
                live = c.get("_live")
                if live is None:
                    continue
            else:
                diffs = [f"{k} differs" for k, v in meta.items() if live.get(k) != v]
                try:
                    live_plist = plistlib.loads(base64.b64decode(live.get("payload") or ""))
                except Exception:  # noqa: BLE001 — an unreadable payload is drift, not a crash
                    live_plist = {}
                li = (live_plist.get("PayloadContent") or [{}])[0]
                wi = want_plist["PayloadContent"][0]
                for k in sorted(set(li) | set(wi)):
                    if li.get(k) != wi.get(k):
                        diffs.append(f"key {k}: live={li.get(k)!r} spec={wi.get(k)!r}" if k in li else f"key {k} missing")
                outer = {k: v for k, v in want_plist.items() if k != "PayloadContent"}
                for k, v in outer.items():
                    if live_plist.get(k) != v:
                        diffs.append(f"profile {k} differs")
                if diffs:
                    self.finding(c, what, "; ".join(diffs), f"PATCH deviceConfigurations/{short(live['id'])} with the payload built from the spec",
                                 lambda live=live: self.g.patch(f"beta/deviceManagement/deviceConfigurations/{live['id']}", body))
                else:
                    self.line("OK", what, f"{short(live['id'])}, {len(wi) - 5} keys as declared")
            self.reconcile_assignments(c, what, self.g.list(f"beta/deviceManagement/deviceConfigurations/{live['id']}/assignments"),
                                       f"beta/deviceManagement/deviceConfigurations/{live['id']}/assign", "assignments",
                                       "#microsoft.graph.deviceConfigurationAssignment")

    # trusted root certificate ────────────────────────────────────────────────────────────────────────────
    def trusted_roots(self):
        for t in self.spec.get("trustedRootCertificates", []):
            what = f"certificate {t['key']} \"{t['displayName']}\""
            path = os.path.join(self.dir, t["certFile"])
            if not t.get("sha256") or not os.path.exists(path):
                self.pending += 1
                self.line("PENDING", what, f"CA not minted yet ({t['certFile']} absent or no sha256 declared)", "nothing")
                continue
            der = cert_der(path)
            sha = hashlib.sha256(der).hexdigest()
            if sha != t["sha256"]:
                self.errors += 1
                self.line("ERROR", what, f"{t['certFile']} hashes to {sha[:12]}, the spec says {t['sha256'][:12]}")
                continue
            body = {"@odata.type": "#microsoft.graph.macOSTrustedRootCertificate", "displayName": t["displayName"],
                    "description": t.get("description", ""), "certFileName": os.path.basename(t["certFile"]),
                    "trustedRootCertificate": base64.b64encode(der).decode()}
            live = self.find("beta/deviceManagement/deviceConfigurations", t, "displayName")
            if live is None:
                self.finding(t, what, "absent", "POST beta/deviceManagement/deviceConfigurations (macOSTrustedRootCertificate)",
                             lambda: t.__setitem__("_live", self.g.post("beta/deviceManagement/deviceConfigurations", body)))
                live = t.get("_live")
                if live is None:
                    continue
            else:
                live_sha = hashlib.sha256(base64.b64decode(live.get("trustedRootCertificate") or "")).hexdigest()
                if live_sha != sha:
                    self.finding(t, what, f"certificate live={live_sha[:12]} spec={sha[:12]}",
                                 f"PATCH deviceConfigurations/{short(live['id'])}",
                                 lambda live=live: self.g.patch(f"beta/deviceManagement/deviceConfigurations/{live['id']}", body))
                else:
                    self.line("OK", what, f"{short(live['id'])}, certificate {sha[:12]}")
            self.reconcile_assignments(t, what, self.g.list(f"beta/deviceManagement/deviceConfigurations/{live['id']}/assignments"),
                                       f"beta/deviceManagement/deviceConfigurations/{live['id']}/assign", "assignments",
                                       "#microsoft.graph.deviceConfigurationAssignment")

    # settings-catalog policies (the DDM update policy) ───────────────────────────────────────────────────
    def settings_policies(self):
        for s in self.spec.get("settingsPolicies", []):
            what = f"settings policy {s['key']} \"{s['name']}\""
            live = self.find("beta/deviceManagement/configurationPolicies", s, "name")
            if live is None and s.get("create"):
                def create(s=s):
                    body = {"name": s["name"], "description": s.get("description", ""),
                            "platforms": s["create"].get("platforms", "macOS"),
                            "technologies": s["create"].get("technologies", "mdm,appleRemoteManagement"),
                            "settings": self.build_settings(s)}
                    s["_live"] = self.g.post("beta/deviceManagement/configurationPolicies", body)
                self.finding(s, what, "absent", "POST beta/deviceManagement/configurationPolicies (the settings tree built "
                             "from the declared values and each setting's own definition)", create)
                live = s.get("_live")
                if live is None:
                    continue
                self.reconcile_assignments(s, what, [], f"beta/deviceManagement/configurationPolicies/{live['id']}/assign", "assignments")
                continue
            if live is None:
                self.errors += 1
                self.line("ERROR", what, "absent; a settings-catalog policy is created in the admin center (its "
                          "settings tree is not generated here — declare `create` to have it built), then its id is recorded in the spec")
                continue
            full = self.g.get(f"beta/deviceManagement/configurationPolicies/{live['id']}?$expand=settings")
            flat = flatten_settings(full.get("settings", []))
            # compared as text: Graph returns an integer setting as 0 where a spec may say "0" (no drift forever)
            diffs = [f"{k}: live={flat.get(k)!r} spec={v!r}" for k, v in s["settings"].items() if str(flat.get(k)) != str(v)]
            if diffs:
                def put(full=full):
                    body = strip_ids({k: full[k] for k in ("name", "description", "platforms", "technologies",
                                                           "roleScopeTagIds", "templateReference", "settings") if k in full})
                    rewrite_settings(body["settings"], s["settings"])
                    self.g.put(f"beta/deviceManagement/configurationPolicies/{full['id']}", body)
                self.finding(s, what, "; ".join(diffs), f"PUT configurationPolicies/{short(live['id'])} with the live tree, "
                             "the declared values rewritten", put)
            else:
                self.line("OK", what, f"{short(live['id'])}, " + ", ".join(f"{k.split('_')[-1]}={v}" for k, v in s["settings"].items()))
            self.reconcile_assignments(s, what, self.g.list(f"beta/deviceManagement/configurationPolicies/{live['id']}/assignments"),
                                       f"beta/deviceManagement/configurationPolicies/{live['id']}/assign", "assignments")

    def build_settings(self, s):
        """The settings-catalog tree for a declared policy: one group-collection instance per `create.groups` entry
        (group definition id → its child setting ids), each child built from its OWN definition read from Graph — a
        choice setting carries the declared option id, a simple one an integer or string value as the definition says."""
        out = []
        for group, children in s["create"]["groups"].items():
            # every declared child must be one the GROUP's own definition lists as a direct child — a setting that lives
            # under a choice option instead would be posted in the wrong place, so it is refused by name (review on #281)
            gdef = self.g.get(f"beta/deviceManagement/configurationSettings/{urllib.parse.quote(group)}")
            allowed = set(gdef.get("childIds") or [])
            wrong = [c for c in children if allowed and c not in allowed]
            if wrong:
                raise GraphError(400, "GET", f"configurationSettings/{group}",
                                 "not direct children of this group (declare them under their parent): " + ", ".join(wrong))
            kids = []
            for sid in children:
                d = self.g.get(f"beta/deviceManagement/configurationSettings/{urllib.parse.quote(sid)}")
                kind = (d.get("@odata.type") or "").rsplit(".", 1)[-1]
                v = s["settings"][sid]
                tmpl = {"settingDefinitionId": sid}
                if kind == "deviceManagementConfigurationChoiceSettingDefinition":
                    kids.append({"@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingInstance", **tmpl,
                                 "choiceSettingValue": {"value": v, "children": []}})
                elif kind == "deviceManagementConfigurationSimpleSettingDefinition":
                    integer = "Integer" in ((d.get("valueDefinition") or {}).get("@odata.type") or "")
                    try:
                        value = int(v) if integer else str(v)
                    except (TypeError, ValueError):
                        # an ERROR line naming the setting, never a traceback (review on #281)
                        raise GraphError(400, "GET", f"configurationSettings/{sid}", f"declared value {v!r} is not an integer") from None
                    kids.append({"@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingInstance", **tmpl,
                                 "simpleSettingValue": {"@odata.type": "#microsoft.graph.deviceManagementConfiguration"
                                                        + ("IntegerSettingValue" if integer else "StringSettingValue"),
                                                        "value": value}})
                else:
                    raise GraphError(400, "GET", f"configurationSettings/{sid}", f"cannot build a {kind or 'unknown'} setting")
            out.append({"@odata.type": "#microsoft.graph.deviceManagementConfigurationSetting",
                        "settingInstance": {"@odata.type": "#microsoft.graph.deviceManagementConfigurationGroupSettingCollectionInstance",
                                            "settingDefinitionId": group, "groupSettingCollectionValue": [{"children": kids}]}})
        return out

    # compliance policies ─────────────────────────────────────────────────────────────────────────────────
    def compliance(self):
        for c in self.spec.get("compliancePolicies", []):
            what = f"compliance {c['key']} \"{c['displayName']}\""
            live = self.find("beta/deviceManagement/deviceCompliancePolicies", c, "displayName")
            if live is None:
                body = dict(c["properties"], displayName=c["displayName"], description=c.get("description", ""))
                body["scheduledActionsForRule"] = [{"ruleName": "PasswordRequired", "scheduledActionConfigurations": [
                    {"actionType": "block", "gracePeriodHours": 0, "notificationTemplateId": "", "notificationMessageCCList": []}]}]
                self.finding(c, what, "absent", "POST beta/deviceManagement/deviceCompliancePolicies "
                             + ", ".join(f"{k}={v}" for k, v in c["properties"].items() if k != "@odata.type"),
                             lambda: c.__setitem__("_live", self.g.post("beta/deviceManagement/deviceCompliancePolicies", body)))
                live = c.get("_live")
                if live is None:
                    continue
            else:
                props = {k: v for k, v in (c.get("properties") or {}).items() if k != "@odata.type"}
                diffs = [f"{k}: live={live.get(k)!r} spec={v!r}" for k, v in props.items() if live.get(k) != v]
                if diffs:
                    self.finding(c, what, "; ".join(diffs), f"PATCH deviceCompliancePolicies/{short(live['id'])}",
                                 lambda live=live: self.g.patch(f"beta/deviceManagement/deviceCompliancePolicies/{live['id']}",
                                                                dict(props, **{"@odata.type": live.get("@odata.type")})))
                else:
                    self.line("OK", what, short(live["id"]) + (", " + ", ".join(f"{k}={v}" for k, v in props.items()) if props else ""))
            self.reconcile_assignments(c, what, self.g.list(f"beta/deviceManagement/deviceCompliancePolicies/{live['id']}/assignments"),
                                       f"beta/deviceManagement/deviceCompliancePolicies/{live['id']}/assign", "assignments")

    # the app identity (verify only) ──────────────────────────────────────────────────────────────────────
    def app_identity(self):
        app = self.spec.get("appIdentity") or {}
        what = f"app identity \"{app.get('displayName', '?')}\""
        claims = token_claims(self.token or "")
        need = set(app.get("requiredRoles", []))
        if "roles" in claims or claims.get("idtyp") == "app":
            have = set(claims.get("roles", []))
            if app.get("clientId") and claims.get("appid") not in (None, app["clientId"]):
                # A read-only drift identity (the scheduled check's) cannot vouch for the apply identity's grants.
                self.line("INFO", what, f"signed in as a different application ({short(claims.get('appid'))}, roles: "
                          + ", ".join(sorted(claims.get("roles", []))) + "); the apply identity's permissions are not verifiable from it")
                return
            miss = sorted(need - have)
            if miss:
                self.drift += 1
                self.line("DRIFT", what, "missing application permissions: " + ", ".join(miss),
                          "nothing — an admin-consented grant is the owner's act")
            else:
                self.line("OK", what, f"holds {len(need)} declared permissions")
        else:
            self.line("INFO", what, "a delegated (user) token: application permissions are not verifiable from it; "
                      "scopes: " + (claims.get("scp") or "none"))

    # device sync ──────────────────────────────────────────────────────────────────────────────────────────
    def delivered(self, obj):
        """An object Intune DELIVERS to the Macs (whose change a Mac sees only at its next sync)."""
        return any(obj is x for k in ("scripts", "customProfiles", "trustedRootCertificates", "settingsPolicies")
                   for x in self.spec.get(k, []))

    def group_devices(self, key):
        """{directory object id: (name, Entra deviceId)} of a declared group's members."""
        gid = self.gid.get(key)
        if not gid:
            return {}
        return {m["id"]: (m.get("displayName") or short(m["id"]), m.get("deviceId"))
                for m in self.g.list(f"v1.0/groups/{gid}/members?$select=id,displayName,deviceId")}

    def sync_new_members(self):
        """Every Mac this apply ADDED to a group is asked to sync at once (its first bootstrap, its profiles) — unless the
        changed-object sync above already asked it."""
        done = {w for w in getattr(self, "_synced", set())}
        for name, mid in self.new_members:
            if mid in done:
                continue
            try:
                self.g.post(f"v1.0/deviceManagement/managedDevices/{mid}/syncDevice", None)
                self.line("SYNCED", f"device sync {name}", f"managed {short(mid)} asked to sync (newly added to a group)")
                done.add(mid)
            except GraphError as e:
                self.errors += 1
                self.line("ERROR", f"device sync {name}", f"syncDevice failed: {e}")

    def sync_devices(self, objs, why):
        """Ask Intune to sync every Mac the objects reach: include groups' members minus that object's exclude
        groups' members. One line per Mac; a failure is an ERROR naming the Mac, never a silent skip."""
        cache, reach = {}, {}
        def members(key):
            if key not in cache:
                cache[key] = self.group_devices(key) if key in self.spec.get("groups", {}) else {}
            return cache[key]
        for o in objs:
            a = o.get("assign") or {}
            out = set().union(*[set(members(k)) for k in a.get("exclude", [])]) if a.get("exclude") else set()
            for k in a.get("include", []):
                for oid, v in members(k).items():
                    if oid not in out:
                        reach[oid] = v
        if not reach:
            self.line("INFO", "device sync", f"{why}: no Mac to sync")
            return
        for oid, (name, aad) in sorted(reach.items(), key=lambda kv: kv[1][0]):
            what = f"device sync {name}"
            if not aad:
                # a user or service principal in the group: no device to sync (review on #278)
                self.line("INFO", what, f"directory object {short(oid)} is not a device — nothing to sync")
                continue
            md = [m for m in self.g.list("v1.0/deviceManagement/managedDevices?$filter="
                                         + urllib.parse.quote(f"azureADDeviceId eq '{aad}'") + "&$select=id,deviceName,operatingSystem")
                  if (m.get("operatingSystem") or "macOS") == "macOS"]   # the fleet's Macs only, as the device step
            if len(md) != 1:
                self.pending += 1
                self.line("PENDING", what, f"{len(md)} managed macOS devices for directory device {short(oid)} — not synced")
                continue
            try:
                self.g.post(f"v1.0/deviceManagement/managedDevices/{md[0]['id']}/syncDevice", None)
                self.line("SYNCED", what, f"managed {short(md[0]['id'])} asked to sync ({why})")
                self._synced = getattr(self, "_synced", set()) | {md[0]["id"]}
            except GraphError as e:
                self.errors += 1
                hint = " (needs DeviceManagementManagedDevices.PrivilegedOperations.All)" if e.status in (401, 403) else ""
                self.line("ERROR", what, f"syncDevice failed: {e}{hint}")

    def run(self):
        steps = [("app identity", self.app_identity), ("groups", self.groups), ("devices", self.devices),
                 ("scripts", self.scripts), ("profiles", self.custom_profiles), ("certificates", self.trusted_roots),
                 ("settings policies", self.settings_policies), ("compliance", self.compliance)]
        if self.sync_only:
            steps = [("groups", self.groups)]
        for name, step in steps:
            try:
                step()
            except GraphError as e:
                self.errors += 1
                hint = " (the credential lacks a permission — see appIdentity.requiredRoles)" if e.status in (401, 403) else ""
                self.line("ERROR", name, f"{e}{hint}")
        try:
            if self.sync_only:
                # a HELD object is not touched at all — not even by a device action on its behalf (review on #278)
                self.sync_devices([x for k in ("scripts", "customProfiles", "trustedRootCertificates", "settingsPolicies")
                                   for x in self.spec.get(k, []) if not x.get("hold")], "--sync")
            elif self.apply and self.changed:
                self.sync_devices(self.changed, "changed by this apply: " + ", ".join(o.get("key", "?") for o in self.changed))
            if self.apply and not self.sync_only:
                self.sync_new_members()
        except GraphError as e:
            self.errors += 1
            self.line("ERROR", "device sync", str(e))
        mode = "sync" if self.sync_only else "apply" if self.apply else "check"
        self.out(f"── {mode}: drift {self.drift}, held {self.held}, pending {self.pending}, errors {self.errors}")
        if self.errors:
            return 1
        return 2 if (self.drift or self.held) else 0


# ── self-test: an in-memory Graph ───────────────────────────────────────────────────────────────────────────
class FakeGraph:
    """Answers the routes the reconcile uses, from a dict. `writes` records every mutation."""

    def __init__(self, state):
        self.s = state
        self.writes = []
        self.n = 0

    def _new(self):
        self.n += 1
        return f"00000000-0000-4000-8000-{self.n:012d}"

    @staticmethod
    def _filter(items, path):
        q = urllib.parse.unquote(path.split("$filter=", 1)[1].split("&")[0]) if "$filter=" in path else ""
        m = re.match(r"(\w+) eq '([^']*)'", q)
        return [x for x in items if x.get(m.group(1)) == m.group(2)] if m else items

    def _coll(self, path):
        p = path.split("?")[0]
        for prefix, key in (("v1.0/groups", "groups"), ("v1.0/devices", "devices"),
                            ("v1.0/deviceManagement/managedDevices", "managedDevices"),
                            ("beta/deviceManagement/deviceShellScripts", "scripts"),
                            ("beta/deviceManagement/deviceConfigurations", "configs"),
                            ("beta/deviceManagement/configurationPolicies", "policies"),
                            ("beta/deviceManagement/configurationSettings", "settingDefs"),
                            ("beta/deviceManagement/deviceCompliancePolicies", "compliance")):
            if p == prefix or p.startswith(prefix + "/"):
                return key, p[len(prefix):].strip("/").split("/") if p != prefix else []
        raise GraphError(400, "?", p, "route not faked")

    def get(self, path):
        key, rest = self._coll(path)
        obj = self.s[key].get(rest[0])
        if obj is None:
            raise GraphError(404, "GET", path, "not found")
        out = copy.deepcopy(obj)
        if "expand=assignments" in path:
            out["assignments"] = out.get("_assign", [])
        return out

    def list(self, path):
        key, rest = self._coll(path)
        if not rest:
            return self._filter(copy.deepcopy(list(self.s[key].values())), path)
        obj = self.s[key].get(rest[0])
        if obj is None:
            raise GraphError(404, "GET", path, "not found")
        if rest[1] == "members":
            return [copy.deepcopy(self.s["devices"][m]) for m in obj.get("_members", [])]
        if rest[1] == "assignments":
            return copy.deepcopy(obj.get("_assign", []))
        raise GraphError(400, "GET", path, "route not faked")

    def post(self, path, body):
        self.writes.append(("POST", path.split("?")[0], body))
        key, rest = self._coll(path)
        if not rest:
            i = self._new()
            self.s[key][i] = dict(body, id=i)
            return {"id": i, **body}
        obj = self.s[key][rest[0]]
        if rest[1] == "assign":
            items = next(iter(body.values()))
            obj["_assign"] = [{"target": it["target"]} for it in items]
            return {}
        if rest[1:] == ["members", "$ref"]:
            obj.setdefault("_members", []).append(body["@odata.id"].rsplit("/", 1)[1])
            return {}
        if key == "managedDevices" and rest[1:] == ["syncDevice"]:
            if body is not None:
                raise GraphError(400, "POST", path, "syncDevice takes no body")
            obj["_synced"] = obj.get("_synced", 0) + 1
            return {}
        raise GraphError(400, "POST", path, "route not faked")

    def patch(self, path, body):
        self.writes.append(("PATCH", path, body))
        key, rest = self._coll(path)
        self.s[key][rest[0]].update({k: v for k, v in body.items() if k != "@odata.type"})
        return {}

    def put(self, path, body):
        self.writes.append(("PUT", path, body))
        key, rest = self._coll(path)
        self.s[key][rest[0]].update(body)
        return {}


def self_test():
    script = b"#!/bin/bash\necho fleet\n"
    sha = hashlib.sha256(script).hexdigest()
    G1, G2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    spec = {
        "groups": {"studios": {"id": G1, "displayName": "Fleet — Mac Studios",
                               "devices": [{"serial": "SERIAL0001", "name": "studio-1"}, {"serial": "SERIAL0009", "name": "studio-9"}]},
                   "noBootstrap": {"id": G2, "displayName": "Fleet — Mac Studios, no bootstrap"}},
        "appIdentity": {"displayName": "fleet app", "requiredRoles": ["Device.Read.All"]},
        "scripts": [{"key": "bootstrap", "displayName": "Fleet — bootstrap", "description": "d", "fileName": "fleet-bootstrap.sh",
                     "runAsAccount": "system", "executionFrequency": "PT1H", "retryCount": 3, "blockExecutionNotifications": True,
                     "source": {"repo": "acme/deploy", "path": "fleet-bootstrap.sh", "commit": "a" * 40, "sha256": sha},
                     "assign": {"include": ["studios"], "exclude": ["noBootstrap"]}}],
        "customProfiles": [{"key": "memexLocal", "displayName": "Fleet — Memex Local", "description": "d", "payloadName": "p",
                            "payloadFileName": "f.mobileconfig", "deploymentChannel": "deviceChannel",
                            "plist": {"payloadType": "com.memex.local", "identifier": "com.example.fleet", "uuid": "U1",
                                      "innerIdentifier": "com.example.fleet.settings", "innerUuid": "U2", "displayName": "Memex Local",
                                      "organization": "Example"},
                            "keys": {"ControlUrl": "https://control.example.com", "RegistryUrl": "https://registry.example.com",
                                     "LoginServer": "https://headscale.example.com", "OwnerId": "jroe", "OwnerEmail": "jane.roe@example.com",
                                     "AcceptJobs": True, "AcceptJobsFrom": ["control.example.com"], "AcceptJobsChannels": ["mac"],
                                     "AcceptTokens": False},
                            "assign": {"include": ["studios"], "exclude": ["noBootstrap"]}}],
        "trustedRootCertificates": [{"key": "fleetCa", "displayName": "Fleet — local CA", "certFile": "absent-ca.pem", "sha256": "",
                                     "assign": {"include": ["studios"]}}],
        "settingsPolicies": [{"key": "updates", "id": "33333333-3333-4333-8333-333333333333", "name": "Fleet — updates",
                              "settings": {"enforcement_targetosversion": "27.2"}, "assign": {"include": ["studios"]}}],
        "compliancePolicies": [{"key": "basic", "id": "44444444-4444-4444-8444-444444444444", "displayName": "Basic",
                                "assign": {"include": ["allLicensedUsers", "allDevices"], "exclude": ["studios"]},
                                "hold": "the owner's go"}],
    }
    assert not validate(spec, "."), validate(spec, ".")
    upd_tree = [{"settingInstance": {"settingDefinitionId": "enforcement_enforcement", "groupSettingCollectionValue": [{"children": [
        {"settingDefinitionId": "enforcement_targetosversion", "simpleSettingValue": {"value": "27.1"}}]}]}}]

    def tenant():
        return {"groups": {G1: {"id": G1, "displayName": "Fleet — Mac Studios", "groupTypes": [], "_members": []},
                           G2: {"id": G2, "displayName": "Fleet — Mac Studios, no bootstrap", "groupTypes": []}},
                "devices": {"d1": {"id": "d1", "displayName": "studio-1", "deviceId": "aad-1"},
                            "dx": {"id": "dx", "displayName": "hand-added", "deviceId": "aad-x"}},
                "managedDevices": {"m1": {"id": "m1", "serialNumber": "SERIAL0001", "azureADDeviceId": "aad-1", "operatingSystem": "macOS"}},
                "scripts": {}, "configs": {},
                "policies": {"33333333-3333-4333-8333-333333333333": {"id": "33333333-3333-4333-8333-333333333333", "name": "Fleet — updates",
                                                                      "settings": copy.deepcopy(upd_tree), "_assign": [{"target": {"@odata.type": INCLUDE_T, "groupId": G1}}]}},
                "compliance": {"44444444-4444-4444-8444-444444444444": {
                    "id": "44444444-4444-4444-8444-444444444444", "displayName": "Basic", "@odata.type": "#microsoft.graph.macOSCompliancePolicy",
                    "_assign": [{"target": {"@odata.type": SPECIAL_TARGETS["allLicensedUsers"]}}, {"target": {"@odata.type": SPECIAL_TARGETS["allDevices"]}}]}}}

    tok = "x." + base64.urlsafe_b64encode(json.dumps({"idtyp": "app", "roles": ["Device.Read.All"]}).encode()).decode().rstrip("=") + ".y"
    fails = []

    def run(state, apply, sp=spec):
        g, lines = FakeGraph(state), []
        rc = Reconcile(copy.deepcopy(sp), tempfile.gettempdir(), g, apply=apply, fetch=lambda s: script, token=tok, out=lines.append).run()
        return rc, lines, g

    def expect(name, cond, lines=()):
        print(("  ok    " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)
            for ln in lines:
                print("        " + ln)

    st = tenant()
    rc, lines, g = run(st, False)
    expect("check on a fresh tenant reports drift, exit 2, and writes nothing", rc == 2 and not g.writes, lines)
    expect("an absent script / profile is reported with what apply would do",
           any(ln.startswith("DRIFT") and "script bootstrap" in ln and "POST" in ln for ln in lines)
           and any(ln.startswith("DRIFT") and "profile memexLocal" in ln for ln in lines), lines)
    expect("the CA that is not minted yet is PENDING, never drift", any("PENDING" in ln and "CA not minted yet" in ln for ln in lines), lines)
    expect("a declared Mac not enrolled yet is PENDING", any("PENDING" in ln and "studio-9" in ln and "not enrolled" in ln for ln in lines), lines)

    rc, lines, g = run(st, True)
    posts = [w for w in g.writes if w[0] == "POST"]
    expect("apply CREATES the script with the pinned content and the profile",
           any(w[1] == "beta/deviceManagement/deviceShellScripts" and base64.b64decode(w[2]["scriptContent"]) == script for w in posts)
           and any(w[1] == "beta/deviceManagement/deviceConfigurations" for w in posts), lines)
    sid = next(iter(st["scripts"]))
    expect("apply assigns the script to the include group AND the exclusion group",
           {(a["target"]["@odata.type"], a["target"].get("groupId")) for a in st["scripts"][sid]["_assign"]} == {(INCLUDE_T, G1), (EXCLUDE_T, G2)}, lines)
    expect("device membership: the enrolled Mac is resolved (serial → managed device → directory object) and added",
           "d1" in st["groups"][G1]["_members"], lines)
    expect("the declared update-policy value is rewritten in the live tree (PUT)",
           flatten_settings(st["policies"]["33333333-3333-4333-8333-333333333333"]["settings"]).get("enforcement_targetosversion") == "27.2", lines)
    expect("a HELD exclusion is reported and never written", any(ln.startswith("HELD") and "compliance basic" in ln and "exclude studios" in ln for ln in lines)
           and not any("deviceCompliancePolicies" in w[1] for w in g.writes) and rc == 2, lines)

    syncs = [w for w in g.writes if w[1].endswith("/syncDevice")]
    expect("device sync: an apply that changed delivered objects asks every Mac they reach to sync, ONE call each, no body",
           [w[1] for w in syncs] == ["v1.0/deviceManagement/managedDevices/m1/syncDevice"] and syncs[0][2] is None
           and any(ln.startswith("SYNCED") and "studio-1" in ln for ln in lines), lines)
    st["groups"][G1]["_members"].append("dx")
    rc, lines, g = run(st, False)
    expect("device sync: --check never syncs", not any(w[1].endswith("/syncDevice") for w in g.writes)
           and not any(ln.startswith("SYNCED") for ln in lines), lines)
    rc, lines, g = run(st, True, {**spec, "compliancePolicies": []})
    expect("device sync: an apply that changed nothing syncs nothing", not g.writes and not any(ln.startswith("SYNCED") for ln in lines), lines)
    st["managedDevices"]["mx"] = {"id": "mx", "serialNumber": "SERIALX", "azureADDeviceId": "aad-x", "operatingSystem": "macOS"}
    st["groups"][G2]["_members"] = ["dx"]
    lines = []
    g = FakeGraph(st)
    rc = Reconcile(copy.deepcopy(spec), tempfile.gettempdir(), g, token=tok, out=lines.append, sync_only=True).run()
    synced = sorted(w[1].split("/")[3] for w in g.writes if w[1].endswith("/syncDevice"))
    expect("device sync: --sync syncs only, writes nothing else; a Mac in a policy's include group is synced even when "
           "another object excludes it (the update policy reaches it), and each Mac is asked ONCE",
           rc == 0 and synced == ["m1", "mx"] and all(w[1].endswith("/syncDevice") for w in g.writes), lines)
    lines = []
    g = FakeGraph(st)
    r = Reconcile(copy.deepcopy(spec), tempfile.gettempdir(), g, apply=True, token=tok, out=lines.append)
    r.groups()
    r.sync_devices([r.spec["scripts"][0]], "test")
    synced = [w[1].split("/")[3] for w in g.writes if w[1].endswith("/syncDevice")]
    expect("device sync: a Mac in the object's EXCLUDE group is not synced for it", synced == ["m1"], lines)
    del st["managedDevices"]["mx"]
    lines = []
    g = FakeGraph(st)
    r = Reconcile(copy.deepcopy(spec), tempfile.gettempdir(), g, apply=True, token=tok, out=lines.append)
    r.groups()
    r.sync_devices([r.spec["settingsPolicies"][0]], "test")
    expect("device sync: a member with no managed device is PENDING, named, never a silent skip",
           r.pending == 1 and any(ln.startswith("PENDING") and "hand-added" in ln for ln in lines), lines)
    st2s = copy.deepcopy(st)
    st2s["managedDevices"]["mw"] = {"id": "mw", "serialNumber": "W", "azureADDeviceId": "aad-x", "operatingSystem": "Windows"}
    st2s["devices"]["u1"] = {"id": "u1", "displayName": "a person"}
    st2s["groups"][G1]["_members"].append("u1")
    lines = []
    g = FakeGraph(st2s)
    r = Reconcile(copy.deepcopy(spec), tempfile.gettempdir(), g, apply=True, token=tok, out=lines.append)
    r.groups()
    r.sync_devices([r.spec["settingsPolicies"][0]], "test")
    synced = [w[1].split("/")[3] for w in g.writes if w[1].endswith("/syncDevice")]
    expect("device sync: a non-macOS managed device is never synced, and a non-device member is INFO, not PENDING",
           synced == ["m1"] and any(ln.startswith("INFO") and "not a device" in ln for ln in lines) and r.pending == 1, lines)
    held = copy.deepcopy(spec)
    held["settingsPolicies"][0]["hold"] = "the owner's go"
    held["scripts"] = []; held["customProfiles"] = []; held["trustedRootCertificates"] = []
    lines = []
    g = FakeGraph(copy.deepcopy(st))
    Reconcile(held, tempfile.gettempdir(), g, token=tok, out=lines.append, sync_only=True).run()
    expect("device sync: --sync never acts for a HELD object", not any(w[1].endswith("/syncDevice") for w in g.writes), lines)
    # the real client: an empty POST carries Content-Length: 0 (Graph answers 411 without it), and no Content-Type
    seen = {}
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""
    real = urllib.request.urlopen
    def _open(req, timeout=60):
        seen.update(method=req.get_method(), data=req.data, headers={k.lower(): v for k, v in req.header_items()})
        return _R()
    urllib.request.urlopen = _open
    try:
        Graph("t").post("v1.0/deviceManagement/managedDevices/x/syncDevice", None)
    finally:
        urllib.request.urlopen = real
    # what is observable here is the Request the client builds (urllib's handler adds its own form Content-Type later,
    # harmless to Graph — the 411 is about Content-Length): an explicit Content-Length: 0, an empty body, no JSON type
    expect("Graph: an action POST with no body is built with an empty body and an explicit Content-Length: 0 (no JSON type)",
           seen.get("method") == "POST" and seen.get("data") == b"" and seen["headers"].get("content-length") == "0"
           and seen["headers"].get("content-type") != "application/json", [repr(seen)])
    rc, lines, g = run(st, False)
    expect("no-op: a reconciled tenant checks clean apart from the held policy (writes nothing)",
           rc == 2 and not g.writes and sum(ln.startswith("DRIFT") for ln in lines) == 0 and sum(ln.startswith("HELD") for ln in lines) == 1, lines)
    expect("an undeclared group member is reported and kept", any(ln.startswith("INFO") and "hand-added" in ln and "kept" in ln for ln in lines), lines)
    no_hold = copy.deepcopy(spec)
    no_hold["compliancePolicies"][0].pop("hold")
    rc, lines, g = run(copy.deepcopy(st), False, no_hold)
    expect("no-op without the hold reads exactly one drift (the exclusion)", rc == 2 and sum(ln.startswith("DRIFT") for ln in lines) == 1, lines)
    rc, lines, g = run(st, False, {**spec, "compliancePolicies": []})
    expect("no-op: everything else in sync → exit 0, no write", rc == 0 and not g.writes, lines)

    cid = next(k for k, v in st["configs"].items() if v.get("payload"))
    p = plistlib.loads(base64.b64decode(st["configs"][cid]["payload"]))
    p["PayloadContent"][0]["ControlUrl"] = "https://elsewhere.example.com"
    st["configs"][cid]["payload"] = base64.b64encode(plistlib.dumps(p)).decode()
    st["scripts"][sid]["scriptContent"] = base64.b64encode(b"#!/bin/bash\necho stale\n").decode()
    rc, lines, g = run(st, False, {**spec, "compliancePolicies": []})
    expect("drift: a changed profile key and stale script content are each named", rc == 2
           and any("key ControlUrl" in ln and "elsewhere" in ln for ln in lines) and any("content sha256" in ln for ln in lines), lines)
    rc, lines, g = run(st, True, {**spec, "compliancePolicies": []})
    rc2, lines2, g2 = run(st, False, {**spec, "compliancePolicies": []})
    expect("drift: apply patches both, and the next check is clean", rc == 0 and rc2 == 0 and not g2.writes, lines + lines2)

    st2 = copy.deepcopy(st)
    pol = st2["compliance"]["44444444-4444-4444-8444-444444444444"]
    rc, lines, g = run(st2, True, no_hold)
    got = {(a["target"]["@odata.type"], a["target"].get("groupId")) for a in pol["_assign"]}
    expect("exclusion assignment: apply (no hold) adds the exclusion and KEEPS all-users and all-devices",
           got == {(SPECIAL_TARGETS["allLicensedUsers"], None), (SPECIAL_TARGETS["allDevices"], None), (EXCLUDE_T, G1)}, lines)
    rm = copy.deepcopy(no_hold)
    rm["compliancePolicies"][0]["assign"] = {"include": ["allDevices"], "exclude": ["studios"], "remove": ["allLicensedUsers"]}
    st4 = copy.deepcopy(st2)
    rc, lines, g = run(st4, False, rm)
    expect("a declared removal is drift in check and named as such", rc == 2 and any("declared removal of allLicensedUsers" in ln for ln in lines), lines)
    run(st4, True, rm)
    got = {(a["target"]["@odata.type"], a["target"].get("groupId")) for a in st4["compliance"]["44444444-4444-4444-8444-444444444444"]["_assign"]}
    expect("apply drops ONLY the declared removal (device-targeted policy, device-group exclusion effective)",
           got == {(SPECIAL_TARGETS["allDevices"], None), (EXCLUDE_T, G1)}, lines)
    pol["_assign"].append({"target": {"@odata.type": INCLUDE_T, "groupId": "99999999-9999-4999-8999-999999999999"}})
    rc, lines, g = run(st2, False, no_hold)
    expect("an assignment the spec does not name is reported as kept, not drift", rc == 0
           and any(ln.startswith("INFO") and "kept" in ln and "compliance basic" in ln for ln in lines), lines)

    bad = copy.deepcopy(spec)
    del bad["customProfiles"][0]["keys"]["AcceptTokens"]
    bad["scripts"][0]["source"]["commit"] = "main"
    bad["scripts"][0]["assign"]["exclude"] = ["nowhere"]
    v = validate(bad, ".")
    expect("validate: a missing participation answer, a branch instead of a pin and an undeclared group are each refused",
           any("AcceptTokens" in x for x in v) and any("40-hex" in x for x in v) and any("nowhere" in x for x in v), v)
    ca = copy.deepcopy(spec)
    ca["trustedRootCertificates"][0]["sha256"] = "f" * 64
    v = validate(ca, ".")
    expect("validate: a declared CA without FleetCaSha256 in the profile is refused", any("FleetCaSha256 is absent" in x for x in v), v)
    ca["customProfiles"][0]["keys"]["FleetCaSha256"] = "e" * 64
    v = validate(ca, ".")
    expect("validate: a FleetCaSha256 that names no declared CA is refused", any("names no declared" in x for x in v), v)
    ca["customProfiles"][0]["keys"]["FleetCaSha256"] = "f" * 64
    expect("validate: the profile's FleetCaSha256 equal to the declared CA passes", not validate(ca, "."), validate(ca, "."))
    tok_on = copy.deepcopy(spec)
    tok_on["customProfiles"][0]["keys"].update(AcceptTokens=True, AcceptTokensFrom=["*.example.com"])
    v = validate(tok_on, ".")
    expect("validate: tokens on without models is refused, as the bootstrap refuses it", any("AcceptTokensModels" in x for x in v), v)
    tok_on["customProfiles"][0]["keys"]["AcceptTokensModels"] = ["model-a"]
    expect("validate: tokens on with models passes", not validate(tok_on, "."), validate(tok_on, "."))
    op = "ssh-ed25519 " + base64.b64encode(len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + bytes(32)).decode() + " operator, laptop"
    ssh = copy.deepcopy(spec)
    ssh["customProfiles"][0]["keys"]["OperatorSshKeys"] = [op]
    expect("validate: OperatorSshKeys with one public key (a comma in its comment) passes", not validate(ssh, "."), validate(ssh, "."))
    inner = build_plist(ssh["customProfiles"][0])["PayloadContent"][0]
    expect("OperatorSshKeys stays a plist ARRAY in the profile (never comma-joined), the participation lists still a string",
           inner["OperatorSshKeys"] == [op] and inner["AcceptJobsFrom"] == "control.example.com", [repr(inner)])
    for label, k in (("an options prefix", 'from="10.0.0.1" ' + op), ("private key material", "-----BEGIN OPENSSH PRIVATE KEY-----"),
                     ("an ed25519 blob labelled ssh-rsa", "ssh-rsa " + op.split(" ")[1]), ("two lines", op + "\n" + op)):
        bad_ssh = copy.deepcopy(spec)
        bad_ssh["customProfiles"][0]["keys"]["OperatorSshKeys"] = [k]
        v = validate(bad_ssh, ".")
        expect(f"validate: an OperatorSshKeys entry with {label} is refused", any("OperatorSshKeys[0] is refused" in x for x in v), v)
    bad_ssh["customProfiles"][0]["keys"]["OperatorSshKeys"] = op
    v = validate(bad_ssh, ".")
    expect("validate: OperatorSshKeys as a bare string is refused (a list, or omitted)", any("must be a non-empty list" in x for x in v), v)

    wrong = copy.deepcopy(spec)
    wrong["scripts"][0]["source"]["sha256"] = "0" * 64
    st3 = tenant()
    rc, lines, g = run(st3, True, wrong)
    expect("a pinned source whose bytes do not match the declared sha256 is never uploaded",
           not st3["scripts"] and any(ln.startswith("ERROR") and "refusing to upload" in ln for ln in lines) and rc == 1, lines)

    other = "x." + base64.urlsafe_b64encode(json.dumps({"idtyp": "app", "appid": "read-only-app", "roles": ["Group.Read.All"]}).encode()).decode().rstrip("=") + ".y"
    lines = []
    r = Reconcile({**spec, "appIdentity": {**spec["appIdentity"], "clientId": "apply-app"}}, ".", FakeGraph(tenant()), token=other, out=lines.append)
    r.app_identity()
    expect("a read-only drift identity is named as a different application, never counted as missing grants",
           r.drift == 0 and any(ln.startswith("INFO") and "not verifiable" in ln for ln in lines), lines)
    lines = []
    r = Reconcile(spec, ".", FakeGraph(tenant()), token="x." + base64.urlsafe_b64encode(b'{"idtyp":"app","roles":[]}').decode().rstrip("=") + ".y", out=lines.append)
    r.app_identity()
    expect("the apply identity without a declared permission is drift", r.drift == 1 and any("Device.Read.All" in ln for ln in lines), lines)

    # 2c: a Mac this apply ADDS to a group is asked to sync at once, even when nothing it receives changed
    st5 = tenant()
    for k in ("scripts", "configs"):
        st5[k] = copy.deepcopy(st[k])
    st5["groups"][G1]["_members"] = []
    st5["policies"] = copy.deepcopy(st["policies"])
    rc, lines, g = run(st5, True, {**spec, "compliancePolicies": []})
    syn = [ln for ln in lines if ln.startswith("SYNCED")]
    expect("device sync: a Mac newly ADDED to a group is asked to sync at once (its first bootstrap), once",
           "d1" in st5["groups"][G1]["_members"] and len(syn) == 1 and "newly added" in syn[0]
           and sum(1 for w in g.writes if w[1].endswith("/syncDevice")) == 1, lines)
    # 2d: a settings-catalog policy declared with `create` is BUILT from its settings' own definitions and posted
    fv = copy.deepcopy(spec)
    fv["settingsPolicies"].append({"key": "fileVault", "name": "Fleet — FileVault", "description": "d",
        "settings": {"com.apple.mcx.filevault2_enable": "com.apple.mcx.filevault2_enable_0",
                     "com.apple.mcx.filevault2_deferforceatuserloginmaxbypassattempts": "0",
                     "com.apple.security.fderecoverykeyescrow_location": "Intune"},
        "create": {"platforms": "macOS", "technologies": "mdm,appleRemoteManagement",
                   "groups": {"com.apple.mcx.filevault2_com.apple.mcx.filevault2": ["com.apple.mcx.filevault2_enable",
                                                                                    "com.apple.mcx.filevault2_deferforceatuserloginmaxbypassattempts"],
                              "com.apple.security.fderecoverykeyescrow_com.apple.security.fderecoverykeyescrow": ["com.apple.security.fderecoverykeyescrow_location"]}},
        "assign": {"include": ["studios"]}})
    expect("validate: a declared create shape passes", not validate(fv, "."), validate(fv, "."))
    st6 = copy.deepcopy(st5)
    st6["settingDefs"] = {
        "com.apple.mcx.filevault2_enable": {"@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingDefinition"},
        "com.apple.mcx.filevault2_deferforceatuserloginmaxbypassattempts": {"@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingDefinition",
            "valueDefinition": {"@odata.type": "#microsoft.graph.deviceManagementConfigurationIntegerSettingValueDefinition"}},
        "com.apple.security.fderecoverykeyescrow_location": {"@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingDefinition",
            "valueDefinition": {"@odata.type": "#microsoft.graph.deviceManagementConfigurationStringSettingValueDefinition"}},
        # the groups, as this tenant's Graph defines them: the declared settings are their DIRECT children (childIds)
        "com.apple.mcx.filevault2_com.apple.mcx.filevault2": {"childIds": ["com.apple.mcx.filevault2_enable",
                                                                           "com.apple.mcx.filevault2_deferforceatuserloginmaxbypassattempts"]},
        "com.apple.security.fderecoverykeyescrow_com.apple.security.fderecoverykeyescrow": {"childIds": ["com.apple.security.fderecoverykeyescrow_location"]}}
    rc, lines, g = run(st6, True, {**fv, "compliancePolicies": []})
    rc2, lines2, g2 = run(st6, False, {**fv, "compliancePolicies": []})
    expect("create: the next --check of the created policy is clean (an integer 0 matches a declared \"0\")",
           not any(ln.startswith("DRIFT") and "FileVault" in ln for ln in lines2), lines2)
    made = [x for x in st6["policies"].values() if x.get("name") == "Fleet — FileVault"]
    flat = flatten_settings(made[0].get("settings", [])) if made else {}
    vals = [x for x in flatten_settings_values(made[0]["settings"])] if made else []
    expect("create: an absent declared policy is POSTed with the tree built from each setting's definition (choice id, "
           "integer, string), assigned, and its Macs synced",
           len(made) == 1 and flat == fv["settingsPolicies"][-1]["settings"] | {"com.apple.mcx.filevault2_deferforceatuserloginmaxbypassattempts": 0}
           and 0 in vals and made[0].get("_assign") and any(ln.startswith("SYNCED") for ln in lines), lines + [repr(made)])
    st7 = copy.deepcopy(st6)
    st7["policies"] = {k: v for k, v in st7["policies"].items() if v.get("name") != "Fleet — FileVault"}
    st7["settingDefs"]["com.apple.mcx.filevault2_com.apple.mcx.filevault2"] = {"childIds": ["com.apple.mcx.filevault2_enable"]}
    rc, lines, g = run(st7, True, {**fv, "compliancePolicies": []})
    expect("create: a declared child the group's definition does not list is refused by NAME (ERROR, nothing posted)",
           rc == 1 and any(ln.startswith("ERROR") and "deferforceatuserloginmaxbypassattempts" in ln for ln in lines)
           and not any(x.get("name") == "Fleet — FileVault" for x in st7["policies"].values()), lines)
    st8 = copy.deepcopy(st6)
    st8["policies"] = {k: v for k, v in st8["policies"].items() if v.get("name") != "Fleet — FileVault"}
    nan = copy.deepcopy(fv)
    nan["settingsPolicies"][-1]["settings"]["com.apple.mcx.filevault2_deferforceatuserloginmaxbypassattempts"] = "abc"
    rc, lines, g = run(st8, True, {**nan, "compliancePolicies": []})
    expect("create: a non-integer value for an integer setting is an ERROR line naming it, never a traceback",
           rc == 1 and any(ln.startswith("ERROR") and "is not an integer" in ln for ln in lines), lines)
    bad_fv = copy.deepcopy(fv)
    bad_fv["settingsPolicies"][-1]["create"]["groups"]["x"] = ["undeclared.setting"]
    expect("validate: create.groups naming a setting the policy does not declare is refused",
           any("does not declare" in x for x in validate(bad_fv, ".")), validate(bad_fv, "."))

    print(f"self-test: {'FAILED ' + str(len(fails)) if fails else 'passed'}")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read-only; exit 2 on drift")
    mode.add_argument("--apply", action="store_true", help="create/patch/assign to match the spec; never deletes")
    mode.add_argument("--sync", action="store_true", help="only ask Intune to sync every Mac the spec assigns objects to")
    mode.add_argument("--validate", action="store_true", help="the spec's shape only, no Graph call")
    mode.add_argument("--self-test", action="store_true", help="against an in-memory fake Graph")
    ap.add_argument("--spec", help="the declared fleet spec (JSON)")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.spec:
        ap.error("--spec is required")
    with open(a.spec) as f:
        spec = json.load(f)
    spec_dir = os.path.dirname(os.path.abspath(a.spec))
    problems = validate(spec, spec_dir)
    if problems:
        for p in problems:
            print(f"SPEC    {p}")
        return 1
    if a.validate:
        print(f"spec valid: {a.spec}")
        return 0
    token, source = acquire_token(spec.get("tenant"))
    print(f"── {'sync' if a.sync else 'apply' if a.apply else 'check'} against Graph, signed in with {source}")
    return Reconcile(spec, spec_dir, Graph(token), apply=a.apply, token=token, sync_only=a.sync).run()


if __name__ == "__main__":
    sys.exit(main())
