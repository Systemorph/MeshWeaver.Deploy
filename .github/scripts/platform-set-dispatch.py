#!/usr/bin/env python3
"""platform-set-dispatch.py — read the platform's hand-over of a sealed set to this repository.

The platform's CD (Systemorph/MeshWeaver `main-cd.yml`) BUILDS and SEALS the product image set
and nothing else. At the end of every CD run it hands the result over to this repository with ONE
`repository_dispatch` of type `platform-set-sealed` (sent with the org's dispatch App, which needs
Contents: write here — only a holder of that grant can send one). This repository owns what
happens next: the build-host runner image for the set, and the hand-over of the newest ARMED set
to the standing build host `build-latest` (`build-host.yml`).

THE CONTRACT (schema 1) — `client_payload`:

    {
      "schema": 1,
      "run":   {"url": "<the CD run's URL>", "coreSha": "<40 hex: the commit the CD run ran>"},
      "set":   {"version": "<x.y.z>" | "", "coreSha": "<40 hex>" | ""},
      "armed": {"armed": true | false, "version": "<x.y.z>" | "",
                "coreSha": "<40 hex>" | "", "pluginsSha": "<40 hex>" | ""}
    }

  * `set` is the set THIS CD run published (its image version and the commit it was built from),
    or both empty when the run published nothing (a reconcile tick with nothing missing). An empty
    set is a statement, not an absence: the image job says so by name and builds nothing.
  * `armed` is what this CD run's `arm` job decided. It is a HINT, never the truth: the hand-over
    re-reads the newest armed set from the registry (`arm-promoted-set.py armed-state`, the same
    reading the platform makes) and uses `armed.coreSha`/`pluginsSha` only when `armed.version` IS
    that newest set — exactly what core's former `build-host-latest` job did with its `needs.arm`
    outputs.
  * `run.coreSha` is the platform commit whose scripts the jobs here fetch (acr-login.sh,
    arm-promoted-set.py, …): the scripts of the same vintage as the run that sent the event.

🚨 NOTHING IN THE PAYLOAD IS TRUSTED BY SHAPE ALONE. Every field is validated here (a malformed
payload is RED, naming the field), every commit is then checked to be on the platform's `main`
(`build-host.yml`, GitHub's compare API on the public repository), and every version is resolved
against the registry before anything is built or signed. A `workflow_dispatch` (a person re-running
a hand-over) goes through the same reader with the same rules.

    platform-set-dispatch.py read --event "$GITHUB_EVENT_PATH" --event-name "$GITHUB_EVENT_NAME"
        → GITHUB_OUTPUT lines: run_url, run_core_sha, set_version, set_core_sha, armed,
          armed_version, armed_core_sha, armed_plugins_sha, shas (space-separated, de-duplicated)
    platform-set-dispatch.py --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EVENT_TYPE = "platform-set-sealed"
SCHEMA = 1
SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
URL = re.compile(r"^https://[^\s\"'`$\\]+$")


class Refusal(Exception):
    pass


def _sha(v: object, field: str, allow_empty: bool) -> str:
    s = "" if v is None else str(v).strip()
    if not s and allow_empty:
        return ""
    if not SHA.fullmatch(s):
        raise Refusal(f"{field} must be a full 40-hex commit sha, got {s!r}")
    return s


def _version(v: object, field: str) -> str:
    s = "" if v is None else str(v).strip()
    if s and not VERSION.fullmatch(s):
        raise Refusal(f"{field} must be a platform version (x.y.z[-pre]) or empty, got {s!r}")
    return s


def _bool(v: object, field: str) -> bool:
    if isinstance(v, bool):
        return v
    s = "" if v is None else str(v).strip().lower()
    if s in ("true", "false"):
        return s == "true"
    raise Refusal(f"{field} must be true or false, got {v!r}")


def read_payload(p: object) -> dict:
    """The validated hand-over. Raises Refusal naming the first field that is wrong."""
    if not isinstance(p, dict):
        raise Refusal("client_payload is not an object")
    if p.get("schema") != SCHEMA:
        raise Refusal(f"client_payload.schema must be {SCHEMA}, got {p.get('schema')!r} — "
                      "the sender and this reader disagree on the contract; change both together")
    run = p.get("run") if isinstance(p.get("run"), dict) else None
    st = p.get("set") if isinstance(p.get("set"), dict) else None
    armed = p.get("armed") if isinstance(p.get("armed"), dict) else None
    if run is None or st is None or armed is None:
        raise Refusal("client_payload must carry the objects run, set and armed")
    url = str(run.get("url") or "").strip()
    if not URL.fullmatch(url):
        raise Refusal(f"run.url must be an https URL, got {url!r}")
    out = {"run_url": url, "run_core_sha": _sha(run.get("coreSha"), "run.coreSha", False)}
    out["set_version"] = _version(st.get("version"), "set.version")
    out["set_core_sha"] = _sha(st.get("coreSha"), "set.coreSha", True)
    if bool(out["set_version"]) != bool(out["set_core_sha"]):
        raise Refusal("set.version and set.coreSha are both present or both empty — a version without "
                      "its commit (or a commit without its version) names no set")
    is_armed = _bool(armed.get("armed"), "armed.armed")
    out["armed"] = "true" if is_armed else "false"
    out["armed_version"] = _version(armed.get("version"), "armed.version")
    out["armed_core_sha"] = _sha(armed.get("coreSha"), "armed.coreSha", True)
    out["armed_plugins_sha"] = _sha(armed.get("pluginsSha"), "armed.pluginsSha", True)
    if is_armed and not (out["armed_version"] and out["armed_core_sha"] and out["armed_plugins_sha"]):
        raise Refusal("armed.armed is true but armed.version / coreSha / pluginsSha are not all present")
    if not is_armed and (out["armed_version"] or out["armed_core_sha"] or out["armed_plugins_sha"]):
        raise Refusal("armed.armed is false but the payload names an armed set — contradictory")
    # The core commits the workflow proves are on the platform's main (plugins_sha is a
    # MeshWeaver.Plugins commit the receiver records; it is not a platform commit).
    shas = [out["run_core_sha"], out["set_core_sha"], out["armed_core_sha"]]
    out["shas"] = " ".join(dict.fromkeys(s for s in shas if s))
    return out


def read_event(event: dict, event_name: str) -> dict:
    if event_name == "repository_dispatch":
        action = event.get("action")
        if action != EVENT_TYPE:
            raise Refusal(f"repository_dispatch of type {action!r} — this workflow reads only {EVENT_TYPE!r}")
        return read_payload(event.get("client_payload"))
    if event_name == "workflow_dispatch":
        i = event.get("inputs") or {}
        return read_payload({
            "schema": SCHEMA,
            "run": {"url": i.get("run_url"), "coreSha": i.get("run_core_sha")},
            "set": {"version": i.get("set_version"), "coreSha": i.get("set_core_sha")},
            # A person re-running a hand-over names no arming: the hand-over reads the newest
            # armed set from the registry, as it always does, and records no commits for it.
            "armed": {"armed": False},
        })
    raise Refusal(f"event {event_name!r} carries no hand-over (only repository_dispatch / workflow_dispatch do)")


def _self_test() -> int:
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    good = {"schema": 1, "run": {"url": "https://github.com/o/r/actions/runs/1", "coreSha": a},
            "set": {"version": "3.1.10412", "coreSha": a},
            "armed": {"armed": True, "version": "3.1.10400", "coreSha": b, "pluginsSha": c}}
    fails = []

    def check(name, fn, want):
        try:
            got = fn()
            ok = False if want is REFUSED else want(got)
        except Refusal as e:
            got, ok = f"Refusal: {e}", want is REFUSED
        except Exception as e:  # a crash is never a refusal
            got, ok = f"{type(e).__name__}: {e}", False
        if not ok:
            fails.append(f"{name}: {got}")

    REFUSED = object()

    def refuse(p):
        return lambda: read_payload(p)

    def mod(**kw):
        p = json.loads(json.dumps(good))
        for k, v in kw.items():
            sect, _, field = k.partition("__")
            if field:
                p[sect][field] = v
            else:
                p[sect] = v
        return p

    check("a good payload reads", lambda: read_payload(good),
          lambda o: o["set_version"] == "3.1.10412" and o["armed"] == "true" and o["shas"] == f"{a} {b}")
    check("an empty set is a statement", lambda: read_payload(mod(set__version="", set__coreSha="")),
          lambda o: o["set_version"] == "" and o["set_core_sha"] == "")
    check("not armed reads", lambda: read_payload(mod(armed={"armed": False})),
          lambda o: o["armed"] == "false" and o["armed_version"] == "")
    check("armed as the string 'true' reads", lambda: read_payload(mod(armed__armed="true")),
          lambda o: o["armed"] == "true")
    for name, p in [
        ("a wrong schema", mod(schema=2)),
        ("a missing run", {k: v for k, v in good.items() if k != "run"}),
        ("a short run sha", mod(run__coreSha="abc1234")),
        ("an http run url", mod(run__url="http://github.com/x")),
        ("a url with a shell metacharacter", mod(run__url="https://x/$(id)")),
        ("a version that is not a version", mod(set__version="latest")),
        ("a version without its commit", mod(set__coreSha="")),
        ("a commit without its version", mod(set__version="")),
        ("armed without its commits", mod(armed__pluginsSha="")),
        ("not armed but naming a set", mod(armed__armed=False)),
        ("armed that is not a boolean", mod(armed={"armed": "yes"})),
        ("an upper-case sha", mod(set__coreSha="A" * 40)),
    ]:
        check(f"refuses {name}", refuse(p), REFUSED)
    check("refuses another dispatch type",
          lambda: read_event({"action": "something-else", "client_payload": good}, "repository_dispatch"), REFUSED)
    check("reads its own dispatch type",
          lambda: read_event({"action": EVENT_TYPE, "client_payload": good}, "repository_dispatch"),
          lambda o: o["run_core_sha"] == a)
    check("a workflow_dispatch reads, never armed",
          lambda: read_event({"inputs": {"run_url": "https://github.com/o/r/actions/runs/2", "run_core_sha": a,
                                         "set_version": "3.1.1", "set_core_sha": a}}, "workflow_dispatch"),
          lambda o: o["armed"] == "false" and o["set_version"] == "3.1.1")
    check("refuses a push event", lambda: read_event({}, "push"), REFUSED)
    if fails:
        print("platform-set-dispatch self-test FAILED:")
        for f in fails:
            print("  - " + f)
        return 1
    print("platform-set-dispatch self-test: all cases passed (the contract, every refusal, both events)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("read")
    r.add_argument("--event", type=Path, required=True)
    r.add_argument("--event-name", required=True)
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.cmd != "read":
        ap.error("read or --self-test")
    try:
        out = read_event(json.loads(a.event.read_text()), a.event_name)
    except Refusal as e:
        print(f"::error::the platform's hand-over is refused: {e}")
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
