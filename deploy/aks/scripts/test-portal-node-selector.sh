#!/usr/bin/env bash
#
# The portal's nodeSelector (portal.nodeSelector, Systemorph/Memex#148), checked the only ways that can
# fail without a cluster:
#
#   1. NEGATIVE CONTROL: the chart defaults (portal.nodeSelector: {}) render the portal Deployment with
#      NO nodeSelector key at all, so a release that does not declare one renders as before;
#   2. a declared selector renders on the portal Deployment's pod spec exactly as declared;
#   3. two labels render as two labels, and the selector lands on the PORTAL Deployment only, never on
#      another workload of the release.
#
# NO SKIP-TRAPDOOR: helm, python3 and PyYAML are asserted; a render that yields no portal Deployment is RED.
set -uo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SELF_DIR/../../.." && pwd)"
CHART="$REPO/deploy/helm"
TD="$SELF_DIR/testdata"
fail=0
report() { echo "::error::$1"; fail=1; }
ok() { echo "✅ $1"; }

command -v helm >/dev/null 2>&1 || { echo "::error::helm is not on PATH"; exit 1; }
python3 -c "import yaml" >/dev/null 2>&1 || { echo "::error::python3 with PyYAML is required (pip install pyyaml)"; exit 1; }

# stdout only is parsed; helm's stderr goes to a file and is quoted only when the render FAILS, so a warning
# on a successful render can never corrupt the YAML the inspection reads.
ERR="$(mktemp)"; trap 'rm -f "$ERR"' EXIT
render() { helm template t "$CHART" "$@" 2>"$ERR" || { cat "$ERR"; return 1; }; }

# Prints one JSON object: {"portal": <nodeSelector or "ABSENT">, "others": [names of other workloads carrying the portal's selector]}
# or NOPORTAL / DUPLICATE-PORTAL when the render holds no portal Deployment, or more than one.
INSPECT='
import json, sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if isinstance(d, dict)]
portal = [d for d in docs if d.get("kind") == "Deployment" and (d.get("metadata") or {}).get("name") == "memex-portal-deployment"]
if len(portal) != 1:
    print("NOPORTAL" if not portal else "DUPLICATE-PORTAL x%d" % len(portal)); sys.exit(0)
spec = portal[0]["spec"]["template"]["spec"]
sel = spec["nodeSelector"] if "nodeSelector" in spec else "ABSENT"
others = []
if isinstance(sel, dict) and sel:
    for d in docs:
        if d is portal[0] or d.get("kind") not in ("Deployment", "StatefulSet", "Job", "CronJob"):
            continue
        tmpl = (d.get("spec") or {}).get("template") or ((d.get("spec") or {}).get("jobTemplate") or {}).get("spec", {}).get("template") or {}
        if ((tmpl.get("spec") or {}).get("nodeSelector") or {}) == sel:
            others.append(d["kind"] + "/" + d["metadata"]["name"])
print(json.dumps({"portal": sel, "others": others}, sort_keys=True))
'
inspect() { python3 -c "$INSPECT"; }

# ---- 1. negative control -----------------------------------------------------------------------
out="$(render)" || { report "the chart defaults do not render: $out"; out=""; }
got="$(printf '%s' "$out" | inspect)"
if [ "$got" = '{"others": [], "portal": "ABSENT"}' ]; then
  ok "the defaults render the portal Deployment with no nodeSelector key"
else
  report "the defaults: expected no nodeSelector on the portal Deployment, got: $got"
fi

# ---- 2. one label ------------------------------------------------------------------------------
out="$(render -f "$TD/values.portal-node-selector.yaml")" || { report "the one-label fixture does not render: $out"; out=""; }
got="$(printf '%s' "$out" | inspect)"
if [ "$got" = '{"others": [], "portal": {"workload": "silos"}}' ]; then
  ok "a declared selector renders on the portal pod spec as declared"
else
  report "one label: expected {workload: silos} on the portal only, got: $got"
fi

# ---- 3. two labels -----------------------------------------------------------------------------
out="$(render -f "$TD/values.portal-node-selector.yaml" --set portal.nodeSelector.zone=a)" || { report "the two-label render failed: $out"; out=""; }
got="$(printf '%s' "$out" | inspect)"
if [ "$got" = '{"others": [], "portal": {"workload": "silos", "zone": "a"}}' ]; then
  ok "two labels render as two labels, on the portal only"
else
  report "two labels: expected {workload: silos, zone: a} on the portal only, got: $got"
fi

[ "$fail" = 0 ] && echo "PASS: portal nodeSelector" || echo "FAIL: portal nodeSelector"
exit "$fail"
