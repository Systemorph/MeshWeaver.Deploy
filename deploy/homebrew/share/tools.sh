#!/usr/bin/env bash
# tools.sh — ONE resolver for the toolchain memex-local runs on (colima, lima, kubectl, helm, mkcert, …).
#
# Sourced, never executed: by deploy/homebrew/bin/memex-local (preflight, `need`, `doctor`) and by the
# .pkg's postinstall (deploy/macos/scripts/postinstall). Both read deploy/homebrew/share/tools.manifest,
# so the floors, the pins and the probe commands exist in exactly one place.
#
# THE PRECEDENCE LIST (maintainer, 2026-10-26: "it will see what's there and install whatever is
# missing"): for each tool,
#   1. an EXISTING installation on PATH — the developer's own copy (Homebrew, a hand install) — is used
#      when it meets the manifest's floor; it is never overwritten and never shadowed;
#   2. the VENDORED copy the .pkg laid down under <prefix>/libexec/vendor/<tool>/ is used only when
#      nothing existing satisfies the floor (missing, or present but too old);
#   3. otherwise the tool is MISSING, and the caller says so with the floor and how to get it.
# A copy the .pkg itself activated (<prefix>/bin/<tool>, a symlink to the vendored binary) is never
# counted as "existing" — or the fallback would masquerade as the user's installation on the next probe.
#
# Every function is bash 3.2 clean (macOS's /bin/bash: no associative arrays, no ${var,,}).
#
# Inputs (all optional):
#   MEMEX_TOOLS_MANIFEST  the manifest to read (default: tools.manifest beside this file)
#   MEMEX_PREFIX          the .pkg prefix (default: /opt/memex); <prefix>/libexec/vendor and <prefix>/bin
#                         are the paths excluded from "existing" and searched for the fallback
#   MEMEX_VENDOR_DIR      where vendored copies live (default: $MEMEX_PREFIX/libexec/vendor)

_tools_here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_MANIFEST="${MEMEX_TOOLS_MANIFEST:-$_tools_here/tools.manifest}"
TOOLS_PREFIX="${MEMEX_PREFIX:-/opt/memex}"
TOOLS_VENDOR_DIR="${MEMEX_VENDOR_DIR:-$TOOLS_PREFIX/libexec/vendor}"
TOOLS_PREFIX_BIN="$TOOLS_PREFIX/bin"

# The names of every `tool` row, in manifest order.
tools_list() { awk '$1 == "tool" { print $2 }' "$TOOLS_MANIFEST"; }

# One field of a tool row: 1 name, 2 binary, 3 brew formula, 4 brew-dep, 5 floor, 6 vendored version.
tool_field() {
  awk -v n="$1" -v f="$2" '$1 == "tool" && $2 == n { print $(f + 1); exit }' "$TOOLS_MANIFEST"
}
# The probe arguments (fields 8..NF) of a tool row; `-` means presence only.
tool_probe() {
  awk -v n="$1" '$1 == "tool" && $2 == n { s = ""; for (i = 8; i <= NF; i++) s = s (i > 8 ? " " : "") $i; print s; exit }' "$TOOLS_MANIFEST"
}
# Is <name> a manifest tool? Exit status only.
tool_known() { [ -n "$(tool_field "$1" 1)" ]; }

# The asset row for <name> on <arch>: "kind member sha256 url", or nothing.
tool_asset() {
  awk -v n="$1" -v a="$2" '$1 == "asset" && $2 == n && $3 == a { print $4, $5, $6, $7; exit }' "$TOOLS_MANIFEST"
}

# version_ge A B — is version A at least version B? Numeric per component (up to four), a leading `v`
# and any pre-release / build suffix ignored. `-` as B (no floor) is always satisfied.
version_ge() {
  local a="$1" b="$2" i x y
  [ "$b" = "-" ] && return 0
  a="${a#v}"; a="${a%%[-+]*}"; b="${b#v}"; b="${b%%[-+]*}"
  for i in 1 2 3 4; do
    x="$(printf '%s' "$a" | cut -d. -f"$i")"; y="$(printf '%s' "$b" | cut -d. -f"$i")"
    case "$x" in ''|*[!0-9]*) x=0 ;; esac
    case "$y" in ''|*[!0-9]*) y=0 ;; esac
    if [ "$x" -gt "$y" ]; then return 0; fi
    if [ "$x" -lt "$y" ]; then return 1; fi
  done
  return 0
}

# The version an executable reports: the first X.Y.Z in the output of `<path> <probe args>`. A tool
# with the probe `-` reports `present`. Prints nothing (and fails) when the binary gives no version.
tool_version_of() {
  local path="$1" probe="$2" out
  if [ "$probe" = "-" ]; then printf 'present\n'; return 0; fi
  # shellcheck disable=SC2086  # the probe is a deliberate word-split of the manifest's arguments
  out="$("$path" $probe 2>/dev/null </dev/null | head -5)" || true
  out="$(printf '%s\n' "$out" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  [ -n "$out" ] || return 1
  printf '%s\n' "$out"
}

# The first EXISTING <binary> that is neither the .pkg's fallback link nor its vendored copy. The
# manifest's binary column may hold several candidates separated by `:` — a bare name is searched on
# PATH, an absolute path is tested as is (socket_vmnet is keg-only in Homebrew: never on PATH, always
# at <brew prefix>/opt/socket_vmnet/bin/socket_vmnet, which is where colima looks for it too).
tool_existing() {
  local cands="$1" bin dir rest
  while [ -n "$cands" ]; do
    bin="${cands%%:*}"
    if [ "$cands" = "$bin" ]; then cands=""; else cands="${cands#*:}"; fi
    case "$bin" in
      /*) if [ -x "$bin" ] && [ ! -d "$bin" ]; then printf '%s\n' "$bin"; return 0; fi; continue ;;
    esac
    _tool_existing_on_path "$bin" && return 0
  done
  return 1
}
_tool_existing_on_path() {
  local bin="$1" dir rest="$PATH"
  while [ -n "$rest" ]; do
    dir="${rest%%:*}"
    if [ "$rest" = "$dir" ]; then rest=""; else rest="${rest#*:}"; fi
    [ -n "$dir" ] || continue
    case "$dir" in "$TOOLS_PREFIX_BIN"|"$TOOLS_VENDOR_DIR"|"$TOOLS_VENDOR_DIR"/*) continue ;; esac
    if [ -x "$dir/$bin" ] && [ ! -d "$dir/$bin" ]; then printf '%s\n' "$dir/$bin"; return 0; fi
  done
  return 1
}

# The vendored copy of <name>, if the .pkg laid one down (always bin/<first binary name>).
tool_vendored() {
  local p="$TOOLS_VENDOR_DIR/$1/bin/${2%%:*}"
  [ -x "$p" ] && printf '%s\n' "$p"
}

# The manifest tool whose binary is <bin> (so `need kubectl …` finds the `kubectl` row), or nothing.
tool_name_for_binary() {
  awk -v b="$1" '$1 == "tool" { n = split($3, c, ":"); for (i = 1; i <= n; i++) if (c[i] == b) { print $2; exit } }' "$TOOLS_MANIFEST"
}

# tool_resolve <name> — ONE line, tab-separated:
#   name  kind  path  version  floor  verdict  note
#   kind     existing | vendored | missing
#   verdict  ok | below-floor | no-version | missing
#   note     why the existing copy was passed over, or what to install
# The exit status is 0 for a usable resolution (existing or vendored at/above the floor), 1 otherwise.
tool_resolve() {
  local name="$1" bin floor probe existing vendored ever vver note="" kind path version verdict
  bin="$(tool_field "$name" 2)"; floor="$(tool_field "$name" 5)"; probe="$(tool_probe "$name")"
  [ -n "$bin" ] || { printf '%s\tmissing\t-\t-\t-\tmissing\tnot a manifest tool\n' "$name"; return 1; }
  if existing="$(tool_existing "$bin")"; then
    if ever="$(tool_version_of "$existing" "$probe")"; then
      if version_ge "$ever" "$floor"; then
        printf '%s\texisting\t%s\t%s\t%s\tok\t\n' "$name" "$existing" "$ever" "$floor"; return 0
      fi
      note="existing $existing is $ever, below the floor $floor"
    else
      note="existing $existing reports no version"
    fi
  fi
  if vendored="$(tool_vendored "$name" "$bin")"; then
    # A vendored copy must PROVE its version like any other: a binary that prints none is damaged or
    # incompatible, and substituting the manifest's pin would activate it as a working fallback.
    if vver="$(tool_version_of "$vendored" "$probe")"; then
      if version_ge "$vver" "$floor"; then
        printf '%s\tvendored\t%s\t%s\t%s\tok\t%s\n' "$name" "$vendored" "$vver" "$floor" "${note:-nothing existing}"; return 0
      fi
      note="${note:+$note; }vendored $vendored is $vver, below the floor $floor"
    else
      note="${note:+$note; }vendored $vendored reports no version (damaged or incompatible — not activated)"
    fi
  fi
  if [ -n "$existing" ]; then
    kind="existing"; path="$existing"; version="${ever:-?}"
    case "$note" in *"reports no version"*) verdict="no-version" ;; *) verdict="below-floor" ;; esac
  else
    kind="missing"; path="-"; version="-"; verdict="missing"
    note="${note:-not on PATH and not vendored — brew install $(tool_field "$name" 3)}"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$kind" "$path" "$version" "$floor" "$verdict" "$note"
  return 1
}

# tools_report — the per-tool table `memex-local doctor` and the postinstall print. Exit status: the
# number of tools without a usable resolution is reported through TOOLS_UNRESOLVED; status is always 0.
TOOLS_UNRESOLVED=0
tools_report() {
  local name line kind path version floor verdict note
  TOOLS_UNRESOLVED=0
  printf '  %-13s %-9s %-9s %-8s %-11s %s\n' TOOL USES VERSION FLOOR VERDICT FROM
  for name in $(tools_list); do
    line="$(tool_resolve "$name")" || TOOLS_UNRESOLVED=$((TOOLS_UNRESOLVED + 1))
    IFS=$'\t' read -r name kind path version floor verdict note <<<"$line"
    printf '  %-13s %-9s %-9s %-8s %-11s %s%s\n' "$name" "$kind" "$version" "$floor" "$verdict" "$path" "${note:+  ($note)}"
  done
}

# tools_activate — make the resolution EFFECTIVE for this process and everything it runs: a selection
# directory holding one symlink per resolved tool (to the existing copy or to the vendored one) is
# prepended to PATH, so `colima`, `limactl`, `kubectl`, `helm`, `mkcert` all run exactly the copy the
# resolver chose — whatever else PATH holds (a below-floor Homebrew copy ahead of the fallback, a stale
# fallback link in <prefix>/bin after a Homebrew copy appeared). A tool that does not resolve gets no
# link, so `need` and `command -v` see it as missing. The directory is per process (MEMEX_TOOLS_SELECTED;
# the caller removes it on exit) and nothing on disk outside it is touched.
tools_activate() {
  local name line kind path sel
  sel="${MEMEX_TOOLS_SELECTED:-}"
  if [ -z "$sel" ] || [ ! -d "$sel" ]; then
    sel="$(mktemp -d "${TMPDIR:-/tmp}/memex-tools.XXXXXX")" || return 1
    export MEMEX_TOOLS_SELECTED="$sel"
  fi
  for name in $(tools_list); do
    line="$(tool_resolve "$name")" || { rm -f "$sel/$(tool_field "$name" 2 | cut -d: -f1)"; continue; }
    IFS=$'\t' read -r name kind path _ <<<"$line"
    [ "$kind" = "existing" ] || [ "$kind" = "vendored" ] || continue
    ln -sfn "$path" "$sel/$(basename "$path")"
  done
  case ":$PATH:" in *":$sel:"*) ;; *) export PATH="$sel:$PATH" ;; esac
  return 0
}

# tools_deactivate — remove the selection directory (the CLI's exit trap).
tools_deactivate() {
  [ -n "${MEMEX_TOOLS_SELECTED:-}" ] && [ -d "${MEMEX_TOOLS_SELECTED}" ] && rm -rf "${MEMEX_TOOLS_SELECTED}"
  return 0
}
