#!/usr/bin/env bash
# The runner image's Ubuntu package sources, as MIRROR LISTS — never one host (MeshWeaver.Deploy#235).
#
# WHY. The base image (`mcr.microsoft.com/dotnet/sdk:10.0`, Ubuntu noble) names ONE host per source:
# `archive.ubuntu.com` + `security.ubuntu.com` on amd64, `ports.ubuntu.com` on arm64. When that one host
# stalls, every `apt-get` in the build stalls with it: on 2026-11-10 five consecutive amd64 builds sat
# for the whole 30-minute job cap in ~30 s connect timeouts against `archive.ubuntu.com`, while the arm64
# leg (another host) passed in the same runs.
#
# WHAT. `apply` rewrites every `URIs:` line of the base image's deb822 Ubuntu sources to apt's own mirror
# method (`mirror+file:`, apt-transport-mirror(1), in apt since 1.6): a file listing several mirrors,
# each with a `priority`. apt asks the lowest priority first and, when a file fails there, fetches that
# file from the next mirror — integrity is unchanged, because every index is still verified against
# the source's `Signed-By` keyring, whichever mirror served it. The FIRST mirror of the amd64 lists is
# the cloud's regional Ubuntu mirror (`azure.archive.ubuntu.com`, the one GitHub's own Ubuntu runner VMs
# and the operator's ACR Tasks — both in that cloud — are configured with), so the ordinary path no
# longer touches Canonical's archive at all; Canonical's host and an independent mirror follow it.
# `check` then asserts what the build relies on: no Ubuntu source in the image names a single host, and
# every mirror list it names offers at least two. A base image whose sources move to a shape this
# script does not know is REFUSED, naming the line, never left on one host.
#
# What a mirror list cannot do: a mirror that BLACKHOLES (connects never answer) still costs its connect
# timeout on each file before the next one is asked — which is why the first entry is the mirror inside
# the runners' own network, and why a mirror that refuses or 404s is passed over at once.
#
# Usage:  apt-mirrors.sh apply [ROOT]    rewrite ROOT/etc/apt (default /), then `check`
#         apt-mirrors.sh check [ROOT]    exit 1 naming every Ubuntu source that has one host
#         apt-mirrors.sh --self-test
set -euo pipefail

# One list per base-image source. Each entry: URI <TAB> priority:N (lowest first).
mirror_list() {
  case "$1" in
    ubuntu-archive) printf '%s\tpriority:%s\n' \
        http://azure.archive.ubuntu.com/ubuntu/ 1 \
        http://archive.ubuntu.com/ubuntu/ 2 \
        http://mirrors.edge.kernel.org/ubuntu/ 3 ;;
    ubuntu-security) printf '%s\tpriority:%s\n' \
        http://azure.archive.ubuntu.com/ubuntu/ 1 \
        http://security.ubuntu.com/ubuntu/ 2 \
        http://mirrors.edge.kernel.org/ubuntu/ 3 ;;
    ubuntu-ports) printf '%s\tpriority:%s\n' \
        http://ports.ubuntu.com/ubuntu-ports/ 1 \
        http://mirrors.mit.edu/ubuntu-ports/ 2 \
        http://mirrors.ocf.berkeley.edu/ubuntu-ports/ 3 ;;
    *) return 1 ;;
  esac
}

# The base image's single-host URI → the list that replaces it.
list_for_uri() {
  case "${1%/}" in
    http://archive.ubuntu.com/ubuntu)       echo ubuntu-archive ;;
    http://security.ubuntu.com/ubuntu)      echo ubuntu-security ;;
    http://ports.ubuntu.com/ubuntu-ports)   echo ubuntu-ports ;;
    *) return 1 ;;
  esac
}

# Every apt source file under ROOT (both formats; a missing directory is no file).
source_files() {
  local root="$1" f
  for f in "$root/etc/apt/sources.list" "$root"/etc/apt/sources.list.d/*.list "$root"/etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] && printf '%s\n' "$f"
  done
  return 0
}

apply() {
  local root="${1:-}" f line uri name out rewrote=0
  mkdir -p "$root/etc/apt/mirrors"
  while IFS= read -r f; do
    case "$f" in
      *.sources) ;;
      *)
        # One-line format: the base image ships none; one naming Ubuntu is a shape this script does not rewrite.
        if grep -Eq '^[[:space:]]*deb(-src)?[[:space:]].*ubuntu\.com' "$f"; then
          echo "apt-mirrors: $f names an Ubuntu host in the one-line format — not rewritten; convert it to deb822" >&2
          return 1
        fi
        continue ;;
    esac
    out="$(mktemp)"
    while IFS= read -r line || [ -n "$line" ]; do
      if [[ "$line" =~ ^URIs:[[:space:]]*(.*)$ ]]; then
        uri="${BASH_REMATCH[1]}"
        uri="${uri%"${uri##*[![:space:]]}"}"
        if [[ "$uri" == mirror+file:* ]]; then
          printf '%s\n' "$line" >> "$out"; continue
        fi
        if [[ "$uri" == *ubuntu.com* ]]; then
          if [[ "$uri" == *" "* ]] || ! name="$(list_for_uri "$uri")"; then
            rm -f "$out"
            echo "apt-mirrors: $f: 'URIs: $uri' is an Ubuntu source this script has no mirror list for — add one to mirror_list" >&2
            return 1
          fi
          mirror_list "$name" > "$root/etc/apt/mirrors/$name.list"
          printf 'URIs: mirror+file:/etc/apt/mirrors/%s.list\n' "$name" >> "$out"
          rewrote=$((rewrote + 1))
          continue
        fi
      fi
      printf '%s\n' "$line" >> "$out"
    done < "$f"
    cat "$out" > "$f"
    rm -f "$out"
  done < <(source_files "$root")
  echo "apt-mirrors: $rewrote Ubuntu source(s) rewritten to mirror lists under ${root:-}/etc/apt/mirrors"
  check "$root"
}

check() {
  local root="${1:-}" f bad=0 n uri path listed
  while IFS= read -r f; do
    while IFS= read -r uri; do
      case "$uri" in
        mirror+file:*)
          path="$root${uri#mirror+file:}"
          if [ ! -f "$path" ]; then echo "apt-mirrors: $f names the mirror list $uri, which does not exist" >&2; bad=1; continue; fi
          listed="$(grep -Ev '^[[:space:]]*(#|$)' "$path" | cut -f1 | sort -u | wc -l | tr -d ' ')"
          if [ "$listed" -lt 2 ]; then echo "apt-mirrors: $f: the mirror list $uri offers $listed mirror(s) — a single host again" >&2; bad=1; fi ;;
        *ubuntu.com*)
          echo "apt-mirrors: $f: Ubuntu source '$uri' names ONE host — the build stalls whenever it does (MeshWeaver.Deploy#235)" >&2; bad=1 ;;
      esac
    done < <(
      case "$f" in
        *.sources) sed -n 's/^URIs:[[:space:]]*//p' "$f" | tr ' ' '\n' | grep -v '^$' || true ;;
        *) sed -n 's/^[[:space:]]*deb\(-src\)\{0,1\}[[:space:]]\{1,\}\(\[[^]]*\][[:space:]]\{1,\}\)\{0,1\}\([^[:space:]]*\).*/\3/p' "$f" ;;
      esac)
  done < <(source_files "$root")
  n="$(source_files "$root" | wc -l | tr -d ' ')"
  if [ "$n" -eq 0 ]; then echo "apt-mirrors: no apt source file under ${root:-}/etc/apt — nothing was checked" >&2; return 1; fi
  [ "$bad" -eq 0 ] || return 1
  echo "apt-mirrors: every Ubuntu source in $n source file(s) is a mirror list of at least two mirrors"
}

# The base image's own files, verbatim in the lines that matter (amd64 and arm64).
fixture() {
  local root="$1" arch="$2" main sec
  mkdir -p "$root/etc/apt/sources.list.d"
  if [ "$arch" = amd64 ]; then main=http://archive.ubuntu.com/ubuntu/; sec=http://security.ubuntu.com/ubuntu/
  else main=http://ports.ubuntu.com/ubuntu-ports/; sec=http://ports.ubuntu.com/ubuntu-ports/; fi
  printf '# Ubuntu sources have moved to /etc/apt/sources.list.d/ubuntu.sources\n' > "$root/etc/apt/sources.list"
  cat > "$root/etc/apt/sources.list.d/ubuntu.sources" <<EOF
## URIs: A URL to the repository (you may add multiple URLs)
Types: deb
URIs: $main
Suites: noble noble-updates noble-backports
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

## Ubuntu security updates. Aside from URIs and Suites,
Types: deb
URIs: $sec
Suites: noble-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
}

self_test() {
  local t fails=0 arch
  t="$(mktemp -d)"
  trap 'rm -rf "$t"' RETURN
  expect() { # expect <0|1> <label> <command...>
    local want="$1" label="$2"; shift 2
    if "$@" > "$t/out" 2>&1; then got=0; else got=1; fi
    if [ "$got" = "$want" ]; then echo "  ok   $label"; else echo "  FAIL $label (exit $got, wanted $want):"; sed 's/^/       /' "$t/out"; fails=$((fails + 1)); fi
  }
  echo "apt-mirrors self-test"
  for arch in amd64 arm64; do
    fixture "$t/$arch-raw" "$arch"
    # The negative control: the base image AS SHIPPED is what #235 stalled on, and `check` must say so.
    expect 1 "$arch: the base image's own sources (one host each) are refused" check "$t/$arch-raw"
    fixture "$t/$arch" "$arch"
    expect 0 "$arch: apply rewrites them and check passes" apply "$t/$arch"
    expect 1 "$arch: no Ubuntu URI is left as a single host" grep -Eq '^URIs: http' "$t/$arch/etc/apt/sources.list.d/ubuntu.sources"
    expect 0 "$arch: Signed-By is kept (every mirror's index is still verified)" test "$(grep -c '^Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg$' "$t/$arch/etc/apt/sources.list.d/ubuntu.sources")" = 2
    expect 0 "$arch: apply is idempotent" apply "$t/$arch"
  done
  expect 0 "amd64: the archive list asks the cloud-regional mirror first" grep -q "^http://azure.archive.ubuntu.com/ubuntu/	priority:1$" "$t/amd64/etc/apt/mirrors/ubuntu-archive.list"
  expect 0 "amd64: the security list keeps Canonical's security host" grep -q "^http://security.ubuntu.com/ubuntu/	priority:2$" "$t/amd64/etc/apt/mirrors/ubuntu-security.list"
  expect 0 "arm64: the ports list keeps ports.ubuntu.com first" grep -q "^http://ports.ubuntu.com/ubuntu-ports/	priority:1$" "$t/arm64/etc/apt/mirrors/ubuntu-ports.list"
  # A list cut back to one mirror is a single host again.
  fixture "$t/one" amd64; apply "$t/one" > /dev/null
  printf 'http://archive.ubuntu.com/ubuntu/\tpriority:1\n' > "$t/one/etc/apt/mirrors/ubuntu-archive.list"
  expect 1 "a mirror list of one mirror is refused" check "$t/one"
  # An Ubuntu host the script has no list for is refused, never left alone.
  fixture "$t/unknown" amd64
  sed 's#^URIs: http://security.ubuntu.com/ubuntu/$#URIs: http://esm.ubuntu.com/apps/ubuntu/#' "$t/unknown/etc/apt/sources.list.d/ubuntu.sources" > "$t/u" && cat "$t/u" > "$t/unknown/etc/apt/sources.list.d/ubuntu.sources"
  expect 1 "an Ubuntu host with no mirror list is refused by apply" apply "$t/unknown"
  # The one-line format naming Ubuntu is refused by both.
  fixture "$t/legacy" amd64
  printf 'deb [arch=amd64] http://archive.ubuntu.com/ubuntu noble main\n' > "$t/legacy/etc/apt/sources.list"
  expect 1 "a one-line Ubuntu source is refused by check" check "$t/legacy"
  expect 1 "a one-line Ubuntu source is refused by apply" apply "$t/legacy"
  # Sources that are not Ubuntu's (the PostgreSQL project's, NodeSource) are not this script's subject.
  fixture "$t/other" amd64; apply "$t/other" > /dev/null
  printf 'deb [signed-by=/x.gpg] https://apt.postgresql.org/pub/repos/apt noble-pgdg main\n' > "$t/other/etc/apt/sources.list.d/pgdg.list"
  expect 0 "a non-Ubuntu source does not fail check" check "$t/other"
  # Nothing to read is not a pass.
  mkdir -p "$t/empty/etc/apt"
  expect 1 "a tree with no apt source file is refused (nothing was checked)" check "$t/empty"
  if [ "$fails" -ne 0 ]; then echo "apt-mirrors self-test: $fails case(s) FAILED"; return 1; fi
  echo "apt-mirrors self-test: all cases passed"
}

case "${1:-}" in
  apply) apply "${2:-}" ;;
  check) check "${2:-}" ;;
  --self-test) self_test ;;
  *) echo "usage: $0 apply [ROOT] | check [ROOT] | --self-test" >&2; exit 2 ;;
esac
