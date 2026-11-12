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
#   0. (right after the console user) Operator SSH: when OperatorSshKeys is configured, Remote Login is turned on
#      and each declared PUBLIC key is appended to ~<console user>/.ssh/authorized_keys (written AS that user;
#      only when absent; nothing else in the file is touched; sshd_config never). Not configured: nothing changes.
#   0b. Bootstrap token: when it is not escrowed to the MDM, the log, a reading and a notification to the person say so
#      (sign out and back in first; a later run names `sudo profiles install -type bootstraptoken`). Never a password.
#   0c. Energy enabler (1.8.0): until the Intune Energy Saver profile is installed, only `pmset -c sleep 0`, so the
#      Mac is awake to receive it (a sleeping Mac answers MDM commands NotNow during DarkWake).
#   5b. Once the join is filed (1.9.0): the approval page is opened for the person at the console and a notification
#      posted, and a root LaunchDaemon (com.meshweaver.fleet.join-poller) polls control every 30 s for at most 24 h — the
#      Approve is collected within half a minute and the run continues at once. It removes itself when done.
#   Readings the person can read without root: /Library/MeshWeaver/fleet-state/ — one file per step (operator-ssh,
#      version, bootstrap-token, energy, join-poller, participation, homebrew, devtools, tools, tailscale, tailscaled, network, up, setup, join, doctor,
#      heartbeat, buildhost) and `stage`: the step the onboarding is stuck on, since when and why (1.7.0). A join that
#      failed or was skipped for a missing prerequisite also reaches the person as a notification, naming it.
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
# OPERATOR SSH, optional: OperatorSshKeys (a profile array of OpenSSH public keys, or one string of newline-separated
# keys; environment FLEET_OPERATOR_SSH_KEYS, newline-separated). Accepted: one line each, ssh-ed25519 /
# ecdsa-sha2-nistp{256,384,521} / ssh-rsa, no options prefix, never private key material. Absent ⇒ not configured.
#
# Output: one line per step, also appended to /Library/Logs/MeshWeaver/fleet-bootstrap.log. Exit 0 when the
# run did all it could (including "waiting for X"), non-zero only when a step FAILED.
#
# Test hooks: FLEET_BOOTSTRAP_LIB=1 sources the functions without running; FLEET_BOOTSTRAP_DRY_RUN=1 prints
# every command that would change the machine instead of running it.
set -uo pipefail

readonly FLEET_BOOTSTRAP_VERSION="1.11.1"
readonly BREW_PREFIX="${FLEET_BREW_PREFIX:-/opt/homebrew}"
readonly TAILSCALED_LABEL="com.meshweaver.fleet.tailscaled"
readonly TAILSCALED_PLIST="${FLEET_LAUNCHDAEMONS_DIR:-/Library/LaunchDaemons}/${TAILSCALED_LABEL}.plist"
readonly TAILSCALED_SOCKET="${FLEET_TAILSCALED_SOCKET:-/var/run/tailscaled.socket}"
readonly TAILSCALED_STATE_DIR="${FLEET_TAILSCALED_STATE_DIR:-/var/lib/tailscale}"
readonly LOG_DIR="${FLEET_LOG_DIR:-/Library/Logs/MeshWeaver}"
# The JOIN POLLER (1.9.0): once a join is filed, a root LaunchDaemon re-runs THIS script's `poll-join` every 30 s for at
# most 24 h, so an Approve is collected within half a minute instead of at the next hourly run. It runs a root-owned
# copy of the script (Intune runs it from a temporary file) and removes itself when the join is collected, refused or
# expired.
readonly POLLER_LABEL="com.meshweaver.fleet.join-poller"
readonly POLLER_PLIST="${FLEET_LAUNCHDAEMONS_DIR:-/Library/LaunchDaemons}/${POLLER_LABEL}.plist"
readonly POLLER_SCRIPT="${FLEET_BIN_DIR:-/Library/MeshWeaver/bin}/fleet-bootstrap.sh"
readonly POLLER_EVERY_S="${FLEET_POLLER_EVERY_S:-30}"
readonly POLLER_MAX_S="${FLEET_POLLER_MAX_S:-86400}"
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
# READINGS the console user (and `memex-local fleet doctor`) may read without root — the run log is root-only. One
# root-owned 0644 file per reading, one line: "<verdict> <UTC time> <detail>". Nothing secret is ever written here.
readonly STATE_DIR="${FLEET_STATE_DIR:-/Library/MeshWeaver/fleet-state}"
# Where the Energy Saver profile (com.apple.MCX, pushed by Intune) lands once installed.
readonly MCX_PREFS="${FLEET_MCX_PREFS:-/Library/Managed Preferences/com.apple.MCX.plist}"

log() {
  local line; line="$(date -u +%Y-%m-%dT%H:%M:%SZ) fleet-bootstrap ${FLEET_BOOTSTRAP_VERSION}: $*"
  # 🚨 a token-bearing URL (the setup wizard's ?token=) never reaches the output or the log
  case "$line" in *token=*) line="$(printf '%s' "$line" | sed -E 's/token=[^[:space:]&"]+/token=<withheld>/g')" ;; esac
  printf '%s\n' "$line"
  LAST_LOG="${line#* fleet-bootstrap "$FLEET_BOOTSTRAP_VERSION": }"
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
# OPERATOR SSH (owner, 2026-11-12: "i try to avoid manual steps"). Until 1.5.0 Remote Login and the operator's key
# were set up by hand on every Mac. Now, when the profile declares OperatorSshKeys, the run turns Remote Login on,
# lets the console user through Remote Login's access group when that group restricts it, and appends each
# declared PUBLIC key to that user's ~/.ssh/authorized_keys. It never removes a line, never rewrites the file and
# never touches sshd_config. Not configured ⇒ nothing changes (Remote Login stays as it is), and the run says so.
readonly SSHD_LABEL="com.openssh.sshd"
readonly SSHD_PLIST="${FLEET_SSHD_PLIST:-/System/Library/LaunchDaemons/ssh.plist}"
readonly SSH_ACCESS_GROUP="com.apple.access_ssh"
# Appends ONE key line to "$1/.ssh/authorized_keys". Run AS the console user (as_user), so a symlink planted there
# can only ever reach what that user could write anyway: ~/.ssh 0700, the file 0600, a missing final newline
# repaired first (an append must never glue the key onto someone else's last line).
# shellcheck disable=SC2016   # a script text: expanded by the /bin/sh it is handed to, never here
readonly AUTHKEYS_APPEND_SH='umask 077; d="$1/.ssh"; f="$d/authorized_keys"
mkdir -p "$d" && chmod 700 "$d" && touch "$f" && chmod 600 "$f" || exit 1
if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then printf "\n" >> "$f" || exit 1; fi
printf "%s\n" "$2" >> "$f"'

# The declared keys, one per line, blank lines dropped: the environment (newline-separated) wins, then the managed
# preference — a plist array (one key per element) or a string (newline-separated). Pure read.
operator_ssh_keys() {
  local f="${MANAGED_PREFS}.plist" i=0 v
  if [ -n "${FLEET_OPERATOR_SSH_KEYS:-}" ]; then printf '%s\n' "$FLEET_OPERATOR_SSH_KEYS" | sed '/^[[:space:]]*$/d'; return 0; fi
  [ -f "$f" ] || return 0
  case "$(plutil -type OperatorSshKeys "$f" 2>/dev/null)" in
    array) while v="$(plutil -extract "OperatorSshKeys.$i" raw -o - "$f" 2>/dev/null)"; do printf '%s\n' "$v"; i=$((i + 1)); done ;;
    string) plutil -extract OperatorSshKeys raw -o - "$f" 2>/dev/null; printf '\n' ;;
  esac | sed '/^[[:space:]]*$/d'
}

# "<type> <base64>" of a line that is exactly ONE OpenSSH public key of an accepted type, optionally followed by a
# comment — no options prefix (`from=…`, `command=…`), no private key material, no second line — and whose blob
# names the same type it is labelled with (RFC 4253 §6.6: the blob starts with its length-prefixed type name).
# Returns 1 for anything else. Pure.
ssh_key_core() {
  local line="$1" type blob
  case "$line" in *$'\r'*|*$'\n'*) return 1 ;; esac
  printf '%s\n' "$line" | LC_ALL=C grep -Eq '^(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa) [A-Za-z0-9+/]+={0,3}( [^[:cntrl:]]*)?$' || return 1
  type="${line%% *}"; blob="${line#* }"; blob="${blob%% *}"
  [ "$(printf '%s' "$blob" | base64 --decode 2>/dev/null | head -c $((4 + ${#type})) | tail -c "${#type}")" = "$type" ] || return 1
  printf '%s %s' "$type" "$blob"
}

# The key's SHA256 fingerprint — what the log names, never the key itself. Empty when ssh-keygen cannot read it.
ssh_key_fingerprint() {
  printf '%s\n' "$1" | ssh-keygen -l -f - 2>/dev/null | awk '{print $2}'
}

# Remote Login is ON when launchd has sshd loaded and its override is not "disabled". Pure reads.
sshd_loaded() { launchctl print "system/$SSHD_LABEL" >/dev/null 2>&1; }
sshd_disabled() { launchctl print-disabled system 2>/dev/null | grep -Eq "\"$SSHD_LABEL\" => (disabled|true)"; }

# Remote Login on, as root. FIRST launchd itself — `launchctl enable` (the persisted override) + `bootstrap` of the
# system's own ssh.plist — which needs no privacy grant. Only when that does not take, `systemsetup -setremotelogin
# on`, which on current macOS refuses without Full Disk Access for the calling process ("Turning Remote Login on or
# off requires Full Disk Access privileges"), so it is the fallback and its words are logged.
step_remote_login() {
  if sshd_loaded && ! sshd_disabled; then log "operator ssh: Remote Login is on ($SSHD_LABEL loaded)"; return 0; fi
  log "operator ssh: turning Remote Login on (launchctl enable system/$SSHD_LABEL + bootstrap $SSHD_PLIST)"
  run launchctl enable "system/$SSHD_LABEL" || log "operator ssh: 'launchctl enable system/$SSHD_LABEL' failed"
  sshd_loaded || run launchctl bootstrap system "$SSHD_PLIST" || log "operator ssh: 'launchctl bootstrap system $SSHD_PLIST' failed"
  [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && return 0
  if sshd_loaded && ! sshd_disabled; then log "operator ssh: Remote Login turned on (launchctl)"; return 0; fi
  local out; out="$(systemsetup -f -setremotelogin on 2>&1)"
  if sshd_loaded && ! sshd_disabled; then log "operator ssh: Remote Login turned on (systemsetup)"; return 0; fi
  log "operator ssh: FAILED to turn Remote Login on (launchctl did not load $SSHD_LABEL; systemsetup said: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)) — retried next run"
  return 1
}

# When Remote Login is restricted to "only these users", macOS keeps that list as the group com.apple.access_ssh:
# the console user is added unless already a member (directly or through a nested group such as admin). No such
# group ⇒ every local user may log in, and nothing is changed.
step_ssh_access() {
  local user="$1"
  if ! dscl . -read "/Groups/$SSH_ACCESS_GROUP" >/dev/null 2>&1; then
    log "operator ssh: Remote Login is not restricted to a group (no $SSH_ACCESS_GROUP) — $user may log in"; return 0
  fi
  if dseditgroup -o checkmember -m "$user" "$SSH_ACCESS_GROUP" >/dev/null 2>&1; then
    log "operator ssh: $user is allowed through Remote Login's access group ($SSH_ACCESS_GROUP)"; return 0
  fi
  log "operator ssh: adding $user to $SSH_ACCESS_GROUP (Remote Login is restricted to that group)"
  run dseditgroup -o edit -a "$user" -t user "$SSH_ACCESS_GROUP" || { log "operator ssh: FAILED to add $user to $SSH_ACCESS_GROUP"; return 1; }
}

step_operator_ssh() {
  local user="$1" keys home dir f line core fp n=0 valid=() added=0 present=0 refused=0
  keys="$(operator_ssh_keys)"
  if [ -z "$keys" ]; then
    log "operator ssh: not configured (no OperatorSshKeys in the com.memex.local profile) — Remote Login and authorized_keys left as they are"
    OPSSH_VERDICT="not-configured"
    return 0
  fi
  while IFS= read -r line; do
    n=$((n + 1))
    if core="$(ssh_key_core "$line")" && fp="$(ssh_key_fingerprint "$line")" && [ -n "$fp" ]; then valid+=("$line")
    else refused=$((refused + 1)); log "operator ssh: REFUSED OperatorSshKeys entry $n — not one single-line ssh-ed25519 / ecdsa-sha2-* / ssh-rsa public key without options (its content is not logged)"
    fi
  done <<<"$keys"
  [ "${#valid[@]}" -gt 0 ] || { log "operator ssh: FAILED — none of the $n declared key(s) is an accepted public key; nothing changed"; return 1; }
  home="$(user_home "$user")"; dir="$home/.ssh"; f="$dir/authorized_keys"
  # a symlink, or a file someone else owns, where the person's own keys belong is never followed or written
  if [ -L "$dir" ] || [ -L "$f" ] || { [ -e "$dir" ] && [ "$(stat -f%Su "$dir" 2>/dev/null)" != "$user" ]; } || { [ -e "$f" ] && [ "$(stat -f%Su "$f" 2>/dev/null)" != "$user" ]; }; then
    log "operator ssh: REFUSED $f — it (or ~/.ssh) is a symlink or not owned by $user; nothing written"; return 1
  fi
  step_remote_login || return 1
  step_ssh_access "$user" || return 1
  for line in "${valid[@]}"; do
    core="$(ssh_key_core "$line")"; fp="$(ssh_key_fingerprint "$line")"
    if [ -f "$f" ] && grep -qF -- "$core" "$f"; then present=$((present + 1)); log "operator ssh: key $fp already in $f"; continue; fi
    log "operator ssh: adding key $fp to $f (as $user)"
    as_user "$user" /bin/sh -c "$AUTHKEYS_APPEND_SH" sh "$home" "$line" || { log "operator ssh: FAILED to append key $fp to $f"; return 1; }
    added=$((added + 1))
  done
  log "operator ssh: $user — $added key(s) added, $present already present, $refused refused"
  [ "$refused" = 0 ]
}

# Writes one reading (above), atomically, root-owned 0644. Dry run: printed. A reading that cannot be written is
# said and never fails the run.
write_reading() {
  local name="$1" verdict="$2" detail="${3:-}" line
  line="$verdict $(date -u +%Y-%m-%dT%H:%M:%SZ)${detail:+ $detail}"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: reading %s/%s = %s\n' "$STATE_DIR" "$name" "$line"; return 0; fi
  { mkdir -p "$STATE_DIR" && chmod 755 "$STATE_DIR" && printf '%s\n' "$line" > "$STATE_DIR/.$name.tmp" \
      && chmod 644 "$STATE_DIR/.$name.tmp" && mv -f "$STATE_DIR/.$name.tmp" "$STATE_DIR/$name"; } 2>/dev/null \
    || log "readings: could not write $STATE_DIR/$name"
}

# A notification in the console user's session (Notification Center), posted AS that user. The text reaches osascript
# as an argument, never as script source. A notification that cannot be posted is said and never fails the run.
notify_user() {
  local user="$1" title="$2" msg="$3" uid
  uid="$(id -u "$user" 2>/dev/null)" || { log "notify: no uid for $user"; return 0; }
  run launchctl asuser "$uid" sudo -u "$user" /usr/bin/osascript -e 'on run argv' \
    -e 'display notification (item 2 of argv) with title (item 1 of argv)' -e 'end run' "$title" "$msg" 2>/dev/null \
    || log "notify: could not post the notification to $user (the log line above says the same)"
}

# ---------------------------------------------------------------------------------------------------------------
# ENERGY ENABLER (1.8.0). The fleet's energy policy (never sleep, restart after a power loss) is an Intune PROFILE —
# but a Mac on macOS's default `sleep 1` cannot RECEIVE it: measured on the first Mac mini and the second Mac Studio
# (2026-11-12), every device command Intune sent arrived during PowerNap and mdmclient answered
# "NotNow … reason: Not supported during DarkWake", so the profile never installed. The one case where MDM genuinely
# cannot deliver: until the profile is installed, the bootstrap sets ONLY `pmset -c sleep 0` (system sleep on AC power,
# nothing else), so the Mac is awake when the profile arrives. Once the profile is in Managed Preferences it is the
# source of truth and the bootstrap touches nothing.
# The AC-power system-sleep minutes from `pmset -g custom` (text as $1), or nothing. Pure.
ac_sleep_minutes() {
  printf '%s\n' "$1" | awk '/^AC Power:/ { ac = 1; next } /^[A-Za-z].*:$/ { ac = 0 } ac && $1 == "sleep" { print $2; exit }'
}

step_energy_enabler() {
  local custom ac
  # the key's dots are part of its NAME (plutil -extract would read them as a key path): searched in the XML form
  # read into a variable first: `plutil … | grep -q` could SIGPIPE plutil under pipefail and read a large plist as absent
  local mcx=""; [ -f "$MCX_PREFS" ] && mcx="$(plutil -convert xml1 -o - "$MCX_PREFS" 2>/dev/null)"
  if case "$mcx" in *'<key>com.apple.EnergySaver.desktop.ACPower</key>'*) true ;; *) false ;; esac; then
    log "energy: the Energy Saver profile is installed (Intune) — it decides; nothing set here"
    write_reading energy ok "the Energy Saver profile is installed"; return 0
  fi
  custom="$(pmset -g custom 2>/dev/null)"; ac="$(ac_sleep_minutes "$custom")"
  if [ "$ac" = 0 ]; then
    log "energy: WAITING for the Energy Saver profile (Intune) — system sleep on AC is already off, so the Mac can receive it"
    write_reading energy waiting "profile not installed yet; AC system sleep already 0"; return 0
  fi
  log "energy: the Energy Saver profile is not installed and AC system sleep is ${ac:-unreadable} — a sleeping Mac answers every MDM command NotNow (DarkWake), so setting ONLY 'pmset -c sleep 0' until the profile arrives"
  if ! run pmset -c sleep 0; then
    log "energy: FAILED to set 'pmset -c sleep 0' — the profile may never reach a sleeping Mac (retried next run)"
    write_reading energy failed "pmset -c sleep 0 failed (AC sleep ${ac:-unreadable})"; return 1
  fi
  write_reading energy enabler "AC system sleep ${ac:-unreadable} → 0 until the Energy Saver profile arrives"
}

# ---------------------------------------------------------------------------------------------------------------
# BOOTSTRAP TOKEN (owner, 2026-11-12: "i'll forget for sure"). Without a bootstrap token escrowed to the MDM, Intune's
# macOS updates cannot install without an admin password at the Mac. Escrow takes a secure-token user's password, so the
# bootstrap can never do it and never asks for, reads or handles a password. It makes it impossible to forget instead:
# every run that finds it un-escrowed says so in the log, in a reading, and in a notification to the person. The first
# instruction is to sign out and back in (a supervised Mac may escrow at a secure-token user's next sign-in — NOT yet
# established on these user-enrolled Macs); a later run that still finds it un-escrowed names the command.
readonly BT_COMMAND="sudo profiles install -type bootstraptoken"

# The verdict from `profiles status -type bootstraptoken` (its text as $1): escrowed | not-escrowed | unsupported |
# unknown. Pure.
bootstrap_token_verdict() {
  local sup esc
  sup="$(printf '%s\n' "$1" | sed -n 's/.*[Ss]upported on server: *\([A-Za-z]*\).*/\1/p' | head -1 | tr 'a-z' 'A-Z')"
  esc="$(printf '%s\n' "$1" | sed -n 's/.*[Ee]scrowed to server: *\([A-Za-z]*\).*/\1/p' | head -1 | tr 'a-z' 'A-Z')"
  if [ "$esc" = YES ]; then printf 'escrowed'
  elif [ "$sup" = NO ]; then printf 'unsupported'
  elif [ "$esc" = NO ]; then printf 'not-escrowed'
  else printf 'unknown'; fi
}

step_bootstrap_token() {
  local user="$1" out verdict seen="$STATE_DIR/bootstrap-token.first-seen" since msg
  if [ -n "${FLEET_PROFILES_STATUS_FILE:-}" ]; then out="$(cat "$FLEET_PROFILES_STATUS_FILE" 2>/dev/null)"
  else out="$(profiles status -type bootstraptoken 2>&1)"; fi
  verdict="$(bootstrap_token_verdict "$out")"
  case "$verdict" in
    escrowed)
      log "bootstrap token: escrowed to the MDM (macOS updates install without an admin password)"
      write_reading bootstrap-token escrowed
      [ -e "$seen" ] && run rm -f "$seen"
      return 0 ;;
    unsupported)
      log "bootstrap token: the MDM does not support a bootstrap token (profiles: supported on server NO) — nothing to escrow"
      write_reading bootstrap-token unsupported; return 0 ;;
    unknown)
      log "bootstrap token: UNREAD — 'profiles status -type bootstraptoken' said: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
      write_reading bootstrap-token unknown; return 0 ;;
  esac
  if [ ! -f "$seen" ]; then
    since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: mark %s (first seen un-escrowed)\n' "$seen"
    else mkdir -p "$STATE_DIR" 2>/dev/null && printf '%s\n' "$since" > "$seen" 2>/dev/null; fi
    msg="Sign out and back in to finish fleet setup: this Mac's bootstrap token is not escrowed to Intune yet, so macOS updates would need an admin password."
    write_reading bootstrap-token not-escrowed "signin since $since"
  else
    since="$(head -1 "$seen" 2>/dev/null)"
    msg="Fleet setup is still not finished (bootstrap token not escrowed since ${since:-earlier}). If signing out and back in has not done it, run in Terminal: $BT_COMMAND (it asks for your admin password)."
    write_reading bootstrap-token not-escrowed "command since ${since:-unknown}"
  fi
  log "🚨 bootstrap token: NOT ESCROWED to the MDM — $msg"
  notify_user "$user" "Fleet setup" "$msg"
  return 0
}

# ---------------------------------------------------------------------------------------------------------------
# STEP READINGS (owner, 2026-11-12: "put this in intune onboarding"). The second Mac mini ran 1.6.0 to a verified stack
# and filed no join, and the reason was only in the root-only log. Every step now leaves its outcome as a reading, and
# `stage` names the step the onboarding is stuck on, since when and why. Readings carry log text only — the log never
# holds a secret (keys are files, token-bearing URLs are withheld above).
strip_ansi() { tr -d '\033' | sed -E 's/\[[0-9;]*[A-Za-z]//g'; }
# The last non-empty line a command printed: colour codes removed, a token withheld, one line, bounded.
last_said() {
  printf '%s\n' "$1" | strip_ansi | sed -E 's/token=[^[:space:]&"]+/token=<withheld>/g' | grep -v '^[[:space:]]*$' | tail -1 | cut -c1-300
}
STUCK_NAME=""; STUCK_VERDICT=""; STUCK_WHY=""; WAIT_NAME=""; WAIT_VERDICT=""; WAIT_WHY=""
# Runs one step and records its reading: ok, waiting (the step said WAITING) or failed — with the step's last log line.
# A step may name a finer stage (STEP_STAGE: up, setup, join) and write that reading itself.
track() {
  local name="$1" rc v why; shift
  STEP_STAGE=""; LAST_LOG=""
  "$@"; rc=$?
  why="$LAST_LOG"
  if [ "$rc" != 0 ]; then v=failed; else case "$why" in *WAITING*) v=waiting ;; *) v=ok ;; esac; fi
  [ -n "$STEP_STAGE" ] && name="$STEP_STAGE"
  case "$name" in up|setup|join) ;; *) write_reading "$name" "$v" "$why" ;; esac
  case "$why" in *"WAITING for the approval on control"*) v=waiting-approval ;; esac
  if [ "$v" = failed ] && [ -z "$STUCK_NAME" ]; then STUCK_NAME="$name"; STUCK_VERDICT=failed; STUCK_WHY="$why"; fi
  # the network waits for its key until the join delivers it: that wait is expected, never the stuck stage
  if [ "$v" != ok ] && [ "$v" != failed ] && [ "$name" != network ] && [ -z "$WAIT_NAME" ]; then WAIT_NAME="$name"; WAIT_VERDICT="$v"; WAIT_WHY="$why"; fi
  return "$rc"
}

# `stage`: the first failed step, else the first step still waiting, else done. A join that is not filed or not
# finished for any reason but the Approve (which is control's to give) reaches the person too.
finish_stage() {
  local user="$1" home v n why
  home="$(user_home "$user")"
  if [ -n "$STUCK_NAME" ]; then v="$STUCK_VERDICT"; n="$STUCK_NAME"; why="$STUCK_WHY"
  elif [ -n "$WAIT_NAME" ]; then v="$WAIT_VERDICT"; n="$WAIT_NAME"; why="$WAIT_WHY"
  else write_reading stage "done" "every step this run could do is done"; return 0; fi
  write_reading stage "$v" "$n: $why"
  [ -f "$home/.memex-local/fleet.yaml" ] && return 0
  [ "$v" = waiting-approval ] && return 0
  notify_user "$user" "Fleet join not filed" "This Mac has not joined the fleet: the step '$n' is $v — $why"
}

# After a finished join, what memex-local fleet doctor reads (as the person; read-only): the verdict, the
# heartbeat line and the build-host line, as readings.
record_doctor() {
  local user="$1" mlocal="$2" out rc hb bh
  [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && { printf 'DRY-RUN: (read) memex-local fleet doctor as %s\n' "$user"; return 0; }
  out="$(sudo -n -u "$user" -H env HOME="$(user_home "$user")" PATH="$(user_path)" "$mlocal" fleet doctor 2>&1 | strip_ansi)"; rc=$?
  hb="$(printf '%s\n' "$out" | grep -E "heartbeat" | head -1 | sed -E 's/^[[:space:]]*//' | cut -c1-300)"
  bh="$(printf '%s\n' "$out" | grep -E "build host|build VM" | head -1 | sed -E 's/^[[:space:]]*//' | cut -c1-300)"
  write_reading doctor "$( [ "$rc" = 0 ] && echo ok || echo red )" "$(printf '%s\n' "$out" | grep 'fleet doctor:' | tail -1 | sed -E 's/^[[:space:]]*//')"
  [ -z "$hb" ] || write_reading heartbeat "$(case "$hb" in ✓*) echo ok ;; *) echo red ;; esac)" "$hb"
  [ -z "$bh" ] || write_reading buildhost "$(case "$bh" in ✓*|·*) echo ok ;; *) echo red ;; esac)" "$bh"
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
  if [ "${#missing[@]}" -eq 0 ]; then
    # a memex-local that predates the sign-in hand-over (FLEET_SIGNIN_HANDOVER) drops a fleet Mac's sign-in during a
    # bootstrap-run join (the first Mac mini, 2026-11-12): it is upgraded from the tap, never left as it is
    if ! grep -q 'FLEET_SIGNIN_HANDOVER' "$(memex_libexec)/memex-local" 2>/dev/null; then
      log "tools: memex-local predates the sign-in hand-over — upgrading it from the tap"
      as_user "$user" "$BREW_PREFIX/bin/brew" upgrade "$MEMEX_FORMULA" >/dev/null 2>&1 || log "tools: 'brew upgrade $MEMEX_FORMULA' did not upgrade it (no newer release in the tap yet?) — retried next run"
      return 0
    fi
    log "tools: all present"; return 0
  fi
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

# THE NODE'S STATE, settled (2026-11-11, the second Mac Studio went OFFLINE): right after the daemon is (re)loaded —
# an upgraded tailscale reinstalls and restarts it — `tailscale status` FAILS while the backend is still starting
# (NoState / Starting), and the old step read that as "not joined", ran `up --auth-key … --reset` with a single-use
# key it still held from the original join, got "authkey expired" — and --reset had already logged the working node
# out. So: wait (bounded) for a settled BackendState, and decide from it AND from whether the node was ever logged in.
# Prints the BackendState (Running, NeedsLogin, NeedsMachineAuth, Stopped, …), "unsettled" or "absent" (no CLI).
ts_backend_state() {
  local ts=("$TS_DIR/bin/tailscale" --socket="$TAILSCALED_SOCKET") j st waited=0 max="${FLEET_TS_SETTLE_S:-90}"
  [ -x "$TS_DIR/bin/tailscale" ] || { printf 'absent'; return 0; }
  case "$max" in ''|*[!0-9]*) max=90 ;; esac
  while :; do
    # the JSON is read whatever the exit status: a logged-out node answers its state with a non-zero exit on some releases
    j="$("${ts[@]}" status --json 2>/dev/null || true)"
    st="$(printf '%s' "$j" | sed -n 's/.*"BackendState"[[:space:]]*:[[:space:]]*"\([A-Za-z]*\)".*/\1/p' | head -1)"
    case "$st" in ''|NoState|Starting) ;; *) printf '%s' "$st"; return 0 ;; esac
    [ "$waited" -ge "$max" ] && { printf 'unsettled'; return 0; }
    sleep 2; waited=$((waited + 2))
  done
}
# Whether this node was EVER logged in: tailscaled persists a login profile (and its node key) in its state file, and
# `status --json` reports HaveNodeKey. A node that has either must never be re-keyed with --reset.
ts_node_registered() {
  grep -q '"_profiles"\|"profile-' "$TAILSCALED_STATE_DIR/tailscaled.state" 2>/dev/null && return 0
  "$TS_DIR/bin/tailscale" --socket="$TAILSCALED_SOCKET" status --json 2>/dev/null | grep -q '"HaveNodeKey"[[:space:]]*:[[:space:]]*true'
}
# A held or dropped key is single-use: once the node is joined — by whichever path — every copy goes.
drop_preauth_keys() {
  local machine="$1" user="${2:-}" f
  local copies=("$TAILSCALED_STATE_DIR/preauth.key")
  [ -z "$user" ] || copies+=("$(user_home "$user")/.memex-local/enrol/$machine.preauth")
  for f in "${copies[@]}"; do
    if [ -e "$f" ] || [ -L "$f" ]; then run rm -f "$f"; log "network: removed the spent pre-auth key copy $f (never printed)"; fi
  done
}

# The network step's OWN reading, written whoever runs it (review of the first Mac mini, 2026-11-12: the join's call
# brought the tailnet up at 19:23:02 and the reading kept the 19:22:33 "WAITING for a single-use pre-auth key").
step_network() {
  local rc v
  LAST_LOG=""
  step_network_inner "$@"; rc=$?
  if [ "$rc" != 0 ]; then v=failed; else case "$LAST_LOG" in *WAITING*) v=waiting ;; *) v=ok ;; esac; fi
  write_reading network "$v" "$LAST_LOG"
  return "$rc"
}

step_network_inner() {
  local machine="$1" user="${2:-}" ts=("$TS_DIR/bin/tailscale" --socket="$TAILSCALED_SOCKET") state
  state="$(ts_backend_state)"
  case "$state" in
    Running) log "network: joined (BackendState Running)"; drop_preauth_keys "$machine" "$user"; return 0 ;;
    unsettled) log "network: WAITING — tailscaled did not settle within ${FLEET_TS_SETTLE_S:-90} s (still starting); nothing is changed, the next run reads it again"; return 0 ;;
  esac
  local registered=no; [ "$state" != absent ] && ts_node_registered && registered=yes
  if [ "$registered" = yes ] && [ "$state" != NeedsLogin ]; then
    # Stopped, NeedsMachineAuth, …: a node with an identity that is not running is never re-keyed by this script
    log "network: WAITING — this node is registered but its BackendState is $state; no key is used on a node that has an identity ('tailscale --socket=$TAILSCALED_SOCKET up' by a person, or approve it on the login server)"
    return 0
  fi
  local held="$TAILSCALED_STATE_DIR/preauth.key" key_file="" fresh="" login; login="$(setting FLEET_LOGIN_SERVER LoginServer)"
  # the login server the enrolment code named (`fleet join` leaves it beside the key) when the profile names none
  if [ -z "$login" ] && [ -n "$user" ]; then
    local named; named="$(user_home "$user")/.memex-local/enrol/$machine.login-server"
    [ -f "$named" ] && [ ! -L "$named" ] && login="$(head -1 "$named" | tr -d '[:space:]')"
    case "$login" in https://*) ;; *) login="" ;; esac
  fi
  if [ -n "${FLEET_PREAUTH_KEY_FILE:-}" ]; then key_file="$FLEET_PREAUTH_KEY_FILE"; fresh=yes
  else
    # a key HELD since an earlier run on a node that was already logged in is the original join's spent key: it is
    # removed, never presented (it would answer "authkey expired")
    if [ "$registered" = yes ] && [ -f "$held" ]; then run rm -f "$held"; log "network: removed a pre-auth key held since an earlier run — this node was already logged in, so it is spent"; fi
    # Intune sets no environment: the key is dropped at ~<console user>/.memex-local/enrol/<machine>.preauth
    if [ -n "$user" ]; then
      local drop; drop="$(user_home "$user")/.memex-local/enrol/$machine.preauth"
      if [ -e "$drop" ] || [ -L "$drop" ]; then take_preauth_key "$drop" "$held" "$user" || return 1; fresh=yes; fi
    fi
    [ -f "$held" ] && key_file="$held"
  fi
  if [ -z "$key_file" ] || [ ! -f "$key_file" ] || [ -z "$login" ]; then
    if [ "$registered" = yes ]; then
      log "network: OFFLINE — this node was logged in before and is logged out now (BackendState $state); it needs a NEW single-use pre-auth key at ~<console user>/.memex-local/enrol/$machine.preauth (the next run uses it, never with --reset)"
    else
      log "network: WAITING for a single-use pre-auth key and the login server (control mints the key at enrolment — Hosting/MacStudioFleet §2b.1 target 2)"
    fi
    return 0
  fi
  if [ "$registered" = yes ] && [ -z "$fresh" ]; then log "network: WAITING — no fresh key for a registered node"; return 0; fi
  # --reset only on a node that was NEVER logged in: on one that was, it would wipe its prefs before the server answers
  local reset=(); [ "$registered" = no ] && reset=(--reset)
  log "network: joining $login as $machine with the provided pre-auth key (read from its file, never printed${reset[0]:+, fresh node}${reset[0]:-, re-login without --reset})"
  local out rc
  # by FILE reference: the key never appears in an argument, a process listing or a dry-run line
  out="$(run "${ts[@]}" up --login-server "$login" --hostname "$machine" --auth-key="file:$key_file" ${reset[@]+"${reset[@]}"} 2>&1)"; rc=$?
  [ -z "$out" ] || printf '%s\n' "$out" | grep -v -i 'key' | head -5 | while IFS= read -r l; do log "network: tailscale: $l"; done
  if [ "$rc" != 0 ]; then
    case "$out" in
      # only the login server's own refusals of the KEY (headscale/tailscale wording) — any other failure keeps it
      *"authkey expired"*|*"AuthKey expired"*|*"authkey already used"*|*"AuthKey already used"*|*"invalid key"*|*"AuthKey not found"*)
        run rm -f "$key_file"; log "network: the login server REFUSED the pre-auth key (expired or already used) — removed; a new single-use key is needed"; return 0 ;;
    esac
    log "network: FAILED to join — the key file is kept for the next run"; return 1
  fi
  run rm -f "$key_file"
  drop_preauth_keys "$machine" "$user"
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
    STEP_STAGE=setup
    log "memex: WAITING for the first-run setup answers in the com.memex.local profile — missing: $( [ -n "$org" ] || printf 'Organisation '; [ -n "$owner_name" ] || printf 'OwnerName '; [ -n "$owner_email" ] || printf 'OwnerEmail '; [ "$accept" = yes ] || printf 'AcceptTerms=true ')— nothing is answered without them"
    write_reading setup skipped "${LAST_LOG#memex: }"
    return 0
  fi
  local answered="$STATE_DIR/setup.answered" out rc last
  log "memex: answering the first-run setup through the setup API (instance $machine, organisation $org, owner $owner_email)"
  out="$(as_user "$user" "$mlocal" setup --apply --name "$machine" --company "$org" --owner-name "$owner_name" \
    --owner-email "$owner_email" --accept-terms 2>&1)"; rc=$?
  printf '%s\n' "$out" | while IFS= read -r l; do log "memex:   $l"; done
  last="$(last_said "$out")"
  if [ "$rc" != 0 ]; then
    STEP_STAGE=setup; log "memex: the setup API refused or failed — ${last:-no output} — retried next run"
    write_reading setup failed "exit $rc: ${last:-no output}"; return 1
  fi
  if [ -f "$answered" ] && [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" != "1" ]; then
    # a previous run's answers were ACCEPTED and the portal is still serving the first-run setup: the join can never
    # follow this way — said as a failure, every run, never as another "answered"
    STEP_STAGE=setup
    log "memex: setup FAILED — the setup API accepted the answers at $(head -1 "$answered") but the portal is STILL in first-run setup; it said now: ${last:-nothing}"
    write_reading setup failed "answered at $(head -1 "$answered"), still in first-run setup: ${last:-no output}"; return 1
  fi
  [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] || { mkdir -p "$STATE_DIR" 2>/dev/null; date -u +%Y-%m-%dT%H:%M:%SZ > "$answered" 2>/dev/null; }
  STEP_STAGE=setup
  log "memex: setup answered (${last:-done}) — WAITING for the portal to restart configured; the join follows on the next run"
  write_reading setup waiting "answered: ${last:-done}; the join follows on the next run"
  return 0
}

# After the setup was answered the portal restarts configured: wait for that (bounded, FLEET_SETUP_WAIT_S, default
# 600 s) so the finalisation follows in the same run. Returns 1 — WAITING said — when it has not restarted in time.
wait_setup_applied() {
  local user="$1" waited=0 max="${FLEET_SETUP_WAIT_S:-600}"
  [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && { log "memex: (dry run) would wait for the portal to restart configured"; return 1; }
  while probe_as_user "$user" env MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_SHARE="$(memex_libexec)/share" MEMEX_CHART_DIR="$(memex_libexec)/share/helm" \
          /bin/bash -c '. "$0"; awaiting_setup' "$(memex_libexec)/memex-local"; do
    if [ "$waited" -ge "$max" ]; then
      STEP_STAGE="setup"; log "memex: WAITING for the portal to restart configured after the setup (${max} s so far) — the next run finishes"
      return 1
    fi
    sleep 15; waited=$((waited + 15))
  done
  log "memex: the portal restarted configured after ${waited} s — finishing in this run"
  return 0
}

# The sign-in the join HANDED OVER (memex-local FLEET_SIGNIN_HANDOVER: ~/.memex-local/enrol/<id>.signin, 0600, with
# <id>.signin-facts naming tenant and clientId) is installed with `memex-local internal on` as the person once the
# private network is up — the secret on stdin from its file, never an argument or a log line — and then removed.
step_signin_install() {
  local user="$1" machine="$2" mlocal="$3" home f facts tenant cid out v
  SIGNIN_WAITING=""
  home="$(user_home "$user")"; f="$home/.memex-local/enrol/$machine.signin"; facts="$f-facts"
  [ -e "$f" ] || [ -L "$f" ] || return 0
  # the one file holding the cleartext secret: the person's own regular file, and 0600 (review on #284)
  if [ -L "$f" ] || [ ! -f "$f" ] || [ "$(stat -f%Su "$f" 2>/dev/null)" != "$user" ] || [ "$(stat -f%Lp "$f" 2>/dev/null)" != 600 ]; then
    log "memex: REFUSED the handed-over sign-in ($f is not a 0600 regular file owned by $user)"; write_reading signin failed "the hand-over file is not $user's 0600 regular file"; return 1
  fi
  tenant="$(sed -n 's/^tenant=//p' "$facts" 2>/dev/null | head -1)"; cid="$(sed -n 's/^clientId=//p' "$facts" 2>/dev/null | head -1)"
  for v in "$tenant" "$cid"; do   # each one on its own (review on #284): an empty one never reaches `internal on`
    case "$v" in ''|*[!A-Za-z0-9._-]*) log "memex: the handed-over sign-in has no valid tenant/client id ($facts)"; write_reading signin failed "no valid tenant/client id beside the hand-over"; return 1 ;; esac
  done
  if [ "$(ts_backend_state)" != Running ]; then
    # the caller ends the step on this: the setup is never answered without a sign-in (review on #284)
    SIGNIN_WAITING=1
    log "memex: WAITING for the private network before installing the handed-over sign-in ($cid)"; write_reading signin waiting "the network is not up yet"; return 0
  fi
  log "memex: installing the handed-over sign-in ($cid) with 'memex-local internal on' (the secret from its file, never printed)"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: memex-local internal on --tenant %s --client-id %s < %s\n' "$tenant" "$cid" "$f"; return 0; fi
  if out="$(as_user "$user" /bin/sh -c 'exec "$0" internal on --tenant "$1" --client-id "$2" < "$3"' "$mlocal" "$tenant" "$cid" "$f" 2>&1)" \
     && [ -f "$home/.memex-local/internal.yaml" ]; then
    as_user "$user" /bin/rm -f "$f" "$facts"
    SIGNIN_INSTALLED=1
    log "memex: sign-in installed ($cid); the hand-over removed"; write_reading signin ok "this Mac's own sign-in app $cid"
    return 0
  fi
  log "memex: 'internal on' FAILED for the handed-over sign-in — $(last_said "$out") (the file is kept; retried next run)"
  write_reading signin failed "internal on: $(last_said "$out")"; return 1
}

step_memex() {
  local user="$1" machine="$2" control registry home mlocal
  control="$(setting FLEET_CONTROL_URL ControlUrl)"; registry="$(setting FLEET_REGISTRY_URL RegistryUrl)"
  home="$(user_home "$user")"; mlocal="$BREW_PREFIX/bin/memex-local"
  [ -n "$control" ] || { STEP_STAGE="join"; log "memex: WAITING for ControlUrl (the com.memex.local configuration profile) — the join is not filed without it"; write_reading join skipped "no ControlUrl in the com.memex.local profile"; return 0; }
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
    local up_out
    up_out="$(as_user "$user" ${ca_env[@]+"${ca_env[@]}"} "$mlocal" up 2>&1)" \
      || { STEP_STAGE=up; log "memex: 'memex-local up' FAILED — $(last_said "$up_out") — retried next run"; write_reading up failed "$(last_said "$up_out")"; return 1; }
    [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && { log "memex: (dry run) the join follows once the stack verifies"; return 0; }
  fi
  write_reading up ok "the local stack verifies"
  # A portal still serving its FIRST-RUN SETUP passes `verify` but has no database: nothing is joined or
  # finalised until the console user has configured it.
  if probe_as_user "$user" env MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_SHARE="$(memex_libexec)/share" MEMEX_CHART_DIR="$(memex_libexec)/share/helm" \
       /bin/bash -c '. "$0"; awaiting_setup' "$(memex_libexec)/memex-local"; then
    # 🚨 A FLEET Mac's sign-in (its own Entra app → `internal on`) is DELIVERED by the enrolment, and the setup refuses
    # an instance with no sign-in route ("Choose at least one way to sign in" — right for every other install). Measured
    # on the first Mac mini (2026-11-12): every run answered the setup, was refused, and the join was never filed. So on
    # a fleet Mac that has not joined, the join runs FIRST — it files the request, and on the Approve installs the
    # sign-in as host configuration, which the setup then accepts. The setup is answered on a later run.
    # joined, the setup still pending: a sign-in the join HANDED OVER (no network name at the time) is installed now
    if [ -f "$home/.memex-local/fleet.yaml" ]; then
      step_signin_install "$user" "$machine" "$mlocal" || return 1
      [ -n "${SIGNIN_WAITING:-}" ] && return 0
      # never re-file in the run that has just installed the sign-in (review on #284)
      if [ -z "${SIGNIN_INSTALLED:-}" ] && [ ! -f "$home/.memex-local/internal.yaml" ] && [ ! -f "$home/.memex-local/enrol/$machine.signin" ]; then
        # joined, but its sign-in never landed (a memex-local before the hand-over dropped it) — the setup can never pass
        # and the issued secret is gone with the join's throw-away key: a FRESH join issues a new one (owner, 2026-11-12)
        log "memex: joined but this Mac's SIGN-IN is missing (no internal.yaml) — filing a fresh join for a new one"
        as_user "$user" /bin/mv -f "$home/.memex-local/fleet.yaml" "$home/.memex-local/fleet.yaml.no-signin-$(date -u +%Y%m%dT%H%M%SZ)" \
          || { log "memex: FAILED to set the sign-in-less join aside"; return 1; }
        as_user "$user" /bin/rm -f "$home/.memex-local/fleet.finalized"
        write_reading join refiling "joined at an earlier run but the sign-in was never installed — a fresh join is filed"
      fi
    fi
    if [ ! -f "$home/.memex-local/fleet.yaml" ]; then
      log "memex: the portal is in first-run setup and this Mac has not joined — a fleet Mac's sign-in arrives with the enrolment, so the join runs BEFORE the setup"
      run_join "$user" "$machine" "$control" "$mlocal" pre-setup || return
      # not collected yet (waiting for the Approve): nothing more this run
      [ -f "$home/.memex-local/fleet.yaml" ] || [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] || return 0
      # collected — the sign-in is installed now: the setup and the finalisation follow in THIS run (1.10.0), never an
      # hour later (the first Mac mini waited a full run for nothing, 2026-11-12)
      # the join's sign-in, when it was handed over (the network came up only with the join): installed before the setup
      step_signin_install "$user" "$machine" "$mlocal" || return 1
      [ -n "${SIGNIN_WAITING:-}" ] && return 0
      log "memex: joined before the setup — answering the setup now (the sign-in is installed)"
    fi
    answer_setup "$user" "$machine" "$mlocal" || return
    wait_setup_applied "$user" || return 0
  fi
  [ -e "$STATE_DIR/setup.answered" ] && run rm -f "$STATE_DIR/setup.answered"
  write_reading setup ok "the portal is configured"
  if [ -f "$home/.memex-local/fleet.yaml" ]; then
    # joined: resume a finalisation an earlier run did not finish, from the stored credentials (no new code).
    # Completion is a MARKER written only after BOTH steps succeeded — backlease.yaml alone proves nothing.
    if [ ! -f "$home/.memex-local/fleet.finalized" ]; then
      log "memex: joined as $machine — finishing: back-lease enrolment and the update"
      as_user "$user" "$mlocal" backlease enroll --control "$control" --deployment "$machine" >/dev/null 2>&1 || { log "memex: back-lease enrolment FAILED — retried next run"; return 1; }
      as_user "$user" "$mlocal" update >/dev/null 2>&1 || { log "memex: 'memex-local update' FAILED — retried next run"; return 1; }
      as_user "$user" /usr/bin/touch "$home/.memex-local/fleet.finalized" || return 1
    fi
    log "memex: joined as $machine, finished"; write_reading join ok "joined as $machine, finished"
    record_doctor "$user" "$mlocal"; return 0
  fi
  run_join "$user" "$machine" "$control" "$mlocal" full
}

# `memex-local fleet join`, polling control for this Mac's code. Mode `full` (a configured portal): the join's whole
# tail — update, heartbeat wait — and the finalisation marker. Mode `pre-setup` (the portal still in first-run setup):
# the request, the Approve, the items (network, sign-in, certificate, CA, registry key) and the back-lease enrolment,
# but no update and no heartbeat wait (a portal in setup cannot beat) and NO finalisation marker: once the setup is
# answered, a later run's finalisation (back-lease enrolment + update) completes it.
run_join() {
  local user="$1" machine="$2" control="$3" mlocal="$4" mode="$5" home code_file out rc extra=()
  home="$(user_home "$user")"
  [ "$mode" = pre-setup ] && extra=(--no-update --heartbeat-wait 0)
  code_file="$home/.memex-local/enrol/$machine.code"
  # The POLLING join (enrolment v2): it asks control for its own code, signed with its throw-away key, for a bounded
  # wait per run; the next run resumes with the same key. A code dropped at $code_file is the fallback.
  local wait_min; wait_min="$(setting FLEET_JOIN_WAIT_MIN JoinWaitMinutes 10)"
  # the poller asks ONCE per run (`--wait 0`): launchd runs it again in 30 s
  [ "${FLEET_POLL_MODE:-}" = 1 ] && wait_min=0
  case "$wait_min" in ''|*[!0-9]*) wait_min=10 ;; esac
  log "memex: fleet join as $machine (control $control) — polling control for the code up to $wait_min min this run (fallback: $code_file)"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then STEP_STAGE="join"; log "memex: (dry run) would run 'memex-local fleet join --wait $wait_min${extra[*]:+ ${extra[*]}} ${PART_ARGS[*]-}' and poll control for the code"; return 0; fi
  # the owner's answers (read_participation): jobs NO also means --no-buildhost, so nothing of the build host is installed
  out="$(as_user "$user" "$mlocal" fleet join --control "$control" --id "$machine" --no-open --wait "$wait_min" --code-file "$code_file" ${extra[@]+"${extra[@]}"} ${PART_ARGS[@]+"${PART_ARGS[@]}"} 2>&1)"; rc=$?
  # the approval link and the fingerprints only — never a token-bearing setup URL
  printf '%s\n' "$out" | grep -E 'Hosting/Heartbeats/Enrol/|fingerprint|joined as|WAITING|waiting|approved|REFUSED|network:|sign-in:|certificate:|build host:' | grep -v 'token=' | while IFS= read -r l; do log "memex: $l"; done
  case "$rc" in
    0) # collected by an ordinary run: the poller has nothing left to do — it goes NOW, not at the end of this run
       [ -n "${FLEET_IN_POLLER:-}" ] || remove_join_poller "the Approve was collected by an hourly run"
       # the join handed the private-network key over by file: join the network now, not at the next run — and FIRST,
       # so its own log lines never overwrite the verdict this step leaves for track() (review on #276)
       step_network "$machine" "$user" || true
       if [ "$mode" = pre-setup ]; then
         # the caller (step_memex) goes on into the setup and the finalisation in this same run
         write_reading join ok "joined as $machine (before the setup; the setup and the finalisation follow in this run)"
         log "memex: joined as $machine before the setup"
       else
         as_user "$user" /usr/bin/touch "$home/.memex-local/fleet.finalized" || true
         write_reading join ok "joined as $machine"; log "memex: joined as $machine"
       fi ;;
    3) local link; link="$(printf '%s\n' "$out" | grep -o 'https://[^[:space:]]*Hosting/Heartbeats/Enrol/[^[:space:]]*' | grep -v 'token=' | tail -1)"
       # a NEW request (not the one an earlier run already announced): the approver is told now, not when someone looks
       if [ -n "$link" ] && ! grep -qF "$link" "$STATE_DIR/join" 2>/dev/null; then announce_join "$user" "$machine" "$link"; fi
       install_join_poller
       # the WAITING line is the step's LAST log line: track() reads the verdict from it
       STEP_STAGE="join"; log "memex: WAITING for the approval on control${link:+ ($link)} — polled every ${POLLER_EVERY_S} s until it is approved"
       write_reading join waiting-approval "request filed; waiting for the Approve on control${link:+: $link}" ;;
    *) local refused; refused="$(printf '%s\n' "$out" | grep -E 'REFUSED|refused' | grep -v 'token=' | tail -1 | strip_ansi | cut -c1-300)"
       STEP_STAGE="join"; log "memex: fleet join FAILED (exit $rc) — ${refused:-$(last_said "$out")}"
       write_reading join failed "exit $rc: ${refused:-$(last_said "$out")}"; return 1 ;;
  esac
}

# ONE run at a time (review on #280): the hourly run and the poller's continuation must never run the full bootstrap
# concurrently (both would race on brew, memex-local and the readings). A mkdir lock in the state directory (macOS has
# no flock): the poller SKIPS a tick while it is held, an hourly run waits up to 15 min for it. A lock whose owner is
# gone (its pid dead, or older than 2 h) is taken over. A dry run, or a state directory that cannot be written, runs
# without it (and says so).
RUN_LOCK_HELD_BY_ME=""
acquire_run_lock() {
  local wait_s="$1" lock="$STATE_DIR/.run.lock" waited=0 pid
  [ -n "${FLEET_RUN_LOCK_HELD:-}" ] && return 0
  [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] && return 0
  mkdir -p "$STATE_DIR" 2>/dev/null || { log "run lock: $STATE_DIR is not writable — running without the lock"; return 0; }
  while ! mkdir "$lock" 2>/dev/null; do
    pid="$(cat "$lock/pid" 2>/dev/null)"
    if { [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; } || [ -n "$(find "$lock" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
      log "run lock: taking over a stale lock (pid ${pid:-unknown})"; rm -rf "$lock"; continue
    fi
    [ "$waited" -ge "$wait_s" ] && return 1
    sleep 5; waited=$((waited + 5))
  done
  printf '%s\n' "$$" > "$lock/pid"
  FLEET_RUN_LOCK_HELD=1; export FLEET_RUN_LOCK_HELD; RUN_LOCK_HELD_BY_ME=1
  trap 'release_run_lock' EXIT
}
release_run_lock() {
  [ -n "$RUN_LOCK_HELD_BY_ME" ] || return 0
  rm -rf "$STATE_DIR/.run.lock"; RUN_LOCK_HELD_BY_ME=""
}

# A filed request reaches the person at this Mac at once: a notification, and the approval page opened in their browser
# (as them). The approval needs a global administrator of control — on a fleet Mac usually its owner, at this console or
# over Screen Sharing. (Control itself learns of a request only when the link is opened: the poll of an unknown key
# answers `pending` and records nothing, by design — Hosting/MacStudioOnboarding "Enrolment v2".)
announce_join() {
  local user="$1" machine="$2" link="$3" uid
  log "memex: join request for $machine FILED — the approval page is opened for $user and a notification posted"
  notify_user "$user" "Approve this Mac" "The fleet join for $machine is filed: the approval page is open in the browser (a global administrator of control approves it). This Mac collects the rest by itself."
  uid="$(id -u "$user" 2>/dev/null)" || return 0
  run launchctl asuser "$uid" sudo -u "$user" /usr/bin/open "$link" 2>/dev/null || log "memex: could not open the approval page for $user"
}

# The poller's LaunchDaemon. Pure: prints the plist.
poller_plist() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${POLLER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${POLLER_SCRIPT}</string>
    <string>poll-join</string>
  </array>
  <key>StartInterval</key><integer>${POLLER_EVERY_S}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/join-poller.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/join-poller.log</string>
</dict>
</plist>
PLIST
}

# Installs (idempotently) the root-owned script copy and the poller; records when polling began. Never from the poller.
install_join_poller() {
  [ "${FLEET_POLL_MODE:-}" = 1 ] && return 0
  local self="${BASH_SOURCE[0]}" want
  want="$(poller_plist)"
  [ -f "$STATE_DIR/join-poller.since" ] || { [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] || { mkdir -p "$STATE_DIR" && date +%s > "$STATE_DIR/join-poller.since"; } 2>/dev/null; }
  if [ -f "$POLLER_PLIST" ] && [ "$(cat "$POLLER_PLIST")" = "$want" ] && cmp -s "$self" "$POLLER_SCRIPT"; then return 0; fi
  log "memex: installing the join poller ($POLLER_LABEL, every ${POLLER_EVERY_S} s, at most $((POLLER_MAX_S / 3600)) h) — the Approve is collected within half a minute"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: install %s → %s; write %s; launchctl bootstrap system\n' "$self" "$POLLER_SCRIPT" "$POLLER_PLIST"; return 0; fi
  { install -d -o root -g wheel -m 0755 "$(dirname "$POLLER_SCRIPT")" && install -o root -g wheel -m 0755 "$self" "$POLLER_SCRIPT"; } 2>/dev/null \
    || { cp "$self" "$POLLER_SCRIPT" 2>/dev/null && chmod 755 "$POLLER_SCRIPT"; } \
    || { log "memex: FAILED to install the poller's script copy — the hourly run polls instead"; return 0; }
  mkdir -p "$(dirname "$POLLER_PLIST")" && printf '%s\n' "$want" > "$POLLER_PLIST" && chmod 644 "$POLLER_PLIST" && { chown root:wheel "$POLLER_PLIST" 2>/dev/null || true; } \
    || { log "memex: FAILED to write $POLLER_PLIST — the hourly run polls instead"; return 0; }
  launchctl bootout system "$POLLER_PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap system "$POLLER_PLIST" 2>/dev/null || log "memex: FAILED to load the join poller — the hourly run polls instead"
  return 0
}

remove_join_poller() {
  local why="$1"
  [ -f "$POLLER_PLIST" ] || [ -f "$STATE_DIR/join-poller.since" ] || return 0
  log "memex: removing the join poller ($why)"
  if [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ]; then printf 'DRY-RUN: launchctl bootout system %s; rm %s\n' "$POLLER_PLIST" "$POLLER_PLIST"; return 0; fi
  rm -f "$STATE_DIR/join-poller.since" "$POLLER_PLIST"
  write_reading join-poller removed "$why"
  # last: booting ourselves out ends this very process when the poller is the caller — the lock goes first
  [ -n "${FLEET_IN_POLLER:-}" ] && release_run_lock
  launchctl bootout "system/$POLLER_LABEL" >/dev/null 2>&1 || true
}

# One tick's work, and ONLY that (review on #280): no `memex-local up`, no verify — the probe whether the portal is still
# in its first-run setup (one HTTPS request, which picks the join's mode) and ONE poll of control (`--wait 0`).
poll_join_once() {
  local user="$1" machine="$2" control mlocal mode=full
  control="$(setting FLEET_CONTROL_URL ControlUrl)"; mlocal="$BREW_PREFIX/bin/memex-local"
  [ -n "$control" ] || { log "poll-join: no ControlUrl — nothing to poll"; return 0; }
  if probe_as_user "$user" env MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_SHARE="$(memex_libexec)/share" MEMEX_CHART_DIR="$(memex_libexec)/share/helm" \
       /bin/bash -c '. "$0"; awaiting_setup' "$(memex_libexec)/memex-local"; then mode=pre-setup; fi
  run_join "$user" "$machine" "$control" "$mlocal" "$mode"
}

# `fleet-bootstrap.sh poll-join` — what the poller runs every 30 s: ONE poll of control for this Mac's code. Collected →
# the poller goes and the full run continues at once (network, setup, finalisation). Refused, or older than 24 h → it goes.
poll_join_main() {
  [ "$(id -u)" = "0" ] || [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] || { log "poll-join: must run as root"; exit 2; }
  local user machine since now home
  FLEET_IN_POLLER=1
  user="$(console_user)" || exit 0
  home="$(user_home "$user")"; machine="$(machine_id)"
  # the lock FIRST, before either fast path (review on #280): while any run holds it this tick does nothing at all — it
  # never waits inside main, never mistakes a lock give-up for a finished run, never removes the poller. The next tick
  # (launchd starts it only after this one ended) tries again.
  acquire_run_lock 0 || exit 0
  # joined: the full run continues FIRST (in a subshell — main exits), then the poller goes: booting it out ends this
  # process, so it must be the last thing done
  if [ -f "$home/.memex-local/fleet.yaml" ]; then ( FLEET_POLL_MODE="" main ); remove_join_poller "joined"; exit 0; fi
  since="$(head -1 "$STATE_DIR/join-poller.since" 2>/dev/null)"; now="$(date +%s)"
  case "$since" in ''|*[!0-9]*) since="$now" ;; esac
  if [ $((now - since)) -ge "$POLLER_MAX_S" ]; then
    remove_join_poller "not approved within $((POLLER_MAX_S / 3600)) h — the hourly run keeps polling"; exit 0
  fi
  read_participation >/dev/null || exit 0
  FLEET_POLL_MODE=1 track memex poll_join_once "$user" "$machine"
  if [ -f "$home/.memex-local/fleet.yaml" ]; then
    ( FLEET_POLL_MODE="" main )   # continue NOW: the network, the setup and the finalisation, not at the next hourly run
    remove_join_poller "the Approve was collected"; exit 0
  fi
  case "$(head -1 "$STATE_DIR/join" 2>/dev/null)" in waiting-approval*) ;; *) remove_join_poller "the join is no longer waiting for the Approve ($(head -1 "$STATE_DIR/join" 2>/dev/null | cut -c1-120))" ;; esac
  exit 0
}

main() {
  [ "$(id -u)" = "0" ] || [ "${FLEET_BOOTSTRAP_DRY_RUN:-}" = "1" ] || { log "must run as root (the device-management layer runs it as system)"; exit 2; }
  local user machine failed=0
  if ! user="$(console_user)"; then log "no one is logged in at the console yet — nothing to do until a person logs in (next run continues)"; exit 0; fi
  # FIRST: an expired password makes this Mac unreachable over SSH — said before anything else
  check_password "$user"
  machine="$(machine_id)"
  log "run for console user $user, machine $machine"
  # which bootstrap ran — readable without root (the Intune agent runs a CACHED copy until its full sync, so the pinned
  # version and the running one can differ for hours; measured on the first Mac mini, 2026-11-12)
  write_reading version "$FLEET_BOOTSTRAP_VERSION" "run started"
  # 75 (EX_TEMPFAIL): a give-up is never read as a finished run by anything that calls main
  acquire_run_lock 900 || { log "another bootstrap run (the join poller's continuation) holds the run lock for over 15 min — this run stops; the next one retries"; exit 75; }
  # the operator's way in comes before everything that can fail: a Mac whose later steps fail is still reachable
  # failures that must not stop the rest of the run (operator SSH, the energy enabler): exit 1 at the end
  local deferred_failed=0
  if step_operator_ssh "$user"; then write_reading operator-ssh "${OPSSH_VERDICT:-ok}" "$user"
  else deferred_failed=1; write_reading operator-ssh failed "$user"; fi
  # never fails the run: it says, every run, what only a person can do (escrow takes their password)
  step_bootstrap_token "$user"
  # before anything that can fail or wait: a sleeping Mac receives no profile at all
  step_energy_enabler || deferred_failed=1
  # the owner's answers are required BEFORE anything is installed — an unanswered one fails the run, loudly
  if ! track participation read_participation; then
    log "a step FAILED — the profile must carry the owner's answers (AcceptJobs, AcceptTokens); the next run retries"
    finish_stage "$user"; exit 1
  fi
  track homebrew step_homebrew "$user" || failed=1
  [ "$failed" = 0 ] && { track devtools step_devtools || failed=1; }
  [ "$failed" = 0 ] && { track tools step_tools "$user" || failed=1; }
  [ "$failed" = 0 ] && { track tailscale step_tailscale_binaries || failed=1; }
  [ "$failed" = 0 ] && { track tailscaled step_tailscaled || failed=1; }
  [ "$failed" = 0 ] && { track network step_network "$machine" "$user" || failed=1; }
  [ "$failed" = 0 ] && { track memex step_memex "$user" "$machine" || failed=1; }
  finish_stage "$user"
  # an hourly run that finds the Mac joined removes a leftover poller (never from inside the poller: that is its own job)
  if [ -z "${FLEET_IN_POLLER:-}" ] && [ -f "$(user_home "$user")/.memex-local/fleet.yaml" ]; then remove_join_poller "joined"; fi
  [ "$deferred_failed" = 0 ] || failed=1
  [ "$failed" = 0 ] && log "done (everything this run could do)" || log "a step FAILED — the next run retries from it"
  exit "$failed"
}

if [ "${FLEET_BOOTSTRAP_LIB:-}" != "1" ]; then
  case "${1:-}" in poll-join) poll_join_main ;; *) main "$@" ;; esac
fi
