#!/bin/bash
# uninstall.sh — remove what the Memex .pkg installed (deploy/macos/README.md §1a "Uninstall"). Root.
#   sudo /opt/memex/libexec/uninstall.sh [--purge]
# Removes /opt/memex, /etc/paths.d/memex, the optional LaunchDaemon and the installer receipts. It does
# NOT touch the user's ~/.memex-local, the Colima VM, the per-user LaunchAgents or anything Homebrew
# installed — `memex-local down [--purge]` is the stack's own teardown and runs as the user, before this.
# --purge additionally removes /var/log/memex.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 1; }
prefix="${MEMEX_PKG_PREFIX:-/opt/memex}"
daemon="/Library/LaunchDaemons/com.memex.local.daemon.plist"
if [ -f "$daemon" ]; then
  launchctl bootout system "$daemon" >/dev/null 2>&1 || true
  rm -f "$daemon"; echo "removed $daemon"
fi
rm -rf "$prefix"; echo "removed $prefix"
rm -f /etc/paths.d/memex; echo "removed /etc/paths.d/memex"
for id in com.memex.local.cli com.memex.local.daemon; do pkgutil --forget "$id" >/dev/null 2>&1 && echo "forgot receipt $id" || true; done
[ "${1:-}" = "--purge" ] && { rm -rf /var/log/memex; echo "removed /var/log/memex"; }
echo "memex-local (.pkg) uninstalled. Your ~/.memex-local, the Colima VM and Homebrew are untouched."
