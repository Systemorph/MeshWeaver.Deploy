#!/usr/bin/env bash
# The headscale image's in-build smoke (Dockerfile, test stage). Every check is an ASSERTION that can
# fail: a missing endpoint, a refused config, a CLI that cannot reach the server over its socket.
# Counts failures and exits with that count, printing `N failure(s)` (the lane's negative control
# reads both). Usage: smoke.sh [config]   (default /test/config.yaml)
set -uo pipefail
cfg="${1:-/test/config.yaml}"
fail=0
pass() { echo "PASS $*"; }
bad()  { echo "FAIL $*"; fail=$((fail + 1)); }

mkdir -p /tmp/hs
headscale version || bad "headscale version: the binary does not run"

if headscale configtest -c "$cfg" >/tmp/hs/configtest.log 2>&1; then
  pass "configtest accepts $cfg"
else
  cat /tmp/hs/configtest.log; bad "configtest: headscale refuses $cfg"
fi

headscale serve -c "$cfg" >/tmp/hs/serve.log 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
up=false
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 http://127.0.0.1:8080/health >/tmp/hs/health.json 2>/dev/null; then up=true; break; fi
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.5
done
if [ "$up" = true ]; then
  pass "serve answers /health"
  status=$(jq -r '.status // empty' /tmp/hs/health.json 2>/dev/null || true)
  [ "$status" = "pass" ] && pass "/health status is pass" || bad "/health status is '${status}', expected 'pass'"
else
  tail -n 40 /tmp/hs/serve.log; bad "serve: /health never answered (the server did not start)"
fi

# The Noise public key a Tailscale client fetches first (`/key?v=<capability version>`). Without it no
# client can register, whatever /health says.
if key=$(curl -fsS --max-time 4 'http://127.0.0.1:8080/key?v=116' 2>/dev/null) \
   && [ -n "$(jq -r '.publicKey // empty' <<<"$key" 2>/dev/null)" ]; then
  pass "/key serves the Noise public key"
else
  bad "/key: no Noise public key — a client could not start the control protocol"
fi

# The admin CLI over the unix socket: what an operator action runs to create a user or list nodes.
if headscale -c "$cfg" users create smoke >/dev/null 2>&1 \
   && [ "$(headscale -c "$cfg" users list -o json 2>/dev/null | jq -r '[.[] | select(.name == "smoke")] | length')" = "1" ]; then
  pass "CLI creates and lists a user over the socket"
else
  bad "CLI: could not create and list a user over the unix socket"
fi

# Negative control INSIDE the smoke: a config naming a policy file that does not exist must be refused.
# A configtest that accepts anything proves nothing above.
sed 's#path: /test/policy.hujson#path: /test/does-not-exist.hujson#' "$cfg" > /tmp/hs/bad.yaml
if headscale configtest -c /tmp/hs/bad.yaml >/dev/null 2>&1; then
  bad "configtest accepted a config whose policy file does not exist — its verdict means nothing"
else
  pass "configtest refuses a missing policy file (negative control)"
fi

echo "$fail failure(s)"
exit "$fail"
