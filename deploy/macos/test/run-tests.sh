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
echo "── Intune: the Microsoft sign-in profiles and their Graph bodies (deploy/macos/intune/README.md) ──"
IOS="$REPO/deploy/ios"
INTUNE_PROFILES=("$MACOS"/intune/profiles/*.mobileconfig "$IOS"/intune/profiles/*.mobileconfig)
for f in "${INTUNE_PROFILES[@]}"; do
  if plutil -lint "$f" >/dev/null 2>&1 && xmllint --noout --nonet "$f" 2>/dev/null; then ok "Intune profile lints (plutil + strict XML): ${f#"$REPO"/}"; else bad "Intune profile lint ${f#"$REPO"/}" "$(plutil -lint "$f" 2>&1)"; fi
done
for f in "$MACOS"/intune/graph/*.json "$IOS"/intune/graph/*.json; do
  python3 -m json.tool "$f" >/dev/null 2>&1 && ok "Graph body parses: ${f#"$REPO"/}" || bad "Graph body ${f#"$REPO"/}" "$(python3 -m json.tool "$f" 2>&1 | head -2)"
done
# The UUIDs are stable (an MDM replaces a profile by identifier + UUID) and distinct across every profile.
UUIDS="$(for f in "${INTUNE_PROFILES[@]}"; do xmllint --xpath '//key[.="PayloadUUID"]/following-sibling::string[1]/text()' "$f" 2>/dev/null; echo; done | tr -s ' \n' '\n' | grep -v '^$')"
[ "$(sort <<<"$UUIDS" | uniq -d | wc -l | tr -d ' ')" = 0 ] && [ "$(wc -l <<<"$UUIDS" | tr -d ' ')" -ge 6 ] \
  && ok "every Intune profile's PayloadUUIDs are distinct" || bad "duplicate PayloadUUID" "$UUIDS"
# The Graph body for the com.memex.local custom profile carries exactly the committed file.
PAY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["payload"])' "$MACOS/intune/graph/com.memex.local.json")"
[ "$PAY" = "$(base64 < "$MACOS/intune/profiles/com.memex.local.mobileconfig" | tr -d '\n')" ] \
  && ok "graph/com.memex.local.json's payload is the committed profile (base64), never a stale copy" \
  || bad "com.memex.local.json payload stale" "regenerate: base64 < deploy/macos/intune/profiles/com.memex.local.mobileconfig"
[ "$(plutil -extract PayloadContent.0.UserPrincipalName raw "$MACOS/intune/profiles/com.memex.local.mobileconfig" 2>/dev/null)" = "{{userprincipalname}}" ] \
  && ok "the com.memex.local profile fills UserPrincipalName with Intune's {{userprincipalname}} (what memex-local work_identity reads)" || bad "UserPrincipalName token" ""
# Each SSO profile and its Graph body name the same extension, team and URLs, and the Redirect type.
for pair in "macos:macOSSingleSignOnExtension:com.microsoft.CompanyPortalMac.ssoextension:UBF8T346G9" "ios:iosSingleSignOnExtension:com.microsoft.azureauthenticator.ssoextension:SGGM6D27TK"; do
  IFS=: read -r os prop ext team <<<"$pair"
  prof="$REPO/deploy/$os/intune/profiles/microsoft-enterprise-sso.mobileconfig"; body="$REPO/deploy/$os/intune/graph/microsoft-enterprise-sso.json"
  P_URLS="$(plutil -extract PayloadContent.0.URLs json -o - "$prof" 2>/dev/null | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)))')"
  G="$(python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))[sys.argv[2]]; print(e["extensionIdentifier"]+"|"+e["teamIdentifier"]+"|"+",".join(e["urlPrefixes"])+"|"+e["@odata.type"])' "$body" "$prop")"
  [ "$(plutil -extract PayloadContent.0.Type raw "$prof")" = Redirect ] && [ "$(plutil -extract PayloadContent.0.ExtensionIdentifier raw "$prof")" = "$ext" ] \
    && [ "$(plutil -extract PayloadContent.0.TeamIdentifier raw "$prof")" = "$team" ] \
    && [ "$G" = "$ext|$team|$P_URLS|#microsoft.graph.${prop/SingleSignOnExtension/RedirectSingleSignOnExtension}" ] \
    && grep -q 'https://login.microsoftonline.com' <<<"$P_URLS" \
    && [ "$(plutil -extract PayloadContent.0.ExtensionData.browser_sso_interaction_enabled raw "$prof")" = 1 ] \
    && ok "$os SSO plug-in: Redirect, $ext / $team, profile and Graph body agree" || bad "$os SSO plug-in profile/Graph disagree" "$G vs $P_URLS"
done
grep -q 'b892bd0f-86ed-46ce-8593-3b9860f7a557' "$MACOS/intune/graph/assign-pilot.json" && ! grep -rqi 'allDevices\|allLicensedUsers' "$MACOS/intune/graph" "$IOS/intune/graph" \
  && ok "the assignment bodies target the pilot group only (never all devices or all users)" || bad "assignment target" ""
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
# deploy/backlease/* → libexec/share/backlease, plus the env wrapper. The package mirrors those four sources under /opt/memex;
# any fifth source in the formula is drift.
installs="$(sed -n '/^  def install/,/^  end/p' "$BREW/Formula/memex-local.rb" | grep -c '\.install \|write_env_script')"
[ "$installs" = "5" ] && grep -q 'cp -R "$repo/deploy/backlease/." "$p/share/backlease/"' "$MACOS/build-pkg.sh" \
  && ok "the formula's install block has the 5 steps build-pkg.sh mirrors (4 installs, share/backlease included, + the env wrapper)" \
  || bad "the formula's install block has 5 steps" "found $installs — a new install source in the formula must be mirrored in build-pkg.sh and this test"
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
while IFS= read -r f; do
  rel="${f#deploy/backlease/}"
  [ -e "$PAY/opt/memex/share/backlease/$rel" ] || drift="$drift backlease/$rel"
done < <(git -C "$REPO" ls-files deploy/backlease)
[ -z "$drift" ] && ok "every tracked file of deploy/homebrew/share, deploy/helm and deploy/backlease is in the payload" || bad "every tracked share/helm file is in the payload" "missing:$drift"
extra=""
while IFS= read -r f; do
  rel="${f#"$PAY"/opt/memex/share/}"
  case "$rel" in helm/*|doc/*|backlease/*) continue ;; esac
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

echo "── the fleet bootstrap (Intune, as root): idempotent, says what it waits for ──"
FB="$MACOS/intune/fleet-bootstrap.sh"
bash -n "$FB" && ok "fleet-bootstrap.sh parses" || bad "fleet-bootstrap.sh parses" "$(bash -n "$FB" 2>&1)"
grep -q '^readonly FLEET_BOOTSTRAP_VERSION="[0-9]\+\.[0-9]\+\.[0-9]\+"' "$FB" && ok "the bootstrap carries a version" || bad "bootstrap version" "missing"
fb() { env FLEET_BOOTSTRAP_LIB=1 "$@" bash -c '. "$0"; '"${FB_EVAL:-true}" "$FB"; }
FB_EVAL='tailscaled_plist' fb > "$T/ts.plist"
plutil -lint "$T/ts.plist" >/dev/null 2>&1 && grep -q '<string>--socket=/var/run/tailscaled.socket</string>' "$T/ts.plist" \
  && grep -q 'com.meshweaver.fleet.tailscaled' "$T/ts.plist" && grep -q '<key>RunAtLoad</key><true/>' "$T/ts.plist" \
  && ok "the tailscaled LaunchDaemon plist lints, runs at boot as root, on the fleet socket" || bad "tailscaled plist" "$(cat "$T/ts.plist")"
for u in root loginwindow _mbsetupuser; do
  FB_EVAL='console_user && echo got || echo none' fb FLEET_CONSOLE_USER="$u" | grep -qx none && ok "console user '$u' is nobody (the run waits)" || bad "console user $u" "accepted"
done
[ "$(FB_EVAL='console_user' fb FLEET_CONSOLE_USER=jroe)" = "jroe" ] && ok "a person at the console is the user the Mac's memex runs as" || bad "console user jroe" "not returned"
[ "$(FB_EVAL='machine_id' fb FLEET_MACHINE_ID=studio-2)" = "studio-2" ] && ok "a configured machine id wins" || bad "machine id configured" "$(FB_EVAL='machine_id' fb FLEET_MACHINE_ID=studio-2)"
mkdir -p "$T/fbstub"; printf '#!/bin/bash\necho "Studio_2 Pro"\n' > "$T/fbstub/scutil"; chmod +x "$T/fbstub/scutil"
[ "$(FB_EVAL='machine_id' fb PATH="$T/fbstub:$PATH" FLEET_MANAGED_PREFS="$T/none")" = "studio-2-pro" ] && ok "the host name becomes a deployment-id-shaped machine id" || bad "machine id derived" "$(FB_EVAL='machine_id' fb PATH="$T/fbstub:$PATH" FLEET_MANAGED_PREFS="$T/none")"
# The privileged binaries come from Homebrew's verified bottle, chosen for this macOS — never from the user's tree.
cat > "$T/tailscale.json" <<'JSON'
{"versions":{"stable":"1.104.5"},"bottle":{"stable":{"files":{
 "arm64_golden_gate":{"url":"https://ghcr.io/v2/homebrew/core/tailscale/blobs/sha256:aaa","sha256":"aaa"},
 "arm64_tahoe":{"url":"https://ghcr.io/v2/homebrew/core/tailscale/blobs/sha256:bbb","sha256":"bbb"},
 "arm64_linux":{"url":"x","sha256":"x"}}}}}
JSON
TSJ="$(cat "$T/tailscale.json")"
[ "$(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; bottle_key "$1" 26' "$FB" "$TSJ")" = "arm64_tahoe" ] && ok "the bottle for this macOS release is chosen (26 → tahoe)" || bad "bottle key 26" "$(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; bottle_key "$1" 26' "$FB" "$TSJ")"
[ "$(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; bottle_key "$1" 99' "$FB" "$TSJ")" = "arm64_golden_gate" ] && ok "an unknown release takes the newest arm64 macOS bottle, never linux" || bad "bottle key fallback" "$(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; bottle_key "$1" 99' "$FB" "$TSJ")"
[ "$(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; formula_field "$1" sha256 arm64_tahoe' "$FB" "$TSJ")" = "bbb" ] && [ "$(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; formula_field "$1" version' "$FB" "$TSJ")" = "1.104.5" ] \
  && ok "the formula's version and the bottle's sha256 are read for verification" || bad "formula fields" "wrong"
grep -q '<string>/Library/MeshWeaver/tailscale/bin/tailscaled</string>' "$T/ts.plist" && ! grep -q 'homebrew' "$T/ts.plist" \
  && ok "the root daemon runs tailscaled from the root-owned directory, never the user's Homebrew tree" || bad "daemon binary path" "$(grep tailscaled "$T/ts.plist")"
grep -q 'brew" tap systemorph/memex' "$FB" && grep -q 'brew" trust systemorph/memex' "$FB" && ok "the third-party tap is tapped AND trusted before memex-local is installed" || bad "tap trust" "missing"
grep -q 'tarball/main' "$FB" && ! grep -q 'tarball/master' "$FB" && ok "Homebrew comes from its supported main tree" || bad "homebrew branch" "master"
env FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/tlog" bash -c '. "$0"; log "open https://memex.localhost:8443/setup?token=abc123 now"' "$FB" > "$T/tok.out" 2>&1
! grep -q 'abc123' "$T/tok.out" "$T/tlog/fleet-bootstrap.log" && grep -q 'token=<withheld>' "$T/tok.out" && ok "a token-bearing URL never reaches the output or the log" || bad "token withheld" "$(cat "$T/tok.out")"
[ "$(ls -l "$T/tlog/fleet-bootstrap.log" | cut -c1-10)" = "-rw-------" ] && ok "the run log is readable by root only" || bad "log mode" "$(ls -l "$T/tlog/fleet-bootstrap.log")"
grep -q "WAITING for the first-run setup" "$FB" && ok "a portal still in first-run setup is reported WAITING; nothing is joined or finalised" || bad "setup wait" "missing"
# Regression: the setup probe sources the INSTALLED libexec CLI, never Homebrew's exec-ing bin/ wrapper.
mkdir -p "$T/brewfake/bin" "$T/brewfake/opt/memex-local/libexec/share"
printf '#!/bin/bash\nexec "%s/brewfake/opt/memex-local/libexec/memex-local" "$@"\n' "$T" > "$T/brewfake/bin/memex-local"
printf '#!/bin/bash\nawaiting_setup() { [ "${FAKE_AWAITING:-}" = 1 ]; }\n[ "${MEMEX_LOCAL_LIB:-}" = 1 ] || echo dispatched\n' > "$T/brewfake/opt/memex-local/libexec/memex-local"
chmod +x "$T/brewfake/bin/memex-local" "$T/brewfake/opt/memex-local/libexec/memex-local"
lib="$(env FLEET_BOOTSTRAP_LIB=1 FLEET_BREW_PREFIX="$T/brewfake" bash -c '. "$0"; memex_libexec' "$FB")"
[ "$lib" = "$T/brewfake/opt/memex-local/libexec" ] && ok "the setup probe resolves the installed libexec CLI behind the wrapper" || bad "memex_libexec" "$lib"
if env MEMEX_LOCAL_LIB=1 FAKE_AWAITING=0 /bin/bash -c '. "$0"; awaiting_setup' "$lib/memex-local"; then bad "a configured portal is not 'awaiting setup'" "true"; else ok "sourcing the libexec CLI: a configured portal is not 'awaiting setup'"; fi
env MEMEX_LOCAL_LIB=1 FAKE_AWAITING=1 /bin/bash -c '. "$0"; awaiting_setup' "$lib/memex-local" && ok "sourcing the libexec CLI: a portal in first-run setup is" || bad "awaiting setup detected" "false"
grep -q '"$(memex_libexec)/memex-local"' "$FB" && ok "the bootstrap sources the libexec CLI, not bin/memex-local" || bad "bootstrap sources libexec" "missing"
grep -q 'TS_CHANGED:-0' "$FB" && grep -q 'TS_CHANGED=1' "$FB" && ok "an upgraded tailscaled is reloaded, not left running the old binary" || bad "reload after upgrade" "missing"
grep -q 'fleet.finalized' "$FB" && ok "a join's finalisation is recorded by a marker written only after it completed" || bad "finalisation marker" "missing"
grep -q 'TRUST_STORES=nss' "$FB" && grep -q 'WAITING for the local CA to be trusted' "$FB" && ok "the local CA is created without the keychain prompt, and up waits until it is trusted" || bad "CA trust wait" "missing"
mkdir -p "$T/clt/usr/bin" && printf '#!/bin/sh\n' > "$T/clt/usr/bin/clang" && chmod +x "$T/clt/usr/bin/clang"
env FLEET_BOOTSTRAP_LIB=1 FLEET_CLT_DIR="$T/clt" FLEET_LOG_DIR="$T/clog" bash -c '. "$0"; step_devtools' "$FB" | grep -q 'developer tools: present' \
  && ok "installed Command Line Tools are detected and left alone" || bad "developer tools present" "$(env FLEET_BOOTSTRAP_LIB=1 FLEET_CLT_DIR="$T/clt" FLEET_LOG_DIR="$T/clog" bash -c '. "$0"; step_devtools' "$FB" 2>&1)"
mkdir -p "$T/kh/enrol" "$T/kh/held" && printf 'k' > "$T/kh/enrol/studio-2.preauth"
env FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/klog" bash -c '. "$0"; take_preauth_key "$1" "$2" "$(id -un)"' "$FB" "$T/kh/enrol/studio-2.preauth" "$T/kh/held/a.key" \
  && [ -f "$T/kh/held/a.key" ] && [ ! -e "$T/kh/enrol/studio-2.preauth" ] \
  && ok "an Intune run takes the dropped pre-auth key into root's state directory (renamed, then read from there)" || bad "take pre-auth key" "$(ls -la "$T/kh/enrol" "$T/kh/held")"
ln -sf /etc/hosts "$T/kh/enrol/studio-3.preauth"
! env FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/klog" bash -c '. "$0"; take_preauth_key "$1" "$2" "$(id -un)"' "$FB" "$T/kh/enrol/studio-3.preauth" "$T/kh/held/b.key" \
  && [ ! -e "$T/kh/held/b.key" ] && [ ! -L "$T/kh/held/b.key" ] && [ -f /etc/hosts ] \
  && ok "a symlinked pre-auth key is refused AFTER the rename (no swap window) and removed; its target is untouched" || bad "symlink key refused" "$(ls -la "$T/kh/held")"
env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; take_preauth_key "$1" "$2" "$(id -un)"' "$FB" "$T/kh/enrol/none.preauth" "$T/kh/held/c.key" && [ ! -e "$T/kh/held/c.key" ] \
  && ok "no dropped key: nothing taken, no failure" || bad "no key" "failed"
grep -q 'softwareupdate said:' "$FB" && ok "a failed Command Line Tools install logs what softwareupdate said" || bad "softwareupdate output logged" "missing"
mkdir -p "$T/shh"
SO="$(env FLEET_BOOTSTRAP_LIB=1 FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_USER_HOME="$T/shh" FLEET_BREW_PREFIX="$T/brewx" FLEET_LOG_DIR="$T/shlog" bash -c '. "$0"; ensure_shellenv jroe' "$FB" 2>&1)"
case "$SO" in *"adding the shellenv line to $T/shh/.zprofile"*"DRY-RUN"*) ok "the bootstrap puts Homebrew's shellenv into the person's ~/.zprofile (a new Terminal finds the tools)" ;; *) bad "shellenv written" "$SO" ;; esac
printf 'eval "$(%s/bin/brew shellenv)"\n' "$T/brewx" > "$T/shh/.zprofile"
[ -z "$(env FLEET_BOOTSTRAP_LIB=1 FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_USER_HOME="$T/shh" FLEET_BREW_PREFIX="$T/brewx" FLEET_LOG_DIR="$T/shlog" bash -c '. "$0"; ensure_shellenv jroe' "$FB" 2>&1)" ] \
  && ok "an existing shellenv line is left alone (idempotent)" || bad "shellenv idempotent" "rewritten"
AS="$(env FLEET_BOOTSTRAP_LIB=1 FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_LOG_DIR="$T/aslog" FLEET_MANAGED_PREFS="$T/none" FLEET_ORGANISATION=Acme FLEET_OWNER_NAME="Jane Roe" \
  FLEET_OWNER_EMAIL=jane.roe@example.com FLEET_ACCEPT_TERMS=true bash -c '. "$0"; answer_setup jroe studio-2 /opt/x/memex-local' "$FB" 2>&1)"; RC=$?
case "$RC:$OUT$AS" in 0:*"DRY-RUN: sudo -u jroe -H env"*"/opt/x/memex-local setup --apply --name studio-2 --company Acme --owner-name Jane\ Roe --owner-email jane.roe@example.com --accept-terms"*"setup answered"*) ok "a fleet Mac never waits for a person at the wizard: the bootstrap answers the setup API from the profile, as the console user (executed)" ;; *) bad "unattended setup executed" "rc=$RC: $AS" ;; esac
AS2="$(env FLEET_BOOTSTRAP_LIB=1 FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_LOG_DIR="$T/aslog" FLEET_MANAGED_PREFS="$T/none" FLEET_ORGANISATION=Acme FLEET_OWNER_NAME="Jane Roe" \
  FLEET_OWNER_EMAIL=jane.roe@example.com bash -c '. "$0"; answer_setup jroe studio-2 /opt/x/memex-local' "$FB" 2>&1)"; RC=$?
case "$RC:$AS2" in 0:*"WAITING for the first-run setup answers"*) case "$AS2" in *"setup --apply"*) bad "no AcceptTerms → nothing answered" "$AS2" ;; *) ok "without AcceptTerms=true nothing is answered, and the run says what it waits for" ;; esac ;; *) bad "no AcceptTerms → waits" "rc=$RC: $AS2" ;; esac
! grep -q "runs 'memex-local setup'" "$FB" && ok "the bootstrap never asks a person to run memex-local setup" || bad "no person at the wizard" "still asks"
grep -q 'installondemand.in-progress' "$FB" && grep -q 'rm -f "$flag"' "$FB" && ok "the on-demand flag is set for the catalogue and always removed" || bad "on-demand flag" "missing"
grep -q -- '--auth-key="file:$key_file"' "$FB" && ! grep -q -- '--auth-key "\$key"' "$FB" \
  && ok "the pre-auth key is passed by FILE reference (never an argument)" || bad "auth key by file" "missing"
# Enrolment v2: the join POLLS control for its own code (bounded per run), and a successful join joins the network at once.
grep -q -- 'fleet join --control "$control" --id "$machine" --no-open --wait "$wait_min" --code-file "$code_file" ${extra\[@\]+"${extra\[@\]}"} ${PART_ARGS' "$FB" \
  && ok "the bootstrap runs the POLLING join (bounded wait per run; the code file stays the fallback)" || bad "polling join" "missing"
awk '/^run_join\(\)/{on=1} on' "$FB" | grep -A10 '^    0) ' | grep -q 'step_network "$machine" "$user"' \
  && ok "a successful join hands its pre-auth key straight to the network step (no wait for the next run)" || bad "network after join" "missing"
mkdir -p "$T/lh/u/.memex-local/enrol" && printf 'https://hs.example.test\n' > "$T/lh/u/.memex-local/enrol/studio-9.login-server"
LS="$(env FLEET_BOOTSTRAP_LIB=1 FLEET_USER_HOME="$T/lh/u" FLEET_MANAGED_PREFS="$T/none" FLEET_LOG_DIR="$T/lslog" FLEET_TAILSCALED_SOCKET="$T/no.sock" FLEET_TS_DIR="$T/nots" \
  bash -c '. "$0"; step_network studio-9 "$(id -un)"' "$FB" 2>&1)"
case "$LS" in *"WAITING for a single-use pre-auth key"*) ok "with the login server named by the code but no key yet, the network step still waits" ;; *) bad "login-server fallback, no key" "$LS" ;; esac
grep -q '\.login-server' "$FB" && ok "the network step reads the login server the enrolment code named when the profile names none" || bad "login-server fallback" "missing"
# The dry runs are HERMETIC: the owner's answers given (no), and no password policy read from this machine.
printf '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict/></plist>\n' > "$T/nopol.plist"
FB_HERMETIC="FLEET_ACCEPT_JOBS=false FLEET_ACCEPT_TOKENS=false FLEET_ACCOUNT_POLICIES_FILE=$T/nopol.plist FLEET_USER_ACCOUNT_POLICIES_FILE=$T/nopol.plist FLEET_ACCOUNT_POLICY_DATA_FILE=$T/nopol.plist"
# A dry run on a bare Mac: every changing command is printed, nothing runs, and it waits (exit 0) for the key and the control URL.
OUT="$(env FLEET_TS_FORMULA_API="file://$T/tailscale.json" FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_CONSOLE_USER=jroe FLEET_USER_HOME="$T/home" FLEET_BREW_PREFIX="$T/brew" FLEET_LOG_DIR="$T/logs" \
  FLEET_TS_DIR="$T/tsbin" FLEET_LAUNCHDAEMONS_DIR="$T/ld" FLEET_TAILSCALED_SOCKET="$T/no.sock" FLEET_TAILSCALED_STATE_DIR="$T/ts" FLEET_MANAGED_PREFS="$T/none" FLEET_MACHINE_ID=studio-2 $FB_HERMETIC \
  FLEET_CLT_DIR="$T/noclt" bash "$FB" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*"homebrew: installing"*"developer tools: installing the Command Line Tools"*"DRY-RUN: softwareupdate -i"*"tools: installing"*) ok "a bare Mac gets the Command Line Tools (headless softwareupdate) BEFORE the tools: memex-local builds from source" ;; *) bad "bootstrap installs the developer tools first" "rc=$RC: $OUT" ;; esac
case "$RC:$OUT" in 0:*"homebrew: installing"*"tools: installing"*"tailscale: installing 1.104.5 from the verified bottle"*"tailscaled: (re)loading the root LaunchDaemon"*"network: WAITING"*"memex: WAITING for ControlUrl"*"done"*) ok "a dry run on a bare Mac plans every step (tap trusted, binaries from the verified bottle) and waits, exit 0" ;; *) bad "bootstrap dry run" "rc=$RC: $OUT" ;; esac
case "$OUT" in *"tools: installing "*" lego "*) ok "a bare Mac gets lego with the tools (cert issue needs it; no person runs brew install lego)" ;; *) bad "bootstrap installs lego" "$(grep 'tools: installing' <<<"$OUT")" ;; esac
case "$OUT" in *"DRY-RUN: mkdir -p $T/brew"*"DRY-RUN: write $T/ld/com.meshweaver.fleet.tailscaled.plist"*) ok "the dry run prints the changing commands instead of running them" ;; *) bad "dry-run commands" "$OUT" ;; esac
[ ! -e "$T/brew" ] && [ ! -e "$T/ld/com.meshweaver.fleet.tailscaled.plist" ] && ok "the dry run changed nothing" || bad "dry run changed the machine" "$(ls "$T")"
grep -q 'network: WAITING' "$T/logs/fleet-bootstrap.log" && ok "the run is logged to /Library/Logs/MeshWeaver/fleet-bootstrap.log" || bad "bootstrap log" "$(cat "$T/logs/fleet-bootstrap.log" 2>/dev/null)"
OUT="$(env FLEET_TS_FORMULA_API="file://$T/tailscale.json" FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_CONSOLE_USER=jroe FLEET_USER_HOME="$T/home" FLEET_BREW_PREFIX="$T/brew" FLEET_LOG_DIR="$T/logs" \
  FLEET_TS_DIR="$T/tsbin" FLEET_LAUNCHDAEMONS_DIR="$T/ld" FLEET_TAILSCALED_SOCKET="$T/no.sock" FLEET_TAILSCALED_STATE_DIR="$T/ts" FLEET_MANAGED_PREFS="$T/none" FLEET_MACHINE_ID=studio-2 $FB_HERMETIC \
  FLEET_CONTROL_URL=https://control.example.test bash "$FB" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*"memex: provisioning the local stack"*"the join follows once the stack verifies"*) ok "a dry run with a control URL plans the provisioning from real probes" ;; *) bad "dry run with ControlUrl" "rc=$RC: $OUT" ;; esac
case "$OUT" in *"joined as"*) bad "a dry run never claims a join" "$OUT" ;; *) ok "a dry run never claims a join" ;; esac
OUT="$(env FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_CONSOLE_USER=root FLEET_LOG_DIR="$T/logs" bash "$FB" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*"no one is logged in"*) ok "no one at the console: nothing to do, exit 0" ;; *) bad "no console user" "rc=$RC: $OUT" ;; esac

echo "── the second Mac Studio's defects, pinned (2026-11-09) ─────────────"
# Defect 2 (bootstrap 1.3.2, Deploy #214): the Command Line Tools come FIRST — a failed install stops the run before
# `brew install` (which would refuse with "No developer tools installed").
OUT="$(env FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/d2log" bash -c '. "$0"; step_homebrew() { log "homebrew: present"; }; step_devtools() { log "developer tools: FAILED (test)"; return 1; }
  step_tools() { log "tools: REACHED"; }; failed=0; step_homebrew u || failed=1; [ "$failed" = 0 ] && { step_devtools || failed=1; }; [ "$failed" = 0 ] && { step_tools u || failed=1; }; echo "failed=$failed"' "$FB" 2>&1)"
case "$OUT" in *"tools: REACHED"*) bad "a failed developer-tools install stops the run before the tools" "$OUT" ;; *"failed=1"*) ok "a failed developer-tools install stops the run before 'brew install' (defect 2)" ;; *) bad "devtools ordering" "$OUT" ;; esac
awk '/^main\(\) \{/{on=1} on' "$FB" | grep -n 'step_devtools\|step_tools "\$user"' | awk -F: 'NR==1 && $0 ~ /devtools/ {d=1} END {exit !d}' \
  && ok "main runs step_devtools before step_tools (defect 2)" || bad "main order devtools → tools" "$(awk '/^main\(\) \{/{on=1} on' "$FB")"

# Defect 5: Homebrew on every PATH the bootstrap hands the person's commands (the login shell: ensure_shellenv, #217).
up="$(FB_EVAL='user_path' fb)"
case ":$up:" in *":/opt/homebrew/bin:"*"/usr/local/bin:"*) ok "commands run as the console user get /opt/homebrew/bin and /usr/local/bin on PATH (defect 5)" ;; *) bad "user_path" "$up" ;; esac
grep -q 'PATH="\$(user_path)"' "$FB" && [ "$(grep -c 'PATH="\$(user_path)"' "$FB")" -ge 2 ] && grep -A1 '^as_user()' "$FB" >/dev/null && ! grep -q 'PATH="\$BREW_PREFIX/bin:\$TS_DIR' "$FB" \
  && ok "as_user and probe_as_user both use that PATH" || bad "as_user PATH" "$(grep -n 'PATH=' "$FB")"
# Defect 1: an expired console password is the FIRST line of the run, with the remedy.
mkpol() { cat > "$1" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>policyCategoryPasswordChange</key><array>
<dict><key>policyContent</key><string>(policyAttributeLastPasswordChangeTime &lt; policyAttributeNewPasswordRequiredTime) and (policyAttributeCurrentTime &gt;= policyAttributeNewPasswordRequiredTime)</string>
<key>policyIdentifier</key><string>ProfilePayload:test:changeAtNextAuth</string>
<key>policyParameters</key><dict><key>policyAttributeNewPasswordRequiredTime</key><real>$2</real></dict></dict>
$3
</array></dict></plist>
PL
}
mkapd() { printf '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>passwordLastSetTime</key><real>%s</real></dict></plist>\n' "$2" > "$1"; }
mkpol "$T/pol.plist" 1000.5 ""
mkapd "$T/apd-old.plist" 500.25; mkapd "$T/apd-new.plist" 1500.0
pv() { env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; password_expiry_verdict "$1" "$2" "$3"' "$FB" "$@"; }
case "$(pv "$T/pol.plist" "$T/apd-old.plist" 2000)" in *"REQUIRED"*"changeAtNextAuth"*) ok "changeAtNextAuth past, password set before it → a change is due" ;; *) bad "changeAtNextAuth due" "$(pv "$T/pol.plist" "$T/apd-old.plist" 2000)" ;; esac
[ -z "$(pv "$T/pol.plist" "$T/apd-new.plist" 2000)" ] && ok "a password changed after the policy's moment is not expired" || bad "changed after" "$(pv "$T/pol.plist" "$T/apd-new.plist" 2000)"
[ -z "$(pv "$T/pol.plist" "$T/apd-old.plist" 900)" ] && ok "a policy moment still in the future is not a verdict yet" || bad "future moment" ""
[ -z "$(pv "$T/pol.plist" "$T/nopol.plist" 2000)" ] && ok "no readable last-set time → no verdict (never a guess)" || bad "unreadable apd" ""
mkpol "$T/pol-age.plist" 1 '<dict><key>policyIdentifier</key><string>age</string><key>policyParameters</key><dict><key>policyAttributeExpiresEveryNDays</key><integer>1</integer></dict></dict>'
case "$(pv "$T/pol-age.plist" "$T/apd-new.plist" $(( 1500 + 86400 )))" in *"EXPIRED"*"1 days"*) ok "a password older than policyAttributeExpiresEveryNDays has expired" ;; *) bad "age expiry" "$(pv "$T/pol-age.plist" "$T/apd-new.plist" $(( 1500 + 86400 )))" ;; esac
OUT="$(env FLEET_TS_FORMULA_API="file://$T/tailscale.json" FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_CONSOLE_USER=jroe FLEET_USER_HOME="$T/home" FLEET_BREW_PREFIX="$T/brew" FLEET_LOG_DIR="$T/pwlog" \
  FLEET_TS_DIR="$T/tsbin" FLEET_LAUNCHDAEMONS_DIR="$T/ld" FLEET_TAILSCALED_SOCKET="$T/no.sock" FLEET_TAILSCALED_STATE_DIR="$T/ts" FLEET_MANAGED_PREFS="$T/none" FLEET_MACHINE_ID=studio-2 \
  FLEET_ACCEPT_JOBS=false FLEET_ACCEPT_TOKENS=false FLEET_ACCOUNT_POLICIES_FILE="$T/pol.plist" FLEET_USER_ACCOUNT_POLICIES_FILE="$T/nopol.plist" FLEET_ACCOUNT_POLICY_DATA_FILE="$T/apd-old.plist" FLEET_NOW=2000 \
  FLEET_CLT_DIR="$T/clt" bash "$FB" 2>&1)"
case "$(printf '%s\n' "$OUT" | head -1)" in *"PASSWORD EXPIRED for jroe"*"REMEDY"*"console"*) ok "an expired console password is the FIRST line of the run, with the remedy (defect 1)" ;; *) bad "password first line" "$OUT" ;; esac
case "$(head -1 "$T/pwlog/fleet-bootstrap.log")" in *"PASSWORD EXPIRED"*) ok "…and the first line of the run's log" ;; *) bad "password first log line" "$(head -1 "$T/pwlog/fleet-bootstrap.log")" ;; esac

# Participation (contract §5): both answers REQUIRED — never defaulted — and passed to fleet join.
rp() { env FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/plog" FLEET_MANAGED_PREFS="$T/none" "$@" bash -c '. "$0"; read_participation; rc=$?; printf "rc=%s jobs=%s tokens=%s args=%s\n" "$rc" "$PART_JOBS" "$PART_TOKENS" "${PART_ARGS[*]-}"' "$FB" 2>&1; }
OUT="$(rp)"
case "$OUT" in *"AcceptJobs is absent"*"AcceptTokens is absent"*"rc=1"*) ok "no AcceptJobs and no AcceptTokens → FAILS, naming both" ;; *) bad "participation absent" "$OUT" ;; esac
OUT="$(rp FLEET_ACCEPT_JOBS=maybe FLEET_ACCEPT_TOKENS=false)"
case "$OUT" in *"AcceptJobs is not a boolean ('maybe')"*"rc=1"*) ok "an answer that is not a boolean FAILS (never read as no)" ;; *) bad "participation maybe" "$OUT" ;; esac
OUT="$(rp FLEET_ACCEPT_JOBS=true FLEET_ACCEPT_TOKENS=false)"
case "$OUT" in *"AcceptJobsFrom is empty"*"rc=1"*) ok "jobs YES without AcceptJobsFrom FAILS" ;; *) bad "jobs without from" "$OUT" ;; esac
OUT="$(rp FLEET_ACCEPT_JOBS=false FLEET_ACCEPT_TOKENS=true FLEET_ACCEPT_TOKENS_FROM='*.meshweaver.cloud')"
case "$OUT" in *"AcceptTokensModels is empty"*"rc=1"*) ok "tokens YES without models FAILS" ;; *) bad "tokens without models" "$OUT" ;; esac
OUT="$(rp FLEET_ACCEPT_JOBS=0 FLEET_ACCEPT_TOKENS=no)"
case "$OUT" in *"rc=0 jobs=false tokens=false args=--no-accept-jobs --no-buildhost --no-accept-tokens"*) ok "jobs NO → --no-accept-jobs --no-buildhost (no build host); tokens NO → --no-accept-tokens" ;; *) bad "participation no/no" "$OUT" ;; esac
OUT="$(rp FLEET_ACCEPT_JOBS=1 FLEET_ACCEPT_JOBS_FROM=control.example.test FLEET_ACCEPT_JOBS_CHANNELS=latest FLEET_ACCEPT_JOBS_REPOS=acme/plugins \
  FLEET_ACCEPT_TOKENS=true FLEET_ACCEPT_TOKENS_FROM='*.example.cloud' FLEET_ACCEPT_TOKENS_MODELS='qwen3.8:27b-mlx' FLEET_ACCEPT_TOKENS_RESIDENCY=CH)"
case "$OUT" in *"rc=0 jobs=true tokens=true args=--accept-jobs=control.example.test --job-channels latest --job-repos acme/plugins --accept-tokens=*.example.cloud --token-models qwen3.8:27b-mlx --residency CH"*) ok "YES answers become --accept-jobs/--job-channels/--job-repos and --accept-tokens/--token-models/--residency" ;; *) bad "participation yes/yes" "$OUT" ;; esac
# …read from a real managed-preference plist: a boolean and an ARRAY, as Intune writes them.
mkdir -p "$T/mp"
cat > "$T/mp/com.memex.local.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>AcceptJobs</key><false/>
<key>AcceptTokens</key><true/>
<key>AcceptTokensFrom</key><array><string>*.example.cloud</string><string>control.example.test</string></array>
<key>AcceptTokensModels</key><array><string>qwen3.8:27b-mlx</string></array>
</dict></plist>
PL
OUT="$(env FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/plog" FLEET_MANAGED_PREFS="$T/mp/com.memex.local" bash -c '. "$0"; read_participation; printf "rc=%s args=%s\n" "$?" "${PART_ARGS[*]-}"' "$FB" 2>&1)"
case "$OUT" in *"rc=0 args=--no-accept-jobs --no-buildhost --accept-tokens=*.example.cloud,control.example.test --token-models qwen3.8:27b-mlx"*) ok "the profile's boolean and array preferences are read as Intune writes them" ;; *) bad "managed-pref participation" "$OUT" ;; esac
OUT="$(env FLEET_TS_FORMULA_API="file://$T/tailscale.json" FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_CONSOLE_USER=jroe FLEET_USER_HOME="$T/home" FLEET_BREW_PREFIX="$T/brew" FLEET_LOG_DIR="$T/palog" \
  FLEET_MANAGED_PREFS="$T/none" FLEET_MACHINE_ID=studio-2 FLEET_ACCOUNT_POLICIES_FILE="$T/nopol.plist" FLEET_USER_ACCOUNT_POLICIES_FILE="$T/nopol.plist" FLEET_ACCOUNT_POLICY_DATA_FILE="$T/nopol.plist" bash "$FB" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "a run without the owner's answers exits 0" "$OUT" ;; *"AcceptJobs is absent"*) case "$OUT" in *"homebrew:"*) bad "nothing is installed before the answers" "$OUT" ;; *) ok "a run without the owner's answers exits non-zero BEFORE installing anything, naming the keys" ;; esac ;; *) bad "unanswered run" "rc=$RC $OUT" ;; esac

# Defect 4 (Mac half): the fleet local CA, trusted by profile, ends the wait on `mkcert -install`.
MINT="$MACOS/fleet-ca/mint-fleet-ca.sh"
bash -n "$MINT" && ok "mint-fleet-ca.sh parses" || bad "mint parses" "$(bash -n "$MINT" 2>&1)"
OUT="$(bash "$MINT" --key-file "$T/fca.key" --cert-out "$T/fca.pem" 2>&1)"; RC=$?
FSHA="$(openssl x509 -in "$T/fca.pem" -outform DER | shasum -a 256 | cut -d' ' -f1)"
KEYB64="$(cat "$T/fca.key")"
[ "$RC" = 0 ] && [ "$(ls -l "$T/fca.key" | cut -c1-10)" = "-rw-------" ] && ok "mint --key-file writes the key to a NEW 0600 file" || bad "mint key file" "rc=$RC $OUT"
case "$OUT" in *"$KEYB64"*|*"PRIVATE KEY"*) bad "mint never prints the key" "$OUT" ;; *"$FSHA"*) ok "mint prints the certificate's sha256 (FleetCaSha256), never the key" ;; *) bad "mint output" "$OUT" ;; esac
openssl base64 -d -A <<<"$KEYB64" | openssl pkey -inform DER -noout -text 2>/dev/null | grep -q 'prime256v1\|P-256' && ok "the key is base64 PKCS#8 DER of an EC P-256 key (the enrolment item's form)" || bad "key form" ""
CT="$(openssl x509 -in "$T/fca.pem" -noout -text)"
case "$CT" in *"Basic Constraints: critical"*"CA:TRUE, pathlen:0"*"Name Constraints: critical"*"Permitted:"*"DNS:localhost"*"IP:127.0.0.1/255.255.255.255"*"IP:0:0:0:0:0:0:0:1/FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF"*) ok "the CA is critical-name-constrained to localhost, 127.0.0.1 and ::1, pathlen 0" ;; *) bad "CA constraints" "$CT" ;; esac
case "$CT" in *"Excluded"*|*"DNS:memex"*|*"DNS:*"*) bad "no other names permitted" "$CT" ;; *) ok "nothing else is permitted" ;; esac
OUT="$(bash "$MINT" --key-file "$T/fca.key" --cert-out "$T/fca2.pem" 2>&1)" && bad "mint overwrote an existing key file" "$OUT" || ok "mint refuses to overwrite an existing key"
mkdir -p "$T/azb"; cat > "$T/azb/az" <<'AZ'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_AZ_LOG"
case "$*" in *"secret show"*) exit "${FAKE_AZ_EXISTS:-1}" ;; esac
prev=""; for a in "$@"; do [ "$prev" = "--file" ] && { cp "$a" "$FAKE_AZ_VALUE"; ls -l "$a" | cut -c1-10 > "$FAKE_AZ_VALUE.mode"; }; prev="$a"; done
AZ
chmod +x "$T/azb/az"
OUT="$(env PATH="$T/azb:$PATH" FAKE_AZ_LOG="$T/az.log" FAKE_AZ_VALUE="$T/az.value" bash "$MINT" --vault fleetvault --object fleet-local-ca-key --subscription acme-sub --cert-out "$T/fca3.pem" 2>&1)"; RC=$?
V="$(cat "$T/az.value" 2>/dev/null)"
[ "$RC" = 0 ] && [ -n "$V" ] && grep -q -- 'secret set --subscription acme-sub --vault-name fleetvault --name fleet-local-ca-key --file ' "$T/az.log" && [ "$(grep -c -- '--subscription acme-sub' "$T/az.log")" = "$(wc -l < "$T/az.log" | tr -d ' ')" ] && [ "$(cat "$T/az.value.mode")" = "-rw-------" ] \
  && ok "mint --vault hands the key to 'az keyvault secret set --file' through a 0600 file" || bad "mint to vault" "rc=$RC $OUT $(cat "$T/az.log")"
case "$OUT$(cat "$T/az.log")" in *"$V"*) bad "the key is never printed nor an argument" "" ;; *) ok "the key is never printed nor an argument" ;; esac
OUT="$(env PATH="$T/azb:$PATH" FAKE_AZ_LOG="$T/az.log" FAKE_AZ_VALUE="$T/az2.value" FAKE_AZ_EXISTS=0 bash "$MINT" --vault fleetvault --object fleet-local-ca-key --subscription acme-sub 2>&1)" \
  && bad "mint replaced an existing vault object" "$OUT" || { [ ! -e "$T/az2.value" ] && ok "mint refuses to replace an existing vault object (a second CA orphans every Mac's trust)" || bad "vault replace refused" ""; }
: > "$T/az.log"
OUT="$(env PATH="$T/azb:$PATH" FAKE_AZ_LOG="$T/az.log" FAKE_AZ_VALUE="$T/az3.value" bash "$MINT" --vault fleetvault --object fleet-local-ca-key 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "vault mode without --subscription is refused" "$OUT" ;; *"--subscription <name|id> is required"*"shared"*) [ ! -s "$T/az.log" ] && [ ! -e "$T/az3.value" ] && ok "vault mode without --subscription is REFUSED before any az call, naming why (the shared login's default subscription)" || bad "no az call without subscription" "$(cat "$T/az.log")" ;; *) bad "subscription refusal" "rc=$RC $OUT" ;; esac
# the System keychain as `security find-certificate -a -Z -p` prints it: another certificate first, then the fleet CA
{ printf 'SHA-256 hash: %s\nSHA-1 hash: 00\n' "$(printf 'a%.0s' $(seq 64) | tr a-f A-F)"; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout /dev/null -subj /CN=other -days 1 2>/dev/null
  printf 'SHA-256 hash: %s\nSHA-1 hash: 00\n' "$(printf '%s' "$FSHA" | tr a-f A-F)"; cat "$T/fca.pem"; } > "$T/keychain.txt"
diff <(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; pem_for_sha256 "$1" < "$2"' "$FB" "$FSHA" "$T/keychain.txt") "$T/fca.pem" >/dev/null \
  && ok "the fleet CA's certificate is found in the keychain listing by its sha256" || bad "pem_for_sha256" ""
mkdir -p "$T/secb"; cat > "$T/secb/security" <<'SEC'
#!/bin/bash
case "$1" in
  find-certificate) cat "$FAKE_KEYCHAIN" ;;
  verify-cert) f=""; prev=""; for a in "$@"; do [ "$prev" = "-c" ] && f="$a"; prev="$a"; done
               [ -n "${FAKE_TRUSTED_PEM:-}" ] && cmp -s "$f" "$FAKE_TRUSTED_PEM" ;;
  *) exit 1 ;;
esac
SEC
chmod +x "$T/secb/security"
FCA_ENV="PATH=$T/secb:$PATH FAKE_KEYCHAIN=$T/keychain.txt FAKE_TRUSTED_PEM=$T/fca.pem"
[ "$(env $FCA_ENV FLEET_CA_SHA256="$FSHA" FLEET_MANAGED_PREFS="$T/none" FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; fleet_ca_trusted_by_profile' "$FB")" = "$FSHA" ] \
  && ok "a fleet CA in the System keychain that this Mac trusts is detected (security verify-cert)" || bad "fleet CA detected" ""
env PATH="$T/secb:$PATH" FAKE_KEYCHAIN="$T/keychain.txt" FAKE_TRUSTED_PEM="$T/none.pem" FLEET_CA_SHA256="$FSHA" FLEET_MANAGED_PREFS="$T/none" FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; fleet_ca_trusted_by_profile' "$FB" >/dev/null \
  && bad "an UNTRUSTED fleet CA certificate is not 'trusted by profile'" "" || ok "an untrusted fleet CA certificate is not 'trusted by profile'"
env $FCA_ENV FLEET_MANAGED_PREFS="$T/none" FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; fleet_ca_trusted_by_profile' "$FB" >/dev/null \
  && bad "no FleetCaSha256 → no fleet CA" "" || ok "no declared FleetCaSha256 → no fleet CA (nothing is guessed from the keychain)"
# step_memex end to end, with the person's commands recorded instead of run (as_user/probe_as_user replaced).
mkdir -p "$T/sm/caroot" "$T/sm/home/.memex-local/enrol"; cp "$T/fca2.pem" "$T/sm/caroot/rootCA.pem" 2>/dev/null || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout /dev/null -out "$T/sm/caroot/rootCA.pem" -subj /CN=mkcert -days 1 2>/dev/null
SM='as_user() { shift; printf "%s\n" "$*" >> "$SM_CALLS"; case "$*" in *-CAROOT*) printf "%s" "$SM_CAROOT" ;; esac; return 0; }
    probe_as_user() { return 1; }
    read_participation >/dev/null; step_memex jroe studio-2; echo "rc=$?"'
smrun() { : > "$T/sm/calls"; OUT="$(env $FCA_ENV FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/smlog" FLEET_MANAGED_PREFS="$T/none" FLEET_USER_HOME="$T/sm/home" FLEET_BREW_PREFIX="$T/brew" \
  FLEET_CONTROL_URL=https://control.example.test SM_CALLS="$T/sm/calls" SM_CAROOT="$T/sm/caroot" FLEET_ACCEPT_JOBS=false FLEET_ACCEPT_TOKENS=false "$@" bash -c '. "$0"; '"$SM" "$FB" 2>&1)"; }
smrun FLEET_CA_SHA256="$FSHA"
case "$OUT" in *"fleet local CA is trusted by profile"*"not waiting on 'mkcert -install'"*"rc=0"*) ok "a fleet CA trusted by profile: no wait on 'mkcert -install' (defect 4)" ;; *) bad "fleet CA no wait" "$OUT" ;; esac
grep -q 'env MEMEX_CA_TRUST=defer .*/memex-local up$' "$T/sm/calls" && ok "…and 'up' runs with MEMEX_CA_TRUST=defer (no keychain change, no prompt)" || bad "up deferred" "$(cat "$T/sm/calls")"
grep -q 'fleet join --control https://control.example.test --id studio-2 --no-open --wait 10 --code-file .* --no-accept-jobs --no-buildhost --no-accept-tokens$' "$T/sm/calls" \
  && ok "fleet join gets the owner's answers: jobs NO → --no-accept-jobs --no-buildhost, tokens NO → --no-accept-tokens" || bad "join participation args" "$(cat "$T/sm/calls")"
printf '%s\n' "$KEYB64" > "$T/sm/home/.memex-local/enrol/fleet-ca.key"
smrun FLEET_CA_SHA256="$FSHA"
grep -q "memex-local ca adopt --key-file $T/sm/home/.memex-local/enrol/fleet-ca.key --sha256 $FSHA\$" "$T/sm/calls" && ! grep -q 'MEMEX_CA_TRUST=defer' "$T/sm/calls" \
  && ok "the fleet CA's key delivered → 'memex-local ca adopt' with the declared sha256, then a normal 'up'" || bad "adopt on delivery" "$(cat "$T/sm/calls")"
case "$OUT" in *"$KEYB64"*) bad "the bootstrap never logs the CA key" "" ;; *) ok "the bootstrap never logs the CA key" ;; esac
# a refused adopt: its reason reaches the bootstrap log (review on #223)
SMF='as_user() { shift; printf "%s\n" "$*" >> "$SM_CALLS"; case "$*" in *-CAROOT*) printf "%s" "$SM_CAROOT" ;; *"ca adopt"*) echo "err  ca adopt: the key does not belong to the certificate"; return 1 ;; esac; return 0; }
    probe_as_user() { return 1; }
    read_participation >/dev/null; step_memex jroe studio-2; echo "rc=$?"'
printf '%s\n' "$KEYB64" > "$T/sm/home/.memex-local/enrol/fleet-ca.key"
: > "$T/sm/calls"; OUT="$(env $FCA_ENV FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$T/smlog" FLEET_MANAGED_PREFS="$T/none" FLEET_USER_HOME="$T/sm/home" FLEET_BREW_PREFIX="$T/brew" \
  FLEET_CONTROL_URL=https://control.example.test SM_CALLS="$T/sm/calls" SM_CAROOT="$T/sm/caroot" FLEET_ACCEPT_JOBS=false FLEET_ACCEPT_TOKENS=false FLEET_CA_SHA256="$FSHA" bash -c '. "$0"; '"$SMF" "$FB" 2>&1)"
case "$OUT" in *"ca adopt' FAILED"*"memex:   err  ca adopt: the key does not belong to the certificate"*"rc=1"*) ok "a refused 'ca adopt' logs adopt's own reason and fails the step" ;; *) bad "adopt failure reason logged" "$OUT" ;; esac
rm -f "$T/sm/home/.memex-local/enrol/fleet-ca.key"
smrun
case "$OUT" in *"WAITING for the local CA to be trusted"*"rc=0"*) ok "no fleet CA and an untrusted mkcert CA: WAITING (unchanged), naming the fleet profile remedy" ;; *) bad "no fleet CA waits" "$OUT" ;; esac
! grep -q 'memex-local up' "$T/sm/calls" && ok "…and 'up' is not run while it waits" || bad "up while waiting" "$(cat "$T/sm/calls")"
echo "── Intune objects as code: the reconcile (deploy/macos/intune/intune-reconcile.py) ──"
RECON="$MACOS/intune/intune-reconcile.py"
OUT="$(python3 "$RECON" --self-test 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "the reconcile's self-test against a fake Graph passes ($(grep -c '^  ok' <<<"$OUT") cases)" || bad "intune-reconcile --self-test" "$OUT"
OUT="$(python3 "$RECON" --validate --spec "$MACOS/intune/fleet.spec.example.json" 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "the example fleet spec validates" || bad "fleet.spec.example.json invalid" "$OUT"
sed 's/"AcceptTokens": false,//; s/"AcceptTokens": true,//' "$MACOS/intune/fleet.spec.example.json" > "$T/no-answer.spec.json"
OUT="$(python3 "$RECON" --validate --spec "$T/no-answer.spec.json" 2>&1)"; RC=$?
case "$RC:$OUT" in 1:*"keys.AcceptTokens must be an explicit true/false"*) ok "a spec without the owner's token answer is refused (participation is never implied)" ;; *) bad "missing AcceptTokens accepted" "rc=$RC: $OUT" ;; esac

echo "── network: a restarted daemon is never re-keyed; a spent key never logs a working node out (2026-11-11) ──"
NT="$(mktemp -d)"; mkdir -p "$NT/ts/bin" "$NT/state" "$NT/u/.memex-local/enrol"
# A fake tailscale CLI: `status --json` answers the next state of $NT/states (one per line, the last repeats);
# `up` records its argv (never a key value — the key is a file reference) and answers $NT/up.out with $NT/up.rc.
cat > "$NT/ts/bin/tailscale" <<STUB
#!/bin/bash
shift   # --socket=…
case "\$1" in
  status) n=\$(cat "$NT/n" 2>/dev/null || echo 0); n=\$((n+1)); echo \$n > "$NT/n"
    st=\$(sed -n "\${n}p" "$NT/states"); [ -n "\$st" ] || st=\$(tail -1 "$NT/states")
    [ "\$st" = FAIL ] && { echo "Tailscale is starting" >&2; exit 1; }
    printf '{"BackendState": "%s", "HaveNodeKey": %s}\n' "\$st" "\$(cat "$NT/havekey" 2>/dev/null || echo false)"; [ "\$st" = Running ] ;;
  up) echo "\$*" >> "$NT/up.log"; cat "$NT/up.out" 2>/dev/null; exit "\$(cat "$NT/up.rc" 2>/dev/null || echo 0)" ;;
esac
STUB
chmod +x "$NT/ts/bin/tailscale"
nt() {  # $1 = states (space-separated); rest = extra env
  local st="$1"; shift; printf '%s\n' $st > "$NT/states"; rm -f "$NT/n" "$NT/up.log"
  env FLEET_BOOTSTRAP_LIB=1 FLEET_TS_DIR="$NT/ts" FLEET_TAILSCALED_SOCKET="$NT/sock" FLEET_TAILSCALED_STATE_DIR="$NT/state" \
    FLEET_USER_HOME="$NT/u" FLEET_LOG_DIR="$NT/log" FLEET_MANAGED_PREFS="$NT/none" FLEET_LOGIN_SERVER=https://hs.example.test FLEET_TS_SETTLE_S=4 "$@" \
    bash -c '. "$0"; step_network studio-2 "$(id -un)"; echo "RC=$?"' "$FB" 2>&1; }
# 1. THE RESTART RACE: status fails while the daemon starts, then Running — a held (spent) key is NOT presented, and goes
printf 'spent' > "$NT/state/preauth.key"; printf '{"_profiles":"x"}' > "$NT/state/tailscaled.state"
OUT="$(nt "FAIL Starting Running")"
[ ! -s "$NT/up.log" ] && grep -q 'network: joined (BackendState Running)' <<<"$OUT" && [ ! -e "$NT/state/preauth.key" ] \
  && ok "after a daemon restart the step WAITS for a settled state: Running → joined, no 'up', the held spent key removed (the second Mac Studio was logged out here)" || bad "restart race" "$OUT / up: $(cat "$NT/up.log" 2>/dev/null)"
# 2. a registered node that IS logged out, with only the spent key held: the key is removed, never presented
printf 'spent' > "$NT/state/preauth.key"
OUT="$(nt "NeedsLogin")"
[ ! -s "$NT/up.log" ] && [ ! -e "$NT/state/preauth.key" ] && grep -q 'network: OFFLINE — this node was logged in before' <<<"$OUT" \
  && ok "a logged-out registered node with only a spent held key: removed, no 'up', OFFLINE said with where a new key goes" || bad "stale key" "$OUT / $(cat "$NT/up.log" 2>/dev/null)"
# 3. …and a NEW key dropped by a person: used, WITHOUT --reset, and every copy removed after
printf 'fresh' > "$NT/u/.memex-local/enrol/studio-2.preauth"
OUT="$(nt "NeedsLogin")"
grep -q -- '--auth-key=file:' "$NT/up.log" && ! grep -q -- '--reset' "$NT/up.log" && [ ! -e "$NT/state/preauth.key" ] && [ ! -e "$NT/u/.memex-local/enrol/studio-2.preauth" ] \
  && ok "a NEW key on a registered node re-logs it in WITHOUT --reset, by file reference, and no copy remains" || bad "re-login" "$OUT / $(cat "$NT/up.log" 2>/dev/null)"
# 4. a node never logged in: --reset, the key removed after
rm -f "$NT/state/tailscaled.state"; printf 'fresh' > "$NT/u/.memex-local/enrol/studio-2.preauth"
OUT="$(nt "NeedsLogin")"
grep -q -- '--reset' "$NT/up.log" && [ ! -e "$NT/state/preauth.key" ] && grep -q 'RC=0' <<<"$OUT" \
  && ok "a fresh node joins with --reset and its key is removed" || bad "fresh join" "$OUT / $(cat "$NT/up.log" 2>/dev/null)"
# 5. the login server refuses the key (expired/used): removed, said, never a retry loop
printf 'old' > "$NT/u/.memex-local/enrol/studio-2.preauth"; printf 'backend error: authkey expired\n' > "$NT/up.out"; echo 1 > "$NT/up.rc"
OUT="$(nt "NeedsLogin")"
[ ! -e "$NT/state/preauth.key" ] && [ ! -e "$NT/u/.memex-local/enrol/studio-2.preauth" ] && grep -q 'REFUSED the pre-auth key (expired or already used)' <<<"$OUT" && grep -q 'RC=0' <<<"$OUT" \
  && ok "a refused (expired/used) key is removed and said, not retried every run" || bad "refused key" "$OUT"
# …and a failure that is NOT the key's (the network, the daemon) keeps the key for the next run
printf 'fresh' > "$NT/u/.memex-local/enrol/studio-2.preauth"; printf 'connection refused: the key was not valid at that moment of the network\n' > "$NT/up.out"
OUT="$(nt "NeedsLogin")"
[ -e "$NT/state/preauth.key" ] && grep -q 'FAILED to join — the key file is kept' <<<"$OUT" \
  && ok "an 'up' failure that is not a refusal of the key keeps the key (only the login server's key refusals remove it)" || bad "other failure" "$OUT"
rm -f "$NT/up.out" "$NT/up.rc" "$NT/state/preauth.key"
# 6. a daemon that never settles: nothing is changed
printf 'fresh' > "$NT/u/.memex-local/enrol/studio-2.preauth"
OUT="$(nt "Starting")"
[ ! -s "$NT/up.log" ] && grep -q 'did not settle' <<<"$OUT" && [ -e "$NT/u/.memex-local/enrol/studio-2.preauth" ] \
  && ok "a daemon that does not settle in the bounded wait: WAITING, no 'up', the dropped key untouched" || bad "unsettled" "$OUT"
# 7. a registered node that is Stopped: never re-keyed
printf '{"_profiles":"x"}' > "$NT/state/tailscaled.state"; echo true > "$NT/havekey"
OUT="$(nt "Stopped")"
[ ! -s "$NT/up.log" ] && grep -q 'registered but its BackendState is Stopped' <<<"$OUT" \
  && ok "a registered node that is Stopped is never re-keyed by the script" || bad "stopped" "$OUT"
rm -rf "$NT"

echo "── operator SSH: Remote Login on + the operator's PUBLIC keys for the console user (bootstrap 1.5.0, 2026-11-12) ──"
OT="$(mktemp -d)"; mkdir -p "$OT/bin" "$OT/home"; ME="$(id -un)"
ssh-keygen -q -t ed25519 -N '' -C 'operator one' -f "$OT/k1" && ssh-keygen -q -t ecdsa -N '' -C 'operator two' -f "$OT/k2" \
  && ssh-keygen -q -t ed25519 -N '' -C 'private' -f "$OT/kp" || bad "ssh-keygen fixtures" "failed"
K1="$(cat "$OT/k1.pub")"; K2="$(cat "$OT/k2.pub")"; KPRIV="$(sed -n 2p "$OT/kp")"
FP1="$(ssh-keygen -lf "$OT/k1.pub" | awk '{print $2}')"
# Stubs: launchd (sshd loaded iff $OT/loaded; its override from $OT/disabled), the directory (the access group iff
# $OT/group; membership iff $OT/member), systemsetup (loads sshd iff $OT/systemsetup-works). Every call is recorded.
cat > "$OT/bin/launchctl" <<STUB
#!/bin/bash
echo "launchctl \$*" >> "$OT/calls"
case "\$1" in
  print) [ -e "$OT/loaded" ] ;;
  print-disabled) printf '\tdisabled services = {\n\t\t"com.openssh.sshd" => %s\n\t}\n' "\$(cat "$OT/disabled" 2>/dev/null || echo enabled)" ;;
  enable) rm -f "$OT/disabled" ;;
  bootstrap) [ -e "$OT/bootstrap-works" ] && touch "$OT/loaded"; [ -e "$OT/bootstrap-works" ] ;;
esac
STUB
cat > "$OT/bin/dscl" <<STUB
#!/bin/bash
echo "dscl \$*" >> "$OT/calls"; [ -e "$OT/group" ]
STUB
cat > "$OT/bin/dseditgroup" <<STUB
#!/bin/bash
echo "dseditgroup \$*" >> "$OT/calls"; case "\$2" in checkmember) [ -e "$OT/member" ] ;; *) exit 0 ;; esac
STUB
cat > "$OT/bin/systemsetup" <<STUB
#!/bin/bash
echo "systemsetup \$*" >> "$OT/calls"; [ -e "$OT/systemsetup-works" ] && touch "$OT/loaded"
echo "Turning Remote Login on or off requires Full Disk Access privileges."
STUB
chmod +x "$OT/bin/"*
ost() {  # $1 = the shell to evaluate after sourcing; rest = extra env
  local ev="$1"; shift; rm -f "$OT/calls"
  env FLEET_BOOTSTRAP_LIB=1 PATH="$OT/bin:$PATH" FLEET_USER_HOME="$OT/home" FLEET_LOG_DIR="$OT/log" FLEET_MANAGED_PREFS="$OT/none" "$@" \
    bash -c '. "$0"; '"$ev"'; echo "RC=$?"' "$FB" 2>&1; }
# 1. not configured: nothing happens, never an error
OUT="$(ost 'step_operator_ssh "$(id -un)"')"
case "$OUT" in *"operator ssh: not configured"*"RC=0"*) [ ! -e "$OT/calls" ] && [ ! -e "$OT/home/.ssh" ] && ok "OperatorSshKeys absent: 'not configured', exit 0, Remote Login and ~/.ssh untouched (not even probed)" || bad "not configured touched something" "$(cat "$OT/calls" 2>/dev/null)" ;; *) bad "not configured" "$OUT" ;; esac
# 2. one key on a Mac with Remote Login off and restricted to a group the person is not in: everything planned (dry run)
touch "$OT/group"
OUT="$(ost 'step_operator_ssh "$(id -un)"' FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_OPERATOR_SSH_KEYS="$K1")"
case "$OUT" in *"turning Remote Login on"*"DRY-RUN: launchctl enable system/com.openssh.sshd"*"DRY-RUN: launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist"*"adding $ME to com.apple.access_ssh"*"DRY-RUN: dseditgroup -o edit -a $ME -t user com.apple.access_ssh"*"adding key $FP1 to $OT/home/.ssh/authorized_keys (as $ME)"*"DRY-RUN: sudo -u $ME"*"1 key(s) added, 0 already present, 0 refused"*"RC=0"*)
  ok "one key, Remote Login off, a restricting access group: enable + bootstrap sshd, the person added to the group, the key appended AS the person" ;; *) bad "one key planned" "$OUT" ;; esac
[ ! -e "$OT/home/.ssh" ] && ! grep -q '^launchctl \(enable\|bootstrap\)' "$OT/calls" && ok "…and the dry run changed nothing" || bad "dry run changed something" "$(cat "$OT/calls")"
case "$OUT" in *"$FP1"*) case "$OUT" in *"adding key ssh-ed25519"*) bad "the log names the fingerprint, not the key" "$OUT" ;; *) ok "the log names the key by its SHA256 fingerprint" ;; esac ;; *) bad "fingerprint logged" "$OUT" ;; esac
# 3. already present (another comment, an options-prefixed line kept): nothing appended, the file byte-identical
touch "$OT/loaded" "$OT/member"; mkdir -p "$OT/home/.ssh"; chmod 700 "$OT/home/.ssh"
printf 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyOtherKeyOtherKeyOtherKeyOtherKey0 someone else\nfrom="10.0.0.1" %s renamed\n' "$(cut -d' ' -f1-2 "$OT/k1.pub")" > "$OT/home/.ssh/authorized_keys"
chmod 600 "$OT/home/.ssh/authorized_keys"; SUM="$(shasum "$OT/home/.ssh/authorized_keys")"
OUT="$(ost 'step_operator_ssh "$(id -un)"' FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_OPERATOR_SSH_KEYS="$K1")"
case "$OUT" in *"Remote Login is on (com.openssh.sshd loaded)"*"$ME is allowed through Remote Login's access group"*"key $FP1 already in"*"0 key(s) added, 1 already present"*"RC=0"*)
  case "$OUT" in *"DRY-RUN"*) bad "an already-present key plans no change" "$OUT" ;; *) ok "a key already there (any comment, any options) is not appended again; Remote Login on and a member: no change at all" ;; esac ;; *) bad "already present" "$OUT" ;; esac
[ "$(shasum "$OT/home/.ssh/authorized_keys")" = "$SUM" ] && ok "…and authorized_keys is byte-identical (no line removed or rewritten)" || bad "authorized_keys changed" "$(cat "$OT/home/.ssh/authorized_keys")"
# 4. malformed entries are REFUSED (and never logged); a valid one beside them is still installed; the run says FAILED
OUT="$(ost 'step_operator_ssh "$(id -un)"' FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_OPERATOR_SSH_KEYS="$(printf '%s\ncommand="/bin/sh" %s\n-----BEGIN OPENSSH PRIVATE KEY-----\n%s\nssh-dss AAAAB3NzaC1kc3MAAACB x\n' "$K2" "$K1" "$KPRIV")")"
case "$OUT" in *"REFUSED OperatorSshKeys entry 2"*"REFUSED OperatorSshKeys entry 3"*"REFUSED OperatorSshKeys entry 4"*"REFUSED OperatorSshKeys entry 5"*"adding key SHA256:"*"1 key(s) added, 0 already present, 4 refused"*"RC=1"*)
  ok "an options prefix, private key material and an unaccepted type are each REFUSED; the valid key is still installed; the step fails (exit 1)" ;; *) bad "malformed refused" "$OUT" ;; esac
case "$OUT" in *"$KPRIV"*|*"BEGIN OPENSSH"*|*'command='*) bad "a refused entry's content never reaches the log" "$OUT" ;; *) ok "a refused entry's content never reaches the output or the log" ;; esac
OUT="$(ost 'step_operator_ssh "$(id -un)"' FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_OPERATOR_SSH_KEYS="ssh-rsa ${K1#* }")"
case "$OUT" in *"REFUSED OperatorSshKeys entry 1"*"FAILED — none of the 1 declared key(s)"*"RC=1"*) ! grep -q '^launchctl' "$OT/calls" && ok "only invalid keys (an ed25519 blob labelled ssh-rsa): FAILED, and Remote Login is not touched" || bad "invalid-only probed launchd" "$(cat "$OT/calls")" ;; *) bad "invalid only" "$OUT" ;; esac
# 5. a symlinked authorized_keys is never followed
mkdir -p "$OT/h2/.ssh"; ln -s /etc/hosts "$OT/h2/.ssh/authorized_keys"
OUT="$(ost 'step_operator_ssh "$(id -un)"' FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_USER_HOME="$OT/h2" FLEET_OPERATOR_SSH_KEYS="$K1")"
case "$OUT" in *"REFUSED $OT/h2/.ssh/authorized_keys"*"symlink"*"RC=1"*) case "$OUT" in *"DRY-RUN"*) bad "symlink: nothing planned" "$OUT" ;; *) ok "a symlinked authorized_keys is REFUSED, nothing written" ;; esac ;; *) bad "symlink refused" "$OUT" ;; esac
# 6. Remote Login, for real (stubs): launchd first; systemsetup only when launchd did not take; a failure says why
rm -f "$OT/loaded"; echo disabled > "$OT/disabled"; touch "$OT/bootstrap-works"
OUT="$(ost step_remote_login)"
case "$OUT" in *"Remote Login turned on (launchctl)"*"RC=0"*) grep -q '^launchctl enable system/com.openssh.sshd' "$OT/calls" && grep -q '^launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist' "$OT/calls" && ! grep -q '^systemsetup' "$OT/calls" \
  && ok "Remote Login via launchd: the persisted override enabled and ssh.plist bootstrapped; systemsetup (Full Disk Access) not needed" || bad "launchd path calls" "$(cat "$OT/calls")" ;; *) bad "launchd path" "$OUT" ;; esac
rm -f "$OT/loaded" "$OT/bootstrap-works"; touch "$OT/systemsetup-works"
OUT="$(ost step_remote_login)"
case "$OUT" in *"Remote Login turned on (systemsetup)"*"RC=0"*) ok "launchd did not take: the systemsetup fallback turns it on" ;; *) bad "systemsetup fallback" "$OUT" ;; esac
rm -f "$OT/loaded" "$OT/systemsetup-works"
OUT="$(ost step_remote_login)"
case "$OUT" in *"FAILED to turn Remote Login on"*"Full Disk Access"*"RC=1"*) ok "neither took: FAILED, with what systemsetup said (retried next run)" ;; *) bad "remote login failure" "$OUT" ;; esac
# 7. the append itself (run as the person in production): perms set, a missing final newline repaired, every line kept
mkdir -p "$OT/h3/.ssh"; chmod 755 "$OT/h3/.ssh"; printf 'ssh-ed25519 AAAAexisting keep-me' > "$OT/h3/.ssh/authorized_keys"; chmod 644 "$OT/h3/.ssh/authorized_keys"
AP="$(env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; printf "%s" "$AUTHKEYS_APPEND_SH"' "$FB")"
/bin/sh -c "$AP" sh "$OT/h3" "$K1" && [ "$(sed -n 1p "$OT/h3/.ssh/authorized_keys")" = "ssh-ed25519 AAAAexisting keep-me" ] && [ "$(sed -n 2p "$OT/h3/.ssh/authorized_keys")" = "$K1" ] \
  && [ "$(wc -l < "$OT/h3/.ssh/authorized_keys" | tr -d ' ')" = 2 ] && ok "the append keeps every line and repairs a missing final newline (never glues onto the last key)" || bad "append" "$(cat -e "$OT/h3/.ssh/authorized_keys")"
[ "$(stat -f%Lp "$OT/h3/.ssh")" = 700 ] && [ "$(stat -f%Lp "$OT/h3/.ssh/authorized_keys")" = 600 ] && ok "…~/.ssh is 0700 and authorized_keys 0600" || bad "modes" "$(stat -f'%Lp %N' "$OT/h3/.ssh" "$OT/h3/.ssh/authorized_keys")"
/bin/sh -c "$AP" sh "$OT/h4" "$K1" && [ "$(cat "$OT/h4/.ssh/authorized_keys")" = "$K1" ] && [ "$(stat -f%Lp "$OT/h4/.ssh")" = 700 ] && ok "a home without ~/.ssh gets one (0700) holding just the key" || bad "fresh ~/.ssh" "$(ls -la "$OT/h4/.ssh" 2>&1)"
# 8. the profile shapes: an array (one key per element) and a newline-separated string
plutil -create xml1 "$OT/arr.plist" && plutil -insert OperatorSshKeys -array "$OT/arr.plist" && plutil -insert OperatorSshKeys -string "$K1" -append "$OT/arr.plist" && plutil -insert OperatorSshKeys -string "$K2" -append "$OT/arr.plist"
[ "$(ost operator_ssh_keys FLEET_MANAGED_PREFS="$OT/arr" | sed '$d')" = "$(printf '%s\n%s' "$K1" "$K2")" ] && ok "OperatorSshKeys as a profile array: one key per element" || bad "array keys" "$(ost operator_ssh_keys FLEET_MANAGED_PREFS="$OT/arr")"
plutil -create xml1 "$OT/str.plist" && plutil -insert OperatorSshKeys -string "$(printf '%s\n\n%s' "$K1" "$K2")" "$OT/str.plist"
[ "$(ost operator_ssh_keys FLEET_MANAGED_PREFS="$OT/str" | sed '$d')" = "$(printf '%s\n%s' "$K1" "$K2")" ] && ok "OperatorSshKeys as one string: newline-separated, blank lines dropped" || bad "string keys" "$(ost operator_ssh_keys FLEET_MANAGED_PREFS="$OT/str")"
# 9. it runs right after the console user, before anything that can fail the run
awk '/^main\(\) \{/{on=1} on' "$FB" | grep -n 'step_operator_ssh\|read_participation' | head -1 | grep -q step_operator_ssh \
  && ! grep -q 'sshd_config' <(grep -v '^#\|never touches sshd_config\|sshd_config never' "$FB") \
  && ok "main runs the operator SSH step before the participation answers (a Mac that fails later is still reachable); sshd_config is never touched" || bad "ssh step order" "$(awk '/^main\(\) \{/{on=1} on' "$FB")"
rm -rf "$OT"

echo "── bootstrap token: escrowed, or said every run until it is — never a password (bootstrap 1.6.0, 2026-11-12) ──"
BT="$(mktemp -d)"; mkdir -p "$BT/st"; ME="$(id -un)"
printf 'profiles: Bootstrap Token supported on server: YES\nprofiles: Bootstrap Token escrowed to server: YES\n' > "$BT/yes.txt"
printf 'profiles: Bootstrap Token supported on server: YES\nprofiles: Bootstrap Token escrowed to server: NO\n' > "$BT/no.txt"
printf 'profiles: Bootstrap Token supported on server: NO\nprofiles: Bootstrap Token escrowed to server: NO\n' > "$BT/unsup.txt"
printf 'Must be running as root\n' > "$BT/garbage.txt"
btv() { env FLEET_BOOTSTRAP_LIB=1 bash -c '. "$0"; bootstrap_token_verdict "$(cat "$1")"' "$FB" "$1"; }
[ "$(btv "$BT/yes.txt")" = escrowed ] && [ "$(btv "$BT/no.txt")" = not-escrowed ] && [ "$(btv "$BT/unsup.txt")" = unsupported ] && [ "$(btv "$BT/garbage.txt")" = unknown ] \
  && ok "profiles' bootstrap-token status reads as escrowed / not-escrowed / unsupported / unknown (never a guess)" || bad "bootstrap token verdicts" "$(btv "$BT/yes.txt") $(btv "$BT/no.txt") $(btv "$BT/unsup.txt") $(btv "$BT/garbage.txt")"
bts() { env FLEET_BOOTSTRAP_LIB=1 FLEET_STATE_DIR="$BT/st" FLEET_LOG_DIR="$BT/log" FLEET_PROFILES_STATUS_FILE="$1" "${@:2}" bash -c '. "$0"; step_bootstrap_token "$(id -un)"; echo "RC=$?"' "$FB" 2>&1; }
# escrowed: said, a green reading, no notification
OUT="$(env FLEET_BOOTSTRAP_DRY_RUN=1 FLEET_BOOTSTRAP_LIB=1 FLEET_STATE_DIR="$BT/st" FLEET_LOG_DIR="$BT/log" FLEET_PROFILES_STATUS_FILE="$BT/yes.txt" bash -c '. "$0"; step_bootstrap_token "$(id -un)"; echo "RC=$?"' "$FB" 2>&1)"
case "$OUT" in *"bootstrap token: escrowed to the MDM"*"reading $BT/st/bootstrap-token = escrowed"*"RC=0"*) case "$OUT" in *osascript*) bad "escrowed: no notification" "$OUT" ;; *) ok "escrowed: said, a green reading, no notification" ;; esac ;; *) bad "escrowed" "$OUT" ;; esac
# not escrowed, FIRST run: sign out and back in first — logged, a reading, a notification AS the person; never the command yet
OUT="$(bts "$BT/no.txt" FLEET_BOOTSTRAP_DRY_RUN=1)"
case "$OUT" in *"NOT ESCROWED"*"Sign out and back in to finish fleet setup"*"DRY-RUN: launchctl asuser $(id -u) sudo -u $ME /usr/bin/osascript"*"RC=0"*) case "$OUT" in *"profiles install"*) bad "first run names sign-in only" "$OUT" ;; *) ok "not escrowed, first run: 'sign out and back in' in the log and a notification posted AS the person; exit 0" ;; esac ;; *) bad "not escrowed first run" "$OUT" ;; esac
# real run (the notification stubbed): the marker and the reading are written, root-owned 0644 in production
mkdir -p "$BT/bin"; printf '#!/bin/bash\necho "launchctl $*" >> "%s/calls"\n' "$BT" > "$BT/bin/launchctl"; chmod +x "$BT/bin/launchctl"
OUT="$(bts "$BT/no.txt" PATH="$BT/bin:$PATH")"
[ -f "$BT/st/bootstrap-token.first-seen" ] && grep -q '^not-escrowed .* signin since ' "$BT/st/bootstrap-token" && [ "$(stat -f%Lp "$BT/st/bootstrap-token")" = 644 ] && grep -q 'asuser' "$BT/calls" \
  && ok "…the first-seen marker and a world-readable reading (0644) are written, and the notification is posted" || bad "first run writes" "$OUT / $(cat "$BT/st/bootstrap-token" 2>/dev/null)"
# a LATER run still un-escrowed: the command is named
OUT="$(bts "$BT/no.txt" PATH="$BT/bin:$PATH")"
case "$OUT" in *"still not finished"*"sudo profiles install -type bootstraptoken"*"RC=0"*) grep -q '^not-escrowed .* command since ' "$BT/st/bootstrap-token" && ok "a later run still un-escrowed names 'sudo profiles install -type bootstraptoken' (every run, until escrowed)" || bad "later reading" "$(cat "$BT/st/bootstrap-token")" ;; *) bad "later run" "$OUT" ;; esac
# escrowed after that: the marker goes, the reading turns green
OUT="$(bts "$BT/yes.txt" PATH="$BT/bin:$PATH")"
[ ! -e "$BT/st/bootstrap-token.first-seen" ] && grep -q '^escrowed ' "$BT/st/bootstrap-token" && ok "once escrowed the marker is removed and the reading is green" || bad "escrowed after" "$OUT"
# not supported, and unreadable: said, never a failure, never a notification
rm -f "$BT/calls"; OUT="$(bts "$BT/unsup.txt" PATH="$BT/bin:$PATH")"
case "$OUT" in *"does not support a bootstrap token"*"RC=0"*) [ ! -e "$BT/calls" ] && grep -q '^unsupported ' "$BT/st/bootstrap-token" && ok "an MDM without bootstrap-token support: said, reading 'unsupported', no notification, exit 0" || bad "unsupported side effects" "$(cat "$BT/calls" 2>/dev/null)" ;; *) bad "unsupported" "$OUT" ;; esac
OUT="$(bts "$BT/garbage.txt" PATH="$BT/bin:$PATH")"
case "$OUT" in *"bootstrap token: UNREAD"*"Must be running as root"*"RC=0"*) ok "an unreadable status is UNREAD with what profiles said, never read as escrowed" ;; *) bad "unknown" "$OUT" ;; esac
# never a password
! grep -qiE 'read -s|-passin|password *=|stdin.*pass' <(sed -n '/^# BOOTSTRAP TOKEN/,/^# PARTICIPATION/p' "$FB") && grep -q 'step_bootstrap_token "\$user"' "$FB" \
  && ok "the step never reads or handles a password, and main runs it every run" || bad "bootstrap token wiring" "missing"
rm -rf "$BT"

echo "── a fleet Mac joins BEFORE its first-run setup; every step leaves a reading; a stuck join is said (bootstrap 1.7.0) ──"
# The first Mac mini (2026-11-12): every run answered the setup, the portal refused it ("signIn: Choose at least one way to
# sign in") — a fleet Mac's sign-in arrives WITH the enrolment — and the join was never filed; the reason was root-only.
RS="$(mktemp -d)"; mkdir -p "$RS/home/.memex-local" "$RS/st"
SMW='as_user() { shift; printf "%s\n" "$*" >> "$SM_CALLS"
      case "$*" in *-CAROOT*) printf "%s" "$SM_CAROOT" ;;
        *"fleet join"*) printf "%b\n" "${SM_JOIN_OUT:-}"; return "${SM_JOIN_RC:-0}" ;;
        *"setup --apply"*) printf "%b\n" "${SM_SETUP_OUT:-}"; return "${SM_SETUP_RC:-0}" ;; esac; return 0; }
    probe_as_user() { case "$*" in *verify*) return 0 ;; *awaiting_setup*) [ "${SM_AWAIT:-0}" = 1 ] ;; *) return 1 ;; esac; }
    notify_user() { printf "NOTIFY %s | %s\n" "$2" "$3"; }
    read_participation >/dev/null; track memex step_memex jroe studio-2; rc=$?; finish_stage jroe; echo "rc=$rc"'
rsrun() { : > "$RS/calls"; OUT="$(env $FCA_ENV FLEET_BOOTSTRAP_LIB=1 FLEET_LOG_DIR="$RS/log" FLEET_MANAGED_PREFS="$RS/none" FLEET_USER_HOME="$RS/home" FLEET_BREW_PREFIX="$RS/brew" \
  FLEET_STATE_DIR="$RS/st" FLEET_CONTROL_URL=https://control.example.test SM_CALLS="$RS/calls" SM_CAROOT="$T/sm/caroot" FLEET_ACCEPT_JOBS=false FLEET_ACCEPT_TOKENS=false \
  FLEET_ORGANISATION=Acme FLEET_OWNER_NAME="Jane Roe" FLEET_OWNER_EMAIL=jane.roe@example.com FLEET_ACCEPT_TERMS=true FLEET_CA_SHA256="$FSHA" "$@" bash -c '. "$0"; '"$SMW" "$FB" 2>&1)"; }
rd() { head -1 "$RS/st/$1" 2>/dev/null; }
# 1. in first-run setup, not joined: the join runs FIRST (no update, no heartbeat wait); the setup is NOT answered
rsrun SM_AWAIT=1 SM_JOIN_RC=3 SM_JOIN_OUT='open https://control.example.test/Hosting/Heartbeats/Enrol/studio-2-abc to approve'
grep -q 'fleet join --control https://control.example.test --id studio-2 --no-open --wait 10 --code-file .* --no-update --heartbeat-wait 0 ' "$RS/calls" && ! grep -q 'setup --apply' "$RS/calls" \
  && ok "portal in first-run setup, Mac not joined: 'fleet join --no-update --heartbeat-wait 0' runs BEFORE the setup, and the setup is not answered" || bad "join before setup" "$(cat "$RS/calls") / $OUT"
case "$(rd join)" in "waiting-approval "*"request filed; waiting for the Approve on control: https://control.example.test/Hosting/Heartbeats/Enrol/studio-2-abc") ok "…reading join = waiting-approval, with the approval link" ;; *) bad "join reading" "$(rd join)" ;; esac
case "$(rd stage)" in "waiting-approval "*"join: "*) ok "…reading stage names the join, waiting for the Approve" ;; *) bad "stage approval" "$(rd stage)" ;; esac
case "$OUT" in *NOTIFY*) bad "waiting for the Approve is control's to give: no notification" "$OUT" ;; *) ok "…and no notification (the Approve is control's to give, not the person's)" ;; esac
# 2. the pre-setup join succeeds: joined, but NOT finalised (the setup and the finalisation follow)
rsrun SM_AWAIT=1 SM_JOIN_RC=0 SM_JOIN_OUT='joined as studio-2'
case "$(rd join)" in "ok "*"before the setup"*) ! grep -q 'fleet.finalized' "$RS/calls" && ok "a join before the setup is recorded (join ok) and never marked finalised" || bad "pre-setup finalized" "$(cat "$RS/calls")" ;; *) bad "pre-setup join ok" "$(rd join) / $OUT" ;; esac
# 3. joined, the setup refused — its own words reach the reading, the stage, and the person
printf 'x' > "$RS/home/.memex-local/fleet.yaml"
rsrun SM_AWAIT=1 SM_SETUP_RC=1 SM_SETUP_OUT='\033[31merr\033[0m  setup --apply: the portal refused the answers — signIn: Choose at least one way to sign in'
case "$(rd setup)" in "failed "*"exit 1: err  setup --apply: the portal refused the answers — signIn: Choose at least one way to sign in") ok "a refused setup: reading setup = failed with the portal's own words (colour stripped)" ;; *) bad "setup failed reading" "$(rd setup)" ;; esac
case "$(rd stage)" in "failed "*"setup: memex: the setup API refused or failed — err  setup --apply: the portal refused"*) ok "…stage names the setup and why" ;; *) bad "stage setup" "$(rd stage)" ;; esac
grep -q 'rc=1' <<<"$OUT" && ok "…and the step fails (exit 1)" || bad "setup refused rc" "$OUT"
# 4. answered and ACCEPTED, but the portal is still in first-run setup on the next run: a failure, never another "answered"
rm -f "$RS/st/setup.answered"; rsrun SM_AWAIT=1 SM_SETUP_RC=0 SM_SETUP_OUT='ok setup applied (completed)'
case "$(rd setup)" in "waiting "*"answered: ok setup applied (completed)"*) ok "an accepted setup: reading setup = waiting (the portal restarts configured)" ;; *) bad "setup answered" "$(rd setup)" ;; esac
rsrun SM_AWAIT=1 SM_SETUP_RC=0 SM_SETUP_OUT='ok setup applied (completed)'
case "$(rd setup)" in "failed "*"still in first-run setup"*) grep -q 'rc=1' <<<"$OUT" && ok "accepted earlier and STILL in first-run setup: failed, said every run" || bad "still-in-setup rc" "$OUT" ;; *) bad "still in setup" "$(rd setup) / $OUT" ;; esac
# 5. configured, not joined: the join control REFUSES — the refusal text reaches the reading and the person
rm -f "$RS/home/.memex-local/fleet.yaml" "$RS/st/setup.answered"
rsrun SM_AWAIT=0 SM_JOIN_RC=1 SM_JOIN_OUT='line one\nREFUSED by control: Hosting:Enrolment:RegistryPlan is not declared\nerr  fleet join: stopped'
case "$(rd join)" in "failed "*"exit 1: REFUSED by control: Hosting:Enrolment:RegistryPlan is not declared") ok "a refused join: reading join = failed with control's refusal text" ;; *) bad "join refused reading" "$(rd join)" ;; esac
case "$OUT" in *"NOTIFY Fleet join not filed | This Mac has not joined the fleet: the step 'join' is failed — memex: fleet join FAILED (exit 1) — REFUSED by control"*) ok "…and the person is notified, naming the reason" ;; *) bad "join refused notify" "$OUT" ;; esac
case "$(rd setup):$(rd up)" in "ok "*":ok "*) ok "the earlier steps leave their readings (up ok, setup ok)" ;; *) bad "up/setup readings" "$(rd up) / $(rd setup)" ;; esac
# 6. a missing prerequisite is NAMED: no ControlUrl → the join is skipped, said, and the person told
rsrun FLEET_CONTROL_URL= SM_AWAIT=0
case "$(rd join):$(rd stage)" in "skipped "*"no ControlUrl in the com.memex.local profile:waiting "*"join: memex: WAITING for ControlUrl"*) case "$OUT" in *"NOTIFY Fleet join not filed"*"ControlUrl"*) ok "no ControlUrl: the join is skipped, the stage names ControlUrl, and the person is notified" ;; *) bad "no ControlUrl notify" "$OUT" ;; esac ;; *) bad "no ControlUrl" "$(rd join) / $(rd stage)" ;; esac
# 7. the readings: world-readable, no secrets
[ "$(stat -f%Lp "$RS/st/join")" = 644 ] && ! grep -rqiE 'secret|password|token=[^<]' "$RS/st" && ok "every reading is 0644 and carries no secret" || bad "readings mode/secret" "$(ls -l "$RS/st")"
# 8. a run that ends with every step done says so
FB_EVAL='STUCK_NAME=""; WAIT_NAME=""; finish_stage jroe' fb FLEET_STATE_DIR="$RS/st" FLEET_USER_HOME="$RS/home" FLEET_LOG_DIR="$RS/log" >/dev/null
case "$(rd stage)" in "done "*) ok "a run with nothing stuck writes stage = done" ;; *) bad "stage done" "$(rd stage)" ;; esac
rm -rf "$RS"

echo
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
