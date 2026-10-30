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
  marker="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}/setup-opened.${token}"
  [ -f "$marker" ] && return 0
  mkdir -p "$(dirname "$marker")" && : > "$marker"
  echo "$(date -u +%FT%TZ) first-run setup is waiting — opening the wizard"
  open "$base/setup?token=${token}"
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
exec kubectl --context "$KUBE_CONTEXT" port-forward -n ingress-nginx svc/ingress-nginx-controller \
    --address 0.0.0.0,:: 80:80 443:443 "${HOST_PORT}:443"
