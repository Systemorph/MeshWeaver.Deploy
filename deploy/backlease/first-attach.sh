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
#     [--accept-jobs <patterns> | --no-accept-jobs] [--accept-tokens <patterns> | --no-accept-tokens]
#     [--token-models <m>] [--residency <X>]
#
# THE OWNER'S OPT-IN (MeshWeaver.Plugins Hosting/BackLease §13). Before anything is changed it ASKS — in a terminal —
# whether this Mac accepts fleet jobs and from which instances (offered: control.systemorph.com), and whether it serves
# token requests and from which instances (offered: *.meshweaver.cloud). An enrolled Mac's SAVED answers are the defaults
# and a class left unanswered keeps its saved answer; a fresh Mac with no answer stays OFF. A headless run takes the flags.
# Both answers are validated by the CLI's rules (`backlease accept … --dry-run`) before anything is changed. The answers are written with `memex-local backlease accept` into the Mac's back-lease layer (the
# heartbeat carries them; control enforces them and can never switch them on). Attaching the runners is not an opt-in.
#
# What it does, in order, and nothing else:
#   0. pins EVERY kubectl and helm call to the build profile's context (`colima-<profile>`, never the mutable current one) —
#      and never runs `memex-local up`; CHECKS the profile runs containerd (the docker runtime cannot run a user-namespaced
#      pod, and without one bwrap cannot mount a fresh /proc — measured on the first Mac, 2026-11-07);
#      CHECKS before changing anything: KEDA installed and ready, the node's allocatable, less what its pods already request, covers the host's floor and peak (replicas × requests),
#      both vault objects readable;
#   1. loads the `mw-build-host` AppArmor profile (`userns` only) into the Colima VM, persisted under /etc/apparmor.d,
#      and proves an UNPRIVILEGED user namespace starts under it; raises the VM's inotify limits (8192 instances per user,
#      persisted under /etc/sysctl.d) and checks the kernel took them;
#   2. creates namespace `build-mac`;
#   3. creates two Secrets there from the values read in step 0, on stdin only — never a process argument, never printed:
#      `build-worker-mac` (key `token`) and `build-pull` (kubernetes.io/dockerconfigjson: kubelet image pulls ONLY) and `build-registry-key-mac` (the READ-ONLY
#      instance key the runner reads the plugin registry's bundle index with — a build host holds no registry credential);
#   4. `helm upgrade --install build-host-mac` of THIS checkout's chart with build-host-mac.values.yaml and the set given;
#   5. fleet jobs at the LOWEST priority on this Mac: loads the launchd governor (deploy/homebrew/share/buildhost-priority.sh
#      --install) that puts the build VM's process in the background band while the owner, the portal VM or Ollama want
#      the cores, and gives it every idle core otherwise (nice/renice do nothing on macOS — see the script's header).
# The set (version + tester/portal/runner digests) is the one the operations record names for `latest`; it is passed,
# never committed. The runner digest must be a multi-arch index carrying linux/arm64.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
chart="$here/../helm"
profile=build api= registry= vault= worker= pull= pulluser= version= tester= portal= runner= pluginsreg= instkey=
acc_jobs='' acc_tokens='' acc_models='' acc_res=''   # empty = not answered by a flag; "off" = declined
while [ $# -gt 0 ]; do
  # `--flag=value` is the same as `--flag value` (the form memex-local documents for the opt-in flags)
  case "$1" in --*=*) set -- "${1%%=*}" "${1#*=}" "${@:2}";; esac
  case "$1" in
    --no-accept-jobs) acc_jobs=off; shift; continue;;
    --no-accept-tokens) acc_tokens=off; shift; continue;;
    --accept-jobs) acc_jobs="${2:?--accept-jobs needs instance patterns}";; --accept-tokens) acc_tokens="${2:?--accept-tokens needs instance patterns}";;
    --token-models) acc_models="${2:?--token-models needs model names}";; --residency) acc_res="${2:?--residency needs a residency (e.g. CH, Eu)}";;
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

# ── the owner's opt-in: asked FIRST, VALIDATED before anything changes, applied LAST (Hosting/BackLease §13.3) ──
# An enrolled Mac's SAVED answers are the defaults, and a class nobody answers keeps its saved answer: re-running this
# script to move the runners onto a newer set never resets the owner's consent. A fresh Mac with no answer stays OFF.
ml="$(command -v memex-local || printf '%s' "$here/../homebrew/bin/memex-local")"
layer="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}/backlease.yaml"
saved() { awk -v k="$1" '$1 == k":" { gsub(/"/, "", $2); print $2; exit }' "$layer" 2>/dev/null || true; }
saved_list() { awk -v p="$1__" 'index($1, p) == 1 { v = $0; sub(/^[^:]*:[ ]*/, "", v); gsub(/"/, "", v); printf "%s%s", (c++ ? " " : ""), v }' "$layer" 2>/dev/null || true; }
P=Hosting__BackLease__Accept
ask() { local a; printf '%s [%s] ' "$1" "$2" >&2; IFS= read -r a || a=""; printf '%s' "${a:-$2}"; }
yn_default() { [ "$1" = true ] && printf 'Y/n' || printf 'y/N'; }
if [ -t 0 ] && [ "${MEMEX_HEADLESS:-0}" != 1 ]; then
  if [ -z "$acc_jobs" ]; then
    case "$(ask "Accept fleet jobs (builds and other queue work) on this Mac?" "$(yn_default "$(saved ${P}__Jobs__Enabled)")")" in
      [Yy]|[Yy][Ee][Ss]|Y/n) f="$(saved_list ${P}__Jobs__From)"; acc_jobs="$(ask "  From which instances?" "${f:-control.systemorph.com}")" ;;
      *) acc_jobs=off ;;
    esac
  fi
  if [ -z "$acc_tokens" ]; then
    case "$(ask "Accept token requests (serve local models to other instances)?" "$(yn_default "$(saved ${P}__Tokens__Enabled)")")" in
      [Yy]|[Yy][Ee][Ss]|Y/n) f="$(saved_list ${P}__Tokens__From)"; acc_tokens="$(ask "  From which instances?" "${f:-*.meshweaver.cloud}")"
                         f="$(saved_list ${P}__Tokens__Models)"; acc_models="$(ask "  Which models?" "${acc_models:-${f:-qwen3.8:27b-mlx qwen3.6}}")"
                         acc_res="$(ask "  Where is this machine (residency, e.g. CH or Eu)?" "${acc_res:-$(saved ${P}__Tokens__Residency)}")" ;;
      *) acc_tokens=off ;;
    esac
  fi
fi
# what each answer becomes: '' = unanswered (keep the saved answer; a fresh Mac stays OFF)
jobs_args=(); tokens_args=()
# `mac` is the FIRST-attach default for the channel narrowing only: a saved channel list is the owner's and is kept
# (like the saved repositories, which this script never passes).
case "$acc_jobs" in '') ;; off) jobs_args=(--off) ;; *) jobs_args=(--from "$acc_jobs"); [ -n "$(saved_list ${P}__Jobs__Channels)" ] || jobs_args+=(--channels mac) ;; esac
case "$acc_tokens" in '') ;; off) tokens_args=(--off) ;; *) tokens_args=(--from "$acc_tokens" ${acc_models:+--models "$acc_models"} ${acc_res:+--residency "$acc_res"}) ;; esac
# BOTH answers are checked by the CLI's own rules NOW — before any infrastructure or preference changes
if [ -x "$ml" ]; then
  if [ "${#jobs_args[@]}" -gt 0 ]; then MEMEX_ACCEPT_NONINTERACTIVE=1 "$ml" backlease accept jobs "${jobs_args[@]}" --dry-run >/dev/null || { echo "ERROR: the jobs opt-in answer is invalid (above). Nothing was changed." >&2; exit 2; }; fi
  if [ "${#tokens_args[@]}" -gt 0 ]; then MEMEX_ACCEPT_NONINTERACTIVE=1 "$ml" backlease accept tokens "${tokens_args[@]}" --dry-run >/dev/null || { echo "ERROR: the tokens opt-in answer is invalid (above). Nothing was changed." >&2; exit 2; }; fi
fi
if [ ! -x "$ml" ] || [ ! -f "$layer" ]; then
  echo "WARN: this Mac has no back-lease layer (memex-local backlease enroll) — the opt-in answers are NOT recorded, and control" >&2
  echo "      treats an unenrolled machine as accepting nothing. Enrol it; enrolment asks both questions." >&2
fi

# The build host runs in its OWN Colima profile (default `build`), never in the profile memex-local's portal uses: that one
# runs the docker runtime, which cannot start a pod with hostUsers: false, and bwrap needs that pod's Unmasked /proc.
# Create it once:  colima start -p build --cpu 16 --memory 100 --disk 300 --runtime containerd --kubernetes \
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
# containerd 2 or newer: the chart's sandbox.userNamespace needs it (deploy/helm/values.yaml), and a 1.x runtime would
# take the AppArmor profile and the Secrets and then fail to start the pod.
[[ "$runtime" =~ ^containerd://v?([0-9]+)\. ]] && [ "${BASH_REMATCH[1]}" -ge 2 ] \
  || { echo "ERROR: profile $profile runs '$runtime' — the build host needs containerd 2 or newer (a user-namespaced pod). Nothing was changed." >&2; exit 1; }

# ── preconditions: everything is CHECKED before anything is changed ──────────────────────────────────────────
echo "== 0/5 preconditions"
k get crd scaledobjects.keda.sh -o name >/dev/null 2>&1 \
  || { echo "ERROR: KEDA is not installed in this cluster (no scaledobjects.keda.sh) — the host's scaler needs it; install KEDA first. Nothing was changed." >&2; exit 1; }
k -n keda wait --for=condition=Available deployment --all --timeout=20s >/dev/null 2>&1 \
  || { echo "ERROR: KEDA's deployments in namespace keda are not Available. Nothing was changed." >&2; exit 1; }
alloc="$(k get nodes -o jsonpath='{.items[0].status.allocatable.cpu} {.items[0].status.allocatable.memory}')"
# The declared FLOOR (every enabled channel's minReplicas) and the PEAK (their maxReplicas), each times one replica's
# requests, must fit what the one node has left after the pods already on it (k3s, KEDA) — checking a single replica
# passed a VM that left the second standing replica Pending for good.
k get pods -A -o json | python3 "$here/host-fits-node.py" "$alloc" "$here/build-host-mac.values.yaml" --pods-json - \
  || { echo "Nothing was changed." >&2; exit 1; }
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
# Without the index, the plugins layer is whatever /hosts/inputs names for `mac`. A Mac has no workload identity and its
# pull token reaches the kubelet only, so a SEALED publication on an authenticated registry cannot be pulled (the host would
# refuse every claim). Read the answer now, with the Mac's own token on stdin, and refuse before changing anything.
if [ -z "$pluginsreg" ]; then
  inputs="$(printf 'header = "Authorization: Bearer %s"\n' "$worker_value" \
    | curl -fsS -m 30 -K - "${api%/}/hosts/inputs?channel=mac")" \
    || { echo "ERROR: could not read ${api%/}/hosts/inputs?channel=mac. Nothing was changed." >&2; exit 1; }
  python3 -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if d.get("plugins") is None else 3)' "$inputs" \
    || { echo "ERROR: /hosts/inputs names a sealed plugins publication for channel mac, which this Mac cannot pull (no workload identity). Pass --plugins-registry and --instance-key-secret for the registry index, or attach once the publication is readable. Nothing was changed." >&2; exit 1; }
  echo "   /hosts/inputs names no sealed publication for mac — the host claims with the cluster hosts' plugins contract"
fi

echo "== 1/5 AppArmor profile mw-build-host in the Colima VM"
printf '%s\n' 'abi <abi/4.0>,' 'include <tunables/global>' '' 'profile mw-build-host flags=(unconfined) {' '  userns,' '}' \
  | vm sudo sh -c 'cat > /etc/apparmor.d/mw-build-host && apparmor_parser --replace /etc/apparmor.d/mw-build-host'
# The probe runs as the VM's ordinary, UNPRIVILEGED user — as the build container does (uid 1654, no capabilities).
vm aa-exec -p mw-build-host -- unshare -U -r true \
  || { echo "ERROR: an unprivileged user namespace does not start under mw-build-host — bwrap would fail" >&2; exit 1; }
vm unshare -U -r true 2>/dev/null \
  && echo "   note: unprivileged user namespaces start even WITHOUT the profile in this VM" \
  || echo "   loaded: an unprivileged user namespace starts under mw-build-host, and not without it"

# inotify: every hosted test mesh and every MSBuild node watches files, and the VM's default of 128 inotify instances per user
# failed 71 portal-host tests on the first Mac (2026-11-08: "The configured user limit (128) on the number of inotify
# instances has been reached"). The build VM's kernel limits are host configuration, set here and persisted.
printf '%s\n' 'fs.inotify.max_user_instances = 8192' 'fs.inotify.max_user_watches = 1048576' \
  | vm sudo sh -c 'cat > /etc/sysctl.d/90-mw-build-host.conf && sysctl -q -p /etc/sysctl.d/90-mw-build-host.conf'
got="$(vm sysctl -n fs.inotify.max_user_instances)"
[ "${got:-0}" -ge 8192 ] || { echo "ERROR: fs.inotify.max_user_instances is ${got:-unreadable} in the build VM after setting 8192" >&2; exit 1; }
echo "   inotify: ${got} instances per user (persisted in /etc/sysctl.d/90-mw-build-host.conf)"

echo "== 2/5 namespace build-mac"
k create namespace build-mac --dry-run=client -o yaml | k apply -f - >/dev/null

echo "== 3/5 secrets (values on stdin only — never in a process argument, never printed)"
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

echo "== 4/5 helm upgrade --install build-host-mac ($version)"
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

echo "== 5/5 fleet jobs at the lowest priority (launchd governor for profile $profile)"
# The runners are deployed by now: a governor that cannot load is reported, never a failed attach.
bash "$here/../homebrew/share/buildhost-priority.sh" --install --profile "$profile" \
  || echo "WARN: the priority governor did not load — the build VM competes at the default band; re-run: bash deploy/homebrew/share/buildhost-priority.sh --install --profile $profile" >&2

echo "== the owner's opt-in (jobs: ${acc_jobs:-unchanged} · tokens: ${acc_tokens:-unchanged})"
# The answers live in the back-lease layer the portal's heartbeat reads (the runners alone carry no heartbeat). Both were
# validated before step 0, so neither write below can be refused on its answer.
if [ -x "$ml" ] && [ -f "$layer" ]; then
  [ "${#jobs_args[@]}" -eq 0 ] || MEMEX_ACCEPT_NONINTERACTIVE=1 "$ml" backlease accept jobs "${jobs_args[@]}"
  [ "${#tokens_args[@]}" -eq 0 ] || MEMEX_ACCEPT_NONINTERACTIVE=1 "$ml" backlease accept tokens "${tokens_args[@]}"
  [ "${#jobs_args[@]}${#tokens_args[@]}" = 00 ] && echo "   no answer given — the saved opt-in stands" \
    || echo "   written to the back-lease layer — 'memex-local update' applies it (the next heartbeat carries it to control)"
fi
