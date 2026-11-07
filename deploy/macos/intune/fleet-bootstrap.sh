#!/bin/bash
# fleet-bootstrap.sh — brings a Mac Studio of the fleet to "joined", run by the device-management layer
# (Intune, a macOS shell script assigned to the fleet's Mac Studio group) AS ROOT, again and again: every
# run is idempotent, does only what is missing, and says what it did and what it is waiting for.
# Design: MeshWeaver.Plugins Hosting/MacStudioFleet.md §2b (one-command join) and §2b.2 (device management).
#
#   1. Find the console user — the person the Mac's memex runs as. No one logged in yet: exit 0 (the next run
#      continues; the Mac's own memex runs as a person, never as root).
#   2. Homebrew under /opt/homebrew, owned by that user (the documented untar install — no installer prompt),
#      then the tools as that user: colima docker kubectl helm ollama, and memex-local from the trusted tap.
#   3. tailscale/tailscaled fetched BY ROOT from Homebrew's verified bottle into the root-owned
#      /Library/MeshWeaver/tailscale (never run as root from the user-owned Homebrew tree), and tailscaled as a
#      ROOT LaunchDaemon (com.meshweaver.fleet.tailscaled, socket /var/run/tailscaled.socket):
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

readonly FLEET_BOOTSTRAP_VERSION="1.3.1"
readonly BREW_PREFIX="${FLEET_BREW_PREFIX:-/opt/homebrew}"
readonly TAILSCALED_LABEL="com.meshweaver.fleet.tailscaled"
readonly TAILSCALED_PLIST="${FLEET_LAUNCHDAEMONS_DIR:-/Library/LaunchDaemons}/${TAILSCALED_LABEL}.plist"
readonly TAILSCALED_SOCKET="${FLEET_TAILSCALED_SOCKET:-/var/run/tailscaled.socket}"
readonly TAILSCALED_STATE_DIR="${FLEET_TAILSCALED_STATE_DIR:-/var/lib/tailscale}"
readonly LOG_DIR="${FLEET_LOG_DIR:-/Library/Logs/MeshWeaver}"
readonly MANAGED_PREFS="${FLEET_MANAGED_PREFS:-/Library/Managed Preferences/com.memex.local}"
readonly BREW_TOOLS=(colima docker kubectl helm ollama)
# 🚨 What runs AS ROOT never comes from the Homebrew tree, which the console user owns (they could replace the
# binary and run code as root at the next boot). tailscale/tailscaled are fetched BY ROOT from Homebrew's bottle
# (the formula API names the URL and its sha256, verified here), into a root-owned directory under /Library.
readonly TS_DIR="${FLEET_TS_DIR:-/Library/MeshWeaver/tailscale}"
readonly TS_FORMULA_API="${FLEET_TS_FORMULA_API:-https://formulae.brew.sh/api/formula/tailscale.json}"
readonly MEMEX_FORMULA="systemorph/memex/memex-local"

log() {
  local line; line="$(date -u +%Y-%m-%dT%H:%M:%SZ) fleet-bootstrap ${FLEET_BOOTSTRAP_VERSION}: $*"
  # 🚨 a token-bearing URL (the setup wizard's ?token=) never reaches the output or the log
  case "$line" in *token=*) line="$(printf '%s' "$line" | sed -E 's/token=[^[:space:]&"]+/token=<withheld>/g')" ;; esac
  printf '%s\n' "$line"
  if mkdir -p "$LOG_DIR" 2>/dev/null; then
    ( umask 077; printf '%s\n' "$line" >> "$LOG_DIR/fleet-bootstrap.log" ) 2>/dev/null || true
  fi
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

# A READ-ONLY probe as the console user: it RUNS even in a dry run (it changes nothing), so a dry run plans
# from what is really there instead of from a simulated success. A failed probe means "not yet".
probe_as_user() {
  local user="$1" home; shift; home="$(user_home "$user")"
  sudo -n -u "$user" -H env HOME="$home" PATH="$BREW_PREFIX/bin:$TS_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$@" >/dev/null 2>&1
}

# Runs as the console user with that user's HOME and Homebrew on PATH.
as_user() {
  local user="$1" home; shift; home="$(user_home "$user")"
  # the root-owned tailscale client is on the user's PATH too (execute-only for them): memex-local's network
  # check and `publish on` need it, and it talks to the root daemon's socket
  run sudo -u "$user" -H env HOME="$home" PATH="$BREW_PREFIX/bin:$TS_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$@"
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
    <string>${TS_DIR}/bin/tailscaled</string>
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
    && as_user "$user" /bin/bash -c "curl -fsSL https://github.com/Homebrew/brew/tarball/main | tar xz --strip-components 1 -C '$BREW_PREFIX'" \
    || { log "homebrew: FAILED to install"; return 1; }
}

step_tools() {
  local user="$1" missing=() t
  for t in "${BREW_TOOLS[@]}"; do [ -x "$BREW_PREFIX/bin/$t" ] || missing+=("$t"); done
  [ -x "$BREW_PREFIX/bin/memex-local" ] || missing+=("$MEMEX_FORMULA")
  if [ "${#missing[@]}" -eq 0 ]; then log "tools: all present"; return 0; fi
  log "tools: installing ${missing[*]}"
  # a third-party tap's formulae are refused until the tap is tapped AND trusted (deploy/homebrew/README.md)
  as_user "$user" "$BREW_PREFIX/bin/brew" tap systemorph/memex >/dev/null 2>&1 \
    && as_user "$user" "$BREW_PREFIX/bin/brew" trust systemorph/memex >/dev/null 2>&1 \
    || { log "tools: FAILED to tap and trust systemorph/memex"; return 1; }
  as_user "$user" "$BREW_PREFIX/bin/brew" install "${missing[@]}" || { log "tools: FAILED to install ${missing[*]}"; return 1; }
}

# The bottle file key for this Mac in the formula JSON: the macOS release's codename when the formula has it,
# else the newest arm64 macOS bottle. Pure over its arguments (JSON text, macOS major version).
bottle_key() {
  osascript -l JavaScript -e 'function run(a){var f=JSON.parse(a[0]).bottle.stable.files;var names={"15":"sequoia","26":"tahoe","27":"golden_gate"};var want="arm64_"+(names[a[1]]||"");if(f[want])return want;var k=Object.keys(f).filter(function(x){return x.indexOf("arm64_")===0&&x!=="arm64_linux";});return k.length?k[0]:"";}' "$1" "$2" 2>/dev/null
}

# One field of the formula JSON: version | url <key> | sha256 <key>. Pure.
formula_field() {
  osascript -l JavaScript -e 'function run(a){var d=JSON.parse(a[0]);if(a[1]==="version")return d.versions.stable;var f=d.bottle.stable.files[a[2]];return f?f[a[1]]:"";}' "$1" "$2" "${3:-}" 2>/dev/null
}

# tailscale + tailscaled, fetched by root from the verified bottle into the root-owned $TS_DIR.
step_tailscale_binaries() {
  local json version key url sha tmp have
  json="$(curl -fsSL "$TS_FORMULA_API")" || { log "tailscale: FAILED to read the formula API"; return 1; }
  version="$(formula_field "$json" version)"; key="$(bottle_key "$json" "$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)")"
  url="$(formula_field "$json" url "$key")"; sha="$(formula_field "$json" sha256 "$key")"
  [ -n "$version" ] && [ -n "$url" ] && [ -n "$sha" ] || { log "tailscale: FAILED — no arm64 macOS bottle in the formula API"; return 1; }
  have="$(cat "$TS_DIR/VERSION" 2>/dev/null || true)"
  if [ "$have" = "$version" ] && [ -x "$TS_DIR/bin/tailscaled" ] && [ "$(stat -f%Su "$TS_DIR/bin/tailscaled" 2>/dev/null)" = "root" ]; then
    log "tailscale: $version present, root-owned ($TS_DIR)"; return 0
  fi
  log "tailscale: installing $version from the verified bottle ($key) into $TS_DIR"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: fetch %s (sha256 %s) → %s\n' "$url" "$sha" "$TS_DIR"; return 0; fi
  tmp="$(mktemp -d)"
  curl -fsSL -H "Authorization: Bearer QQ==" -o "$tmp/bottle.tar.gz" "$url" \
    && [ "$(shasum -a 256 "$tmp/bottle.tar.gz" | cut -d' ' -f1)" = "$sha" ] \
    && tar xzf "$tmp/bottle.tar.gz" -C "$tmp" \
    && install -d -o root -g wheel -m 0755 "$(dirname "$TS_DIR")" "$TS_DIR" "$TS_DIR/bin" \
    && install -o root -g wheel -m 0755 "$tmp"/tailscale/*/bin/tailscale "$tmp"/tailscale/*/bin/tailscaled "$TS_DIR/bin/" \
    && printf '%s\n' "$version" > "$TS_DIR/VERSION" \
    || { rm -rf "$tmp"; log "tailscale: FAILED (download, checksum or install)"; return 1; }
  rm -rf "$tmp"
  TS_CHANGED=1
  # the user-facing CLI on every PATH, as a root-owned link to the root-owned binary — only in a root-owned
  # /usr/local/bin (created when absent; an existing user-owned one, e.g. a Rosetta Homebrew's, is left alone)
  [ -d /usr/local/bin ] || install -d -o root -g wheel -m 0755 /usr/local/bin 2>/dev/null || true
  if [ "$(stat -f%Su /usr/local/bin 2>/dev/null)" = "root" ]; then ln -sf "$TS_DIR/bin/tailscale" /usr/local/bin/tailscale || true; fi
  return 0
}

step_tailscaled() {
  local want; want="$(tailscaled_plist)"
  # an upgraded binary (this run) must be RELOADED: the running daemon would otherwise keep the old one
  if [ "${TS_CHANGED:-0}" != "1" ] && [ -f "$TAILSCALED_PLIST" ] && [ "$(cat "$TAILSCALED_PLIST")" = "$want" ] && [ -S "$TAILSCALED_SOCKET" ]; then
    log "tailscaled: running as a root daemon ($TAILSCALED_LABEL, $TAILSCALED_SOCKET)"; return 0
  fi
  log "tailscaled: (re)loading the root LaunchDaemon $TAILSCALED_LABEL (socket $TAILSCALED_SOCKET)"
  run mkdir -p "$TAILSCALED_STATE_DIR" "$(dirname "$TAILSCALED_PLIST")" || return 1
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: write %s\n' "$TAILSCALED_PLIST"
  else printf '%s\n' "$want" > "$TAILSCALED_PLIST" && chmod 644 "$TAILSCALED_PLIST" && chown root:wheel "$TAILSCALED_PLIST" || return 1; fi
  run launchctl bootout system "$TAILSCALED_PLIST" >/dev/null 2>&1 || true
  run launchctl bootstrap system "$TAILSCALED_PLIST" || { log "tailscaled: FAILED to load the daemon"; return 1; }
}

step_network() {
  local machine="$1" ts=("$TS_DIR/bin/tailscale" --socket="$TAILSCALED_SOCKET")
  if "${ts[@]}" status >/dev/null 2>&1; then log "network: joined"; return 0; fi
  local key_file="${FLEET_PREAUTH_KEY_FILE:-}" login; login="$(setting FLEET_LOGIN_SERVER LoginServer)"
  if [ -z "$key_file" ] || [ ! -f "$key_file" ] || [ -z "$login" ]; then
    log "network: WAITING for a single-use pre-auth key and the login server (control mints the key at enrolment — Hosting/MacStudioFleet §2b.1 target 2)"
    return 0
  fi
  log "network: joining $login as $machine with the provided pre-auth key (read from its file, never printed)"
  # by FILE reference: the key never appears in an argument, a process listing or a dry-run line
  run "${ts[@]}" up --login-server "$login" --hostname "$machine" --auth-key="file:$key_file" --reset \
    || { log "network: FAILED to join — the key file is kept for the next run"; return 1; }
  run rm -f "$key_file"
}

# The INSTALLED CLI behind Homebrew's bin/ wrapper (a write_env_script that execs libexec/memex-local — sourcing
# the wrapper would exec instead of defining the functions). Sourced only from here.
memex_libexec() { printf '%s' "${FLEET_MEMEX_LIBEXEC:-$BREW_PREFIX/opt/memex-local/libexec}"; }

# Whether a CA certificate is trusted for SSL by the system (the System keychain's trust settings). Pure read.
ca_trusted() {
  security verify-cert -c "$1" -p ssl >/dev/null 2>&1
}

step_memex() {
  local user="$1" machine="$2" control registry home code_file out rc mlocal
  control="$(setting FLEET_CONTROL_URL ControlUrl)"; registry="$(setting FLEET_REGISTRY_URL RegistryUrl)"
  home="$(user_home "$user")"; mlocal="$BREW_PREFIX/bin/memex-local"
  [ -n "$control" ] || { log "memex: WAITING for ControlUrl (the com.memex.local configuration profile)"; return 0; }
  # registry mode is initialised ONCE: re-running `registry <url>` would rewrite registry.yaml without the
  # bootstrap key or pinned id an existing registration was made with
  if [ -n "$registry" ] && [ ! -f "$home/.memex-local/registry.yaml" ]; then
    log "memex: registry mode ($registry)"
    as_user "$user" "$mlocal" registry "$registry" >/dev/null 2>&1 || { log "memex: FAILED to set registry mode"; return 1; }
  fi
  # The local CA: `memex-local up` runs `mkcert -install`, whose System-keychain step needs an interactive
  # authorisation an unattended run cannot give. So the CA is CREATED here without touching the keychain
  # (TRUST_STORES=nss), and `up` waits until it is trusted — by the device-management layer (a certificate
  # payload of the com.memex.local profile carrying this public CA certificate) or by a person running
  # `mkcert -install` once. Nothing here runs the user-owned mkcert as root.
  local caroot; caroot="$(as_user "$user" env TRUST_STORES=nss "$BREW_PREFIX/bin/mkcert" -CAROOT 2>/dev/null || true)"
  if [ -n "$caroot" ] && [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" != "1" ] && [ ! -f "$caroot/rootCA.pem" ]; then
    as_user "$user" env TRUST_STORES=nss "$BREW_PREFIX/bin/mkcert" -install >/dev/null 2>&1 || true
  fi
  if [ -n "$caroot" ] && [ -f "$caroot/rootCA.pem" ] && ! ca_trusted "$caroot/rootCA.pem"; then
    log "memex: WAITING for the local CA to be trusted ($caroot/rootCA.pem, public) — deploy it as a certificate payload, or run 'mkcert -install' once as $user"
    return 0
  fi
  # the stack is provisioned when it VERIFIES usable, not when the VM merely runs — `up` is retried until it does
  if ! probe_as_user "$user" "$mlocal" verify; then
    log "memex: provisioning the local stack (memex-local up)"
    as_user "$user" "$mlocal" up >/dev/null 2>&1 || { log "memex: 'memex-local up' FAILED — retried next run"; return 1; }
    [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && { log "memex: (dry run) the join follows once the stack verifies"; return 0; }
  fi
  # A portal still serving its FIRST-RUN SETUP passes `verify` but has no database: nothing is joined or
  # finalised until the console user has configured it.
  if probe_as_user "$user" env MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_SHARE="$(memex_libexec)/share" MEMEX_CHART_DIR="$(memex_libexec)/share/helm" \
       /bin/bash -c '. "$0"; awaiting_setup' "$(memex_libexec)/memex-local"; then
    log "memex: WAITING for the first-run setup — $user runs 'memex-local setup' (it prints the wizard's link) and completes it"
    return 0
  fi
  if [ -f "$home/.memex-local/fleet.yaml" ]; then
    # joined: resume a finalisation an earlier run did not finish, from the stored credentials (no new code).
    # Completion is a MARKER written only after BOTH steps succeeded — backlease.yaml alone proves nothing.
    if [ ! -f "$home/.memex-local/fleet.finalized" ]; then
      log "memex: joined as $machine — finishing: back-lease enrolment and the update"
      as_user "$user" "$mlocal" backlease enroll --control "$control" --deployment "$machine" >/dev/null 2>&1 || { log "memex: back-lease enrolment FAILED — retried next run"; return 1; }
      as_user "$user" "$mlocal" update >/dev/null 2>&1 || { log "memex: 'memex-local update' FAILED — retried next run"; return 1; }
      as_user "$user" /usr/bin/touch "$home/.memex-local/fleet.finalized" || return 1
    fi
    log "memex: joined as $machine, finished"; return 0
  fi
  code_file="$home/.memex-local/enrol/$machine.code"
  log "memex: fleet join as $machine (control $control); the code is read from $code_file once present"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then log "memex: (dry run) would run 'memex-local fleet join' and wait for the code at $code_file"; return 0; fi
  out="$(as_user "$user" "$mlocal" fleet join --control "$control" --id "$machine" --no-open --code-file "$code_file" 2>&1)"; rc=$?
  # the approval link and the fingerprints only — never a token-bearing setup URL
  printf '%s\n' "$out" | grep -E 'Hosting/Heartbeats/Enrol/|fingerprint|joined as|WAITING|waiting' | grep -v 'token=' | while IFS= read -r l; do log "memex: $l"; done
  case "$rc" in
    0) as_user "$user" /usr/bin/touch "$home/.memex-local/fleet.finalized" || true; log "memex: joined as $machine" ;;
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
  [ "$failed" = 0 ] && { step_tailscale_binaries || failed=1; }
  [ "$failed" = 0 ] && { step_tailscaled || failed=1; }
  [ "$failed" = 0 ] && { step_network "$machine" || failed=1; }
  [ "$failed" = 0 ] && { step_memex "$user" "$machine" || failed=1; }
  [ "$failed" = 0 ] && log "done (everything this run could do)" || log "a step FAILED — the next run retries from it"
  exit "$failed"
}

[ "${FLEET_BOOTSTRAP_LIB:-}" = "1" ] || main "$@"
