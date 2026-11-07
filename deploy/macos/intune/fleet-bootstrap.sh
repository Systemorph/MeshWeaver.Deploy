#!/bin/bash
# fleet-bootstrap.sh — brings a Mac Studio of the fleet to "joined", run by the device-management layer
# (Intune, a macOS shell script assigned to the fleet's Mac Studio group) AS ROOT, again and again: every
# run is idempotent, does only what is missing, and says what it did and what it is waiting for.
# Design: MeshWeaver.Plugins Hosting/MacStudioFleet.md §2b (one-command join) and §2b.2 (device management).
#
#   1. Find the console user — the person the Mac's memex runs as. No one logged in yet: exit 0 (the next run
#      continues; the Mac's own memex runs as a person, never as root).
#   2. Homebrew under /opt/homebrew, owned by that user (the documented untar install — no installer prompt),
#      then the tools as that user: colima docker kubectl helm ollama tailscale, and memex-local from the tap.
#   3. tailscaled as a ROOT LaunchDaemon (com.meshweaver.fleet.tailscaled, socket /var/run/tailscaled.socket):
#      the network must be up before anyone logs in, which the Tailscale app does not do. memex-local finds
#      this socket by itself.
#   4. The private network: joined with a single-use pre-auth key when one is provided (FLEET_PREAUTH_KEY_FILE,
#      read and deleted, never printed). Until control mints it at enrolment, a run without one says so.
#   5. memex-local: registry mode, `up`, then `fleet join`. The join's approval happens on control; the run
#      prints the approval link (and writes it to the log), and a later run completes the join once the
#      enrolment code is at its code file. Until control hands the code back itself (§2b.1, target 1), the
#      code file is where it is delivered.
#
# Configuration — the managed preference domain com.memex.local (a configuration profile; memex-local reads
# the same keys): ControlUrl, DeploymentId, RegistryUrl. Environment variables of the same meaning win:
# FLEET_CONTROL_URL, FLEET_MACHINE_ID, FLEET_REGISTRY_URL. Nothing secret is configured here.
#
# Output: one line per step, also appended to /Library/Logs/MeshWeaver/fleet-bootstrap.log. Exit 0 when the
# run did all it could (including "waiting for X"), non-zero only when a step FAILED.
#
# Test hooks: FLEET_BOOTSTRAP_LIB=1 sources the functions without running; FLEET_BOOTSTRAP_DRY_RUN=1 prints
# every command that would change the machine instead of running it.
set -uo pipefail

readonly FLEET_BOOTSTRAP_VERSION="1.0.0"
readonly BREW_PREFIX="${FLEET_BREW_PREFIX:-/opt/homebrew}"
readonly TAILSCALED_LABEL="com.meshweaver.fleet.tailscaled"
readonly TAILSCALED_PLIST="${FLEET_LAUNCHDAEMONS_DIR:-/Library/LaunchDaemons}/${TAILSCALED_LABEL}.plist"
readonly TAILSCALED_SOCKET="${FLEET_TAILSCALED_SOCKET:-/var/run/tailscaled.socket}"
readonly TAILSCALED_STATE_DIR="${FLEET_TAILSCALED_STATE_DIR:-/var/lib/tailscale}"
readonly LOG_DIR="${FLEET_LOG_DIR:-/Library/Logs/MeshWeaver}"
readonly MANAGED_PREFS="${FLEET_MANAGED_PREFS:-/Library/Managed Preferences/com.memex.local}"
readonly BREW_TOOLS=(colima docker kubectl helm ollama tailscale)
readonly MEMEX_FORMULA="systemorph/memex/memex-local"

log() {
  local line; line="$(date -u +%Y-%m-%dT%H:%M:%SZ) fleet-bootstrap ${FLEET_BOOTSTRAP_VERSION}: $*"
  printf '%s\n' "$line"
  if mkdir -p "$LOG_DIR" 2>/dev/null; then printf '%s\n' "$line" >> "$LOG_DIR/fleet-bootstrap.log" 2>/dev/null || true; fi
}

# Runs a command that CHANGES the machine — or prints it under FLEET_BOOTSTRAP_DRY_RUN=1.
run() {
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'; return 0; fi
  "$@"
}

# The person at the console: the owner of /dev/console, unless that is root or the login window.
console_user() {
  local u="${FLEET_CONSOLE_USER:-}"
  [ -n "$u" ] || u="$(stat -f%Su /dev/console 2>/dev/null || true)"
  case "$u" in ''|root|loginwindow|_mbsetupuser) return 1 ;; esac
  printf '%s' "$u"
}

user_home() {
  local h="${FLEET_USER_HOME:-}"
  [ -n "$h" ] || h="$(dscl . -read "/Users/$1" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
  printf '%s' "${h:-/Users/$1}"
}

# One setting: the environment wins, then the managed preference, then the default.
setting() {
  local env_name="$1" pref_key="$2" default="${3:-}" v
  v="${!env_name:-}"
  if [ -z "$v" ] && [ -f "${MANAGED_PREFS}.plist" ]; then
    v="$(defaults read "$MANAGED_PREFS" "$pref_key" 2>/dev/null || true)"
  fi
  printf '%s' "${v:-$default}"
}

# The machine id: configured, else the host name lower-cased to the deployment-id shape.
machine_id() {
  local id; id="$(setting FLEET_MACHINE_ID DeploymentId)"
  if [ -z "$id" ]; then
    id="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
    id="$(printf '%s' "$id" | tr '[:upper:]' '[:lower:]' | LC_ALL=C tr -c 'a-z0-9-' '-' | sed -e 's/--*/-/g' -e 's/^-//' -e 's/-$//' | cut -c1-63)"
  fi
  printf '%s' "$id"
}

# Runs as the console user with that user's HOME and Homebrew on PATH.
as_user() {
  local user="$1" home; shift; home="$(user_home "$user")"
  run sudo -u "$user" -H env HOME="$home" PATH="$BREW_PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$@"
}

# The LaunchDaemon that runs tailscaled as root at boot. Pure: prints the plist.
tailscaled_plist() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${TAILSCALED_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${BREW_PREFIX}/bin/tailscaled</string>
    <string>--state=${TAILSCALED_STATE_DIR}/tailscaled.state</string>
    <string>--socket=${TAILSCALED_SOCKET}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/tailscaled.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/tailscaled.log</string>
</dict>
</plist>
PLIST
}

step_homebrew() {
  local user="$1"
  if [ -x "$BREW_PREFIX/bin/brew" ]; then log "homebrew: present ($BREW_PREFIX)"; return 0; fi
  log "homebrew: installing under $BREW_PREFIX for $user (untar install, no prompt)"
  run mkdir -p "$BREW_PREFIX" && run chown -R "$user:admin" "$BREW_PREFIX" \
    && as_user "$user" /bin/bash -c "curl -fsSL https://github.com/Homebrew/brew/tarball/master | tar xz --strip-components 1 -C '$BREW_PREFIX'" \
    || { log "homebrew: FAILED to install"; return 1; }
}

step_tools() {
  local user="$1" missing=() t
  for t in "${BREW_TOOLS[@]}"; do [ -x "$BREW_PREFIX/bin/$t" ] || missing+=("$t"); done
  [ -x "$BREW_PREFIX/bin/memex-local" ] || missing+=("$MEMEX_FORMULA")
  if [ "${#missing[@]}" -eq 0 ]; then log "tools: all present"; return 0; fi
  log "tools: installing ${missing[*]}"
  as_user "$user" "$BREW_PREFIX/bin/brew" install "${missing[@]}" || { log "tools: FAILED to install ${missing[*]}"; return 1; }
}

step_tailscaled() {
  local want; want="$(tailscaled_plist)"
  if [ -f "$TAILSCALED_PLIST" ] && [ "$(cat "$TAILSCALED_PLIST")" = "$want" ] && [ -S "$TAILSCALED_SOCKET" ]; then
    log "tailscaled: running as a root daemon ($TAILSCALED_LABEL, $TAILSCALED_SOCKET)"; return 0
  fi
  log "tailscaled: installing the root LaunchDaemon $TAILSCALED_LABEL (socket $TAILSCALED_SOCKET)"
  run mkdir -p "$TAILSCALED_STATE_DIR" "$(dirname "$TAILSCALED_PLIST")" || return 1
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: write %s\n' "$TAILSCALED_PLIST"
  else printf '%s\n' "$want" > "$TAILSCALED_PLIST" && chmod 644 "$TAILSCALED_PLIST" && chown root:wheel "$TAILSCALED_PLIST" || return 1; fi
  run launchctl bootout system "$TAILSCALED_PLIST" >/dev/null 2>&1 || true
  run launchctl bootstrap system "$TAILSCALED_PLIST" || { log "tailscaled: FAILED to load the daemon"; return 1; }
}

step_network() {
  local machine="$1" ts=("$BREW_PREFIX/bin/tailscale" --socket="$TAILSCALED_SOCKET")
  if "${ts[@]}" status >/dev/null 2>&1; then log "network: joined"; return 0; fi
  local key_file="${FLEET_PREAUTH_KEY_FILE:-}" login; login="$(setting FLEET_LOGIN_SERVER LoginServer)"
  if [ -z "$key_file" ] || [ ! -f "$key_file" ] || [ -z "$login" ]; then
    log "network: WAITING for a single-use pre-auth key and the login server (control mints the key at enrolment — Hosting/MacStudioFleet §2b.1 target 2)"
    return 0
  fi
  log "network: joining $login as $machine with the provided pre-auth key (never printed)"
  local key; key="$(cat "$key_file")"; rm -f "$key_file"
  run "${ts[@]}" up --login-server "$login" --hostname "$machine" --auth-key "$key" --reset || { log "network: FAILED to join"; return 1; }
}

step_memex() {
  local user="$1" machine="$2" control registry home code_file out rc
  control="$(setting FLEET_CONTROL_URL ControlUrl)"; registry="$(setting FLEET_REGISTRY_URL RegistryUrl)"
  home="$(user_home "$user")"
  [ -n "$control" ] || { log "memex: WAITING for ControlUrl (the com.memex.local configuration profile)"; return 0; }
  if [ -n "$registry" ]; then
    as_user "$user" "$BREW_PREFIX/bin/memex-local" registry "$registry" >/dev/null 2>&1 || log "memex: registry mode not set (re-tried next run)"
  fi
  if ! as_user "$user" "$BREW_PREFIX/bin/colima" status >/dev/null 2>&1; then
    log "memex: starting the local stack (memex-local up)"
    as_user "$user" "$BREW_PREFIX/bin/memex-local" up >/dev/null 2>&1 || log "memex: 'memex-local up' did not finish — re-tried next run"
  fi
  if [ -f "$home/.memex-local/fleet.yaml" ]; then log "memex: already joined as $machine"; return 0; fi
  code_file="$home/.memex-local/enrol/$machine.code"
  log "memex: fleet join as $machine (control $control); the code is read from $code_file once present"
  out="$(as_user "$user" "$BREW_PREFIX/bin/memex-local" fleet join --control "$control" --id "$machine" --no-open --code-file "$code_file" 2>&1)"; rc=$?
  printf '%s\n' "$out" | grep -E 'https://|fingerprint|joined as|WAITING|waiting' | while IFS= read -r l; do log "memex: $l"; done
  case "$rc" in
    0) log "memex: joined as $machine" ;;
    3) log "memex: WAITING for the approval on control and the enrolment code at $code_file" ;;
    *) log "memex: fleet join FAILED (exit $rc)"; return 1 ;;
  esac
}

main() {
  [ "$(id -u)" = "0" ] || [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] || { log "must run as root (the device-management layer runs it as system)"; exit 2; }
  local user machine failed=0
  if ! user="$(console_user)"; then log "no one is logged in at the console yet — nothing to do until a person logs in (next run continues)"; exit 0; fi
  machine="$(machine_id)"
  log "run for console user $user, machine $machine"
  step_homebrew "$user" || failed=1
  [ "$failed" = 0 ] && { step_tools "$user" || failed=1; }
  [ "$failed" = 0 ] && { step_tailscaled || failed=1; }
  [ "$failed" = 0 ] && { step_network "$machine" || failed=1; }
  [ "$failed" = 0 ] && { step_memex "$user" "$machine" || failed=1; }
  [ "$failed" = 0 ] && log "done (everything this run could do)" || log "a step FAILED — the next run retries from it"
  exit "$failed"
}

[ "${FLEET_BOOTSTRAP_LIB:-}" = "1" ] || main "$@"
