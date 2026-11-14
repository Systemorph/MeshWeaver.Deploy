#!/usr/bin/env python3
"""Nothing in this repository's workflows follows core `main` — the v4 line.

Policy `major-lines-run-separately` (core `Doc/Architecture/MajorLinesRunSeparately`): core `main`
carries platform v4 (.NET 11) and core `main3` carries v3 (.NET 10). This repository is on v3, so
every reference to core names `main3`. A reference left at, or newly written as, `main` would
follow v4 the moment core main moves — silently, because a `uses:` ref or a fetched script resolves
without any error. Moving to v4 is ONE deliberate pull request that changes every reference and
`CORE_LINE` below together; this guard makes a single stray `main` red instead.

Refused, on every non-comment line of every yml/yaml file under `.github/`:
  * `Systemorph/MeshWeaver/<path>@main`             a shared workflow or composite action;
  * `Systemorph/MeshWeaver/main/…`                  a raw.githubusercontent.com fetch;
  * `…/repos/Systemorph/MeshWeaver/…?ref=main`      a contents-API fetch;
  * `scripts-ref:` / `SCRIPTS_REF:` / `platform-ref:` / `*_PLATFORM_REF:` set to `main`;
  * an `actions/checkout` of core (`repository: Systemorph/MeshWeaver` or `${{ env.PLATFORM_REPO }}`)
    with `ref: main`, or with NO `ref:` (core's default branch, `main`) — read from the checkout's
    own `with:` block, not from nearby lines;
  * a shell assignment `ref=main` (the default a step hands a later checkout);
  * git traffic naming core's branch (`git ls-remote …/Systemorph/MeshWeaver refs/heads/main`,
    `git clone -b main …`), or a `git clone` of core with no branch (its default, `main`).
Core is named literally (`Systemorph/MeshWeaver`) or through this repository's indirection
(`$PLATFORM_REPO`, `${{ env.PLATFORM_REPO }}`). Comments are stripped first, trailing ones too.
A checkout or fetch at a resolved commit is untouched — that is a pin to a set, not a line.

NOT visible to any tree scan: a repository VARIABLE (e.g. `vars.MW_PLATFORM_REF`) that overrides an
env default with `main` at run time. Such a variable is set in the repository settings, outside the
tree, so check those when the line changes.

Stdlib only. Usage:
    python3 .github/scripts/check-core-line.py --self-test
    python3 .github/scripts/check-core-line.py [--root .]
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

CORE_LINE = "main3"
_NOT_MORE = r"(?![\w.-])"
_CORE = r"(?:Systemorph/MeshWeaver|\$PLATFORM_REPO|\$\{PLATFORM_REPO\}|\$\{\{\s*env\.PLATFORM_REPO\s*\}\})(?![\w.-])"
_CORE_URL = r"github\.com[/:]Systemorph/MeshWeaver(?:\.git)?(?![\w.-])"
SINGLE_LINE = [
    re.compile(r"Systemorph/MeshWeaver/[^@\s'\"]+@main" + _NOT_MORE),
    re.compile(r"raw\.githubusercontent\.com/" + _CORE + r"/main/"),
    re.compile(r"repos/" + _CORE + r"/[^\s'\"]*\?ref=main" + _NOT_MORE),
    re.compile(r"^\s*(?:-\s*)?(?:scripts-ref|SCRIPTS_REF|platform-ref|[A-Z_]*PLATFORM_REF):\s*['\"]?main['\"]?\s*$"),
    re.compile(r"(?:^|[\s;&|(])ref=['\"]?main['\"]?(?:\s|;|$)"),
    re.compile(r"^(?=.*" + _CORE_URL + r").*?(?:refs/heads/main|(?:-b|--branch)[ =]['\"]?main)" + _NOT_MORE),
    re.compile(r"^(?=.*\bgit\s+clone\b)(?!.*(?:\s-b\s|--branch[ =])).*" + _CORE_URL),
]
CORE_REPOSITORY = re.compile(r"^(?P<indent>\s*)repository:\s*['\"]?(?:Systemorph/MeshWeaver|\$\{\{\s*env\.PLATFORM_REPO\s*\}\})['\"]?\s*$")
CHECKOUT_REF = re.compile(r"^\s*ref:\s*(?P<ref>.*?)\s*$")


def code(line: str) -> str:
    """`line` without its comment: a whole-line `#`, or a ` #` outside quotes."""
    if line.lstrip().startswith("#"):
        return ""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line


def _checkout_ref(code_lines: list[str], at: int, indent: int) -> str | None:
    """The `ref:` sibling of the `repository:` key at `code_lines[at]` (same `with:` block), or None."""
    def ind(s: str) -> int:
        return len(s) - len(s.lstrip())
    lo = at
    while lo > 0 and (not code_lines[lo - 1].strip() or ind(code_lines[lo - 1]) >= indent):
        lo -= 1
    hi = at + 1
    while hi < len(code_lines) and (not code_lines[hi].strip() or ind(code_lines[hi]) >= indent):
        hi += 1
    for line in code_lines[lo:hi]:
        m = CHECKOUT_REF.match(line)
        if m and ind(line) == indent:
            return m.group("ref").strip("'\"")
    return None


def check(root: Path) -> tuple[list[str], int]:
    github = root / ".github"
    if not github.is_dir():
        return [f"::error::{github} does not exist — this guard checked nothing"], 0
    files = sorted([*github.rglob("*.yml"), *github.rglob("*.yaml")])
    out: list[str] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        code_lines = [code(line) for line in lines]
        for n, raw in enumerate(lines, 1):
            line = code_lines[n - 1]
            hit = any(p.search(line) for p in SINGLE_LINE)
            repo = CORE_REPOSITORY.match(line)
            if not hit and repo:
                # the checkout's OWN `with:` block decides: `ref: main`, or no ref at all (= default branch)
                ref = _checkout_ref(code_lines, n - 1, len(repo.group("indent")))
                hit = ref is None or ref == "main"
            if hit:
                out.append(f"::error file={rel},line={n}::`{raw.strip()}` follows core `main`, the v4 "
                           f"line (policy major-lines-run-separately). This repository is on v3: name "
                           f"core `{CORE_LINE}`. Moving to v4 is one deliberate pull request.")
    return out, len(files)


def self_test() -> int:
    job = "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
    co = "      - uses: actions/checkout@v7\n        with:\n          repository: Systemorph/MeshWeaver\n"
    cases = [
        ("lane-on-main3", "jobs:\n  a:\n    uses: Systemorph/MeshWeaver/.github/workflows/x.yml@main3\n", False),
        ("lane-on-main", "jobs:\n  a:\n    uses: Systemorph/MeshWeaver/.github/workflows/x.yml@main\n", True),
        ("action-on-main", job + "      - uses: Systemorph/MeshWeaver/.github/actions/core-file@main\n", True),
        ("other-repo-on-main", job + "      - uses: jlumbroso/free-disk-space@main\n", False),
        ("comment-naming-main", "# was Systemorph/MeshWeaver/.github/actions/core-file@main\n" + job, False),
        ("trailing-comment-naming-main", job + "      - uses: Systemorph/MeshWeaver/.github/actions/core-file@main3   # was …/core-file@main\n", False),
        ("trailing-comment-on-a-core-checkout-ref", job + co + "          ref: main3   # was: main\n", False),
        ("scripts-ref-main", "jobs:\n  a:\n    with:\n      scripts-ref: main\n", True),
        ("scripts-ref-main3", "jobs:\n  a:\n    with:\n      scripts-ref: main3\n", False),
        ("SCRIPTS_REF-main", "env:\n  SCRIPTS_REF: main\n", True),
        ("platform-ref-env-main", "env:\n  MW_CHART_GATE_PLATFORM_REF: main\n", True),
        ("plugins-ref-env-main (not core)", "env:\n  MW_CHART_GUARDS_PLUGINS_REF: main\n", False),
        ("raw-fetch-at-main", job + "      - run: curl https://raw.githubusercontent.com/Systemorph/MeshWeaver/main/.github/scripts/x.py\n", True),
        ("raw-fetch-at-sha", job + "      - run: curl https://raw.githubusercontent.com/Systemorph/MeshWeaver/$sha/.github/scripts/x.py\n", False),
        ("api-fetch-at-main", job + "      - run: gh api repos/Systemorph/MeshWeaver/contents/x.py?ref=main\n", True),
        ("core-checkout-at-main", job + co + "          ref: main\n", True),
        ("core-checkout-at-main3", job + co + "          ref: main3\n", False),
        ("core-checkout-at-a-sha", job + co + "          ref: ${{ steps.p.outputs.sha }}\n", False),
        ("env-repo-checkout-at-main", job + co.replace("Systemorph/MeshWeaver", "${{ env.PLATFORM_REPO }}") + "          ref: main\n", True),
        ("own-checkout-at-main", job + "      - uses: actions/checkout@v7\n        with:\n          ref: main\n", False),
        ("own-checkout-at-main NEXT TO a core checkout at main3", job + co + "          ref: main3\n"
         "      - uses: actions/checkout@v7\n        with:\n          ref: main\n", False),
        ("core-checkout-with-NO-ref (default = v4)", job + co + "          path: core\n", True),
        ("core-checkout-ref-far-below-repository", job + "      - uses: actions/checkout@v7\n        with:\n"
         "          repository: Systemorph/MeshWeaver\n" + "".join(f"          k{i}: v\n" for i in range(8)) + "          ref: main\n", True),
        ("contents-fetch-via-$PLATFORM_REPO-at-main", job + "      - run: gh api \"repos/$PLATFORM_REPO/contents/.github/scripts/x.py?ref=main\"\n", True),
        ("contents-fetch-via-$PLATFORM_REPO-at-main3", job + "      - run: gh api \"repos/$PLATFORM_REPO/contents/.github/scripts/x.py?ref=main3\"\n", False),
        ("shell-default-ref=main", job + "      - run: |\n          ref=main\n", True),
        ("shell-default-ref=main3", job + "      - run: |\n          ref=main3\n", False),
        ("git-ls-remote-of-core-main", job + "      - run: git ls-remote https://github.com/Systemorph/MeshWeaver refs/heads/main\n", True),
        ("git-ls-remote-of-core-main3", job + "      - run: git ls-remote https://github.com/Systemorph/MeshWeaver refs/heads/main3\n", False),
        ("git-clone-of-core-with-no-branch", job + "      - run: git clone https://github.com/Systemorph/MeshWeaver.git c\n", True),
        ("git-clone-of-core-at-main3", job + "      - run: git clone -b main3 https://github.com/Systemorph/MeshWeaver.git c\n", False),
        ("own-repo refs/heads/main condition", job + "      - if: github.ref == 'refs/heads/main'\n        run: x\n", False),
        ("a-Plugins-clone-is-not-core", job + "      - run: git clone https://github.com/Systemorph/MeshWeaver.Plugins.git p\n", False),
        ("trailing-comment-on-scripts-ref-main-still-refused", "jobs:\n  a:\n    with:\n      scripts-ref: main   # v3\n", True),
        ("plugins-checkout-at-main", job + co.replace("MeshWeaver\n", "MeshWeaver.Plugins\n") + "          ref: main\n", False),
    ]
    failures = 0
    for name, body, expect in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/w.yml").write_text(body, encoding="utf-8")
            got = bool(check(root)[0])
        failures += got != expect
        print(f"  {'ok  ' if got == expect else 'FAIL'} {name:<36} expected={'fire' if expect else 'silent'} "
              f"got={'fire' if got else 'silent'}")
    with tempfile.TemporaryDirectory() as tmp:
        missing = bool(check(Path(tmp))[0])
    failures += not missing
    print(f"  {'ok  ' if missing else 'FAIL'} {'no-.github-directory':<36} expected=fire got={'fire' if missing else 'silent'}")
    total = len(cases) + 1
    if failures:
        print(f"\n::error::check-core-line self-test: {failures} of {total} case(s) wrong.")
        return 1
    print(f"\ncheck-core-line self-test: {total}/{total} cases green (both directions).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    violations, files = check(Path(args.root))
    for v in violations:
        print(v)
    print(f"check-core-line: {files} workflow file(s) read, {len(violations)} reference(s) to core main, "
          f"core line {CORE_LINE}, root={args.root}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
