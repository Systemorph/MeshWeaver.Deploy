# tailnet-bridge — publishes one machine of the private network at a public name: the stock Tailscale
# client (userspace, no capabilities) plus Caddy as the reverse proxy, in one image. Design: README.md.
#
#   docker build -t tailnet-bridge:dev deploy/tailnet-bridge
#
# The registry image is built by CI (.github/workflows/tailnet-bridge-image.yml), never by hand.

# 🚨 THE PINS. Both upstream release images by DIGEST (the index over amd64 + arm64). A bump changes the
# version AND the digest together, and is a new LINE (LINE in tailnet-bridge-image.yml).
ARG TAILSCALE_IMAGE=tailscale/tailscale:v1.104.4@sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c
ARG CADDY_IMAGE=caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d

FROM ${CADDY_IMAGE} AS caddy

FROM ${TAILSCALE_IMAGE} AS bridge
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY entrypoint.sh /usr/local/bin/tailnet-bridge
RUN chmod 0755 /usr/local/bin/tailnet-bridge
ENV XDG_CONFIG_HOME=/tmp/caddy XDG_DATA_HOME=/tmp/caddy
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/tailnet-bridge"]

# ── The test stage: the SHIPPED entrypoint and binaries, before anything is published ──────────────────────
FROM bridge AS test
USER root
RUN apk add --no-cache curl
COPY test /test
RUN chmod 0755 /test/*.sh && su -s /bin/sh -c /test/smoke.sh nobody && touch /smoke-passed

# ── The image that ships: the bridge stage, plus a proof the smoke ran ─────────────────────────────────────
FROM bridge
COPY --from=test /smoke-passed /etc/tailnet-bridge-smoke-passed
