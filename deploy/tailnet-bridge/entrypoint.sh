#!/bin/sh
# tailnet-bridge — publishes ONE machine of the private network at a public name.
#
#   the cluster ingress (TLS) → this pod :8080 (caddy) → tailscaled's outbound proxy → WireGuard → the machine
#
# tailscaled runs in userspace (no TUN device, no capabilities): it joins the network with a pre-auth key
# read from a FILE (TS_AUTHKEY_FILE, never the environment or an argv) and exposes an HTTP proxy on
# loopback only; caddy forwards every request through it. The machine opens no port to the internet, and
# its own sign-in protects it — BRIDGE_BLOCKED names paths the public name must never reach.
#
# Environment (all set by the chart):
#   TS_LOGIN_SERVER        https URL of the network's control server              (required)
#   TS_AUTHKEY_FILE        file holding the pre-auth key                          (required)
#   TS_HOSTNAME            this bridge's machine name                             (default tailnet-bridge)
#   BRIDGE_TARGET          the machine, https://<magicdns name>:<port>            (required; https ONLY:
#                          tailscaled's outbound proxy carries CONNECT tunnels, and an http:// upstream is
#                          sent to a proxy as a plain absolute-URI request, which it does not forward)
#   BRIDGE_UPSTREAM_HOST   Host header sent upstream (default: the public request's host)
#   BRIDGE_TLS_SERVER_NAME SNI for an https target (default: the target's name)
#   BRIDGE_TLS_INSECURE    "true": do not verify the target's certificate. The hop runs inside WireGuard,
#                          which already authenticates the peer by its node key; a local machine's ingress
#                          rarely holds a certificate for its public name.
#   BRIDGE_BLOCKED         space-separated path matchers answered 404 here (e.g. "/dev /dev/*")
#   tailnet-bridge --render prints the generated Caddyfile and exits (the tests read it).
set -eu

die() { echo "tailnet-bridge: $*" >&2; exit 64; }
valid_path() { case "$1" in /*) case "$1" in *[!A-Za-z0-9/_.*-]*) return 1 ;; esac ;; *) return 1 ;; esac; }
valid_host() { case "$1" in ''|*[!a-z0-9.-]*) return 1 ;; esac; }

target="${BRIDGE_TARGET:-}"
case "$target" in
  https://*) ;;
  *) die "BRIDGE_TARGET must be https://<name>:<port> — the tailnet proxy forwards CONNECT tunnels only (got '${target}')" ;;
esac
hostport="${target#*://}"
tname="${hostport%%:*}"
valid_host "$tname" || die "BRIDGE_TARGET names '$tname', not a lower-case DNS name"
case "$hostport" in *:*) port="${hostport##*:}"; case "$port" in ''|*[!0-9]*) die "BRIDGE_TARGET port '$port' is not a number" ;; esac ;; esac
upstream_host="${BRIDGE_UPSTREAM_HOST:-}"
[ -z "$upstream_host" ] || valid_host "$upstream_host" || die "BRIDGE_UPSTREAM_HOST '$upstream_host' is not a lower-case DNS name"
sni="${BRIDGE_TLS_SERVER_NAME:-}"
[ -z "$sni" ] || valid_host "$sni" || die "BRIDGE_TLS_SERVER_NAME '$sni' is not a lower-case DNS name"
blocked=""
set -f   # `/dev/*` is a path MATCHER for caddy, never a glob for this shell
for p in ${BRIDGE_BLOCKED:-}; do valid_path "$p" || die "BRIDGE_BLOCKED entry '$p' is not a path matcher"; blocked="$blocked $p"; done
set +f

conf="${BRIDGE_CONFIG_DIR:-/tmp/tailnet-bridge}/Caddyfile"
mkdir -p "$(dirname "$conf")"
{
  echo '{'
  echo '	admin off'
  echo '	auto_https off'
  echo '	persist_config off'
  echo '	servers {'
  echo '		trusted_proxies static private_ranges'
  echo '	}'
  echo '}'
  echo ':8080 {'
  echo '	handle /__bridge/healthz {'
  echo '		respond "ok" 200'
  echo '	}'
  if [ -n "$blocked" ]; then
    echo "	@blocked path$blocked"
    echo '	handle @blocked {'
    echo '		respond "Not found" 404'
    echo '	}'
  fi
  echo '	handle {'
  echo "		reverse_proxy $target {"
  [ -n "$upstream_host" ] && echo "			header_up Host $upstream_host"
  echo '			transport http {'
  echo '				forward_proxy_url http://127.0.0.1:1055'
  [ -n "$sni" ] && echo "				tls_server_name $sni"
  [ "${BRIDGE_TLS_INSECURE:-false}" = true ] && echo '				tls_insecure_skip_verify'
  echo '			}'
  echo '		}'
  echo '	}'
  echo '}'
} > "$conf"

if [ "${1:-}" = --render ]; then cat "$conf"; exit 0; fi

: "${TS_LOGIN_SERVER:?TS_LOGIN_SERVER is required}"
keyfile="${TS_AUTHKEY_FILE:?TS_AUTHKEY_FILE is required}"
[ -s "$keyfile" ] || die "no pre-auth key in $keyfile yet (the control server's keys sidecar fills it) — exiting so the pod restarts and reads it again"
case "$TS_LOGIN_SERVER" in https://*|http://*) ;; *) die "TS_LOGIN_SERVER must be a URL" ;; esac
caddy validate --config "$conf" --adapter caddyfile >/dev/null 2>&1 || { caddy validate --config "$conf" --adapter caddyfile; die "the generated Caddyfile is invalid"; }

state="${TS_STATE_DIR:-/tmp/tailscale}"
sock="$state/tailscaled.sock"
mkdir -p "$state"
tailscaled --tun=userspace-networking --statedir="$state" --socket="$sock" \
  --outbound-http-proxy-listen=127.0.0.1:1055 &
tsd=$!
for _ in $(seq 1 30); do [ -S "$sock" ] && break; sleep 1; done
tailscale --socket="$sock" up --login-server="$TS_LOGIN_SERVER" --auth-key="file:$keyfile" \
  --hostname="${TS_HOSTNAME:-tailnet-bridge}" --accept-dns=true --timeout=120s \
  || { kill "$tsd" 2>/dev/null; die "could not join $TS_LOGIN_SERVER"; }
echo "tailnet-bridge: joined as $(tailscale --socket="$sock" ip -4 2>/dev/null | head -n1); forwarding to $target"

caddy run --config "$conf" --adapter caddyfile &
cad=$!
trap 'kill "$cad" "$tsd" 2>/dev/null' TERM INT
# Either process ending ends the pod: a bridge without the network, or without its proxy, serves nothing.
while kill -0 "$tsd" 2>/dev/null && kill -0 "$cad" 2>/dev/null; do sleep 5; done
echo "tailnet-bridge: $(kill -0 "$tsd" 2>/dev/null && echo caddy || echo tailscaled) exited" >&2
kill "$cad" "$tsd" 2>/dev/null || true
exit 1
