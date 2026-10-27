# The standing build host's RUNNER image — `mw-build-host` (Doc/Architecture/BuildHosts).
#
# What a stage needs and nothing it does not: bash, python3, git, bubblewrap (every stage runs in a
# `bwrap` sandbox — the host refuses to start without it), the .NET 10 SDK (the src/ half still
# builds with it), PostgreSQL 17 with pgvector and a PREPARED test cluster built at image build
# (host-postgres.sh: UTF8, a pgvector template every suite database is cloned from — a stage starts a
# copy of it inside its own sandbox; sandbox-postgres.sh still initialises a bare one on demand), core's
# SOURCE at the set's commit and this repository's portal chart (the portal-host lane's test-support
# projects and chart guards — handed to stages as MW_PLATFORM_SOURCE / MW_DEPLOY_SOURCE), and the runner with its
# helpers — module-build-key.py (the host recomputes every module cache key before promoting it),
# ci-artifact-store.py (H9: the host hands every module bundle to the shared CI artifact store),
# compile-check.py (the canonical NodeType checker a repository's stages run, handed to them as
# MW_PLATFORM_SCRIPTS — a sandbox has no credential to fetch it)
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
 && cp /src/deploy/.github/scripts/build-run.py /src/deploy/deploy/build-host/host-postgres.sh /out/scripts/ \
 && for f in compose-gate-host.sh sandbox-postgres.sh module-build-key.py node-repo-scope.py ci-artifact-store.py compile-check.py; do \
      [ -s "/src/platform/.github/scripts/$f" ] || { echo "the platform at $PLATFORM_COMMIT has no .github/scripts/$f"; exit 1; }; \
      cp "/src/platform/.github/scripts/$f" /out/scripts/; \
    done \
 && mkdir -p /out/platform-src /out/deploy-src \
 && git -C /src/platform archive --format=tar HEAD | tar -x -C /out/platform-src \
 && printf '%s\n' "$PLATFORM_COMMIT" > /out/platform-src/.source-commit \
 && test -f /out/platform-src/Directory.Build.props && test -f /out/platform-src/.github/scripts/record-host-crash.py \
 && git -C /src/deploy archive --format=tar HEAD deploy/helm | tar -x -C /out/deploy-src \
 && printf '%s\n' "$DEPLOY_COMMIT" > /out/deploy-src/.source-commit \
 && test -f /out/deploy-src/deploy/helm/Chart.yaml \
 && cd /src/platform \
 && dotnet publish src/MeshWeaver.Plugin.Build/MeshWeaver.Plugin.Build.csproj -c Release -o /out/plugin-build -nologo \
 && test -f /out/plugin-build/MeshWeaver.Plugin.Build.dll \
 && test -d /out/platform-src/src && test -d /out/platform-src/tools

FROM mcr.microsoft.com/dotnet/sdk:10.0

# PostgreSQL 17 with pgvector from the PostgreSQL project's own apt repository — the SAME major and
# extension line the CI runners' shared server and the developer container run (pgvector/pgvector:pg17);
# the base image's distribution ships an older major and pgvector 0.6.
ARG PG_MAJOR=17
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash python3 git bubblewrap ca-certificates postgresql-common \
 && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}" "postgresql-${PG_MAJOR}-pgvector" \
 && rm -rf /var/lib/apt/lists/* \
 && test -f "/usr/share/postgresql/${PG_MAJOR}/extension/vector.control" \
 && install -d -o 1654 -g 1654 -m 0700 /opt/build-host/pg-snapshot \
 && bwrap --version && python3 --version && git --version && dotnet --version \
 && find /usr/lib/postgresql -maxdepth 3 -name initdb | grep -q initdb

# The runner (this repository) and the platform's composer and helpers (the set's commit) — one
# implementation each, versioned with the image (the chart ships no copy).
COPY --from=sources /out/scripts/build-run.py /out/scripts/compose-gate-host.sh /out/scripts/sandbox-postgres.sh /out/scripts/module-build-key.py /out/scripts/node-repo-scope.py /out/scripts/ci-artifact-store.py /out/scripts/compile-check.py /opt/build-host/
COPY --from=sources /out/scripts/host-postgres.sh /opt/build-host/
COPY --from=sources /out/plugin-build/ /opt/build-host/plugin-build/
# THE PLATFORM'S SOURCE at the set's commit (its tracked tree, stamped with .source-commit): the MeshWeaverRoot a
# repository's PREPARED inputs — its module-libraries shelf, its Razor build extension — are published against, once
# per (platform, source key), by build-run.py's prepare_inputs, never per run (MW_PLATFORM_SOURCE); the portal-host
# lane reads its test-support projects from the same tree.
COPY --from=sources /out/platform-src/ /opt/build-host/platform-src/
COPY --from=sources /out/deploy-src/ /opt/build-host/deploy-src/
RUN chmod 0555 /opt/build-host/*.py /opt/build-host/*.sh \
 && test -f /opt/build-host/plugin-build/MeshWeaver.Plugin.Build.dll \
 && python3 /opt/build-host/build-run.py --self-test \
 && python3 /opt/build-host/module-build-key.py --self-test \
 && python3 /opt/build-host/compile-check.py --help > /dev/null \
 && bash /opt/build-host/sandbox-postgres.sh --self-test

# The SDK image's unprivileged `app` user (uid 1654). bwrap creates its namespaces unprivileged.
USER 1654
# The PREPARED test cluster (UTF8, a pgvector template) is built HERE, once per image, as the
# unprivileged user (initdb refuses root); a stage starts a copy of it inside its sandbox. The
# self-test is a REAL server: two stage servers from copies, the suites' superuser operations,
# isolation, and TCP / a missing / a stale snapshot refused.
RUN bash /opt/build-host/host-postgres.sh --self-test \
 && bash /opt/build-host/host-postgres.sh prepare /opt/build-host/pg-snapshot
WORKDIR /work
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    MW_PLATFORM_SOURCE=/opt/build-host/platform-src
ARG DEPLOY_COMMIT
ARG PLATFORM_COMMIT
LABEL org.opencontainers.image.source="https://github.com/Systemorph/MeshWeaver.Deploy" \
      org.opencontainers.image.revision="${DEPLOY_COMMIT}" \
      io.meshweaver.platform.revision="${PLATFORM_COMMIT}"
ENTRYPOINT ["/bin/bash", "-c", "set -euo pipefail; bash /opt/build-host/compose-gate-host.sh /portal/app /tester /host/app; exec python3 /opt/build-host/build-run.py --host"]
