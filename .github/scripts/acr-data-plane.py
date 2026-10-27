#!/usr/bin/env python3
"""acr-data-plane.py — reach the platform registry through its DATA PLANE only, as an identity that
holds a role on the registry resource and nothing at subscription scope.

WHY. The publishing identity of `build-host.yml` holds exactly one role: AcrPush on the registry
resource (least privilege, by design). It has no subscription, so `azure/login` signs it in as a
TENANT-LEVEL account (`allow-no-subscriptions: true`). The registry commands of the Azure CLI do
not work for such an account: before they touch the registry they look the registry up through
ARM (azure-cli 2.90 `acr/_docker_utils.py` `_get_credentials` → `get_registry_by_name`, a resource
list in "the current subscription"), and they fall back to the data plane only on a
`ResourceNotFound` or a "please run az login" error. A tenant-level account has no current
subscription, and naming one with `--subscription` fails earlier still ("Subscription … not
found": the profile holds no subscription, so no credential can be issued for it) — both are
re-raised, never fallen back from. So this lane never asks ARM anything: it takes an Entra token
for the registry audience, exchanges it at the registry's own token server, and talks to the
registry with that.

    acr-data-plane.py login     --registry <login server> --tenant <tenant id>
        docker login <login server> with an ACR refresh token (bounded transport retry:
        5 attempts, 5 s → 40 s, the same idiom as the platform's acr-login.sh).
    acr-data-plane.py manifests --registry <login server> --tenant <tenant id> --repository <name> --top N
        the newest N manifests of <name>, newest first, as a JSON array on stdout — the rows
        `az acr manifest list-metadata --orderby time_desc -o json` prints (each carries `tags`).
    acr-data-plane.py check-workflow <workflow.yml>
        RED unless every azure/login step signs in with `allow-no-subscriptions: true` and names no
        `subscription-id`, and nothing in the workflow runs an `az acr` command or the platform's
        `acr-login.sh` (both take the ARM lookup above).
    acr-data-plane.py --self-test

Nothing here prints a token. The refresh token reaches docker on stdin only.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ACR_AUDIENCE = "https://containerregistry.azure.net"
# The user name the registry expects with a refresh token from /oauth2/exchange.
REFRESH_TOKEN_USER = "00000000-0000-0000-0000-000000000000"
ATTEMPTS = 5
PAGE = 100


class Refusal(Exception):
    pass


def _login_server(value: str) -> str:
    v = (value or "").strip().lower()
    # A login server, never a bare registry name: a name is what sends the CLI to ARM.
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9-]+)+", v):
        raise Refusal(f"--registry must be the registry's login server (e.g. myregistry.azurecr.io), got {value!r}")
    return v


def _tenant(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise Refusal("--tenant is empty — the tenant the identity signed in to is required")
    return v


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _get_json(url: str, token: str) -> tuple[object, str | None]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode()), r.headers.get("Link")


class Io:
    """The four edges this script touches — HTTP POST, HTTP GET, a process, a sleep — so the
    self-test drives every path OFFLINE with fakes (the same seam build-run.py uses)."""

    def __init__(self, post=_post_form, get=_get_json, run=subprocess.run, sleep=time.sleep):
        self.post, self.get, self.run, self.sleep = post, get, run, sleep


def entra_token(tenant: str, io: Io) -> str:
    out = io.run(["az", "account", "get-access-token", "--resource", ACR_AUDIENCE, "--tenant", tenant,
                  "--query", "accessToken", "-o", "tsv", "--only-show-errors"],
                 check=True, capture_output=True, text=True).stdout.strip()
    if not out:
        raise Refusal(f"az account get-access-token returned no token for {ACR_AUDIENCE}")
    return out


def refresh_token(server: str, tenant: str, io: Io) -> str:
    body = io.post(f"https://{server}/oauth2/exchange", {
        "grant_type": "access_token", "service": server, "tenant": tenant, "access_token": entra_token(tenant, io)})
    tok = body.get("refresh_token") if isinstance(body, dict) else None
    if not tok:
        raise Refusal(f"{server}/oauth2/exchange answered no refresh_token")
    return tok


def access_token(server: str, refresh: str, scope: str, io: Io) -> str:
    body = io.post(f"https://{server}/oauth2/token", {
        "grant_type": "refresh_token", "service": server, "scope": scope, "refresh_token": refresh})
    tok = body.get("access_token") if isinstance(body, dict) else None
    if not tok:
        raise Refusal(f"{server}/oauth2/token answered no access_token for scope {scope}")
    return tok


def next_link(header: str | None) -> str | None:
    """The `rel="next"` target of an RFC 8288 Link header (the registry pages manifests with it)."""
    for part in (header or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;(.*)$', part)
        if m and re.search(r'rel="?next"?', m.group(2)):
            return m.group(1)
    return None


def list_manifests(server: str, tenant: str, repository: str, top: int, io: Io) -> list:
    if not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository or ""):
        raise Refusal(f"--repository is not a repository name: {repository!r}")
    if top < 1:
        raise Refusal(f"--top must be positive, got {top}")
    token = access_token(server, refresh_token(server, tenant, io), f"repository:{repository}:metadata_read", io)
    rows: list = []
    url: str | None = f"/acr/v1/{repository}/_manifests?orderby=timedesc&n={min(top, PAGE)}"
    while url and len(rows) < top:
        full = url if url.startswith("https://") else f"https://{server}{url}"
        page, link = io.get(full, token)
        batch = page.get("manifests") if isinstance(page, dict) else None
        if not isinstance(batch, list):
            raise Refusal(f"{server} answered no `manifests` list for {repository}")
        rows.extend(batch)
        url = next_link(link)
    return rows[:top]


def docker_login(server: str, tenant: str, io: Io) -> None:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            tok = refresh_token(server, tenant, io)
            io.run(["docker", "login", server, "--username", REFRESH_TOKEN_USER, "--password-stdin"],
                   input=tok, text=True, check=True, capture_output=True)
            if attempt > 1:
                print(f"acr-data-plane: reached {server} on attempt {attempt}")
            print(f"acr-data-plane: docker is signed in to {server} (data plane, no ARM lookup)")
            return
        except (subprocess.CalledProcessError, urllib.error.URLError, OSError, ValueError, Refusal) as e:
            detail = getattr(e, "stderr", None) or str(e)
            print(f"acr-data-plane: attempt {attempt} failed: {detail.strip()}", file=sys.stderr)
            if attempt < ATTEMPTS:
                io.sleep(5 * 2 ** (attempt - 1))
    raise Refusal(f"could not sign docker in to {server} after {ATTEMPTS} attempts")


# ───────────────────────────── the workflow invariant ─────────────────────────────

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _code(line: str) -> str:
    s = line.strip()
    return "" if s.startswith("#") else line


def _with_mapping(step: list[str], dash: int) -> dict:
    """The step's `with:` block as {key: raw value} — ONLY the action's inputs. A key under `env:`
    or anywhere else in the step is not an input, so it never counts. A flow mapping
    (`with: {…}`) is not read, so it cannot satisfy the check."""
    for k, line in enumerate(step):
        c = _code(line)
        m = re.match(r"^(\s*)(?:-\s+)?with:\s*(#.*)?$", c)
        if not m or _indent(c.replace("- ", "  ", 1)) <= dash:
            continue
        key_indent = _indent(c.replace("- ", "  ", 1))
        out: dict = {}
        for x in step[k + 1:]:
            cx = _code(x)
            if not cx.strip():
                continue
            if _indent(cx) <= key_indent:
                break
            mm = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.*?)\s*(#.*)?$", cx)
            if mm:
                out[mm.group(1)] = mm.group(2)
        return out
    return {}


def check_workflow(text: str) -> list[str]:
    lines = text.splitlines()
    problems: list[str] = []
    logins = 0
    for i, line in enumerate(lines):
        # A quoted ref (`uses: "azure/login@v3"`, `'azure/login@v3'`) is the same action. Plain text,
        # not a YAML parser: the test job runs on the runner's bare python3, which has no PyYAML.
        if not re.match(r"""^\s*(?:-\s+)?["']?uses["']?:\s*["']?azure/login@""", _code(line)):
            continue
        logins += 1
        start = i
        while start >= 0 and not re.match(r"^\s*-\s", lines[start]):
            start -= 1
        if start < 0:
            problems.append(f"line {i + 1}: an azure/login `uses:` outside any step")
            continue
        dash = _indent(lines[start])
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > dash):
            end += 1
        where = f"the azure/login step at line {start + 1}"
        inputs = _with_mapping(lines[start:end], dash)
        if inputs.get("allow-no-subscriptions", "").strip("'\"").lower() != "true":
            problems.append(f"{where} lacks `allow-no-subscriptions: true` in its `with:` — a resource-scoped identity has no subscription to sign in to")
        if "subscription-id" in inputs:
            problems.append(f"{where} names a `subscription-id` — the identity holds no subscription")
    if logins == 0:
        problems.append("no azure/login step found — the check has nothing to hold (wrong file?)")
    for i, line in enumerate(lines):
        c = _code(line)
        if re.search(r"(^|[\s;|&(`$])az\s+acr\s", c):
            problems.append(f"line {i + 1}: an `az acr` command — it looks the registry up through ARM first; use acr-data-plane.py")
        if "acr-login.sh" in c:
            problems.append(f"line {i + 1}: the platform's acr-login.sh runs `az acr login` (ARM lookup); use acr-data-plane.py login")
    return problems


# ───────────────────────────── self-test ─────────────────────────────

GOOD = """\
jobs:
  a:
    steps:
      - name: Azure login (OIDC)
        uses: azure/login@v3
        with:
          client-id: x
          tenant-id: y
          allow-no-subscriptions: true
      - name: list
        # comments may mention acr-login.sh and az acr freely
        run: python3 acr-data-plane.py manifests --registry r.azurecr.io --tenant t --repository a --top 3
      - uses: azure/login@v3
        with:
          client-id: x
          tenant-id: y
          allow-no-subscriptions: "true"
"""


def self_test() -> int:
    fails = 0

    def check(name, fn, want):
        nonlocal fails
        try:
            got = fn()
            ok = want(got)
        except Exception as e:  # a crash is a failure unless the case expects a refusal
            got, ok = f"{type(e).__name__}: {e}", want is REFUSED and isinstance(e, Refusal)
        print(f"  {'ok ' if ok else 'FAIL'} {name}" + ("" if ok else f" — got {got!r}"))
        fails += 0 if ok else 1

    REFUSED = object()
    check("the good workflow passes (positive control)", lambda: check_workflow(GOOD), lambda p: p == [])
    check("a login WITHOUT allow-no-subscriptions is red (negative control)",
          lambda: check_workflow(GOOD.replace("          allow-no-subscriptions: true\n", "", 1)),
          lambda p: len(p) == 1 and "allow-no-subscriptions" in p[0])
    check("allow-no-subscriptions: false is red",
          lambda: check_workflow(GOOD.replace("allow-no-subscriptions: true", "allow-no-subscriptions: false", 1)),
          lambda p: len(p) == 1 and "allow-no-subscriptions" in p[0])
    check("a commented-out allow-no-subscriptions does not count",
          lambda: check_workflow(GOOD.replace("          allow-no-subscriptions: true", "          # allow-no-subscriptions: true", 1)),
          lambda p: len(p) == 1)
    check("the flag on a NEIGHBOURING step does not satisfy a login",
          lambda: check_workflow(GOOD.replace("          allow-no-subscriptions: true\n", "", 1).replace(
              "      - name: list\n", "      - name: list\n        with:\n          allow-no-subscriptions: true\n", 1)),
          lambda p: len(p) == 1 and "line 4" in p[0])
    check("a subscription-id on a login is red",
          lambda: check_workflow(GOOD.replace("          tenant-id: y\n", "          tenant-id: y\n          subscription-id: z\n", 1)),
          lambda p: len(p) == 1 and "subscription-id" in p[0])
    check("an `az acr` command is red",
          lambda: check_workflow(GOOD + "      - run: az acr manifest list-metadata --registry r --name a\n"),
          lambda p: len(p) == 1 and "az acr" in p[0])
    check("an `az acr` inside a multi-line run is red",
          lambda: check_workflow(GOOD + "      - run: |\n          set -e\n          x=$(az acr login --name r --expose-token)\n"),
          lambda p: len(p) == 1 and "az acr" in p[0])
    check("the platform's acr-login.sh is red",
          lambda: check_workflow(GOOD + "      - run: bash platform/.github/scripts/acr-login.sh\n"),
          lambda p: len(p) == 1 and "acr-login.sh" in p[0])
    check("a file with no azure/login is red, never vacuously green",
          lambda: check_workflow("jobs: {}\n"), lambda p: len(p) == 1 and "no azure/login" in p[0])
    check("a Link header's next page is followed",
          lambda: next_link('</acr/v1/a/_manifests?last=sha256%3Aab&n=100&orderby=timedesc>; rel="next"'),
          lambda u: u == "/acr/v1/a/_manifests?last=sha256%3Aab&n=100&orderby=timedesc")
    check("no Link header means the last page", lambda: next_link(None), lambda u: u is None)
    check("a Link without rel=next is not followed", lambda: next_link('</x>; rel="prev"'), lambda u: u is None)
    check("a login server is accepted", lambda: _login_server("MyReg.azurecr.io"), lambda s: s == "myreg.azurecr.io")
    check("a bare registry NAME is refused (it would send the CLI to ARM)", lambda: _login_server("acme"), REFUSED)
    check("an empty tenant is refused", lambda: _tenant(" "), REFUSED)
    check("a non-repository name is refused before any network call",
          lambda: list_manifests("r.azurecr.io", "t", "../etc", 3, Io()), REFUSED)
    check("a QUOTED login with the flag false, beside a correct unquoted one, is red",
          lambda: check_workflow(GOOD + '      - name: quoted\n        uses: "azure/login@v3"\n        with:\n'
                                 '          client-id: x\n          allow-no-subscriptions: false\n'),
          lambda p: len(p) == 1 and "allow-no-subscriptions" in p[0])
    check("a single-quoted login ref is checked too",
          lambda: check_workflow(GOOD + "      - uses: 'azure/login@v3'\n        with:\n          client-id: x\n"),
          lambda p: len(p) == 1 and "allow-no-subscriptions" in p[0])
    check("a correctly flagged quoted login passes",
          lambda: check_workflow(GOOD + '      - uses: "azure/login@v3"\n        with:\n          allow-no-subscriptions: true\n'),
          lambda p: p == [])
    check("the flag under `env:` with no `with:` is red (inputs only)",
          lambda: check_workflow("jobs:\n  a:\n    steps:\n      - uses: azure/login@v3\n        env:\n"
                                 "          allow-no-subscriptions: true\n"),
          lambda p: len(p) == 1 and "allow-no-subscriptions" in p[0])
    check("the flag under `env:` while `with:` lacks it is red",
          lambda: check_workflow("jobs:\n  a:\n    steps:\n      - uses: azure/login@v3\n        with:\n"
                                 "          client-id: x\n        env:\n          allow-no-subscriptions: true\n"),
          lambda p: len(p) == 1 and "allow-no-subscriptions" in p[0])
    check("with: allow-no-subscriptions: 'false' (quoted) is red",
          lambda: check_workflow(GOOD.replace("allow-no-subscriptions: true", "allow-no-subscriptions: 'false'", 1)),
          lambda p: len(p) == 1 and "allow-no-subscriptions" in p[0])
    check("with: allow-no-subscriptions: 'true' (quoted) passes",
          lambda: check_workflow(GOOD.replace("allow-no-subscriptions: true", "allow-no-subscriptions: 'true'", 1)),
          lambda p: p == [])
    check("a subscription-id in `env:` is not an input and is not flagged",
          lambda: check_workflow(GOOD.replace("      - name: list\n", "        env:\n          subscription-id: z\n      - name: list\n", 1)),
          lambda p: p == [])

    # ── OFFLINE: the token exchange, the listing and the login, against fakes ──
    class Done:
        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake(posts, pages=None, runs=None, run_fail=0, exchange_ok=True):
        state = {"run_fail": run_fail}

        def post(url, form):
            posts.append((url, dict(form)))
            if url.endswith("/oauth2/exchange"):
                return {"refresh_token": "acr-refresh"} if exchange_ok else {}
            return {"access_token": "acr-access"}

        def get(url, token):
            posts.append(("GET", url, token))
            return pages.pop(0)

        def run(cmd, **kw):
            (runs if runs is not None else []).append((cmd, kw))
            if cmd[0] == "az":
                return Done("entra-token\n")
            if state["run_fail"] > 0:
                state["run_fail"] -= 1
                raise subprocess.CalledProcessError(1, cmd, stderr="registry blip")
            return Done()
        return Io(post=post, get=get, run=run, sleep=lambda s: sleeps.append(s))

    sleeps: list = []
    posts: list = []
    pages = [({"manifests": [{"digest": "d1", "tags": ["3.1.2", "main"]}, {"digest": "d2", "tags": []}]},
              '</acr/v1/mw-plugin-test/_manifests?last=d2&n=2&orderby=timedesc>; rel="next"'),
             ({"manifests": [{"digest": "d3", "tags": ["staging-abc-1"]}, {"digest": "d4", "tags": ["x"]}]},
              '</acr/v1/mw-plugin-test/_manifests?last=d4&n=2&orderby=timedesc>; rel="next"'),
             ({"manifests": [{"digest": "never"}]}, None)]
    rows = list_manifests("r.example.io", "tid", "mw-plugin-test", 3, fake(posts, pages))
    check("offline/the Entra token is exchanged with the registry's fields",
          lambda: posts[0], lambda p: p == ("https://r.example.io/oauth2/exchange", {
              "grant_type": "access_token", "service": "r.example.io", "tenant": "tid", "access_token": "entra-token"}))
    check("offline/the access token asks for metadata_read on the ONE repository",
          lambda: posts[1], lambda p: p == ("https://r.example.io/oauth2/token", {
              "grant_type": "refresh_token", "service": "r.example.io",
              "scope": "repository:mw-plugin-test:metadata_read", "refresh_token": "acr-refresh"}))
    check("offline/page 1 is newest-first with n bounded by --top, bearer the access token",
          lambda: posts[2], lambda p: p == ("GET", "https://r.example.io/acr/v1/mw-plugin-test/_manifests?orderby=timedesc&n=3", "acr-access"))
    check("offline/the Link's next page is followed on the same registry",
          lambda: posts[3][1], lambda u: u == "https://r.example.io/acr/v1/mw-plugin-test/_manifests?last=d2&n=2&orderby=timedesc")
    check("offline/--top truncates across pages and stops fetching once satisfied",
          lambda: ([r["digest"] for r in rows], len(pages)), lambda g: g == (["d1", "d2", "d3"], 1))
    check("offline/tags are preserved row by row",
          lambda: [r["tags"] for r in rows], lambda t: t == [["3.1.2", "main"], [], ["staging-abc-1"]])
    check("offline/a page with no `manifests` list is refused",
          lambda: list_manifests("r.example.io", "tid", "a", 3, fake([], [({"errors": []}, None)])), REFUSED)
    check("offline/an exchange answering no refresh_token is refused",
          lambda: list_manifests("r.example.io", "tid", "a", 3, fake([], [], exchange_ok=False)), REFUSED)

    runs: list = []
    sleeps.clear()
    docker_login("r.example.io", "tid", fake([], runs=runs, run_fail=2))
    logins = [r for r in runs if r[0][0] == "docker"]
    check("offline/docker_login retries a failed login and succeeds on attempt 3",
          lambda: (len(logins), sleeps), lambda g: g == (3, [5, 10]))
    check("offline/the refresh token reaches docker on STDIN as the empty-GUID user, never argv",
          lambda: logins[-1], lambda r: r[0] == ["docker", "login", "r.example.io", "--username", REFRESH_TOKEN_USER,
                                                  "--password-stdin"] and r[1].get("input") == "acr-refresh"
          and all("acr-refresh" not in a for a in r[0]))
    runs.clear()
    sleeps.clear()
    check("offline/docker_login exhausts its attempts and is refused",
          lambda: docker_login("r.example.io", "tid", fake([], runs=runs, run_fail=99)), REFUSED)
    check("offline/exhaustion = 5 attempts, 4 back-offs 5→40 s",
          lambda: (len([r for r in runs if r[0][0] == "docker"]), sleeps), lambda g: g == (5, [5, 10, 20, 40]))
    print(f"acr-data-plane self-test: {'PASS' if fails == 0 else f'{fails} FAILED'}")
    return 0 if fails == 0 else 1


def _arg(argv: list[str], name: str) -> str:
    if name not in argv or argv.index(name) + 1 >= len(argv):
        raise Refusal(f"{name} is required")
    return argv[argv.index(name) + 1]


def main(argv: list[str]) -> int:
    try:
        if argv == ["--self-test"]:
            return self_test()
        if len(argv) == 2 and argv[0] == "check-workflow":
            with open(argv[1], encoding="utf-8") as f:
                problems = check_workflow(f.read())
            for p in problems:
                print(f"::error file={argv[1]}::{p}")
            if not problems:
                print(f"{argv[1]}: every azure/login is tenant-level (allow-no-subscriptions, no subscription-id) "
                      "and the registry is reached through its data plane only")
            return 1 if problems else 0
        if argv and argv[0] == "login":
            docker_login(_login_server(_arg(argv, "--registry")), _tenant(_arg(argv, "--tenant")), Io())
            return 0
        if argv and argv[0] == "manifests":
            rows = list_manifests(_login_server(_arg(argv, "--registry")), _tenant(_arg(argv, "--tenant")),
                                  _arg(argv, "--repository"), int(_arg(argv, "--top")), Io())
            json.dump(rows, sys.stdout)
            sys.stdout.write("\n")
            return 0
        print(__doc__, file=sys.stderr)
        return 2
    except Refusal as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
