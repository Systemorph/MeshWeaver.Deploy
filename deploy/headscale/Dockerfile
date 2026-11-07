# headscale — the fleet's private network control server: the open-source Tailscale control plane,
# so machines (the Macs that run a local mesh, build hosts) join one network with the stock Tailscale
# client pointed at it (`tailscale up --login-server https://<host>`). Design: deploy/headscale/README.md.
#
#   docker build -t headscale:dev deploy/headscale
#
# The registry image is built by CI (.github/workflows/headscale-image.yml), never by hand: the registry
# the cluster already pulls from is the ONE distribution channel (the deploy/whisper rule, #3906).

# 🚨 THE PIN. The upstream release image, by DIGEST (the index over amd64 + arm64). Docker Hub tags can be
# re-pointed; the digest cannot. A bump changes the version AND the digest together, and is a new LINE
# (LINE in headscale-image.yml). Minimum Tailscale client for this release: v1.80.0.
ARG HEADSCALE_IMAGE=headscale/headscale:0.29.7@sha256:11f81665e842809db9fe3006a38c56a66e968baa5abd4bac498e86445572b970

FROM ${HEADSCALE_IMAGE} AS upstream

# ── The keys sidecar (keys/headscale-keys.sh): the SAME headscale binary with a shell, curl and jq ──────
# The upstream image is distroless, so the declared pre-auth keys loop (helm values `preauthKeys`) runs
# from this second image, in the headscale pod, over the shared unix socket. Published beside the server
# image as `headscale-keys:<line>-<sha>` from the same commit (headscale-image.yml).
FROM debian:12-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS keys
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/*
COPY --from=upstream /ko-app/headscale /usr/local/bin/headscale
COPY keys/headscale-keys.sh /usr/local/bin/headscale-keys
RUN chmod 0755 /usr/local/bin/headscale-keys
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/headscale-keys"]

# ── The test stage: the SHIPPED binary and the SHIPPED keys script, exercised before anything is published ──
# test/smoke.sh: `headscale configtest` on the chart's own config shape, then a real `headscale serve`
# answering /health and the Noise key endpoint, then the CLI over the unix socket. test/keys.sh: the keys
# loop against that server and a stand-in API server (test/kube-stub.py). A red test fails the build, so
# nothing untested reaches the registry.
FROM keys AS test
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY test /test
RUN chmod 0755 /test/*.sh && /test/smoke.sh && /test/keys.sh && touch /smoke-passed

# ── The keys image that ships: the keys stage, plus a proof the tests ran ──────────────────────────────────
FROM keys AS keys-release
COPY --from=test /smoke-passed /etc/headscale-smoke-passed

# ── The image that ships: the upstream bytes, unchanged, plus a proof the smoke ran ────────────────────────
FROM upstream
# The COPY makes this stage depend on the test stage, so BuildKit cannot skip the smoke.
COPY --from=test /smoke-passed /etc/headscale-smoke-passed
