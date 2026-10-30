#!/usr/bin/env bash
#
# The top-level `enabled` switch of the whisper and s1-extract service charts (MeshWeaver.Deploy#38),
# checked the only ways that can fail without a cluster:
#
#   1. the DEFAULT renders the chart's full object set (whisper: Service + Deployment; s1-extract:
#      NetworkPolicy, ServiceAccount, ClusterRoleBinding, Service, Deployment) — the switch is ON by
#      default (opt-out), unlike ocr and ollama, because estates already run both charts;
#   2. `--set enabled=true` renders BYTE-IDENTICAL output to the default — declaring the switch
#      changes nothing for an estate that does not set it, so the vendored copies re-vendor as-is;
#   3. `--set enabled=false` renders NO object at all — in particular no cluster-wide
#      ClusterRoleBinding for s1-extract — and needs none of the values the enabled chart requires;
#   4. NEGATIVE CONTROL: the enabled charts still refuse their required values (whisper without
#      image.tag, s1-extract without image.repository), so (3) is the switch, not a broken render.
#
# NO SKIP-TRAPDOOR: helm, python3 and PyYAML are asserted; a render that yields nothing where objects
# are expected is RED.
set -uo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SELF_DIR/../../.." && pwd)"
WHISPER="$REPO/deploy/whisper/helm"
S1="$REPO/deploy/s1-extract/helm"
fail=0
report() { echo "::error::$1"; fail=1; }
ok() { echo "✅ $1"; }

command -v helm >/dev/null 2>&1 || { echo "::error::helm is not on PATH"; exit 1; }
python3 -c "import yaml" >/dev/null 2>&1 || { echo "::error::python3 with PyYAML is required (pip install pyyaml)"; exit 1; }

# The sorted kinds of every object a render produced, comma-separated ("" when none).
KINDS='
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if isinstance(d, dict)]
print(",".join(sorted(d.get("kind", "?") for d in docs)))
'
kinds() { python3 -c "$KINDS"; }

WHISPER_REQ=(--set image.tag=1.7.4-flix-large-v3-q8-abc1234)
S1_REQ=(--set image.repository=registry.example/s1-extract --set 'auth.allowedServiceAccounts[0]=system:serviceaccount:memex:portal')

check_chart() {
  local name="$1" chart="$2" expected="$3"; shift 3
  local req=("$@")

  # ---- 1. the default renders the full set ---------------------------------------------------
  local def on off k
  def="$(helm template t "$chart" "${req[@]}" 2>&1)" || { report "$name: the default does not render: $def"; return; }
  k="$(printf '%s' "$def" | kinds)"
  if [ "$k" = "$expected" ]; then
    ok "$name: the default (enabled: true) renders [$k]"
  else
    report "$name: the default rendered [$k], expected [$expected] — the switch must be ON by default"
  fi

  # ---- 2. enabled=true is byte-identical to the default ----------------------------------------
  on="$(helm template t "$chart" "${req[@]}" --set enabled=true 2>&1)" || { report "$name: enabled=true does not render: $on"; return; }
  if [ "$on" = "$def" ]; then
    ok "$name: enabled=true renders byte-identical to the default"
  else
    report "$name: enabled=true differs from the default render"
    diff <(printf '%s\n' "$def") <(printf '%s\n' "$on") | head -40
  fi

  # ---- 3. enabled=false renders nothing, with and without the required values ------------------
  for variant in with-values without-values; do
    if [ "$variant" = with-values ]; then
      off="$(helm template t "$chart" "${req[@]}" --set enabled=false 2>&1)" || { report "$name: enabled=false ($variant) does not render: $off"; continue; }
    else
      off="$(helm template t "$chart" --set enabled=false 2>&1)" || { report "$name: enabled=false ($variant) refused — a disabled chart must need no values: $off"; continue; }
    fi
    k="$(printf '%s' "$off" | kinds)"
    if [ -z "$k" ]; then
      ok "$name: enabled=false ($variant) renders no object"
    else
      report "$name: enabled=false ($variant) rendered [$k]; a disabled chart must render nothing"
    fi
  done
}

check_chart whisper "$WHISPER" "Deployment,Service" "${WHISPER_REQ[@]}"
check_chart s1-extract "$S1" "ClusterRoleBinding,Deployment,NetworkPolicy,Service,ServiceAccount" "${S1_REQ[@]}"

# ---- 4. negative control: the enabled charts still refuse missing required values --------------
if helm template t "$WHISPER" >/dev/null 2>&1; then
  report "whisper: the enabled chart rendered without image.tag — the required-tag refusal is gone, so (3) proves nothing"
else
  ok "whisper: the enabled chart still refuses a missing image.tag"
fi
if helm template t "$S1" --set 'auth.allowedServiceAccounts[0]=system:serviceaccount:memex:portal' >/dev/null 2>&1; then
  report "s1-extract: the enabled chart rendered without image.repository — the refusal is gone, so (3) proves nothing"
else
  ok "s1-extract: the enabled chart still refuses a missing image.repository"
fi

exit "$fail"
