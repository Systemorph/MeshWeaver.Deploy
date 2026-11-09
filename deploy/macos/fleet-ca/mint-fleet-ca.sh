#!/bin/bash
# mint-fleet-ca.sh — mints the fleet's LOCAL CA: one EC P-256 CA, NAME-CONSTRAINED to localhost (and every name
# below it), 127.0.0.1 and ::1, whose certificate the device-management layer deploys to every fleet Mac as a
# trusted-root profile and whose private key reaches each Mac sealed in its enrolment code (item `a`), where
# `memex-local ca adopt` installs it as mkcert's CA. Why: `mkcert -install` needs a person at the Mac's UI —
# macOS refuses a trust-settings change without it, even as root (the second Mac Studio, 2026-11-09).
#
# 🚨 MINTING THE FLEET CA IS THE OWNER'S CALL. Run it once, on the owner's go; every fleet Mac trusts what it
# signs (for the loopback names only — the constraints are what make sharing one key across Macs tolerable).
#
#   mint-fleet-ca.sh --vault <key vault> --object <secret name> --subscription <name|id> [--cert-out <file>] [--days <n>] [--cn <name>]
#   mint-fleet-ca.sh --key-file <new file> [--cert-out <file>] …      (offline: the key into a NEW 0600 file)
#
# Output: the certificate PEM (to --cert-out, else stdout) and, on stderr, its SHA-256 — the value of the managed
# preference FleetCaSha256 and the enrolment fact caCertSha256. The key is generated in a 0700 directory of this
# run, converted to the enrolment item's form (base64 of the PKCS#8 DER, one line) in a 0600 file, handed to
# `az keyvault secret set --file` (never an argument, never printed — not even a fingerprint of it), and the
# directory is removed. An existing vault object is never overwritten (a second CA would orphan every Mac's
# trust); --key-file refuses an existing file for the same reason. Vault mode REQUIRES --subscription, passed to every
# `az keyvault` call: the az login is shared, its default subscription is whatever someone last selected, and a vault
# name resolved there can be the wrong vault (or tenant). Never `az account set`.
set -euo pipefail

vault="" object="" subscription="" key_out="" cert_out="" days=3650 cn="MeshWeaver Fleet Local CA"
die() { printf 'mint-fleet-ca: %s\n' "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --vault) shift; vault="${1:?--vault needs a name}" ;;
    --object) shift; object="${1:?--object needs a secret name}" ;;
    --subscription) shift; subscription="${1:?--subscription needs a subscription name or id}" ;;
    --key-file) shift; key_out="${1:?--key-file needs a path}" ;;
    --cert-out) shift; cert_out="${1:?--cert-out needs a path}" ;;
    --days) shift; days="${1:?--days needs a number}" ;;
    --cn) shift; cn="${1:?--cn needs a name}" ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) die "unknown argument '$1'" ;;
  esac
  shift
done
case "$days" in ''|*[!0-9]*) die "--days must be a whole number" ;; esac
case "$cn" in *[!A-Za-z0-9\ ._-]*) die "--cn: letters, digits, spaces, dot, dash and underscore only" ;; esac
if [ -n "$key_out" ]; then
  [ -z "$vault$object$subscription" ] || die "--key-file OR --vault/--object/--subscription, not both"
  [ ! -e "$key_out" ] && [ ! -L "$key_out" ] || die "--key-file '$key_out' exists — refusing to overwrite a CA key"
else
  [ -n "$vault" ] && [ -n "$object" ] || die "name where the key goes: --vault <v> --object <name> --subscription <s> (or --key-file <new file>)"
  [ -n "$subscription" ] || die "--subscription <name|id> is required with --vault: the az login is shared and its default subscription is whatever was selected last — a vault resolved there can be the wrong one (never 'az account set')"
  command -v az >/dev/null 2>&1 || die "az (Azure CLI) not found"
  if az keyvault secret show --subscription "$subscription" --vault-name "$vault" --name "$object" -o none >/dev/null 2>&1; then
    die "the vault object $vault/$object already exists — refusing to replace the fleet CA (every Mac trusts the old one)"
  fi
fi
command -v openssl >/dev/null 2>&1 || die "openssl not found"

work="$(mktemp -d)"; chmod 700 "$work"
trap 'rm -rf "$work"' EXIT
umask 077
cat > "$work/ca.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = $cn
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
nameConstraints = critical,permitted;DNS:localhost,permitted;IP:127.0.0.1/255.255.255.255,permitted;IP:::1/ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
CNF
openssl ecparam -name prime256v1 -genkey -noout -out "$work/key.pem" 2>/dev/null || die "could not generate the EC P-256 key"
openssl req -new -x509 -key "$work/key.pem" -config "$work/ca.cnf" -extensions v3_ca -days "$days" -sha256 -out "$work/ca.pem" 2>/dev/null \
  || die "could not create the CA certificate"
# the enrolment item's form: base64 of the PKCS#8 DER, one line
openssl pkcs8 -topk8 -nocrypt -in "$work/key.pem" -outform DER 2>/dev/null | openssl base64 -A > "$work/key.b64" \
  || die "could not convert the key to PKCS#8"
printf '\n' >> "$work/key.b64"
sha="$(openssl x509 -in "$work/ca.pem" -outform DER | shasum -a 256 | cut -d' ' -f1)"

if [ -n "$key_out" ]; then
  ( set -o noclobber; cat "$work/key.b64" > "$key_out" ) || die "could not write '$key_out'"
  chmod 600 "$key_out"
  where="$key_out (0600)"
else
  az keyvault secret set --subscription "$subscription" --vault-name "$vault" --name "$object" --file "$work/key.b64" --encoding utf-8 \
    --content-type "application/pkcs8;base64" -o none >/dev/null || die "could not store the key in $vault/$object"
  where="$vault/$object"
fi
if [ -n "$cert_out" ]; then cp "$work/ca.pem" "$cert_out"; chmod 644 "$cert_out"; else cat "$work/ca.pem"; fi
printf 'fleet local CA minted — certificate sha256 %s (FleetCaSha256 / caCertSha256); key stored in %s, never printed\n' "$sha" "$where" >&2
