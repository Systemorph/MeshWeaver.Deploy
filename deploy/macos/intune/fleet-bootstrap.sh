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
#      else ~<console user>/.memex-local/enrol/<machine>.preauth — the path an Intune run can reach, since Intune
#      sets no environment; read by file reference and deleted after the join, never printed). Until control mints it at enrolment, a run without one says so.
#   5. memex-local: registry mode, `up`, then `fleet join`. The join's approval happens on control; the run
#      prints the approval link (and writes it to the log), and a later run completes the join once the
#      enrolment code is at its code file. Until control hands the code back itself (§2b.1, target 1), the
#      code file is where it is delivered.
#
# Configuration — the managed preference domain com.memex.local (a configuration profile; memex-local reads
# the same keys): ControlUrl, DeploymentId, RegistryUrl, FleetCaSha256 (the fleet local CA's certificate), and for the
# unattended first-run setup Organisation, OwnerName, OwnerEmail and AcceptTerms (the organisation's acceptance of the
# privacy statement and terms). Environment variables of the same meaning win: FLEET_CONTROL_URL, FLEET_MACHINE_ID,
# FLEET_REGISTRY_URL, FLEET_CA_SHA256. Nothing secret is configured here.
#
# PARTICIPATION (the owner's answers — Mac onboarding contract §5), REQUIRED, never defaulted: AcceptJobs (bool —
# does this Mac take the fleet's build-queue work), AcceptJobsFrom / AcceptJobsChannels / AcceptJobsRepos, and
# AcceptTokens (bool — does it serve local models to the token queue), AcceptTokensFrom / AcceptTokensModels /
# AcceptTokensResidency (lists: a profile array or a comma-separated string). Environment: FLEET_ACCEPT_JOBS,
# FLEET_ACCEPT_JOBS_FROM, FLEET_ACCEPT_JOBS_CHANNELS, FLEET_ACCEPT_JOBS_REPOS, FLEET_ACCEPT_TOKENS,
# FLEET_ACCEPT_TOKENS_FROM, FLEET_ACCEPT_TOKENS_MODELS, FLEET_ACCEPT_TOKENS_RESIDENCY. A run without AcceptJobs or
# AcceptTokens FAILS (exit 1, named in the log) before it installs anything. Jobs NO ⇒ no build host (no build VM,
# runners, priority agent); tokens NO ⇒ no token models beyond the portal's own chat model.
#
# Output: one line per step, also appended to /Library/Logs/MeshWeaver/fleet-bootstrap.log. Exit 0 when the
# run did all it could (including "waiting for X"), non-zero only when a step FAILED.
#
# Test hooks: FLEET_BOOTSTRAP_LIB=1 sources the functions without running; FLEET_BOOTSTRAP_DRY_RUN=1 prints
# every command that would change the machine instead of running it.
set -uo pipefail

readonly FLEET_BOOTSTRAP_VERSION="1.4.2"
readonly BREW_PREFIX="${FLEET_BREW_PREFIX:-/opt/homebrew}"
readonly TAILSCALED_LABEL="com.meshweaver.fleet.tailscaled"
readonly TAILSCALED_PLIST="${FLEET_LAUNCHDAEMONS_DIR:-/Library/LaunchDaemons}/${TAILSCALED_LABEL}.plist"
readonly TAILSCALED_SOCKET="${FLEET_TAILSCALED_SOCKET:-/var/run/tailscaled.socket}"
readonly TAILSCALED_STATE_DIR="${FLEET_TAILSCALED_STATE_DIR:-/var/lib/tailscale}"
readonly LOG_DIR="${FLEET_LOG_DIR:-/Library/Logs/MeshWeaver}"
readonly MANAGED_PREFS="${FLEET_MANAGED_PREFS:-/Library/Managed Preferences/com.memex.local}"
readonly BREW_TOOLS=(colima docker kubectl helm ollama lego)   # lego: memex-local cert issue (the internal name's certificate, DNS-01)
# 🚨 What runs AS ROOT never comes from the Homebrew tree, which the console user owns (they could replace the
# binary and run code as root at the next boot). tailscale/tailscaled are fetched BY ROOT from Homebrew's bottle
# (the formula API names the URL and its sha256, verified here), into a root-owned directory under /Library.
readonly TS_DIR="${FLEET_TS_DIR:-/Library/MeshWeaver/tailscale}"
readonly TS_FORMULA_API="${FLEET_TS_FORMULA_API:-https://formulae.brew.sh/api/formula/tailscale.json}"
readonly MEMEX_FORMULA="systemorph/memex/memex-local"
# The Command Line Tools: memex-local is a source formula, so `brew install` refuses with "No developer tools
# installed" on a fresh Mac (measured on the second Mac Studio, 2026-11-09). Installed headless, as root.
readonly CLT_DIR="${FLEET_CLT_DIR:-/Library/Developer/CommandLineTools}"
# Where a profile-deployed trusted root lives (the fleet local CA's certificate).
readonly SYSTEM_KEYCHAIN="${FLEET_SYSTEM_KEYCHAIN:-/Library/Keychains/System.keychain}"

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

# The PATH every command run as the console user gets: Homebrew's bin (Apple silicon, then Intel), the root-owned
# tailscale client, the system. Never the person's shell PATH — on the second Mac Studio (2026-11-09) that had no
# Homebrew at all, and a kubectl that is "not found" made memex-local print nothing.
user_path() { printf '%s' "$BREW_PREFIX/bin:/usr/local/bin:$TS_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin"; }

# A READ-ONLY probe as the console user: it RUNS even in a dry run (it changes nothing), so a dry run plans
# from what is really there instead of from a simulated success. A failed probe means "not yet".
probe_as_user() {
  local user="$1" home; shift; home="$(user_home "$user")"
  sudo -n -u "$user" -H env HOME="$home" PATH="$(user_path)" "$@" >/dev/null 2>&1
}

# Runs as the console user with that user's HOME and Homebrew on PATH.
as_user() {
  local user="$1" home; shift; home="$(user_home "$user")"
  # the root-owned tailscale client is on the user's PATH too (execute-only for them): memex-local's network
  # check and `publish on` need it, and it talks to the root daemon's socket
  run sudo -u "$user" -H env HOME="$home" PATH="$(user_path)" "$@"
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

# ---------------------------------------------------------------------------------------------------------------
# The console user's PASSWORD. Measured on the second Mac Studio (2026-11-09): Intune enrolment applied the
# tenant's password policy at once (a changeAtNextAuth policy), the local account's password was marked expired,
# and every non-interactive SSH login then failed with "Your password has expired. Password change required but
# no TTY available." The bootstrap cannot change policy (that is Intune's), but it SAYS SO, as the first line of
# its run, with the remedy — instead of a Mac that silently cannot be reached.
#
# The verdict, pure over its inputs: $1 the account policies (a plist: `pwpolicy -getaccountpolicies`, global or
# per user), $2 the user's accountPolicyData (a plist: `dscl . -read /Users/<u> accountPolicyData`), $3 now (unix
# seconds). Prints why a password change is due, or nothing. Two policy shapes are read: a change REQUIRED from a
# moment on (policyAttributeNewPasswordRequiredTime — Intune's changeAtNextAuth: due when the password was last
# set before that moment and the moment has passed) and an AGE limit (policyAttributeExpiresEveryNDays).
password_expiry_verdict() {
  local pol="$1" apd="$2" now="$3" last i id t n
  [ -f "$pol" ] || return 0
  # when the password was last set — unreadable (no such account, no record): no verdict, never a guess
  last="$(plutil -extract passwordLastSetTime raw -o - "$apd" 2>/dev/null || true)"; last="${last%%.*}"
  case "$last" in ''|*[!0-9]*) return 0 ;; esac
  i=0
  while plutil -extract "policyCategoryPasswordChange.$i" xml1 -o /dev/null "$pol" >/dev/null 2>&1; do
    id="$(plutil -extract "policyCategoryPasswordChange.$i.policyIdentifier" raw -o - "$pol" 2>/dev/null || true)"
    t="$(plutil -extract "policyCategoryPasswordChange.$i.policyParameters.policyAttributeNewPasswordRequiredTime" raw -o - "$pol" 2>/dev/null || true)"; t="${t%%.*}"
    case "$t" in ''|*[!0-9]*) ;; *)
      if [ "$last" -lt "$t" ] && [ "$now" -ge "$t" ]; then
        printf 'a password change is REQUIRED since %s (policy %s); the password was last set %s' \
          "$(date -u -r "$t" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "$t")" "${id:-unnamed}" \
          "$(date -u -r "$last" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "$last")"
        return 0
      fi ;;
    esac
    n="$(plutil -extract "policyCategoryPasswordChange.$i.policyParameters.policyAttributeExpiresEveryNDays" raw -o - "$pol" 2>/dev/null || true)"; n="${n%%.*}"
    case "$n" in ''|*[!0-9]*) ;; *)
      if [ "$n" -gt 0 ] && [ "$now" -ge $(( last + n * 86400 )) ]; then
        printf 'the password EXPIRED: older than %s days (policy %s)' "$n" "${id:-unnamed}"; return 0
      fi ;;
    esac
    i=$((i + 1))
  done
  return 0
}

# Reads the inputs for the console user (as root) and logs the verdict LOUDLY when a change is due. Test hooks:
# FLEET_ACCOUNT_POLICIES_FILE, FLEET_USER_ACCOUNT_POLICIES_FILE, FLEET_ACCOUNT_POLICY_DATA_FILE, FLEET_NOW.
check_password() {
  local user="$1" tmp v="" f now="${FLEET_NOW:-$(date +%s)}"
  tmp="$(mktemp -d)" || return 0
  if [ -n "${FLEET_ACCOUNT_POLICIES_FILE:-}" ]; then cp "$FLEET_ACCOUNT_POLICIES_FILE" "$tmp/global.plist" 2>/dev/null
  else pwpolicy -getaccountpolicies 2>/dev/null | sed -n '/^<?xml/,$p' > "$tmp/global.plist"; fi
  if [ -n "${FLEET_USER_ACCOUNT_POLICIES_FILE:-}" ]; then cp "$FLEET_USER_ACCOUNT_POLICIES_FILE" "$tmp/user.plist" 2>/dev/null
  else pwpolicy -u "$user" -getaccountpolicies 2>/dev/null | sed -n '/^<?xml/,$p' > "$tmp/user.plist"; fi
  if [ -n "${FLEET_ACCOUNT_POLICY_DATA_FILE:-}" ]; then cp "$FLEET_ACCOUNT_POLICY_DATA_FILE" "$tmp/apd.plist" 2>/dev/null
  else dscl . -read "/Users/$user" accountPolicyData 2>/dev/null | sed -n '/^ *<?xml/,$p' | sed 's/^ //' > "$tmp/apd.plist"; fi
  for f in "$tmp/global.plist" "$tmp/user.plist"; do
    [ -s "$f" ] || continue
    v="$(password_expiry_verdict "$f" "$tmp/apd.plist" "$now")"
    [ -z "$v" ] || break
  done
  rm -rf "$tmp"
  [ -n "$v" ] || return 0
  log "🚨🚨 PASSWORD EXPIRED for $user: $v. Every non-interactive SSH login fails until it is changed (\"Password change required but no TTY available\"). REMEDY: the person signs in at this Mac's console (the login window asks for a new password) or runs 'passwd' in Terminal; device-management policy should not force it on a fleet Mac (Intune: the macOS password policy's 'change at next sign-in')"
  return 0
}

# ---------------------------------------------------------------------------------------------------------------
# PARTICIPATION — the owner's two answers (contract §5), read from the profile, never defaulted.
# A list preference as `defaults read` prints it (an array "( a, b )" or a plain string) → "a,b".
pref_list() {
  printf '%s' "$1" | tr -d '()"\n\t' | tr ',' ' ' | tr -s ' ' | sed -e 's/^ //' -e 's/ $//' | tr ' ' ','
}
# A boolean preference → true | false; anything else (absent included) → nothing.
pref_bool() {
  case "$(printf '%s' "$1" | tr -d ' \n' | tr 'A-Z' 'a-z')" in
    1|true|yes) printf 'true' ;;
    0|false|no) printf 'false' ;;
  esac
}
# Sets PART_JOBS / PART_TOKENS (true|false) and PART_ARGS (the `fleet join` flags). Returns 1 — after saying
# exactly which key is missing or wrong — when an answer is absent or incomplete: an unanswered question is
# never read as a no (or a yes).
PART_JOBS=""; PART_TOKENS=""; PART_ARGS=()
read_participation() {
  local jobs_raw tok_raw jfrom jch jrepo tfrom tmodels tres bad=0
  jobs_raw="$(setting FLEET_ACCEPT_JOBS AcceptJobs)"; tok_raw="$(setting FLEET_ACCEPT_TOKENS AcceptTokens)"
  PART_JOBS="$(pref_bool "$jobs_raw")"; PART_TOKENS="$(pref_bool "$tok_raw")"
  if [ -z "$PART_JOBS" ]; then
    log "participation: FAILED — AcceptJobs is ${jobs_raw:+not a boolean ('$jobs_raw')}${jobs_raw:-absent}: the owner's answer to 'does this Mac take the fleet's build-queue jobs' is required, never defaulted. Set AcceptJobs (true/false) in the com.memex.local profile"
    bad=1
  fi
  if [ -z "$PART_TOKENS" ]; then
    log "participation: FAILED — AcceptTokens is ${tok_raw:+not a boolean ('$tok_raw')}${tok_raw:-absent}: the owner's answer to 'does this Mac serve local models to the token queue' is required, never defaulted. Set AcceptTokens (true/false) in the com.memex.local profile"
    bad=1
  fi
  [ "$bad" = 0 ] || return 1
  PART_ARGS=()
  if [ "$PART_JOBS" = true ]; then
    jfrom="$(pref_list "$(setting FLEET_ACCEPT_JOBS_FROM AcceptJobsFrom)")"
    jch="$(pref_list "$(setting FLEET_ACCEPT_JOBS_CHANNELS AcceptJobsChannels)")"
    jrepo="$(pref_list "$(setting FLEET_ACCEPT_JOBS_REPOS AcceptJobsRepos)")"
    [ -n "$jfrom" ] || { log "participation: FAILED — AcceptJobs is true but AcceptJobsFrom is empty: name the instances whose jobs this Mac takes"; return 1; }
    PART_ARGS+=("--accept-jobs=$jfrom")
    [ -z "$jch" ] || PART_ARGS+=(--job-channels "$jch")
    [ -z "$jrepo" ] || PART_ARGS+=(--job-repos "$jrepo")
  else
    PART_ARGS+=(--no-accept-jobs --no-buildhost)
  fi
  if [ "$PART_TOKENS" = true ]; then
    tfrom="$(pref_list "$(setting FLEET_ACCEPT_TOKENS_FROM AcceptTokensFrom)")"
    tmodels="$(pref_list "$(setting FLEET_ACCEPT_TOKENS_MODELS AcceptTokensModels)")"
    tres="$(pref_list "$(setting FLEET_ACCEPT_TOKENS_RESIDENCY AcceptTokensResidency)")"
    [ -n "$tfrom" ] || { log "participation: FAILED — AcceptTokens is true but AcceptTokensFrom is empty: name the instances whose token requests this Mac serves"; return 1; }
    [ -n "$tmodels" ] || { log "participation: FAILED — AcceptTokens is true but AcceptTokensModels is empty: name the models this Mac serves"; return 1; }
    PART_ARGS+=("--accept-tokens=$tfrom" --token-models "$tmodels")
    [ -z "$tres" ] || PART_ARGS+=(--residency "$tres")
  else
    PART_ARGS+=(--no-accept-tokens)
  fi
  log "participation: jobs ${PART_JOBS}${jfrom:+ (from $jfrom)}$( [ "$PART_JOBS" = true ] || printf ' — no build host'), tokens ${PART_TOKENS}${tfrom:+ (from $tfrom; models $tmodels)}$( [ "$PART_TOKENS" = true ] || printf ' — no token models beyond the chat model')"
}

# The person's login shell finds Homebrew: the documented `brew shellenv` line in ~/.zprofile, written ONCE
# (idempotent), as that person. Without it a new Terminal has no $BREW_PREFIX/bin, and every tool the
# bootstrap installed reads as missing to the person (measured on the second Mac Studio, 2026-11-09).
ensure_shellenv() {
  local user="$1" home line profile
  home="$(user_home "$user")"; profile="$home/.zprofile"
  line="eval \"\$($BREW_PREFIX/bin/brew shellenv)\""
  if [ -f "$profile" ] && grep -qF "$BREW_PREFIX/bin/brew shellenv" "$profile"; then return 0; fi
  log "homebrew: adding the shellenv line to $profile (a new Terminal finds the tools)"
  as_user "$user" /bin/sh -c 'printf "\n%s\n" "$1" >> "$2"' sh "$line" "$profile" \
    || { log "homebrew: FAILED to write $profile"; return 1; }
}

step_homebrew() {
  local user="$1"
  if [ -x "$BREW_PREFIX/bin/brew" ]; then log "homebrew: present ($BREW_PREFIX)"; ensure_shellenv "$user"; return; fi
  log "homebrew: installing under $BREW_PREFIX for $user (untar install, no prompt)"
  run mkdir -p "$BREW_PREFIX" && run chown -R "$user:admin" "$BREW_PREFIX" \
    && as_user "$user" /bin/bash -c "curl -fsSL https://github.com/Homebrew/brew/tarball/main | tar xz --strip-components 1 -C '$BREW_PREFIX'" \
    || { log "homebrew: FAILED to install"; return 1; }
  ensure_shellenv "$user"
}

# Whether a compiler toolchain Homebrew accepts is present: the Command Line Tools, or (only when the CLT
# directory is not overridden) a selected Xcode. Pure read.
devtools_present() {
  [ -x "$CLT_DIR/usr/bin/clang" ] && return 0
  [ -z "${FLEET_CLT_DIR:-}" ] || return 1
  local dev; dev="$(xcode-select -p 2>/dev/null || true)"
  [ -n "$dev" ] && [ -x "$dev/usr/bin/clang" ]
}

# The Command Line Tools, installed with softwareupdate (no dialog, no person): the on-demand flag makes the
# catalogue offer them, the newest label is installed, and the flag is removed whatever happened.
step_devtools() {
  if devtools_present; then log "developer tools: present"; return 0; fi
  local flag="/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress" label
  log "developer tools: installing the Command Line Tools (softwareupdate, headless — memex-local builds from source)"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: softwareupdate -i "<newest Command Line Tools label>"\n'; return 0; fi
  touch "$flag"
  label="$(softwareupdate -l 2>/dev/null | sed -n 's/^[[:space:]]*\* Label: \(Command Line Tools.*\)$/\1/p' | sort -V | tail -1)"
  if [ -z "$label" ]; then rm -f "$flag"; log "developer tools: FAILED — softwareupdate offers no Command Line Tools (retried next run)"; return 1; fi
  local out; out="$(softwareupdate -i "$label" 2>&1)"
  rm -f "$flag"
  if ! devtools_present; then
    log "developer tools: FAILED to install '$label' (retried next run) — softwareupdate said:"
    printf '%s\n' "$out" | tail -5 | while IFS= read -r l; do log "developer tools:   $l"; done
    return 1
  fi
  log "developer tools: installed '$label'"
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

# A default-path pre-auth key is taken into ROOT's hands before it is used: the directory entry is RENAMED into
# the root-owned state directory (a rename moves a symlink as a symlink, never its target), validated THERE —
# where the console user can no longer swap it — and made root's own. Only then is it read, by file reference.
# Returns 1 (and removes it) when it is not a regular file the console user owned.
take_preauth_key() {
  local src="$1" held="$2" user="$3"
  [ -e "$src" ] || [ -L "$src" ] || return 0
  run mv -f "$src" "$held" || { log "network: FAILED to take the pre-auth key file"; return 1; }
  [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && return 0
  if [ -L "$held" ] || [ ! -f "$held" ] || [ "$(stat -f%Su "$held" 2>/dev/null)" != "$user" ]; then
    rm -f "$held"; log "network: REFUSED the pre-auth key file (not a regular file owned by $user) — removed"; return 1
  fi
  chown root:wheel "$held" 2>/dev/null || true; chmod 600 "$held"
}

step_network() {
  local machine="$1" user="${2:-}" ts=("$TS_DIR/bin/tailscale" --socket="$TAILSCALED_SOCKET")
  if "${ts[@]}" status >/dev/null 2>&1; then log "network: joined"; return 0; fi
  local held="$TAILSCALED_STATE_DIR/preauth.key" key_file="" login; login="$(setting FLEET_LOGIN_SERVER LoginServer)"
  # the login server the enrolment code named (`fleet join` leaves it beside the key) when the profile names none
  if [ -z "$login" ] && [ -n "$user" ]; then
    local named; named="$(user_home "$user")/.memex-local/enrol/$machine.login-server"
    [ -f "$named" ] && [ ! -L "$named" ] && login="$(head -1 "$named" | tr -d '[:space:]')"
    case "$login" in https://*) ;; *) login="" ;; esac
  fi
  if [ -n "${FLEET_PREAUTH_KEY_FILE:-}" ]; then key_file="$FLEET_PREAUTH_KEY_FILE"
  else
    # Intune sets no environment: the key is dropped at ~<console user>/.memex-local/enrol/<machine>.preauth
    [ -n "$user" ] && { take_preauth_key "$(user_home "$user")/.memex-local/enrol/$machine.preauth" "$held" "$user" || return 1; }
    [ -f "$held" ] && key_file="$held"
  fi
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

# The PEM of the certificate whose SHA-256 is $1, from `security find-certificate -a -Z -p` on stdin. Pure.
pem_for_sha256() {
  awk -v want="$(printf '%s' "$1" | tr 'a-f' 'A-F')" '
    /^SHA-256 hash: / { cur = $3; next }
    /-----BEGIN CERTIFICATE-----/ { on = (cur == want) }
    on { print }
    /-----END CERTIFICATE-----/ { if (on) exit }'
}

# The FLEET LOCAL CA, trusted by PROFILE: the System keychain holds the certificate the declared FleetCaSha256 names
# and this Mac trusts it (deployed by device management as a trusted root — no person, no `mkcert -install`).
# Prints the fingerprint (64 hex) and exits 0; exits 1 when there is no such CA.
fleet_ca_trusted_by_profile() {
  local sha pem rc=1
  sha="$(setting FLEET_CA_SHA256 FleetCaSha256 | tr -d ': ' | tr 'A-F' 'a-f')"; sha="${sha#sha256}"
  printf '%s' "$sha" | grep -Eq '^[0-9a-f]{64}$' || return 1
  pem="$(mktemp)" || return 1
  security find-certificate -a -Z -p "$SYSTEM_KEYCHAIN" 2>/dev/null | pem_for_sha256 "$sha" > "$pem"
  if [ -s "$pem" ] && [ "$(openssl x509 -in "$pem" -outform DER 2>/dev/null | shasum -a 256 | cut -d' ' -f1)" = "$sha" ] && ca_trusted "$pem"; then
    printf '%s' "$sha"; rc=0
  fi
  rm -f "$pem"; return "$rc"
}

# The first-run setup, answered through the portal's setup API from the managed profile (no person at the
# wizard). Waits (returns 0) when the profile lacks an answer; returns 1 when the API refused or failed.
answer_setup() {
  local user="$1" machine="$2" mlocal="$3"
  # 🚨 No person at the wizard on a fleet Mac (owner, 2026-11-09: "pls get rid of this memex-local setup step"):
  # the setup is answered through the portal's setup API from the managed profile — the instance name is the
  # machine id, the owner and organisation are the profile's, the database is the one the wizard pre-selects,
  # and the privacy statement and terms are accepted ONLY where the profile says so (AcceptTerms = true).
  local org owner_name owner_email accept
  org="$(setting FLEET_ORGANISATION Organisation)"; owner_name="$(setting FLEET_OWNER_NAME OwnerName)"
  owner_email="$(setting FLEET_OWNER_EMAIL OwnerEmail)"; accept="$(setting FLEET_ACCEPT_TERMS AcceptTerms)"
  case "$accept" in 1|true|TRUE|True|yes) accept=yes ;; *) accept=no ;; esac
  if [ -z "$org" ] || [ -z "$owner_name" ] || [ -z "$owner_email" ] || [ "$accept" != "yes" ]; then
    log "memex: WAITING for the first-run setup answers in the com.memex.local profile (Organisation, OwnerName, OwnerEmail, AcceptTerms=true) — nothing is answered without them"
    return 0
  fi
  log "memex: answering the first-run setup through the setup API (instance $machine, organisation $org, owner $owner_email)"
  as_user "$user" "$mlocal" setup --apply --name "$machine" --company "$org" --owner-name "$owner_name" \
    --owner-email "$owner_email" --accept-terms 2>&1 | while IFS= read -r l; do log "memex:   $l"; done
  [ "${PIPESTATUS[0]}" = "0" ] || { log "memex: the setup API refused or failed (above) — retried next run"; return 1; }
  log "memex: setup answered — the portal restarts configured; the join follows on the next run"
  return 0
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
  # A FLEET Mac does not wait on that at all: the fleet local CA (name-constrained to the loopback names) is trusted
  # by its profile, and its key arrives sealed in the enrolment code (item `a`), which `memex-local ca adopt` installs
  # as mkcert's CA. Until the key is here, `up` runs with MEMEX_CA_TRUST=defer (no keychain change, no prompt).
  local caroot fleet_sha ca_env=() key_drop="$home/.memex-local/enrol/fleet-ca.key"
  caroot="$(as_user "$user" env TRUST_STORES=nss "$BREW_PREFIX/bin/mkcert" -CAROOT 2>/dev/null || true)"
  if [ -n "$caroot" ] && [ -f "$caroot/rootCA.pem" ] && ca_trusted "$caroot/rootCA.pem"; then
    :   # trusted — the adopted fleet CA, or a CA a person trusted once
  elif fleet_sha="$(fleet_ca_trusted_by_profile)"; then
    if [ -f "$key_drop" ]; then
      log "memex: adopting the fleet local CA (trusted by profile, sha256:${fleet_sha:0:12}) — its key arrived with the enrolment code"
      # adopt's own refusals (key not 0600, key ≠ the declared certificate, no such certificate in the keychain) are the
      # reasons an unattended Mac needs in its log. adopt never prints the key (fingerprints only), so its output is logged.
      local adopt_out
      if ! adopt_out="$(as_user "$user" "$mlocal" ca adopt --key-file "$key_drop" --sha256 "$fleet_sha" 2>&1)"; then
        log "memex: 'memex-local ca adopt' FAILED — retried next run; it said:"
        printf '%s\n' "$adopt_out" | tail -5 | while IFS= read -r l; do log "memex:   $l"; done
        return 1
      fi
    else
      log "memex: the fleet local CA is trusted by profile (sha256:${fleet_sha:0:12}) — not waiting on 'mkcert -install'; the portal's certificate moves under it when its key arrives with the enrolment code"
      ca_env=(env MEMEX_CA_TRUST=defer)
    fi
  else
    if [ -n "$caroot" ] && [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" != "1" ] && [ ! -f "$caroot/rootCA.pem" ]; then
      as_user "$user" env TRUST_STORES=nss "$BREW_PREFIX/bin/mkcert" -install >/dev/null 2>&1 || true
    fi
    if [ -n "$caroot" ] && [ -f "$caroot/rootCA.pem" ] && ! ca_trusted "$caroot/rootCA.pem"; then
      log "memex: WAITING for the local CA to be trusted ($caroot/rootCA.pem, public) — a fleet Mac gets the fleet local CA's trusted-root profile and FleetCaSha256; otherwise run 'mkcert -install' once as $user"
      return 0
    fi
  fi
  # the stack is provisioned when it VERIFIES usable, not when the VM merely runs — `up` is retried until it does
  if ! probe_as_user "$user" "$mlocal" verify; then
    log "memex: provisioning the local stack (memex-local up)"
    as_user "$user" ${ca_env[@]+"${ca_env[@]}"} "$mlocal" up >/dev/null 2>&1 || { log "memex: 'memex-local up' FAILED — retried next run"; return 1; }
    [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && { log "memex: (dry run) the join follows once the stack verifies"; return 0; }
  fi
  # A portal still serving its FIRST-RUN SETUP passes `verify` but has no database: nothing is joined or
  # finalised until the console user has configured it.
  if probe_as_user "$user" env MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_SHARE="$(memex_libexec)/share" MEMEX_CHART_DIR="$(memex_libexec)/share/helm" \
       /bin/bash -c '. "$0"; awaiting_setup' "$(memex_libexec)/memex-local"; then
    answer_setup "$user" "$machine" "$mlocal"; return
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
  # The POLLING join (enrolment v2): it asks control for its own code, signed with its throw-away key, for a bounded
  # wait per run; the next run resumes with the same key. A code dropped at $code_file is the fallback.
  local wait_min; wait_min="$(setting FLEET_JOIN_WAIT_MIN JoinWaitMinutes 10)"
  case "$wait_min" in ''|*[!0-9]*) wait_min=10 ;; esac
  log "memex: fleet join as $machine (control $control) — polling control for the code up to $wait_min min this run (fallback: $code_file)"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then log "memex: (dry run) would run 'memex-local fleet join --wait $wait_min ${PART_ARGS[*]-}' and poll control for the code"; return 0; fi
  # the owner's answers (read_participation): jobs NO also means --no-buildhost, so nothing of the build host is installed
  out="$(as_user "$user" "$mlocal" fleet join --control "$control" --id "$machine" --no-open --wait "$wait_min" --code-file "$code_file" ${PART_ARGS[@]+"${PART_ARGS[@]}"} 2>&1)"; rc=$?
  # the approval link and the fingerprints only — never a token-bearing setup URL
  printf '%s\n' "$out" | grep -E 'Hosting/Heartbeats/Enrol/|fingerprint|joined as|WAITING|waiting|approved|REFUSED|network:|sign-in:|certificate:|build host:' | grep -v 'token=' | while IFS= read -r l; do log "memex: $l"; done
  case "$rc" in
    0) as_user "$user" /usr/bin/touch "$home/.memex-local/fleet.finalized" || true; log "memex: joined as $machine"
       # the join handed the private-network key over by file: join the network now, not at the next run
       step_network "$machine" "$user" || true ;;
    3) log "memex: WAITING for the approval on control — the next run polls again" ;;
    *) log "memex: fleet join FAILED (exit $rc)"; return 1 ;;
  esac
}

main() {
  [ "$(id -u)" = "0" ] || [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] || { log "must run as root (the device-management layer runs it as system)"; exit 2; }
  local user machine failed=0
  if ! user="$(console_user)"; then log "no one is logged in at the console yet — nothing to do until a person logs in (next run continues)"; exit 0; fi
  # FIRST: an expired password makes this Mac unreachable over SSH — said before anything else
  check_password "$user"
  machine="$(machine_id)"
  log "run for console user $user, machine $machine"
  # the owner's answers are required BEFORE anything is installed — an unanswered one fails the run, loudly
  read_participation || { log "a step FAILED — the profile must carry the owner's answers (AcceptJobs, AcceptTokens); the next run retries"; exit 1; }
  step_homebrew "$user" || failed=1
  [ "$failed" = 0 ] && { step_devtools || failed=1; }
  [ "$failed" = 0 ] && { step_tools "$user" || failed=1; }
  [ "$failed" = 0 ] && { step_tailscale_binaries || failed=1; }
  [ "$failed" = 0 ] && { step_tailscaled || failed=1; }
  [ "$failed" = 0 ] && { step_network "$machine" "$user" || failed=1; }
  [ "$failed" = 0 ] && { step_memex "$user" "$machine" || failed=1; }
  [ "$failed" = 0 ] && log "done (everything this run could do)" || log "a step FAILED — the next run retries from it"
  exit "$failed"
}

[ "${FLEET_BOOTSTRAP_LIB:-}" = "1" ] || main "$@"
