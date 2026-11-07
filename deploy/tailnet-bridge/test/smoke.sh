#!/bin/sh
# The bridge image's in-build smoke (Dockerfile, test stage). Every check is an assertion that can fail.
# Counts failures and exits with that count, printing `N failure(s)`. The joined-network path (a real
# control server, a real member machine) is the image lane's job, which has docker.
set -u
fail=0
pass() { echo "PASS $*"; }
bad()  { echo "FAIL $*"; fail=$((fail + 1)); }
w=$(mktemp -d); export BRIDGE_CONFIG_DIR="$w" HOME="$w"
render() { env "$@" tailnet-bridge --render 2>"$w/render.err"; }

tailscale version >/dev/null 2>&1 && pass "tailscale runs" || bad "tailscale does not run"
caddy version >/dev/null 2>&1 && pass "caddy runs" || bad "caddy does not run"

out=$(render BRIDGE_TARGET=https://mac.tailnet.internal:443 BRIDGE_TLS_SERVER_NAME=mac.example.com BRIDGE_TLS_INSECURE=true 'BRIDGE_BLOCKED=/dev /dev/*')
caddy validate --config $w/Caddyfile --adapter caddyfile >/dev/null 2>&1 && pass "the generated config is valid" || bad "the generated config is invalid"
echo "$out" | grep -q 'forward_proxy_url http://127.0.0.1:1055' && pass "every upstream goes through the tailnet proxy" || bad "the upstream does not go through the tailnet proxy"
echo "$out" | grep -q 'tls_server_name mac.example.com' && pass "the SNI is the configured name" || bad "no SNI"
echo "$out" | grep -q 'tls_insecure_skip_verify' && pass "insecure verification only when asked" || bad "BRIDGE_TLS_INSECURE=true was not applied"
out=$(render BRIDGE_TARGET=https://mac.tailnet.internal:443)
echo "$out" | grep -q 'tls_insecure_skip_verify' && bad "verification is skipped without being asked" || pass "verification is on by default"

for bad_env in "BRIDGE_TARGET=" "BRIDGE_TARGET=ftp://mac:21" "BRIDGE_TARGET=http://mac:80" "BRIDGE_TARGET=https://Mac.Example:443" "BRIDGE_TARGET=https://mac:8x" \
               "BRIDGE_TARGET=https://mac:443 BRIDGE_BLOCKED=dev" "BRIDGE_TARGET=https://mac:443 BRIDGE_BLOCKED=/dev}" \
               "BRIDGE_TARGET=https://mac:443 BRIDGE_UPSTREAM_HOST=a{b}"; do
  # shellcheck disable=SC2086
  if render $bad_env >/dev/null; then bad "rendered although [$bad_env]"; else pass "refused [$bad_env]"; fi
done

# The proxy itself: health answered here, blocked paths answered here, everything else forwarded (and,
# with no tailnet proxy running, refused as a bad gateway — proof it was forwarded, not served).
render BRIDGE_TARGET=https://mac.tailnet.internal:443 'BRIDGE_BLOCKED=/dev /dev/*' >/dev/null
caddy run --config $w/Caddyfile --adapter caddyfile >$w/caddy.log 2>&1 &
pid=$!
for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:8080/__bridge/healthz && break; sleep 0.25; done
code() { curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080$1"; }
[ "$(code /__bridge/healthz)" = 200 ] && pass "health answers 200" || bad "health answered $(code /__bridge/healthz)"
[ "$(code /dev/login)" = 404 ] && pass "a blocked path answers 404" || bad "/dev/login answered $(code /dev/login)"
[ "$(code /dev)" = 404 ] && pass "the blocked prefix itself answers 404" || bad "/dev answered $(code /dev)"
[ "$(code /developer)" = 502 ] && pass "a path that only starts like a blocked one is forwarded" || bad "/developer answered $(code /developer)"
[ "$(code /login)" = 502 ] && pass "other paths are forwarded" || bad "/login answered $(code /login), expected 502 (forwarded, no proxy)"
kill "$pid" 2>/dev/null

# Without a key the bridge refuses to start (the pod restarts until the keys sidecar has filled it).
: > $w/empty
if TS_LOGIN_SERVER=https://hs.example.com TS_AUTHKEY_FILE=$w/empty BRIDGE_TARGET=https://mac:443 tailnet-bridge >$w/nokey.log 2>&1; then
  bad "started without a pre-auth key"
else
  grep -q 'no pre-auth key' $w/nokey.log && pass "no key: refuses to start, says why" || bad "no key: failed without saying why"
fi

echo "$fail failure(s)"
exit "$fail"
