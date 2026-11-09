#!/bin/bash
# =============================================================================
# memex-local port-forward — 1:1 automation of LocalColimaMac.md §7
# =============================================================================
# Run by the launchd login agent (com.memex.local.plist) with RunAtLoad +
# KeepAlive so http(s)://memex.localhost (and legacy :8443) survives reboots with NO sudo.
# `memex-local up` installs this script to ~/.memex-local/port-forward.sh and
# wires the plist; `memex-local port-forward` can also run it directly.
#
# It does three things, in order (exactly as the doc's §7 snippet):
#   1. Start Colima if it isn't running (brings k3s + the portal back).
#   2. Wait until the ingress-nginx namespace/controller exists.
#   3. Forward host :8443 to the ingress controller's :443.
# =============================================================================
set -uo pipefail

# launchd gives login agents a minimal PATH; make the brew tools reachable.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/memex/bin:${PATH:-}"
# The SAME resolution the CLI made (share/tools.sh): an existing copy that meets its floor first, the
# .pkg's vendored copy second — so this agent runs the colima/kubectl the CLI chose, not whatever a PATH
# order happens to find first. MEMEX_LOCAL_SHARE is set by the launchd plist (__SHARE__) and by the CLI.
if [ -f "${MEMEX_LOCAL_SHARE:-}/tools.sh" ]; then
  # shellcheck source=tools.sh
  . "${MEMEX_LOCAL_SHARE}/tools.sh"
  tools_activate
  trap tools_deactivate EXIT
fi

HOST_PORT="${MEMEX_PORT:-8443}"

# 1. Start Colima if it isn't running.
colima status >/dev/null 2>&1 || colima start

# 2. Wait until the ingress-nginx namespace/controller is ready.
# 🚨 PIN the kube context: this agent runs with whatever context the USER last
# selected — a kubectl pointed at a remote (or unreachable) cluster left the wait
# loop spinning forever while the local stack sat healthy (2026-08-20). The local
# stack always lives in the colima context.
KUBE_CONTEXT="${MEMEX_KUBE_CONTEXT:-colima}"
until kubectl --context "$KUBE_CONTEXT" get ns ingress-nginx >/dev/null 2>&1; do sleep 2; done

# 2b. A first start that nobody finished: open the setup wizard, token already in the URL. The
#     maintainer, 2026-10-30: "can we do the memex local setup at startup?" — an instance that boots
#     into the wizard and waits for someone to type `memex-local setup` looks broken. Runs in the
#     background (the forward below must start first, or there is nothing to open), only in a GUI
#     session, and once per TOKEN: the token is minted per portal process, so a KeepAlive restart of
#     this agent does not reopen the same page, while a portal restart (a new token) does.
open_setup_if_waiting() {
  [ "${MEMEX_HEADLESS:-}" = "1" ] && return 0
  command -v open >/dev/null 2>&1 || return 0
  local base="https://${MEMEX_HOSTNAME:-memex.localhost}:${HOST_PORT}" location token marker
  for _ in $(seq 1 90); do
    location="$(curl -k -sS -o /dev/null -w '%{redirect_url}' --max-time 5 "$base/" 2>/dev/null || true)"
    [ -n "$location" ] && break
    [ "$(curl -k -sS -o /dev/null -w '%{http_code}' --max-time 5 "$base/" 2>/dev/null)" = "200" ] && return 0
    sleep 2
  done
  case "$location" in */setup) ;; *) return 0 ;; esac
  token="$(kubectl --context "$KUBE_CONTEXT" logs deploy/memex-portal-deployment -n "${MEMEX_NAMESPACE:-memex}" --tail=-1 2>/dev/null \
           | awk '/^ *Token: /{ t=$2 } END { print t }')"
  [ -n "$token" ] || return 0
  # Named by a DIGEST of the token (never the token: a directory entry is listable), shared with the
  # CLI's `up`/`update` hand-off so the two never open the same wizard twice, and written only after
  # the browser actually opened, so a failed launch is retried on the next start.
  marker="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}/setup-opened.$(printf '%s' "$token" | shasum -a 256 | cut -c1-64)"
  [ -f "$marker" ] && return 0
  # 🚨 Never into a certificate error (the second Mac Studio, 2026-11-09): opened only once the certificate the
  # portal SERVES is trusted on this Mac (`security verify-cert <url>` evaluates the presented chain). No marker
  # is written, so the next start opens it once the CA is trusted; `memex-local setup` prints the link meanwhile.
  if ! { command -v security >/dev/null 2>&1 && security verify-cert "$base/" >/dev/null 2>&1; }; then
    echo "$(date -u +%FT%TZ) first-run setup is waiting — NOT opening the browser: the certificate $base serves is not trusted on this Mac yet (the local CA). 'memex-local setup' prints the link."
    return 0
  fi
  echo "$(date -u +%FT%TZ) first-run setup is waiting — opening the wizard"
  if open "$base/setup?token=${token}"; then
    mkdir -p "$(dirname "$marker")" && : > "$marker"
  fi
}
open_setup_if_waiting &

# 3. Forward the portless URLs AND the legacy port: one process carries all three
#    mappings, so http://memex.localhost and https://memex.localhost work directly
#    (macOS allows unprivileged binds below 1024 since Mojave — still NO sudo) and
#    https://memex.localhost:8443 keeps working for anything that learned it.
# --address 0.0.0.0,:: because macOS's unprivileged low-port allowance applies to
# WILDCARD binds only — the default loopback bind gets "permission denied" on 80/443.
# The portal enforces its own sign-in, and the mesh was already reachable on the LAN
# through the 8443 forward's wildcard sibling risks; keep the machine firewalled as usual.
# A published Mac (`memex-local publish on` — its tailnet name and/or a public name, <state dir>/publish.yaml) binds its front
# door to LOOPBACK only, on the unprivileged port alone (the low-port allowance above is for wildcard binds,
# so a loopback 80/443 would be refused): https://memex.localhost:<port> locally, and the private network
# reaches the same port through `tailscale serve` (TCP 443 → 127.0.0.1:<port>), so nothing on the LAN can
# reach the portal directly. Every other install keeps all three mappings on every interface.
# MEMEX_LOCAL_HOME comes from the LaunchAgent (__LOCAL_HOME__), so a non-default state dir is honoured.
if [ -f "${MEMEX_LOCAL_HOME:-$HOME/.memex-local}/publish.yaml" ]; then
  echo "$(date -u +%FT%TZ) published on the private network — the front door binds loopback only, port ${HOST_PORT}"
  # The network's OWN entry point (memex-local publish → share/network-entry.yaml), loopback only too:
  # tailscale serve forwards the network's TCP 443 here, and the proxy behind it marks every request.
  # Held in a restart loop beside the front door; the front door is the process launchd watches.
  entry_port="${MEMEX_ENTRY_PORT:-8446}"   # memex-local ENTRY_PORT: never 8443 (front door) or 8444 (e2e)
  # This script stays the SUPERVISOR (no exec): launchd's KeepAlive and the headless PF_PID both track
  # this process, and stopping it must stop BOTH forwards — the entry loop, its active kubectl, and the
  # front door — so a restart never leaves an old process holding a port or a second retry loop.
  ( trap 'kill "$child" 2>/dev/null; exit 0' TERM INT
    while true; do
      kubectl --context "$KUBE_CONTEXT" port-forward -n "${MEMEX_NAMESPACE:-memex}" svc/memex-network-entry \
        --address 127.0.0.1,::1 "${entry_port}:8443" &
      child=$!
      wait "$child"
      sleep 5
    done ) &
  entry_loop=$!
  kubectl --context "$KUBE_CONTEXT" port-forward -n ingress-nginx svc/ingress-nginx-controller \
      --address 127.0.0.1,::1 "${HOST_PORT}:443" &
  front_door=$!
  stop_both() {
    kill "$front_door" 2>/dev/null
    kill "$entry_loop" 2>/dev/null   # its trap stops its active kubectl
    wait "$front_door" "$entry_loop" 2>/dev/null
    command -v tools_deactivate >/dev/null 2>&1 && tools_deactivate
  }
  trap 'stop_both; exit 0' TERM INT
  trap stop_both EXIT
  # The front door is what this agent is for: when it ends, the entry forward ends with it, and the
  # non-zero exit lets launchd (or the headless daemon) start both again.
  wait "$front_door"
  exit 1
fi
# An INTERNAL-ONLY Mac (`memex-local internal on`, <state dir>/internal.yaml — the fleet default): the front
# door binds LOOPBACK only, on the unprivileged port, and the private network reaches it through
# `tailscale serve --tcp 443` (TLS passed through to the ingress, which serves the machine's own name with its
# trusted certificate). Nothing on the LAN reaches the portal directly; no entry proxy is involved.
if [ -f "${MEMEX_LOCAL_HOME:-$HOME/.memex-local}/internal.yaml" ]; then
  echo "$(date -u +%FT%TZ) internal only — the front door binds loopback only, port ${HOST_PORT}; the private network reaches it through tailscale serve"
  exec kubectl --context "$KUBE_CONTEXT" port-forward -n ingress-nginx svc/ingress-nginx-controller \
      --address 127.0.0.1,::1 "${HOST_PORT}:443"
fi
exec kubectl --context "$KUBE_CONTEXT" port-forward -n ingress-nginx svc/ingress-nginx-controller \
    --address 0.0.0.0,:: 80:80 443:443 "${HOST_PORT}:443"
