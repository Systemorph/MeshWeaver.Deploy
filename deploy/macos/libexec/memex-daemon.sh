#!/bin/bash
# memex-daemon.sh — what the OPTIONAL LaunchDaemon com.memex.local.daemon runs at boot, as the service
# user (deploy/macos/README.md §1d). Two things, in order:
#   1. `memex-local up` — idempotent: Colima, the chart, Ollama; on a host that is already up this is a
#      no-op pass over every step.
#   2. hold the port-forward itself. The per-user LaunchAgent (com.memex.local) needs a login session a
#      headless host does not have, so `up`'s launchd step cannot load it here; this process runs the
#      same share/port-forward.sh in its place, and launchd's KeepAlive restarts it if it ever exits.
#
# 🚨 FOLLOW-UP (README §1d): `memex-local up` does not yet know it runs headless — its launchd step
# fails without a GUI session and `up` stops there, AFTER the chart is deployed and BEFORE verify_usable.
# The forward below still comes up, so the host serves; the headless `up` that skips the agent and runs
# the verification is the next change, proven on a real Mac mini.
set -uo pipefail
export PATH="/opt/memex/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
user="$(id -un)"
# launchd does not set HOME for a UserName daemon; Colima, kubectl and ~/.memex-local all need it.
home="$(dscl . -read "/Users/$user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
export HOME="${home:-/Users/$user}"
log() { printf '%s memex-daemon: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "starting as $user (HOME=$HOME)"
# The network is not up at the instant launchd starts us; `up` pulls images and talks to the registry.
tries=0
until route -n get default >/dev/null 2>&1 || [ "$tries" -ge 60 ]; do tries=$((tries + 1)); sleep 5; done
[ "$tries" -lt 60 ] || log "no default route after 300s — continuing; 'up' will say what it cannot reach"

if memex-local up; then log "'memex-local up' completed"
else log "'memex-local up' exited $? — the stack may be partially up (a headless host cannot load the per-user port-forward agent; this daemon holds the forward instead)"; fi

share="${MEMEX_LOCAL_SHARE:-/opt/memex/share}"
log "holding the port-forward ($share/port-forward.sh)"
exec /bin/bash "$share/port-forward.sh"
