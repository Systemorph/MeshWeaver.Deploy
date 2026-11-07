#!/usr/bin/env bash
# The declared pre-auth keys sidecar (keys/headscale-keys.sh), exercised in the build against the
# SHIPPED binary and a stand-in API server (kube-stub.py). Every check is an assertion that can fail.
# Counts failures and exits with that count, printing `N failure(s)`. Usage: keys.sh [config]
set -uo pipefail
cfg="${1:-/test/config.yaml}"
fail=0
pass() { echo "PASS $*"; }
bad()  { echo "FAIL $*"; fail=$((fail + 1)); }

root=/tmp/hk; rm -rf "$root"; mkdir -p "$root/sa" "$root/state" /tmp/hs
# A fresh server of its own (smoke.sh's database is left alone).
sed -e 's#/tmp/hs/#/tmp/hk/#g' "$cfg" > "$root/config.yaml"
headscale serve -c "$root/config.yaml" > "$root/serve.log" 2>&1 &
hs_pid=$!
echo stub-token > "$root/sa/token"; echo control > "$root/sa/namespace"
python3 /test/kube-stub.py 18080 "$root/sa/token" "$root/state" hs-preauth-bridge hs-preauth-mac > "$root/stub.log" 2>&1 &
stub_pid=$!
trap 'kill "$hs_pid" "$stub_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do headscale -c "$root/config.yaml" health >/dev/null 2>&1 && break; sleep 0.5; done
headscale -c "$root/config.yaml" health >/dev/null 2>&1 || { tail -n 20 "$root/serve.log"; bad "headscale never answered"; }
for _ in $(seq 1 60); do curl -s -o /dev/null http://127.0.0.1:18080/ && break; sleep 0.5; done
curl -s -o /dev/null http://127.0.0.1:18080/ || { cat "$root/stub.log"; bad "the API stand-in never answered"; }

cat > "$root/keys.json" <<'EOF'
[
  {"name": "bridge", "user": "tailnet-bridge", "secret": "hs-preauth-bridge", "reusable": true, "ephemeral": true, "validHours": 720, "renewHours": 168},
  {"name": "mac", "user": "fleet-macs", "secret": "hs-preauth-mac", "reusable": false, "ephemeral": false, "validHours": 24, "renewHours": 2}
]
EOF
run() {
  KEYS_CONFIG="$root/keys.json" HEADSCALE_CONFIG="$root/config.yaml" KUBE_API=http://127.0.0.1:18080 \
    KUBE_SA_DIR="$root/sa" headscale-keys --once > "$root/run.log" 2>&1
}
h() { headscale -c "$root/config.yaml" "$@"; }
key_of() { jq -r '.data.authkey // empty' "$root/state/$1.json" 2>/dev/null | base64 -d 2>/dev/null; }
id_of()  { jq -r '.metadata.annotations["headscale.meshweaver.net/key-id"] // empty' "$root/state/$1.json" 2>/dev/null; }

# 1. Empty Secrets get a key each; the users are created; the key matches what headscale holds.
if run; then pass "first pass ensures both declared keys"; else cat "$root/run.log"; bad "first pass exited non-zero"; fi
for s in hs-preauth-bridge hs-preauth-mac; do
  k=$(key_of "$s")
  case "$k" in hskey-auth-*) pass "$s holds a pre-auth key" ;; *) bad "$s holds no pre-auth key" ;; esac
done
[ "$(h users list -o json | jq -r '[.[].name] | sort | join(",")')" = "fleet-macs,tailnet-bridge" ] \
  && pass "the declared users were created" || bad "users are [$(h users list -o json | jq -r '[.[].name] | join(",")')]"
b=$(id_of hs-preauth-bridge)
[ "$(h preauthkeys list -o json | jq -r --argjson id "${b:-0}" '[.[] | select(.id == $id)][0] | "\(.reusable) \(.ephemeral)"')" = "true true" ] \
  && pass "the bridge key is reusable and ephemeral, as declared" || bad "the bridge key's flags differ from its declaration"
m=$(id_of hs-preauth-mac)
[ "$(h preauthkeys list -o json | jq -r --argjson id "${m:-0}" '[.[] | select(.id == $id)][0] | "\(.reusable // false) \(.ephemeral // false)"')" = "false false" ] \
  && pass "the mac key is single-use and not ephemeral, as declared" || bad "the mac key's flags differ from its declaration"

# 2. The key is never printed.
grep -q 'hskey-auth-' "$root/run.log" && bad "the sidecar's log carries a key" || pass "no key in the log"

# 3. A second pass keeps good keys.
before=$(id_of hs-preauth-bridge)
run || bad "second pass exited non-zero"
[ "$(id_of hs-preauth-bridge)" = "$before" ] && pass "a good key is kept" || bad "a good key was replaced"

# 4. A key expired in headscale is replaced, and the replaced key stays expired.
[ -n "$before" ] && h preauthkeys expire -i "$before" >/dev/null
run || bad "pass after an expiry exited non-zero"
after=$(id_of hs-preauth-bridge)
[ -n "$before" ] && [ -n "$after" ] && [ "$after" != "$before" ] && pass "an expired key is replaced ($before → $after)" || bad "an expired key was kept"
grep -q "was expired in headscale" "$root/run.log" && pass "the reason is logged" || bad "no reason logged for the replacement"

# 5. A key inside its renewal window is replaced (24h valid, renew 25h before expiry).
jq '.[1].renewHours = 25' "$root/keys.json" > "$root/k2.json" && mv "$root/k2.json" "$root/keys.json"
mac_before=$(id_of hs-preauth-mac)
run || bad "pass in the renewal window exited non-zero"
[ "$(id_of hs-preauth-mac)" != "$mac_before" ] && pass "a key inside its renewal window is renewed" || bad "a key inside its renewal window was kept"
now=$(date +%s)
[ "$(h preauthkeys list -o json | jq -r --argjson id "${mac_before:-0}" --argjson now "$now" '[.[] | select(.id == $id)][0].expiration.seconds <= $now')" = true ] \
  && pass "the renewed key's predecessor was expired" || bad "the replaced key is still valid"

# 6. A Secret the chart did not create is never created: the pass fails and names it.
jq '. + [{"name": "stray", "user": "x", "secret": "hs-preauth-stray"}]' "$root/keys.json" > "$root/k3.json"
if KEYS_CONFIG="$root/k3.json" HEADSCALE_CONFIG="$root/config.yaml" KUBE_API=http://127.0.0.1:18080 \
     KUBE_SA_DIR="$root/sa" headscale-keys --once > "$root/run.log" 2>&1; then
  bad "a pass with a missing Secret exited 0"
else
  grep -q 'Secret hs-preauth-stray does not exist' "$root/run.log" && pass "a missing Secret fails the pass, named" || bad "a missing Secret failed without saying so"
fi

# 7. A refused API call (wrong token) is a failure, not a silent pass.
echo wrong > "$root/sa/token.bad"
if KEYS_CONFIG="$root/keys.json" HEADSCALE_CONFIG="$root/config.yaml" KUBE_API=http://127.0.0.1:18080 \
     KUBE_SA_DIR="$root/sa" KUBE_TOKEN_FILE="$root/sa/token.bad" headscale-keys --once > "$root/run.log" 2>&1; then
  bad "a pass the API server refused exited 0"
else
  grep -q 'answered HTTP 401' "$root/run.log" && pass "a refused API call fails the pass" || bad "a refused API call failed without naming the answer"
fi

echo "$fail failure(s)"
exit "$fail"
