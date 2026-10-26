#!/bin/bash
# memex-daemon.sh — what the OPTIONAL LaunchDaemon com.memex.local.daemon runs at boot, as the service
# user (deploy/macos/README.md §1d). It is the headless host's whole lifecycle:
#   1. `memex-local up` with MEMEX_HEADLESS=1 — idempotent; headless, the CLI holds the port-forward as a
#      background process (recorded in ~/.memex-local/port-forward.pid) instead of a LaunchAgent, which no
#      session exists to load, and runs its usability verification against it like any other `up`.
#      A FAILED `up` (an image pull before the network settled, a registry not yet reachable) is RETRIED
#      with a growing pause — never mistaken for "done" — until it succeeds.
#   2. then watch the forward's pid. When it ends this script exits non-zero and launchd's KeepAlive
#      starts it again from step 1, which re-creates whatever is missing.
# PATH: existing installations first, the .pkg's activated fallbacks (/opt/memex/bin) LAST — the same
# order as every agent; the CLI itself runs each tool through its resolver's selection.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/memex/bin"
export MEMEX_HEADLESS=1
user="$(id -un)"
# launchd does not set HOME for a UserName daemon; Colima, kubectl and ~/.memex-local all need it.
home="$(dscl . -read "/Users/$user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
export HOME="${home:-/Users/$user}"
local_home="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}"
log() { printf '%s memex-daemon: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "starting as $user (HOME=$HOME)"
# The network is not up at the instant launchd starts us; `up` pulls images and talks to the registry.
tries=0
until route -n get default >/dev/null 2>&1 || [ "$tries" -ge 60 ]; do tries=$((tries + 1)); sleep 5; done
[ "$tries" -lt 60 ] || log "no default route after 300s — continuing; 'up' will say what it cannot reach"

attempt=0
until memex-local up; do
  attempt=$((attempt + 1))
  pause=$((attempt * 60)); [ "$pause" -gt 600 ] && pause=600
  log "'memex-local up' failed (attempt $attempt) — retrying in ${pause}s; the stack is NOT reported up until it succeeds"
  sleep "$pause"
done
log "'memex-local up' completed after $((attempt + 1)) attempt(s)"

pidfile="$local_home/port-forward.pid"
pid="$(cat "$pidfile" 2>/dev/null || true)"
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  log "no live headless port-forward recorded in $pidfile — exiting so launchd re-runs 'up'"
  exit 1
fi
log "holding: watching the port-forward (pid $pid)"
while kill -0 "$pid" 2>/dev/null; do sleep 30; done
log "the port-forward (pid $pid) ended — exiting so launchd re-runs 'up'"
exit 1
