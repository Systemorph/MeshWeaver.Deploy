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
include and exclude assignment, the people in a group of users (the Mac admins Platform SSO names), any other custom
profile payload written verbatim (Platform SSO's com.apple.extensiblesso and PPPC validated by shape), the macOS apps
(read and assigned, a post-install template rendered per client — the package itself is uploaded by a person), the Apple Business Manager enrollment program (the ADE token by name, the fleet's
macOS enrollment profile, its default flag and the serials assigned to it), and the application permissions the app
identity must hold (verified only).

The rules the reconcile keeps:
  * --check never writes. Every line says what --apply WOULD do for it.
  * --apply never deletes and never removes: an assignment, a group member or an object the spec does not
    name is reported ("kept") and left alone. Assignments are written as the UNION of live and declared.
  * An object marked "hold" is reported as drift and never written, not even by --apply: the owner's go
    lifts the hold in the spec (a reviewed change), not a flag on the command line.
  * A secret is never printed: the token is used, never echoed; ids are shortened to 8 characters.
  * The enrollment program's token is uploaded by a PERSON (Intune's public key → Apple Business Manager, the server
    token back to Intune — two portals, renewed yearly). Until a token of the declared name exists, its lines read
    AWAITING (counted as pending, exit 0): a missing token never turns the rest of the spec red.
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
import datetime
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
# The line after which the fleet loader carries its embedded bootstrap (deploy/macos/intune/fleet-loader.sh).
EMBED_MARK = b"__FLEET_EMBEDDED_BOOTSTRAP__"
# The loader's last line: what makes the bytes appended after it inert. Only a host that ends in it may embed (#288).
EMBED_GUARD = b"return 0 2>/dev/null || exit 0"
# The Apple Business Manager enrollment program (Automated Device Enrollment). Graph beta only (measured against the
# Microsoft Graph beta reference: depOnboardingSetting, depMacOSEnrollmentProfile, setDefaultProfile,
# updateDeviceProfileAssignment, importedAppleDeviceIdentity). Read: DeviceManagementServiceConfig.Read.All;
# write: DeviceManagementServiceConfig.ReadWrite.All.
DEP = "beta/deviceManagement/depOnboardingSettings"
DEP_MACOS_T = "#microsoft.graph.depMacOSEnrollmentProfile"
# The depMacOSEnrollmentProfile properties a spec may declare, by JSON type. A name outside this set is refused by
# validation (a typo would otherwise be drift forever, or a 400 at apply). adminAccountPassword is deliberately absent:
# a secret never lives in the spec; isDefault is absent because `default` declares it (setDefaultProfile writes it).
DEP_MACOS_BOOL = frozenset((
    "requiresUserAuthentication", "enableAuthenticationViaCompanyPortal", "requireCompanyPortalOnSetupAssistantEnrolledDevices",
    "supervisedModeEnabled", "isMandatory", "locationDisabled", "profileRemovalDisabled", "restoreBlocked", "appleIdDisabled",
    "termsAndConditionsDisabled", "touchIdDisabled", "applePayDisabled", "siriDisabled", "diagnosticsDisabled",
    "displayToneSetupDisabled", "privacyPaneDisabled", "screenTimeScreenDisabled", "configurationWebUrl",
    "waitForDeviceConfiguredConfirmation", "registrationDisabled", "fileVaultDisabled", "iCloudDiagnosticsDisabled",
    "passCodeDisabled", "zoomDisabled", "iCloudStorageDisabled", "chooseYourLockScreenDisabled", "accessibilityScreenDisabled",
    "autoUnlockWithWatchDisabled", "skipPrimarySetupAccountCreation", "setPrimarySetupAccountAsRegularUser",
    "dontAutoPopulatePrimaryAccountInfo", "enableRestrictEditing", "hideAdminAccount", "requestRequiresNetworkTether",
    "autoAdvanceSetupEnabled", "usePlatformSSODuringSetupAssistant"))
DEP_MACOS_STR = frozenset(("supportDepartment", "supportPhoneNumber", "deviceNameTemplate", "primaryAccountFullName",
                           "primaryAccountUserName", "adminAccountUserName", "adminAccountFullName", "configurationEndpointUrl"))
DEP_MACOS_LIST = frozenset(("enabledSkipKeys",))
DEP_SECRET = frozenset(("adminAccountPassword",))
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
# A custom profile whose payload is NOT com.memex.local is written VERBATIM from `keys` (lists stay arrays, dicts stay
# dicts): the comma-joining above is com.memex.local's contract with memex-local, not a plist rule. Two Apple payloads
# are validated by shape because a mistake in them is silent on the Mac:
#   com.apple.extensiblesso (with PlatformSSO) — Apple, Device Management "ExtensibleSingleSignOn" + "PlatformSSO";
#   com.apple.TCC.configuration-profile-policy (PPPC) — Apple, "Privacy Preferences Policy Control payload".
SSO_T = "com.apple.extensiblesso"
TCC_T = "com.apple.TCC.configuration-profile-policy"
PSSO_AUTH = ("Password", "UserSecureEnclaveKey", "SmartCard")
PSSO_MODES = ("Standard", "Admin", "Groups", "Temporary")
# Services an MDM can only DENY (or, for these two, let a standard user approve): Apple's PPPC payload. A profile that
# claims to ALLOW them installs and grants nothing — the approval is still a person's click at the Mac.
TCC_DENY_ONLY = ("ScreenCapture", "ListenEvent")
TCC_AUTH = ("Allow", "Deny", "AllowStandardUserToSetSystemService")
# apps[]: the Intune macOS app types the reconcile reads and assigns (it never uploads app CONTENT — the package is
# uploaded by a person in the admin center; see README "Apps").
APP_TYPES = ("macOSPkgApp", "macOSLobApp", "macOSDmgApp")
APP_INTENTS = ("required", "available", "uninstall")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PARAM_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
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
        for u in g.get("users", []):
            if "@" not in str(u.get("upn", "")):
                p.append(f"groups.{k}.users: every user names its upn (user@domain)")
            if u.get("id") and not GUID.match(u["id"]):
                p.append(f"groups.{k}.users: {u.get('upn')} id is not a GUID (the user's directory object id)")
        if g.get("users") and g.get("devices"):
            p.append(f"groups.{k}: declare users OR devices — a group that grants admin rights never also carries Macs")

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
        emb = src.get("embed")
        if emb is not None:
            if not isinstance(emb, dict) or not HEX40.match(emb.get("commit", "")) or not HEX64.match(emb.get("sha256", "")) \
                    or not emb.get("repo") or not emb.get("path"):
                p.append(f"{w}: source.embed needs repo, path, a 40-hex commit and a 64-hex sha256 (the loader's embedded bootstrap)")
            if not str(src.get("path", "")).endswith("fleet-loader.sh"):
                p.append(f"{w}: source.embed is for the fleet loader only (its exit guard keeps the appended bootstrap inert)")
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
            # the jobs answer per model class (AcceptJobsMacStudio …, bootstrap 1.14.0): overrides AcceptJobs for that class,
            # and the bootstrap keeps it on every run (NO takes a joined Mac out of the build queue). An explicit bool only.
            for k, v in keys.items():
                if k.startswith("AcceptJobs") and k != "AcceptJobs" and k not in PARTICIPATION_LIST:
                    if not isinstance(v, bool):
                        p.append(f"{w}: keys.{k} must be an explicit true/false (the jobs answer for one model class)")
                    elif v is True and not keys.get("AcceptJobsFrom"):
                        p.append(f"{w}: {k} is true but AcceptJobsFrom names no instance")
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
            # the build host's fleet default size per model class (BuildVmCpusMacMini …): a whole number above 0, never
            # a bool or a string — memex-local ignores anything else and the Mac would fall back to auto in silence
            for k, v in keys.items():
                if (k.startswith("BuildVmCpus") or k.startswith("BuildMaxRunners")) and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
                    p.append(f"{w}: keys.{k} must be a whole number above 0 (memex-local reads it as the build host's fleet default)")
            # the GPU wired-memory reserve per model class (GpuWiredReserveMbMacStudio …, bootstrap 1.13.0): a whole number of
            # MiB of at least 4096 — the bootstrap refuses anything else, so the Mac would keep macOS's default in silence
            for k, v in keys.items():
                if k.startswith("GpuWiredReserveMb") and (isinstance(v, bool) or not isinstance(v, int) or v < 4096):
                    p.append(f"{w}: keys.{k} must be a whole number of MiB of at least 4096 (the bootstrap keeps it from the GPU)")
            # the bootstrap's pin for the fleet loader (it travels in the profile: minutes, not the agent's 8 h)
            if ("BootstrapCommit" in keys) != ("BootstrapSha256" in keys):
                p.append(f"{w}: keys.BootstrapCommit and keys.BootstrapSha256 go together (the loader verifies one by the other)")
            if "BootstrapCommit" in keys and not HEX40.match(str(keys["BootstrapCommit"])):
                p.append(f"{w}: keys.BootstrapCommit must be a full 40-hex commit")
            if "BootstrapSha256" in keys and not HEX64.match(str(keys["BootstrapSha256"])):
                p.append(f"{w}: keys.BootstrapSha256 must be 64 lower-hex")
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
        if pl.get("payloadType") == SSO_T:
            p.extend(f"{w}: {x}" for x in validate_sso(keys))
        if pl.get("payloadType") == TCC_T:
            p.extend(f"{w}: {x}" for x in validate_tcc(keys))
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
    for a in spec.get("apps", []):
        w = f"apps.{a.get('key')}"
        p.extend(f"{w}: {x}" for x in validate_app(a, groups))
        targets(w, a.get("assign"))
    p.extend(validate_enrollment_program(spec.get("enrollmentProgram"), groups))
    app = spec.get("appIdentity") or {}
    if not isinstance(app.get("requiredRoles"), list) or not app.get("requiredRoles"):
        p.append("appIdentity.requiredRoles: declare the Graph application permissions the reconcile needs")
    return p


def validate_sso(keys):
    """com.apple.extensiblesso: the extension, and — when it carries PlatformSSO — the values macOS accepts. A wrong
    value is not an install error on the Mac: the profile installs and the setting is ignored, so it is refused here."""
    p = []
    for k in ("ExtensionIdentifier", "TeamIdentifier", "Type"):
        if not keys.get(k):
            p.append(f"keys.{k} is required for {SSO_T}")
    if keys.get("Type") not in (None, "Redirect", "Credential"):
        p.append("keys.Type must be Redirect or Credential")
    if keys.get("Type") == "Redirect" and not (isinstance(keys.get("URLs"), list) and keys["URLs"]):
        p.append("keys.URLs must be a non-empty list for a Redirect extension")
    ps = keys.get("PlatformSSO")
    if ps is None:
        return p
    if not isinstance(ps, dict):
        return p + ["keys.PlatformSSO must be a dictionary"]
    if ps.get("AuthenticationMethod") not in PSSO_AUTH:
        p.append("keys.PlatformSSO.AuthenticationMethod must be one of " + ", ".join(PSSO_AUTH))
    groups_used = False
    for k in ("NewUserAuthorizationMode", "UserAuthorizationMode"):
        if k in ps and ps[k] not in PSSO_MODES:
            p.append(f"keys.PlatformSSO.{k} must be one of " + ", ".join(PSSO_MODES))
        groups_used |= ps.get(k) == "Groups"
    for k in ("AdministratorGroups", "AdditionalGroups"):
        if k in ps and not (isinstance(ps[k], list) and ps[k] and all(isinstance(x, str) and x for x in ps[k])):
            p.append(f"keys.PlatformSSO.{k} must be a non-empty list of group names")
    if groups_used and not (ps.get("AdministratorGroups") or ps.get("AuthorizationGroups")):
        p.append("keys.PlatformSSO: an authorization mode of Groups needs AdministratorGroups (or AuthorizationGroups) — "
                 "without one every Platform SSO user is a standard user")
    if ps.get("EnableCreateUserAtLogin") is True and ps.get("AuthenticationMethod") == "UserSecureEnclaveKey":
        p.append("keys.PlatformSSO.EnableCreateUserAtLogin needs AuthenticationMethod Password (or SmartCard): the login "
                 "window can only create an account from a credential it can verify")
    if ps.get("EnableCreateUserAtLogin") is True and ps.get("UseSharedDeviceKeys") is not True:
        p.append("keys.PlatformSSO.EnableCreateUserAtLogin needs UseSharedDeviceKeys true (Apple's com.apple.extensiblesso schema)")
    for k in ("NonPlatformSSOAccounts",):
        if k in ps and not (isinstance(ps[k], list) and all(isinstance(x, str) and x for x in ps[k])):
            p.append(f"keys.PlatformSSO.{k} must be a list of local account short names")
    if not isinstance(keys.get("RegistrationToken", ""), str):
        p.append("keys.RegistrationToken must be a string")
    return p


def validate_tcc(keys):
    """PPPC: every entry names its code (identifier, type, designated requirement) and one authorization; a service an
    MDM can only deny is refused when the spec ALLOWS it (Apple ignores the grant; the approval stays a person's)."""
    p = []
    svc = keys.get("Services")
    if not isinstance(svc, dict) or not svc:
        return ["keys.Services must map each TCC service to its entries"]
    for name, entries in svc.items():
        if not isinstance(entries, list) or not entries:
            p.append(f"keys.Services.{name} must be a non-empty list")
            continue
        for i, e in enumerate(entries):
            where = f"keys.Services.{name}[{i}]"
            if not isinstance(e, dict):
                p.append(f"{where} must be a dictionary")
                continue
            for k in ("Identifier", "IdentifierType", "CodeRequirement"):
                if not e.get(k):
                    p.append(f"{where}.{k} is required (an entry without the designated requirement is ignored by macOS)")
            if e.get("IdentifierType") not in (None, "bundleID", "path"):
                p.append(f"{where}.IdentifierType must be bundleID or path")
            auth = e.get("Authorization")
            if auth is not None and auth not in TCC_AUTH:
                p.append(f"{where}.Authorization must be one of " + ", ".join(TCC_AUTH))
            if auth is None and not isinstance(e.get("Allowed"), bool):
                p.append(f"{where}: declare Authorization (or the older boolean Allowed)")
            if name in TCC_DENY_ONLY and (auth == "Allow" or e.get("Allowed") is True):
                p.append(f"{where}: {name} cannot be pre-granted by MDM — Apple honours only Deny or "
                         "AllowStandardUserToSetSystemService; the approval is a person's click at the Mac")
    return p


def validate_app(a, groups):
    p = []
    for f in ("key", "displayName", "type", "intent"):
        if not a.get(f):
            p.append(f"{f} is required")
    if a.get("type") and a["type"] not in APP_TYPES:
        p.append("type must be one of " + ", ".join(APP_TYPES))
    if a.get("intent") and a["intent"] not in APP_INTENTS:
        p.append("intent must be one of " + ", ".join(APP_INTENTS))
    if a.get("id") and not GUID.match(a["id"]):
        p.append("id is not a GUID")
    props = a.get("properties", {})
    if not isinstance(props, dict) or any(k in props for k in ("postInstallScript", "preInstallScript", "@odata.type")):
        p.append("properties is a map of the app's simple properties (scripts are declared under postInstallScript)")
    sc = a.get("postInstallScript")
    if sc is not None:
        if a.get("type") != "macOSPkgApp":
            p.append("postInstallScript is only carried by a macOSPkgApp")
        src = (sc or {}).get("source") or {}
        if not HEX40.match(src.get("commit", "")) or not HEX64.match(src.get("sha256", "")) or not src.get("repo") or not src.get("path"):
            p.append("postInstallScript.source needs repo, path, a 40-hex commit and a 64-hex sha256 (a pin, never a branch)")
        for k, v in (sc.get("params") or {}).items():
            if not PARAM_NAME.match(k) or not isinstance(v, str):
                p.append(f"postInstallScript.params.{k}: an UPPER_CASE name and a string value")
        for k, v in (sc.get("secrets") or {}).items():
            if not PARAM_NAME.match(k) or not isinstance(v, str) or not ENV_NAME.match(v):
                p.append(f"postInstallScript.secrets.{k} must name an ENVIRONMENT VARIABLE (the secret itself never lives in the spec)")
        af = sc.get("aliasesFrom")
        if af is not None and af not in groups:
            p.append(f"postInstallScript.aliasesFrom names an undeclared group '{af}'")
    return p


def render_script(template, sc, spec, secret_values):
    """A post-install script from its pinned TEMPLATE: `@@NAME@@` → params[NAME]; `@@DEVICE_ALIASES@@` → one
    `<serial> <name>` line per Mac of the aliasesFrom group (each Mac names itself by its own serial); a secret's line
    `NAME='@@NAME@@'` → its value from the environment — or, when `secret_values` is None, the placeholder is KEPT, which
    is the form a live script is compared in (normalise_script), so the drift check needs no secret."""
    out = template.decode()
    for k, v in (sc.get("params") or {}).items():
        out = out.replace(f"@@{k}@@", v)
    if sc.get("aliasesFrom"):
        devs = spec["groups"][sc["aliasesFrom"]].get("devices", [])
        out = out.replace("@@DEVICE_ALIASES@@", "\n".join(f"{d['serial']} {d['name']}" for d in devs))
    for k in (sc.get("secrets") or {}):
        if secret_values is not None:
            v = secret_values[k]
            if "'" in v or "\n" in v:
                raise GraphError(0, "SECRET", k, "a secret value may not contain a quote or a newline")
            out = out.replace(f"{k}='@@{k}@@'", f"{k}='{v}'")
    return out.encode()


def normalise_script(content, sc):
    """A live script with every secret's VALUE put back to its placeholder: what the drift check compares."""
    out = content.decode(errors="replace")
    for k in (sc.get("secrets") or {}):
        out = re.sub(rf"^{k}='[^'\n]*'$", f"{k}='@@{k}@@'", out, flags=re.M)
    return out.encode()


def validate_enrollment_program(ep, groups):
    """The `enrollmentProgram` section's shape (absent = no enrollment program declared, valid)."""
    p = []
    if ep is None:
        return p
    if not isinstance(ep, dict):
        return ["enrollmentProgram: must be an object"]
    tok = ep.get("token") or {}
    if not isinstance(tok.get("tokenName"), str) or not tok.get("tokenName").strip():
        p.append("enrollmentProgram.token.tokenName: required — the token's display name, as the owner names it when "
                 "uploading the Apple Business Manager server token to Intune")
    if tok.get("id") and not GUID.match(str(tok["id"])):
        p.append("enrollmentProgram.token.id: is not a GUID")
    rd = tok.get("renewWarnDays", 30)
    if isinstance(rd, bool) or not isinstance(rd, int) or rd < 1:
        p.append("enrollmentProgram.token.renewWarnDays: a whole number of days above 0")
    profiles = ep.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        p.append("enrollmentProgram.profiles: declare at least one enrollment profile")
        return p
    defaults = 0
    for pr in profiles:
        w = f"enrollmentProgram.profiles.{pr.get('key') if isinstance(pr, dict) else '?'}"
        if not isinstance(pr, dict):
            p.append(f"{w}: must be an object")
            continue
        for f in ("key", "displayName"):
            if not pr.get(f):
                p.append(f"{w}: {f} is required")
        if pr.get("id") and not GUID.match(str(pr["id"])):
            p.append(f"{w}: id is not a GUID")
        if pr.get("platform") != "macOS":
            p.append(f"{w}: platform must be \"macOS\" (the only enrollment profile this reconcile builds)")
        if not isinstance(pr.get("default", False), bool):
            p.append(f"{w}: default must be true/false")
        defaults += pr.get("default") is True
        st = pr.get("settings")
        if not isinstance(st, dict) or not st:
            p.append(f"{w}: settings must map depMacOSEnrollmentProfile properties to their values")
            st = {}
        for k, v in st.items():
            if k in DEP_SECRET:
                p.append(f"{w}: settings.{k} is refused — a secret never lives in the spec (leave the local admin account undeclared)")
            elif k == "isDefault":
                p.append(f"{w}: settings.isDefault is refused — declare `default: true` (setDefaultProfile writes it)")
            elif k in DEP_MACOS_BOOL:
                if not isinstance(v, bool):
                    p.append(f"{w}: settings.{k} must be true/false")
            elif k in DEP_MACOS_STR:
                if not isinstance(v, str):
                    p.append(f"{w}: settings.{k} must be a string")
            elif k in DEP_MACOS_LIST:
                if not (isinstance(v, list) and all(isinstance(x, str) and x for x in v)):
                    p.append(f"{w}: settings.{k} must be a list of non-empty strings")
            else:
                p.append(f"{w}: settings.{k} is not a depMacOSEnrollmentProfile property this reconcile knows")
        a = pr.get("assign") or {}
        dof = a.get("devicesOf", [])
        if not isinstance(dof, list) or not all(isinstance(x, str) for x in dof):
            p.append(f"{w}: assign.devicesOf must be a list of declared group keys")
            dof = []
        for gk in dof:
            if gk not in groups:
                p.append(f"{w}: assign.devicesOf names an undeclared group '{gk}'")
            elif not (groups[gk] or {}).get("devices"):
                p.append(f"{w}: assign.devicesOf names group '{gk}', which declares no devices")
        if not dof:
            p.append(f"{w}: assign.devicesOf is empty — name the declared group(s) whose serials this profile enrols")
    if defaults > 1:
        p.append("enrollmentProgram.profiles: more than one macOS profile declares default: true")
    return p


def build_plist(c):
    """The custom profile's mobileconfig, built from the spec. Lists become ONE comma-separated string, except the
    ARRAY_KEYS, which stay a plist array."""
    pl = c["plist"]
    inner = {"PayloadType": pl["payloadType"], "PayloadVersion": 1, "PayloadIdentifier": pl["innerIdentifier"],
             "PayloadUUID": pl["innerUuid"], "PayloadDisplayName": pl["displayName"]}
    joined = pl["payloadType"] == "com.memex.local"
    for k, v in c.get("keys", {}).items():
        inner[k] = ",".join(v) if joined and isinstance(v, list) and k not in ARRAY_KEYS else v
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

    def reconcile_assignments(self, obj, what, live, assign_path, body_key, odata_item=None, extra=None):
        """Writes the UNION of live and declared targets. The only target ever dropped is one the spec names under
        `assign.remove` (a declared removal, reviewed like any other line). A kept live target is written back
        verbatim, assignment filter included. `extra` (an app's {"intent": …}) is carried by every declared item: a
        declared target live with a different value is rewritten with the declared one; a kept item keeps its own."""
        extra = extra or {}
        want = self.desired_targets(obj.get("assign"))
        drop = self.removal_targets(obj.get("assign"))
        if any(g is None and k in (INCLUDE_T, EXCLUDE_T) for k, g in want):
            self.line("PENDING", what, "assignments wait for a group that does not exist yet")
            self.pending += 1
            return
        have = self.live_targets(live)
        wrong = {(a["target"]["@odata.type"], a["target"].get("groupId")) for a in live
                 if (a["target"]["@odata.type"], a["target"].get("groupId")) in want and any(a.get(k) != v for k, v in extra.items())}
        missing, kept, removing = (want - have) | wrong, have - want - drop, have & drop
        if kept:
            self.line("INFO", what, "assignment not in the spec, kept: " + ", ".join(sorted(self.label(t) for t in kept)))
        if not missing and not removing:
            self.line("OK", what, "assignments " + ", ".join(sorted(self.label(t) for t in want)))
            return
        items = []
        for a in live:
            if (a["target"]["@odata.type"], a["target"].get("groupId")) in drop | wrong:
                continue
            item = {"target": {k: v for k, v in a["target"].items() if v is not None}}
            item.update({k: a[k] for k in extra if k in a})
            item.update({k: a[k] for k in ("settings",) if extra and a.get(k) is not None})
            if odata_item:
                item["@odata.type"] = odata_item
            items.append(item)
        for kind, gid in sorted(missing, key=lambda t: (t[0], t[1] or "")):
            target = {"@odata.type": kind}
            if gid:
                target["groupId"] = gid
            item = dict({"target": target}, **extra)
            if odata_item:
                item["@odata.type"] = odata_item
            items.append(item)
        detail = "; ".join(x for x in (
            ("missing assignment " + ", ".join(sorted(self.label(t) for t in missing - wrong))) if missing - wrong else "",
            (", ".join(sorted(self.label(t) for t in wrong)) + " live with another " + "/".join(extra)
             + " (spec: " + ", ".join(f"{k}={v}" for k, v in extra.items()) + ")") if wrong else "",
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
            if not g.get("devices") and not g.get("users") and not g.get("reportMembers"):
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
            for u in g.get("users", []):
                what = f"user {u['upn']} in {key}"
                if not u.get("id"):
                    # resolving a UPN app-only needs User.ReadBasic.All, which the app identity does not hold: the id is declared
                    self.pending += 1
                    self.line("PENDING", what, "its directory object id is not declared — read it with "
                              f"`az ad user show --id {u['upn']} --query id -o tsv` and record it as users[].id")
                    continue
                declared.add(u["id"])
                if u["id"] in members:
                    self.line("OK", what, f"member (directory {short(u['id'])})")
                    continue
                self.finding(g, what, "not a member", f"POST v1.0/groups/{short(gid)}/members/$ref directory object {short(u['id'])}",
                             lambda oid=u["id"]: self.g.post(f"v1.0/groups/{gid}/members/$ref",
                                                             {"@odata.id": f"{GRAPH}/v1.0/directoryObjects/{oid}"}))
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
                want_sha = self.expected_sha(s)
                diffs = [f"{k} live={full.get(k)!r}" for k, v in meta.items() if full.get(k) != v]
                if live_sha != want_sha:
                    diffs.append(f"content sha256 live={live_sha[:12]} spec={want_sha[:12]}")
                if diffs:
                    def patch(full=full, live_sha=live_sha, want_sha=want_sha):
                        body = dict(meta, **{"@odata.type": "#microsoft.graph.deviceShellScript"})
                        if live_sha != want_sha:
                            body["scriptContent"] = base64.b64encode(self.source(s)).decode()
                        self.g.patch(f"beta/deviceManagement/deviceShellScripts/{full['id']}", body)
                    self.finding(s, what, "; ".join(diffs), f"PATCH deviceShellScripts/{short(live['id'])}"
                                 + (f" with the content of {s['source']['repo']}@{s['source']['commit'][:12]}" if live_sha != want_sha else ""),
                                 patch)
                else:
                    self.line("OK", what, f"{short(live['id'])}, content {live_sha[:12]} = {s['source']['path']}@{s['source']['commit'][:12]}")
            self.reconcile_assignments(s, what, live.get("assignments", []), f"beta/deviceManagement/deviceShellScripts/{live['id']}/assign",
                                       "deviceManagementScriptAssignments")

    def pinned(self, src):
        content = self.fetch(src)
        got = hashlib.sha256(content).hexdigest()
        if got != src["sha256"]:
            raise GraphError(0, "FETCH", src["path"], f"the pinned source hashes to {got[:12]}, the spec says "
                             f"{src['sha256'][:12]} — refusing to upload it")
        return content

    def source(self, s):
        """The script's uploaded content: the pinned source, and — with `source.embed` (the fleet loader) — the pinned
        EMBEDDED copy appended after the marker line the loader reads it from (deploy/macos/intune/fleet-loader.sh).
        Fetched ONCE per run and reused (review on #288)."""
        cache = self.__dict__.setdefault("_sources", {})
        if s["key"] in cache:
            return cache[s["key"]]
        content = self.pinned(s["source"])
        if s["source"].get("embed"):
            # only a host whose LAST line is the loader's exit guard keeps the appended bytes inert — any other script
            # would run them inline, as root
            if content.rstrip(b"\n").rsplit(b"\n", 1)[-1].strip() != EMBED_GUARD:
                raise GraphError(0, "FETCH", s["source"]["path"], "source.embed on a script that does not end in the loader's "
                                 "exit guard — the appended bytes would execute; refusing to upload it")
            content = content.rstrip(b"\n") + b"\n" + EMBED_MARK + b"\n" + self.pinned(s["source"]["embed"])
        cache[s["key"]] = content
        return content

    def expected_sha(self, s):
        """What the live content must hash to: the source's own sha256, or that of the combined upload."""
        return hashlib.sha256(self.source(s)).hexdigest() if s["source"].get("embed") else s["source"]["sha256"]

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

    # apps (an Intune macOS app: read, its declared properties, its post-install script, its assignments) ──
    def apps(self):
        for a in self.spec.get("apps", []):
            what = f"app {a['key']} \"{a['displayName']}\""
            t = "#microsoft.graph." + a["type"]
            live = None
            if a.get("id"):
                try:
                    live = self.g.get(f"beta/deviceAppManagement/mobileApps/{a['id']}")
                except GraphError as e:
                    if e.status != 404:
                        raise
            if live is None:
                found = [x for x in self.g.list("beta/deviceAppManagement/mobileApps?$filter="
                                                + urllib.parse.quote(f"displayName eq '{a['displayName']}'"))
                         if x.get("@odata.type") == t]
                if found:
                    self.name_match(what, a, found)
                    live = found[0]
            if live is None:
                # the CONTENT of an app (the package) is never uploaded here: it is the licensed vendor's or the client's
                # file, uploaded by a person — AWAITING (counted as pending), never red, like a missing ADE token
                self.pending += 1
                self.line("AWAITING", what, f"no {a['type']} of this name — a person uploads the package in the Intune "
                          "admin center (Apps → macOS → Add → " + {"macOSPkgApp": "macOS app (PKG)", "macOSLobApp":
                          "Line-of-business app", "macOSDmgApp": "macOS app (DMG)"}[a["type"]]
                          + f") named exactly \"{a['displayName']}\"", "nothing — app content is never uploaded by the reconcile")
                continue
            if live.get("@odata.type") not in (None, t):
                self.errors += 1
                self.line("ERROR", what, f"is a {live.get('@odata.type')}, the spec declares {a['type']}")
                continue
            diffs = [f"{k}: live={live.get(k)!r} spec={v!r}" for k, v in (a.get("properties") or {}).items() if live.get(k) != v]
            body = {"@odata.type": t, **(a.get("properties") or {})}
            sc = a.get("postInstallScript")
            if sc:
                tmpl = self.pinned(sc["source"])
                want = render_script(tmpl, sc, self.spec, None)
                have = base64.b64decode(((live.get("postInstallScript") or {}).get("scriptContent")) or "")
                if normalise_script(have, sc) != want:
                    diffs.append("post-install script differs from " + f"{sc['source']['path']}@{sc['source']['commit'][:12]}"
                                 + " rendered from the spec" + (" (live: none)" if not have else ""))
                    env = {k: os.environ.get(v) for k, v in (sc.get("secrets") or {}).items()}
                    if self.apply and not a.get("hold") and any(v is None for v in env.values()):
                        self.errors += 1
                        self.line("ERROR", what, "; ".join(diffs) + "; the script needs its secret(s) from the environment: "
                                  + ", ".join(sc["secrets"][k] for k, v in env.items() if v is None) + " — nothing written")
                        diffs = None
                    elif self.apply:
                        body["postInstallScript"] = {"@odata.type": "#microsoft.graph.macOSAppScript", "scriptContent":
                                                     base64.b64encode(render_script(tmpl, sc, self.spec, env)).decode()}
            if diffs:
                self.finding(a, what, "; ".join(diffs), f"PATCH mobileApps/{short(live['id'])} ("
                             + ", ".join(k for k in body if k != "@odata.type") + " — the script with its secret from the environment)",
                             lambda live=live, body=body: self.g.patch(f"beta/deviceAppManagement/mobileApps/{live['id']}", body))
            elif diffs is not None:
                self.line("OK", what, f"{short(live['id'])}" + (f", post-install script = {sc['source']['path']}@{sc['source']['commit'][:12]}" if sc else ""))
            self.reconcile_assignments(a, what, self.g.list(f"beta/deviceAppManagement/mobileApps/{live['id']}/assignments"),
                                       f"beta/deviceAppManagement/mobileApps/{live['id']}/assign", "mobileAppAssignments",
                                       "#microsoft.graph.mobileAppAssignment", extra={"intent": a["intent"]})

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

    # the Apple Business Manager enrollment program (Automated Device Enrollment) ───────────────────────────
    def enrollment_program(self):
        """The ADE token (verify only: a person uploads and renews it), the declared macOS enrollment profile(s), the
        default flag, and each declared serial's profile assignment. No token of the declared name yet = AWAITING."""
        ep = self.spec.get("enrollmentProgram")
        if not ep:
            return
        tok = ep["token"]
        what = f"enrollment token \"{tok['tokenName']}\""
        try:
            live = self.find(DEP, tok, "tokenName")
        except GraphError as e:
            if e.status in (401, 403):
                raise GraphError(e.status, "GET", DEP, "the credential cannot read the enrollment program (needs "
                                 "DeviceManagementServiceConfig.Read.All to check, .ReadWrite.All to apply)") from None
            raise
        if live is None:
            self.pending += 1
            self.line("AWAITING", what, "awaiting the ABM token — no enrollment program token of this name in Intune "
                      "(the owner exchanges it in two portals: Intune's public key → an ABM MDM server, its server token "
                      "back to Intune, named exactly as declared)", "nothing")
            for pr in ep["profiles"]:
                self.pending += 1
                self.line("AWAITING", f"enrollment profile {pr['key']} \"{pr['displayName']}\"", "waits for the ABM token",
                          "nothing until the token exists")
            return
        tid = live["id"]
        self.token_health(what, tok, live)
        idents = None
        for pr in ep["profiles"]:
            pwhat = f"enrollment profile {pr['key']} \"{pr['displayName']}\""
            coll = f"{DEP}/{tid}/enrollmentProfiles"
            body = dict(pr["settings"], **{"@odata.type": DEP_MACOS_T, "displayName": pr["displayName"],
                                           "description": pr.get("description", "")})
            prof = self.find(coll, pr, "displayName")
            if prof is not None and prof.get("@odata.type") not in (None, DEP_MACOS_T):
                self.errors += 1
                self.line("ERROR", pwhat, f"the profile of this name is a {prof.get('@odata.type')}, not a macOS ADE profile")
                continue
            if prof is None:
                self.finding(pr, pwhat, "absent", f"POST {coll.split('/', 2)[2].replace(tid, short(tid))} "
                             "(depMacOSEnrollmentProfile, settings as declared)",
                             lambda pr=pr, coll=coll, body=body: pr.__setitem__("_live", self.g.post(coll, body)))
                prof = pr.get("_live")
                if prof is None:
                    self.line("INFO", pwhat, "its default flag and serial assignments are checked once it exists")
                    continue
            else:
                diffs = []
                for k, v in sorted(dict(pr["settings"], description=pr.get("description", "")).items()):
                    lv = prof.get(k)
                    # an unset value may read back as null: null is "" for a string, [] for a list and false for a
                    # boolean (Graph's default) — else a declared false would read as drift forever (review on #332)
                    same = sorted(lv or []) == sorted(v) if isinstance(v, list) else (lv or "") == v if isinstance(v, str) \
                        else (False if lv is None else lv) == v
                    if not same:
                        diffs.append(f"{k} live={lv!r} spec={v!r}")
                if diffs:
                    self.finding(pr, pwhat, "; ".join(diffs), f"PATCH enrollmentProfiles/{short(prof['id'])} with the declared settings",
                                 lambda prof=prof, coll=coll, body=body: self.g.patch(f"{coll}/{prof['id']}", body))
                else:
                    self.line("OK", pwhat, f"{short(prof['id'])}, {len(pr['settings'])} settings as declared")
            if pr.get("default"):
                if prof.get("isDefault") is True:
                    self.line("OK", pwhat, "the token's default macOS profile")
                else:
                    self.finding(pr, pwhat, "not the token's default macOS profile",
                                 f"POST enrollmentProfiles/{short(prof['id'])}/setDefaultProfile",
                                 lambda prof=prof, coll=coll: self.g.post(f"{coll}/{prof['id']}/setDefaultProfile", None))
            if idents is None:
                idents = {}
                for d in self.g.list(f"{DEP}/{tid}/importedAppleDeviceIdentities"):
                    if not d.get("isDeleted"):
                        idents.setdefault((d.get("serialNumber") or "").upper(), d)
            self.profile_assignment(pr, pwhat, prof, coll, idents)

    def token_health(self, what, tok, live):
        """Expiry, last sync and Apple's last sync error. Nothing here is --apply's to repair: the token is renewed by a
        person in Apple Business Manager and Intune, so a problem is drift with no write (as a missing grant is)."""
        now = getattr(self, "now", None) or datetime.datetime.now(datetime.timezone.utc)
        def when(k):
            v = live.get(k)
            try:
                return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None
            except ValueError:
                return None
        exp, synced = when("tokenExpirationDateTime"), when("lastSuccessfulSyncDateTime")
        detail = (f"{short(live['id'])}, expires {exp.date() if exp else '?'}, last sync "
                  f"{synced.strftime('%Y-%m-%dT%H:%MZ') if synced else 'never'}, {live.get('syncedDeviceCount', '?')} devices synced")
        if exp is None or exp <= now:
            self.drift += 1
            self.line("DRIFT", what, detail + ("; EXPIRED" if exp else "; no expiry readable"),
                      "nothing — renew the server token in Apple Business Manager and upload it to the SAME token in Intune (the owner's act)")
            return
        if live.get("lastSyncErrorCode"):
            self.drift += 1
            self.line("DRIFT", what, f"{detail}; Apple reported sync error {live['lastSyncErrorCode']}",
                      "nothing — read the token's sync status in the Intune admin center (the owner's act)")
            return
        left = (exp - now).days
        if left < tok.get("renewWarnDays", 30):
            self.pending += 1
            self.line("PENDING", what, f"{detail}; renew within {left} days (yearly, in Apple Business Manager + Intune)", "nothing")
            return
        self.line("OK", what, detail)

    def profile_assignment(self, pr, pwhat, prof, coll, idents):
        """Each declared serial (the devices of `assign.devicesOf`) assigned to this profile. A serial Apple Business
        Manager has not assigned to this MDM server is PENDING (that is done in ABM, not here); a serial assigned to no
        or another profile is drift, and --apply assigns every such serial in ONE updateDeviceProfileAssignment call."""
        serials = {}
        for gk in pr["assign"]["devicesOf"]:
            for d in self.spec["groups"][gk].get("devices", []):
                serials.setdefault(d["serial"].upper(), d.get("name") or d["serial"])
        move = []
        for serial, name in serials.items():
            w = f"enrollment assignment {name} → {pr['key']}"
            d = idents.get(serial)
            if d is None:
                self.pending += 1
                self.line("PENDING", w, "not in this token's devices — assign the serial to this MDM server in Apple Business Manager", "nothing")
                continue
            state = f"enrollment {d.get('enrollmentState') or '?'}, supervised {d.get('isSupervised')}"
            if d.get("requestedEnrollmentProfileId") == prof["id"]:
                self.line("OK", w, f"assigned ({state})")
                continue
            have = d.get("requestedEnrollmentProfileId")
            move.append((serial, name, f"profile live={short(have) if have else 'none'} ({state})"))
        if move:
            names = ", ".join(n for _s, n, _d in move)
            self.finding(pr, f"enrollment assignment {pr['key']}", "; ".join(f"{n}: {d}" for _s, n, d in move),
                         f"POST enrollmentProfiles/{short(prof['id'])}/updateDeviceProfileAssignment for {names}",
                         lambda: self.g.post(f"{coll}/{prof['id']}/updateDeviceProfileAssignment",
                                             {"deviceIds": [s_ for s_, _n, _d in move]}))

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
        return any(obj is x for k in ("scripts", "customProfiles", "trustedRootCertificates", "settingsPolicies", "apps")
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
            try:
                md = [m for m in self.g.list("v1.0/deviceManagement/managedDevices?$filter="
                                             + urllib.parse.quote(f"azureADDeviceId eq '{aad}'") + "&$select=id,deviceName,operatingSystem")
                      if (m.get("operatingSystem") or "macOS") == "macOS"]   # the fleet's Macs only, as the device step
            except GraphError as e:
                # ONE Mac's lookup failing (a transient 503, measured 2026-11-12) never costs the other Macs their sync
                self.errors += 1
                self.line("ERROR", what, f"managed-device lookup failed: {e} — not synced (--sync retries)")
                continue
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
                 ("settings policies", self.settings_policies), ("apps", self.apps), ("compliance", self.compliance),
                 ("enrollment program", self.enrollment_program)]
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
                self.sync_devices([x for k in ("scripts", "customProfiles", "trustedRootCertificates", "settingsPolicies", "apps")
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

    def _dep(self, method, path, body=None):
        """The enrollment program's nested routes: the token, its profiles, its imported device identities, and the two
        profile actions — answered as Graph beta documents them (setDefaultProfile: no body; a 204 = {})."""
        rest = path.split("?")[0][len(DEP):].strip("/")
        parts = rest.split("/") if rest else []
        toks = self.s.setdefault("dep", {})
        if not parts:
            if method == "LIST":
                return self._filter([{k: copy.deepcopy(v) for k, v in t.items() if not k.startswith("_")} for t in toks.values()], path)
            raise GraphError(400, method, path, "route not faked")
        t = toks.get(parts[0])
        if t is None:
            raise GraphError(404, method, path, "not found")
        if len(parts) == 1 and method == "GET":
            return {k: copy.deepcopy(v) for k, v in t.items() if not k.startswith("_")}
        profs = t.setdefault("_profiles", {})
        if parts[1:] == ["importedAppleDeviceIdentities"] and method == "LIST":
            return copy.deepcopy(t.get("_identities", []))
        if parts[1:] == ["enrollmentProfiles"]:
            if method == "LIST":
                return copy.deepcopy(list(profs.values()))
            if method == "POST":
                i = self._new()
                profs[i] = dict(body, id=i, isDefault=False)
                return copy.deepcopy(profs[i])
        if len(parts) >= 3 and parts[1] == "enrollmentProfiles":
            pr = profs.get(parts[2])
            if pr is None:
                raise GraphError(404, method, path, "not found")
            if len(parts) == 3 and method == "GET":
                return copy.deepcopy(pr)
            if len(parts) == 3 and method == "PATCH":
                pr.update({k: v for k, v in body.items() if k != "@odata.type"})
                return {}
            if parts[3:] == ["setDefaultProfile"] and method == "POST":
                if body is not None:
                    raise GraphError(400, method, path, "setDefaultProfile takes no body")
                for o in profs.values():
                    if o.get("@odata.type") == pr.get("@odata.type"):
                        o["isDefault"] = o is pr
                return {}
            if parts[3:] == ["updateDeviceProfileAssignment"] and method == "POST":
                ids = (body or {}).get("deviceIds")
                if not isinstance(ids, list) or not ids:
                    raise GraphError(400, method, path, "deviceIds required")
                known = {d["serialNumber"]: d for d in t.get("_identities", [])}
                if any(x not in known for x in ids):
                    raise GraphError(400, method, path, "a serial this token does not hold")
                for x in ids:
                    known[x]["requestedEnrollmentProfileId"] = pr["id"]
                return {}
        raise GraphError(400, method, path, "route not faked")

    def _coll(self, path):
        p = path.split("?")[0]
        for prefix, key in (("v1.0/groups", "groups"), ("v1.0/devices", "devices"),
                            ("v1.0/deviceManagement/managedDevices", "managedDevices"),
                            ("beta/deviceManagement/deviceShellScripts", "scripts"),
                            ("beta/deviceManagement/deviceConfigurations", "configs"),
                            ("beta/deviceManagement/configurationPolicies", "policies"),
                            ("beta/deviceManagement/configurationSettings", "settingDefs"),
                            ("beta/deviceManagement/deviceCompliancePolicies", "compliance"),
                            ("beta/deviceAppManagement/mobileApps", "apps")):
            if p == prefix or p.startswith(prefix + "/"):
                return key, p[len(prefix):].strip("/").split("/") if p != prefix else []
        raise GraphError(400, "?", p, "route not faked")

    def get(self, path):
        if path.startswith(DEP):
            return self._dep("GET", path)
        key, rest = self._coll(path)
        obj = self.s[key].get(rest[0])
        if obj is None:
            raise GraphError(404, "GET", path, "not found")
        out = copy.deepcopy(obj)
        if "expand=assignments" in path:
            out["assignments"] = out.get("_assign", [])
        return out

    def list(self, path):
        if path.startswith(DEP):
            return self._dep("LIST", path)
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
        if path.startswith(DEP):
            return self._dep("POST", path, body)
        key, rest = self._coll(path)
        if not rest:
            i = self._new()
            self.s[key][i] = dict(body, id=i)
            return {"id": i, **body}
        obj = self.s[key][rest[0]]
        if rest[1] == "assign":
            items = next(iter(body.values()))
            obj["_assign"] = [{k: v for k, v in it.items() if k != "@odata.type"} for it in items]
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
        if path.startswith(DEP):
            return self._dep("PATCH", path, body)
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
    size = copy.deepcopy(spec)
    size["customProfiles"][0]["keys"].update(BuildVmCpusMacMini=8, BuildMaxRunnersMacMini=2)
    expect("validate: the minis' build-host size keys (8 / 2) pass", not validate(size, "."), validate(size, "."))
    expect("validate: …and travel as plist integers", build_plist(size["customProfiles"][0])["PayloadContent"][0].get("BuildVmCpusMacMini") == 8)
    for badv in ("8", True, 0):
        size["customProfiles"][0]["keys"]["BuildVmCpusMacMini"] = badv
        v = validate(size, ".")
        expect(f"validate: BuildVmCpusMacMini = {badv!r} is refused (a whole number above 0)", any("BuildVmCpusMacMini" in x for x in v), v)
    gpu = copy.deepcopy(spec)
    gpu["customProfiles"][0]["keys"]["GpuWiredReserveMbMacStudio"] = 8192
    expect("validate: GpuWiredReserveMbMacStudio = 8192 passes", not validate(gpu, "."), validate(gpu, "."))
    expect("validate: …and travels as a plist integer", build_plist(gpu["customProfiles"][0])["PayloadContent"][0].get("GpuWiredReserveMbMacStudio") == 8192)
    for badv in ("8192", True, 4095):
        gpu["customProfiles"][0]["keys"]["GpuWiredReserveMbMacStudio"] = badv
        v = validate(gpu, ".")
        expect(f"validate: GpuWiredReserveMbMacStudio = {badv!r} is refused (whole MiB, at least 4096)", any("GpuWiredReserveMbMacStudio" in x for x in v), v)
    cls = copy.deepcopy(spec)
    cls["customProfiles"][0]["keys"]["AcceptJobsMacStudio"] = False
    expect("validate: AcceptJobsMacStudio = false passes", not validate(cls, "."), validate(cls, "."))
    expect("validate: …and travels as a plist boolean", build_plist(cls["customProfiles"][0])["PayloadContent"][0].get("AcceptJobsMacStudio") is False)
    for badv in ("false", 0, None):
        cls["customProfiles"][0]["keys"]["AcceptJobsMacStudio"] = badv
        v = validate(cls, ".")
        expect(f"validate: AcceptJobsMacStudio = {badv!r} is refused (an explicit true/false)", any("AcceptJobsMacStudio" in x for x in v), v)
    cls["customProfiles"][0]["keys"]["AcceptJobsMacStudio"] = True
    cls["customProfiles"][0]["keys"].pop("AcceptJobsFrom", None)
    cls["customProfiles"][0]["keys"]["AcceptJobs"] = False
    expect("validate: AcceptJobsMacStudio = true without AcceptJobsFrom is refused", any("AcceptJobsMacStudio is true" in x for x in validate(cls, ".")), validate(cls, "."))
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

    # the fleet loader: the uploaded script is the loader + the pinned EMBEDDED bootstrap after the marker
    loader, boot = b"#!/bin/bash\necho loader\nreturn 0 2>/dev/null || exit 0\n", b"#!/bin/bash\necho bootstrap\n"
    ld = copy.deepcopy(spec)
    ld["scripts"][0]["source"].update(path="fleet-loader.sh", sha256=hashlib.sha256(loader).hexdigest(),
                                      embed={"repo": "acme/deploy", "path": "fleet-bootstrap.sh", "commit": "b" * 40, "sha256": hashlib.sha256(boot).hexdigest()})
    ld["customProfiles"][0]["keys"].update(BootstrapCommit="b" * 40, BootstrapSha256=hashlib.sha256(boot).hexdigest())
    expect("validate: a loader script with an embedded bootstrap and the profile's pin passes", not validate(ld, "."), validate(ld, "."))
    fetch2 = lambda src: loader if src["path"] == "fleet-loader.sh" else boot
    st9 = tenant()
    lines9 = []
    g9 = FakeGraph(st9)
    Reconcile(copy.deepcopy(ld), tempfile.gettempdir(), g9, apply=True, fetch=fetch2, token=tok, out=lines9.append).run()
    up = [base64.b64decode(w[2]["scriptContent"]) for w in g9.writes if w[1] == "beta/deviceManagement/deviceShellScripts"]
    expect("the loader is uploaded WITH its embedded bootstrap after the marker line", up == [loader + b"__FLEET_EMBEDDED_BOOTSTRAP__\n" + boot], [repr(up)])
    lines9b = []
    rc9 = Reconcile(copy.deepcopy(ld), tempfile.gettempdir(), FakeGraph(st9), fetch=fetch2, token=tok, out=lines9b.append).run()
    expect("…and the next check of the combined upload is clean (no drift forever on the combined sha256)",
           not any(ln.startswith("DRIFT") and "script" in ln for ln in lines9b), lines9b)
    noguard = copy.deepcopy(ld)
    lines10 = []
    st10 = tenant()
    bad_loader = b"#!/bin/bash\necho loader\n"
    noguard["scripts"][0]["source"]["sha256"] = hashlib.sha256(bad_loader).hexdigest()
    g10 = FakeGraph(st10)
    Reconcile(noguard, tempfile.gettempdir(), g10, apply=True, fetch=lambda src: bad_loader if src["path"] == "fleet-loader.sh" else boot,
              token=tok, out=lines10.append).run()
    expect("a host script that does not end in the loader's exit guard is never uploaded with an embed (it would run the bytes)",
           not any(w[1] == "beta/deviceManagement/deviceShellScripts" for w in g10.writes) and any("exit guard" in ln for ln in lines10), lines10)
    other = copy.deepcopy(ld)
    other["scripts"][0]["source"]["path"] = "some-other-script.sh"
    expect("validate: source.embed on a script other than the fleet loader is refused", any("fleet loader only" in x for x in validate(other, ".")), validate(other, "."))
    calls = []
    def counting(src):
        calls.append(src["path"]); return fetch2(src)
    r11 = Reconcile(copy.deepcopy(ld), tempfile.gettempdir(), FakeGraph(st9), fetch=counting, token=tok, out=lambda _l: None)
    r11.run()
    expect("the combined upload is fetched ONCE per run (the loader and the bootstrap one fetch each)", sorted(calls) == ["fleet-bootstrap.sh", "fleet-loader.sh"], [repr(calls)])
    half = copy.deepcopy(ld)
    del half["customProfiles"][0]["keys"]["BootstrapSha256"]
    expect("validate: a BootstrapCommit without its BootstrapSha256 is refused", any("go together" in x for x in validate(half, ".")), validate(half, "."))
    badpin = copy.deepcopy(ld)
    badpin["customProfiles"][0]["keys"]["BootstrapCommit"] = "main"
    expect("validate: a bootstrap pin that is a branch, not a commit, is refused", any("BootstrapCommit must be a full 40-hex" in x for x in validate(badpin, ".")), validate(badpin, "."))

    # one Mac's managed-device lookup failing never stops the others' sync
    st12 = copy.deepcopy(st5)
    st12["managedDevices"]["mx"] = {"id": "mx", "serialNumber": "X", "azureADDeviceId": "aad-x", "operatingSystem": "macOS"}
    st12["groups"][G1]["_members"] = ["d1", "dx"]
    class Flaky(FakeGraph):
        def list(self, path):
            if "managedDevices" in path and "aad-1" in urllib.parse.unquote(path):
                raise GraphError(503, "GET", "v1.0/deviceManagement/managedDevices", "transient")
            return super().list(path)
    g12, lines12 = Flaky(st12), []
    r12 = Reconcile(copy.deepcopy(spec), tempfile.gettempdir(), g12, token=tok, out=lines12.append, sync_only=True)
    r12.run()
    synced12 = [w[1].split("/")[3] for w in g12.writes if w[1].endswith("/syncDevice")]
    expect("device sync: one Mac's lookup failing (503) is an ERROR for THAT Mac and the others are still synced",
           synced12 == ["mx"] and any(ln.startswith("ERROR") and "lookup failed" in ln for ln in lines12), lines12)

    # the Apple Business Manager enrollment program (ADE): AWAITING without the token, then create / default / assign
    ep = {"token": {"tokenName": "Fleet — Apple Business Manager"},
          "profiles": [{"key": "fleetMacs", "platform": "macOS", "default": True, "displayName": "Fleet — Macs (ADE)",
                        "description": "d", "settings": {"requiresUserAuthentication": False, "supervisedModeEnabled": True,
                                                         "waitForDeviceConfiguredConfirmation": True, "isMandatory": True,
                                                         "profileRemovalDisabled": True, "appleIdDisabled": True,
                                                         "supportDepartment": "Fleet operations"},
                        "assign": {"devicesOf": ["studios"]}}]}
    ade = {**spec, "compliancePolicies": [], "enrollmentProgram": ep}
    expect("validate: a declared enrollment program passes", not validate(ade, "."), validate(ade, "."))
    base = copy.deepcopy(st)
    rc, lines, g = run(copy.deepcopy(base), False, ade)
    expect("ADE: no token of the declared name → AWAITING lines (token and profile), exit 0, nothing red, nothing written",
           rc == 0 and not g.writes and any(ln.startswith("AWAITING") and "awaiting the ABM token" in ln for ln in lines)
           and any(ln.startswith("AWAITING") and "enrollment profile fleetMacs" in ln for ln in lines)
           and not any(ln.startswith(("ERROR", "DRIFT")) for ln in lines), lines)
    rc, lines, g = run(copy.deepcopy(base), True, ade)
    expect("ADE: --apply without the token writes nothing to the enrollment program and stays green",
           rc == 0 and not any(DEP in w[1] for w in g.writes), lines)
    wrong_name = copy.deepcopy(base)
    wrong_name["dep"] = {"t0": {"id": "t0", "tokenName": "Some other token", "tokenExpirationDateTime": "2099-01-01T00:00:00Z"}}
    rc, lines, g = run(wrong_name, False, ade)
    expect("ADE (negative control): a token of ANOTHER name is not taken for the declared one — still AWAITING",
           rc == 0 and any(ln.startswith("AWAITING") for ln in lines), lines)

    def with_token(**over):
        t = copy.deepcopy(base)
        tokn = {"id": "aaaaaaaa-0000-4000-8000-000000000001", "tokenName": "Fleet — Apple Business Manager", "tokenType": "dep",
                "tokenExpirationDateTime": "2099-01-01T00:00:00Z", "lastSuccessfulSyncDateTime": "2026-11-20T06:00:00Z",
                "syncedDeviceCount": 1, "lastSyncErrorCode": 0,
                "_identities": [{"id": "i1", "serialNumber": "SERIAL0001", "platform": "macOS", "enrollmentState": "notContacted",
                                 "isSupervised": False, "requestedEnrollmentProfileId": None}]}
        tokn.update(over)
        t["dep"] = {tokn["id"]: tokn}
        return t
    st13 = with_token()
    rc, lines, g = run(st13, False, ade)
    expect("ADE: --check with the token reports it (expiry, last sync), the absent profile as drift, the serial ABM has not "
           "assigned as PENDING, and writes nothing",
           rc == 2 and not g.writes and any(ln.startswith("OK") and "expires 2099-01-01" in ln and "last sync 2026-11-20" in ln for ln in lines)
           and any(ln.startswith("DRIFT") and "enrollment profile fleetMacs" in ln and "absent" in ln for ln in lines), lines)
    rc, lines, g = run(st13, True, ade)
    tk = st13["dep"]["aaaaaaaa-0000-4000-8000-000000000001"]
    made = list(tk["_profiles"].values())
    expect("ADE: --apply creates the macOS profile with the declared settings, makes it the default, and assigns the serial",
           len(made) == 1 and made[0]["@odata.type"] == DEP_MACOS_T and made[0]["requiresUserAuthentication"] is False
           and made[0]["waitForDeviceConfiguredConfirmation"] is True and made[0]["isDefault"] is True
           and tk["_identities"][0]["requestedEnrollmentProfileId"] == made[0]["id"]
           and any(w[1].endswith("/setDefaultProfile") and w[2] is None for w in g.writes)
           and any(w[1].endswith("/updateDeviceProfileAssignment") and w[2] == {"deviceIds": ["SERIAL0001"]} for w in g.writes), lines)
    rc, lines, g = run(st13, False, ade)
    expect("ADE: the next --check is clean apart from the serial ABM has not assigned (PENDING), exit 0",
           rc == 0 and not g.writes and not any(ln.startswith("DRIFT") for ln in lines)
           and any(ln.startswith("OK") and "the token's default macOS profile" in ln for ln in lines)
           and any(ln.startswith("OK") and "enrollment assignment studio-1" in ln for ln in lines)
           and any(ln.startswith("PENDING") and "studio-9" in ln and "Apple Business Manager" in ln for ln in lines), lines)
    pid = made[0]["id"]
    tk["_profiles"][pid]["registrationDisabled"] = None
    tk["_profiles"][pid].pop("enableAuthenticationViaCompanyPortal", None)
    nl = run(st13, False, {**ade, "enrollmentProgram": {**ep, "profiles": [dict(ep["profiles"][0], settings=dict(
        ep["profiles"][0]["settings"], registrationDisabled=False, enableAuthenticationViaCompanyPortal=False))]}})[1]
    expect("ADE: a boolean Graph reads back as null (or omits) matches a declared false — never drift forever",
           not any(ln.startswith("DRIFT") and ("registrationDisabled" in ln or "enableAuthenticationViaCompanyPortal" in ln) for ln in nl), nl)
    tk["_profiles"][pid]["requiresUserAuthentication"] = True
    tk["_profiles"]["other"] = {"id": "other", "@odata.type": DEP_MACOS_T, "displayName": "Someone's", "isDefault": True}
    tk["_profiles"][pid]["isDefault"] = False
    tk["_identities"][0]["requestedEnrollmentProfileId"] = "other"
    rc, lines, g = run(st13, False, ade)
    expect("ADE drift: a changed setting, a lost default and a serial moved to another profile are each named",
           rc == 2 and any("requiresUserAuthentication live=True spec=False" in ln for ln in lines)
           and any("not the token's default" in ln for ln in lines) and any("studio-1: profile live=other" in ln for ln in lines), lines)
    class LyingAssign(FakeGraph):
        def post(self, path, body):
            if path.endswith("/updateDeviceProfileAssignment"):
                self.writes.append(("POST", path, body))
                return {}   # Graph answers 204, nothing changes
            return super().post(path, body)
    lg, ll = LyingAssign(st13), []
    Reconcile(copy.deepcopy(ade), tempfile.gettempdir(), lg, apply=True, fetch=lambda s_: script, token=tok, out=ll.append).run()
    rc, lines, g = run(st13, False, ade)
    expect("ADE (negative control): an assignment Graph accepted but did not record is STILL drift at the next --check "
           "(the check reads the live assignment, never trusts the apply)",
           rc == 2 and any(ln.startswith("DRIFT") and "studio-1: profile live=other" in ln for ln in lines)
           and not any("requiresUserAuthentication" in ln and ln.startswith("DRIFT") for ln in lines), lines + ll)
    run(st13, True, ade)
    rc, lines, g = run(st13, False, ade)
    expect("ADE drift: --apply restores all three, and the next check is clean", rc == 0
           and tk["_profiles"]["other"]["isDefault"] is False and tk["_identities"][0]["requestedEnrollmentProfileId"] == pid, lines)
    rc, lines, g = run(with_token(tokenExpirationDateTime="2020-01-01T00:00:00Z"), False, ade)
    expect("ADE: an EXPIRED token is drift no --apply repairs (renewal is the owner's act)",
           rc == 2 and any(ln.startswith("DRIFT") and "EXPIRED" in ln and "renew" in ln for ln in lines), lines)
    soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rc, lines, g = run(with_token(tokenExpirationDateTime=soon), False, ade)
    expect("ADE: a token expiring within renewWarnDays is PENDING with the days left, never red on its own",
           any(ln.startswith("PENDING") and "renew within" in ln for ln in lines), lines)
    class Denied(FakeGraph):
        def list(self, path):
            if path.startswith(DEP):
                raise GraphError(403, "GET", path, "forbidden")
            return super().list(path)
    dl = []
    rc = Reconcile(copy.deepcopy(ade), tempfile.gettempdir(), Denied(copy.deepcopy(base)), token=tok, out=dl.append).run()
    expect("ADE: a credential that cannot read the enrollment program is an ERROR naming the permission, never AWAITING",
           rc == 1 and any(ln.startswith("ERROR") and "DeviceManagementServiceConfig.Read.All" in ln for ln in dl)
           and not any(ln.startswith("AWAITING") for ln in dl), dl)
    for label, mut, needle in (
            ("a local admin password (a secret)", lambda e: e["profiles"][0]["settings"].__setitem__("adminAccountPassword", "x"), "secret"),
            ("an unknown setting", lambda e: e["profiles"][0]["settings"].__setitem__("supervisedModeEnabledd", True), "knows"),
            ("a non-boolean boolean", lambda e: e["profiles"][0]["settings"].__setitem__("isMandatory", "yes"), "true/false"),
            ("isDefault in settings", lambda e: e["profiles"][0]["settings"].__setitem__("isDefault", True), "default: true"),
            ("an undeclared group", lambda e: e["profiles"][0]["assign"].__setitem__("devicesOf", ["nowhere"]), "nowhere"),
            ("two default profiles", lambda e: e["profiles"].append(dict(copy.deepcopy(e["profiles"][0]), key="second")), "more than one"),
            ("no token name", lambda e: e["token"].pop("tokenName"), "tokenName")):
        b = copy.deepcopy(ade)
        mut(b["enrollmentProgram"])
        v = validate(b, ".")
        expect(f"validate: an enrollment program with {label} is refused", any(needle in x for x in v), v)

    # ── Platform SSO / PPPC payloads, user members, apps (MeshWeaver.Deploy README "Mac admins through an Entra group",
    #    "Apps") ────────────────────────────────────────────────────────────────────────────────────────────────────
    def prof(ptype, keys):
        return {"key": "p", "displayName": "P", "deploymentChannel": "deviceChannel",
                "plist": {"payloadType": ptype, "identifier": "i", "uuid": "U", "innerIdentifier": "ii", "innerUuid": "IU",
                          "displayName": "P"}, "keys": keys, "assign": {"include": ["studios"]}}
    psso = {"ExtensionIdentifier": "com.microsoft.CompanyPortalMac.ssoextension", "TeamIdentifier": "UBF8T346G9",
            "Type": "Redirect", "URLs": ["https://login.microsoftonline.com", "https://login.microsoft.com"],
            "RegistrationToken": "{{DEVICEREGISTRATION}}",
            "PlatformSSO": {"AuthenticationMethod": "Password", "UseSharedDeviceKeys": True, "EnableCreateUserAtLogin": True,
                            "NewUserAuthorizationMode": "Groups", "UserAuthorizationMode": "Groups",
                            "AdministratorGroups": ["Fleet — Mac Admins"]}}
    ok_sp = {**spec, "customProfiles": [prof(SSO_T, psso)]}
    expect("validate: a Platform SSO payload with Groups authorization and its administrator group is accepted",
           not validate(ok_sp, "."), validate(ok_sp, "."))
    bad = copy.deepcopy(psso); del bad["PlatformSSO"]["AdministratorGroups"]
    expect("validate: Groups authorization with no AdministratorGroups is refused (every user would be standard)",
           any("needs AdministratorGroups" in x for x in validate({**spec, "customProfiles": [prof(SSO_T, bad)]}, ".")))
    bad = copy.deepcopy(psso); bad["PlatformSSO"]["AuthenticationMethod"] = "UserSecureEnclaveKey"
    expect("validate: creating the account at the login window with the Secure Enclave key method is refused",
           any("EnableCreateUserAtLogin" in x for x in validate({**spec, "customProfiles": [prof(SSO_T, bad)]}, ".")))
    bad = copy.deepcopy(psso); bad["PlatformSSO"]["UseSharedDeviceKeys"] = False
    expect("validate: creating the account at the login window without shared device keys is refused",
           any("UseSharedDeviceKeys" in x for x in validate({**spec, "customProfiles": [prof(SSO_T, bad)]}, ".")))
    bad = copy.deepcopy(psso); bad["PlatformSSO"]["UserAuthorizationMode"] = "Administrator"
    expect("validate: an authorization mode macOS does not know is refused",
           any("UserAuthorizationMode must be one of" in x for x in validate({**spec, "customProfiles": [prof(SSO_T, bad)]}, ".")))
    built = build_plist(prof(SSO_T, psso))["PayloadContent"][0]
    expect("a non-com.memex.local payload is written VERBATIM: URLs stay an array, PlatformSSO a dictionary",
           built["URLs"] == psso["URLs"] and built["PlatformSSO"]["AdministratorGroups"] == ["Fleet — Mac Admins"], [repr(built)])
    pppc = {"Services": {"Accessibility": [{"Identifier": "com.example.host", "IdentifierType": "bundleID",
                                             "CodeRequirement": "identifier \"com.example.host\"", "Authorization": "Allow"}],
                         "ScreenCapture": [{"Identifier": "com.example.host", "IdentifierType": "bundleID",
                                            "CodeRequirement": "identifier \"com.example.host\"",
                                            "Authorization": "AllowStandardUserToSetSystemService"}]}}
    expect("validate: a PPPC payload granting Accessibility and letting a standard user approve Screen Recording is accepted",
           not validate({**spec, "customProfiles": [prof(TCC_T, pppc)]}, "."))
    bad = copy.deepcopy(pppc); bad["Services"]["ScreenCapture"][0]["Authorization"] = "Allow"
    expect("validate: a PPPC payload that claims to ALLOW Screen Recording is refused (MDM cannot pre-grant it)",
           any("ScreenCapture cannot be pre-granted" in x for x in validate({**spec, "customProfiles": [prof(TCC_T, bad)]}, ".")))
    bad = copy.deepcopy(pppc); del bad["Services"]["Accessibility"][0]["CodeRequirement"]
    expect("validate: a PPPC entry without its designated requirement is refused",
           any("CodeRequirement is required" in x for x in validate({**spec, "customProfiles": [prof(TCC_T, bad)]}, ".")))

    GA = "55555555-5555-4555-8555-555555555555"
    U1, U2 = "66666666-6666-4666-8666-666666666661", "66666666-6666-4666-8666-666666666662"
    usp = {**spec, "groups": {**spec["groups"], "admins": {"id": GA, "displayName": "Fleet — Mac Admins", "users": [
        {"upn": "jane.roe@example.com", "id": U1}, {"upn": "john.doe@example.com"}]}}}
    expect("validate: a group of users is accepted", not validate(usp, "."), validate(usp, "."))
    bad = copy.deepcopy(usp); bad["groups"]["admins"]["devices"] = [{"serial": "S", "name": "n"}]
    expect("validate: a group that declares users AND devices is refused", any("users OR devices" in x for x in validate(bad, ".")))
    stu = tenant(); stu["apps"] = {}
    stu["groups"][GA] = {"id": GA, "displayName": "Fleet — Mac Admins", "groupTypes": [], "_members": []}
    stu["devices"][U1] = {"id": U1, "displayName": "Jane Roe"}
    rc, lines, g = run(stu, False, usp)
    expect("user members: a declared user not in the group is DRIFT; one without a declared id is PENDING (no UPN lookup)",
           any(ln.startswith("DRIFT") and "user jane.roe@example.com in admins" in ln for ln in lines)
           and any(ln.startswith("PENDING") and "john.doe@example.com" in ln and "az ad user show" in ln for ln in lines), lines)
    rc, lines, g = run(stu, True, usp)
    expect("user members: apply adds the user by its directory object id", U1 in stu["groups"][GA]["_members"], lines)
    rc, lines, g = run(stu, False, usp)
    expect("user members: then OK", any(ln.startswith("OK") and "user jane.roe@example.com in admins" in ln for ln in lines), lines)

    tmpl = (b"#!/bin/bash\nASSIGNMENT_ID='@@ASSIGNMENT_ID@@'\nPREFIX='@@ALIAS_PREFIX@@'\n"
            b"while read -r s n; do :; done <<'EOF'\n@@DEVICE_ALIASES@@\nEOF\n")
    tsha = hashlib.sha256(tmpl).hexdigest()
    app = {"key": "remoteHost", "displayName": "Remote Host — Example", "type": "macOSPkgApp", "intent": "required",
           "properties": {"primaryBundleId": "com.example.host"},
           "postInstallScript": {"source": {"repo": "acme/deploy", "path": "host-postinstall.sh", "commit": "b" * 40, "sha256": tsha},
                                 "params": {"ALIAS_PREFIX": "example-"}, "aliasesFrom": "studios",
                                 "secrets": {"ASSIGNMENT_ID": "EXAMPLE_ASSIGNMENT_ID"}},
           "assign": {"include": ["studios"]}}
    asp = {**spec, "apps": [app]}
    expect("validate: an app with a pinned post-install template and an environment secret is accepted", not validate(asp, "."), validate(asp, "."))
    bad = copy.deepcopy(asp); bad["apps"][0]["postInstallScript"]["secrets"]["ASSIGNMENT_ID"] = "0001CoABChBkZXZpY2U"
    expect("validate: a secret written INTO the spec (not an environment variable name) is refused",
           any("ENVIRONMENT VARIABLE" in x for x in validate(bad, ".")))
    def run_app(state, apply, env=None):
        g, lines = FakeGraph(state), []
        old = {k: os.environ.get(k) for k in ("EXAMPLE_ASSIGNMENT_ID",)}
        if env is None:
            os.environ.pop("EXAMPLE_ASSIGNMENT_ID", None)
        else:
            os.environ["EXAMPLE_ASSIGNMENT_ID"] = env
        try:
            rc = Reconcile(copy.deepcopy(asp), tempfile.gettempdir(), g, apply=apply, fetch=lambda s: tmpl, token=tok, out=lines.append).run()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        return rc, [ln for ln in lines if "app remoteHost" in ln], g
    sta = tenant(); sta["apps"] = {}
    rc, lines, g = run_app(sta, True, "SECRET-1")
    expect("apps: an app not uploaded yet is AWAITING (a person uploads the package), and nothing is written for it",
           any(ln.startswith("AWAITING") and "macOS app (PKG)" in ln for ln in lines)
           and not any("mobileApps" in w[1] for w in g.writes), lines)
    sta["apps"]["a1"] = {"id": "a1", "@odata.type": "#microsoft.graph.macOSPkgApp", "displayName": "Remote Host — Example",
                         "primaryBundleId": "com.example.host",
                         "_assign": [{"target": {"@odata.type": INCLUDE_T, "groupId": G1}, "intent": "available"}]}
    rc, lines, g = run_app(sta, False)
    expect("apps: --check names the missing post-install script and the wrong intent, needs no secret, writes nothing",
           any(ln.startswith("DRIFT") and "post-install script differs" in ln for ln in lines)
           and any(ln.startswith("DRIFT") and "another intent" in ln for ln in lines) and not g.writes, lines)
    rc, lines, g = run_app(sta, True)
    expect("apps: --apply without the secret in the environment is an ERROR naming the variable, and writes no script",
           any(ln.startswith("ERROR") and "EXAMPLE_ASSIGNMENT_ID" in ln for ln in lines)
           and not any(w[0] == "PATCH" and "mobileApps" in w[1] for w in g.writes), lines)
    rc, lines, g = run_app(sta, True, "SECRET-1")
    got = base64.b64decode(sta["apps"]["a1"]["postInstallScript"]["scriptContent"]).decode()
    expect("apps: --apply writes the rendered script — the secret from the environment, the prefix, one alias line per Mac — "
           "and never prints the secret",
           "ASSIGNMENT_ID='SECRET-1'" in got and "PREFIX='example-'" in got and "SERIAL0001 studio-1\nSERIAL0009 studio-9" in got
           and not any("SECRET-1" in ln for ln in lines), lines + [got])
    expect("apps: --apply rewrites the declared target's intent to required",
           sta["apps"]["a1"]["_assign"] == [{"target": {"@odata.type": INCLUDE_T, "groupId": G1}, "intent": "required"}], lines)
    rc, lines, g = run_app(sta, False)
    expect("apps: then --check is OK without the secret (the live value is compared as its placeholder)",
           any(ln.startswith("OK") and "post-install script" in ln for ln in lines)
           and not any(ln.startswith(("DRIFT", "ERROR")) for ln in lines), lines)
    sta["apps"]["a1"]["_assign"].append({"target": {"@odata.type": INCLUDE_T, "groupId": G2}, "intent": "available"})
    sta["apps"]["a1"]["postInstallScript"]["scriptContent"] = base64.b64encode(got.replace("PREFIX='example-'", "PREFIX='x'").encode()).decode()
    rc, lines, g = run_app(sta, True, "SECRET-1")
    expect("apps: an edited live script is drift and rewritten; an undeclared assignment is kept with ITS intent",
           {"target": {"@odata.type": INCLUDE_T, "groupId": G2}, "intent": "available"} in sta["apps"]["a1"]["_assign"]
           and "PREFIX='example-'" in base64.b64decode(sta["apps"]["a1"]["postInstallScript"]["scriptContent"]).decode(), lines)

    real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teamviewer", "teamviewer-host-postinstall.sh")
    if os.path.exists(real):
        rt = open(real, "rb").read()
        rsc = {"params": {"ALIAS_PREFIX": "ACME-"}, "aliasesFrom": "studios", "secrets": {"ASSIGNMENT_ID": "X"}}
        filled = render_script(rt, rsc, spec, {"ASSIGNMENT_ID": "0001-secret"})
        syn = subprocess.run(["bash", "-n"], input=filled, capture_output=True)
        expect("the committed TeamViewer template renders to valid bash with no placeholder left, and its secret line "
               "normalises back to the placeholder",
               syn.returncode == 0 and b"@@" not in filled.replace(b"''|@@*)", b"")
               and normalise_script(filled, rsc) == render_script(rt, rsc, spec, None), [syn.stderr.decode()])
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
