#!/usr/bin/env python3
"""Do the build host's standing replicas AND its peak fit the Mac's one node? (first-attach.sh, step 0)

    host-fits-node.py "<allocatable cpu> <allocatable memory>" <values file> [--pods-json <file>|-]
    host-fits-node.py --self-test

A Mac is ONE node, so every replica the chart can run has to fit on it beside what the node already runs.
Two checks, against the node's allocatable MINUS the requests of the pods already scheduled there (k3s, KEDA,
the kubelet's own: `kubectl get pods -A -o json`, given with --pods-json; only pods BOUND to a node count, each
with the scheduler's effective request (sidecars included); the host's own namespace `build-mac` is left out, so a
re-run does not count the replicas it is about to replace):

  * the FLOOR — the replicas the chart keeps standing: the sum of `buildHosts.<channel>.minReplicas` (default 0,
    counted as at least one) — checking one replica alone passed a 24 GiB VM for a floor of two 20 GiB replicas,
    and the second stayed Pending for good;
  * the PEAK — every replica KEDA may scale to: the sum of `maxReplicas` (default 1). Four 4-CPU requests on a
    16-CPU VM leave nothing for the system pods' requests, so the fourth replica stays Pending at peak demand.

The channels are the chart's own (deploy/helm/templates/build-host/_build-hosts.tpl): `latest`, `stable` and each
of `buildHosts.extraChannels`, DE-DUPLICATED as the chart's `uniq` does — `extraChannels: [mac, mac]` renders one
Deployment, so its floor counts once — and only the enabled ones.

Exits 1 naming the shortfall; prints the reading on success. Needs PyYAML.
"""
import json
import re
import sys


def cores(v):
    v = str(v)
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)


def gib(v):
    units = {"Ki": 1 / 1048576, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024, "k": 1e3 / 1073741824, "M": 1e6 / 1073741824,
             "G": 1e9 / 1073741824}
    m = re.fullmatch(r"([0-9.]+)([KMGT]i|[kMG])?", str(v))
    if not m:
        raise ValueError(f"unreadable memory quantity {v!r}")
    return float(m.group(1)) * (units[m.group(2)] if m.group(2) else 1 / 1073741824)


def channels(b):
    """The enabled channels, in the chart's order, each once (the chart's `concat (list latest stable) extra | uniq`)."""
    seen, out = set(), []
    for n in ["latest", "stable"] + list(b.get("extraChannels") or []):
        if n in seen:
            continue
        seen.add(n)
        if (b.get(n) or {}).get("enabled"):
            out.append(n)
    return out


def floor_of(b):
    """The replicas the chart keeps standing: the sum of the enabled channels' minReplicas (default 0)."""
    return sum(int((b.get(n) or {}).get("minReplicas") or 0) for n in channels(b))


def peak_of(b):
    """Every replica KEDA may scale to: the sum of the enabled channels' maxReplicas (the chart's default 1)."""
    return sum(int((b.get(n) or {}).get("maxReplicas") or 1) for n in channels(b))


def pod_request(spec, key, conv):
    """One pod's effective request for `key`, as the scheduler computes it: the larger of (a) the app containers
    plus every restartable init container (a sidecar, `restartPolicy: Always`, which keeps running beside them)
    and (b) the peak of the init sequence — each ordinary init container's request plus the sidecars started
    before it, and each sidecar's start (itself plus the sidecars before it)."""
    def req(c):
        return conv(((c.get("resources") or {}).get("requests") or {}).get(key, "0"))
    sidecars = 0.0
    init_peak = 0.0
    for c in spec.get("initContainers") or []:
        if c.get("restartPolicy") == "Always":
            sidecars += req(c)
            init_peak = max(init_peak, sidecars)
        else:
            init_peak = max(init_peak, req(c) + sidecars)
    return max(init_peak, sum(req(c) for c in spec.get("containers") or []) + sidecars)


def used_by_others(pods, own_namespace="build-mac"):
    """(cpu, GiB) requested on the node by the pods already BOUND to it (`spec.nodeName` set — an unscheduled
    Pending pod reserves nothing, a bound Pending one does), not finished, outside the host's own namespace."""
    cpu = mem = 0.0
    for p in (pods or {}).get("items", []):
        if p.get("metadata", {}).get("namespace") == own_namespace:
            continue
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        spec = p.get("spec", {})
        if not spec.get("nodeName"):
            continue
        cpu += pod_request(spec, "cpu", cores)
        mem += pod_request(spec, "memory", gib)
    return cpu, mem


def verdict(alloc, b, used=(0.0, 0.0)):
    """(ok, message) for a node allocating `alloc` ("<cpu> <memory>") with `used` already requested, and the block `b`."""
    cpu, mem = alloc.split()
    free_cpu, free_mem = cores(cpu) - used[0], gib(mem) - used[1]
    req = b["resources"]["requests"]
    one_cpu, one_mem = cores(req["cpu"]), gib(req["memory"])
    floor, peak = floor_of(b), peak_of(b)
    room = (f"the node allocates {cpu} CPU / {mem}, {used[0]:g} CPU / {used[1]:g} GiB of it already requested by "
            f"other pods, leaving {free_cpu:g} CPU / {free_mem:g} GiB")
    for what, n in (("standing floor", max(floor, 1)), ("peak", max(peak, 1))):
        need_cpu, need_mem = one_cpu * n, one_mem * n
        if need_cpu > free_cpu or need_mem > free_mem:
            return False, (f"ERROR: the host's {what} is {n} replica(s) at {req['cpu']} CPU / {req['memory']} each "
                           f"= {need_cpu:g} CPU / {need_mem:g} GiB; {room} — a replica would stay Pending; give the "
                           "Colima VM more (colima stop; colima start --cpu N --memory M) or lower the values file's "
                           "requests, minReplicas or maxReplicas")
    return True, (f"   floor {floor} / peak {peak} replica(s) at {req['cpu']} CPU / {req['memory']} each "
                  f"(peak {one_cpu * max(peak, 1):g} CPU / {one_mem * max(peak, 1):g} GiB); {room}")


def self_test():
    import os
    import yaml
    here = os.path.dirname(os.path.abspath(__file__))
    shipped = yaml.safe_load(open(os.path.join(here, "build-host-mac.values.yaml")))["buildHosts"]
    one = lambda req, **ch: {"resources": {"requests": req}, "extraChannels": ["mac"], "mac": {"enabled": True, **ch}}
    # what a fresh k3s + KEDA node requests (measured shape: coredns, local-path, metrics-server, KEDA's three)
    def pod(ns, phase, containers, init=None, node="colima-build"):
        spec = {"containers": [{"resources": {"requests": r}} for r in containers]}
        if init is not None:
            spec["initContainers"] = init
        if node:
            spec["nodeName"] = node
        return {"metadata": {"namespace": ns}, "status": {"phase": phase}, "spec": spec}
    system = {"items": [
        pod("kube-system", "Running", [{"cpu": "100m", "memory": "70Mi"}]),
        pod("kube-system", "Running", [{"cpu": "100m", "memory": "70Mi"}]),
        # an ordinary init container counts as the larger of it and the containers
        pod("keda", "Running", [{"cpu": "100m", "memory": "100Mi"}], init=[{"resources": {"requests": {"cpu": "500m"}}}]),
        # a bound Pending pod reserves its request
        pod("kube-system", "Pending", [{"cpu": "200m", "memory": "10Mi"}]),
        # NOT counted: a finished pod, the host's own replica, an UNSCHEDULED Pending pod (no nodeName)
        pod("kube-system", "Succeeded", [{"cpu": "8", "memory": "64Gi"}]),
        pod("build-mac", "Running", [{"cpu": "4", "memory": "20Gi"}]),
        pod("default", "Pending", [{"cpu": "4", "memory": "20Gi"}], node=None),
    ]}
    # a 1.5-CPU app with a 1-CPU restartable sidecar requests 2.5 CPU; an ordinary init after the sidecar adds to it
    sidecar = {"items": [pod("kube-system", "Running", [{"cpu": "1500m"}],
                             init=[{"restartPolicy": "Always", "resources": {"requests": {"cpu": "1"}}},
                                   {"resources": {"requests": {"cpu": "2"}}}])]}
    unbound_only = {"items": [pod("default", "Pending", [{"cpu": "4", "memory": "20Gi"}], node=None)]}
    sys_used = used_by_others(system)
    cases = [
        # The shipped values on the documented 16 CPU / 100 GiB VM, beside the system pods: floor and peak fit.
        ("documented build VM fits the shipped floor and peak beside the system pods", "16 98Gi", shipped, sys_used, True),
        # The floor regression: an 8 CPU / 24 GiB VM holds ONE 20 GiB replica but not the floor of two.
        ("an 8 CPU / 24 GiB VM refuses a floor of two 20 GiB replicas", "8 24Gi", shipped, (0, 0), False),
        ("CPU alone can fall short", "6 98Gi", shipped, (0, 0), False),
        ("allocatable in Ki and millicores", "16000m 102760448Ki", shipped, sys_used, True),
        # The peak regression (review 4503894461): four 4-CPU requests fill a 16-CPU node; any system request strands one.
        ("four 4-CPU requests on 16 CPU beside the system pods are refused at peak", "16 98Gi",
         one({"cpu": "4", "memory": "20Gi"}, minReplicas=2, maxReplicas=4), sys_used, False),
        ("the same peak fits a node with nothing else requested", "16 98Gi",
         one({"cpu": "4", "memory": "20Gi"}, minReplicas=2, maxReplicas=4), (0, 0), True),
        # Review 4504126537: an unscheduled Pending pod reserves nothing on the node.
        ("an unbound Pending pod does not block an empty node", "16 98Gi", shipped, used_by_others(unbound_only), True),
        # Review 4504126561: a restartable sidecar runs beside the app containers — 1.5 + 1 = 2.5 CPU, and the ordinary
        # init container after it peaks at 2 + 1 = 3 CPU; the shipped 14-CPU peak then does not fit 16 CPU.
        ("a pod with a sidecar is counted with it, so the shipped peak does not fit beside it", "16 98Gi", shipped,
         used_by_others(sidecar), False),
        # The dedup regression (review 4503894482): extraChannels [mac, mac] renders ONE Deployment.
        ("a duplicated extra channel counts its floor once", "12 60Gi",
         {**one({"cpu": "4", "memory": "20Gi"}, minReplicas=2, maxReplicas=2), "extraChannels": ["mac", "mac"]}, (0, 0), True),
        ("a floor of 0 still needs one replica to fit", "2 8Gi", one({"cpu": "4", "memory": "1Gi"}, minReplicas=0), (0, 0), False),
        ("a floor of 0 with room for one passes", "4 8Gi", one({"cpu": "4", "memory": "1Gi"}, minReplicas=0), (0, 0), True),
        ("a disabled channel is not counted", "4 20Gi",
         {**one({"cpu": "4", "memory": "20Gi"}, minReplicas=1), "latest": {"enabled": False, "minReplicas": 3, "maxReplicas": 3}}, (0, 0), True),
        ("every enabled channel is counted", "8 40Gi",
         {**one({"cpu": "4", "memory": "20Gi"}, minReplicas=2, maxReplicas=2), "latest": {"enabled": True, "minReplicas": 1}}, (0, 0), False),
    ]
    bad = 0
    if abs(sys_used[0] - 0.9) > 1e-9 or abs(sys_used[1] - 250 / 1024) > 1e-9:
        bad += 1
        print(f"FAIL: the system pods' requests read {sys_used}, expected 0.9 CPU / 250 MiB (finished pods, the host's "
              "own namespace and an unscheduled pod left out; a bound Pending pod counted; an init container counted as the larger)",
              file=sys.stderr)
    sc = used_by_others(sidecar)[0]
    if abs(sc - 3.0) > 1e-9:
        bad += 1
        print(f"FAIL: the sidecar pod reads {sc} CPU, expected 3 (the init after the sidecar: 2 + 1, above 1.5 + 1)", file=sys.stderr)
    for name, alloc, b, used, want in cases:
        got, msg = verdict(alloc, b, used)
        if got != want:
            bad += 1
            print(f"FAIL: {name}: expected {'fit' if want else 'refusal'}, got {msg.strip()}", file=sys.stderr)
        else:
            print(f"ok: {name}")
    if bad:
        sys.exit(1)
    print(f"host-fits-node self-test: {len(cases) + 2} of {len(cases) + 2} checks hold")


def main(argv):
    if argv[1:] == ["--self-test"]:
        self_test()
        return
    args = argv[1:]
    pods = None
    if "--pods-json" in args:
        i = args.index("--pods-json")
        src = args[i + 1]
        pods = json.load(sys.stdin if src == "-" else open(src))
        del args[i:i + 2]
    if len(args) != 2:
        sys.exit(__doc__)
    import yaml
    ok, msg = verdict(args[0], yaml.safe_load(open(args[1]))["buildHosts"], used_by_others(pods))
    print(msg, file=sys.stdout if ok else sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv)
