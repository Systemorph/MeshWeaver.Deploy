#!/usr/bin/env bash
#
# first-attach.sh — attach THIS Mac as a build host on the opt-in `mac` channel (first attach; Hosting/BackLease
# phase 2b). Idempotent: re-run it to move the host onto a newer set.
#
#   deploy/backlease/first-attach.sh \
#     --api https://<operations instance>/api/builds --registry <name>.azurecr.io --vault <key vault> \
#     --worker-secret <vault object of the Mac's worker token> --pull-secret <vault object of the pull token password> \
#     --pull-user <registry token name> \
#     --version <x.y.z> --tester sha256:… --portal sha256:… --runner sha256:…
#
# What it does, in order, and nothing else:
#   1. refuses unless kubectl's CURRENT context is `colima` (never another cluster) — and never runs `memex-local up`;
#   2. loads the `mw-build-host` AppArmor profile (`userns` only) into the Colima VM, persisted under /etc/apparmor.d,
#      and proves an unprivileged user namespace now starts under it;
#   3. creates namespace `build-mac` and two Secrets there from Key Vault, piped — no value is printed or written to disk:
#      `build-worker-mac` (key `token`) and `build-pull` (docker-registry, the registry token);
#   4. `helm upgrade --install build-host-mac` of THIS checkout's chart with build-host-mac.values.yaml and the set given.
# The set (version + tester/portal/runner digests) is the one the operations record names for `latest`; it is passed,
# never committed. The runner digest must be a multi-arch index carrying linux/arm64.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
chart="$here/../helm"
api= registry= vault= worker= pull= pulluser= version= tester= portal= runner=
while [ $# -gt 0 ]; do
  case "$1" in
    --api) api="$2";; --registry) registry="$2";; --vault) vault="$2";;
    --worker-secret) worker="$2";; --pull-secret) pull="$2";; --pull-user) pulluser="$2";;
    --version) version="$2";; --tester) tester="$2";; --portal) portal="$2";; --runner) runner="$2";;
    *) echo "unknown argument $1" >&2; exit 2;;
  esac; shift 2
done
for v in api registry vault worker pull pulluser version tester portal runner; do
  [ -n "${!v}" ] || { echo "ERROR: --${v//_/-} is required (see the header)" >&2; exit 2; }
done
[[ "$api" == https://* ]] || { echo "ERROR: --api must be https (the worker token rides on it)" >&2; exit 2; }
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "ERROR: --version must be x.y.z" >&2; exit 2; }
for d in "$tester" "$portal" "$runner"; do [[ "$d" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ERROR: $d is not sha256:<64 hex>" >&2; exit 2; }; done

ctx="$(kubectl config current-context)"
[ "$ctx" = colima ] || { echo "ERROR: kubectl's current context is '$ctx', not colima — refusing to touch any other cluster" >&2; exit 1; }

echo "== 1/4 AppArmor profile mw-build-host in the Colima VM"
printf '%s\n' 'abi <abi/4.0>,' 'include <tunables/global>' '' 'profile mw-build-host flags=(unconfined) {' '  userns,' '}' \
  | colima ssh -- sudo sh -c 'cat > /etc/apparmor.d/mw-build-host && apparmor_parser --replace /etc/apparmor.d/mw-build-host'
colima ssh -- sudo aa-exec -p mw-build-host -- unshare -U -r true \
  || { echo "ERROR: an unprivileged user namespace still does not start under mw-build-host — bwrap would fail" >&2; exit 1; }
echo "   loaded, and a user namespace starts under it"

echo "== 2/4 namespace build-mac"
kubectl create namespace build-mac --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "== 3/4 secrets from Key Vault (piped, never printed)"
az keyvault secret show --vault-name "$vault" --name "$worker" --query value -o tsv \
  | kubectl -n build-mac create secret generic build-worker-mac --from-file=token=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
pw="$(az keyvault secret show --vault-name "$vault" --name "$pull" --query value -o tsv)"
kubectl -n build-mac create secret docker-registry build-pull --docker-server="$registry" --docker-username="$pulluser" \
  --docker-password="$pw" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset pw
echo "   build-worker-mac and build-pull applied"

echo "== 4/4 helm upgrade --install build-host-mac ($version)"
helm upgrade --install build-host-mac "$chart" -n build-mac \
  -f "$chart/values.yaml" -f "$here/build-host-mac.values.yaml" \
  --set-string buildHosts.apiUrl="$api" \
  --set-string buildHosts.testerRepository="$registry/mw-plugin-test" \
  --set-string buildHosts.portalRepository="$registry/memex-portal-ai" \
  --set-string buildHosts.runnerRepository="$registry/mw-build-host" \
  --set-string buildHosts.pluginsRegistry="$registry" \
  --set-string buildHosts.mac.platformVersion="$version" \
  --set-string buildHosts.mac.testerDigest="$tester" \
  --set-string buildHosts.mac.portalDigest="$portal" \
  --set-string buildHosts.mac.runnerDigest="$runner" \
  --wait=false
kubectl -n build-mac get deploy,scaledobject,pvc
