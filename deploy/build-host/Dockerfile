# The standing build host's RUNNER image — `mw-build-host` (Doc/Architecture/BuildHosts).
#
# What a stage needs and nothing it does not: bash, python3, git, bubblewrap (every stage runs in a
# `bwrap` sandbox — the host refuses to start without it), the .NET 10 SDK (the src/ half still
# builds with it), the PostgreSQL SERVER BINARIES (no server runs: a stage that needs a database
# starts a disposable one inside its own sandbox with sandbox-postgres.sh), and the runner with its
# helpers — module-build-key.py (the host recomputes every module cache key before promoting it),
# ci-artifact-store.py (H9: the host hands every module bundle to the shared CI artifact store)
# and core's `meshweaver-plugin-build` (module-fetch: the plugins layer from the registry's index). The PLATFORM (the compiler and the reference set) is NOT
# here: it is the set's tester and portal images, copied in by the host's init containers by digest.
#
# 🧱 SELF-CONTAINED: every source is fetched BY COMMIT inside the build, from two PUBLIC repositories —
# the runner (`build-run.py`) from THIS repository at DEPLOY_COMMIT, the platform's helpers and the
# plugin-build tool from the platform at PLATFORM_COMMIT (the set's own commit). The build context is
# therefore never read, so nothing has to be staged and no credential is involved: control's operator
# builds the image with an ACR Task (MeshWeaver.Plugins Hosting/BuildQueue phase 3) as
#   az acr build -r <registry> -t mw-build-host:<version> -f deploy/build-host/Dockerfile \
#     --build-arg DEPLOY_COMMIT=<sha> --build-arg PLATFORM_COMMIT=<set commit> \
#     https://github.com/Systemorph/MeshWeaver.Deploy.git#<sha>
# and a local build is
#   docker build -f deploy/build-host/Dockerfile --build-arg DEPLOY_COMMIT=$(git rev-parse HEAD) \
#     --build-arg PLATFORM_COMMIT=<platform sha> deploy/build-host
# Handed to the hosts BY DIGEST with the rest of the set (`BuildHostChannel.RunnerDigest`). No moving
# tag names it anywhere.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS sources

# 40-hex commits only: a branch or tag would let one version's image be rebuilt from other bytes.
ARG DEPLOY_COMMIT
ARG PLATFORM_COMMIT
ARG DEPLOY_REPO=https://github.com/Systemorph/MeshWeaver.Deploy.git
ARG PLATFORM_REPO=https://github.com/Systemorph/MeshWeaver.git
RUN set -eu \
 && for c in "$DEPLOY_COMMIT" "$PLATFORM_COMMIT"; do \
      printf '%s' "$c" | grep -Eq '^[0-9a-f]{40}$' || { echo "DEPLOY_COMMIT and PLATFORM_COMMIT must be 40-hex commits (got '$c')"; exit 1; }; \
    done \
 && fetch() { mkdir -p "$1" && git -C "$1" init -q && git -C "$1" remote add origin "$2" \
      && git -C "$1" fetch -q --depth 1 origin "$3" && git -C "$1" checkout -q FETCH_HEAD \
      && [ "$(git -C "$1" rev-parse HEAD)" = "$3" ]; } \
 && fetch /src/deploy "$DEPLOY_REPO" "$DEPLOY_COMMIT" \
 && fetch /src/platform "$PLATFORM_REPO" "$PLATFORM_COMMIT" \
 && mkdir -p /out/scripts \
 && cp /src/deploy/.github/scripts/build-run.py /out/scripts/ \
 && for f in compose-gate-host.sh sandbox-postgres.sh module-build-key.py node-repo-scope.py ci-artifact-store.py; do \
      [ -s "/src/platform/.github/scripts/$f" ] || { echo "the platform at $PLATFORM_COMMIT has no .github/scripts/$f"; exit 1; }; \
      cp "/src/platform/.github/scripts/$f" /out/scripts/; \
    done \
 && cd /src/platform \
 && dotnet publish src/MeshWeaver.Plugin.Build/MeshWeaver.Plugin.Build.csproj -c Release -o /out/plugin-build -nologo \
 && test -f /out/plugin-build/MeshWeaver.Plugin.Build.dll

FROM mcr.microsoft.com/dotnet/sdk:10.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash python3 git bubblewrap ca-certificates postgresql \
 && rm -rf /var/lib/apt/lists/* \
 && bwrap --version && python3 --version && git --version && dotnet --version \
 && find /usr/lib/postgresql -maxdepth 3 -name initdb | grep -q initdb

# The runner (this repository) and the platform's composer and helpers (the set's commit) — one
# implementation each, versioned with the image (the chart ships no copy).
COPY --from=sources /out/scripts/build-run.py /out/scripts/compose-gate-host.sh /out/scripts/sandbox-postgres.sh /out/scripts/module-build-key.py /out/scripts/node-repo-scope.py /out/scripts/ci-artifact-store.py /opt/build-host/
COPY --from=sources /out/plugin-build/ /opt/build-host/plugin-build/
RUN chmod 0555 /opt/build-host/*.py /opt/build-host/*.sh \
 && test -f /opt/build-host/plugin-build/MeshWeaver.Plugin.Build.dll \
 && python3 /opt/build-host/build-run.py --self-test \
 && python3 /opt/build-host/module-build-key.py --self-test \
 && bash /opt/build-host/sandbox-postgres.sh --self-test

# The SDK image's unprivileged `app` user (uid 1654). bwrap creates its namespaces unprivileged.
USER 1654
WORKDIR /work
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
ARG DEPLOY_COMMIT
ARG PLATFORM_COMMIT
LABEL org.opencontainers.image.source="https://github.com/Systemorph/MeshWeaver.Deploy" \
      org.opencontainers.image.revision="${DEPLOY_COMMIT}" \
      io.meshweaver.platform.revision="${PLATFORM_COMMIT}"
ENTRYPOINT ["/bin/bash", "-c", "set -euo pipefail; bash /opt/build-host/compose-gate-host.sh /portal/app /tester /host/app; exec python3 /opt/build-host/build-run.py --host"]
