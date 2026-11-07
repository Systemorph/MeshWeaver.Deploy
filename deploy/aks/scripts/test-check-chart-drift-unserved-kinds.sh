#!/usr/bin/env bash
#
# Self-test for check-chart-drift.sh's LIVE reads on a cluster that does not serve a kind
# (MeshWeaver.Deploy#155).
#
# WHY THIS EXISTS. On a cluster without KEDA (keda.enabled: false) `kubectl get scaledobjects.keda.sh`
# answers "the server doesn't have a resource type", and the drift check treated that answer as fatal:
# the Audit's chart step stopped before comparing anything, on every such cluster, while hosting-audit
# — the first step of the same Audit — recorded the same kind as skipped. Both now source ONE rule,
# kind-not-served.sh. This file runs the real check-chart-drift.sh — the real chart, the real render,
# the real comparator — with kubectl and `helm get manifest` answered from the chart-drift fixtures,
# so chart-gate.yml exercises the read phase on every pull request with no cluster.
#
# THE CASES
#   1. not served, the render has no ScaledObject  → skipped BY NAME, and the comparison is made
#   2. not served, the render HAS a ScaledObject   → red, naming the kind, and no comparison
#   3. Forbidden                                   → red, no comparison (a kind that could not be read
#                                                    is never reported as clean)
#   4. served (an empty list)                      → compared, nothing skipped — the control for 1
#   5. the rule file absent                        → the preflight refuses, naming it
#   6. a discovery warning + Forbidden/Unauthorized → red: the authorization failure wins
#   7. a discovery warning + "not served"          → skipped (the control for 6)
#   8. the rule on the exact texts, line by line
# NEGATIVE CONTROLS — the rule is replaced, in a copy of this directory, by a wrong one, and the case
# that should catch it must go red:
#   N1. "nothing is ever not served" (the pre-#155 behaviour) → case 1 must fail
#   N2. "everything is not served"                            → case 3 must fail
#   N3. the first, whole-text version of the rule            → case 6 must fail (mixed output skipped)
# Without them, a test whose assertions could not tell the fix from the defect would still pass.
set -uo pipefail
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SELF_DIR/../../.." && pwd)"
CHART="$REPO/deploy/helm"
DATA="$SELF_DIR/testdata/chart-drift/clean"
VALUES="$SELF_DIR/testdata/values.record-render-no-vault-half.yaml"
fail=0

for required in "$SELF_DIR/check-chart-drift.sh" "$SELF_DIR/kind-not-served.sh" "$CHART/values.yaml" "$VALUES" \
                "$DATA/live-configmap.json" "$DATA/live-deployment.json" "$DATA/live-poddisruptionbudgets.json" \
                "$DATA/release-manifest.yaml" "$DATA/record.json"; do
  if [ ! -f "$required" ]; then
    echo "::error::missing input '$required' — this self-test cannot run, and a self-test that cannot run must never report a pass."
    exit 1
  fi
done
REAL_HELM="$(command -v helm || true)"
[ -n "$REAL_HELM" ] || { echo "::error::helm is not on PATH — the render is real, so this self-test needs it"; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "::error::python3 has no PyYAML — the render and the comparator need it"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── the transport, answered from fixtures ──────────────────────────────────────────────────────
# kubectl: the two named objects and the PDB list from the clean fixture; the ScaledObject list per
# $STUB_SO; any go-template read (an envFrom source's KEY NAMES) answers one key.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/kubectl" <<'SHEOF'
#!/usr/bin/env bash
res="" tmpl=""
while [ $# -gt 0 ]; do
  case "$1" in
    get) res="$2"; shift 2 ;;
    -o) case "$2" in go-template=*) tmpl=1 ;; esac; shift 2 ;;
    *) shift ;;
  esac
done
[ -z "$tmpl" ] || { echo "SOME_KEY"; exit 0; }
case "$res" in
  configmap/memex-portal-config)      cat "$STUB_DATA/live-configmap.json" ;;
  deployment/memex-portal-deployment) cat "$STUB_DATA/live-deployment.json" ;;
  poddisruptionbudgets)               cat "$STUB_DATA/live-poddisruptionbudgets.json" ;;
  scaledobjects.keda.sh)
    case "$STUB_SO" in
      served)    echo '{"apiVersion":"v1","kind":"List","items":[]}' ;;
      unserved)  echo 'error: the server doesn'"'"'t have a resource type "scaledobjects"' >&2; exit 1 ;;
      forbidden) echo 'Error from server (Forbidden): scaledobjects.keda.sh is forbidden: User "system:serviceaccount:x:y" cannot list resource "scaledobjects" in API group "keda.sh" in the namespace "check"' >&2; exit 1 ;;
      # Mixed diagnostics: a discovery WARNING for another API group (which carries a not-served
      # phrase) printed before the read's real answer.
      warn-forbidden)   echo "$STUB_WARN" >&2; echo 'Error from server (Forbidden): scaledobjects.keda.sh is forbidden: User "system:serviceaccount:x:y" cannot list resource "scaledobjects" in API group "keda.sh" in the namespace "check"' >&2; exit 1 ;;
      warn-unauthorized) echo "$STUB_WARN" >&2; echo 'error: You must be logged in to the server (Unauthorized)' >&2; exit 1 ;;
      warn-unserved)    echo "$STUB_WARN" >&2; echo 'error: the server doesn'"'"'t have a resource type "scaledobjects"' >&2; exit 1 ;;
    esac ;;
  *) echo "stub kubectl: unexpected read '$res'" >&2; exit 1 ;;
esac
SHEOF
# helm: `get manifest` from the fixture; everything else (the render) is the real helm.
cat > "$WORK/bin/helm" <<SHEOF
#!/usr/bin/env bash
if [ "\${1:-}" = get ] && [ "\${2:-}" = manifest ]; then cat "\$STUB_DATA/release-manifest.yaml"; exit 0; fi
exec "$REAL_HELM" "\$@"
SHEOF
chmod +x "$WORK/bin/kubectl" "$WORK/bin/helm"
# kubectl's discovery warning for an API group whose service is gone — it carries a not-served phrase
# about ANOTHER group, and it precedes the read's own answer on stderr.
DISCOVERY_WARNING='E1107 10:00:00.000000   1 memcache.go:287] couldn'"'"'t get resource list for external.metrics.k8s.io/v1beta1: the server could not find the requested resource'

run_check() {  # run_check <scripts-dir> <scaledobject answer> [extra args...] — sets OUT and RC
  local dir="$1" so="$2"; shift 2
  OUT="$(env PATH="$WORK/bin:$PATH" STUB_DATA="$DATA" STUB_SO="$so" STUB_WARN="$DISCOVERY_WARNING" GITHUB_STEP_SUMMARY="" \
    bash "$dir/check-chart-drift.sh" -n check -r rel --via kubectl -c "$CHART" \
      -f "$CHART/values.yaml" -f "$VALUES" --record "$DATA/record.json" "$@" 2>&1)"; RC=$?
}
# The comparator prints exactly one of these, and only once it has compared: the measured-at-all signal.
compared() { printf '%s\n' "$OUT" | grep -q -E '^(No drift: [0-9]+ fields compared|[0-9]+ divergence\(s\) across [0-9]+ compared fields)'; }
has() { case "$OUT" in *"$1"*) return 0 ;; esac; return 1; }
pass() { echo "  ok   $1"; }
bad()  { echo "::error::$1"; printf '%s\n' "$OUT" | sed 's/^/      | /' | tail -25; fail=1; }

echo "── check-chart-drift.sh: a kind this cluster does not serve (#155) ──"

# 1. not served, nothing rendered → skipped by name, and the comparison is made.
run_check "$SELF_DIR" unserved
if compared && has "kind scaledobjects.keda.sh is not served by this cluster" \
   && has "kinds skipped (not served by this cluster, rendered by nothing): scaledobjects.keda.sh" \
   && ! has "could not read scaledobjects.keda.sh"; then
  pass "an unserved kind the render does not contain is skipped by name, and the comparison is made"
else
  bad "case 1: an unserved, unrendered kind must be skipped by name and the comparison made (exit $RC)"
fi

# 2. not served, rendered → red, naming it, and nothing compared.
run_check "$SELF_DIR" unserved --set keda.enabled=true
if [ "$RC" -ne 0 ] && ! compared \
   && has "this cluster does not serve scaledobjects.keda.sh, but the rendered chart contains"; then
  pass "an unserved kind the render DOES contain is a failure, never a skip"
else
  bad "case 2: an unserved kind the render contains must fail, naming it, without a comparison (exit $RC)"
fi

# 3. Forbidden → red, nothing compared.
run_check "$SELF_DIR" forbidden
if [ "$RC" -ne 0 ] && ! compared && has "could not read scaledobjects.keda.sh" && ! has "skipped"; then
  pass "a Forbidden read stays fatal — never read as 'not served'"
else
  bad "case 3: a Forbidden read must stay fatal (exit $RC)"
fi

# 4. served → compared, nothing skipped.
run_check "$SELF_DIR" served
if compared && ! has "skipped"; then
  pass "a served kind is read and compared, nothing skipped (the control for case 1)"
else
  bad "case 4: a served kind must be compared with nothing skipped (exit $RC)"
fi

# 6. Mixed diagnostics (review of #178): a discovery warning carrying a not-served phrase, then an
#    authorization failure. The authorization failure wins: fatal, nothing skipped, nothing compared.
for so in warn-forbidden warn-unauthorized; do
  run_check "$SELF_DIR" "$so"
  if [ "$RC" -ne 0 ] && ! compared && has "could not read scaledobjects.keda.sh" && ! has "skipped"; then
    pass "a discovery warning does not turn an authorization failure into 'not served' ($so)"
  else
    bad "case 6 ($so): warning + authorization failure must stay fatal (exit $RC)"
  fi
done
# 7. …while a warning beside a genuine "not served" answer still skips (the control for case 6).
run_check "$SELF_DIR" warn-unserved
if compared && has "kind scaledobjects.keda.sh is not served by this cluster"; then
  pass "a discovery warning beside a genuine 'not served' answer still skips (the control for case 6)"
else
  bad "case 7: warning + not served must still skip and compare (exit $RC)"
fi

# 8. The rule itself, on the exact texts — one line per verdict.
# shellcheck source=kind-not-served.sh
source "$SELF_DIR/kind-not-served.sh"
rule_case() {  # rule_case <expected 0|1> <description> <text>
  local got=0
  kind_not_served "$3" || got=1
  if [ "$got" = "$1" ]; then pass "rule: $2"; else OUT="$3"; bad "rule: $2 — expected $1, got $got"; fi
}
SO_FORBIDDEN='Error from server (Forbidden): scaledobjects.keda.sh is forbidden: User "u" cannot list resource "scaledobjects"'
rule_case 0 "a lone not-served answer is not served" 'error: the server doesn'"'"'t have a resource type "scaledobjects"'
rule_case 1 "warning + Forbidden is NOT not-served" "$DISCOVERY_WARNING"$'\n'"$SO_FORBIDDEN"
rule_case 1 "warning + Unauthorized is NOT not-served" "$DISCOVERY_WARNING"$'\n''error: You must be logged in to the server (Unauthorized)'
rule_case 1 "a line naming Forbidden fails even if it also carries a not-served phrase" "$SO_FORBIDDEN: the server could not find the requested resource"
rule_case 1 "an unknown error beside a not-served line fails closed" "$DISCOVERY_WARNING"$'\n''Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout'
rule_case 1 "empty output is not an answer" ""

# A copy of this directory with the rule replaced (or removed) — never a committed second copy.
scripts_copy() {  # scripts_copy <name> <rule body | "absent">
  local d="$WORK/$1"
  mkdir -p "$d"
  cp "$SELF_DIR"/check-chart-drift.sh "$SELF_DIR"/chart-drift-*.py "$d/"
  if [ "$2" != absent ]; then printf 'kind_not_served() { %s; }\n' "$2" > "$d/kind-not-served.sh"; fi
  echo "$d"
}

# 5. the rule absent → the preflight refuses, naming it.
run_check "$(scripts_copy no-rule absent)" unserved
if [ "$RC" -ne 0 ] && ! compared && has "kind-not-served.sh" && has "cannot run"; then
  pass "a drift directory without the shared rule is refused in the preflight"
else
  bad "case 5: a missing kind-not-served.sh must be refused by name (exit $RC)"
fi

echo "── negative controls: a wrong rule must turn the matching case red ──"
# N1. The pre-#155 behaviour: no failure is ever "not served".
run_check "$(scripts_copy never-unserved 'return 1')" unserved
if [ "$RC" -ne 0 ] && ! compared && has "could not read scaledobjects.keda.sh"; then
  pass "N1: with the old rule case 1's cluster stops at the read — case 1 tells the fix from the defect"
else
  bad "N1: the pre-#155 rule should have stopped at the ScaledObject read (exit $RC)"
fi
# N2. Everything is "not served": a Forbidden read would be skipped.
run_check "$(scripts_copy always-unserved 'return 0')" forbidden
if compared && has "kind scaledobjects.keda.sh is not served"; then
  pass "N2: with an over-wide rule a Forbidden read is skipped — case 3 tells the fix from it"
else
  bad "N2: an over-wide rule should have skipped the Forbidden read (exit $RC)"
fi

# N3. The first version of the shared rule — a whole-text match — measured against the mixed cases:
#     each authorization failure must then read as skipped, which is the defect case 6 exists to catch.
OLD_RULE='case "${1:-}" in *"doesn'"'"'t have a resource type"*|*"the server could not find the requested resource"*|*"no matches for kind"*) return 0 ;; esac; return 1'
for so in warn-forbidden warn-unauthorized; do
  run_check "$(scripts_copy "whole-text-$so" "$OLD_RULE")" "$so"
  if compared && has "kind scaledobjects.keda.sh is not served"; then
    pass "N3: with the whole-text rule, $so is skipped and compared — case 6 tells the fix from it"
  else
    bad "N3 ($so): the whole-text rule should have skipped the authorization failure (exit $RC)"
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "::error::check-chart-drift.sh does not handle a kind this cluster does not serve as specified (#155)"
  exit 1
fi
echo "all unserved-kind cases pass"
