#!/usr/bin/env bash
#
# The node-pool headroom (templates/node-headroom/, Memex#863; core Doc/Architecture/DeploymentAKS →
# "A spare slot per node pool"), checked the only ways that can fail without a cluster:
#
#   1. NEGATIVE CONTROL — the chart defaults (nodeHeadroom.pools: []) render NO placeholder Deployment:
#      only the release that declares a pool (the control record's) carries one;
#   2. one declared pool renders exactly ONE Deployment `node-headroom-<pool>` whose pod REFERENCES the
#      PriorityClass `node-headroom`, is pinned to the pool by kubernetes.azure.com/agentpool, holds
#      requests == limits == the declared size, terminates at once (grace 0), runs non-root with a
#      read-only root, dropped capabilities, no service-account token, and the declared replicas;
#   3. two pools render two Deployments;
#   4. NO render — default, one pool or two — carries a PriorityClass: the class is INFRA-DECLARED
#      (Systemorph/Memex deployments/aks/cluster-bootstrap/, applied by the governed InfraDeploy) and the
#      hosting operator holds no write on priorityclasses, so a chart that rendered one would be refused
#      by hosting-deploy's preflight on the first release that declares a pool;
#   5. the refusals hold: a `priority` value (the class is not the chart's), a pool declared twice, a
#      name that is not a pool name, and fewer than one replica fail `helm template`, naming why.
#
# NO SKIP-TRAPDOOR: helm, python3 and PyYAML are asserted; a render that yields nothing is RED.
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

render() { helm template t "$CHART" "$@" 2>&1; }

# The objects of the headroom component (and every PriorityClass), as one JSON list.
INSPECT='
import json, sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if isinstance(d, dict)]
out = [d for d in docs
       if ((d.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/component") == "node-headroom"
       or d.get("kind") == "PriorityClass"]
print(json.dumps(out))
'
inspect() { python3 -c "$INSPECT"; }

# ---- 1. negative control: the defaults render nothing -----------------------------------------
default_out="$(render)" || { report "the chart defaults do not render: $default_out"; default_out=""; }
[ -n "$default_out" ] || report "the chart defaults rendered NOTHING — the negative control checked nothing"
n="$(printf '%s' "$default_out" | inspect | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
if [ "$n" = "0" ] && [ -n "$default_out" ]; then
  ok "the defaults (nodeHeadroom.pools: []) render no PriorityClass and no placeholder"
else
  report "the defaults rendered $n node-headroom object(s); an undeclared release must render none"
fi

# ---- 2. one pool -------------------------------------------------------------------------------
one_out="$(render -f "$TD/values.node-headroom.yaml")" || { report "the one-pool fixture does not render: $one_out"; one_out=""; }
inspected=1
problems="$(printf '%s' "$one_out" | inspect | python3 -c '
import json, sys
objs = json.load(sys.stdin)
p = []
pcs = [o for o in objs if o["kind"] == "PriorityClass"]
deps = [o for o in objs if o["kind"] == "Deployment"]
if pcs: p.append(f"{len(pcs)} PriorityClass object(s) rendered — the class is infra-declared, the chart must only reference it")
if len(deps) != 1: p.append(f"{len(deps)} placeholder Deployments, expected exactly one")
else:
    d = deps[0]
    if d["metadata"]["name"] != "node-headroom-silos": p.append("the placeholder is not named node-headroom-silos")
    if d["spec"].get("replicas") != 1: p.append("replicas " + repr(d["spec"].get("replicas")) + ", expected the default 1")
    ru = ((d["spec"].get("strategy") or {}).get("rollingUpdate") or {})
    if ru.get("maxSurge") != 0 or ru.get("maxUnavailable") != 1: p.append("the placeholder does not roll maxSurge 0 / maxUnavailable 1, so an update stalls on a full pool: " + repr(d["spec"].get("strategy")))
    s = d["spec"]["template"]["spec"]
    if s.get("priorityClassName") != "node-headroom": p.append("the pod does not run under node-headroom")
    if (s.get("nodeSelector") or {}).get("kubernetes.azure.com/agentpool") != "silos": p.append("the pod is not pinned to the silos pool")
    if s.get("terminationGracePeriodSeconds") != 0: p.append("the pod does not terminate at once (grace 0)")
    if s.get("automountServiceAccountToken") is not False: p.append("the pod mounts a service-account token")
    if (s.get("securityContext") or {}).get("runAsNonRoot") is not True: p.append("the pod is not runAsNonRoot")
    cs = s.get("containers") or []
    if len(cs) != 1: p.append(f"{len(cs)} containers, expected one pause container")
    else:
        c = cs[0]
        if "pause" not in c.get("image", ""): p.append("the container image is not a pause image: " + repr(c.get("image")))
        r = c.get("resources") or {}
        want = {"cpu": "4", "memory": "8Gi"}
        if r.get("requests") != want or r.get("limits") != want: p.append(f"resources {r} are not requests == limits == {want}")
        sc = c.get("securityContext") or {}
        if sc.get("readOnlyRootFilesystem") is not True or sc.get("allowPrivilegeEscalation") is not False or sc.get("capabilities", {}).get("drop") != ["ALL"]:
            p.append("the container is not read-only / no-escalation / all capabilities dropped")
print("\n".join(p))
')" || inspected=0
if [ -z "$one_out" ]; then report "the one-pool fixture rendered nothing"
elif [ "$inspected" != 1 ]; then report "the one-pool inspector itself FAILED (see its error above) — nothing was checked"
elif [ -n "$problems" ]; then while IFS= read -r line; do report "one pool: $line"; done <<< "$problems"
else ok "one declared pool renders one pinned, requests==limits placeholder that references the infra-declared node-headroom class, and no PriorityClass"; fi

# ---- 3. two pools ------------------------------------------------------------------------------
two_out="$(render -f "$TD/values.node-headroom-two-pools.yaml")" || { report "the two-pool fixture does not render: $two_out"; two_out=""; }
counts="$(printf '%s' "$two_out" | inspect | python3 -c '
import json, sys
objs = json.load(sys.stdin)
pcs = sum(1 for o in objs if o["kind"] == "PriorityClass")
deps = sorted((o["metadata"]["name"], o["spec"]["replicas"]) for o in objs if o["kind"] == "Deployment")
print(pcs, deps)')"
if [ "$counts" = "0 [('node-headroom-cibuildv6', 2), ('node-headroom-silos', 1)]" ]; then
  ok "two pools render two placeholders (with their own replicas) and no PriorityClass"
else
  report "two pools rendered '$counts', expected no PriorityClass and node-headroom-cibuildv6 x2 + node-headroom-silos x1"
fi

# ---- 4. refusals -------------------------------------------------------------------------------
refuse() { # fixture, expected substring
  local out
  if out="$(render -f "$TD/$1")"; then
    report "$1 rendered; it must be refused ($2)"
  elif printf '%s' "$out" | grep -qF "$2"; then
    ok "$1 is refused: $2"
  else
    report "$1 failed, but not naming '$2': $out"
  fi
}
refuse values.node-headroom-priority-value.yaml "INFRA-DECLARED"
refuse values.node-headroom-pool-twice.yaml "is declared twice"
refuse values.node-headroom-bad-pool.yaml "is not a node pool name"
refuse values.node-headroom-zero-replicas.yaml "must be at least 1"

exit "$fail"
