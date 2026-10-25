#!/usr/bin/env bash
#
# stage-context.sh <deploy checkout> <platform checkout> <context dir>
#
# Assembles the build context of the build host's RUNNER image (`mw-build-host`, Dockerfile beside
# this file) from its two sources, at the paths the Dockerfile's COPY lines name:
#
#   .github/scripts/build-run.py           — the runner, from THIS repository
#   .github/scripts/compose-gate-host.sh   ┐
#   .github/scripts/sandbox-postgres.sh    │ the platform's own helpers, from the PLATFORM checkout
#   .github/scripts/module-build-key.py    │ (the set's commit): one implementation each, shared
#   .github/scripts/ci-artifact-store.py   ┘ with the platform's CI, read — never vendored here
#   artifacts/plugin-build/                — `meshweaver-plugin-build`, published from the platform
#                                            checkout (framework-dependent; the image's SDK carries
#                                            the runtime)
#
# 🛡️ No partial context: a missing source file, an empty one, or a publish that yields no
# MeshWeaver.Plugin.Build.dll is RED, naming it. The Dockerfile's .dockerignore admits exactly these
# paths, so nothing else of either checkout can reach the image.
set -euo pipefail

DEPLOY="${1:?usage: stage-context.sh <deploy checkout> <platform checkout> <context dir>}"
PLATFORM="${2:?usage: stage-context.sh <deploy checkout> <platform checkout> <context dir>}"
CTX="${3:?usage: stage-context.sh <deploy checkout> <platform checkout> <context dir>}"

[ -d "$PLATFORM/.github/scripts" ] || { echo "::error::$PLATFORM is not a platform checkout (no .github/scripts)"; exit 1; }
[ -f "$PLATFORM/src/MeshWeaver.Plugin.Build/MeshWeaver.Plugin.Build.csproj" ] \
  || { echo "::error::$PLATFORM has no src/MeshWeaver.Plugin.Build — the plugin-build tool cannot be published"; exit 1; }

rm -rf "$CTX"
mkdir -p "$CTX/.github/scripts" "$CTX/artifacts"
# Absolute from here on: the publish below runs INSIDE the platform checkout, so a relative context
# would put the plugin-build tool in a second, different directory.
CTX="$(cd -- "$CTX" && pwd)"
DEPLOY="$(cd -- "$DEPLOY" && pwd)"
PLATFORM="$(cd -- "$PLATFORM" && pwd)"

put() {  # put <source file> <name>
  [ -s "$1" ] || { echo "::error::stage-context: $1 is missing or empty"; exit 1; }
  cp "$1" "$CTX/.github/scripts/$2"
}
put "$DEPLOY/.github/scripts/build-run.py" build-run.py
for f in compose-gate-host.sh sandbox-postgres.sh module-build-key.py ci-artifact-store.py; do
  put "$PLATFORM/.github/scripts/$f" "$f"
done

# The platform's own global.json and Directory.*.props govern the publish, so it runs IN the
# platform checkout.
( cd "$PLATFORM" && dotnet publish src/MeshWeaver.Plugin.Build/MeshWeaver.Plugin.Build.csproj -c Release -o "$CTX/artifacts/plugin-build" -nologo )
[ -f "$CTX/artifacts/plugin-build/MeshWeaver.Plugin.Build.dll" ] \
  || { echo "::error::stage-context: the publish produced no MeshWeaver.Plugin.Build.dll"; exit 1; }

echo "staged $CTX: runner from $(git -C "$DEPLOY" rev-parse --short HEAD 2>/dev/null || echo '?'), helpers + plugin-build from platform $(git -C "$PLATFORM" rev-parse --short HEAD 2>/dev/null || echo '?')"
