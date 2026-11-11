#!/usr/bin/env bash
# Pack what the memex-local formula installs — deploy/homebrew (CLI, share assets, formula) and
# deploy/helm (the chart it vendors) — into ONE reproducible tarball, laid out under a
# memex-local-<version>/ prefix so the formula's `install` paths are identical for a HEAD install
# (git checkout) and a stable one (this tarball).
#
# Usage: make-tarball.sh <version> <out.tar.gz> [<git ref>]
#
# Used by .github/workflows/homebrew.yml twice: on every PR to install + test the formula from the
# PR's own tree (file:// URL), and on main to attach the tarball to a release of the published tap
# (Systemorph/homebrew-memex). Nothing else in the repository is needed at install time — the
# whole MeshWeaver tree (~100 MB gzipped) would make `brew install` a source download of the
# platform for a ~100 KB CLI.
set -euo pipefail

version="${1:?usage: make-tarball.sh <version> <out.tar.gz> [<git ref>]}"
out="${2:?usage: make-tarball.sh <version> <out.tar.gz> [<git ref>]}"
ref="${3:-HEAD}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
{ [ -f "$repo/deploy/helm/Chart.yaml" ] && [ -d "$repo/deploy/homebrew" ]; } || { echo "not a MeshWeaver.Deploy checkout: $repo" >&2; exit 1; }

mkdir -p "$(dirname "$out")"
# `gzip -n` omits the timestamp so the same tree yields the same bytes — the sha256 the formula
# pins is then a statement about CONTENT, and a re-run of the publish step is a no-op.
# 🚨 The archived directories are READ FROM THE FORMULA's install lines, never listed here: a fixed
# list (deploy/homebrew deploy/helm) silently dropped deploy/backlease when the formula started
# installing it, so every tap install lacked first-attach.sh and `fleet join` skipped the build
# host on the second Mac Studio (2026-11-10) — a warn line on a join that otherwise succeeded.
# The list is read from the formula AT $ref — the tree being archived — so the extraction and the
# archive agree by construction (a working-tree formula differing from $ref cannot drop a directory).
formula_text="$(git -C "$repo" show "$ref:deploy/homebrew/Formula/memex-local.rb")"
# Every install line that names a deploy/ path must name it as a quoted "deploy/<dir>…" (a Dir[] glob
# or a plain path):
# a line with another shape is RED here, never silently left out of the tarball (the blind spot the
# assert loop below cannot see on its own).
odd="$(grep -E '\.install' <<<"$formula_text" | grep -E 'deploy/' | grep -vE '"deploy/[A-Za-z0-9_.-]+' || true)"
[ -z "$odd" ] || { echo "the formula installs from deploy/ in a shape make-tarball.sh does not read (expected a quoted \"deploy/<dir>…\"):" >&2; printf '%s\n' "$odd" >&2; exit 1; }
# Seeded with the two the CLI cannot work without, so the array is never empty (bash 3.2 + set -u).
dirs=(deploy/homebrew deploy/helm)
while IFS= read -r d; do
  [ -n "$d" ] || continue
  printf '%s\n' "${dirs[@]}" | grep -qxF "$d" || dirs+=("$d")
done < <(grep -E '\.install' <<<"$formula_text" | grep -oE '"deploy/[A-Za-z0-9_.-]+' | sed 's/^"//' | sort -u)
git -C "$repo" archive --format=tar --prefix="memex-local-${version}/" "$ref" \
  "${dirs[@]}" | gzip -n > "$out"

# A tarball that does not carry the CLI is a green step that installs nothing — assert the one
# file everything else depends on is inside, never "tar exited 0".
# 🚨 List to a variable, then grep — never `tar -tzf … | grep -q`: grep -q exits on its first match
# and closes the pipe, tar dies with "stdout: write error", and under pipefail the check reports
# the CLI as MISSING from a tarball that carries it (main's first tap publish, 2026-08-30).
entries="$(tar -tzf "$out")"
if ! grep -qxF "memex-local-${version}/deploy/homebrew/bin/memex-local" <<<"$entries"; then
  echo "tarball $out does not contain deploy/homebrew/bin/memex-local" >&2
  exit 1
fi
# …and every directory the formula installs from is IN it (an empty Dir[] installs nothing, quietly).
for d in "${dirs[@]}"; do
  grep -qE "^memex-local-${version}/${d}/." <<<"$entries" || { echo "tarball $out carries nothing under $d, which the formula installs" >&2; exit 1; }
done
grep -qxF "memex-local-${version}/deploy/backlease/first-attach.sh" <<<"$entries" \
  || { echo "tarball $out does not contain deploy/backlease/first-attach.sh (fleet join's build-host attach)" >&2; exit 1; }
printf '%s\n' "$out"
