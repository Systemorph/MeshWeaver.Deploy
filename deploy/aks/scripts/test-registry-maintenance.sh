#!/usr/bin/env bash
#
# The registry's maintenance window (templates/registry/, registry.maintenance.*; MeshWeaver.Plugins#4370,
# slice 3; deploy/aks/REGISTRY-MAINTENANCE.md), checked the only ways that can fail without a cluster:
#
#   1. NEGATIVE CONTROL — the complete registry example with no maintenance key set renders NO read-only
#      block, NO maintenance annotation and NO collect Job; and the explicit defaults
#      (readOnly=false, garbageCollect="") render byte-identically to that;
#   2. readOnly=true renders `storage.maintenance.readonly.enabled: true` in the registry configuration,
#      and the registry pod template carries `memex-registry/maintenance: read-only` and a checksum/config
#      that differs from the read-write one (so the apply rolls the pods), and still no Job;
#   3. readOnly=true + garbageCollect=<run> renders ONE Job memex-registry-gc-<run> that runs
#      `garbage-collect /etc/registry/config.yml` with NO --delete-untagged, as the registry's own
#      ServiceAccount and workload identity, over the same ConfigMap, never retried (backoffLimit 0),
#      with the declared deadline — and leaves the registry pod template exactly as the read-only render;
#   4. the refusals hold: a collect outside a read-only window, a run id that is not one, a readOnly that
#      is not a boolean, and a deadline outside 60..2400.
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

RUN="20261110101500"
render() { helm template t "$CHART" -f "$EXAMPLE" "$@" 2>&1; }

# What the maintenance window touches in one render, as JSON.
INSPECT='
import json, sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if isinstance(d, dict)]
def meta(d): return d.get("metadata") or {}
def comp(d): return (meta(d).get("labels") or {}).get("app.kubernetes.io/component", "")
cm = [d for d in docs if d.get("kind") == "ConfigMap" and meta(d).get("name") == "memex-registry-config"]
config = yaml.safe_load(cm[0]["data"]["config.yml"]) if cm else None
dep = [d for d in docs if d.get("kind") == "Deployment" and meta(d).get("name") == "memex-registry-deployment"]
tmpl = dep[0]["spec"]["template"] if dep else {}
jobs = [d for d in docs if d.get("kind") == "Job" and comp(d) == "memex-registry-gc"]
print(json.dumps({
  "configmaps": len(cm), "deployments": len(dep),
  "readonly": ((config or {}).get("storage", {}).get("maintenance", {}) or {}).get("readonly"),
  "annotations": (tmpl.get("metadata") or {}).get("annotations") or {},
  "podSpec": tmpl.get("spec"),
  "registrySa": (tmpl.get("spec") or {}).get("serviceAccountName"),
  "registryImage": ((tmpl.get("spec") or {}).get("containers") or [{}])[0].get("image"),
  "jobs": jobs,
}))
'
inspect() { python3 -c "$INSPECT"; }

base="$(render)" || { report "the registry example does not render: $base"; exit 1; }
explicit="$(render --set registry.maintenance.readOnly=false --set-string registry.maintenance.garbageCollect=)" \
  || { report "the explicit defaults do not render: $explicit"; exit 1; }
ro="$(render --set registry.maintenance.readOnly=true)" || { report "readOnly=true does not render: $ro"; exit 1; }
gc="$(render --set registry.maintenance.readOnly=true --set-string registry.maintenance.garbageCollect="$RUN")" \
  || { report "a collect inside a read-only window does not render: $gc"; exit 1; }
[ -n "$base" ] && [ -n "$ro" ] && [ -n "$gc" ] || { report "a render yielded nothing"; exit 1; }

if ! { B="$(printf '%s' "$base" | inspect)" && R="$(printf '%s' "$ro" | inspect)" && G="$(printf '%s' "$gc" | inspect)"; }; then
  report "the inspection itself failed, so there is no evidence"; exit 1
fi

# The three verdicts, from ONE python program read from a quoted heredoc (no shell escaping inside
# it). 🚨 Fail-closed: a program that does not run, or prints no verdict line for a check, is RED —
# an empty verdict string is never read as a pass.
verdicts="$(B="$B" R="$R" G="$G" RUN="$RUN" python3 - <<'PY'
import json, os
b = json.loads(os.environ["B"]); r = json.loads(os.environ["R"]); g = json.loads(os.environ["G"]); run = os.environ["RUN"]

def verdict(name, problems):
    print(name + "\t" + ("PASS" if not problems else "; ".join(problems)))

p = []
if b["configmaps"] != 1 or b["deployments"] != 1: p.append("the registry ConfigMap and Deployment did not both render")
if b["readonly"] is not None: p.append("a read-only block renders with no maintenance key: %r" % (b["readonly"],))
if "memex-registry/maintenance" in b["annotations"]: p.append("the maintenance annotation renders with no maintenance key")
if b["jobs"]: p.append("%d collect Job(s) render with no maintenance key" % len(b["jobs"]))
verdict("negative", p)

p = []
if r["readonly"] != {"enabled": True}: p.append("storage.maintenance.readonly is %r, expected {enabled: true}" % (r["readonly"],))
if r["annotations"].get("memex-registry/maintenance") != "read-only": p.append("the registry pod template is not annotated read-only")
if not r["annotations"].get("checksum/config") or r["annotations"].get("checksum/config") == b["annotations"].get("checksum/config"):
    p.append("checksum/config does not change with readOnly, so the apply would not roll the registry pods")
if r["jobs"]: p.append("a collect Job renders without garbageCollect")
verdict("readonly", p)

p = []
if len(g["jobs"]) != 1:
    p.append("%d collect Jobs, expected exactly one" % len(g["jobs"]))
else:
    job = g["jobs"][0]; spec = job["spec"]; tmeta = spec["template"].get("metadata") or {}
    pod = spec["template"]["spec"]; c = pod["containers"][0]
    words = [str(x) for x in (c.get("args") or []) + (c.get("command") or [])]
    if job["metadata"]["name"] != "memex-registry-gc-" + run: p.append("the Job is named %r" % job["metadata"]["name"])
    if c.get("args") != ["garbage-collect", "/etc/registry/config.yml"]: p.append("the Job runs %r" % (c.get("args"),))
    if any("delete-untagged" in w for w in words): p.append("the Job deletes untagged manifests")
    if c.get("image") != g["registryImage"]: p.append("the Job does not run the registry image")
    if pod.get("serviceAccountName") != g["registrySa"]: p.append("the Job does not run as the registry ServiceAccount")
    if (tmeta.get("labels") or {}).get("azure.workload.identity/use") != "true": p.append("the Job pod does not use the workload identity")
    if pod.get("restartPolicy") != "Never" or spec.get("backoffLimit") != 0: p.append("the Job may be retried")
    if spec.get("activeDeadlineSeconds") != 1800: p.append("the Job deadline is %r, expected the default 1800" % (spec.get("activeDeadlineSeconds"),))
    if not any((v.get("configMap") or {}).get("name") == "memex-registry-config" for v in pod.get("volumes") or []):
        p.append("the Job does not mount the registry configuration")
    if (tmeta.get("annotations") or {}).get("checksum/config") != r["annotations"].get("checksum/config"):
        p.append("the Job does not carry the read-only configuration checksum")
if g["annotations"] != r["annotations"] or g["podSpec"] != r["podSpec"]:
    p.append("rendering the collect changes the registry pod template - it would roll the pods it just verified")
verdict("collect", p)
PY
)" || { report "the verdict program failed, so there is no evidence: $verdicts"; exit 1; }
judged() {  # judged <check> <pass sentence>
  local line
  line="$(printf '%s\n' "$verdicts" | grep "^$1"$'\t')" || { report "$1: no verdict was printed — there is no evidence"; return; }
  if [ "${line#*$'\t'}" = PASS ]; then ok "$2"; else report "$1: ${line#*$'\t'}"; fi
}

# ---- 1. negative control ---------------------------------------------------------------------
judged negative "no maintenance key: no read-only block, no maintenance annotation, no collect Job"
if [ "$base" = "$explicit" ]; then ok "the explicit defaults (readOnly=false, garbageCollect=\"\") render byte-identically"
else report "the explicit defaults change the render — an installation would roll its registry for nothing"; fi

# ---- 2. read-only ----------------------------------------------------------------------------
judged readonly "readOnly=true: readonly.enabled in the configuration, the pod template annotated read-only with a new checksum, no Job"

# ---- 3. the collect --------------------------------------------------------------------------
judged collect "a collect: ONE Job memex-registry-gc-$RUN, garbage-collect with no --delete-untagged, the registry's image, account and configuration, never retried; the registry pods are untouched"

# ---- 4. refusals -----------------------------------------------------------------------------
refuses() {
  local why="$1" needle="$2"; shift 2
  local out
  if out="$(render "$@")"; then report "$why rendered — it must be refused"; return; fi
  if printf '%s' "$out" | grep -qF "$needle"; then ok "$why is refused, naming why"
  else report "$why failed, but not naming why: $out"; fi
}
refuses "a collect outside a read-only window" "needs registry.maintenance.readOnly: true" \
  --set-string registry.maintenance.garbageCollect="$RUN"
refuses "a run id that is not one" "is not a run id" \
  --set registry.maintenance.readOnly=true --set-string registry.maintenance.garbageCollect='Bad_Run'
refuses "a readOnly that is not a boolean" "must be true or false" \
  --set-string registry.maintenance.readOnly=yes
refuses "a collect deadline past the job cut" "is outside 60..2400" \
  --set registry.maintenance.readOnly=true --set-string registry.maintenance.garbageCollect="$RUN" --set registry.maintenance.gcDeadlineSeconds=3600

exit $fail
