#!/usr/bin/env bash
#
# first-attach.sh — attach THIS Mac as a build host on the opt-in `mac` channel (first attach; Hosting/BackLease
# phase 2b). Idempotent: re-run it to move the host onto a newer set.
#
#   deploy/backlease/first-attach.sh \
#     --api https://<operations instance>/api/builds --registry <name>.azurecr.io --vault <key vault> \
#     --worker-secret <vault object of the Mac's worker token> --pull-secret <vault object of the pull token password> \
#     --pull-user <registry token name> \
#     [--profile build] [--plugins-registry <https URL> --instance-key-secret <vault object of a READ-ONLY mwi_ key>] \
#     --version <x.y.z> --tester sha256:… --portal sha256:… --runner sha256:…
#
# What it does, in order, and nothing else:
#   0. pins EVERY kubectl and helm call to the build profile's context (`colima-<profile>`, never the mutable current one) —
#      and never runs `memex-local up`; CHECKS the profile runs containerd (the docker runtime cannot run a user-namespaced
#      pod, and without one bwrap cannot mount a fresh /proc — measured on the first Mac, 2026-11-07);
#      CHECKS before changing anything: KEDA installed and ready, the node's allocatable covers the host's requests,
#      both vault objects readable;
#   1. loads the `mw-build-host` AppArmor profile (`userns` only) into the Colima VM, persisted under /etc/apparmor.d,
#      and proves an UNPRIVILEGED user namespace starts under it;
#   2. creates namespace `build-mac`;
#   3. creates two Secrets there from the values read in step 0, on stdin only — never a process argument, never printed:
#      `build-worker-mac` (key `token`) and `build-pull` (kubernetes.io/dockerconfigjson: kubelet image pulls ONLY) and `build-registry-key-mac` (the READ-ONLY
#      instance key the runner reads the plugin registry's bundle index with — a build host holds no registry credential);
#   4. `helm upgrade --install build-host-mac` of THIS checkout's chart with build-host-mac.values.yaml and the set given.
# The set (version + tester/portal/runner digests) is the one the operations record names for `latest`; it is passed,
# never committed. The runner digest must be a multi-arch index carrying linux/arm64.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
chart="$here/../helm"
profile=build api= registry= vault= worker= pull= pulluser= version= tester= portal= runner= pluginsreg= instkey=
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) profile="$2";; --api) api="$2";; --registry) registry="$2";; --vault) vault="$2";;
    --worker-secret) worker="$2";; --pull-secret) pull="$2";; --pull-user) pulluser="$2";;
    --plugins-registry) pluginsreg="$2";; --instance-key-secret) instkey="$2";;
    --version) version="$2";; --tester) tester="$2";; --portal) portal="$2";; --runner) runner="$2";;
    *) echo "unknown argument $1" >&2; exit 2;;
  esac; shift 2
done
for v in api registry vault worker pull pulluser version tester portal runner; do
  [ -n "${!v}" ] || { echo "ERROR: --${v//_/-} is required (see the header)" >&2; exit 2; }
done
[[ "$api" == https://* ]] || { echo "ERROR: --api must be https (the worker token rides on it)" >&2; exit 2; }
# The registry bundle index (H8) is OPT-IN: pass BOTH --plugins-registry and --instance-key-secret, or neither. Without them
# the host takes its plugins layer exactly as the cluster hosts do — the sealed publication /hosts/inputs names, or none.
# Measured 2026-11-07: the registry serves a mixed package (AI) as an assemblies-only bundle, which module-fetch refuses,
# so an H8 host never claimed.
if [ -n "$pluginsreg$instkey" ]; then
  { [ -n "$pluginsreg" ] && [ -n "$instkey" ]; } || { echo "ERROR: --plugins-registry and --instance-key-secret go together" >&2; exit 2; }
  [[ "$pluginsreg" == https://* ]] || { echo "ERROR: --plugins-registry must be https (the instance key rides on it)" >&2; exit 2; }
fi
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "ERROR: --version must be x.y.z" >&2; exit 2; }
for d in "$tester" "$portal" "$runner"; do [[ "$d" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ERROR: $d is not sha256:<64 hex>" >&2; exit 2; }; done

# The build host runs in its OWN Colima profile (default `build`), never in the profile memex-local's portal uses: that one
# runs the docker runtime, which cannot start a pod with hostUsers: false, and bwrap needs that pod's Unmasked /proc.
# Create it once:  colima start -p build --cpu 8 --memory 24 --disk 100 --runtime containerd --kubernetes \
#                    --k3s-arg=--disable=traefik --k3s-arg=--kubelet-arg=root-dir=/var/lib/rancher/kubelet
# (the kubelet root on the data disk: the tester and portal images are copied into emptyDirs, which the 20 GiB root
# disk cannot hold). Then install KEDA into it. EVERY Kubernetes call below names that context explicitly — never the
# mutable current context, so another
# terminal switching contexts mid-run cannot redirect a Secret or the release into another cluster.
[[ "$profile" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "ERROR: --profile must be a Colima profile name" >&2; exit 2; }
if [ "$profile" = default ]; then ctx=colima; else ctx="colima-$profile"; fi
kubectl config get-contexts "$ctx" >/dev/null 2>&1 || { echo "ERROR: no kubectl context named $ctx (colima start -p $profile … --kubernetes, see above)" >&2; exit 1; }
k() { kubectl --context "$ctx" "$@"; }
vm() { colima ssh -p "$profile" -- "$@"; }
runtime="$(k get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}')"
[[ "$runtime" == containerd://* ]] || { echo "ERROR: profile $profile runs '$runtime' — the build host needs containerd (a user-namespaced pod). Nothing was changed." >&2; exit 1; }

# ── preconditions: everything is CHECKED before anything is changed ──────────────────────────────────────────
echo "== 0/4 preconditions"
k get crd scaledobjects.keda.sh -o name >/dev/null 2>&1 \
  || { echo "ERROR: KEDA is not installed in this cluster (no scaledobjects.keda.sh) — the host's scaler needs it; install KEDA first. Nothing was changed." >&2; exit 1; }
k -n keda wait --for=condition=Available deployment --all --timeout=20s >/dev/null 2>&1 \
  || { echo "ERROR: KEDA's deployments in namespace keda are not Available. Nothing was changed." >&2; exit 1; }
alloc="$(k get nodes -o jsonpath='{.items[0].status.allocatable.cpu} {.items[0].status.allocatable.memory}')"
python3 - "$alloc" "$here/build-host-mac.values.yaml" <<'PY2' || { echo "Nothing was changed." >&2; exit 1; }
import re, sys, yaml
cpu, mem = sys.argv[1].split()
def cores(v): return float(v[:-1]) / 1000 if v.endswith("m") else float(v)
def gib(v):
    units = {"Ki": 1 / 1048576, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024}
    m = re.fullmatch(r"([0-9.]+)([KMGT]i)?", v)
    return float(m.group(1)) * (units[m.group(2)] if m.group(2) else 1 / 1073741824)
req = yaml.safe_load(open(sys.argv[2]))["buildHosts"]["resources"]["requests"]
if cores(str(req["cpu"])) > cores(cpu) or gib(str(req["memory"])) > gib(mem):
    print(f"ERROR: the host requests {req['cpu']} CPU / {req['memory']}, the node allocates {cpu} / {mem} — it would stay Pending; "
          "give the Colima VM more (colima stop; colima start --cpu N --memory M) or lower the values file", file=sys.stderr)
    sys.exit(1)
print(f"   node allocates {cpu} CPU / {mem}; the host requests {req['cpu']} / {req['memory']}")
PY2
# The vault reads happen BEFORE any apply, so a failed read never replaces a working Secret with an empty one.
worker_value="$(az keyvault secret show --vault-name "$vault" --name "$worker" --query value -o tsv)" \
  && [ -n "$worker_value" ] || { echo "ERROR: could not read the worker token $worker from $vault. Nothing was changed." >&2; exit 1; }
pull_value="$(az keyvault secret show --vault-name "$vault" --name "$pull" --query value -o tsv)" \
  && [ -n "$pull_value" ] || { echo "ERROR: could not read the pull token $pull from $vault. Nothing was changed." >&2; exit 1; }
key_value=
if [ -n "$instkey" ]; then
  key_value="$(az keyvault secret show --vault-name "$vault" --name "$instkey" --query value -o tsv)" \
    && [[ "$key_value" == mwi_* ]] || { echo "ERROR: could not read a mwi_ instance key from $instkey in $vault. Nothing was changed." >&2; exit 1; }
fi
echo "   KEDA ready; the vault objects read"

echo "== 1/4 AppArmor profile mw-build-host in the Colima VM"
printf '%s\n' 'abi <abi/4.0>,' 'include <tunables/global>' '' 'profile mw-build-host flags=(unconfined) {' '  userns,' '}' \
  | vm sudo sh -c 'cat > /etc/apparmor.d/mw-build-host && apparmor_parser --replace /etc/apparmor.d/mw-build-host'
# The probe runs as the VM's ordinary, UNPRIVILEGED user — as the build container does (uid 1654, no capabilities).
vm aa-exec -p mw-build-host -- unshare -U -r true \
  || { echo "ERROR: an unprivileged user namespace does not start under mw-build-host — bwrap would fail" >&2; exit 1; }
vm unshare -U -r true 2>/dev/null \
  && echo "   note: unprivileged user namespaces start even WITHOUT the profile in this VM" \
  || echo "   loaded: an unprivileged user namespace starts under mw-build-host, and not without it"

echo "== 2/4 namespace build-mac"
k create namespace build-mac --dry-run=client -o yaml | k apply -f - >/dev/null

echo "== 3/4 secrets (values on stdin only — never in a process argument, never printed)"
printf '%s' "$worker_value" \
  | k -n build-mac create secret generic build-worker-mac --from-file=token=/dev/stdin --dry-run=client -o yaml \
  | k apply -f - >/dev/null
printf '%s' "$pull_value" | python3 -c '
import base64, json, sys
registry, user = sys.argv[1], sys.argv[2]
password = sys.stdin.read()
auth = base64.b64encode(f"{user}:{password}".encode()).decode()
sys.stdout.write(json.dumps({"auths": {registry: {"username": user, "password": password, "auth": auth}}}))' "$registry" "$pulluser" \
  | k -n build-mac create secret generic build-pull --type=kubernetes.io/dockerconfigjson \
      --from-file=.dockerconfigjson=/dev/stdin --dry-run=client -o yaml \
  | k apply -f - >/dev/null
# The plugins layer: a host holds NO registry credential for it (StandingBuildHosts §2); it reads the registry's bundle
# index (H8) with the Mac's READ-ONLY instance key.
if [ -n "$key_value" ]; then
  printf '%s' "$key_value" \
    | k -n build-mac create secret generic build-registry-key-mac --from-file=key=/dev/stdin --dry-run=client -o yaml \
    | k apply -f - >/dev/null
fi
unset worker_value pull_value key_value
echo "   build-worker-mac and build-pull (image pulls only) applied${instkey:+, and build-registry-key-mac}"

echo "== 4/4 helm upgrade --install build-host-mac ($version)"
helm upgrade --install build-host-mac "$chart" --kube-context "$ctx" -n build-mac \
  -f "$chart/values.yaml" -f "$here/build-host-mac.values.yaml" \
  --set-string buildHosts.apiUrl="$api" \
  --set-string buildHosts.testerRepository="$registry/mw-plugin-test" \
  --set-string buildHosts.portalRepository="$registry/memex-portal-ai" \
  --set-string buildHosts.runnerRepository="$registry/mw-build-host" \
  --set-string buildHosts.pluginsRegistry="$registry" \
  ${pluginsreg:+--set-string buildHosts.registry.url="$pluginsreg"} \
  ${pluginsreg:+--set-string buildHosts.registry.instanceKeySecret.name=build-registry-key-mac} \
  ${pluginsreg:+--set-string buildHosts.registry.instanceKeySecret.key=key} \
  --set-string buildHosts.mac.platformVersion="$version" \
  --set-string buildHosts.mac.testerDigest="$tester" \
  --set-string buildHosts.mac.portalDigest="$portal" \
  --set-string buildHosts.mac.runnerDigest="$runner" \
  --wait=false
k -n build-mac get deploy,scaledobject,pvc
