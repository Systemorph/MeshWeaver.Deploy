#!/bin/bash
# memex-daemon.sh — what the OPTIONAL LaunchDaemon com.memex.local.daemon runs at boot, as the service
# user (deploy/macos/README.md §1d). It is the headless host's whole lifecycle:
#   1. `/opt/memex/bin/memex-local up` with MEMEX_HEADLESS=1 — the CLI THIS package shipped, by its path:
#      with both channels installed a bare `memex-local` would find Homebrew's copy first on the PATH below,
#      and an older tap version has no headless mode. Idempotent; headless, the CLI holds the port-forward
#      as a background process (recorded in ~/.memex-local/port-forward.pid) instead of a LaunchAgent,
#      which no session exists to load, and runs its usability verification against it like any other `up`.
#      A FAILED `up` (an image pull before the network settled, a registry not yet reachable) is RETRIED
#      with a growing pause — never mistaken for "done" — until it succeeds.
#   2. then watch the forward's pid. When it ends this script exits non-zero and launchd's KeepAlive
#      (SuccessfulExit=false) starts it again from step 1, which re-creates whatever is missing.
# STOPPING: `memex-local down` writes ~/.memex-local/daemon.stopped BEFORE it kills the forward. While that
# file exists this script exits 0 — at start, between `up` attempts, after an `up`, and when the forward
# ends — and exit 0 is the one exit KeepAlive does not restart, so a deliberate teardown is never mistaken
# for a transport failure and re-provisioned under `down`'s feet. An explicit `memex-local up` (not this
# daemon's, which runs with MEMEX_DAEMON=1) removes the file; then `sudo launchctl kickstart
# system/com.memex.local.daemon` hands the stack back to the daemon.
# PATH: existing installations first, the .pkg's activated fallbacks (/opt/memex/bin) LAST — the same
# order as every agent; the CLI itself runs each tool through its resolver's selection.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/memex/bin"
export MEMEX_HEADLESS=1
export MEMEX_DAEMON=1
# The shipped CLI, explicitly (MEMEX_DAEMON_CLI exists for the test suites, which run this against a stub).
cli="${MEMEX_DAEMON_CLI:-/opt/memex/bin/memex-local}"
retry_unit="${MEMEX_DAEMON_RETRY_UNIT:-60}"   # seconds per failed attempt (capped at 10 units)
watch_every="${MEMEX_DAEMON_WATCH_INTERVAL:-30}"
user="$(id -un)"
# launchd does not set HOME for a UserName daemon; Colima, kubectl and ~/.memex-local all need it.
home="$(dscl . -read "/Users/$user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
export HOME="${home:-/Users/$user}"
local_home="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}"
stop_marker="$local_home/daemon.stopped"
log() { printf '%s memex-daemon: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
# Exit 0 (no KeepAlive restart) when `memex-local down` asked the stack to stay down.
honour_stop() {
  [ -e "$stop_marker" ] || return 0
  log "stopped by 'memex-local down' ($stop_marker, $1) — exiting 0, launchd will not restart; 'memex-local up' clears it"
  exit 0
}

log "starting as $user (HOME=$HOME, CLI $cli)"
honour_stop "at start"
# The network is not up at the instant launchd starts us; `up` pulls images and talks to the registry.
tries=0
until route -n get default >/dev/null 2>&1 || [ "$tries" -ge 60 ]; do tries=$((tries + 1)); sleep 5; done
[ "$tries" -lt 60 ] || log "no default route after 300s — continuing; 'up' will say what it cannot reach"

attempt=0
until "$cli" up; do
  attempt=$((attempt + 1))
  honour_stop "after failed attempt $attempt"
  pause=$((attempt * retry_unit)); [ "$pause" -gt $((10 * retry_unit)) ] && pause=$((10 * retry_unit))
  log "'$cli up' failed (attempt $attempt) — retrying in ${pause}s; the stack is NOT reported up until it succeeds"
  sleep "$pause"
  honour_stop "before attempt $((attempt + 1))"
done
log "'$cli up' completed after $((attempt + 1)) attempt(s)"
honour_stop "after 'up'"

pidfile="$local_home/port-forward.pid"
pid="$(cat "$pidfile" 2>/dev/null || true)"
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  log "no live headless port-forward recorded in $pidfile — exiting so launchd re-runs 'up'"
  exit 1
fi
log "holding: watching the port-forward (pid $pid)"
while kill -0 "$pid" 2>/dev/null; do sleep "$watch_every"; done
honour_stop "the port-forward (pid $pid) ended"
log "the port-forward (pid $pid) ended — exiting so launchd re-runs 'up'"
exit 1
