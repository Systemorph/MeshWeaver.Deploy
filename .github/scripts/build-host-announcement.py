#!/usr/bin/env python3
"""build-host-announcement.py — core's CD hands the promoted set to the standing build host of a channel.

Rule 2 of the standing build hosts (MeshWeaver.Plugins `Hosting/StandingBuildHosts` §2, *How the
platform layer follows*; platform reference Doc/Architecture/BuildHosts): when core's CD ARMS a set,
the same run rolls `build-latest` onto it. It does so the way CD hands control its next image —
a SIGNED event into the control plane's inbox (`Hosting/PlatformBuilds`), which the operations
instance turns into a change of ITS OWN record (`buildHosts.<channel>.testerDigest`, `.portalDigest`,
`.platformVersion`) and the record's Reconcile. Never `kubectl set image`, never a hand-edited
Deployment: the record is the only source of truth for what the host runs.

    build-host-announcement.py decide --declaration .github/build-hosts.json \
        --control-instance .github/control-instance.json --channel latest|stable \
        [--trigger schedule]   (stable: only on build-host-stable.yml's daily schedule)
        → prints `enabled=true|false`, `deployment=<operations record>`, `why=<sentence>`
          (GITHUB_OUTPUT lines)
    build-host-announcement.py body --deployment control --channel latest --version 3.1.10412 \
        --tester-image cr.example.com/mw-plugin-test --tester-digest sha256:… \
        --portal-image cr.example.com/memex-portal-ai --portal-digest sha256:… \
        --runner-image cr.example.com/mw-build-host --runner-digest sha256:… \
        --core-sha <40 hex> [--plugins-sha <40 hex>] --instance <run url> \
        [--supersedes "sha256:… sha256:…"]
        → the event JSON, one line (the bytes that are signed are the bytes that are sent)
    build-host-announcement.py channels --declaration .github/build-hosts.json --version 3.1.13071
        → `channels=<the declared channels this set moves>` (GITHUB_OUTPUT line): its LINE's channel
          `latest<major>`, and `latest` for a set of the line it names (see THE LINES)
    build-host-announcement.py --self-test

🚨 THE LINES — A CHANNEL NEVER CROSSES A MAJOR (policy `major-lines-run-separately`, platform
Doc/Architecture/MajorLinesRunSeparately; the incident of core #8129). Each platform major is its own
line with its own channel, `latest<major>` (`latest3`, `latest4`) — an OPEN vocabulary: a new major
needs a declared channel here and on the record, no change to this script. The bare `latest` is the
v3 line's COMPATIBILITY name (`LATEST_ALIAS_MAJOR`), kept while any reader still follows it (the
standing `build-host-latest`, a leased host's `follow-set.py`). A set is handed to EVERY declared
channel of its own major — `latest3` and `latest` for a 3.x set — as ONE event per channel: each
event moves only its own channel's key on the record, so nothing is applied twice, and the receiver
answers a re-send of the set a channel already runs with "nothing changed". A set is never handed to
a channel of another major: `body` refuses it here, and the receiver (MeshWeaver.Plugins
`BuildHostUpdates.Decide`) refuses it again.

🚨 A NO-OP UNTIL DECLARED. `.github/build-hosts.json` says whether the platform's estate runs
standing build hosts at all (`enabled`) and which channels CD rolls (`channels`). While it says
`false` the CD job writes one summary line and sends nothing — and the RECEIVER guards the same
fact a second time: it changes a record only when that record's `buildHosts.enabled` is true, so
an event cannot switch the hosts on by itself. Both halves default to off.

🚨 NEVER BACKWARDS. The job sends only for the newest ARMED set (the same reading
`notify-platform-update` makes), and the receiver refuses a `platformVersion` older than the one
the record names.

🚨 …AND THE RUNNER NEVER BACKWARDS EITHER. A set's runner follows this repository's main
(platform-set-dispatch.py, THE RUNNER FOLLOWS DEPLOY'S MAIN), so one set can be handed over again
with a NEWER runner. `supersedesRunnerDigests` names every older runner of the same set; the
receiver takes a runner for the SAME set only when the runner it holds is among them, so a re-run
of an old hand-over (whose runner is older than the one held) can never move the host back.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

EVENT = "build-host-update-available"
POLICY = "build-hosts-follow-latest"
REPORTER = "main-cd build-host-latest"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CHANNELS = ("latest", "stable")
# A major line's channel: `latest<major>` (policy `major-lines-run-separately`). Open — any major.
LINE_CHANNEL = re.compile(r"^latest([1-9][0-9]{0,3})$")
# The line the compatibility name `latest` belongs to: the v3 line, which every reader followed before
# the lines split. MeshWeaver.Plugins `BuildHostUpdates.CompatibilityAliasMajor` names the same major.
LATEST_ALIAS_MAJOR = 3
# Which channel each CD trigger may roll. `latest` follows the set CD ARMS (any trigger that arms);
# `stable` — until core's stable promoter exists — is the main-passed ceiling, handed on by the
# DAILY `schedule` of build-host-stable.yml only (§2: "by the daily full run, to the main-passed
# ceiling"; not main-cd.yml, which reads no MeshWeaver.Plugins verdict).
CD_CHANNELS = {"latest": None, "stable": "schedule"}
SET_NAME = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_-]*$")
# MeshWeaver.Plugins BuildHostUpdates.MaxSupersededRunners — a longer list is refused there.
MAX_SUPERSEDED = 64


def is_channel(channel: object) -> bool:
    """A channel this lane may name: `latest`, `stable`, or a line's `latest<major>`. Pure."""
    return isinstance(channel, str) and (channel in CHANNELS or LINE_CHANNEL.match(channel) is not None)


def channel_major(channel: str) -> int | None:
    """The major a channel's NAME commits it to: `latest<N>` → N, `latest` → LATEST_ALIAS_MAJOR, any other
    (`stable`) → None — no line, no major guard. Pure."""
    if channel == "latest":
        return LATEST_ALIAS_MAJOR
    m = LINE_CHANNEL.match(channel or "")
    return int(m.group(1)) if m else None


def set_major(version: str) -> int | None:
    """The MAJOR of a set name (`3.1.13071` → 3, `3.0.0-ci.10052` → 3, `4.0.13200` → 4); None for no set name. Pure."""
    return int(version.split(".", 1)[0]) if SET_NAME.match(version or "") else None


def load_declaration(path: Path) -> dict:
    return load_declaration_doc(json.loads(path.read_text()), path)


def load_declaration_doc(d: object, path: object = ".github/build-hosts.json") -> dict:
    if not isinstance(d, dict) or type(d.get("enabled")) is not bool:
        raise ValueError(f"{path}: `enabled` must be true or false")
    ch = d.get("channels")
    if not isinstance(ch, list) or not all(is_channel(c) for c in ch) or len(set(ch)) != len(ch):
        raise ValueError(f"{path}: `channels` must be a list of distinct names drawn from {list(CHANNELS)} or latest<major>")
    return d


def channels_for(declaration: dict, version: str) -> list[str]:
    """The declared channels CD's newest armed set `version` moves, in send order: its line's own
    `latest<major>` first, then the compatibility `latest` when the set is of the line `latest` names.
    `stable` is never one (build-host-stable.yml's daily lane rolls it). Empty when the estate does not
    run build hosts or declares no channel of the set's major. Raises ValueError for no set name. Pure."""
    major = set_major(version)
    if major is None:
        raise ValueError(f"version {version!r} is not a platform set name")
    if not declaration["enabled"]:
        return []
    line = f"latest{major}"
    ordered = [line, "latest"]
    return [c for c in ordered if c in declaration["channels"] and channel_major(c) == major]


def decide(declaration: dict, control: dict, channel: str, trigger: str = "") -> tuple[bool, str, str]:
    """(send?, deployment, why). Pure."""
    dep = str(control.get("deployment") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", dep):
        raise ValueError(f"control-instance.json names no plain `deployment` ({dep!r}) — the operations record "
                         f"the build hosts live on")
    if channel not in CD_CHANNELS and not LINE_CHANNEL.match(channel or ""):
        raise ValueError(f"channel {channel!r} is not a channel CD rolls ({list(CD_CHANNELS)} or latest<major>)")
    only_on = CD_CHANNELS.get(channel)
    if only_on is not None and trigger != only_on:
        raise ValueError(f"channel {channel!r} is rolled only by the {only_on!r} trigger (this is {trigger or 'no trigger'!r})")
    if not declaration["enabled"]:
        return False, dep, ("standing build hosts are not enabled for this estate (.github/build-hosts.json "
                            "enabled: false) — nothing is sent")
    if channel not in declaration["channels"]:
        return False, dep, f"channel {channel} is not in .github/build-hosts.json channels — nothing is sent"
    return True, dep, f"build-{channel} follows the armed set on record {dep}"


def body(*, deployment: str, channel: str, version: str, tester_image: str, tester_digest: str,
         portal_image: str, portal_digest: str, runner_image: str, runner_digest: str, core_sha: str,
         plugins_sha: str, instance: str, detected_at: str, supersedes: list[str] | None = None) -> dict:
    """The signed event. Pure; every field validated — a fact naming something malformed is worse
    than no fact."""
    if not is_channel(channel):
        raise ValueError(f"channel {channel!r} is not one of {list(CHANNELS)} or latest<major>")
    if not SET_NAME.match(version or ""):
        raise ValueError(f"version {version!r} is not a platform set name")
    if (line := channel_major(channel)) is not None and set_major(version) != line:
        raise ValueError(f"{version} is not a {line}.x set, and build-{channel} is the {line}.x line's channel — "
                         "a channel never crosses a major; refusing to sign it")
    for name, d in (("tester", tester_digest), ("portal", portal_digest), ("runner", runner_digest)):
        if not DIGEST.match(d or ""):
            raise ValueError(f"{name} digest {d!r} is not sha256:<64 hex>")
    for name, r in (("tester", tester_image), ("portal", portal_image), ("runner", runner_image)):
        if not REPOSITORY.match(r or "") or ":" in r.rsplit("/", 1)[-1] or "@" in r:
            raise ValueError(f"{name} image {r!r} is not a plain repository (no tag, no digest)")
    if core_sha and not SHA.match(core_sha):
        raise ValueError(f"core sha {core_sha!r} is not a full commit")
    if plugins_sha and not SHA.match(plugins_sha):
        raise ValueError(f"plugins sha {plugins_sha!r} is not a full commit")
    superseded = list(dict.fromkeys(supersedes or []))
    for d in superseded:
        if not DIGEST.match(d or ""):
            raise ValueError(f"superseded runner digest {d!r} is not sha256:<64 hex>")
    if runner_digest in superseded:
        raise ValueError("the runner handed over is named as superseded by itself — refusing a contradictory event")
    if len(superseded) > MAX_SUPERSEDED:
        raise ValueError(f"{len(superseded)} superseded runners — at most {MAX_SUPERSEDED} (the receiver's bound)")
    prefix = f"buildHosts.{channel}"
    # Key order is the wire order: `event` first.
    return {
        "event": EVENT,
        "deployment": deployment,
        "channel": channel,
        "platformVersion": version,
        "testerImage": tester_image,
        "testerDigest": tester_digest,
        "portalImage": portal_image,
        "portalDigest": portal_digest,
        "runnerImage": runner_image,
        "runnerDigest": runner_digest,
        # The set's OLDER runners: the receiver replaces a runner of the SAME set only when it holds one of these.
        "supersedesRunnerDigests": superseded,
        "coreSha": core_sha or None,     # empty when CD re-sends a set another run armed
        "pluginsSha": plugins_sha or None,
        # What the receiver writes on the record, spelled as the chart reads it.
        "recordFields": {
            f"{prefix}.platformVersion": version,
            f"{prefix}.testerDigest": tester_digest,
            f"{prefix}.portalDigest": portal_digest,
            f"{prefix}.runnerDigest": runner_digest,
        },
        "policy": POLICY,
        "trigger": "build-host-latest",
        "reporter": REPORTER,
        "instance": instance,
        "detectedAt": detected_at,
    }


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--declaration", type=Path, default=Path(".github/build-hosts.json"))
    d.add_argument("--control-instance", type=Path, default=Path(".github/control-instance.json"))
    d.add_argument("--channel", default="latest")
    d.add_argument("--trigger", default="")
    c = sub.add_parser("channels")
    c.add_argument("--declaration", type=Path, default=Path(".github/build-hosts.json"))
    c.add_argument("--version", required=True)
    b = sub.add_parser("body")
    for k in ("deployment", "channel", "version", "tester-image", "tester-digest", "portal-image",
              "portal-digest", "runner-image", "runner-digest", "instance"):
        b.add_argument(f"--{k}", required=True)
    b.add_argument("--core-sha", default="")
    b.add_argument("--plugins-sha", default="")
    b.add_argument("--supersedes", default="", help="space-separated digests of the set's OLDER runners")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "channels":
            print(f"channels={' '.join(channels_for(load_declaration(a.declaration), a.version))}")
            return 0
        if a.cmd == "decide":
            send, dep, why = decide(load_declaration(a.declaration),
                                    json.loads(a.control_instance.read_text()), a.channel, a.trigger)
            print(f"enabled={'true' if send else 'false'}")
            print(f"deployment={dep}")
            print(f"why={why}")
            return 0
        ev = body(deployment=a.deployment, channel=a.channel, version=a.version,
                  tester_image=a.tester_image, tester_digest=a.tester_digest,
                  portal_image=a.portal_image, portal_digest=a.portal_digest,
                  runner_image=a.runner_image, runner_digest=a.runner_digest,
                  core_sha=a.core_sha, plugins_sha=a.plugins_sha, instance=a.instance,
                  supersedes=a.supersedes.split(),
                  detected_at=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        print(json.dumps(ev, separators=(",", ":")))
        return 0
    except (ValueError, OSError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1


def self_test() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: object = "") -> None:
        if not cond:
            failures.append(f"{name}: {detail}")

    control = {"deployment": "control", "url": "https://control.example.com", "platformLagBoundMinutes": 180}
    off = {"enabled": False, "channels": ["latest"]}
    on = {"enabled": True, "channels": ["latest"]}
    send, dep, why = decide(off, control, "latest")
    check("decide/off is a no-op", send is False and dep == "control" and "not enabled" in why, (send, why))
    send, _, _ = decide(on, control, "latest")
    check("decide/on sends", send is True)
    send, _, why = decide({"enabled": True, "channels": []}, control, "latest")
    check("decide/channel not declared", send is False and "not in" in why, why)
    for bad_ctl, bad_ch, trig in (({"deployment": ""}, "latest", ""), (control, "stable", ""),
                                  (control, "stable", "push"), (control, "nightly", "schedule")):
        try:
            decide(on, bad_ctl, bad_ch, trig)
            check(f"decide/refuses {bad_ctl} {bad_ch} {trig}", False, "accepted")
        except ValueError:
            pass
    both = {"enabled": True, "channels": ["latest", "stable"]}
    send, _, why = decide(both, control, "stable", "schedule")
    check("decide/stable on the daily schedule sends", send is True, why)
    try:
        decide(both, control, "stable", "workflow_dispatch")
        check("decide/stable on a dispatch is refused", False, "accepted")
    except ValueError:
        pass
    send, _, why = decide(on, control, "stable", "schedule")
    check("decide/stable not declared is a no-op", send is False and "not in" in why, why)
    send, _, _ = decide(both, control, "latest", "push")
    check("decide/latest follows any arming trigger", send is True)

    ok = dict(deployment="control", channel="latest", version="3.1.10412",
              tester_image="cr.example.com/mw-plugin-test", tester_digest="sha256:" + "1" * 64,
              portal_image="cr.example.com/memex-portal-ai", portal_digest="sha256:" + "2" * 64,
              runner_image="cr.example.com/mw-build-host", runner_digest="sha256:" + "3" * 64,
              core_sha="a" * 40, plugins_sha="b" * 40, instance="https://github.com/x/y/actions/runs/1",
              detected_at="2026-01-01T00:00:00Z")
    ev = body(**ok)
    check("body/event first", list(ev)[0] == "event" and ev["event"] == EVENT, list(ev))
    check("body/record fields as the chart reads them", ev["recordFields"] == {
        "buildHosts.latest.platformVersion": "3.1.10412",
        "buildHosts.latest.testerDigest": "sha256:" + "1" * 64,
        "buildHosts.latest.portalDigest": "sha256:" + "2" * 64,
        "buildHosts.latest.runnerDigest": "sha256:" + "3" * 64}, ev["recordFields"])
    check("body/a re-sent set without its core sha says null", body(**dict(ok, core_sha=""))["coreSha"] is None)
    check("body/names no superseded runner by default (an empty list, never absent)", ev["supersedesRunnerDigests"] == [], ev)
    older = ["sha256:" + "4" * 64, "sha256:" + "5" * 64]
    check("body/names the set's older runners", body(**dict(ok, supersedes=older + older[:1]))["supersedesRunnerDigests"] == older)
    for bad in (["sha256:abc"], ["sha256:" + "3" * 64], ["sha256:%064x" % i for i in range(MAX_SUPERSEDED + 1)]):
        try:
            body(**dict(ok, supersedes=bad))
            check(f"body/refuses supersedes={bad[:1]}…", False, "accepted")
        except ValueError:
            pass
    check("body/ci set name accepted", body(**dict(ok, version="3.0.0-ci.10052"))["platformVersion"] == "3.0.0-ci.10052")
    for k, v in (("tester_digest", "3.1.10412"), ("portal_digest", "sha256:abc"), ("runner_digest", ""),
                 ("runner_image", "cr.example.com/mw-build-host:latest"),
                 ("tester_image", "cr.example.com/mw-plugin-test:3.1.1"),
                 ("portal_image", "cr.example.com/memex-portal-ai@sha256:" + "2" * 64),
                 ("core_sha", "abc1234"), ("version", "latest"), ("channel", "nightly")):
        try:
            body(**dict(ok, **{k: v}))
            check(f"body/refuses {k}={v}", False, "accepted")
        except ValueError:
            pass
    # THE LINES (policy major-lines-run-separately; core #8129): a set moves every declared channel of its
    # own major, one event each, and never a channel of another major.
    lines = {"enabled": True, "channels": ["latest", "latest3", "latest4"]}
    check("lines/a 3.x set moves its line's channel first, then the compatibility latest",
          channels_for(lines, "3.1.13071") == ["latest3", "latest"], channels_for(lines, "3.1.13071"))
    check("lines/a 3.0.0-ci set is of the v3 line too", channels_for(lines, "3.0.0-ci.10052") == ["latest3", "latest"])
    check("lines/a 4.x set moves latest4 ALONE — never latest or latest3 (the incident)",
          channels_for(lines, "4.0.13200") == ["latest4"], channels_for(lines, "4.0.13200"))
    check("lines/a set of a line nobody declared moves nothing", channels_for(lines, "5.0.1") == [])
    check("lines/today's declaration (latest only) moves latest for a 3.x set — as before",
          channels_for({"enabled": True, "channels": ["latest"]}, "3.1.13071") == ["latest"])
    check("lines/…and nothing for a 4.x set (latest is the v3 line's name)",
          channels_for({"enabled": True, "channels": ["latest"]}, "4.0.13200") == [])
    check("lines/stable is never moved by the CD hand-over", channels_for({"enabled": True, "channels": ["latest3", "stable"]}, "3.1.13071") == ["latest3"])
    check("lines/off moves nothing", channels_for(dict(lines, enabled=False), "3.1.13071") == [])
    try:
        channels_for(lines, "latest")
        check("lines/a moving name is no set — refused", False, "accepted")
    except ValueError:
        pass
    check("lines/the vocabulary", channel_major("latest") == 3 and channel_major("latest3") == 3 and channel_major("latest4") == 4
          and channel_major("stable") is None and channel_major("latest03") is None and channel_major("latest-x") is None)
    send, _, _ = decide({"enabled": True, "channels": ["latest3"]}, control, "latest3")
    check("decide/a declared line channel follows any arming trigger", send is True)
    for v, ch in (("3.1.13071", "latest3"), ("3.1.13071", "latest"), ("4.0.13200", "latest4")):
        check(f"body/{v} is signed for {ch}", body(**dict(ok, version=v, channel=ch))["channel"] == ch)
    check("body/the record fields name the line's channel", "buildHosts.latest3.platformVersion" in body(**dict(ok, channel="latest3"))["recordFields"])
    # NEGATIVE CONTROLS — each would have moved a v3 reader onto v4 (or the reverse) under a lane that compared nothing.
    for v, ch in (("4.0.13200", "latest"), ("4.0.13200", "latest3"), ("3.1.13071", "latest4")):
        try:
            body(**dict(ok, version=v, channel=ch))
            check(f"body/refuses {v} on {ch}", False, "accepted")
        except ValueError as e:
            check(f"body/the refusal of {v} on {ch} is named", "never crosses a major" in str(e), str(e))
    check("body/stable takes any major (no line, no guard)", body(**dict(ok, version="4.0.13200", channel="stable"))["channel"] == "stable")
    for bad in (["latest", "latest"], ["latest-x"], ["latest0"], "latest"):
        try:
            load_declaration_doc({"enabled": True, "channels": bad})
            check(f"declaration/refuses channels={bad!r}", False, "accepted")
        except ValueError:
            pass
    shipped = Path(__file__).resolve().parents[1] / "build-hosts.json"
    try:
        shipped_decl = load_declaration(shipped)
        check("the shipped declaration hands a 3.x set to latest (the compatibility name every current reader follows)",
              "latest" in channels_for(shipped_decl, "3.1.13071"), channels_for(shipped_decl, "3.1.13071"))
        check("the shipped declaration never hands a 4.x set to latest or latest3",
              not {"latest", "latest3"} & set(channels_for(shipped_decl, "4.0.13200")), channels_for(shipped_decl, "4.0.13200"))
    except (ValueError, OSError) as e:
        check("the shipped .github/build-hosts.json validates", False, str(e))
    if failures:
        for f in failures:
            print(f"SELF-TEST FAILED: {f}")
        return 1
    print("build-host-announcement self-test: all cases passed (no-op while off, channel declaration, "
          "stable only on the daily schedule, the lines (a set moves only its own major's channels, one event each), "
          "record fields incl. the runner, the superseded runners, "
          "digest/repository/sha/version refusals, the shipped declaration)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
