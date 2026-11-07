#!/usr/bin/env bash
# headscale-keys — keeps every DECLARED pre-auth key valid in its own Kubernetes Secret.
#
# Runs as a sidecar in the headscale pod (helm/templates/deployment.yaml, `preauthKeys.enabled`). It
# reaches headscale over the pod's shared unix socket and the API server with a projected token that
# only this container mounts, whose Role may get/update/patch exactly the Secrets the chart created
# (it cannot create, list or read any other Secret).
#
# Why a declaration and not a command: a machine that joins the network unattended (the ingress
# bridge, a build host) needs a pre-auth key, and minting one by hand means `kubectl exec` into the
# control server and pasting the key somewhere. Here the overlay DECLARES the keys (name, user,
# reusable, ephemeral, lifetime), ApplyChart applies that declaration under its approval, and this loop
# makes the cluster match it: a Secret without a key, with a key about to expire, with a key headscale
# no longer knows, or with a single-use key already used, gets a fresh key. The key itself is never
# printed, never on an argv (a mode-600 file, then curl --data-binary @file), and never leaves the pod
# except into its Secret.
#
#   headscale-keys            loop forever (KEYS_INTERVAL seconds between passes, default 600)
#   headscale-keys --once     one pass; exit status = the number of keys that could not be ensured
#
# Inputs (env): KEYS_CONFIG (default /etc/headscale-keys/keys.json — a JSON array of
#   {name, user, secret, reusable, ephemeral, validHours, renewHours}), HEADSCALE_CONFIG (default
#   /etc/headscale/config.yaml), KUBE_API (default https://kubernetes.default.svc), KUBE_TOKEN_FILE,
#   KUBE_CA_FILE, KUBE_NAMESPACE_FILE (default under /var/run/secrets/headscale-keys).
set -uo pipefail

KEYS_CONFIG="${KEYS_CONFIG:-/etc/headscale-keys/keys.json}"
HEADSCALE_CONFIG="${HEADSCALE_CONFIG:-/etc/headscale/config.yaml}"
KUBE_API="${KUBE_API:-https://kubernetes.default.svc}"
SA_DIR="${KUBE_SA_DIR:-/var/run/secrets/headscale-keys}"
KUBE_TOKEN_FILE="${KUBE_TOKEN_FILE:-$SA_DIR/token}"
KUBE_CA_FILE="${KUBE_CA_FILE:-$SA_DIR/ca.crt}"
KUBE_NAMESPACE_FILE="${KUBE_NAMESPACE_FILE:-$SA_DIR/namespace}"
KEYS_INTERVAL="${KEYS_INTERVAL:-600}"
ANN="headscale.meshweaver.net"

log() { printf '%s headscale-keys %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
hs() { headscale -c "$HEADSCALE_CONFIG" "$@"; }

work="$(mktemp -d)"; chmod 700 "$work"
trap 'rm -rf "$work"' EXIT

kube() { # kube METHOD PATH [BODY_FILE] → body on stdout, HTTP code in $work/code
  local method="$1" path="$2" body="${3:-}" ca=() args=()
  [ -f "$KUBE_CA_FILE" ] && ca=(--cacert "$KUBE_CA_FILE")
  [ -n "$body" ] && args=(-H 'Content-Type: application/merge-patch+json' --data-binary "@$body")
  curl -sS --max-time 20 "${ca[@]}" -X "$method" \
    -H "Authorization: Bearer $(cat "$KUBE_TOKEN_FILE")" -H 'Accept: application/json' \
    "${args[@]}" -o "$work/resp" -w '%{http_code}' "$KUBE_API$path" > "$work/code" 2>"$work/curl.err" \
    || { echo 000 > "$work/code"; }
  cat "$work/resp" 2>/dev/null || true
}

user_id() { # the numeric id of a headscale user, created when absent
  local name="$1" id
  id=$(hs users list -o json 2>/dev/null | jq -r --arg n "$name" '[.[]? | select(.name == $n) | .id][0] // empty')
  if [ -z "$id" ]; then
    hs users create "$name" >/dev/null 2>&1 || { log "user $name: could not be created"; return 1; }
    id=$(hs users list -o json 2>/dev/null | jq -r --arg n "$name" '[.[]? | select(.name == $n) | .id][0] // empty')
    [ -n "$id" ] && log "user $name: created (id $id)"
  fi
  [ -n "$id" ] && printf '%s' "$id"
}

# The declaration a key was minted for: user, flags and lifetime. A changed declaration is a new key — a
# still-valid key must not keep enrolling machines under the user or flags it was minted for.
declaration_of() { jq -c '{user, reusable: (.reusable // false), ephemeral: (.ephemeral // false), validHours: (.validHours // 720)}' <<<"$1" | sha256sum | cut -c1-16; }

# Expire one key and confirm headscale no longer accepts it (an already expired or deleted key is done).
revoke() {
  local id="$1" now; now=$(date +%s)
  case "$id" in ''|*[!0-9]*) return 0 ;; esac
  hs preauthkeys expire -i "$id" >/dev/null 2>&1
  [ "$(hs preauthkeys list -o json 2>/dev/null | jq -r --argjson id "$id" --argjson now "$now" \
        '[.[]? | select(.id == $id)][0] | if . == null then "done" elif (.expiration.seconds // 0) <= $now then "done" else "valid" end')" = done ]
}

# Why the key in a Secret must be replaced, or empty when it is still good.
renew_reason() {
  local secret_json="$1" reusable="$2" renew_hours="$3" decl="$4" now key_id expires has_key known stored_decl
  now=$(date +%s)
  has_key=$(jq -r '(.data.authkey // "") != ""' <<<"$secret_json")
  key_id=$(jq -r --arg a "$ANN/key-id" '.metadata.annotations[$a] // ""' <<<"$secret_json")
  expires=$(jq -r --arg a "$ANN/expires-at" '.metadata.annotations[$a] // "0"' <<<"$secret_json")
  stored_decl=$(jq -r --arg a "$ANN/declaration" '.metadata.annotations[$a] // ""' <<<"$secret_json")
  [ "$has_key" = true ] || { echo "no key yet"; return; }
  [ -n "$key_id" ] || { echo "the key carries no id"; return; }
  [ "$stored_decl" = "$decl" ] || { echo "the declaration changed (user, flags or lifetime)"; return; }
  case "$expires" in ''|*[!0-9]*) echo "the key carries no expiry"; return ;; esac
  [ "$now" -lt $((expires - renew_hours * 3600)) ] || { echo "the key expires within ${renew_hours}h"; return; }
  known=$(hs preauthkeys list -o json 2>/dev/null \
    | jq -r --argjson id "$key_id" --argjson now "$now" '[.[]? | select(.id == $id)][0]
        | if . == null then "gone" elif (.expiration.seconds // 0) <= $now then "expired"
          elif (.used // false) then "used" else "ok" end')
  case "$known" in
    gone) echo "headscale no longer knows key $key_id" ;;
    expired) echo "key $key_id was expired in headscale" ;;
    used) [ "$reusable" = true ] || echo "single-use key $key_id was used" ;;
    ok)   ;;
    *)    echo "headscale could not list its keys" ;;
  esac
}

# Keys this process minted but could not store (their Secret write failed) and could not expire either:
# retried every pass for the life of the process, and every pass reports them until they are gone.
ORPHANS="$work/orphans"; : > "$ORPHANS"

# Revoke every id in the Secret's pending list; store what is still pending. 0 when nothing is left.
drain_pending() {
  local ns="$1" secret="$2" name="$3" pending="$4" left="" id
  for id in $pending; do revoke "$id" && log "key $name: expired the replaced key $id" || left="$left $id"; done
  left="${left# }"
  if [ "$left" != "$pending" ]; then
    jq -n --arg a "$ANN/pending-expire" --arg v "$left" '{metadata: {annotations: {($a): (if $v == "" then null else $v end)}}}' > "$work/pending.json"
    kube PATCH "/api/v1/namespaces/$ns/secrets/$secret" "$work/pending.json" >/dev/null
    rm -f "$work/pending.json"
    [ "$(cat "$work/code")" = 200 ] || { log "key $name: could not record the pending revocations [$left] (HTTP $(cat "$work/code"))"; return 1; }
  fi
  [ -z "$left" ] || { log "key $name: replaced key(s) [$left] are STILL VALID — headscale refused to expire them; retrying next pass"; return 1; }
}

ensure_key() { # ensure_key <one entry of keys.json, as JSON> → 0 when the Secret holds a good key and nothing replaced is still valid
  local entry="$1" name user secret reusable ephemeral valid renew ns secret_json reason uid flags=() old_id decl pending new_id
  name=$(jq -r .name <<<"$entry"); user=$(jq -r .user <<<"$entry"); secret=$(jq -r .secret <<<"$entry")
  reusable=$(jq -r '.reusable // false' <<<"$entry"); ephemeral=$(jq -r '.ephemeral // false' <<<"$entry")
  valid=$(jq -r '.validHours // 720' <<<"$entry"); renew=$(jq -r '.renewHours // 168' <<<"$entry")
  decl=$(declaration_of "$entry")
  ns=$(cat "$KUBE_NAMESPACE_FILE")
  secret_json=$(kube GET "/api/v1/namespaces/$ns/secrets/$secret")
  case "$(cat "$work/code")" in
    200) ;;
    404) log "key $name: Secret $secret does not exist — the chart creates it; nothing minted"; return 1 ;;
    *)   log "key $name: reading Secret $secret answered HTTP $(cat "$work/code") $(head -c 200 "$work/curl.err")"; return 1 ;;
  esac
  pending=$(jq -r --arg a "$ANN/pending-expire" '.metadata.annotations[$a] // ""' <<<"$secret_json")
  reason=$(renew_reason "$secret_json" "$reusable" "$renew" "$decl")
  if [ -z "$reason" ]; then
    [ -z "$pending" ] || drain_pending "$ns" "$secret" "$name" "$pending" || return 1
    return 0
  fi
  uid=$(user_id "$user") || return 1
  [ "$reusable" = true ] && flags+=(--reusable)
  [ "$ephemeral" = true ] && flags+=(--ephemeral)
  umask 077
  if ! hs preauthkeys create -u "$uid" -e "${valid}h" "${flags[@]}" -o json > "$work/minted.json" 2>"$work/mint.err"; then
    log "key $name: headscale refused to mint ($(head -c 200 "$work/mint.err"))"; rm -f "$work/minted.json"; return 1
  fi
  jq -e '(.key // "") != "" and (.id != null) and (.expiration.seconds != null)' "$work/minted.json" >/dev/null \
    || { log "key $name: headscale answered without a key, id or expiry"; rm -f "$work/minted.json"; return 1; }
  new_id=$(jq -r .id "$work/minted.json")
  # The key it replaces joins the pending revocations IN THE SAME WRITE as the new key, so a failure
  # after this point never loses track of a credential that is still valid.
  old_id=$(jq -r --arg a "$ANN/key-id" '.metadata.annotations[$a] // ""' <<<"$secret_json")
  [ -n "$old_id" ] && [ "$old_id" != "$new_id" ] && pending="${pending:+$pending }$old_id"
  jq --arg a "$ANN" --arg reason "$reason" --arg decl "$decl" --arg pending "$pending" '{
      data: { authkey: (.key | @base64) },
      metadata: { annotations: {
        ($a + "/key-id"): (.id | tostring),
        ($a + "/expires-at"): (.expiration.seconds | tostring),
        ($a + "/minted-at"): (now | floor | tostring),
        ($a + "/declaration"): $decl,
        ($a + "/pending-expire"): (if $pending == "" then null else $pending end),
        ($a + "/reason"): $reason } } }' "$work/minted.json" > "$work/patch.json"
  rm -f "$work/minted.json"
  kube PATCH "/api/v1/namespaces/$ns/secrets/$secret" "$work/patch.json" >/dev/null
  rm -f "$work/patch.json"
  if [ "$(cat "$work/code")" != 200 ]; then
    log "key $name: writing Secret $secret answered HTTP $(cat "$work/code"); expiring the unstored key $new_id"
    revoke "$new_id" || { echo "$new_id" >> "$ORPHANS"; log "key $name: the unstored key $new_id could NOT be expired — retrying every pass"; }
    return 1
  fi
  log "key $name: minted key $new_id for user $user into Secret $secret ($reason)"
  # The replaced key: a machine that already joined keeps its registration; nothing new may use it.
  [ -z "$pending" ] || drain_pending "$ns" "$secret" "$name" "$pending" || return 1
  return 0
}

retry_orphans() { # → the number of unstored keys that are still valid
  local id left=0; [ -s "$ORPHANS" ] || return 0
  : > "$ORPHANS.next"
  while read -r id; do
    if revoke "$id"; then log "expired the unstored key $id"; else echo "$id" >> "$ORPHANS.next"; left=$((left + 1)); fi
  done < "$ORPHANS"
  mv "$ORPHANS.next" "$ORPHANS"
  [ "$left" = 0 ] || log "$left unstored key(s) are STILL VALID: $(tr '\n' ' ' < "$ORPHANS")"
  return "$left"
}

pass() {
  local failed=0 n entry
  # Wait for the server: the socket appears once headscale is serving.
  for _ in $(seq 1 60); do hs health >/dev/null 2>&1 && break; sleep 2; done
  hs health >/dev/null 2>&1 || { log "headscale does not answer on its socket"; return 1; }
  jq -e 'type == "array"' "$KEYS_CONFIG" >/dev/null 2>&1 || { log "$KEYS_CONFIG is not a JSON array"; return 1; }
  n=$(jq length "$KEYS_CONFIG")
  for i in $(seq 0 $((n - 1))); do
    entry=$(jq -c ".[$i]" "$KEYS_CONFIG")
    ensure_key "$entry" || failed=$((failed + 1))
  done
  retry_orphans || failed=$((failed + $?))
  return "$failed"
}

if [ "${1:-}" = --once ]; then pass; exit $?; fi
log "keeping $(jq length "$KEYS_CONFIG" 2>/dev/null || echo '?') declared key(s), a pass every ${KEYS_INTERVAL}s"
while true; do
  pass || log "pass ended with $? key(s) not ensured — retrying in ${KEYS_INTERVAL}s"
  sleep "$KEYS_INTERVAL"
done
