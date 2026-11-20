#!/bin/bash
# TeamViewer Host — post-install script of an Intune macOS app (PKG) (`apps[].postInstallScript` in the fleet spec).
#
# A TEMPLATE: intune-reconcile.py renders it per client from the spec and uploads the result as the app's
# postInstallScript. Placeholders (never edit them into values here — this file is public and client-neutral):
#   ASSIGNMENT_ID   the client's TeamViewer assignment id (Management Console → Design & Deploy). A SECRET: the spec
#                   names only the environment variable that carries it at --apply; --check compares the live script
#                   with this line put back to its placeholder, so the drift check never needs it.
#   ALIAS_PREFIX    the client's prefix for the device alias (e.g. "PG3-").
#   DEVICE_ALIASES  one "<serial> <name>" line per Mac of the client's group, from the spec: each Mac finds its own
#                   alias by its serial number; a Mac not listed falls back to its LocalHostName.
#
# Runs as root after the package installed (Intune management agent 2309.007+). Intune does not report a failed
# post-install script (Microsoft Learn, "Add an unmanaged macOS PKG app"), so every outcome is also written to
# /Library/Logs/MeshWeaver/teamviewer-assignment.log and the script exits 0 only when the assignment succeeded.
#
# The command: TeamViewer, "Assign a device via command line" (Corporate / Tensor licence, TeamViewer 15+):
#   /Applications/TeamViewerHost.app/Contents/Helpers/TeamViewer_Assignment -assignment_id <id> [-device_alias <name>]
# Docs: MeshWeaver.Deploy deploy/macos/intune/README.md → "Apps: TeamViewer Host per client".
set -u

ASSIGNMENT_ID='@@ASSIGNMENT_ID@@'
ALIAS_PREFIX='@@ALIAS_PREFIX@@'
LOG=/Library/Logs/MeshWeaver/teamviewer-assignment.log
HELPER=/Applications/TeamViewerHost.app/Contents/Helpers/TeamViewer_Assignment

mkdir -p "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

case "$ASSIGNMENT_ID" in
  ''|@@*) log "REFUSED: no assignment id rendered into this script (the reconcile writes it from the environment)"; exit 1 ;;
esac
if [ ! -x "$HELPER" ]; then
  log "FAILED: $HELPER not found — is this the client's TeamViewer HOST package?"
  exit 1
fi

serial=$(/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice | /usr/bin/awk -F'"' '/IOPlatformSerialNumber/ {print $4}')
name=""
while read -r s n; do
  [ -n "${s:-}" ] && [ "$s" = "$serial" ] && name=$n
done <<'ALIASES'
@@DEVICE_ALIASES@@
ALIASES
[ -n "$name" ] || name=$(/usr/sbin/scutil --get LocalHostName 2>/dev/null || hostname -s)
alias="${ALIAS_PREFIX}${name}"

# --reassign: a Mac already assigned (a reinstall, or a Mac that served another client before) is moved to THIS
# client's account rather than failing
for attempt in 1 2 3; do
  if "$HELPER" -assignment_id "$ASSIGNMENT_ID" -device_alias "$alias" --reassign >>"$LOG" 2>&1; then
    log "OK: assigned as \"$alias\" (serial $serial, attempt $attempt)"
    exit 0
  fi
  log "attempt $attempt failed; retrying in 30 s"
  sleep 30
done
log "FAILED: TeamViewer_Assignment did not succeed for \"$alias\" after 3 attempts"
exit 1
