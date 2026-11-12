#!/bin/bash
# fleet-loader.sh — the STABLE script the device-management layer (Intune) runs, as root, every hour. It does one thing:
# run the fleet bootstrap the com.memex.local profile PINS (BootstrapCommit + BootstrapSha256), fetched from this
# repository at that commit and verified by its sha256.
#
# Why (measured on the first Mac mini, 2026-11-12): the Intune agent re-runs its CACHED copy of a script until its next
# full sync, every 8 hours, and `syncDevice` does not change that — but a PROFILE arrives within a minute of a
# `syncDevice`. So the bootstrap's pin travels in the profile, and a bump reaches every awake Mac within minutes of the
# reconcile's apply; this loader changes only when its own behaviour does.
#
# Order: the pinned bootstrap (the verified copy when it already matches the pin, else fetched and verified) → when there
# is no pin, the fetch fails or the bytes do not match: the EMBEDDED copy the reconcile appends to the uploaded script
# (after the line __FLEET_EMBEDDED_BOOTSTRAP__) → else the last verified copy → else nothing (exit 1, said).
# Every decision is logged (/Library/Logs/MeshWeaver/fleet-loader.log) and left as the reading `loader`.
set -uo pipefail

readonly FLEET_LOADER_VERSION="1.0.0"
readonly MANAGED_PREFS="${FLEET_MANAGED_PREFS:-/Library/Managed Preferences/com.memex.local}"
readonly BOOTSTRAP_REPO="${FLEET_BOOTSTRAP_REPO:-Systemorph/MeshWeaver.Deploy}"
readonly BOOTSTRAP_PATH="deploy/macos/intune/fleet-bootstrap.sh"
readonly BASE_URL="${FLEET_BOOTSTRAP_BASE_URL:-https://raw.githubusercontent.com}"
readonly LOADER_DIR="${FLEET_LOADER_DIR:-/Library/MeshWeaver/loader}"
readonly VERIFIED="$LOADER_DIR/fleet-bootstrap.sh"
readonly LOG_DIR="${FLEET_LOG_DIR:-/Library/Logs/MeshWeaver}"
readonly STATE_DIR="${FLEET_STATE_DIR:-/Library/MeshWeaver/fleet-state}"
readonly MARK="__FLEET_EMBEDDED_BOOTSTRAP__"

llog() {
  local line; line="$(date -u +%Y-%m-%dT%H:%M:%SZ) fleet-loader ${FLEET_LOADER_VERSION}: $*"
  printf '%s\n' "$line"
  { mkdir -p "$LOG_DIR" && ( umask 077; printf '%s\n' "$line" >> "$LOG_DIR/fleet-loader.log" ); } 2>/dev/null || true
}
reading() {   # one world-readable line, as the bootstrap's readings
  { mkdir -p "$STATE_DIR" && printf '%s %s %s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" > "$STATE_DIR/.loader.tmp" \
      && chmod 644 "$STATE_DIR/.loader.tmp" && mv -f "$STATE_DIR/.loader.tmp" "$STATE_DIR/loader"; } 2>/dev/null || true
}
# A profile key (the environment wins: FLEET_BOOTSTRAP_COMMIT / FLEET_BOOTSTRAP_SHA256).
pref() {
  local env_name="$1" key="$2" v
  v="${!env_name:-}"
  [ -n "$v" ] || v="$(plutil -extract "$key" raw -o - "${MANAGED_PREFS}.plist" 2>/dev/null || true)"
  printf '%s' "$v"
}
sha_of() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1; }
# Places a file root-owned 0755 in the loader directory, atomically.
place() {
  local src="$1" dst="$2"
  mkdir -p "$LOADER_DIR" && chmod 755 "$LOADER_DIR" && cp "$src" "$dst.tmp" && chmod 755 "$dst.tmp" \
    && { [ "$(id -u)" != 0 ] || chown root:wheel "$dst.tmp"; } && mv -f "$dst.tmp" "$dst"
}
# The copy embedded after the marker line of THIS file, into $1. Returns 1 when there is none.
embedded_to() {
  awk -v m="$MARK" 'found { print } $0 == m { found = 1 }' "${BASH_SOURCE[0]}" > "$1" 2>/dev/null
  [ -s "$1" ]
}

choose() {
  local commit want tmp got
  commit="$(pref FLEET_BOOTSTRAP_COMMIT BootstrapCommit)"; want="$(pref FLEET_BOOTSTRAP_SHA256 BootstrapSha256)"
  if printf '%s' "$commit" | grep -Eq '^[0-9a-f]{40}$' && printf '%s' "$want" | grep -Eq '^[0-9a-f]{64}$'; then
    if [ -f "$VERIFIED" ] && [ "$(sha_of "$VERIFIED")" = "$want" ]; then
      CHOSEN="$VERIFIED"; WHY="the pinned bootstrap (${commit:0:7}, verified copy)"; return 0
    fi
    tmp="$(mktemp)" || return 1
    if curl -fsSL --max-time 60 -o "$tmp" "$BASE_URL/$BOOTSTRAP_REPO/$commit/$BOOTSTRAP_PATH" 2>/dev/null; then
      got="$(sha_of "$tmp")"
      if [ "$got" = "$want" ] && place "$tmp" "$VERIFIED"; then
        rm -f "$tmp"; CHOSEN="$VERIFIED"; WHY="the pinned bootstrap (${commit:0:7}, fetched and verified)"; return 0
      fi
      llog "REFUSED the fetched bootstrap at ${commit:0:7}: it hashes to ${got:0:12}, the profile pins ${want:0:12}"
    else
      llog "could not fetch the pinned bootstrap ${commit:0:7} from $BOOTSTRAP_REPO"
    fi
    rm -f "$tmp"
  else
    llog "no bootstrap pin in the com.memex.local profile (BootstrapCommit / BootstrapSha256)"
  fi
  tmp="$LOADER_DIR/embedded.sh"
  mkdir -p "$LOADER_DIR" 2>/dev/null
  if embedded_to "$tmp.new" && place "$tmp.new" "$tmp"; then
    rm -f "$tmp.new"; CHOSEN="$tmp"; WHY="the embedded bootstrap (sha256 $(sha_of "$tmp" | cut -c1-12))"; return 0
  fi
  rm -f "$tmp.new"
  if [ -f "$VERIFIED" ]; then CHOSEN="$VERIFIED"; WHY="the last verified bootstrap (sha256 $(sha_of "$VERIFIED" | cut -c1-12))"; return 0; fi
  return 1
}

if [ "${FLEET_LOADER_LIB:-}" != 1 ]; then
  CHOSEN=""; WHY=""
  if ! choose; then
    llog "FAILED — no pinned, embedded or verified bootstrap to run"; reading failed "no bootstrap to run"; exit 1
  fi
  llog "running $WHY"; reading ok "$WHY"
  exec /bin/bash "$CHOSEN" "$@"
fi
# 🚨 the end of the loader: nothing below this line is ever executed — the reconcile appends the embedded bootstrap here
return 0 2>/dev/null || exit 0
