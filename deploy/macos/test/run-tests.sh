#!/usr/bin/env bash
# Tests for the Memex macOS installer (deploy/macos) — the package build, its payload, its scripts.
#
# Same shape as deploy/homebrew/test/run-tests.sh: one process, `ok`/`bad` per assertion, a count at the
# end, exit 1 on any failure. This suite needs macOS (pkgbuild, productbuild, pkgutil, plutil); it refuses
# anywhere else rather than printing a green it did not measure.
#
# WHAT IT PROVES, and what it cannot. It proves that what build-pkg.sh produces carries what the Homebrew
# formula installs (the drift test), that every plist and the Distribution parse, that the postinstall's
# probe-first activation does exactly what deploy/macos/README.md §2 says in each of the four states the
# maintainer named (all present and satisfying → nothing activated; one missing → only that one; one
# below the floor → the vendored one, and the report says why; a re-run → unchanged), that the signing
# decision is explicit in every branch, that a sha256 mismatch on a vendored asset is fatal and names
# the tool, and that `pkgutil --expand` of a built package contains the expected files. It does NOT
# install anything on this Mac (the CI job does, on a runner) and it never downloads a real tool: the
# vendoring path is exercised against file:// fixtures with their own manifest.
set -uo pipefail
case "$(uname -s)" in Darwin) ;; *) echo "this suite needs macOS (pkgbuild/productbuild/pkgutil/plutil); refusing on $(uname -s)" >&2; exit 1 ;; esac
for t in pkgbuild productbuild pkgutil plutil xmllint cpio shasum; do
  command -v "$t" >/dev/null 2>&1 || { echo "missing tool: $t" >&2; exit 1; }
done

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MACOS="$HERE/.."
REPO="$(cd "$MACOS/../.." && pwd)"
BREW="$REPO/deploy/homebrew"
BUILD="$MACOS/build-pkg.sh"
pass=0; fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s\n' "$1"; printf '        %s\n' "${2:-}"; fail=$((fail+1)); }
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
# The build must never pick up a developer's own signing identities: every build here says never, or
# sets the identities on purpose.
unset MEMEX_SIGN_APP_IDENTITY MEMEX_SIGN_INSTALLER_IDENTITY MEMEX_NOTARY_KEY_PATH MEMEX_NOTARY_KEY_ID MEMEX_NOTARY_ISSUER_ID

echo "── every plist and the Distribution parse ───────────────────────"
for f in "$BREW"/share/*.plist "$MACOS"/launchd/*.plist "$MACOS"/entitlements/*.plist "$MACOS"/profiles/*.mobileconfig.example; do
  if plutil -lint "$f" >/dev/null 2>&1; then ok "plutil -lint $(basename "$f")"; else bad "plutil -lint $(basename "$f")" "$(plutil -lint "$f" 2>&1)"; fi
done
if xmllint --noout "$MACOS/Distribution.xml.in" 2>/dev/null; then ok "Distribution.xml.in is well-formed XML"; else bad "Distribution.xml.in is well-formed XML" "$(xmllint --noout "$MACOS/Distribution.xml.in" 2>&1)"; fi
# plutil tolerates `--` inside a comment; a strict XML parser (an MDM importer) does not — xmllint is strict.
for f in "$BREW"/share/*.plist "$MACOS"/launchd/*.plist "$MACOS"/entitlements/*.plist "$MACOS"/profiles/*.mobileconfig.example; do
  if xmllint --noout --nonet "$f" 2>/dev/null; then ok "strict XML: $(basename "$f")"; else bad "strict XML: $(basename "$f")" "$(xmllint --noout --nonet "$f" 2>&1 | head -3)"; fi
done
grep -q 'pair incomplete — replacing BOTH from this run' "$REPO/.github/workflows/macos-pkg.yml" && grep -q 'gh release upload "$tag" "$pkg" "$pkg.sha256" --clobber' "$REPO/.github/workflows/macos-pkg.yml" && ok "the release step reconciles each .pkg/.sha256 PAIR from the same bytes (either missing → both replaced)" || bad "release pair reconciliation" ""
if grep -q 'if: ${{ always() }}' "$REPO/.github/workflows/macos-pkg.yml" && grep -q 'rm -f "$RUNNER_TEMP/notary.p8" "$RUNNER_TEMP/app.p12" "$RUNNER_TEMP/inst.p12"' "$REPO/.github/workflows/macos-pkg.yml"; then ok "the workflow removes the keychain, the notary key and both decoded .p12 files under always()"; else bad "signing cleanup always()" ""; fi
for tok in __VERSION__ __HOST_ARCH__; do
  if grep -q "$tok" "$MACOS/Distribution.xml.in"; then ok "Distribution.xml.in carries $tok for build-pkg.sh to substitute"; else bad "Distribution.xml.in carries $tok" "the build would render a fixed value"; fi
done
for f in welcome.html conclusion.html; do
  if grep -q '/opt/memex' "$MACOS/resources/$f"; then ok "$f tells the user where the files land (/opt/memex)"; else bad "$f names /opt/memex" "the welcome page must say what is installed and where"; fi
done
if grep -q '__USER__' "$MACOS/launchd/com.memex.local.daemon.plist"; then ok "the daemon plist carries __USER__ for the daemon postinstall"; else bad "the daemon plist carries __USER__" ""; fi
[ "$(plutil -extract Disabled raw "$MACOS/launchd/com.memex.local.daemon.plist" 2>/dev/null)" = "true" ] && ok "the daemon plist is Disabled by default — RunAtLoad cannot fire at boot until launchctl enable (Unattended=true)" || bad "daemon plist Disabled=true" "$(plutil -extract Disabled raw "$MACOS/launchd/com.memex.local.daemon.plist" 2>&1)"

echo "── the build refuses bad arguments ──────────────────────────────"
out="$("$BUILD" --version 1.2 --arch arm64 --out "$T/o" --bogus 2>&1)"; rc=$?
case "$rc:$out" in 0:*) bad "an unknown argument is refused" "exited 0" ;; *"unknown argument '--bogus'"*) ok "an unknown argument is refused" ;; *) bad "an unknown argument is refused" "$out" ;; esac
out="$("$BUILD" --version 1.2.x --arch arm64 --out "$T/o" --plan 2>&1)"; rc=$?
case "$rc:$out" in 0:*) bad "a non-numeric version is refused" "exited 0" ;; *"dotted numerics"*) ok "a non-numeric version is refused" ;; *) bad "a non-numeric version is refused" "$out" ;; esac
out="$("$BUILD" --version 1.2.3 --arch armv7 --out "$T/o" --plan 2>&1)"; rc=$?
case "$rc:$out" in 0:*) bad "an unknown arch is refused" "exited 0" ;; *"arm64 or x86_64"*) ok "an unknown arch is refused" ;; *) bad "an unknown arch is refused" "$out" ;; esac
out="$("$BUILD" --version 1.2.3 --arch arm64 --out "$T/o" --prefix /opt/acme --plan 2>&1)"; rc=$?
case "$rc:$out" in 0:*) bad "--prefix is refused (the prefix is a literal in the postinstall and the daemon assets)" "exited 0" ;; *"unknown argument '--prefix'"*) ok "--prefix is refused: /opt/memex is a literal in the postinstall and the daemon assets, not an option" ;; *) bad "--prefix refused" "$out" ;; esac
case "$(cat "$BUILD")" in *'pkg="$work/$pkgname"'*'mv "$pkg" "$pkg.sha256" "$out/"'*) ok "the archive is built, checked and only then MOVED to --out (a later failure leaves nothing behind)" ;; *) bad "stage-then-publish" "" ;; esac

echo "── the signing decision is explicit in every branch (--plan) ────"
plan() { out="$(env "$@" "$BUILD" --version 1.2.3 --arch arm64 --out "$T/o" --plan 2>&1)"; rc=$?; }
plan MEMEX_SIGN_APP_IDENTITY= MEMEX_SIGN_INSTALLER_IDENTITY=
case "$rc:$out" in 0:*"unsigned — MEMEX_SIGN_APP_IDENTITY (CI secret APPLE_DEVELOPER_ID_APPLICATION_P12), MEMEX_SIGN_INSTALLER_IDENTITY (CI secret APPLE_DEVELOPER_ID_INSTALLER_P12) absent"*"name=memex-local-1.2.3-arm64-unsigned.pkg"*)
  ok "no identities → 'unsigned — <input> (<CI secret>) absent', label -unsigned, exit 0" ;;
  *) bad "no identities → unsigned, named, exit 0" "rc=$rc: $out" ;; esac
plan MEMEX_SIGN_APP_IDENTITY="Developer ID Application: X" MEMEX_SIGN_INSTALLER_IDENTITY=
case "$rc:$out" in 0:*"unsigned — MEMEX_SIGN_INSTALLER_IDENTITY (CI secret APPLE_DEVELOPER_ID_INSTALLER_P12) absent"*) ok "only the installer identity absent → it is the one named" ;;
  *) bad "only the installer identity absent → it is the one named" "rc=$rc: $out" ;; esac
plan MEMEX_SIGN_APP_IDENTITY="Developer ID Application: X" MEMEX_SIGN_INSTALLER_IDENTITY="Developer ID Installer: X"
case "$rc:$out" in 0:*"signed, NOT notarized — MEMEX_NOTARY_KEY_PATH (CI secret APPLE_NOTARY_KEY_P8)"*"name=memex-local-1.2.3-arm64-signed-unnotarized.pkg"*) ok "identities without the notary key → signed-unnotarized, the three notary inputs named" ;;
  *) bad "identities without the notary key → signed-unnotarized" "rc=$rc: $out" ;; esac
plan MEMEX_SIGN_APP_IDENTITY="Developer ID Application: X" MEMEX_SIGN_INSTALLER_IDENTITY="Developer ID Installer: X" MEMEX_NOTARY_KEY_PATH=/k.p8 MEMEX_NOTARY_KEY_ID=K MEMEX_NOTARY_ISSUER_ID=I
case "$rc:$out" in 0:*"sign=1 notarize=1 name=memex-local-1.2.3-arm64.pkg"*) ok "every input present → sign + notarize, no label" ;;
  *) bad "every input present → sign + notarize, no label" "rc=$rc: $out" ;; esac
plan MEMEX_SIGN_APP_IDENTITY="Developer ID Application: X" MEMEX_SIGN_INSTALLER_IDENTITY="Developer ID Installer: X" MEMEX_NOTARY_KEY_PATH=/k.p8 MEMEX_NOTARY_KEY_ID=K MEMEX_NOTARY_ISSUER_ID=I
out="$(env MEMEX_SIGN_APP_IDENTITY=X MEMEX_SIGN_INSTALLER_IDENTITY=Y "$BUILD" --version 1.2.3 --arch arm64 --out "$T/o" --plan --sign never 2>&1)"
case "$out" in *"unsigned — --sign never"*"-unsigned.pkg"*) ok "--sign never wins over identities in the environment" ;; *) bad "--sign never wins over identities" "$out" ;; esac

echo "── a signing ERROR exits non-zero (identities present, tool fails) ──"
out="$(env MEMEX_SIGN_APP_IDENTITY="Developer ID Application: Nobody (NOPE)" MEMEX_SIGN_INSTALLER_IDENTITY="Developer ID Installer: Nobody (NOPE)" \
  "$BUILD" --version 1.2.3 --arch arm64 --out "$T/sign-err" --vendor none 2>&1)"; rc=$?
case "$rc:$out" in 0:*) bad "a bogus installer identity fails the build" "exited 0: $out" ;;
  *"productbuild FAILED (signing with 'Developer ID Installer: Nobody (NOPE)')"*) ok "a bogus installer identity fails the build, naming the identity" ;;
  *) bad "a bogus installer identity fails the build, naming the identity" "rc=$rc: $out" ;; esac
if ls "$T/sign-err"/*.pkg >/dev/null 2>&1; then bad "a failed signing leaves no package behind" "$(ls "$T/sign-err")"; else ok "a failed signing leaves no package behind"; fi

echo "── build an unsigned package without the toolchain, expand it ───"
out="$("$BUILD" --version 0.3.777 --arch arm64 --out "$T/novendor" --vendor none --sign never 2>&1)"; rc=$?
pkg="$(printf '%s\n' "$out" | tail -1)"
case "$rc:$pkg" in 0:*/memex-local-0.3.777-arm64-novendor-unsigned.pkg) ok "builds memex-local-0.3.777-arm64-novendor-unsigned.pkg (labels: -novendor -unsigned)" ;;
  *) bad "builds the novendor unsigned package" "rc=$rc: $out" ;; esac
[ -f "$pkg.sha256" ] && [ "$(wc -c < "$pkg.sha256")" -eq 65 ] && ok "writes a 64-hex .sha256 beside it" || bad "writes a .sha256 beside it" "$(ls "$T/novendor")"
EXP="$T/expanded"
if pkgutil --expand "$pkg" "$EXP" >/dev/null 2>&1; then ok "pkgutil --expand succeeds"; else bad "pkgutil --expand succeeds" ""; fi
for f in Distribution memex-local-cli.pkg/PackageInfo memex-local-cli.pkg/Scripts memex-local-cli.pkg/Payload memex-local-daemon.pkg/PackageInfo memex-local-daemon.pkg/Scripts; do
  [ -e "$EXP/$f" ] && ok "expanded: $f" || bad "expanded: $f" "missing from $(ls "$EXP")"
done
grep -q 'hostArchitectures="arm64"' "$EXP/Distribution" && ok "Distribution pins hostArchitectures=arm64" || bad "Distribution pins hostArchitectures" "$(grep -o 'hostArchitectures="[^"]*"' "$EXP/Distribution")"
grep -q 'Memex Local 0.3.777' "$EXP/Distribution" && ok "Distribution title carries the version" || bad "Distribution title carries the version" ""
grep -q 'choice id="daemon"' "$EXP/Distribution" && grep -q 'start_selected="false"' "$EXP/Distribution" && ok "the daemon is an optional choice, not selected by default" || bad "the daemon is an optional, unselected choice" ""
for r in welcome.html license.html conclusion.html; do [ -f "$EXP/Resources/$r" ] && ok "Resources/$r shipped" || bad "Resources/$r shipped" ""; done
grep -q 'Apache License' "$EXP/Resources/license.html" && grep -q 'MIT License' "$EXP/Resources/license.html" && ok "license.html carries the repository's Apache-2.0 and MIT texts" || bad "license.html carries both licence texts" ""
grep -q 'identifier="com.memex.local.cli"' "$EXP/memex-local-cli.pkg/PackageInfo" && ok "cli component identifier com.memex.local.cli" || bad "cli component identifier" "$(head -3 "$EXP/memex-local-cli.pkg/PackageInfo")"
grep -q 'version="0.3.777"' "$EXP/memex-local-cli.pkg/PackageInfo" && ok "cli component version 0.3.777" || bad "cli component version" ""
# The payload listing: pkgutil --expand-full lays the payload out as files.
FULL="$T/full"; pkgutil --expand-full "$pkg" "$FULL" >/dev/null 2>&1
PAY="$FULL/memex-local-cli.pkg/Payload"
for f in opt/memex/bin/memex-local opt/memex/libexec/memex-local opt/memex/libexec/uninstall.sh opt/memex/share/tools.manifest opt/memex/share/tools.sh \
         opt/memex/share/helm/Chart.yaml opt/memex/share/com.memex.local.plist opt/memex/share/com.memex.local.backlease.plist opt/memex/share/backlease-sampler.sh \
         opt/memex/share/port-forward.sh opt/memex/share/doc/VERSION opt/memex/share/doc/BUILD etc/paths.d/memex; do
  [ -e "$PAY/$f" ] && ok "payload: $f" || bad "payload: $f" "missing"
done
[ -d "$PAY/opt/memex/libexec/vendor" ] && bad "--vendor none ships no libexec/vendor" "$(ls "$PAY/opt/memex/libexec/vendor")" || ok "--vendor none ships no libexec/vendor"
[ "$(cat "$PAY/etc/paths.d/memex")" = "/opt/memex/bin" ] && ok "/etc/paths.d/memex puts /opt/memex/bin on PATH" || bad "/etc/paths.d/memex content" "$(cat "$PAY/etc/paths.d/memex")"
[ "$(cat "$PAY/opt/memex/share/doc/VERSION")" = "0.3.777" ] && ok "share/doc/VERSION is the build version" || bad "share/doc/VERSION" "$(cat "$PAY/opt/memex/share/doc/VERSION")"
grep -q 'MEMEX_LOCAL_SHARE="/opt/memex/share" MEMEX_CHART_DIR="/opt/memex/share/helm" MEMEX_PREFIX="/opt/memex"' "$PAY/opt/memex/bin/memex-local" \
  && ok "the wrapper sets MEMEX_LOCAL_SHARE, MEMEX_CHART_DIR and MEMEX_PREFIX like the formula's env script" || bad "the wrapper's env" "$(cat "$PAY/opt/memex/bin/memex-local")"
[ -x "$PAY/opt/memex/libexec/memex-local" ] && [ -x "$PAY/opt/memex/bin/memex-local" ] && ok "the CLI and its wrapper are executable" || bad "CLI executable bits" ""
cmp -s "$PAY/opt/memex/libexec/memex-local" "$BREW/bin/memex-local" && ok "libexec/memex-local is byte-identical to deploy/homebrew/bin/memex-local" || bad "libexec/memex-local identical to the source" ""
[ -x "$FULL/memex-local-cli.pkg/Scripts/postinstall" ] && ok "cli Scripts/postinstall present and executable" || bad "cli Scripts/postinstall" ""
[ -x "$FULL/memex-local-daemon.pkg/Scripts/postinstall" ] && ok "daemon Scripts/postinstall present and executable" || bad "daemon Scripts/postinstall" ""
DPAY="$FULL/memex-local-daemon.pkg/Payload"
[ -f "$DPAY/Library/LaunchDaemons/com.memex.local.daemon.plist" ] && plutil -lint "$DPAY/Library/LaunchDaemons/com.memex.local.daemon.plist" >/dev/null 2>&1 \
  && ok "daemon payload: a valid /Library/LaunchDaemons/com.memex.local.daemon.plist" || bad "daemon payload plist" ""
[ -x "$DPAY/opt/memex/libexec/memex-daemon.sh" ] && ok "daemon payload: /opt/memex/libexec/memex-daemon.sh" || bad "daemon payload script" ""

echo "── DRIFT: the package's share/ is the formula's share/, file for file ──"
# Formula/memex-local.rb installs exactly: bin/memex-local → libexec, share/* → libexec/share, deploy/helm/* → libexec/share/helm,
# plus the env wrapper. The package mirrors those three sources under /opt/memex; any fourth source in the formula is drift.
installs="$(sed -n '/^  def install/,/^  end/p' "$BREW/Formula/memex-local.rb" | grep -c '\.install \|write_env_script')"
[ "$installs" = "4" ] && ok "the formula's install block has the 4 steps build-pkg.sh mirrors (3 installs + the env wrapper)" \
  || bad "the formula's install block has 4 steps" "found $installs — a new install source in the formula must be mirrored in build-pkg.sh and this test"
grep -q 'libexec.install "deploy/homebrew/bin/memex-local"' "$BREW/Formula/memex-local.rb" \
  && grep -q '(libexec/"share").install Dir\["deploy/homebrew/share/\*"\]' "$BREW/Formula/memex-local.rb" \
  && grep -q '(libexec/"share/helm").install Dir\["deploy/helm/\*"\]' "$BREW/Formula/memex-local.rb" \
  && ok "the formula still names the three sources (bin/memex-local, share/*, deploy/helm/*)" || bad "the formula's three sources" "edit build-pkg.sh step 1 and this test together"
drift=""
while IFS= read -r f; do
  rel="${f#deploy/homebrew/share/}"
  [ -e "$PAY/opt/memex/share/$rel" ] || drift="$drift share/$rel"
done < <(git -C "$REPO" ls-files deploy/homebrew/share)
while IFS= read -r f; do
  rel="${f#deploy/helm/}"
  [ -e "$PAY/opt/memex/share/helm/$rel" ] || drift="$drift helm/$rel"
done < <(git -C "$REPO" ls-files deploy/helm)
[ -z "$drift" ] && ok "every tracked file of deploy/homebrew/share and deploy/helm is in the payload" || bad "every tracked share/helm file is in the payload" "missing:$drift"
extra=""
while IFS= read -r f; do
  rel="${f#"$PAY"/opt/memex/share/}"
  case "$rel" in helm/*|doc/*) continue ;; esac
  [ -e "$BREW/share/$rel" ] || extra="$extra $rel"
done < <(find "$PAY/opt/memex/share" -type f)
[ -z "$extra" ] && ok "the payload's share/ has nothing the formula would not install" || bad "no extra share/ files in the payload" "extra:$extra"

echo "── vendoring: sha256-verified against the manifest, fatal on mismatch ──"
# A fixture manifest with file:// assets — one binary, one tar.gz — and its own `tool` rows.
FIX="$T/fix"; mkdir -p "$FIX/tarsrc/dir"
printf '#!/bin/bash\necho "fakea version 1.2.3"\n' > "$FIX/fakea"; chmod +x "$FIX/fakea"
printf '#!/bin/bash\necho "v9.9.9"\n' > "$FIX/tarsrc/dir/fakeb"; chmod +x "$FIX/tarsrc/dir/fakeb"
tar -czf "$FIX/fakeb.tar.gz" -C "$FIX/tarsrc" dir
sha_a="$(shasum -a 256 "$FIX/fakea" | awk '{print $1}')"; sha_b="$(shasum -a 256 "$FIX/fakeb.tar.gz" | awk '{print $1}')"
cat > "$FIX/good.manifest" <<MAN
tool  fakea  fakea  fakea  direct  1.0.0  1.2.3  version
tool  fakeb  fakeb  fakeb  direct  9.0.0  9.9.9  --version
asset fakea  arm64  binary  -         $sha_a  file://$FIX/fakea
asset fakeb  arm64  tar.gz  dir/fakeb  $sha_b  file://$FIX/fakeb.tar.gz
MAN
sed "s/$sha_b/0000000000000000000000000000000000000000000000000000000000000000/" "$FIX/good.manifest" > "$FIX/bad.manifest"
out="$(env MEMEX_TOOLS_MANIFEST="$FIX/good.manifest" "$BUILD" --version 0.3.778 --arch arm64 --out "$T/vend" --cache "$T/cache" --sign never 2>&1)"; rc=$?
pkg2="$(printf '%s\n' "$out" | tail -1)"
case "$rc:$pkg2" in 0:*/memex-local-0.3.778-arm64-unsigned.pkg) ok "a vendored build (fixture manifest, file:// assets) succeeds with no -novendor label" ;;
  *) bad "a vendored build succeeds" "rc=$rc: $out" ;; esac
F2="$T/full2"; pkgutil --expand-full "$pkg2" "$F2" >/dev/null 2>&1
[ -x "$F2/memex-local-cli.pkg/Payload/opt/memex/libexec/vendor/fakea/bin/fakea" ] && ok "a `binary` asset lands at libexec/vendor/<tool>/bin/<bin>" || bad "binary asset placement" "$(find "$F2/memex-local-cli.pkg/Payload/opt/memex/libexec" 2>/dev/null)"
[ -x "$F2/memex-local-cli.pkg/Payload/opt/memex/libexec/vendor/fakeb/bin/fakeb" ] && ok "a `tar.gz` asset's member lands at libexec/vendor/<tool>/bin/<bin>" || bad "tar.gz asset placement" ""
[ "$(cat "$F2/memex-local-cli.pkg/Payload/opt/memex/libexec/vendor/fakeb/VERSION")" = "9.9.9" ] && ok "libexec/vendor/<tool>/VERSION records the pinned version" || bad "vendor VERSION file" ""
[ -f "$T/cache/$sha_a" ] && [ -f "$T/cache/$sha_b" ] && ok "downloads are cached by sha256" || bad "downloads are cached by sha256" "$(ls "$T/cache")"
out="$(env MEMEX_TOOLS_MANIFEST="$FIX/bad.manifest" "$BUILD" --version 0.3.779 --arch arm64 --out "$T/vend-bad" --cache "$T/cache-bad" --sign never 2>&1)"; rc=$?
case "$rc:$out" in 0:*) bad "a sha256 mismatch is FATAL" "exited 0: $out" ;;
  *"fakeb — sha256 MISMATCH"*"manifest: 0000000000"*"download: $sha_b"*) ok "a sha256 mismatch is fatal and names the tool, the pinned and the downloaded hash" ;;
  *) bad "a sha256 mismatch is fatal, naming the tool" "rc=$rc: $out" ;; esac
if ls "$T/vend-bad"/*.pkg >/dev/null 2>&1; then bad "a mismatch leaves no package behind" "$(ls "$T/vend-bad")"; else ok "a mismatch leaves no package behind"; fi
[ -f "$T/cache-bad/$sha_a" ] && [ ! -f "$T/cache-bad/0000000000000000000000000000000000000000000000000000000000000000" ] && ok "the mismatched download is not kept in the cache" || bad "mismatched download not cached" "$(ls "$T/cache-bad")"
# The manifest every real build reads must itself be whole: an asset for both arches per vendored tool, 64-hex sha, https URLs.
mfail=""
while read -r name; do
  for a in arm64 x86_64; do
    row="$(awk -v n="$name" -v a="$a" '$1=="asset" && $2==n && $3==a' "$BREW/share/tools.manifest")"
    [ -n "$row" ] || mfail="$mfail $name/$a:missing"
    sha="$(printf '%s' "$row" | awk '{print $6}')"; url="$(printf '%s' "$row" | awk '{print $7}')"
    [ -n "$row" ] && { printf '%s' "$sha" | grep -Eq '^[0-9a-f]{64}$' || mfail="$mfail $name/$a:sha"; }
    [ -n "$row" ] && { case "$url" in https://*) ;; *) mfail="$mfail $name/$a:url" ;; esac; }
  done
done < <(awk '$1=="tool" && $7!="-" { print $2 }' "$BREW/share/tools.manifest")
[ -z "$mfail" ] && ok "tools.manifest: every vendored tool has an arm64 and an x86_64 asset with a 64-hex sha256 and an https URL" || bad "tools.manifest assets complete" "$mfail"
while read -r name; do
  if awk -v n="$name" '$1=="asset" && $2==n' "$BREW/share/tools.manifest" | grep -q .; then bad "tools.manifest: $name is not vendored and has no asset rows" "has asset rows"; else ok "tools.manifest: $name (not vendored) has no asset rows"; fi
done < <(awk '$1=="tool" && $7=="-" { print $2 }' "$BREW/share/tools.manifest")

echo "── postinstall: probe first, activate a vendored copy only where needed, idempotent ──"
# A staged prefix as the payload would leave it: share/tools.sh + manifest, vendored stubs; and an
# "existing" PATH directory standing for the developer's Homebrew copies.
P="$T/prefix"; EX="$T/existing"; mkdir -p "$P/share" "$EX"
# Only the fixtures and the utilities the scripts need — never a system directory, whose real
# kubectl/helm would satisfy a floor the fixture means to fail (see the homebrew suite).
TSYS="$T/sys"; mkdir -p "$TSYS"
for u in bash sh awk cut head tail grep sed tr mktemp ln rm mkdir dirname basename readlink ls wc cat date id uname stat sort env sleep chmod printf sudo; do
  for d in /usr/bin /bin /usr/sbin; do [ -x "$d/$u" ] && { ln -sf "$d/$u" "$TSYS/$u"; break; }; done
done
cp "$BREW/share/tools.sh" "$BREW/share/tools.manifest" "$P/share/"
stub() { printf '#!/bin/bash\nprintf "%%s\\n" "%s"\n' "$3" > "$1/$2"; chmod +x "$1/$2"; }
vend() { mkdir -p "$P/libexec/vendor/$1/bin"; stub "$P/libexec/vendor/$1/bin" "$2" "$3"; }
vend colima colima "colima version 0.10.3"; vend lima limactl "limactl version 2.2.1"; vend kubectl kubectl "Client Version: v1.37.1"
vend helm helm "v4.3.1+gabc"; vend mkcert mkcert "v1.4.4"
existing_all() {
  rm -rf "$EX"; mkdir -p "$EX"
  stub "$EX" colima "colima version 0.10.3"; stub "$EX" limactl "limactl version 2.2.0"; stub "$EX" kubectl "Client Version: v1.37.0"
  stub "$EX" helm "v4.2.4+g"; stub "$EX" mkcert "v1.4.4"; stub "$EX" ollama "ollama version is 0.33.2"; stub "$EX" socket_vmnet ""
}
POST="$MACOS/scripts/postinstall"
runpost() { OUT="$(env MEMEX_PKG_PREFIX="$P" MEMEX_PKG_PROBE_PATH="$EX:$TSYS" PATH="/usr/bin:/bin" "$POST" 2>&1)"; RC=$?; }
links() { ls "$P/bin" 2>/dev/null | tr '\n' ' '; }

existing_all; runpost
case "$RC:$OUT" in 0:*"changed: 0"*) ok "all tools present and satisfying → exits 0, changed: 0" ;; *) bad "all present → changed: 0" "rc=$RC: $OUT" ;; esac
[ -z "$(links)" ] && ok "all present and satisfying → NOTHING vendored is activated (bin/ holds no fallback)" || bad "nothing activated when all satisfy" "links: $(links)"
[ -f "$P/etc/tools.report" ] && grep -q 'colima        existing  0.10.3' "$P/etc/tools.report" && ok "etc/tools.report records the probe (colima existing 0.10.3)" || bad "tools.report written" "$(cat "$P/etc/tools.report" 2>/dev/null)"
grep -q 'using the existing installation' "$P/etc/tools.report" && ok "the report says the existing installation is used" || bad "report wording" ""
case "$(sed -n '/^probe_all()/,/^}/p' "$POST")" in *'sudo -u "$probe_user" -H env PATH="$PROBE_PATH" MEMEX_PREFIX="$PREFIX" /bin/bash -c'*) ok "as root, the probe (which EXECUTES the found tools) runs through sudo as the console user / nobody, never as root, with the SYSTEM bash by absolute path" ;; *) bad "unprivileged probe" "$(sed -n '/^probe_all()/,/^}/p' "$POST")" ;; esac
grep -q '^export PATH="/usr/bin:/bin:/usr/sbin:/sbin"$' "$POST" && ok "root's own PATH in the postinstall is the trusted system PATH" || bad "trusted root PATH" "$(grep -n 'export PATH' "$POST")"
grep -q 'brew_bin="$(PATH="$PROBE_PATH" command -v brew 2>/dev/null || true)"' "$POST" && ! grep -q 'env PATH="$PROBE_PATH" command' "$POST" && ! grep -q 'PATH="$PROBE_PATH" bash -c' "$POST" \
  && ok "the brew lookup is the shell builtin under a PATH assignment in THIS shell — no env … command, no PATH=… bash -c (bash resolves the command under the prefix PATH)" || bad "brew lookup via builtin" "$(grep -n 'brew_bin=' "$POST")"
# Behaviour: a planted `bash` on the probe PATH must never run during the lookup.
PL="$T/planted"; mkdir -p "$PL"; printf '#!/bin/sh\necho PLANTED-BASH-RAN\n' > "$PL/bash"; chmod +x "$PL/bash"; printf '#!/bin/sh\n' > "$PL/brew"; chmod +x "$PL/brew"
out="$(env MEMEX_PKG_PREFIX="$P" MEMEX_PKG_PROBE_PATH="$PL:$EX:$TSYS" PATH="/usr/bin:/bin" "$POST" 2>&1)"
case "$out" in *PLANTED-BASH-RAN*) bad "a planted bash on the probe PATH is never executed by the postinstall's own lookups" "$out" ;; *) ok "a planted bash on the probe PATH is never executed by the postinstall's own lookups" ;; esac
grep -q '^set -euo pipefail' "$POST" && ok "the postinstall fails on an unhandled error (set -e)" || bad "set -e" ""

rm -f "$EX/helm"; runpost
case "$RC:$OUT" in 0:*"ACTIVATED the vendored fallback → $P/bin/helm"*"changed: 1"*) ok "one tool missing (helm) → only it is activated, changed: 1" ;; *) bad "one missing → only that one activated" "rc=$RC: $OUT" ;; esac
[ "$(links)" = "helm " ] && ok "bin/ holds exactly the helm fallback" || bad "bin/ holds exactly helm" "links: $(links)"
[ "$(readlink "$P/bin/helm")" = "$P/libexec/vendor/helm/bin/helm" ] && ok "the fallback links to libexec/vendor/helm/bin/helm" || bad "fallback target" "$(readlink "$P/bin/helm")"

runpost
case "$RC:$OUT" in 0:*"kept the vendored fallback"*"changed: 0"*) ok "a re-run in the same state changes nothing (changed: 0, fallback kept)" ;; *) bad "re-run unchanged" "rc=$RC: $OUT" ;; esac
[ "$(links)" = "helm " ] && ok "a re-run leaves bin/ as it was" || bad "re-run leaves bin/" "links: $(links)"

stub "$EX" kubectl "Client Version: v1.20.0"; runpost
case "$RC:$OUT" in 0:*"kubectl       vendored  1.37.1"*"below the floor 1.31.0"*"changed: 1"*) ok "one tool below the floor (kubectl 1.20.0) → the vendored one is used and the report says why" ;;
  *) bad "below floor → vendored, with the reason" "rc=$RC: $OUT" ;; esac
case "$(links)" in "helm kubectl ") ok "bin/ now holds helm and kubectl, nothing else" ;; *) bad "bin/ holds helm + kubectl" "links: $(links)" ;; esac

existing_all; runpost
case "$RC:$OUT" in 0:*"removed the stale fallback $P/bin/kubectl"*"removed the stale fallback $P/bin/helm"*"changed: 2"*) ok "satisfying installations appear → both fallbacks are removed, the user's copies take over" ;;
  *) bad "stale fallbacks removed" "rc=$RC: $OUT" ;; esac
[ -z "$(links)" ] && ok "bin/ is empty again" || bad "bin/ empty again" "links: $(links)"

# A filesystem error is an install FAILURE, never a report of a repair that was not written.
rm -f "$EX/helm"; chmod 555 "$P/bin"; runpost; chmod 755 "$P/bin"
case "$RC:$OUT" in 0:*) bad "an unwritable bin/ fails the postinstall" "exited 0: $OUT" ;; *"cannot activate $P/bin/helm"*"NOT repaired"*) ok "an unwritable bin/ fails the postinstall (non-zero) and says the toolchain was NOT repaired" ;; *) bad "unwritable bin/ fails" "rc=$RC: $OUT" ;; esac
existing_all; runpost
# A vendored copy that prints no version is never activated.
rm -f "$EX/mkcert"; stub "$P/libexec/vendor/mkcert/bin" mkcert ""; runpost
case "$RC:$OUT" in 0:*"mkcert        existing"*|0:*"mkcert        missing"*) ;; *) bad "vendored no-version mkcert handled" "rc=$RC: $OUT" ;; esac
[ ! -e "$P/bin/mkcert" ] && ok "a vendored copy that reports no version is NOT activated (bin/mkcert absent)" || bad "no-version vendored not activated" "$(ls -l "$P/bin")"
grep -q 'reports no version (damaged or incompatible — not activated)' "$P/etc/tools.report" && ok "the report says the vendored copy reports no version" || bad "report no-version wording" "$(grep mkcert "$P/etc/tools.report")"
stub "$P/libexec/vendor/mkcert/bin" mkcert "v1.4.4"; existing_all; runpost
rm -f "$EX/ollama"; runpost
case "$RC:$OUT" in 0:*"ollama        missing"*"NOT RESOLVED — nothing vendored for it; install it (ollama)"*) ok "a missing tool with no vendored copy (ollama) is reported, named with its formula, and the install still exits 0" ;;
  *) bad "missing, not vendored → reported, exit 0" "rc=$RC: $OUT" ;; esac
grep -q 'ollama        missing' "$P/etc/tools.report" && ok "the report carries the unresolved tool" || bad "report carries unresolved" ""

echo "── daemon postinstall: service user from the managed preference, loaded only when Unattended ──"
D="$T/daemon"; mkdir -p "$D/stubs" "$D/log"
cp "$MACOS/launchd/com.memex.local.daemon.plist" "$D/plist"
cat > "$D/stubs/defaults" <<'STUB'
#!/bin/bash
key="${!#}"
case "$key" in ServiceUser) [ -n "${FAKE_SERVICE_USER:-}" ] && { printf '%s\n' "$FAKE_SERVICE_USER"; exit 0; }; exit 1 ;;
  Unattended) [ -n "${FAKE_UNATTENDED:-}" ] && { printf '%s\n' "$FAKE_UNATTENDED"; exit 0; }; exit 1 ;; esac
exit 1
STUB
printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "$FAKE_LAUNCHCTL_LOG"\ncase " $* " in *" ${FAKE_LAUNCHCTL_FAIL:-never} "*) exit 1 ;; esac\nexit 0\n' > "$D/stubs/launchctl"
chmod +x "$D/stubs/defaults" "$D/stubs/launchctl"
DPOST="$MACOS/daemon/postinstall"
rund() { OUT="$(env "$@" FAKE_LAUNCHCTL_LOG="$D/launchctl.log" MEMEX_PKG_DAEMON_PLIST="$D/plist" MEMEX_PKG_DAEMON_LOGDIR="$D/log" PATH="$D/stubs:/usr/libexec:/usr/bin:/bin:/usr/sbin" "$DPOST" 2>&1)"; RC=$?; }
: > "$D/launchctl.log"
rund FAKE_SERVICE_USER=memexsvc FAKE_UNATTENDED=
case "$RC:$OUT" in 0:*"service user memexsvc"*"DISABLED and not loaded"*) ok "ServiceUser preference → written into the plist; Unattended unset → installed, DISABLED, not loaded" ;; *) bad "daemon: user from preference, disabled" "rc=$RC: $OUT" ;; esac
[ "$(plutil -extract UserName raw "$D/plist" 2>/dev/null)" = "memexsvc" ] && ok "the plist's UserName is memexsvc" || bad "plist UserName" "$(plutil -extract UserName raw "$D/plist" 2>&1)"
plutil -lint "$D/plist" >/dev/null 2>&1 && ok "the rendered daemon plist is valid" || bad "rendered daemon plist valid" ""
grep -q "bootout system $D/plist" "$D/launchctl.log" && grep -q "disable system/com.memex.local.daemon" "$D/launchctl.log" && ! grep -q "bootstrap" "$D/launchctl.log" \
  && ok "Unattended unset → bootout (stops a daemon an earlier install loaded) + launchctl disable (persisted: no start at boot), no bootstrap" || bad "disable semantics" "$(cat "$D/launchctl.log")"
: > "$D/launchctl.log"
rund FAKE_SERVICE_USER=memexsvc FAKE_UNATTENDED=true
case "$RC:$OUT" in 0:*"enabled + bootstrapped (Unattended=true)"*) ok "Unattended=true → the daemon is enabled and bootstrapped" ;; *) bad "Unattended=true → enabled + bootstrapped" "rc=$RC: $OUT" ;; esac
grep -q "enable system/com.memex.local.daemon" "$D/launchctl.log" && grep -q "bootstrap system $D/plist" "$D/launchctl.log" && ok "launchctl enable + bootstrap system <plist> were issued" || bad "launchctl enable + bootstrap issued" "$(cat "$D/launchctl.log")"
: > "$D/launchctl.log"
rund FAKE_SERVICE_USER=memexsvc FAKE_UNATTENDED=false
grep -q "bootout system $D/plist" "$D/launchctl.log" && grep -q "disable system/com.memex.local.daemon" "$D/launchctl.log" && ok "Unattended=false after true → the loaded daemon is booted out and disabled" || bad "true → false unloads" "$(cat "$D/launchctl.log")"
rund FAKE_SERVICE_USER=memexsvc FAKE_UNATTENDED=true
[ "$(plutil -extract UserName raw "$D/plist")" = "memexsvc" ] && case "$OUT" in *"(was memexsvc)"*) ok "a re-run finds the previous user, re-renders the same plist and re-bootstraps" ;; *) bad "re-run idempotent" "$OUT" ;; esac
cp "$MACOS/launchd/com.memex.local.daemon.plist" "$D/plist"
: > "$D/launchctl.log"
rund FAKE_SERVICE_USER=root FAKE_UNATTENDED=true
case "$RC:$OUT" in 0:*"no service user"*"booted out and disabled"*) ok "a root/absent service user → refuses to render, exit 0" ;; *) bad "no service user handled" "rc=$RC: $OUT" ;; esac
grep -q "bootout system $D/plist" "$D/launchctl.log" && grep -q "disable system/com.memex.local.daemon" "$D/launchctl.log" && ok "…and an EARLIER enabled job is booted out and disabled before the early return" || bad "no-user path disables the old job" "$(cat "$D/launchctl.log")"
grep -q '__USER__' "$D/plist" && ok "the token stays in the plist when no user could be chosen" || bad "token stays" ""
# A requested enable/bootstrap that FAILS fails the install (Installer and the MDM must hear it).
: > "$D/launchctl.log"
rund FAKE_SERVICE_USER=memexsvc FAKE_UNATTENDED=true FAKE_LAUNCHCTL_FAIL=bootstrap
case "$RC:$OUT" in 0:*) bad "a failed bootstrap under Unattended=true fails the postinstall" "exited 0: $OUT" ;; *"bootstrap system $D/plist FAILED"*"NOT armed"*) ok "a failed bootstrap under Unattended=true exits non-zero and says unattended startup is NOT armed" ;; *) bad "failed bootstrap → non-zero" "rc=$RC: $OUT" ;; esac
rund FAKE_SERVICE_USER=memexsvc FAKE_UNATTENDED=true FAKE_LAUNCHCTL_FAIL=enable
case "$RC:$OUT" in 0:*) bad "a failed enable under Unattended=true fails the postinstall" "exited 0: $OUT" ;; *"enable system/com.memex.local.daemon FAILED"*) ok "a failed enable under Unattended=true exits non-zero" ;; *) bad "failed enable → non-zero" "rc=$RC: $OUT" ;; esac
rund FAKE_SERVICE_USER=memexsvc FAKE_UNATTENDED=false FAKE_LAUNCHCTL_FAIL=bootout
[ "$RC" = "0" ] && ok "an absent old job (bootout fails) is still tolerated" || bad "bootout tolerated" "rc=$RC: $OUT"

echo
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
