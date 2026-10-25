#!/usr/bin/env bash
# deploy-test-switch-select.sh — the ONE selection both `select` and `apply` run (the second time
# UNDER the apply concurrency group, with --exclude-run set to its own run).
#
# Reads the newest armed set and the runner images from the platform registry (needs an `az` login),
# the attempt markers for that set and EVERY run they name (REST, never GraphQL — a marker counts
# only when its run is this lane's own automatic run, and the trigger decides that from the run),
# then prints `deploy-test-switch-trigger.py select`'s key=value lines on stdout.
#
#   GH_TOKEN, GITHUB_REPOSITORY, RUNNER_TEMP   from the job
#   API             vars.MW_BUILD_API
#   UP_EVENT        github.event.workflow_run.event
#   UP_CONCLUSION   github.event.workflow_run.conclusion
#   DEFAULT_BRANCH  github.event.repository.default_branch
#   EXCLUDE_RUN     optional: the asking run's id (apply's re-check)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
t="$here/deploy-test-switch-trigger.py"
d=$(mktemp -d "${RUNNER_TEMP:-/tmp}/dts-select.XXXXXX")
for repo in memex-portal-ai mw-build-host; do
  az acr manifest list-metadata --registry meshweaver --name "$repo" --top 300 --orderby time_desc \
    -o json --only-show-errors > "$d/$repo.json"
done
python3 "$t" armed --portal "$d/memex-portal-ai.json" > "$d/armed"
v=$(sed -n 's/^version=//p' "$d/armed")
# The record of automatic attempts: one exact-name artifact per set.
gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=deploy-test-switch-attempt-$v&per_page=100" > "$d/attempts.json"
# Every run a marker names — who wrote it decides whether it counts. A run that cannot be read
# fails this script (set -e): an unreadable marker must never read as "never attempted".
: > "$d/runs.ndjson"
jq -r '.artifacts[]?.workflow_run.id // empty' "$d/attempts.json" | sort -u > "$d/ids"
while read -r id; do
  [ "$id" = "${EXCLUDE_RUN:-}" ] && continue
  gh api "repos/$GITHUB_REPOSITORY/actions/runs/$id" >> "$d/runs.ndjson"
done < "$d/ids"
jq -s . "$d/runs.ndjson" > "$d/runs.json"
python3 "$t" select --upstream-event "$UP_EVENT" --upstream-conclusion "$UP_CONCLUSION" \
  --portal "$d/memex-portal-ai.json" --runner "$d/mw-build-host.json" \
  --attempts "$d/attempts.json" --attempt-runs "$d/runs.json" --default-branch "$DEFAULT_BRANCH" \
  --exclude-run "${EXCLUDE_RUN:-}" --api "$API" --window 120 --poll 10
