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
    with `ref: main`.
A checkout or fetch at a resolved commit is untouched — that is a pin to a set, not a line.

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
SINGLE_LINE = [
    re.compile(r"Systemorph/MeshWeaver/[^@\s'\"]+@main" + _NOT_MORE),
    re.compile(r"Systemorph/MeshWeaver/main/"),
    re.compile(r"repos/Systemorph/MeshWeaver/[^\s'\"]*\?ref=main" + _NOT_MORE),
    re.compile(r"^\s*(?:-\s*)?(?:scripts-ref|SCRIPTS_REF|platform-ref|[A-Z_]*PLATFORM_REF):\s*['\"]?main['\"]?\s*$"),
]
CORE_REPOSITORY = re.compile(r"^\s*repository:\s*['\"]?(?:Systemorph/MeshWeaver|\$\{\{\s*env\.PLATFORM_REPO\s*\}\})['\"]?\s*$")
REF_MAIN = re.compile(r"^\s*ref:\s*['\"]?main['\"]?\s*$")


def code(line: str) -> str:
    return "" if line.lstrip().startswith("#") else line


def check(root: Path) -> tuple[list[str], int]:
    github = root / ".github"
    if not github.is_dir():
        return [f"::error::{github} does not exist — this guard checked nothing"], 0
    files = sorted([*github.rglob("*.yml"), *github.rglob("*.yaml")])
    out: list[str] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, raw in enumerate(lines, 1):
            line = code(raw)
            hit = any(p.search(line) for p in SINGLE_LINE)
            if not hit and REF_MAIN.match(line):
                # a `ref: main` belongs to core when its step's `with:` names core's repository
                window = lines[max(0, n - 6):n + 5]
                hit = any(CORE_REPOSITORY.match(code(w)) for w in window)
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
