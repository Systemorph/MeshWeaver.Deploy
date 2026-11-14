#!/usr/bin/env python3
"""platform-line.py — which of the platform's DELIVERY LINES carries a major (or a set's major).

Core delivers one line per platform major (policy `major-lines-run-separately`; platform
Doc/Architecture/MajorLinesRunSeparately, CdDeliveryLines): the branches are declared in the
platform's `.github/delivery-lines.json` (on its default branch), each line's major is the
`<PlatformVersion>` its OWN `Directory.Build.props` declares, and each line seals its own sets. Core's
`resolve-platform.py` lists the CD runs of the ONE line it serves — so a lane that asks the resolver
about a set must fetch the resolver AT that set's line and tell it the line (`MW_CORE_LINE`). Asking
core `main`'s copy about a `main3` set answers "matched no main-cd.yml run on main"
(MeshWeaver.Deploy#302). Both build-host lanes (`build-host.yml`'s hand-over, `build-host-stable.yml`)
place their line here, so the two cannot drift.

    platform-line.py of-set   --version 3.1.13192 [--repo Systemorph/MeshWeaver]
    platform-line.py of-major --major 3            [--repo Systemorph/MeshWeaver]
        → `line=<branch>` and `majors=<branch>:<major> …` (GITHUB_OUTPUT lines), exit 0;
          no line of that major, or more than one, is `::error::` naming every line, exit 1.
    platform-line.py --self-test

The line's major is read by the platform's own `cd-line.py platform-major` (fetched from the default
branch) — the reading core's CD keys its set selection on — never re-implemented here. Every read is
`gh api` against the PUBLIC platform repository; a read that fails is red, never "no line".
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SET_NAME = re.compile(r"^(\d+)\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
RAW = "Accept: application/vnd.github.raw"


class Refusal(Exception):
    """The line cannot be placed — said by name, never guessed."""


def set_major(version: str) -> int:
    """`3.1.13192` → 3, `3.0.0-ci.10052` → 3, `4.0.13212` → 4. Refuses anything that is not a set. Pure."""
    m = SET_NAME.match(version or "")
    if not m:
        raise Refusal(f"{version!r} is not a platform set name — its line cannot be placed")
    return int(m.group(1))


def describe(majors: dict[str, int]) -> str:
    return " ".join(f"{line}:{major}" for line, major in majors.items())


def choose_line(major: int, majors: dict[str, int]) -> str:
    """The ONE line whose declared major is `major`. None, or several, is a Refusal naming every line. Pure."""
    if not majors:
        raise Refusal("the platform's delivery-lines.json declares no line")
    hits = [line for line, m in majors.items() if m == major]
    if not hits:
        raise Refusal(f"no platform delivery line declares major {major} ({describe(majors)})")
    if len(hits) > 1:
        raise Refusal(f"the platform's lines {' and '.join(hits)} all declare major {major} "
                      f"({describe(majors)}) — the line is ambiguous, and is never guessed")
    return hits[0]


def _gh(path: str, raw: bool = True) -> str:
    cmd = ["gh", "api", path] + (["-H", RAW] if raw else [])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Refusal(f"could not read {path} from the platform ({proc.stderr.strip() or proc.returncode})")
    return proc.stdout


def read_majors(repo: str) -> dict[str, int]:
    """{line branch: its declared major}, in delivery-lines.json order, read live through `gh api`."""
    default = _gh(f"repos/{repo}", raw=False)
    default_branch = json.loads(default).get("default_branch") or ""
    if not default_branch:
        raise Refusal(f"{repo} names no default branch")
    doc = json.loads(_gh(f"repos/{repo}/contents/.github/delivery-lines.json?ref={default_branch}"))
    lines = [str(line.get("branch", "")) for line in doc.get("lines", []) if isinstance(line, dict)]
    if not lines or not all(lines):
        raise Refusal(f"{repo}'s delivery-lines.json declares no well-formed line")
    with tempfile.TemporaryDirectory() as tmp:
        cd_line = Path(tmp) / "cd-line.py"
        cd_line.write_text(_gh(f"repos/{repo}/contents/.github/scripts/cd-line.py?ref={default_branch}"))
        majors: dict[str, int] = {}
        for line in lines:
            props = Path(tmp) / "Directory.Build.props"
            props.write_text(_gh(f"repos/{repo}/contents/Directory.Build.props?ref={line}"))
            proc = subprocess.run([sys.executable, "-I", str(cd_line), "platform-major", str(props)],
                                  capture_output=True, text=True)
            out = proc.stdout.strip()
            if proc.returncode != 0 or not out.isdigit():
                raise Refusal(f"the platform's line {line} declares no readable PlatformVersion major "
                              f"({(out or proc.stderr.strip())[:300]})")
            majors[line] = int(out)
    return majors


def self_test() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: object = "") -> None:
        if not cond:
            failures.append(f"{name}: {detail}")

    def refused(fn, *a) -> str:
        try:
            fn(*a)
        except Refusal as e:
            return str(e)
        return ""

    two = {"main": 4, "main3": 3}
    check("a v3 major picks main3", choose_line(3, two) == "main3")
    check("a v4 major picks main", choose_line(4, two) == "main")
    msg = refused(choose_line, 5, two)
    check("a major no line declares is refused, naming every line", "major 5" in msg and "main:4 main3:3" in msg, msg)
    msg = refused(choose_line, 3, {"main": 3, "main3": 3})
    check("two lines of one major are refused as ambiguous", "ambiguous" in msg and "main and main3" in msg, msg)
    check("no line at all is refused", "declares no line" in refused(choose_line, 3, {}))
    check("set major of a SemVer set", set_major("3.1.13192") == 3 and set_major("4.0.13212") == 4)
    check("set major of a legacy ci set", set_major("3.0.0-ci.10052") == 3)
    for bad in ("", "latest", "3.1", "v3.1.1"):
        check(f"set major refuses {bad!r}", bool(refused(set_major, bad)))
    # Negative control: a chooser that ignores the major (always the first line) must fail the v3 case.
    first = lambda major, majors: next(iter(majors))  # noqa: E731
    check("negative control: a major-blind chooser picks the wrong line for v3", first(3, two) != "main3")
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("platform-line.py self-test: all cases pass")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("of-set")
    s.add_argument("--version", required=True)
    s.add_argument("--repo", default="Systemorph/MeshWeaver")
    m = sub.add_parser("of-major")
    m.add_argument("--major", type=int, required=True)
    m.add_argument("--repo", default="Systemorph/MeshWeaver")
    a = ap.parse_args(argv)
    try:
        major = set_major(a.version) if a.cmd == "of-set" else a.major
        majors = read_majors(a.repo)
        line = choose_line(major, majors)
    except (Refusal, ValueError, OSError) as e:
        print(f"::error::{e}")
        return 1
    print(f"line={line}")
    print(f"majors={describe(majors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
