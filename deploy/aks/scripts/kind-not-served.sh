# shellcheck shell=bash
# The ONE decision "this cluster does not serve that kind". Sourced, never executed.
#
# Two steps of one Audit read the live namespace kind by kind: hosting-audit (live vs the last-deployed
# manifest) and check-chart-drift.sh (live vs the rendered chart, run by hosting-chart-drift). They
# must agree on what a failed read MEANS, so the rule lives here and both source it — never a second
# copy (MeshWeaver.Deploy#155: the drift check treated the answer as fatal while hosting-audit skipped
# it, and the Audit stopped on every cluster without KEDA).
#
# THE RULE. A list read that fails because the API server has no such resource type means nothing of
# that kind can exist in the namespace: the read is ANSWERED (an empty list), and the caller records
# the kind as skipped by name. Every other failure — Forbidden above all — stays fatal: a kind the
# check could not read is a kind it must never report as clean.
#
# What the caller still decides for itself: whether the kind not being served is acceptable at all.
# hosting-audit's manifest step already refuses a chart that renders a kind the cluster does not serve
# (`kubectl apply --dry-run=client` needs discovery for every rendered kind); check-chart-drift.sh
# refuses the same case by looking at its render. Skipping is only ever right when the chart renders
# nothing of the kind.
#
#   kind_not_served "<everything the failed read printed>"   → 0 = not served, 1 = any other failure
#
# The three phrasings are kubectl's, across versions and paths: `error: the server doesn't have a
# resource type "scaledobjects"` (discovery has no such resource), `the server could not find the
# requested resource` (the API path is absent — a list GET, so not an object NotFound, which reads
# `Error from server (NotFound): <kind> "<name>" not found`), and `no matches for kind` (a kind/version
# pair discovery does not know).
kind_not_served() {
  case "${1:-}" in
    *"doesn't have a resource type"*|*"the server could not find the requested resource"*|*"no matches for kind"*)
      return 0 ;;
  esac
  return 1
}
