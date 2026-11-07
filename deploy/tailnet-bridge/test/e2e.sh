#!/usr/bin/env bash
# The bridge END TO END, in docker: a real control server, a member machine serving HTTPS on the private
# network only, and the bridge joining with a pre-auth key from a FILE and forwarding to it.
#   e2e.sh <bridge image> <headscale test image (deploy/headscale --target test)> <tailscale client image>
# Asserts: the member answers through the bridge (TLS to the member, SNI and Host = the public name, its
# MagicDNS name resolved through the tailnet proxy); a blocked path never reaches it; the bridge's health
# answers; the bridge is one node of its key's user. Exits non-zero on any failure. KEEP=1 leaves the
# containers running for debugging.
set -euo pipefail
bridge="$1" hs_image="$2" ts_image="$3"
net="tbe2e-$$"
docker network create "$net" >/dev/null
cleanup() {
  for c in tb-hs tb-mac tb-web tb-bridge; do docker logs "$c" 2>&1 | tail -n 15 | sed "s/^/[$c] /" || true; done
  docker rm -f tb-hs tb-mac tb-web tb-bridge >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
}
[ -n "${KEEP:-}" ] || trap cleanup EXIT
fail() { echo "::error::$*"; exit 1; }

# The control server: the in-build test config, reachable as http://tb-hs:8080.
docker run -d --name tb-hs --network "$net" --user root --entrypoint bash "$hs_image" -c '
  mkdir -p /tmp/hs
  sed -e "s#^server_url:.*#server_url: http://tb-hs:8080#" -e "s#^listen_addr:.*#listen_addr: 0.0.0.0:8080#" /test/config.yaml > /tmp/hs/c.yaml
  exec headscale serve -c /tmp/hs/c.yaml' >/dev/null
hc() { docker exec tb-hs headscale -c /tmp/hs/c.yaml "$@"; }
for _ in $(seq 1 60); do hc health >/dev/null 2>&1 && break; sleep 1; done
hc health >/dev/null 2>&1 || fail "the control server never answered"
hc users create owner >/dev/null; hc users create tailnet-bridge >/dev/null
uid() { hc users list -o json | jq -r --arg n "$1" '.[] | select(.name == $n) | .id'; }
mac_key=$(hc preauthkeys create -u "$(uid owner)" -e 10m -o json | jq -r .key)

# The member: a stock client in userspace and a web server in the SAME network namespace, published
# nowhere but on the private network (the bridge's only way in is WireGuard).
docker run -d --name tb-mac --network "$net" -e TS_AUTHKEY="$mac_key" -e TS_USERSPACE=true -e TS_STATE_DIR=/tmp \
  -e TS_HOSTNAME=mac -e TS_EXTRA_ARGS="--login-server=http://tb-hs:8080" "$ts_image" >/dev/null
# The member's web server speaks TLS with a certificate of its own (Caddy's internal CA, as a Mac's local
# ingress holds a local certificate): the bridge connects with SNI = the public name and does not verify.
docker run -d --name tb-web --network "container:tb-mac" --user root --entrypoint sh "$bridge" -c '
  printf "{\n admin off\n local_certs\n skip_install_trust\n}\nhttps://mac.example.com:443 {\n respond /dev/* \"SECRET\" 200\n respond \"member-says-hello host={host}\" 200\n}\n" > /tmp/web.caddy
  exec caddy run --config /tmp/web.caddy --adapter caddyfile' >/dev/null
for _ in $(seq 1 60); do
  [ "$(docker exec tb-mac tailscale status --json 2>/dev/null | jq -r .BackendState)" = Running ] && break; sleep 2
done
[ "$(docker exec tb-mac tailscale status --json | jq -r .BackendState)" = Running ] || fail "the member never joined"

# The bridge: its key in a FILE (as the chart mounts the Secret), ephemeral + reusable as declared.
bridge_key=$(hc preauthkeys create -u "$(uid tailnet-bridge)" -e 10m --reusable --ephemeral -o json | jq -r .key)
docker run -d --name tb-bridge --network "$net" --entrypoint sh \
  -e TS_LOGIN_SERVER=http://tb-hs:8080 -e TS_AUTHKEY_FILE=/tmp/key -e TS_STATE_DIR=/tmp/ts \
  -e BRIDGE_TARGET=https://mac.tailnet.example.com:443 -e BRIDGE_TLS_SERVER_NAME=mac.example.com -e BRIDGE_TLS_INSECURE=true -e BRIDGE_UPSTREAM_HOST=mac.example.com -e 'BRIDGE_BLOCKED=/dev /dev/*' -e BRIDGE_CONFIG_DIR=/tmp/tb \
  "$bridge" -c 'while [ ! -s /tmp/key ]; do sleep 1; done; exec /usr/local/bin/tailnet-bridge' >/dev/null
printf '%s' "$bridge_key" | docker exec -i tb-bridge sh -c 'umask 077; cat > /tmp/key.part && mv /tmp/key.part /tmp/key'

get() { docker run --rm --network "$net" --entrypoint curl "$hs_image" -s -m 10 -w '\n%{http_code}' "http://tb-bridge:8080$1" 2>/dev/null || true; }
body=""
for _ in $(seq 1 60); do body=$(get /index.html); [ "${body##*$'\n'}" = 200 ] && break; sleep 2; done
[ "${body##*$'\n'}" = 200 ] || fail "the bridge never forwarded to the member (last: ${body//$'\n'/ | })"
grep -q "member-says-hello host=mac.example.com" <<<"$body" && echo "PASS the member answers through the bridge, for the public host" || fail "the bridge answered, but not with the member's page"
dev=$(get /dev/login)
[ "${dev##*$'\n'}" = 404 ] && ! grep -q SECRET <<<"$dev" && echo "PASS a blocked path never reaches the member" || fail "/dev/login reached the member (${dev//$'\n'/ | })"
[ "$(get /__bridge/healthz | tail -n1)" = 200 ] && echo "PASS the bridge's own health answers" || fail "health did not answer"
eph=$(hc nodes list -o json | jq -r '[.[] | select(.user.name == "tailnet-bridge")] | length')
[ "$eph" = 1 ] && echo "PASS the bridge is one node of user tailnet-bridge" || fail "user tailnet-bridge has $eph node(s)"
echo "e2e: 4 checks passed"
