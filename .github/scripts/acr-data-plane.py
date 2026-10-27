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
    acr-data-plane.py retag     --registry <login server> --tenant <tenant id> --repository <name> --from <tag> --to <tag>
        put the manifest <name>:<from> names under <to> as well, BYTE FOR BYTE (GET by the source tag,
        PUT the same body under the target with the same media type), so both tags name ONE digest;
        prints that digest. `docker buildx imagetools create` is no substitute: it wraps a
        single-platform manifest in a new index, which is a different digest (the service-image lanes,
        e.g. ollama-image.yml, move their line tag this way without re-uploading ~25 GB).
    acr-data-plane.py check-workflow <workflow.yml>
        RED unless every azure/login step signs in with `allow-no-subscriptions: true` and names no
        `subscription-id`, and nothing in the workflow runs an `az acr` command or the platform's
        `acr-login.sh` (both take the ARM lookup above).
    acr-data-plane.py converge-line --registry <login server> --tenant <tenant id> --repository <name>
                              --line <line tag> --self <commit>
        ONE INVARIANT: <line> names the image of the NEWEST PUBLISHED <line>-<sha7> tag, "newest" in main's
        first-parent order. Every run recomputes that from the REGISTRY's own tag list (what was really
        published — a dispatch on any main commit, a multi-commit push's tip, included) and main's
        first-parent history (no path filter), writes the line by a byte-for-byte retag if it differs,
        and RE-CHECKS AFTER EVERY WRITE (re-list, re-fetch, read the line back) until nothing moved
        underneath. Whichever write lands LAST was followed by its writer's own re-check, so concurrent
        runs cannot leave the line on an older image, and an unpublished or failed newer build is simply
        not in the tag list. A tag whose commit is not on main's first-parent history (e.g. one written
        before the lane moved here) cannot be ordered and is ignored. Red: the tag list could not be
        read, this run's own tag is not in it, no tag can be ordered, or a 7-char prefix is ambiguous.
    acr-data-plane.py check-line-convergence <workflow.yml>
        RED unless the service-image lane moves its line tag ONLY through `converge-line` (never a raw
        `retag … --to "$LINE"`, which has no post-write re-check, and never a build or push), in a step
        that runs on EVERY publish run (no `if:`), with main's history checked out.
    acr-data-plane.py check-cache-handover <workflow.yml>
        For a lane that builds through a REGISTRY build cache (`--cache-to`): RED unless the cache is
        built only after the job confirmed this commit's image is NOT published (an `id: published`
        step earlier in the job, and the cache build gated on it — a rerun of a published commit skips
        straight to converging the line), and the image is published from an IMMUTABLE hand-over
        (`--cache-from` the cache job's digest-pinned `cache_ref`, refused unless it is `@sha256:`), never
        from the shared cache tag an overlapping run may re-point. A lane with no `--cache-to` passes.
    acr-data-plane.py --self-test

Nothing here prints a token. The refresh token reaches docker on stdin only.
"""
from __future__ import annotations

import hashlib
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


def _fetch(method: str, url: str, token: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    """One raw registry request: (status, lower-cased headers, body). An HTTP error status is
    RETURNED, never raised, so the caller decides what an answer means. A transport failure (no
    answer at all) still raises."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bearer {token}", **headers})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


class Io:
    """The edges this script touches — HTTP POST, HTTP GET, a raw registry request, a process, a
    sleep — so the self-test drives every path OFFLINE with fakes (the same seam build-run.py uses)."""

    def __init__(self, post=_post_form, get=_get_json, run=subprocess.run, sleep=time.sleep, fetch=_fetch):
        self.post, self.get, self.run, self.sleep, self.fetch = post, get, run, sleep, fetch


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


# Every manifest shape the registry may hold under a tag: a single image (OCI or Docker v2) or an
# index / manifest list. The registry answers with the stored media type, which the PUT repeats.
MANIFEST_ACCEPT = ",".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))
TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
REPOSITORY_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*")


def retag(server: str, tenant: str, repository: str, source: str, target: str, io: Io) -> str:
    """Name the manifest `<repository>:<source>` names under `<target>` as well, byte for byte, and
    return its digest. Refused — never a partial success — when the source is absent or unreadable,
    when the registry's digest does not match the bytes it served, or when the PUT is not stored
    under that same digest."""
    if not REPOSITORY_RE.fullmatch(repository or ""):
        raise Refusal(f"--repository is not a repository name: {repository!r}")
    for flag, tag in (("--from", source), ("--to", target)):
        if not TAG_RE.fullmatch(tag or ""):
            raise Refusal(f"{flag} is not a tag: {tag!r}")
    if source == target:
        raise Refusal(f"--from and --to are the same tag ({source!r}) — nothing to move")
    token = access_token(server, refresh_token(server, tenant, io), f"repository:{repository}:pull,push", io)
    base = f"https://{server}/v2/{repository}/manifests"
    status, headers, body = io.fetch("GET", f"{base}/{source}", token, {"Accept": MANIFEST_ACCEPT}, None)
    if status != 200:
        raise Refusal(f"GET {repository}:{source} answered HTTP {status} — the source manifest was not read")
    media = (headers.get("content-type") or "").split(";")[0].strip()
    if not media:
        raise Refusal(f"{repository}:{source} was served with no Content-Type — its media type is unknown")
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    served = headers.get("docker-content-digest")
    if served and served != digest:
        raise Refusal(f"{repository}:{source}: the registry names {served}, but the bytes it served hash to {digest}")
    status, headers, _ = io.fetch("PUT", f"{base}/{target}", token, {"Content-Type": media}, body)
    if status not in (200, 201):
        raise Refusal(f"PUT {repository}:{target} answered HTTP {status} — the tag was not moved")
    stored = headers.get("docker-content-digest")
    if stored and stored != digest:
        raise Refusal(f"{repository}:{target} was stored as {stored}, not {digest} — the tags name two manifests")
    return digest


def tag_digest(server: str, tenant: str, repository: str, tag: str, io: Io) -> str | None:
    """The digest `<repository>:<tag>` names, or None when the registry ANSWERED that it is absent (404).
    Any other answer is a Refusal: an unanswered lookup is never read as absence."""
    if not REPOSITORY_RE.fullmatch(repository or "") or not TAG_RE.fullmatch(tag or ""):
        raise Refusal(f"not a repository:tag: {repository!r}:{tag!r}")
    token = access_token(server, refresh_token(server, tenant, io), f"repository:{repository}:pull", io)
    status, headers, body = io.fetch("GET", f"https://{server}/v2/{repository}/manifests/{tag}", token,
                                     {"Accept": MANIFEST_ACCEPT}, None)
    if status == 404:
        return None
    if status != 200:
        raise Refusal(f"GET {repository}:{tag} answered HTTP {status} — presence not established")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def list_tags(server: str, tenant: str, repository: str, io: Io) -> list[str]:
    """Every tag of <repository>, from the registry's data plane (`/v2/<name>/tags/list`, Link-paged).
    Any answer but 200 is a Refusal: an unread listing never reads as "nothing published"."""
    if not REPOSITORY_RE.fullmatch(repository or ""):
        raise Refusal(f"--repository is not a repository name: {repository!r}")
    token = access_token(server, refresh_token(server, tenant, io), f"repository:{repository}:pull", io)
    tags: list[str] = []
    url: str | None = f"/v2/{repository}/tags/list?n={PAGE}"
    pages = 0
    while url:
        pages += 1
        if pages > 1000:
            raise Refusal(f"{repository}: the tag listing did not end after 1000 pages")
        full = url if url.startswith("https://") else f"https://{server}{url}"
        status, headers, body = io.fetch("GET", full, token, {"Accept": "application/json"}, None)
        if status != 200:
            raise Refusal(f"GET {repository} tags answered HTTP {status} — the published set is unknown")
        page = json.loads(body.decode() or "{}")
        batch = page.get("tags") if isinstance(page, dict) else None
        if not isinstance(batch, list):
            raise Refusal(f"{server} answered no `tags` list for {repository}")
        tags += [t for t in batch if isinstance(t, str)]
        url = next_link(headers.get("link"))
    return tags


def main_first_parent(io: Io) -> list[str]:
    """main's first-parent history, NEWEST FIRST, from a FRESH fetch — no path filter."""
    io.run(["git", "fetch", "--quiet", "origin", "main"], check=True, capture_output=True, text=True)
    out = io.run(["git", "rev-list", "--first-parent", "origin/main"],
                 check=True, capture_output=True, text=True).stdout.split()
    if not out or not all(re.fullmatch(r"[0-9a-f]{40}", c) for c in out):
        raise Refusal(f"could not read main's first-parent history (got {out[:3]!r})")
    return out


def newest_published(server: str, tenant: str, repository: str, line: str, self_sha: str,
                     io: Io) -> tuple[str, str]:
    """(commit, digest) of the newest PUBLISHED `<line>-<sha7>` tag, newest in main's first-parent order."""
    pattern = re.compile(rf"^{re.escape(line)}-([0-9a-f]{{7}})$")
    published = {m.group(1) for t in list_tags(server, tenant, repository, io) if (m := pattern.match(t))}
    if self_sha[:7] not in published:
        raise Refusal(f"{repository}:{line}-{self_sha[:7]} is this run's own image and the tag list does not have it")
    position: dict[str, int] = {}
    ambiguous: set[str] = set()
    order = main_first_parent(io)
    for i, commit in enumerate(order):
        k = commit[:7]
        if k in position:
            ambiguous.add(k)
        else:
            position[k] = i
    orderable = sorted((position[k], k) for k in published if k in position)
    if not orderable:
        raise Refusal(f"none of the {len(published)} published {line}-<sha7> tags of {repository} names a commit on "
                      "main's first-parent history — nothing to converge the line to")
    i, k = orderable[0]
    if k in ambiguous:
        raise Refusal(f"{line}-{k}: the 7-character prefix names more than one commit on main — refusing to guess")
    digest = tag_digest(server, tenant, repository, f"{line}-{k}", io)
    if digest is None:
        raise Refusal(f"{repository}:{line}-{k} is in the tag list but the registry says it is absent")
    unordered = len(published) - len(orderable)
    if unordered:
        print(f"converge-line: ignored {unordered} published {line}-<sha7> tag(s) whose commit is not on main's first-parent history")
    return order[i], digest


CONVERGE_ROUNDS = 6


def converge_line(server: str, tenant: str, repository: str, line: str, self_sha: str,
                  io: Io) -> tuple[str, str]:
    """(commit, digest) the line names once converged onto the newest PUBLISHED tag in main order."""
    if not re.fullmatch(r"[0-9a-f]{40}", self_sha or ""):
        raise Refusal(f"--self must be a 40-hex commit, got {self_sha!r}")
    if not TAG_RE.fullmatch(line or ""):
        raise Refusal(f"--line is not a tag: {line!r}")
    for round_ in range(1, CONVERGE_ROUNDS + 1):
        target, want = newest_published(server, tenant, repository, line, self_sha, io)
        if tag_digest(server, tenant, repository, line, io) != want:
            retag(server, tenant, repository, f"{line}-{target[:7]}", line, io)
        # The re-check that makes a stale write self-correcting: after OUR write, is the target still the
        # newest published tag in main order, and does the line still name it? Anything that moved
        # underneath (a newer tag published, another writer's PUT) sends us round again.
        again, _ = newest_published(server, tenant, repository, line, self_sha, io)
        if again == target and tag_digest(server, tenant, repository, line, io) == want:
            print(f"converge-line: {repository}:{line} = {want} ({line}-{target[:7]}, round {round_})")
            return target, want
    raise Refusal(f"{repository}:{line} did not settle in {CONVERGE_ROUNDS} rounds — something keeps moving it")


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


# ───────────────────────────── the line-convergence invariant ─────────────────────────────

# A WRITE of the line tag: docker push/build/tag, a buildx build or imagetools create, or a BuildKit
# `name=` output. A READ (`imagetools inspect`, the read-back) is not one.
LINE_TAG_PUSH = re.compile(
    r"""(docker\s+(push|build|tag)\b|buildx\s+(build|imagetools\s+create)\b|name=)[^\n]*:\$\{?LINE\}?["'\s,]""")


def _step_around(lines: list[str], i: int) -> tuple[int, int]:
    """(start, end) of the workflow step that holds line i: its `- ` line and everything indented below."""
    start = i
    while start >= 0 and not re.match(r"^\s*-\s", lines[start]):
        start -= 1
    if start < 0:
        return -1, -1
    dash = _indent(lines[start])
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > dash):
        end += 1
    return start, end


def check_line_convergence(text: str) -> list[str]:
    lines = text.splitlines()
    problems: list[str] = []
    steps = []
    for i, ln in enumerate(lines):
        c = _code(ln)
        if c.strip().startswith("#"):
            continue
        if "acr-data-plane.py converge-line" in c:
            start, end = _step_around(lines, i)
            steps.append((start, "\n".join(_code(x) for x in lines[start:end])))
        if "acr-data-plane.py retag" in c:
            start, end = _step_around(lines, i)
            body = "\n".join(_code(x) for x in lines[start:end])
            if re.search(r'--to\s+"?\$\{?LINE\}?"?', body):
                problems.append(f"line {i + 1}: a raw `retag … --to \"$LINE\"` moves the line with no re-check after the "
                                "write — an older run that pauses before its PUT overwrites a newer, verified line. "
                                "Use `converge-line`")
        if LINE_TAG_PUSH.search(c + " "):
            problems.append(f"line {i + 1}: the moving line tag is written by a build or push — it must only be "
                            "moved by `converge-line`, or a rebuild of an older commit moves it backwards")
    if not steps:
        problems.append("no step runs `acr-data-plane.py converge-line` — the line tag must be converged to main's "
                        "newest publication with a re-check after every write")
    for start, body in steps:
        where = f"the converge-line step at line {start + 1}"
        if re.search(r"^\s*(?:-\s+)?if:", body, flags=re.M):
            problems.append(f"{where} carries an `if:` — the line must converge on EVERY publish run, a rerun whose "
                            "image was already published included, or a failed line move is never completed")
        for flag, why in (('--line "$LINE"', "the line it moves"), ('--self "$GITHUB_SHA"', "this run's own commit")):
            if flag not in body:
                problems.append(f"{where} does not pass `{flag}` ({why})")
    if steps and not re.search(r"^\s*fetch-depth:\s*0\s*$", text, flags=re.M):
        problems.append("no checkout carries `fetch-depth: 0` — converge-line reads main's history")
    return problems


# ───────────────────────────── the cache hand-over invariant ─────────────────────────────

def _job_of(lines: list[str], i: int) -> int:
    """Index of the `  <job>:` line that owns line i (two-space indent under `jobs:`)."""
    j = i
    while j >= 0 and not re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[j]):
        j -= 1
    return j


CACHE_REF_CASE = re.compile(r'\bcase\s+"?\$\{?CACHE_REF\}?"?\s+in\b(?P<arms>.*?)\besac\b', re.S)


def digest_refusal_problem(body: str) -> str | None:
    """None when the step refuses a non-digest hand-over: a `case` on $CACHE_REF whose `*@sha256:*)` arm
    proceeds and whose catch-all `*)` arm EXITS NON-ZERO. Grepping for the pattern alone is not enough —
    an arm deleted, or `exit 1` turned into `true`, keeps the pattern and refuses nothing."""
    m = CACHE_REF_CASE.search(body)
    if not m:
        return "does not test the hand-over with `case \"$CACHE_REF\" in …` — nothing refuses a non-digest cache"
    arms = m.group("arms")
    if not re.search(r"(^|\s)\*@sha256:\*\)", arms):
        return "tests $CACHE_REF without a `*@sha256:*)` arm — the digest-pinned case is not recognised"
    catch_all = re.search(r"(^|\s)\*\)(?P<arm>.*?);;", arms, flags=re.S)
    if not catch_all:
        return "has no catch-all `*)` arm in its `case \"$CACHE_REF\"` — a non-digest hand-over is not refused"
    if not re.search(r"\bexit\s+[1-9][0-9]*\b", catch_all.group("arm")):
        return ("does not exit non-zero in the catch-all `*)` arm of its `case \"$CACHE_REF\"` — a non-digest "
                "hand-over would be built from anyway")
    return None


def _shell_commands(body: str) -> list[str]:
    """The step's shell, one entry per COMMAND: `\\`-continued lines and lines starting with `|` are joined."""
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if out and (out[-1].endswith("\\") or line.startswith("|")):
            out[-1] = out[-1].rstrip("\\").rstrip() + " " + line
        else:
            out.append(line)
    return out


def _redirect_target(command: str) -> str | None:
    m = re.search(r'>\s*"?([^"\s;|&]+)"?\s*$', command)
    return m.group(1) if m else None


def run_cache_import_problem(body: str) -> str | None:
    """None when the LISTED run caches actually reach the build: the tag listing is written to a file, a
    command derived from that file writes the selection (or the listing itself is read), a `while read -r V`
    loop over that selection APPENDS `--cache-from "type=registry,ref=…$V"` to `from`, and the build expands
    "${from[@]}". Presence of the listing and of the expansion alone proves nothing: delete the loop and
    both remain while only the shared tag is imported."""
    cmds = _shell_commands(body)
    listing = next((c for c in cmds if "acr-data-plane.py tags" in c), None)
    if not listing:
        return "no `acr-data-plane.py tags` listing"
    listed = _redirect_target(listing)
    if not listed:
        return "the tag listing is not written to a file the selection can read"
    sources = {listed}
    for c in cmds:
        if c is listing:
            continue
        target = _redirect_target(c)
        if target and any(src in c.rsplit(">", 1)[0] for src in sources):
            sources.add(target)
    loops = re.finditer(r"while\s+read\s+-r\s+(\w+)\s*;\s*do(?P<loop>.*?)\bdone\s*<\s*\"?([^\"\s]+)\"?",
                        "\n".join(cmds), flags=re.S)
    consuming = []
    for m in loops:
        var, feed = m.group(1), m.group(3)
        # The read variable EXACTLY: `${V}`, or `$V` not followed by an identifier character (so neither
        # `$V_other` nor `${V_other}` counts as consuming V).
        appends = re.search(rf'\bfrom\+=\(\s*--cache-from\s+"type=registry,ref=[^"]*'
                            rf'(?:\$\{{{var}\}}|\${var}(?![A-Za-z0-9_]))[^"]*"\s*\)',
                            m.group("loop"))
        if appends and feed in sources:
            consuming.append(m)
    if not consuming:
        return ("nothing appends the listed run caches to `from` (a `while read -r V … from+=(--cache-from "
                "\"type=registry,ref=…$V\") … done < <file derived from the listing>` loop)")
    if not re.search(r'docker\s+buildx\s+build\b[^\n]*"\$\{from\[@\]\}"', "\n".join(cmds)):
        return "the build does not expand \"${from[@]}\""
    return None


def check_cache_handover(text: str) -> list[str]:
    lines = text.splitlines()
    code = [_code(x) for x in lines]
    problems: list[str] = []
    exporters = [i for i, c in enumerate(code) if "--cache-to" in c and not c.strip().startswith("#")]
    if not exporters:
        return []
    seen_steps: set[int] = set()
    for i in exporters:
        start, end = _step_around(lines, i)
        body = "\n".join(code[start:end])
        job = _job_of(lines, i)
        if start in seen_steps:
            continue
        seen_steps.add(start)
        exports = len(re.findall(r"--cache-to\b", body))
        if exports > 1:
            problems.append(f"the cache build at line {start + 1} has {exports} `--cache-to` targets — BuildKit runs one "
                            "export per target CONCURRENTLY and each uploads every new blob (run 40559129837 paid the "
                            "~24 GB weights twice and was cut at 45 min). Export ONCE, then move other tags onto that "
                            "manifest with `acr-data-plane.py retag` (a manifest PUT)")
        flow = run_cache_import_problem(body)
        if flow:
            problems.append(f"the cache build at line {start + 1} does not import the COMPLETED run caches: {flow} — a "
                            "finished export whose job was cut before the shared tag moved would be lost (runs "
                            "40559129837 -> 40571543897 re-uploaded 24 GB the registry already held)")
        published = [k for k in range(job, start) if re.search(r"^\s*id:\s*published\s*$", code[k])]
        if not published:
            problems.append(f"the cache build at line {start + 1} is not preceded, in its job, by the `id: published` "
                            "existence check — a rerun of a published commit would rebuild the cache (and fail if "
                            "it was pruned) before it ever reaches the converge step")
        if not re.search(r"^\s*if:\s*steps\.published\.outputs\.exists\s*==\s*'false'\s*$", body, flags=re.M):
            problems.append(f"the cache build at line {start + 1} is not gated on `steps.published.outputs.exists == "
                            "'false'` — an already-published commit must skip cache construction entirely")
    publishers = [i for i, c in enumerate(code) if re.search(r'--output\s+"type=image,[^"]*push=true', c)]
    if not publishers:
        problems.append("a lane with a registry build cache publishes no image (`--output \"type=image,…push=true\"`) "
                        "— the hand-over has nothing to check (wrong file?)")
    for i in publishers:
        start, end = _step_around(lines, i)
        body = "\n".join(code[start:end])
        where = f"the image publish at line {start + 1}"
        if re.search(r'--cache-from\s+"type=registry,ref=[^"]*\$\{?BUILD_CACHE\b', body):
            problems.append(f"{where} imports the SHARED cache tag — an overlapping run may re-point it between the "
                            "cache export and this import, and a miss re-uploads everything. Import the cache job's "
                            "digest-pinned `cache_ref`")
        if not re.search(r"CACHE_REF:\s*\$\{\{\s*needs\.[A-Za-z0-9_-]+\.outputs\.cache_ref\s*\}\}", body):
            problems.append(f"{where} does not take `CACHE_REF` from the cache job's `cache_ref` output")
        if '--cache-from "type=registry,ref=$CACHE_REF"' not in body:
            problems.append(f"{where} does not build `--cache-from` the handed-over `$CACHE_REF`")
        refusal = digest_refusal_problem(body)
        if refusal:
            problems.append(f"{where} {refusal}")
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

    # ── OFFLINE: the byte-for-byte retag ──
    manifest = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
    mdigest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    oci = "application/vnd.oci.image.manifest.v1+json"

    def retag_io(calls, get=None, put=None):
        def post(url, form):
            calls.append(("POST", url, dict(form)))
            return {"refresh_token": "acr-refresh"} if url.endswith("/oauth2/exchange") else {"access_token": "acr-access"}

        def fetch(method, url, token, headers, body):
            calls.append((method, url, token, dict(headers), body))
            if method == "GET":
                return get if get is not None else (200, {"content-type": oci, "docker-content-digest": mdigest}, manifest)
            return put if put is not None else (201, {"docker-content-digest": mdigest}, b"")

        def run(cmd, **kw):
            calls.append(("RUN", cmd))
            return Done("entra-token\n")
        return Io(post=post, run=run, fetch=fetch, sleep=lambda s: None)

    def calls_of(kind, calls):
        return [c for c in calls if c[0] == kind]

    calls: list = []
    got = retag("r.example.io", "tid", "ollama", "0.1-abc1234", "0.1", retag_io(calls))
    check("offline/retag returns the ONE digest both tags name", lambda: got, lambda d: d == mdigest)
    check("offline/retag asks for pull,push on the ONE repository",
          lambda: calls_of("POST", calls)[1], lambda c: c[2]["scope"] == "repository:ollama:pull,push")
    check("offline/retag GETs the source tag, accepting every manifest shape",
          lambda: calls_of("GET", calls)[0],
          lambda c: c[1] == "https://r.example.io/v2/ollama/manifests/0.1-abc1234"
          and c[2] == "acr-access" and "application/vnd.oci.image.index.v1+json" in c[3]["Accept"])
    check("offline/retag PUTs the SAME bytes under the target with the served media type",
          lambda: calls_of("PUT", calls)[0],
          lambda c: c[1] == "https://r.example.io/v2/ollama/manifests/0.1"
          and c[3] == {"Content-Type": oci} and c[4] == manifest)
    params: list = []
    retag("r.example.io", "tid", "ollama", "a", "b",
          retag_io(params, get=(200, {"content-type": oci + "; charset=utf-8"}, manifest)))
    check("offline/a media type with parameters is PUT without them",
          lambda: calls_of("PUT", params)[0][3]["Content-Type"], lambda t: t == oci)
    absent: list = []
    check("offline/an absent source (404) is refused",
          lambda: retag("r.example.io", "tid", "ollama", "a", "b", retag_io(absent, get=(404, {}, b""))), REFUSED)
    check("offline/(the 404 case issued no PUT)", lambda: calls_of("PUT", absent), lambda p: p == [])
    check("offline/a source served with no Content-Type is refused",
          lambda: retag("r.example.io", "tid", "ollama", "a", "b", retag_io([], get=(200, {}, manifest))), REFUSED)
    check("offline/bytes that do not hash to the served digest are refused",
          lambda: retag("r.example.io", "tid", "ollama", "a", "b", retag_io([], get=(
              200, {"content-type": oci, "docker-content-digest": "sha256:" + "0" * 64}, manifest))), REFUSED)
    check("offline/a refused PUT (403) is refused",
          lambda: retag("r.example.io", "tid", "ollama", "a", "b", retag_io([], put=(403, {}, b"denied"))), REFUSED)
    check("offline/a PUT stored under ANOTHER digest is refused",
          lambda: retag("r.example.io", "tid", "ollama", "a", "b",
                        retag_io([], put=(201, {"docker-content-digest": "sha256:" + "1" * 64}, b""))), REFUSED)
    early: list = []
    check("offline/a malformed tag is refused before any network call",
          lambda: retag("r.example.io", "tid", "ollama", "a/../b", "b", retag_io(early)), REFUSED)
    check("offline/(the malformed tag made no call)", lambda: early, lambda c: c == [])
    check("offline/the same tag twice is refused",
          lambda: retag("r.example.io", "tid", "ollama", "a", "a", retag_io([])), REFUSED)

    # ── the line-convergence invariant ──
    lane = """\
jobs:
  publish:
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - name: Build
        if: steps.published.outputs.exists == 'false'
        run: docker build -t "$ACR/x:${LINE}-${short}" deploy/x
      - name: Push
        if: steps.published.outputs.exists == 'false'
        run: docker push "$ACR/x:${LINE}-${short}"
      - name: Converge the line
        id: line
        run: |
          python3 .github/scripts/acr-data-plane.py converge-line --registry "$ACR" --tenant "$T" \\
            --repository x --line "$LINE" --self "$GITHUB_SHA"
"""
    check("line: the converging lane passes (positive control)", lambda: check_line_convergence(lane), lambda p: p == [])
    check("line: a converge step gated on `exists == 'false'` is red (round-2 review)",
          lambda: check_line_convergence(lane.replace("        id: line\n",
                                                      "        id: line\n        if: steps.published.outputs.exists == 'false'\n")),
          lambda p: len(p) == 1 and "`if:`" in p[0])
    check("line: a raw ordered retag onto $LINE is red (round-3 review: no re-check after the write)",
          lambda: check_line_convergence(lane + '      - run: |\n          python3 .github/scripts/acr-data-plane.py retag '
                                                '--registry r --tenant t --repository x --from "a" --to "$LINE"\n'),
          lambda p: len(p) == 1 and "raw `retag" in p[0])
    check("line: a converge step without --self is red",
          lambda: check_line_convergence(lane.replace(' --self "$GITHUB_SHA"', "")),
          lambda p: len(p) == 1 and "--self" in p[0])
    check("line: pushing :$LINE with docker is red",
          lambda: check_line_convergence(lane.replace('run: docker push "$ACR/x:${LINE}-${short}"',
                                                      'run: docker push "$ACR/x:${LINE}"')),
          lambda p: len(p) == 1 and "build or push" in p[0])
    check("line: tagging :${LINE} in the build is red",
          lambda: check_line_convergence(lane.replace('docker build -t "$ACR/x:${LINE}-${short}"',
                                                      'docker build -t "$ACR/x:${LINE}-${short}" -t "$ACR/x:${LINE}"')),
          lambda p: len(p) == 1 and "build or push" in p[0])
    check("line: READING the line back (imagetools inspect) is not a write",
          lambda: check_line_convergence(lane + '      - run: moving=$(docker buildx imagetools inspect "$ACR/x:${LINE}" --format x)\n'),
          lambda p: p == [])
    check("line: imagetools create onto :$LINE is a write and red",
          lambda: check_line_convergence(lane + '      - run: docker buildx imagetools create --tag "$ACR/x:${LINE}" src\n'),
          lambda p: len(p) == 1 and "build or push" in p[0])
    check("line: a lane with no converge step is red, never vacuously green",
          lambda: check_line_convergence("jobs: {}\n"), lambda p: len(p) == 1 and "converge-line" in p[0])
    check("line: a shallow checkout is red",
          lambda: check_line_convergence(lane.replace("        with:\n          fetch-depth: 0\n", "")),
          lambda p: len(p) == 1 and "fetch-depth" in p[0])
    check("line: a commented-out converge step does not count",
          lambda: check_line_convergence(lane.replace("          python3 .github/scripts/acr-data-plane.py converge-line",
                                                      "          # python3 .github/scripts/acr-data-plane.py converge-line")),
          lambda p: any("no step runs" in x for x in p))

    # ── converge-line against a fake registry + a fake main ──
    A, B, C, D, S, T, X = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "5" * 40, "7" * 40, "e" * 40)
    PRE = "0123456"     # a tag written before the lane moved here: its commit is not on this main
    blob = {f"0.1-{c[:7]}": ('{"img":"%s"}' % c[:7]).encode() for c in (A, B, C, D, S, T, X)}
    blob[f"0.1-{PRE}"] = b'{"img":"pre"}'

    def img(c):
        return blob[f"0.1-{c[:7]}"]

    def digest_of(c):
        return "sha256:" + hashlib.sha256(img(c)).hexdigest()

    def converge_io(mains, reg, puts, after_put=None, list_status=200, page=0):
        """mains: successive answers of `git rev-list --first-parent origin/main` (newest first);
        reg: tag -> bytes (the registry). page > 0 splits the tag listing into pages of that size."""
        state = {"mains": [list(x) for x in mains]}

        def post(url, form):
            return {"refresh_token": "r"} if url.endswith("/oauth2/exchange") else {"access_token": "t"}

        def run(cmd, **kw):
            if cmd[:2] == ["git", "rev-list"]:
                h = state["mains"].pop(0) if len(state["mains"]) > 1 else state["mains"][0]
                return Done("\n".join(h) + "\n")
            return Done("entra-token\n" if cmd[0] == "az" else "")

        def fetch(method, url, token, headers, body):
            if "/tags/list" in url:
                if list_status != 200:
                    return list_status, {}, b""
                names = sorted(reg)
                if not page:
                    return 200, {}, json.dumps({"tags": names}).encode()
                start = int(url.split("last=")[1]) if "last=" in url else 0
                link = (f'</v2/x/tags/list?n={page}&last={start + page}>; rel="next"'
                        if start + page < len(names) else None)
                return 200, ({"link": link} if link else {}), json.dumps({"tags": names[start:start + page]}).encode()
            tag = url.rsplit("/", 1)[1]
            if method == "GET":
                return (200, {"content-type": "application/vnd.oci.image.manifest.v1+json"}, reg[tag]) if tag in reg \
                    else (404, {}, b"")
            reg[tag] = body
            puts.append(tag)
            if after_put:
                after_put(reg)
            return 201, {"docker-content-digest": "sha256:" + hashlib.sha256(body).hexdigest()}, b""
        return Io(post=post, run=run, fetch=fetch, sleep=lambda s: None)

    def published(*cs):
        return {f"0.1-{c[:7]}": img(c) for c in cs}

    def run_converge(self_sha, mains, reg, **kw):
        puts: list = []
        got = converge_line("r.io", "t", "x", "0.1", self_sha, converge_io(mains, reg, puts, **kw))
        return got, reg.get("0.1"), puts

    check("converge/the newest run moves the line onto its own image and verifies it",
          lambda: run_converge(A, [[A]], published(A)), lambda g: g == ((A, digest_of(A)), img(A), ["0.1"]))
    # Round-3 race: A believes it is newest (its fetch predates B), B was promoted while A paused; A's PUT
    # lands on top and A's own re-check puts the line back on B.
    check("converge/a stale write by an older run is corrected by that run's own re-check",
          lambda: run_converge(A, [[A], [B, A]], dict(published(A, B), **{"0.1": img(B)})),
          lambda g: g == ((B, digest_of(B)), img(B), ["0.1", "0.1"]))
    # Round-4 race (A/B/C): A's delayed PUT lands after C became newest on main, C's image unpublished.
    check("converge/A/B/C: A's stale PUT after C became newest (C unpublished) ends on B, not A",
          lambda: run_converge(A, [[A], [C, B, A]], dict(published(A, B), **{"0.1": img(B)})),
          lambda g: g == ((B, digest_of(B)), img(B), ["0.1", "0.1"]))
    check("converge/A/B/C: C's build failed — a later converge keeps the line on B, with no write",
          lambda: run_converge(B, [[C, B, A]], dict(published(A, B), **{"0.1": img(B)})),
          lambda g: g == ((B, digest_of(B)), img(B), []))
    check("converge/A/B/C: once C is published, C's run moves the line onto C",
          lambda: run_converge(C, [[C, B, A]], dict(published(A, B, C), **{"0.1": img(B)})),
          lambda g: g == ((C, digest_of(C)), img(C), ["0.1"]))
    # Round-5 (path-filtered history missed real publications):
    # (1) a manual dispatch on X, a main commit that touches no service path, published `0.1-<X>`; the
    #     newer D touches nothing either and published nothing. X IS the newest publication.
    check("converge/a dispatch on an unrelated main commit is a real publication (line -> X)",
          lambda: run_converge(X, [[D, X, A]], dict(published(A, X), **{"0.1": img(A)})),
          lambda g: g == ((X, digest_of(X)), img(X), ["0.1"]))
    # (2) a multi-commit push: S changed the service, T (the tip, GITHUB_SHA) did not; the run published
    #     `0.1-<T>`. A path filter finds S, which has no image; the registry says T.
    check("converge/a multi-commit push publishes its TIP, which a path filter never finds (line -> T)",
          lambda: run_converge(T, [[T, S, A]], dict(published(A, T), **{"0.1": img(A)})),
          lambda g: g == ((T, digest_of(T)), img(T), ["0.1"]))
    # A line moved by another writer after our write: the read-back disagrees, so we go round again.
    flips = {"n": 0}

    def clobber(r):
        if flips["n"] == 0:
            flips["n"] = 1
            r["0.1"] = img(A)
    check("converge/a line moved underneath after our write is re-converged, not reported green",
          lambda: run_converge(B, [[B, A]], published(A, B), after_put=clobber),
          lambda g: g[0] == (B, digest_of(B)) and g[1] == img(B) and len(g[2]) == 2)
    check("converge/a tag from before the move (commit not on this main) is ignored, not chosen",
          lambda: run_converge(A, [[A]], dict(published(A), **{f"0.1-{PRE}": blob[f"0.1-{PRE}"]})),
          lambda g: g[0] == (A, digest_of(A)))
    check("converge/a Link-paged tag listing is read to the end",
          lambda: run_converge(B, [[B, A]], published(A, B, C), page=1),
          lambda g: g[0] == (B, digest_of(B)))
    check("converge/an unreadable tag listing (503) is RED, never 'nothing published'",
          lambda: run_converge(A, [[A]], published(A), list_status=503), REFUSED)
    check("converge/this run's own tag missing from the listing is RED",
          lambda: run_converge(A, [[A]], published(B)), REFUSED)
    check("converge/no published tag on main's history is RED",
          lambda: converge_line("r.io", "t", "x", "0.1", "0123456" + "8" * 33,
                                converge_io([[A]], {f"0.1-{PRE}": blob[f"0.1-{PRE}"]}, [])), REFUSED)
    check("converge/a 7-char prefix naming two main commits is RED, never guessed",
          lambda: run_converge(A, [["a" * 7 + "1" * 33, A]], published(A)), REFUSED)
    check("converge/an already-converged line is not rewritten",
          lambda: run_converge(A, [[A]], dict(published(A), **{"0.1": img(A)})),
          lambda g: g[2] == [])
    check("converge/an unanswered lookup (500) is refused, never read as absent",
          lambda: tag_digest("r.io", "t", "x", "0.1", Io(post=lambda u, f: {"refresh_token": "r", "access_token": "t"},
                                                       run=lambda c, **k: Done("e\n"), fetch=lambda *a: (500, {}, b""))),
          REFUSED)

    # ── the cache hand-over invariant ──
    handover = """\
jobs:
  cache:
    outputs:
      cache_ref: ${{ steps.handover.outputs.cache_ref }}
    steps:
      - name: Is this commit already published?
        id: published
        run: echo exists=false
      - name: Build into the registry cache
        if: steps.published.outputs.exists == 'false'
        run: |
          python3 .github/scripts/acr-data-plane.py tags --registry r --tenant t --repository x > "$RUNNER_TEMP/tags"
          { grep -E "^$BUILD_CACHE-run-" "$RUNNER_TEMP/tags" || true; } \\
            | sort -r > "$RUNNER_TEMP/run-caches"
          from=(--cache-from "type=registry,ref=$ACR/$REPO:$BUILD_CACHE")
          while read -r tag; do
            from+=(--cache-from "type=registry,ref=$ACR/$REPO:$tag")
          done < "$RUNNER_TEMP/run-caches"
          docker buildx build "${from[@]}" \\
            --cache-to "type=registry,ref=$ACR/$REPO:$BUILD_CACHE-run-1,mode=max" --output type=cacheonly x
  publish:
    needs: cache
    steps:
      - name: Build and push
        if: steps.published.outputs.exists == 'false'
        env:
          CACHE_REF: ${{ needs.cache.outputs.cache_ref }}
        run: |
          case "$CACHE_REF" in *@sha256:*) ;; *) exit 1 ;; esac
          docker buildx build --cache-from "type=registry,ref=$CACHE_REF" \\
            --output "type=image,name=$ACR/$REPO:t,push=true,unpack=false" x
"""
    check("cache: the immutable hand-over + early existence check passes (positive control)",
          lambda: check_cache_handover(handover), lambda p: p == [])
    check("cache: publish importing the SHARED cache tag is red (review: overlapping runs re-point it)",
          lambda: check_cache_handover(handover.replace('--cache-from "type=registry,ref=$CACHE_REF"',
                                                        '--cache-from "type=registry,ref=$ACR/$REPO:$BUILD_CACHE"')),
          lambda p: any("SHARED cache tag" in x for x in p))
    check("cache: a hand-over not refused unless digest-pinned is red",
          lambda: check_cache_handover(handover.replace('case "$CACHE_REF" in *@sha256:*) ;; *) exit 1 ;; esac', "true")),
          lambda p: len(p) == 1 and "case \"$CACHE_REF\" in" in p[0])
    check("cache: the refusal arm DELETED (pattern still present) is red (review round 2)",
          lambda: check_cache_handover(handover.replace('case "$CACHE_REF" in *@sha256:*) ;; *) exit 1 ;; esac',
                                                        'case "$CACHE_REF" in *@sha256:*) ;; esac')),
          lambda p: len(p) == 1 and "no catch-all" in p[0])
    check("cache: the refusal arm's `exit 1` turned into `true` is red (review round 2)",
          lambda: check_cache_handover(handover.replace('*) exit 1 ;; esac', '*) true ;; esac')),
          lambda p: len(p) == 1 and "exit non-zero" in p[0])
    check("cache: `exit 0` in the refusal arm is red",
          lambda: check_cache_handover(handover.replace('*) exit 1 ;; esac', '*) echo no; exit 0 ;; esac')),
          lambda p: len(p) == 1 and "exit non-zero" in p[0])
    check("cache: a case on ANOTHER variable is red",
          lambda: check_cache_handover(handover.replace('case "$CACHE_REF" in', 'case "$OTHER" in')),
          lambda p: len(p) == 1 and "case \"$CACHE_REF\" in" in p[0])
    check("cache: the digest arm missing is red",
          lambda: check_cache_handover(handover.replace('*@sha256:*) ;; ', '')),
          lambda p: len(p) == 1 and "*@sha256:*)" in p[0])
    check("cache: the real refusal arm (echo …; exit 1) passes",
          lambda: digest_refusal_problem('case "$CACHE_REF" in *@sha256:*) ;; *) echo "::error::x (\'$CACHE_REF\')"; exit 1 ;; esac'),
          lambda p: p is None)
    check("cache: the cache built BEFORE the existence check is red (review: skip cache construction entirely)",
          lambda: check_cache_handover(handover.replace(
              "      - name: Is this commit already published?\n        id: published\n        run: echo exists=false\n", "")
              .replace("    needs: cache\n    steps:\n",
                       "    needs: cache\n    steps:\n      - name: Is this commit already published?\n        id: published\n        run: echo x\n")),
          lambda p: any("not preceded" in x for x in p))
    check("cache: two `--cache-to` targets in one build are red (run 40559129837 uploaded the weights twice)",
          lambda: check_cache_handover(handover.replace(
              '            --cache-to "type=registry,ref=$ACR/$REPO:$BUILD_CACHE-run-1,mode=max"',
              '            --cache-to "type=registry,ref=$ACR/$REPO:$BUILD_CACHE,mode=max" \\\\\n'
              '            --cache-to "type=registry,ref=$ACR/$REPO:$BUILD_CACHE-run-1,mode=max"')),
          lambda p: len(p) == 1 and "2 `--cache-to` targets" in p[0])
    loop = ('          while read -r tag; do\n'
            '            from+=(--cache-from "type=registry,ref=$ACR/$REPO:$tag")\n'
            '          done < "$RUNNER_TEMP/run-caches"\n')
    check("cache: a cache build importing ONLY the shared tag is red (runs 40559129837 -> 40571543897)",
          lambda: check_cache_handover(handover.replace(loop, "")
              .replace('docker buildx build "${from[@]}"', 'docker buildx build --cache-from "type=registry,ref=$ACR/$REPO:$BUILD_CACHE"')),
          lambda p: len(p) == 1 and "COMPLETED run caches" in p[0])
    check("cache: ONLY the consuming loop removed (listing + \"${from[@]}\" kept) is red (review of #54)",
          lambda: check_cache_handover(handover.replace(loop, "")),
          lambda p: len(p) == 1 and "nothing appends the listed run caches" in p[0])
    check("cache: a loop appending a CONSTANT instead of the listed tag is red",
          lambda: check_cache_handover(handover.replace('ref=$ACR/$REPO:$tag")', 'ref=$ACR/$REPO:$BUILD_CACHE")')),
          lambda p: len(p) == 1 and "nothing appends" in p[0])
    check("cache: a loop appending `$tag_other` (not the read `tag`) is red (review of #54)",
          lambda: check_cache_handover(handover.replace('ref=$ACR/$REPO:$tag")', 'ref=$ACR/$REPO:$tag_other")')),
          lambda p: len(p) == 1 and "nothing appends" in p[0])
    check("cache: a loop appending `${tag_other}` (not the read `tag`) is red (review of #54)",
          lambda: check_cache_handover(handover.replace('ref=$ACR/$REPO:$tag")', 'ref=$ACR/$REPO:${tag_other}")')),
          lambda p: len(p) == 1 and "nothing appends" in p[0])
    check("cache: a loop appending `${tag}` (braced, the read variable) passes",
          lambda: check_cache_handover(handover.replace('ref=$ACR/$REPO:$tag")', 'ref=$ACR/$REPO:${tag}")')),
          lambda p: p == [])
    check("cache: a loop over a file NOT derived from the listing is red",
          lambda: check_cache_handover(handover.replace('done < "$RUNNER_TEMP/run-caches"', 'done < "$RUNNER_TEMP/elsewhere"')),
          lambda p: len(p) == 1 and "nothing appends" in p[0])
    check("cache: the listing not written to a file is red",
          lambda: check_cache_handover(handover.replace('--repository x > "$RUNNER_TEMP/tags"', "--repository x")),
          lambda p: len(p) == 1 and "not written to a file" in p[0])
    check("cache: the array built but the build not expanding it is red",
          lambda: check_cache_handover(handover.replace('docker buildx build "${from[@]}"', 'docker buildx build')),
          lambda p: len(p) == 1 and "does not expand" in p[0])
    check("cache: an ungated cache build is red",
          lambda: check_cache_handover(handover.replace(
              "      - name: Build into the registry cache\n        if: steps.published.outputs.exists == 'false'\n",
              "      - name: Build into the registry cache\n")),
          lambda p: len(p) == 1 and "not gated" in p[0])
    check("cache: a lane with no registry cache passes (nothing to hand over)",
          lambda: check_cache_handover("jobs:\n  publish:\n    steps:\n      - run: docker push x\n"), lambda p: p == [])
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
        if argv and argv[0] == "converge-line":
            newest, digest = converge_line(_login_server(_arg(argv, "--registry")), _tenant(_arg(argv, "--tenant")),
                                           _arg(argv, "--repository"), _arg(argv, "--line"), _arg(argv, "--self"), Io())
            print(f"newest={newest}")
            print(f"digest={digest}")
            return 0
        if len(argv) == 2 and argv[0] == "check-cache-handover":
            with open(argv[1], encoding="utf-8") as f:
                problems = check_cache_handover(f.read())
            for p in problems:
                print(f"::error file={argv[1]}::{p}")
            if not problems:
                print(f"{argv[1]}: a registry build cache, if any, is built only for an unpublished commit and handed to "
                      "the publish by digest")
            return 1 if problems else 0
        if len(argv) == 2 and argv[0] == "check-line-convergence":
            with open(argv[1], encoding="utf-8") as f:
                problems = check_line_convergence(f.read())
            for p in problems:
                print(f"::error file={argv[1]}::{p}")
            if not problems:
                print(f"{argv[1]}: the line tag is moved only by converge-line (newest published tag in main order, re-checked after every write), on every publish run")
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
        if argv and argv[0] == "tags":
            for tag in sorted(list_tags(_login_server(_arg(argv, "--registry")), _tenant(_arg(argv, "--tenant")),
                                        _arg(argv, "--repository"), Io())):
                print(tag)
            return 0
        if argv and argv[0] == "retag":
            digest = retag(_login_server(_arg(argv, "--registry")), _tenant(_arg(argv, "--tenant")),
                           _arg(argv, "--repository"), _arg(argv, "--from"), _arg(argv, "--to"), Io())
            print(digest)
            return 0
        print(__doc__, file=sys.stderr)
        return 2
    except Refusal as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
