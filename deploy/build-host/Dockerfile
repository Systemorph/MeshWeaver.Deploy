# The standing build host's RUNNER image — `mw-build-host` (Doc/Architecture/BuildHosts).
#
# What a stage needs and nothing it does not: bash, python3, git, jq (the workflow steps core's tests
# execute call it, as the GitHub-hosted runner they were written for ships it), bubblewrap (every stage runs in a
# `bwrap` sandbox — the host refuses to start without it), the .NET 10 AND .NET 11 SDKs side by side, one
# root each (/opt/dotnet/10, /opt/dotnet/11: a v3 set builds with 10, a v4 set with 11 — build-run.py
# DOTNET_LINES, policy major-lines-run-separately), PostgreSQL 17 with pgvector and a PREPARED test cluster built at image build
# (host-postgres.sh: UTF8, a pgvector template every suite database is cloned from — a stage starts a
# copy of it inside its own sandbox; sandbox-postgres.sh still initialises a bare one on demand), core's
# SOURCE at the set's commit and this repository's portal chart (the portal-host lane's test-support
# projects and chart guards — handed to stages as MW_PLATFORM_SOURCE / MW_DEPLOY_SOURCE), the portal's headless
# browser and font set at the platform's own Playwright version (core's browser/PDF tests), and the runner with its
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
#
# 🧰 TWO SDKs, TWO ROOTS. The .NET 10 SDK is copied whole from its own SDK image into /opt/dotnet/10; the
# .NET 11 SDK is the base image's /usr/share/dotnet, linked as /opt/dotnet/11. Each root carries ONE SDK
# major, so the muxer in a root can only pick that line's SDK — the host puts its set's root first on
# PATH and as DOTNET_ROOT (build-run.py select_platform_dotnet), never a global.json. Both bases float
# on their band exactly as the single base did; the SDKs they carry are part of every tree's build
# identity (build-run.py build_toolchain), so a moved base rebuilds rather than mixing compilers.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS sdk10

FROM mcr.microsoft.com/dotnet/sdk:11.0 AS sources
COPY --from=sdk10 /usr/share/dotnet/ /opt/dotnet/10/

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
 && cp /src/deploy/.github/scripts/build-run.py /src/deploy/deploy/build-host/host-postgres.sh /src/deploy/deploy/build-host/apt-mirrors.sh /out/scripts/ \
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
 && { pv="$(/usr/share/dotnet/dotnet msbuild src/MeshWeaver.Plugin.Build/MeshWeaver.Plugin.Build.csproj -getProperty:PlatformVersion -nologo 2>&1)" \
      || { echo "the platform at $PLATFORM_COMMIT: evaluating MeshWeaver.Plugin.Build's PlatformVersion failed — $pv"; exit 1; }; } \
 && pv="$(printf '%s\n' "$pv" | tail -n 1 | tr -d '[:space:]')" \
 && major="$(printf '%s' "$pv" | sed -n 's/^\([0-9][0-9]*\)\.[0-9][0-9]*\.[0-9][0-9]*.*$/\1/p')" \
 && case "$major" in \
      3) dn=/opt/dotnet/10/dotnet ;; \
      4) dn=/usr/share/dotnet/dotnet ;; \
      *) echo "the platform at $PLATFORM_COMMIT evaluates PlatformVersion to '$pv' (major '$major') for MeshWeaver.Plugin.Build — this image carries SDK 10 for 3.x and SDK 11 for 4.x only (build-run.py DOTNET_LINES); an empty or unparsable value is refused, never guessed"; exit 1 ;; \
    esac \
 && echo "platform $pv (major $major): MeshWeaver.Plugin.Build is published with $("$dn" --version)" \
 && "$dn" publish src/MeshWeaver.Plugin.Build/MeshWeaver.Plugin.Build.csproj -c Release -o /out/plugin-build -nologo \
 && test -f /out/plugin-build/MeshWeaver.Plugin.Build.dll \
 && test -d /out/platform-src/src && test -d /out/platform-src/tools

# The headless browser's VERSION and the font set come from the PLATFORM at the set's commit, never a
# literal here — and from BOTH places the platform declares them, which must agree:
#   * the portal image (deploy/base-images/portal-ai/Dockerfile `ARG PLAYWRIGHT_VERSION=`, its
#     fonts-local.conf) — the browser production ships;
#   * core's Actions test shards (.github/workflows/dotnet-test.yml `PLAYWRIGHT_VERSION:`, the version every
#     `test` shard provisions and the `browser-fonts` job checks its font set against) and
#     .github/chromium-fonts.list (the font packages that job stages and every shard installs).
# A set whose two versions disagree is refused here, naming both: a host shard would otherwise run a
# different browser from the Actions shard it replaces, or from production. The font list is checked
# against what the image installed (final stage), so a host prints with every font an Actions shard has.
RUN set -eu; \
    f=/src/platform/deploy/base-images/portal-ai/Dockerfile; \
    w=/src/platform/.github/workflows/dotnet-test.yml; \
    l=/src/platform/.github/chromium-fonts.list; \
    v="$(sed -n 's/^ARG PLAYWRIGHT_VERSION=\([0-9][0-9.]*\)$/\1/p' "$f" 2>/dev/null | head -n 1)"; \
    if [ -z "$v" ]; then echo "the platform at $PLATFORM_COMMIT declares no 'ARG PLAYWRIGHT_VERSION=<x.y.z>' in deploy/base-images/portal-ai/Dockerfile — the host cannot follow the portal's browser"; exit 1; fi; \
    a="$(sed -n 's/^  PLAYWRIGHT_VERSION: *\([0-9][0-9.]*\) *$/\1/p' "$w" 2>/dev/null | head -n 1)"; \
    if [ -z "$a" ]; then echo "the platform at $PLATFORM_COMMIT declares no 'PLAYWRIGHT_VERSION: <x.y.z>' in .github/workflows/dotnet-test.yml — the host cannot follow the Actions test shards' browser"; exit 1; fi; \
    if [ "$a" != "$v" ]; then echo "the platform at $PLATFORM_COMMIT disagrees with itself: the portal image ships Playwright $v, the Actions test shards provision $a — a host shard cannot match both"; exit 1; fi; \
    if [ ! -s /src/platform/deploy/base-images/portal-ai/fonts-local.conf ]; then echo "the platform at $PLATFORM_COMMIT has no deploy/base-images/portal-ai/fonts-local.conf"; exit 1; fi; \
    if [ ! -s "$l" ]; then echo "the platform at $PLATFORM_COMMIT has no .github/chromium-fonts.list — the font set the Actions shards install is unknown"; exit 1; fi; \
    mkdir -p /out/browser; \
    printf '%s\n' "$v" > /out/browser/playwright-version; \
    cp /src/platform/deploy/base-images/portal-ai/fonts-local.conf /out/browser/fonts-local.conf; \
    sed -e 's/#.*//' -e 's/[[:space:]]//g' "$l" | grep -v '^$' > /out/browser/chromium-fonts.list; \
    [ -s /out/browser/chromium-fonts.list ] || { echo ".github/chromium-fonts.list at $PLATFORM_COMMIT names no package"; exit 1; }

FROM mcr.microsoft.com/dotnet/sdk:11.0

# The second line's SDK (see the header): .NET 10 in its own root, .NET 11 the base's own root under the
# same parent. `--check-dotnet-roots` below fails the BUILD when either root is missing or carries the
# other major's SDK too.
COPY --from=sdk10 /usr/share/dotnet/ /opt/dotnet/10/
# The base image's /usr/bin/dotnet link is REMOVED: it would expose the 11 SDK on every PATH, so a v3 host
# could reach it by ordering alone — the runner refuses a PATH entry that exposes a dotnet outside its
# line's root (select_platform_dotnet). A dotnet is reached only through the root the host selected.
RUN ln -s /usr/share/dotnet /opt/dotnet/11 \
 && rm /usr/bin/dotnet \
 && if command -v dotnet; then echo "a dotnet is still on PATH outside the line roots"; exit 1; fi \
 && /opt/dotnet/10/dotnet --list-sdks && /opt/dotnet/11/dotnet --list-sdks

# PostgreSQL 17 with pgvector from the PostgreSQL project's own apt repository — the SAME major and
# extension line the CI runners' shared server and the developer container run (pgvector/pgvector:pg17);
# the base image's distribution ships an older major and pgvector 0.6.
ARG PG_MAJOR=17
# Every Ubuntu source is a MIRROR LIST before the first apt-get, never the base image's one host
# (apt-mirrors.sh; MeshWeaver.Deploy#235: five amd64 builds in a row sat out the job's 30-minute cap
# in connect timeouts against archive.ubuntu.com while arm64, on another host, passed). amd64 asks the
# cloud's regional mirror first; every list falls back to Canonical's host and an independent mirror,
# and every index is still verified against the image's Ubuntu keyring. The self-test's negative
# control is the base image's own sources, refused; `apply` ends in `check`, so an image left on one
# host fails its BUILD; `prove-fallback` then updates a copy whose first mirror refuses every
# connection, so a fallback that does not work (apt 2.8's delayed retry hangs it) fails the BUILD too.
COPY --from=sources /out/scripts/apt-mirrors.sh /opt/build-host/apt-mirrors.sh
RUN bash /opt/build-host/apt-mirrors.sh --self-test \
 && bash /opt/build-host/apt-mirrors.sh apply \
 && bash /opt/build-host/apt-mirrors.sh prove-fallback
# jq: core's MeshWeaver.Documentation.Test runs the publication workflow's own `run:` steps
# (PluginPublicationProvenanceTest), and those steps call jq exactly as they do on Actions' ubuntu
# runner, which ships it. Without it a host's shard 7 was RED on 11 tests (`step.sh: jq: command not
# found`) that pass on Actions — on every run, serial or fanned out. The version print below makes an
# image without it fail its BUILD rather than a test.
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash python3 git jq bubblewrap ca-certificates postgresql-common \
 && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}" "postgresql-${PG_MAJOR}-pgvector" \
 && rm -rf /var/lib/apt/lists/* \
 && test -f "/usr/share/postgresql/${PG_MAJOR}/extension/vector.control" \
 && install -d -o 1654 -g 1654 -m 0700 /opt/build-host/pg-snapshot \
 && bwrap --version && python3 --version && git --version && jq --version && /opt/dotnet/10/dotnet --version && /opt/dotnet/11/dotnet --version \
 && find /usr/lib/postgresql -maxdepth 3 -name initdb | grep -q initdb

# The headless BROWSER and its FONT SET, as the portal image installs them (portal-ai's Dockerfile):
# Node from NodeSource, Playwright's `--only-shell` Chromium with `--with-deps` (this Ubuntu release's
# shared libraries AND font packages, per architecture) under /opt/ms-playwright, `/usr/bin/chromium`
# resolved from whichever binary name this architecture gets (`headless_shell` on arm64,
# `chrome-headless-shell` on amd64), CHROME_BIN, and the platform's fonts-local.conf. Installed ONCE
# per image — a stage provisions nothing. The version is the platform's (sources stage), so a new set
# brings its browser with it. The smoke print runs as the unprivileged uid 1654 the stages run as: an
# image whose browser cannot print a PDF fails its BUILD, never a test.
COPY --from=sources /out/browser/ /opt/build-host/browser/
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg; \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    npx --yes "playwright-core@$(cat /opt/build-host/browser/playwright-version)" install --with-deps --only-shell chromium; \
    BROWSER="$(find /opt/ms-playwright -type f -executable \( -name headless_shell -o -name chrome-headless-shell \) -print -quit)"; \
    if [ -z "$BROWSER" ]; then echo "No headless-shell binary under /opt/ms-playwright for $(uname -m):"; find /opt/ms-playwright -maxdepth 3; exit 1; fi; \
    ln -s "$BROWSER" /usr/bin/chromium; \
    /usr/bin/chromium --version; \
    cp /opt/build-host/browser/fonts-local.conf /etc/fonts/local.conf; \
    fc-cache -f > /dev/null; \
    # THE ACTIONS SHARDS' FONT SET: every package core's `browser-fonts` job stages, plus the two the
    # GitHub runner image already carries (the list's own header: fonts-liberation, fonts-noto-color-emoji).
    missing=""; \
    for p in $(cat /opt/build-host/browser/chromium-fonts.list) fonts-liberation fonts-noto-color-emoji; do \
      [ "$(dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null)" = installed ] || missing="$missing $p"; \
    done; \
    if [ -n "$missing" ]; then echo "the image lacks font package(s)$missing that core's Actions test shards print with (.github/chromium-fonts.list + the runner image)"; exit 1; fi; \
    echo "font set: $(wc -l < /opt/build-host/browser/chromium-fonts.list) staged packages + the runner's two, $(fc-list | wc -l) faces"; \
    chmod -R a+rX /opt/ms-playwright; \
    smoke="$(mktemp -d)"; chown 1654:1654 "$smoke"; \
    printf '<html><body><h1>mw-build-host</h1><p><b>bold</b> <i>italic</i> &auml;&ouml;&uuml;</p></body></html>' > "$smoke/p.html"; \
    setpriv --reuid=1654 --regid=1654 --clear-groups env HOME="$smoke" \
      timeout 60 /usr/bin/chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --no-pdf-header-footer \
        --user-data-dir="$smoke/profile" --print-to-pdf="$smoke/p.pdf" "file://$smoke/p.html" > /dev/null 2>&1 || true; \
    if [ ! -s "$smoke/p.pdf" ] || [ "$(head -c 4 "$smoke/p.pdf")" != "%PDF" ]; then echo "the headless shell did not print a PDF as uid 1654 — a host on this image cannot run core's browser tests"; exit 1; fi; \
    rm -rf "$smoke" /root/.npm /root/.cache; \
    npm cache clean --force; \
    apt-get purge -y gnupg; apt-get autoremove -y; apt-get clean; rm -rf /var/lib/apt/lists/*
# MESHWEAVER_CHROMIUM_PATH is what core's `test` shards export for the tests (the renderer's first probe);
# build-run.py hands all three to every stage through its env allow-list.
ENV CHROME_BIN=/usr/bin/chromium
ENV MESHWEAVER_CHROMIUM_PATH=/usr/bin/chromium

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
 && python3 /opt/build-host/build-run.py --check-dotnet-roots \
 && python3 /opt/build-host/module-build-key.py --self-test \
 && python3 /opt/build-host/compile-check.py --help > /dev/null \
 && bash /opt/build-host/sandbox-postgres.sh --self-test

# The SDK image's unprivileged `app` user (uid 1654). bwrap creates its namespaces unprivileged.
USER 1654
# The runner's self-test runs as the uid the host RUNS as, never as root: root reads past file modes, so a case
# whose subject is what an unprivileged host meets (a build that leaves its output unreadable) cannot be built as root.
RUN python3 /opt/build-host/build-run.py --self-test
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
