#!/usr/bin/env bash
#
# Does the boot page ANSWER what it promises? Renders the chart's boot page (memex-local layers),
# runs its nginx config and HTML in the image the chart names, and asserts every path — root,
# non-root, a POST with a body over nginx's 1m default — answers 503 with Retry-After: 5,
# Cache-Control: no-store and the spinner page.
#
#   deploy/aks/scripts/check-boot-page-serves.sh     exit 0 = the contract holds, 1 = it does not
#
# No skip: docker, helm and PyYAML are inputs, asserted below and named when missing.
set -uo pipefail
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SELF_DIR/../../.." && pwd)"
fail=0
report() { echo "::error::$1"; fail=1; }

missing=()
command -v helm >/dev/null 2>&1   || missing+=("helm not on PATH")
command -v docker >/dev/null 2>&1 || missing+=("docker not on PATH")
python3 -c "import yaml" >/dev/null 2>&1 || missing+=("python3 with PyYAML")
if [ ${#missing[@]} -gt 0 ]; then
  echo "::error::check-boot-page-serves cannot run — provide: ${missing[*]}"; exit 1
fi

WORK="$(mktemp -d)"; NAME="boot-page-check-$$"
trap 'docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
helm template release "$REPO/deploy/helm" --namespace check \
  -f "$REPO/deploy/helm/values.yaml" -f "$REPO/deploy/homebrew/share/values.local.defaults.yaml" \
  --set ingress.enabled=true > "$WORK/all.yaml" || { echo "::error::the chart does not render"; exit 1; }
python3 - "$WORK" <<'PY' || { echo "::error::the boot page ConfigMap/Deployment did not render"; exit 1; }
import sys, yaml, os
w = sys.argv[1]
docs = [d for d in yaml.safe_load_all(open(os.path.join(w, "all.yaml"))) if d]
boot = {d["kind"]: d for d in docs if d.get("metadata", {}).get("name") == "memex-boot-page"}
for k, v in boot["ConfigMap"]["data"].items():
    open(os.path.join(w, k), "w").write(v)
open(os.path.join(w, "image"), "w").write(boot["Deployment"]["spec"]["template"]["spec"]["containers"][0]["image"])
PY
IMAGE="$(cat "$WORK/image")"
# Copied in rather than bind-mounted, so the check runs on any docker host (a VM-backed daemon does
# not see the runner's temp directory).
docker pull -q "$IMAGE" >/dev/null || { echo "::error::could not pull $IMAGE"; exit 1; }
docker create --name "$NAME" "$IMAGE" >/dev/null \
  && docker cp "$WORK/default.conf" "$NAME:/etc/nginx/conf.d/default.conf" \
  && docker cp "$WORK/boot.html" "$NAME:/usr/share/nginx/html/boot.html" \
  && docker start "$NAME" >/dev/null \
  || { echo "::error::could not start $IMAGE with the rendered boot page"; exit 1; }
# curl runs in its own container on the boot page's network namespace: no published port, so the
# check reads the same on a hosted runner and on a VM-backed daemon that forwards none.
CURL_IMAGE="curlimages/curl:8.11.1"
docker pull -q "$CURL_IMAGE" >/dev/null || { echo "::error::could not pull $CURL_IMAGE"; exit 1; }
dcurl() { docker run --rm -i --network "container:$NAME" "$CURL_IMAGE" "$@"; }
for _ in $(seq 1 30); do dcurl -s -o /dev/null http://127.0.0.1:8080/ && break; sleep 1; done

check() { # label, extra curl args…, url — prints headers then body; stdin may carry a request body
  local label="$1"; shift
  local out code
  out="$(dcurl -s -D - -w '\n%{http_code}' "$@")"
  code="$(printf '%s' "$out" | tail -n1)"
  local before=$fail
  [ "$code" = 503 ] || report "$label answered $code, want 503"
  printf '%s' "$out" | grep -qi '^Retry-After: 5' || report "$label lacks Retry-After: 5"
  printf '%s' "$out" | grep -qi '^Cache-Control: no-store' || report "$label lacks Cache-Control: no-store"
  printf '%s' "$out" | grep -q 'class="spin"' || report "$label body is not the spinner page"
  [ "$fail" -eq "$before" ] && echo "✅ $label → 503, Retry-After 5, no-store, spinner"
}
check "GET /" http://127.0.0.1:8080/ < /dev/null
check "GET /some/deep/path?x=1" 'http://127.0.0.1:8080/some/deep/path?x=1' < /dev/null
# Over nginx's 1m default client_max_body_size: must still be the retryable 503, never a 413.
# The body comes from a file on stdin, never a pipe: a piped `check` runs in a subshell and its
# `fail` would never reach the exit status.
head -c 2000000 /dev/zero > "$WORK/big.bin"
check "POST 2 MB body" -X POST --data-binary @- http://127.0.0.1:8080/api/upload < "$WORK/big.bin"
[ "$fail" -ne 0 ] && docker logs "$NAME" 2>&1 | tail -20
exit "$fail"
