#!/usr/bin/env bash
#
# The standing build hosts' chart half (templates/build-host/, Doc/Architecture/BuildHosts), checked
# the only ways that can fail without a cluster:
#
#   1. the runner ships in ONE place — the mw-build-host image, which copies .github/scripts'
#      build-run.py and compose-gate-host.sh (deploy/build-host/Dockerfile); the chart carries no
#      copy, and the runner's self-test passes;
#   2. the chart defaults render NO build-host object at all (buildHosts.enabled: false), so no
#      estate changes until its record turns the hosts on;
#   3. the fixture renders, per enabled channel, a Deployment with no `replicas` (KEDA owns it),
#      images by DIGEST, the ci-build toleration + nodeSelector, the worker token from a Secret, the
#      build-cache claim, readiness on the staged marker, and a ScaledObject with minReplicaCount 0,
#      the channel's maxReplicaCount and a metrics-api trigger on <apiUrl>/queue?channel=<c> with
#      valueLocation demand and a scale-down stabilization window, the worker token as a mounted
#      FILE (never env), grace = the run deadline, no credential volume but that one and no bake
#      share — plus a ServiceAccount with no token and a NetworkPolicy with no ingress whose egress
#      is pinned whole (DNS to the cluster resolver only; the build API's pods on their port; 80/443
#      outside every private range, IMDS and WireServer);
#   3d. a build-host-only release (portal.enabled: false) renders the version's host, its cache and
#      the release-named ServiceAccount + NetworkPolicy and NOTHING of the portal, and two such
#      releases and the portal's own never name one object twice;
#   4. the refusals hold: a tag instead of a digest, plain http to a public host, and a registry
#      credential for the host, fail
#      `helm template` naming why.
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

# 1. one implementation: the runner and the composer ship INSIDE the mw-build-host image, copied
#    from .github/scripts by deploy/build-host/Dockerfile — the chart carries no copy of either.
#    The runner and its Dockerfile live in THIS repository; the Dockerfile fetches the runner (this
#    repository) and the platform's helpers by commit inside the build, so the context is empty.
DOCKERFILE="$REPO/deploy/build-host/Dockerfile"
if [ -f "$CHART/files/build-run.py" ]; then
  report "deploy/helm/files/build-run.py exists — the chart must not carry a second copy of the runner; it ships in the mw-build-host image"
fi
if [ -f "$DOCKERFILE" ] \
   && grep -q '^COPY --from=sources /out/scripts/build-run.py /out/scripts/compose-gate-host.sh /out/scripts/sandbox-postgres.sh /out/scripts/module-build-key.py /out/scripts/ci-artifact-store.py /opt/build-host/$' "$DOCKERFILE" \
   && grep -q '^COPY --from=sources /out/plugin-build/ /opt/build-host/plugin-build/$' "$DOCKERFILE" \
   && ! grep -Eq '^COPY [^-]' "$DOCKERFILE" \
   && grep -q "grep -Eq '\^\[0-9a-f\]{40}\$'" "$DOCKERFILE" \
   && grep -q 'bubblewrap' "$DOCKERFILE" \
   && grep -q 'sandbox-postgres.sh --self-test' "$DOCKERFILE" \
   && grep -q 'build-run.py --self-test' "$DOCKERFILE" \
   && ! grep -Eq '^FROM [^ ]*:latest' "$DOCKERFILE"; then
  ok "deploy/build-host/Dockerfile fetches the runner, composer, sandbox-postgres, module-build-key and the plugin-build tool by 40-hex commit (nothing COPYed from the context), installs bubblewrap, runs both self-tests, and pins no :latest"
else
  report "deploy/build-host/Dockerfile must fetch its sources by 40-hex commit in the 'sources' stage, COPY only --from=sources (build-run.py, compose-gate-host.sh, sandbox-postgres.sh, module-build-key.py, ci-artifact-store.py, plugin-build) to /opt/build-host/, install bubblewrap, run both self-tests, and name no :latest base"
fi
if python3 "$REPO/.github/scripts/build-run.py" --self-test; then
  ok "build-run.py passes its self-test"
else
  report "build-run.py fails its self-test"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 2. defaults render nothing
if helm template r "$CHART" -f "$CHART/values.yaml" --namespace check > "$WORK/defaults.yaml" 2> "$WORK/defaults.err"; then
  if python3 - "$WORK/defaults.yaml" <<'PY'
import sys, yaml
bad = [f"{d['kind']}/{d['metadata']['name']}" for d in yaml.safe_load_all(open(sys.argv[1]))
       if d and (d.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/component") == "build-host"]
if bad:
    print("rendered with buildHosts.enabled false: " + ", ".join(bad)); sys.exit(1)
PY
  then ok "the chart defaults render no build-host object"
  else report "the chart defaults render build-host objects — buildHosts must be OFF until a record turns it on"
  fi
else
  report "the chart defaults do not render:"; sed 's/^/    /' "$WORK/defaults.err"
fi

# 3. the fixture
if helm template r "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-hosts.yaml" --namespace check \
     > "$WORK/hosts.yaml" 2> "$WORK/hosts.err"; then
  if python3 - "$WORK/hosts.yaml" <<'PY'
import re, sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
mine = [d for d in docs if (d.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/component") == "build-host"]
problems = []
def find(kind, name):
    return next((d for d in mine if d["kind"] == kind and d["metadata"]["name"] == name), None)
want = {"latest": ("1111", "2222", "5555", 2, "3.1.10412"), "stable": ("3333", "4444", "6666", 1, "3.1.10388")}
for ch, (tester, portal, runner, mx, pv) in want.items():
    dep = find("Deployment", f"build-host-{ch}")
    so = find("ScaledObject", f"build-host-{ch}-scaler")
    if not dep or not so:
        problems.append(f"{ch}: Deployment or ScaledObject missing"); continue
    spec = dep["spec"]
    if "replicas" in spec:
        problems.append(f"{ch}: Deployment renders spec.replicas beside a ScaledObject")
    pod = spec["template"]["spec"]
    inits = {c["name"]: c for c in pod.get("initContainers", [])}
    if not inits.get("tester", {}).get("image", "").endswith("@sha256:" + tester * 16):
        problems.append(f"{ch}: tester init image is not the channel's digest: {inits.get('tester', {}).get('image')}")
    if not inits.get("portal", {}).get("image", "").endswith("@sha256:" + portal * 16):
        problems.append(f"{ch}: portal init image is not the channel's digest: {inits.get('portal', {}).get('image')}")
    if pod.get("nodeSelector") != {"workload": "ci-build"}:
        problems.append(f"{ch}: nodeSelector is not workload=ci-build: {pod.get('nodeSelector')}")
    if not any(t.get("key") == "ci" and t.get("value") == "build" for t in pod.get("tolerations", [])):
        problems.append(f"{ch}: no ci=build toleration")
    if pod.get("serviceAccountName") != "build-host" or pod.get("automountServiceAccountToken") is not False:
        problems.append(f"{ch}: not the build-host ServiceAccount without a mounted token")
    build = next((c for c in pod["containers"] if c["name"] == "build"), {})
    if build.get("image") != "cr.example.com/mw-build-host@sha256:" + runner * 16:
        problems.append(f"{ch}: the runner image is not the channel's mw-build-host digest: {build.get('image')}")
    if {e["name"]: e for e in build.get("env", [])}.get("MW_BUILD_RUNNER_DIGEST", {}).get("value") != "sha256:" + runner * 16:
        problems.append(f"{ch}: MW_BUILD_RUNNER_DIGEST does not carry the channel's runner digest")
    sc = pod.get("securityContext", {})
    if not (sc.get("runAsNonRoot") is True and sc.get("runAsUser") == 1654 and sc.get("fsGroup") == 1654):
        problems.append(f"{ch}: the pod does not run as the runner image's unprivileged user 1654: {sc}")
    if spec["template"]["metadata"].get("labels", {}).get("azure.workload.identity/use") != "true":
        problems.append(f"{ch}: the pod does not opt into Azure workload identity (label azure.workload.identity/use)")
    skip = set((spec["template"]["metadata"].get("annotations") or {}).get("azure.workload.identity/skip-containers", "").split(";"))
    if skip != {"tester", "portal", "oras"}:
        problems.append(f"{ch}: the identity token is not restricted to the build container (skip-containers {sorted(skip)})")
    env = {e["name"]: e for e in build.get("env", [])}
    if "BUILD_TOKEN" in env:
        problems.append(f"{ch}: BUILD_TOKEN is an environment variable — the worker token must be a mounted file")
    if env.get("BUILD_TOKEN_FILE", {}).get("value") != "/run/build-worker/token":
        problems.append(f"{ch}: BUILD_TOKEN_FILE is not /run/build-worker/token")
    if pod.get("terminationGracePeriodSeconds") != 7200 or env.get("MW_BUILD_RUN_DEADLINE_SECONDS", {}).get("value") != "7200":
        problems.append(f"{ch}: terminationGracePeriodSeconds / the run deadline are not the same 7200 s (the default run budget)")
    if build.get("securityContext", {}).get("seccompProfile", {}) != {"type": "Unconfined"}:
        problems.append(f"{ch}: the stated sandbox seccomp profile did not render: {build.get('securityContext')}")
    if build.get("securityContext", {}).get("appArmorProfile", {}) != {"type": "Localhost", "localhostProfile": "build-host-userns"}:
        problems.append(f"{ch}: a Localhost AppArmor profile must render its localhostProfile: {build.get('securityContext')}")
    if env.get("MW_BUILD_CHANNEL", {}).get("value") != ch or env.get("MW_BUILD_PLATFORM_VERSION", {}).get("value") != pv:
        problems.append(f"{ch}: channel/platformVersion env wrong")
    if env.get("BUILD_API", {}).get("value") != "http://memex-portal-service:8080/api/builds":
        problems.append(f"{ch}: BUILD_API not the trimmed apiUrl: {env.get('BUILD_API')}")
    if "exec python3 /opt/build-host/build-run.py --host" not in " ".join(build.get("command", [])):
        problems.append(f"{ch}: the runner is not exec'd (SIGTERM would not reach it)")
    if "test -f /tmp/build-host-ready" not in " ".join(build.get("readinessProbe", {}).get("exec", {}).get("command", [])):
        problems.append(f"{ch}: readiness is not the staged marker")
    cmd = " ".join(build.get("command", []))
    if "compose-gate-host.sh /portal/app /tester /host/app" not in cmd or env.get("MW_BUILD_GATE_HOST", {}).get("value") != "/host/app":
        problems.append(f"{ch}: the gate host must be composed into /host/app (the composer removes its output; /host is a mount point)")
    # An init container copying into the ROOT of a mount it does not own must not preserve
    # attributes: `cp -a` sets the mount's own times/mode and fails EPERM as the unprivileged user
    # (measured on control 2026-10-26: the tester copy crash-looped, the host never started).
    for _ic in pod.get("initContainers", []):
        _c = " ".join(_ic.get("command", []))
        for _m in _ic.get("volumeMounts", []):
            if re.search(r"cp -a \S+/\. " + re.escape(_m["mountPath"]) + r"/?(\s|$)", _c):
                problems.append(f"{ch}: init container {_ic['name']} copies with cp -a into the root of mount {_m['mountPath']} — fails EPERM as the pod's user; use cp -R")
    if any(c["name"] == "postgres" for c in pod.get("containers", []) + pod.get("initContainers", [])):
        problems.append(f"{ch}: a shared Postgres container renders — a stage's database is disposable, inside its own sandbox")
    _vols = {v["name"]: v for v in pod.get("volumes", [])}
    if env.get("MW_BUILD_ARTIFACT_STORE", {}).get("value") != "file:/ci-artifacts" \
            or _vols.get("ci-artifacts", {}).get("persistentVolumeClaim", {}).get("claimName") != "ci-artifacts":
        problems.append(f"{ch}: the H9 artifact store is not wired (file:/ci-artifacts on its claim): {env.get('MW_BUILD_ARTIFACT_STORE')}")
    if env.get("MW_REGISTRY_URL", {}).get("value") != "https://portal.example.com" or env.get("MW_REGISTRY_KEY_FILE", {}).get("value") != "/run/registry-key/key":
        problems.append(f"{ch}: the registry bundle index (H8) is not wired: {env.get('MW_REGISTRY_URL')}")
    rk = vols.get("registry-key", {}).get("secret", {}) if (vols := {v["name"]: v for v in pod.get("volumes", [])}) else {}
    if rk.get("secretName") != "build-registry-key":
        problems.append(f"{ch}: the mwi_ instance key is not mounted as a file from its Secret: {rk}")
    vols = {v["name"]: v for v in pod.get("volumes", [])}
    if vols.get("cache", {}).get("persistentVolumeClaim", {}).get("claimName") != "build-cache":
        problems.append(f"{ch}: the cache is not the build-cache claim")
    wt = vols.get("worker-token", {}).get("secret", {})
    if wt.get("secretName") != "build-worker" or wt.get("items") != [{"key": "token", "path": "token"}]:
        problems.append(f"{ch}: the worker token is not the build-worker Secret mounted as a file: {wt}")
    secret_vols = sorted(v["name"] for v in pod.get("volumes", []) if "secret" in v)
    if secret_vols != ["registry-key", "worker-token"]:
        problems.append(f"{ch}: a host mounts no credential but the worker token; secret volumes: {secret_vols}")
    if any("bake" in v["name"] for v in pod.get("volumes", [])):
        problems.append(f"{ch}: a bake share is mounted")
    s = so["spec"]
    if s.get("minReplicaCount") != 0 or s.get("maxReplicaCount") != mx:
        problems.append(f"{ch}: ScaledObject min/max {s.get('minReplicaCount')}/{s.get('maxReplicaCount')} != 0/{mx}")
    if s.get("scaleTargetRef", {}).get("name") != f"build-host-{ch}":
        problems.append(f"{ch}: ScaledObject targets {s.get('scaleTargetRef')}")
    trig = [t for t in s.get("triggers", []) if t.get("type") == "metrics-api"]
    url = f"http://memex-portal-service:8080/api/builds/queue?channel={ch}"
    if len(trig) != 1 or trig[0]["metadata"].get("url") != url or trig[0]["metadata"].get("valueLocation") != "demand":
        problems.append(f"{ch}: not ONE metrics-api trigger on {url} valueLocation demand: {trig}")
    win = s.get("advanced", {}).get("horizontalPodAutoscalerConfig", {}).get("behavior", {}).get("scaleDown", {}).get("stabilizationWindowSeconds")
    if win != 300:
        problems.append(f"{ch}: scale-down stabilization window {win} != 300")
sa = find("ServiceAccount", "build-host")
if not sa or sa.get("automountServiceAccountToken") is not False:
    problems.append("ServiceAccount build-host missing or mounts a token")
elif (sa["metadata"].get("annotations") or {}).get("azure.workload.identity/client-id") != "00000000-0000-0000-0000-000000000001":
    problems.append("ServiceAccount build-host does not carry the workload identity's client id")
if find("ConfigMap", "memex-build-host-scripts"):
    problems.append("the chart renders a scripts ConfigMap — the runner ships in the image")
np = find("NetworkPolicy", "build-host")
if np:
    # The egress shape, pinned whole: exactly three rules.
    #   1. DNS (53/udp+tcp) to the cluster resolver ONLY;
    #   2. the build API's pods on their port ONLY;
    #   3. 80/443 to 0.0.0.0/0 EXCEPT every private range, link-local/IMDS and WireServer.
    egress = np["spec"].get("egress", [])
    if len(egress) != 3:
        problems.append(f"NetworkPolicy build-host has {len(egress)} egress rules, not the pinned 3")
    def ports(rule):
        return sorted((p.get("protocol"), p.get("port")) for p in rule.get("ports", []))
    dns = [r for r in egress if ports(r) == [("TCP", 53), ("UDP", 53)]]
    if not dns or not all(to.get("namespaceSelector", {}).get("matchLabels") == {"kubernetes.io/metadata.name": "kube-system"}
                          and to.get("podSelector", {}).get("matchLabels") == {"k8s-app": "kube-dns"}
                          for r in dns for to in (r.get("to") or [{}])):
        problems.append(f"DNS egress is not restricted to the cluster resolver: {dns}")
    api = [r for r in egress if ports(r) == [("TCP", 8080)]]
    if not api or any("ipBlock" in to for r in api for to in r.get("to", [])):
        problems.append(f"the build-API egress is not a pod selector on its port: {api}")
    web = [r for r in egress if ports(r) == [("TCP", 80), ("TCP", 443)]]
    blocks = [to.get("ipBlock") for r in web for to in r.get("to", [])]
    must = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "168.63.129.16/32"}
    if len(blocks) != 1 or not blocks[0] or blocks[0].get("cidr") != "0.0.0.0/0" or not must <= set(blocks[0].get("except", [])):
        problems.append(f"the 80/443 egress does not exclude every private range, IMDS and WireServer: {blocks}")
    if any(not to for r in egress for to in r.get("to", [None])):
        problems.append("an egress rule has no destination (it would allow every address)")
if not np or np["spec"].get("ingress") != [] or "Ingress" not in np["spec"].get("policyTypes", []):
    problems.append("NetworkPolicy build-host missing or admits ingress")
if problems:
    print("\n".join(problems)); sys.exit(1)
PY
  then ok "the fixture renders both channels' hosts: tester/portal/runner digests, non-root, workload identity (AcrPull, host only), pool, token as a file, grace = run deadline, cache, readiness, KEDA 0..max on demand with a scale-down window, no other credential, no bake share, no SA token, no ingress"
  else report "the build-host fixture renders a wrong shape (see above)"
  fi
else
  report "the build-host fixture does not render:"; sed 's/^/    /' "$WORK/hosts.err"
fi

# 3c. versioned build servers: 1 and 3 versions (0 is the defaults case above)
for spec in "values.build-hosts-one-version.yaml|3.1.10500:latest:2" \
            "values.build-hosts-versions.yaml|3.1.10400:previous:2,3.1.10500:latest:3,3.1.10600:candidate:4"; do
  f="${spec%%|*}"; want="${spec#*|}"
  if helm template r "$CHART" -f "$CHART/values.yaml" -f "$TD/$f" --namespace check > "$WORK/v.yaml" 2> "$WORK/v.err"; then
    if python3 - "$WORK/v.yaml" "$want" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
want = [w.split(":") for w in sys.argv[2].split(",")]
problems = []
deps = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment" and (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component") == "build-host"}
sos = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ScaledObject"}
names = sorted("build-" + v.replace(".", "-") for v, _, _ in want)
if sorted(deps) != names:
    problems.append(f"Deployments {sorted(deps)} are not one per version {names}")
for v, alias, mx in want:
    n = "build-" + v.replace(".", "-")
    dep, so = deps.get(n), sos.get(n + "-scaler")
    if not dep or not so:
        problems.append(f"{v}: Deployment or ScaledObject missing"); continue
    labels = dep["metadata"]["labels"]
    if labels.get("meshweaver.io/build-version") != v or labels.get(f"meshweaver.io/build-alias-{alias}") != "true":
        problems.append(f"{v}: labels do not name the version and its alias {alias}: {labels}")
    if "hosting.meshweaver.io/channel" in labels:
        problems.append(f"{v}: a versioned server carries a channel label")
    other = [k for k in labels if k.startswith("meshweaver.io/build-alias-") and k != f"meshweaver.io/build-alias-{alias}"]
    if other:
        problems.append(f"{v}: carries aliases it was not given: {other}")
    build = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "build")
    env = {e["name"]: e.get("value") for e in build.get("env", [])}
    if env.get("MW_BUILD_VERSION") != v or "MW_BUILD_CHANNEL" in env or env.get("MW_BUILD_PLATFORM_VERSION") != v:
        problems.append(f"{v}: the runner does not claim by version: {[k for k in env if k.startswith('MW_BUILD_')]}")
    # The core-tests image reaches the runner exactly when the version's set names one (it is proved on every claim).
    want_tests = "sha256:" + "d" * 64 if v == "3.1.10600" and "3.1.10400" in sys.argv[2] else None
    if env.get("MW_BUILD_CORE_TESTS_DIGEST") != want_tests:
        problems.append(f"{v}: MW_BUILD_CORE_TESTS_DIGEST is {env.get('MW_BUILD_CORE_TESTS_DIGEST')!r}, want {want_tests!r}")
    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    if any(k.startswith("meshweaver.io/build-alias-") for k in pod_labels):
        problems.append(f"{v}: an alias label is in the POD template — a switch would roll the pods: {pod_labels}")
    sel = dep["spec"]["selector"]["matchLabels"]
    if sel.get("meshweaver.io/build-version") != v:
        problems.append(f"{v}: the selector does not pin the version (two versions' pods would mix): {sel}")
    s = so["spec"]
    url = f"http://memex-portal-service:8080/api/builds/queue?version={v}"
    trig = s.get("triggers", [{}])[0].get("metadata", {}).get("url")
    if trig != url or s.get("maxReplicaCount") != int(mx) or s.get("scaleTargetRef", {}).get("name") != n:
        problems.append(f"{v}: ScaledObject not demand-by-version {url} / max {mx} / target {n}: {trig} {s.get('maxReplicaCount')}")
if problems:
    print("\n".join(problems)); sys.exit(1)
PY
    then ok "$f renders one build-<v> per version, each claiming and scaling by its version, labelled with its alias alone"
    else report "$f renders a wrong versioned shape (see above)"
    fi
  else
    report "$f does not render:"; sed 's/^/    /' "$WORK/v.err"
  fi
done

# 3b. the cache claim (opt-in; absent from the fixture above)
if helm template r "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-hosts-cache.yaml" --namespace check \
     > "$WORK/cache.yaml" 2> "$WORK/cache.err"; then
  if python3 - "$WORK/cache.yaml" "$WORK/hosts.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
plain = [d for d in yaml.safe_load_all(open(sys.argv[2])) if d]
problems = []
def find(ds, kind, name):
    return next((d for d in ds if d["kind"] == kind and d["metadata"]["name"] == name), None)
pvc = find(docs, "PersistentVolumeClaim", "build-cache")
if not pvc:
    problems.append("cache.create renders no build-cache claim")
else:
    spec = pvc["spec"]
    if spec.get("accessModes") != ["ReadWriteMany"] or spec.get("storageClassName") != "azurefile-memex" \
            or spec.get("resources", {}).get("requests", {}).get("storage") != "100Gi":
        problems.append(f"the build-cache claim is not RWX on the stated class and size: {spec}")
    if (pvc["metadata"].get("annotations") or {}).get("helm.sh/resource-policy") != "keep":
        problems.append("the build-cache claim is not kept on uninstall (helm.sh/resource-policy: keep)")
if find(plain, "PersistentVolumeClaim", "build-cache"):
    problems.append("without cache.create the chart still renders the claim")
if any(d["kind"] == "DaemonSet" for d in docs if (d.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/component", "").startswith("build-host")):
    problems.append("the chart renders a node-level DaemonSet — node setup is the platform lane's, never an instance release's")
if problems:
    print("\n".join(problems)); sys.exit(1)
PY
  then ok "cache.create renders the kept RWX build-cache claim, and nothing without it; the chart renders no node-level DaemonSet"
  else report "the cache claim renders a wrong shape (see above)"
  fi
else
  report "the cache fixture does not render:"; sed 's/^/    /' "$WORK/cache.err"
fi


# 3d. a BUILD-HOST-ONLY release (portal.enabled: false — MeshWeaver.Plugins Hosting/BuildQueue, phase 2):
#     one platform version's host as its OWN release beside the control instance's portal. It must
#     render EXACTLY the version's Deployment + ScaledObject, its cache claim, and the release's
#     ServiceAccount + NetworkPolicy — and nothing of the portal. A second such release (another
#     version) and the portal's own release (which may render hosts too) share the namespace, so no
#     (kind, name) may occur in two of the three renders.
REL="build-3-1-11641"
sed -e 's/3\.1\.11641/3.1.11700/' -e 's/build-cache-3-1-11641/build-cache-3-1-11700/' \
  "$TD/values.build-host-release.yaml" > "$WORK/release-b.yaml"
if helm template "$REL" "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-host-release.yaml" --namespace check \
     > "$WORK/release.yaml" 2> "$WORK/release.err" \
   && helm template build-3-1-11700 "$CHART" -f "$CHART/values.yaml" -f "$WORK/release-b.yaml" --namespace check \
     > "$WORK/release-b.out" 2> "$WORK/release-b.err"; then
  if python3 - "$WORK/release.yaml" "$REL" "$WORK/release-b.out" "$WORK/hosts.yaml" <<'PY'
import sys, yaml
from collections import Counter
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
rel = sys.argv[2]
problems = []
kinds = Counter(d["kind"] for d in docs)
want = Counter({"Deployment": 1, "ScaledObject": 1, "PersistentVolumeClaim": 1, "ServiceAccount": 1, "NetworkPolicy": 1})
if kinds != want:
    problems.append(f"the release renders {dict(kinds)}, not exactly {dict(want)}")
foreign = [f"{d['kind']}/{d['metadata']['name']}" for d in docs
           if (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component") != "build-host"]
if foreign:
    problems.append(f"objects that are not build-host objects render (portal, migration, postgres, registry, …): {foreign}")
names = {(d["kind"], d["metadata"]["name"]) for d in docs}
shared = f"build-host-{rel}"
for k, n in [("Deployment", "build-3-1-11641"), ("ScaledObject", "build-3-1-11641-scaler"),
             ("PersistentVolumeClaim", "build-cache-3-1-11641"), ("ServiceAccount", shared), ("NetworkPolicy", shared)]:
    if (k, n) not in names:
        problems.append(f"{k}/{n} is missing: {sorted(names)}")
dep = next((d for d in docs if d["kind"] == "Deployment"), None)
if dep:
    pod = dep["spec"]["template"]["spec"]
    if pod.get("serviceAccountName") != shared:
        problems.append(f"the host does not run as its release's ServiceAccount {shared}: {pod.get('serviceAccountName')}")
    if pod.get("imagePullSecrets") != [{"name": "build-pull"}]:
        problems.append(f"imagePullSecret did not render: {pod.get('imagePullSecrets')}")
    vols = {v["name"]: v for v in pod.get("volumes", [])}
    if vols.get("cache", {}).get("persistentVolumeClaim", {}).get("claimName") != "build-cache-3-1-11641":
        problems.append("the host does not mount the release's cache claim")
    wt = vols.get("worker-token", {}).get("secret", {})
    if wt.get("secretName") != "build-worker-synced" or wt.get("items") != [{"key": "Hosting__Builds__WorkerToken", "path": "token"}]:
        problems.append(f"the worker token is not the synced Secret's key mounted as a file: {wt}")
    build = next(c for c in pod["containers"] if c["name"] == "build")
    env = {e["name"]: e.get("value") for e in build.get("env", [])}
    if env.get("MW_BUILD_VERSION") != "3.1.11641" or env.get("MW_BUILD_CORE_TESTS_DIGEST") != "sha256:" + "4" * 64:
        problems.append(f"the host does not claim by its version with its core-tests digest: {env.get('MW_BUILD_VERSION')} {env.get('MW_BUILD_CORE_TESTS_DIGEST')}")
    if build.get("securityContext", {}).get("appArmorProfile") != {"type": "Localhost", "localhostProfile": "mw-build-host"}:
        problems.append(f"the sandbox AppArmor profile did not render: {build.get('securityContext')}")
np = next((d for d in docs if d["kind"] == "NetworkPolicy"), None)
if np and np["spec"]["podSelector"]["matchLabels"].get("app.kubernetes.io/instance") != rel:
    problems.append(f"the release's NetworkPolicy does not select its own release's pods: {np['spec']['podSelector']}")
pvc = next((d for d in docs if d["kind"] == "PersistentVolumeClaim"), None)
if pvc and (pvc["spec"].get("accessModes") != ["ReadWriteMany"] or pvc["spec"].get("storageClassName") != "azurefile-memex"
            or (pvc["metadata"].get("annotations") or {}).get("helm.sh/resource-policy") != "keep"):
    problems.append(f"the cache claim is not a kept RWX claim on the stated class: {pvc}")
# Two releases and the portal's own release in ONE namespace: no object may be named twice.
seen = {}
for label, path in [("release " + rel, sys.argv[1]), ("release build-3-1-11700", sys.argv[3]), ("the portal release", sys.argv[4])]:
    for d in yaml.safe_load_all(open(path)):
        if d and (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component") == "build-host":
            key = (d["kind"], d["metadata"]["name"])
            if key in seen:
                problems.append(f"{key[0]}/{key[1]} renders in both {seen[key]} and {label} — they would collide in one namespace")
            seen[key] = label
if problems:
    print("\n".join(problems)); sys.exit(1)
PY
  then ok "a build-host-only release renders exactly build-3-1-11641 (+ its scaler), its cache claim and the release-named ServiceAccount + NetworkPolicy, nothing of the portal, and collides with neither another version's release nor the portal's"
  else report "the build-host-only release renders a wrong shape (see above)"
  fi
else
  report "the build-host-only release does not render:"; sed 's/^/    /' "$WORK/release.err" "$WORK/release-b.err"
fi

# 3d'. two valid 53-character release names that differ only in their LAST character must still get
#      two distinct shared names within a DNS label's 63 characters (review of #14: a plain truncation
#      mapped both onto one ServiceAccount + NetworkPolicy, and the second install failed Helm's
#      ownership check).
long_a="build-3-1-0-$(printf 'a%.0s' $(seq 40))1"; long_b="${long_a%1}2"
if helm template "$long_a" "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-host-release.yaml" --namespace check > "$WORK/long-a.yaml" 2> "$WORK/long.err" \
   && helm template "$long_b" "$CHART" -f "$CHART/values.yaml" -f "$WORK/release-b.yaml" --namespace check > "$WORK/long-b.yaml" 2>> "$WORK/long.err"; then
  if python3 - "$WORK/long-a.yaml" "$WORK/long-b.yaml" <<'PY'
import sys, yaml
def shared(p):
    return {d["kind"]: d["metadata"]["name"] for d in yaml.safe_load_all(open(p)) if d and d["kind"] in ("ServiceAccount", "NetworkPolicy")}
a, b = shared(sys.argv[1]), shared(sys.argv[2])
problems = []
for k in ("ServiceAccount", "NetworkPolicy"):
    if k not in a or k not in b:
        problems.append(f"{k} missing: {a} {b}"); continue
    if a[k] == b[k]:
        problems.append(f"{k}: two release names that differ only in the last character share the name {a[k]}")
    if max(len(a[k]), len(b[k])) > 63:
        problems.append(f"{k}: a name exceeds 63 characters: {a[k]} / {b[k]}")
if problems:
    print("\n".join(problems)); sys.exit(1)
PY
  then ok "53-character release names that differ only at the end get distinct shared names within 63 characters"
  else report "long release names collide or overflow (see above)"
  fi
else
  report "the long-release-name renders fail:"; sed 's/^/    /' "$WORK/long.err"
fi

# 3e. every template OUTSIDE templates/build-host/ opens with the portal gate — so a template added
#     later cannot render into a build-host-only release because nobody wrapped it (the fixture above
#     only sees the values it sets; this sees every file).
GATE='{{- if eq (include "memex.portal.enabled" .) "true" -}}'
ungated=""
while IFS= read -r f; do
  [ "$(head -n1 "$f")" = "$GATE" ] || ungated="$ungated ${f#"$CHART"/}"
done < <(find "$CHART/templates" -type f ! -name '_*' ! -path '*/templates/build-host/*' | sort)
if [ -z "$ungated" ]; then
  ok "every template outside templates/build-host/ opens with the portal.enabled gate"
else
  report "templates outside templates/build-host/ that do not open with the portal.enabled gate (they would render into a build-host-only release):$ungated"
fi

# 3c. a leased-back Mac's first attach: an EXTRA channel `mac` renders its own Deployment + ScaledObject that claims
#     and scales by channel=mac against the public build API, and its cache claim is ReadWriteOnce on the node-local class.
if helm template build-host-mac "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-host-mac.yaml" --namespace memex \
     > "$WORK/mac.yaml" 2> "$WORK/mac.err"; then
  python3 - "$WORK/mac.yaml" <<'PY2' && ok "an extra channel 'mac' renders build-host-mac (claiming channel=mac at the public API) with a ReadWriteOnce cache, and nothing of the portal" || report "the mac first-attach render is wrong (above)"
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
problems = []
dep = by.get(("Deployment", "build-host-mac"))
if not dep:
    problems.append("no Deployment build-host-mac")
else:
    env = {e["name"]: e.get("value") for c in dep["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
    if env.get("MW_BUILD_CHANNEL") != "mac": problems.append(f"MW_BUILD_CHANNEL={env.get('MW_BUILD_CHANNEL')!r}")
    if env.get("BUILD_API") != "https://control.example.com/api/builds": problems.append(f"BUILD_API={env.get('BUILD_API')!r}")
    if env.get("MW_REGISTRY_URL") != "https://registry.example.com" or env.get("MW_REGISTRY_KEY_FILE") != "/run/registry-key/key":
        problems.append("the plugins layer is not read through the registry bundle index (H8)")
    spec = dep["spec"]["template"]["spec"]
    if spec.get("nodeSelector") or spec.get("tolerations"): problems.append(f"the CI pool selector/tolerations leaked: {spec.get('nodeSelector')} {spec.get('tolerations')}")
    vols = {v["name"]: v for v in spec.get("volumes", [])}
    if vols.get("registry-key", {}).get("secret", {}).get("secretName") != "build-registry-key-mac": problems.append("no registry-key volume")
    builds = [c for c in spec["containers"] if c["name"] == "build"]
    if not builds or builds[0]["resources"]["requests"] != {"cpu": "4", "memory": "8Gi"}: problems.append(f"requests: {builds and builds[0]['resources']}")
    if dep["spec"]["strategy"] != {"type": "Recreate"}: problems.append(f"strategy: {dep['spec']['strategy']}")
so = [d for d in docs if d["kind"] == "ScaledObject"]
if len(so) != 1 or "channel=mac" not in str(so[0]): problems.append("the ScaledObject does not scale on channel=mac")
pvc = by.get(("PersistentVolumeClaim", "build-cache-mac"))
if not pvc or pvc["spec"]["accessModes"] != ["ReadWriteOnce"] or pvc["spec"]["storageClassName"] != "local-path":
    problems.append(f"cache claim: {pvc and pvc['spec']}")
kinds = sorted({d["kind"] for d in docs})
if any(k in kinds for k in ("Service", "Ingress", "StatefulSet")): problems.append(f"portal objects rendered: {kinds}")
for p in problems: print("   ", p)
sys.exit(1 if problems else 0)
PY2
else
  report "the mac first-attach values did not render:"; sed 's/^/    /' "$WORK/mac.err"
fi

# 3d. the opt-in user-namespace sandbox: hostUsers false on the pod, procMount Unmasked on the build container, and on a
#     node pool the selector narrowed to kernel-userns nodes. Off (the default), none of it renders.
if helm template r "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-hosts.yaml" --set buildHosts.sandbox.userNamespace=true \
     --namespace check > "$WORK/userns.yaml" 2> "$WORK/userns.err" \
   && helm template r "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-hosts.yaml" --namespace check > "$WORK/no-userns.yaml" 2>> "$WORK/userns.err"; then
  python3 - "$WORK/userns.yaml" "$WORK/no-userns.yaml" <<'PY3' && ok "sandbox.userNamespace renders hostUsers false + procMount Unmasked on kernel-userns nodes, and nothing of it when off" || report "the user-namespace sandbox render is wrong (above)"
import sys, yaml
def hosts(path):
    return [d for d in yaml.safe_load_all(open(path)) if d and d.get("kind") == "Deployment"
            and (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component") == "build-host"]
problems = []
on, off = hosts(sys.argv[1]), hosts(sys.argv[2])
if not on or not off: problems.append(f"no build-host Deployment rendered (on={len(on)} off={len(off)})")
for d in on:
    spec = d["spec"]["template"]["spec"]
    build = next(c for c in spec["containers"] if c["name"] == "build")
    if spec.get("hostUsers") is not False: problems.append(f"{d['metadata']['name']}: hostUsers={spec.get('hostUsers')!r}")
    if build.get("securityContext", {}).get("procMount") != "Unmasked": problems.append(f"{d['metadata']['name']}: procMount not Unmasked")
    if (spec.get("nodeSelector") or {}).get("kernel-userns") != "true" or (spec.get("nodeSelector") or {}).get("workload") != "ci-build":
        problems.append(f"{d['metadata']['name']}: nodeSelector {spec.get('nodeSelector')}")
    if {"key": "kernel-userns", "operator": "Equal", "value": "true", "effect": "NoSchedule"} not in (spec.get("tolerations") or []):
        problems.append(f"{d['metadata']['name']}: no toleration for the kernel-userns pool's taint")
for d in off:
    spec = d["spec"]["template"]["spec"]
    build = next(c for c in spec["containers"] if c["name"] == "build")
    if "hostUsers" in spec or "procMount" in build.get("securityContext", {}) or "kernel-userns" in (spec.get("nodeSelector") or {}) \
            or any(t.get("key") == "kernel-userns" for t in spec.get("tolerations") or []):
        problems.append(f"{d['metadata']['name']}: the user-namespace sandbox rendered while off")
for p in problems: print("   ", p)
sys.exit(1 if problems else 0)
PY3
else
  report "the user-namespace values did not render:"; sed 's/^/    /' "$WORK/userns.err"
fi

# 3e. a node-local cache (cache.medium: node): an emptyDir sized by cache.size on every host, and no cache claim at all
#     — what a host in its own user namespace needs, since an SMB/NFS claim cannot be ID-mapped into it.
if helm template r "$CHART" -f "$CHART/values.yaml" -f "$TD/values.build-hosts.yaml" --set buildHosts.cache.medium=node \
     --set buildHosts.cache.size=100Gi --set buildHosts.cache.create=true --set buildHosts.cache.storageClass=azurefile-memex \
     --namespace check > "$WORK/nodecache.yaml" 2> "$WORK/nodecache.err"; then
  python3 - "$WORK/nodecache.yaml" <<'PY4' && ok "cache.medium node renders a node-local emptyDir cache (sizeLimit) on every host and no cache claim" || report "the node-local cache render is wrong (above)"
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
problems = []
hosts = [d for d in docs if d.get("kind") == "Deployment" and (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component") == "build-host"]
if not hosts: problems.append("no build-host Deployment rendered")
for d in hosts:
    vols = {v["name"]: v for v in d["spec"]["template"]["spec"]["volumes"]}
    if vols.get("cache", {}).get("emptyDir") != {"sizeLimit": "100Gi"} or "persistentVolumeClaim" in vols.get("cache", {}):
        problems.append(f"{d['metadata']['name']}: cache volume {vols.get('cache')}")
pvcs = [d["metadata"]["name"] for d in docs if d.get("kind") == "PersistentVolumeClaim"]
if pvcs: problems.append(f"a claim rendered although the cache is node-local: {pvcs}")
for p in problems: print("   ", p)
sys.exit(1 if problems else 0)
PY4
else
  report "the node-local cache values did not render:"; sed 's/^/    /' "$WORK/nodecache.err"
fi

# 4. refusals
for entry in \
  "values.build-hosts-tag-not-digest.yaml|must be a sha256:<64 hex> digest" \
  "values.build-hosts-public-http.yaml|must be https, or http to a cluster-local host" \
  "values.build-hosts-registry-credential.yaml|a build host holds NO registry credential" \
  "values.build-hosts-postgres-sidecar.yaml|runs NO shared database" \
  "values.build-hosts-localhost-no-profile.yaml|names no profile" \
  "values.build-hosts-run-deadline-too-long.yaml|RUN budget" \
  "values.build-hosts-run-deadline-zero.yaml|RUN budget" \
  "values.build-hosts-alias-twice.yaml|an alias is ONE version" \
  "values.build-hosts-version-twice.yaml|one deployment per version" \
  "values.build-hosts-bad-version.yaml|must be MAJOR.MINOR.PATCH" \
  "values.build-hosts-version-too-long.yaml|allows at most 48" \
  "values.build-hosts-bad-core-tests.yaml|coreTestsDigest \"latest\" must be a sha256" \
  "values.build-hosts-versions-old-runner.yaml|versionedRunner is not true" \
  "values.build-hosts-trailing-dash.yaml|ending in a letter or digit" \
  "values.build-hosts-zero-replicas.yaml|maxReplicas must be at least 1" \
  "values.build-hosts-name-collision.yaml|one deployment per name" \
  "values.build-hosts-cache-no-class.yaml|buildHosts.cache.storageClass is required" \
  "values.build-hosts-bad-extra-channel.yaml|not latest, stable or probe*" \
  "values.build-hosts-cache-bad-access.yaml|must be ReadWriteMany or ReadWriteOnce" \
  "values.build-hosts-extra-channel-dash.yaml|ending in a letter or digit" \
  "values.build-hosts-userns-not-bool.yaml|sandbox.userNamespace" \
  "values.build-hosts-cache-bad-medium.yaml|must be claim or node" \
  "values.build-hosts-cache-medium-false.yaml|must be claim or node" \
  "values.build-hosts-cache-medium-zero.yaml|must be claim or node" \
  "values.build-hosts-cache-medium-empty.yaml|must be claim or node" \
  "values.build-hosts-userns-empty-0.yaml|sandbox.userNamespace" \
  "values.build-hosts-userns-empty-string.yaml|sandbox.userNamespace" \
  "values.build-hosts-userns-empty-list.yaml|sandbox.userNamespace" \
  "values.build-host-release-no-hosts.yaml|portal.enabled is false but buildHosts.enabled is not true" \
  "values.build-host-release-postgres.yaml|portal.enabled is false but postgres.enabled is true" \
  "values.build-host-release-ingress.yaml|portal.enabled is false but ingress.enabled is true"; do
  f="${entry%%|*}"; phrase="${entry#*|}"
  if helm template r "$CHART" -f "$CHART/values.yaml" -f "$TD/$f" > "$WORK/refusal.yaml" 2> "$WORK/refusal.err"; then
    report "refusal $f RENDERED — the chart must refuse it"
  elif grep -qF -- "$phrase" "$WORK/refusal.err"; then
    ok "refusal $f holds ($phrase)"
  else
    report "refusal $f failed, but not for the stated reason ('$phrase'):"; sed 's/^/    /' "$WORK/refusal.err"
  fi
done

exit "$fail"
