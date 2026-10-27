#!/usr/bin/env python3
"""build-host-platforms.py — which platforms does an `mw-build-host:<version>` tag carry?

    build-host-platforms.py read --raw <manifest.json> [--image <image.json>] [--want linux/amd64 ...]
        prints `known=true|false`, `platforms=<os/arch ...>`, `missing=<os/arch ...>`

The `image` job of build-host.yml keeps a version's runner once it is written, so a tag written
before the runner was multi-arch can be a SINGLE-platform image. This answers which platforms the
tag can run on, from the two readings `docker buildx imagetools inspect` gives:

  --raw    the tag's raw manifest (`--raw`). An INDEX names its platforms on its `manifests`
           (attestation entries, `unknown/unknown`, are not platforms).
  --image  the tag's image config (`--format '{{json .Image}}'`). A single IMAGE MANIFEST carries
           no platform of its own: its os/architecture live only in the config.

🚨 An answer that cannot be established is `known=false`, never an empty platform list: an empty
list reads as "carries no platform" and would name EVERY wanted platform as missing — including the
one the image was built for (MeshWeaver.Deploy#30 review). `missing` is empty when `known=false`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WANT = ["linux/amd64", "linux/arm64"]


def index_platforms(raw: dict) -> list[str]:
    """The platforms an INDEX names. An image manifest has no `manifests`, so this is empty for it."""
    out = []
    for m in raw.get("manifests") or []:
        p = m.get("platform") or {}
        os_, arch = p.get("os"), p.get("architecture")
        if isinstance(os_, str) and isinstance(arch, str) and os_ != "unknown" and arch != "unknown":
            out.append(f"{os_}/{arch}")
    return out


def platforms(raw: dict, image: object | None) -> list[str] | None:
    """The tag's platforms, or None when they cannot be established."""
    if isinstance(raw.get("manifests"), list):
        found = index_platforms(raw)
        return found or None
    if isinstance(raw.get("config"), dict):
        if isinstance(image, dict):
            os_, arch = image.get("os"), image.get("architecture")
            if isinstance(os_, str) and os_ and isinstance(arch, str) and arch:
                return [f"{os_}/{arch}"]
        return None
    return None


def read(raw: dict, image: object | None, want: list[str]) -> dict[str, str]:
    found = platforms(raw, image)
    if found is None:
        return {"known": "false", "platforms": "", "missing": ""}
    return {"known": "true", "platforms": " ".join(found),
            "missing": " ".join(w for w in want if w not in found)}


def self_test() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{name}: {detail}")

    index = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": "sha256:a", "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": "sha256:b", "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        {"digest": "sha256:c", "platform": {"os": "unknown", "architecture": "unknown"}}]}
    amd64_index = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": "sha256:a", "platform": {"os": "linux", "architecture": "amd64"}}]}
    single = {"mediaType": "application/vnd.oci.image.manifest.v1+json",
              "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "sha256:d"},
              "layers": []}
    amd64_config = {"os": "linux", "architecture": "amd64", "config": {}}
    arm64_config = {"os": "linux", "architecture": "arm64", "config": {}}

    r = read(index, None, WANT)
    check("a two-platform index carries both (attestations are not platforms)",
          r == {"known": "true", "platforms": "linux/amd64 linux/arm64", "missing": ""}, str(r))
    r = read(amd64_index, None, WANT)
    check("an amd64-only index misses arm64 and only arm64",
          r == {"known": "true", "platforms": "linux/amd64", "missing": "linux/arm64"}, str(r))
    # The review's case: a SINGLE-platform image manifest, read from its image config.
    r = read(single, amd64_config, WANT)
    check("a single amd64 image manifest is amd64 (from its config), missing only arm64",
          r == {"known": "true", "platforms": "linux/amd64", "missing": "linux/arm64"}, str(r))
    r = read(single, arm64_config, WANT)
    check("a single arm64 image manifest is arm64, missing only amd64",
          r == {"known": "true", "platforms": "linux/arm64", "missing": "linux/amd64"}, str(r))
    r = read(single, None, WANT)
    check("a single image manifest with no readable config is UNKNOWN, never 'missing everything'",
          r == {"known": "false", "platforms": "", "missing": ""}, str(r))
    r = read(single, {"os": "linux"}, WANT)
    check("a config without an architecture is unknown", r["known"] == "false", str(r))
    r = read({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": "sha256:c", "platform": {"os": "unknown", "architecture": "unknown"}}]}, None, WANT)
    check("an index of attestations only is unknown", r["known"] == "false", str(r))
    # NEGATIVE CONTROL: the reading this replaced looked at the index entries alone. On the single
    # manifest it yields NO platform — so 'missing' would have named amd64 too. The cases above
    # therefore fail on that defect; this proves the input really is the shape that triggers it.
    old = index_platforms(single)
    check("negative control: the index-only reading finds nothing on a single manifest", old == [], str(old))
    old_missing = [w for w in WANT if w not in old]
    check("negative control: the index-only reading would warn about BOTH platforms",
          old_missing == WANT, str(old_missing))

    if failures:
        print("build-host-platforms self-test FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("build-host-platforms self-test: all cases passed (index, amd64-only index, single manifest "
          "via its config both ways, unreadable config, attestation-only index, the index-only negative control)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("read")
    r.add_argument("--raw", type=Path, required=True)
    r.add_argument("--image", type=Path)
    r.add_argument("--want", nargs="+", default=WANT)
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.cmd != "read":
        ap.error("a subcommand is required (read), or --self-test")
    raw = json.loads(a.raw.read_text())
    image = None
    if a.image is not None and a.image.exists() and a.image.stat().st_size > 0:
        try:
            image = json.loads(a.image.read_text())
        except json.JSONDecodeError:
            image = None
    for k, v in read(raw, image, a.want).items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
