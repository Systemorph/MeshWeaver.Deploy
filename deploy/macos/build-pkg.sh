#!/usr/bin/env bash
# build-pkg.sh — build the Memex macOS installer (.pkg) from this tree: the memex-local CLI and its share/
# assets exactly as the Homebrew formula installs them, the deploy/helm chart, the pinned toolchain the
# manifest names (vendored, sha256-verified), the optional LaunchDaemon, a readable Distribution — and,
# when the signing inputs are present, Developer ID signing, notarization and stapling.
# Design: deploy/macos/README.md. Tests: deploy/macos/test/run-tests.sh.
#
# Usage: build-pkg.sh --version <x.y.z> --arch <arm64|x86_64> --out <dir>
#                     [--vendor auto|none] [--cache <dir>] [--sign auto|never] [--prefix /opt/memex] [--plan]
#
#   --plan           decide and print (vendor, signing, notarization, the output file name) and exit 0
#                    without building — what the test suite uses to pin the labelling of every branch.
#   --vendor auto    (default) download every `asset` row of share/tools.manifest for --arch into --cache
#                    (default: <out>/.vendor-cache; reused when the sha256 still matches) and lay it under
#                    <prefix>/libexec/vendor/<tool>/. A sha256 mismatch is FATAL and names the tool.
#   --vendor none    no vendored toolchain — the package is LABELLED `-novendor`; for the test suite and
#                    for a developer who only wants the CLI.
#   --sign auto      (default) sign + notarize when the inputs below are present; otherwise build an
#                    UNSIGNED package, say which input is absent, label the file, and exit 0. A signing
#                    or notarization ERROR (inputs present, tool failed) exits non-zero. Never silent.
#   --sign never     an unsigned package whatever the environment holds (a bisect, the tests).
#
# Signing inputs (environment; the CI workflow derives them from the repository secrets in brackets):
#   MEMEX_SIGN_APP_IDENTITY        "Developer ID Application: …"  [APPLE_DEVELOPER_ID_APPLICATION_P12 (+_PASSWORD)]
#                                  re-signs every vendored Mach-O (hardened runtime; limactl with the
#                                  virtualization entitlement), which notarization requires
#   MEMEX_SIGN_INSTALLER_IDENTITY  "Developer ID Installer: …"    [APPLE_DEVELOPER_ID_INSTALLER_P12 (+_PASSWORD)]
#                                  signs the product archive (productbuild --sign)
#   MEMEX_NOTARY_KEY_PATH          the App Store Connect API key (.p8)  [APPLE_NOTARY_KEY_P8]
#   MEMEX_NOTARY_KEY_ID            its key id                            [APPLE_NOTARY_KEY_ID]
#   MEMEX_NOTARY_ISSUER_ID         its issuer id                         [APPLE_NOTARY_ISSUER_ID]
#
# Output: <out>/memex-local-<version>-<arch>[-novendor][-unsigned|-signed-unnotarized].pkg and its
# .sha256 beside it; the package path is the LAST line on stdout. A name without a signing suffix is a
# signed, notarized and stapled package — the suffix IS the label, so an artifact listing says it.
set -euo pipefail

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
version="" arch="" out="" vendor="auto" cache="" sign="auto" prefix="/opt/memex" plan=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="${2:?--version needs x.y.z}"; shift ;;
    --arch)    arch="${2:?--arch needs arm64|x86_64}"; shift ;;
    --out)     out="${2:?--out needs a directory}"; shift ;;
    --vendor)  vendor="${2:?--vendor needs auto|none}"; shift ;;
    --cache)   cache="${2:?--cache needs a directory}"; shift ;;
    --sign)    sign="${2:?--sign needs auto|never}"; shift ;;
    --prefix)  prefix="${2:?--prefix needs an absolute path}"; shift ;;
    --plan)    plan=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "build-pkg.sh: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
case "$version" in *[!0-9.]*|"") echo "build-pkg.sh: --version must be dotted numerics (got '${version:-<none>}')" >&2; exit 2 ;; esac
case "$arch" in arm64|x86_64) ;; *) echo "build-pkg.sh: --arch must be arm64 or x86_64 (got '${arch:-<none>}')" >&2; exit 2 ;; esac
[ -n "$out" ] || { echo "build-pkg.sh: --out is required" >&2; exit 2; }
case "$vendor" in auto|none) ;; *) echo "build-pkg.sh: --vendor must be auto or none" >&2; exit 2 ;; esac
case "$sign" in auto|never) ;; *) echo "build-pkg.sh: --sign must be auto or never" >&2; exit 2 ;; esac
case "$prefix" in /*) ;; *) echo "build-pkg.sh: --prefix must be absolute" >&2; exit 2 ;; esac
for t in pkgbuild productbuild plutil pkgutil cpio shasum curl tar; do
  command -v "$t" >/dev/null 2>&1 || { echo "build-pkg.sh: '$t' is required (macOS with the command-line tools)" >&2; exit 1; }
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
brew_dir="$repo/deploy/homebrew"
{ [ -f "$repo/deploy/helm/Chart.yaml" ] && [ -x "$brew_dir/bin/memex-local" ] && [ -f "$brew_dir/share/tools.manifest" ]; } \
  || { echo "build-pkg.sh: not a MeshWeaver.Deploy checkout: $repo" >&2; exit 1; }
mkdir -p "$out"; out="$(cd "$out" && pwd)"
cache="${cache:-$out/.vendor-cache}"; mkdir -p "$cache"
work="$(mktemp -d "${TMPDIR:-/tmp}/memex-pkg.XXXXXX")"
trap 'rm -rf "$work"' EXIT
say() { printf '==> %s\n' "$*"; }

# The manifest and the resolver are the CLI's own (share/), so the package carries what it reads.
export MEMEX_PREFIX="$prefix"
# shellcheck source=../homebrew/share/tools.sh
. "$brew_dir/share/tools.sh"


# The signing decision — explicit, never silent. Sets label_sign, do_sign, do_notarize.
label_sign=""; do_sign=0; do_notarize=0
decide_signing() {
  if [ "$sign" = "never" ]; then
    say "unsigned — --sign never"
    label_sign="-unsigned"
  else
    missing=""
    [ -n "${MEMEX_SIGN_APP_IDENTITY:-}" ]       || missing="${missing:+$missing, }MEMEX_SIGN_APP_IDENTITY (CI secret APPLE_DEVELOPER_ID_APPLICATION_P12)"
    [ -n "${MEMEX_SIGN_INSTALLER_IDENTITY:-}" ] || missing="${missing:+$missing, }MEMEX_SIGN_INSTALLER_IDENTITY (CI secret APPLE_DEVELOPER_ID_INSTALLER_P12)"
    if [ -n "$missing" ]; then
      say "unsigned — $missing absent"
      label_sign="-unsigned"
    else
      do_sign=1
      nmissing=""
      [ -n "${MEMEX_NOTARY_KEY_PATH:-}" ]  || nmissing="${nmissing:+$nmissing, }MEMEX_NOTARY_KEY_PATH (CI secret APPLE_NOTARY_KEY_P8)"
      [ -n "${MEMEX_NOTARY_KEY_ID:-}" ]    || nmissing="${nmissing:+$nmissing, }MEMEX_NOTARY_KEY_ID (CI secret APPLE_NOTARY_KEY_ID)"
      [ -n "${MEMEX_NOTARY_ISSUER_ID:-}" ] || nmissing="${nmissing:+$nmissing, }MEMEX_NOTARY_ISSUER_ID (CI secret APPLE_NOTARY_ISSUER_ID)"
      if [ -n "$nmissing" ]; then
        say "signed, NOT notarized — $nmissing absent"
        label_sign="-signed-unnotarized"
      else
        do_notarize=1
      fi
    fi
  fi
}

label_vendor=""
if [ "$plan" -eq 1 ]; then
  [ "$vendor" = "none" ] && label_vendor="-novendor"
  decide_signing
  printf 'plan: vendor=%s sign=%s notarize=%s name=%s\n' "$vendor" "$do_sign" "$do_notarize" "memex-local-${version}-${arch}${label_vendor}${label_sign}.pkg"
  exit 0
fi

# ---------------------------------------------------------------------------------------------------
# 1. The CLI component's root — 1:1 with Formula/memex-local.rb's `install`: libexec/memex-local,
#    share/* (the formula's `(libexec/"share").install Dir["deploy/homebrew/share/*"]`), share/helm
#    (the vendored chart), and a wrapper on PATH that sets MEMEX_LOCAL_SHARE + MEMEX_CHART_DIR exactly
#    as the formula's write_env_script does. The drift test (deploy/macos/test) compares the two lists.
# ---------------------------------------------------------------------------------------------------
root="$work/root"; p="$root$prefix"
say "staging the CLI under $prefix"
mkdir -p "$p/bin" "$p/libexec" "$p/share/helm" "$p/share/doc" "$root/etc/paths.d"
install -m 0755 "$brew_dir/bin/memex-local" "$p/libexec/memex-local"
cp -R "$brew_dir/share/." "$p/share/"
cp -R "$repo/deploy/helm/." "$p/share/helm/"
install -m 0755 "$here/libexec/uninstall.sh" "$p/libexec/uninstall.sh"
cat > "$p/bin/memex-local" <<WRAP
#!/bin/bash
# memex-local wrapper (the Memex .pkg) — points the CLI at the package's assets and chart, exactly as the
# Homebrew formula's wrapper does for its libexec. MEMEX_PREFIX tells the toolchain resolver where the
# vendored fallbacks live (share/tools.sh).
export MEMEX_LOCAL_SHARE="$prefix/share" MEMEX_CHART_DIR="$prefix/share/helm" MEMEX_PREFIX="$prefix"
exec "$prefix/libexec/memex-local" "\$@"
WRAP
chmod 0755 "$p/bin/memex-local"
printf '%s/bin\n' "$prefix" > "$root/etc/paths.d/memex"
chmod 0644 "$root/etc/paths.d/memex"
printf '%s\n' "$version" > "$p/share/doc/VERSION"
{ printf 'version=%s\narch=%s\nvendor=%s\nsource=%s@%s\nbuilt=%s\n' "$version" "$arch" "$vendor" \
    "${GITHUB_REPOSITORY:-Systemorph/MeshWeaver.Deploy}" "$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$p/share/doc/BUILD"

# ---------------------------------------------------------------------------------------------------
# 2. The vendored toolchain — every `asset` row for this arch, downloaded once into the cache, verified
#    against the manifest's sha256 BEFORE anything is extracted, laid out as libexec/vendor/<tool>/bin/<bin>.
# ---------------------------------------------------------------------------------------------------
fetch_verified() {   # <tool> <sha256> <url> → the cached path (or dies naming the tool)
  local tool="$1" sha="$2" url="$3" got
  local dst="$cache/$sha"
  if [ -f "$dst" ]; then
    got="$(shasum -a 256 "$dst" | awk '{print $1}')"
    [ "$got" = "$sha" ] || rm -f "$dst"
  fi
  if [ ! -f "$dst" ]; then
    curl -fsSL --retry 3 --retry-delay 2 -o "$dst.part" "$url" \
      || { echo "build-pkg.sh: $tool — cannot download $url" >&2; exit 1; }
    got="$(shasum -a 256 "$dst.part" | awk '{print $1}')"
    if [ "$got" != "$sha" ]; then
      rm -f "$dst.part"
      echo "build-pkg.sh: $tool — sha256 MISMATCH for $url" >&2
      echo "  manifest: $sha" >&2
      echo "  download: $got" >&2
      echo "  the publisher's asset changed under its pin, or the manifest's pin is wrong: do not ship it" >&2
      exit 1
    fi
    mv "$dst.part" "$dst"
  fi
  printf '%s\n' "$dst"
}
if [ "$vendor" = "auto" ]; then
  say "vendoring the toolchain for $arch ($TOOLS_MANIFEST; cache $cache)"
  for name in $(tools_list); do
    row="$(tool_asset "$name" "$arch")"
    [ -n "$row" ] || { printf '    %-8s not vendored (manifest: %s)\n' "$name" "$(tool_field "$name" 6)"; continue; }
    read -r kind member sha url <<<"$row"
    bin="$(tool_field "$name" 2)"; bin="${bin%%:*}"
    vdir="$p/libexec/vendor/$name"; mkdir -p "$vdir/bin"
    file="$(fetch_verified "$name" "$sha" "$url")"
    case "$kind" in
      binary) install -m 0755 "$file" "$vdir/bin/$bin" ;;
      tar.gz)
        if [ "$member" = "-" ]; then
          # the archive already carries bin/<bin> (lima: bin/limactl + share/lima/… — laid out as a whole)
          tar -xzf "$file" -C "$vdir"
          [ -x "$vdir/bin/$bin" ] || { echo "build-pkg.sh: $name — $url does not carry bin/$bin" >&2; exit 1; }
        else
          tar -xzf "$file" -C "$work" "$member"
          install -m 0755 "$work/$member" "$vdir/bin/$bin"
        fi ;;
      *) echo "build-pkg.sh: $name — unknown asset kind '$kind'" >&2; exit 1 ;;
    esac
    printf '%s\n' "$(tool_field "$name" 6)" > "$vdir/VERSION"
    printf '    %-8s %s  (%s)\n' "$name" "$(tool_field "$name" 6)" "$(basename "$url")"
  done
else
  say "no vendored toolchain (--vendor none) — the package is labelled -novendor"
  label_vendor="-novendor"
fi

# ---------------------------------------------------------------------------------------------------
# 3. The optional daemon component's root.
# ---------------------------------------------------------------------------------------------------
droot="$work/droot"
mkdir -p "$droot/Library/LaunchDaemons" "$droot$prefix/libexec"
install -m 0644 "$here/launchd/com.memex.local.daemon.plist" "$droot/Library/LaunchDaemons/com.memex.local.daemon.plist"
install -m 0755 "$here/libexec/memex-daemon.sh" "$droot$prefix/libexec/memex-daemon.sh"
plutil -lint "$droot/Library/LaunchDaemons/com.memex.local.daemon.plist" >/dev/null

# ---------------------------------------------------------------------------------------------------
# 4. The signing decision — explicit, never silent.
# ---------------------------------------------------------------------------------------------------
decide_signing

if [ "$do_sign" -eq 1 ]; then
  say "signing every vendored Mach-O with '$MEMEX_SIGN_APP_IDENTITY' (hardened runtime, timestamp)"
  # Notarization scans the payload: each Mach-O must carry OUR Developer ID. Lima's guest agents are
  # Linux ELF files and are left alone; limactl needs the virtualization entitlement.
  while IFS= read -r f; do
    file "$f" | grep -q 'Mach-O' || continue
    ent=()
    case "$(basename "$f")" in limactl) ent=(--entitlements "$here/entitlements/limactl.plist") ;; esac
    codesign --force --timestamp --options runtime ${ent[@]+"${ent[@]}"} --sign "$MEMEX_SIGN_APP_IDENTITY" "$f" \
      || { echo "build-pkg.sh: codesign FAILED on $f" >&2; exit 1; }
    codesign --verify --strict "$f" || { echo "build-pkg.sh: codesign verification FAILED on $f" >&2; exit 1; }
  done < <(find "$p/libexec/vendor" -type f -perm -u+x 2>/dev/null)
fi

# ---------------------------------------------------------------------------------------------------
# 5. Component packages, the Distribution, the product archive.
# ---------------------------------------------------------------------------------------------------
say "pkgbuild — com.memex.local.cli + com.memex.local.daemon $version"
pkgbuild --root "$root" --identifier com.memex.local.cli --version "$version" --install-location / \
  --scripts "$here/scripts" "$work/memex-local-cli.pkg" >/dev/null
pkgbuild --root "$droot" --identifier com.memex.local.daemon --version "$version" --install-location / \
  --scripts "$here/daemon" "$work/memex-local-daemon.pkg" >/dev/null

res="$work/resources"; mkdir -p "$res"
cp "$here/resources/welcome.html" "$here/resources/conclusion.html" "$res/"
# The licence page is the repository's own licence text (Apache-2.0 OR MIT), never a second copy.
html_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' "$1"; }
{
  printf '<!DOCTYPE html>\n<html><head><meta charset="utf-8"><style>body{font-family:-apple-system,Helvetica,sans-serif;font-size:12px;margin:12px}pre{font-family:Menlo,monospace;font-size:11px;white-space:pre-wrap}</style></head><body>\n'
  printf '<p>memex-local and this installer are licensed under <b>Apache-2.0 OR MIT</b>, at your option. The vendored tools keep their own licences: Colima (MIT), Lima (Apache-2.0), kubectl (Apache-2.0), helm (Apache-2.0), mkcert (BSD-3-Clause).</p>\n'
  printf '<pre>\n'; html_escape "$repo/LICENSE"; printf '\n\n'; html_escape "$repo/LICENSE-APACHE"; printf '\n\n'; html_escape "$repo/LICENSE-MIT"; printf '</pre>\n</body></html>\n'
} > "$res/license.html"
sed -e "s|__VERSION__|$version|g" -e "s|__HOST_ARCH__|$arch|g" "$here/Distribution.xml.in" > "$work/Distribution.xml"
if grep -q '__[A-Z_]*__' "$work/Distribution.xml"; then echo "build-pkg.sh: Distribution.xml has an unsubstituted token" >&2; exit 1; fi
command -v xmllint >/dev/null 2>&1 && xmllint --noout "$work/Distribution.xml"

name="memex-local-${version}-${arch}${label_vendor}${label_sign}.pkg"
pkg="$out/$name"
say "productbuild → $name"
sign_args=()
[ "$do_sign" -eq 1 ] && sign_args=(--sign "$MEMEX_SIGN_INSTALLER_IDENTITY" --timestamp)
productbuild --distribution "$work/Distribution.xml" --resources "$res" --package-path "$work" \
  --version "$version" ${sign_args[@]+"${sign_args[@]}"} "$pkg" >/dev/null \
  || { echo "build-pkg.sh: productbuild FAILED${sign_args[*]:+ (signing with '$MEMEX_SIGN_INSTALLER_IDENTITY')}" >&2; exit 1; }
if [ "$do_sign" -eq 1 ]; then
  pkgutil --check-signature "$pkg" >/dev/null || { echo "build-pkg.sh: the product archive's signature does not verify" >&2; exit 1; }
fi

# ---------------------------------------------------------------------------------------------------
# 6. Notarize + staple — only with every input; an error here is an error, never an unsigned fallback.
# ---------------------------------------------------------------------------------------------------
if [ "$do_notarize" -eq 1 ]; then
  say "notarizing (xcrun notarytool submit --wait)"
  log="$work/notary.json"
  if ! xcrun notarytool submit "$pkg" --key "$MEMEX_NOTARY_KEY_PATH" --key-id "$MEMEX_NOTARY_KEY_ID" \
        --issuer "$MEMEX_NOTARY_ISSUER_ID" --wait --output-format json > "$log"; then
    echo "build-pkg.sh: notarytool submit FAILED" >&2; cat "$log" >&2; exit 1
  fi
  status="$(sed -n 's/.*"status" *: *"\([^"]*\)".*/\1/p' "$log" | head -1)"
  if [ "$status" != "Accepted" ]; then
    echo "build-pkg.sh: notarization status '$status' (expected Accepted)" >&2
    id="$(sed -n 's/.*"id" *: *"\([^"]*\)".*/\1/p' "$log" | head -1)"
    [ -n "$id" ] && xcrun notarytool log "$id" --key "$MEMEX_NOTARY_KEY_PATH" --key-id "$MEMEX_NOTARY_KEY_ID" --issuer "$MEMEX_NOTARY_ISSUER_ID" >&2 || true
    exit 1
  fi
  xcrun stapler staple "$pkg" >/dev/null || { echo "build-pkg.sh: stapler FAILED" >&2; exit 1; }
  spctl -a -vv -t install "$pkg" >/dev/null 2>&1 || { echo "build-pkg.sh: Gatekeeper (spctl -t install) rejects the stapled package" >&2; exit 1; }
  say "notarized and stapled"
fi

# ---------------------------------------------------------------------------------------------------
# 7. Prove the archive carries what it must — never "productbuild exited 0".
# ---------------------------------------------------------------------------------------------------
exp="$work/expanded"
pkgutil --expand-full "$pkg" "$exp" >/dev/null 2>&1 || { echo "build-pkg.sh: pkgutil --expand-full failed on $pkg" >&2; exit 1; }
for f in "memex-local-cli.pkg/Payload$prefix/libexec/memex-local" "memex-local-cli.pkg/Payload$prefix/bin/memex-local" \
         "memex-local-cli.pkg/Payload$prefix/share/tools.manifest" "memex-local-cli.pkg/Payload$prefix/share/tools.sh" \
         "memex-local-cli.pkg/Payload$prefix/share/helm/Chart.yaml" "memex-local-cli.pkg/Payload/etc/paths.d/memex" \
         "memex-local-cli.pkg/Scripts/postinstall" "memex-local-daemon.pkg/Payload/Library/LaunchDaemons/com.memex.local.daemon.plist" \
         "memex-local-daemon.pkg/Scripts/postinstall" "Distribution"; do
  [ -e "$exp/$f" ] || { echo "build-pkg.sh: the built package lacks $f" >&2; exit 1; }
done
if [ "$vendor" = "auto" ]; then
  for name in $(tools_list); do
    [ -n "$(tool_asset "$name" "$arch")" ] || continue
    bin="$(tool_field "$name" 2)"; bin="${bin%%:*}"
    [ -x "$exp/memex-local-cli.pkg/Payload$prefix/libexec/vendor/$name/bin/$bin" ] \
      || { echo "build-pkg.sh: the built package lacks the vendored $name (libexec/vendor/$name/bin/$bin)" >&2; exit 1; }
  done
fi
shasum -a 256 "$pkg" | awk '{print $1}' > "$pkg.sha256"
say "built $(du -h "$pkg" | awk '{print $1}')  sha256 $(cat "$pkg.sha256")"
printf '%s\n' "$pkg"
