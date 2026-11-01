#!/usr/bin/env bash
#
# The hosted registry's OWN workload identity (templates/registry/, registry.workloadIdentity.clientId;
# Systemorph/Memex#922), checked the only ways that can fail without a cluster:
#
#   1. NEGATIVE CONTROL — the complete registry example WITHOUT a client id renders no registry
#      ServiceAccount and runs BOTH registry pods as the portal's memex-portal-sa, as before the
#      field existed (stated or blank serviceAccount alike);
#   2. with a client id, ONE ServiceAccount `memex-registry-sa` renders, annotated
#      azure.workload.identity/client-id with that id and with no Kubernetes token mounted, and BOTH
#      registry Deployments run as it; a stated serviceAccount name is the one rendered and used;
#   3. the refusals hold: a client id that is not a GUID, and a client id beside the portal's own
#      memex-portal-sa (annotating it would move the PORTAL onto the registry's identity), fail
#      `helm template` naming why.
#
# NO SKIP-TRAPDOOR: helm, python3 and PyYAML are asserted; a render that yields nothing is RED.
set -uo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SELF_DIR/../../.." && pwd)"
CHART="$REPO/deploy/helm"
EXAMPLE="$CHART/values.registry.example.yaml"
fail=0
report() { echo "::error::$1"; fail=1; }
ok() { echo "✅ $1"; }

command -v helm >/dev/null 2>&1 || { echo "::error::helm is not on PATH"; exit 1; }
python3 -c "import yaml" >/dev/null 2>&1 || { echo "::error::python3 with PyYAML is required (pip install pyyaml)"; exit 1; }
[ -f "$EXAMPLE" ] || { echo "::error::$EXAMPLE is missing — nothing to render"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CLIENT_ID="11111111-2222-3333-4444-555555555555"

render() { helm template t "$CHART" -f "$EXAMPLE" "$@" 2>&1; }

# The registry's ServiceAccounts and the serviceAccountName of each registry Deployment, as JSON.
INSPECT='
import json, sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if isinstance(d, dict)]
def comp(d): return ((d.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/component", "")
sas = [d for d in docs if d.get("kind") == "ServiceAccount" and comp(d) == "memex-registry"]
deps = {d["metadata"]["name"]: (d["spec"]["template"]["spec"].get("serviceAccountName"))
        for d in docs if d.get("kind") == "Deployment" and comp(d).startswith("memex-registry")}
print(json.dumps({"sas": sas, "deps": deps}))
'
inspect() { python3 -c "$INSPECT"; }

# ---- 1. negative control: no client id -------------------------------------------------------
for variant in stated blank; do
  if [ "$variant" = "stated" ]; then out="$(render)"; else out="$(render --set registry.serviceAccount=)"; fi
  rc=$?
  [ $rc -eq 0 ] && [ -n "$out" ] || { report "the registry example ($variant serviceAccount) does not render: $out"; continue; }
  verdict="$(printf '%s' "$out" | inspect | python3 -c '
import json, sys
j = json.load(sys.stdin); p = []
if j["sas"]: p.append(f"{len(j['"'"'sas'"'"'])} registry ServiceAccount(s) rendered with no client id")
if len(j["deps"]) != 2: p.append(f"expected the two registry Deployments, found {sorted(j['"'"'deps'"'"'])}")
for name, sa in j["deps"].items():
    if sa != "memex-portal-sa": p.append(f"{name} runs as {sa!r}, not the portal account memex-portal-sa")
print("; ".join(p))')"
  if [ -z "$verdict" ]; then ok "no client id ($variant serviceAccount): no registry ServiceAccount, both registry pods run as memex-portal-sa"
  else report "no client id ($variant serviceAccount): $verdict"; fi
done

# ---- 2. a client id: the dedicated account -----------------------------------------------------
check_dedicated() {
  local expected="$1"; shift
  local out rc verdict
  out="$(render "$@")"; rc=$?
  [ $rc -eq 0 ] && [ -n "$out" ] || { report "the registry with a client id ($expected) does not render: $out"; return; }
  verdict="$(printf '%s' "$out" | inspect | EXPECTED="$expected" CLIENT_ID="$CLIENT_ID" python3 -c '
import json, os, sys
j = json.load(sys.stdin); p = []; want = os.environ["EXPECTED"]; cid = os.environ["CLIENT_ID"]
if len(j["sas"]) != 1: p.append(f"{len(j['"'"'sas'"'"'])} registry ServiceAccounts, expected exactly one")
else:
    sa = j["sas"][0]
    if sa["metadata"]["name"] != want: p.append(f"the account is {sa['"'"'metadata'"'"']['"'"'name'"'"']!r}, expected {want!r}")
    if (sa["metadata"].get("annotations") or {}).get("azure.workload.identity/client-id") != cid:
        p.append("the account is not annotated azure.workload.identity/client-id with the client id")
    if sa.get("automountServiceAccountToken") is not False: p.append("the account mounts a Kubernetes token")
if len(j["deps"]) != 2: p.append(f"expected the two registry Deployments, found {sorted(j['"'"'deps'"'"'])}")
for name, sa in j["deps"].items():
    if sa != want: p.append(f"{name} runs as {sa!r}, not {want!r}")
print("; ".join(p))')"
  if [ -z "$verdict" ]; then ok "a client id renders ONE annotated, tokenless account $expected and both registry pods run as it"
  else report "a client id ($expected): $verdict"; fi
}
check_dedicated memex-registry-sa --set registry.serviceAccount= --set registry.workloadIdentity.clientId="$CLIENT_ID"
check_dedicated registry-writer --set registry.serviceAccount=registry-writer --set registry.workloadIdentity.clientId="$CLIENT_ID"

# ---- 3. refusals -------------------------------------------------------------------------------
refuses() {
  local why="$1" needle="$2"; shift 2
  local out
  if out="$(render "$@")"; then report "$why rendered — it must be refused"; return; fi
  if printf '%s' "$out" | grep -qF "$needle"; then ok "$why is refused, naming why"
  else report "$why failed, but not naming why: $out"; fi
}
refuses "a client id that is not a GUID" "is not a client id" \
  --set registry.serviceAccount= --set registry.workloadIdentity.clientId=not-a-guid
refuses "a client id beside the portal's memex-portal-sa" "a dedicated identity needs a dedicated account" \
  --set registry.serviceAccount=memex-portal-sa --set registry.workloadIdentity.clientId="$CLIENT_ID"

exit $fail
