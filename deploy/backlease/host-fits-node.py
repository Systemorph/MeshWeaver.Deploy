#!/usr/bin/env python3
"""Do the build host's standing replicas AND its peak fit the Mac's one node? (first-attach.sh, step 0)

    host-fits-node.py "<allocatable cpu> <allocatable memory>" <values file> [--pods-json <file>|-]
    host-fits-node.py --self-test

A Mac is ONE node, so every replica the chart can run has to fit on it beside what the node already runs.
Two checks, against the node's allocatable MINUS the requests of the pods already scheduled there (k3s, KEDA,
the kubelet's own: `kubectl get pods -A -o json`, given with --pods-json; the host's own namespace `build-mac`
is left out, so a re-run does not count the replicas it is about to replace):

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


def used_by_others(pods, own_namespace="build-mac"):
    """(cpu, GiB) requested by the pods already on the node: every container of a pod that is not finished,
    outside the host's own namespace. An init container's request is the larger of it and the containers'."""
    cpu = mem = 0.0
    for p in (pods or {}).get("items", []):
        if p.get("metadata", {}).get("namespace") == own_namespace:
            continue
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        spec = p.get("spec", {})
        def req(cs, key, conv):
            return [conv(((c.get("resources") or {}).get("requests") or {}).get(key, "0")) for c in cs or []]
        cpu += max([sum(req(spec.get("containers"), "cpu", cores))] + req(spec.get("initContainers"), "cpu", cores))
        mem += max([sum(req(spec.get("containers"), "memory", gib))] + req(spec.get("initContainers"), "memory", gib))
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
    system = {"items": [
        {"metadata": {"namespace": "kube-system"}, "status": {"phase": "Running"},
         "spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "70Mi"}}}]}},
        {"metadata": {"namespace": "kube-system"}, "status": {"phase": "Running"},
         "spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "70Mi"}}}]}},
        {"metadata": {"namespace": "keda"}, "status": {"phase": "Running"},
         "spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "100Mi"}}}],
                  "initContainers": [{"resources": {"requests": {"cpu": "500m"}}}]}},
        # a finished pod and the host's own replica are not counted
        {"metadata": {"namespace": "kube-system"}, "status": {"phase": "Succeeded"},
         "spec": {"containers": [{"resources": {"requests": {"cpu": "8", "memory": "64Gi"}}}]}},
        {"metadata": {"namespace": "build-mac"}, "status": {"phase": "Running"},
         "spec": {"containers": [{"resources": {"requests": {"cpu": "4", "memory": "20Gi"}}}]}},
    ]}
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
    if abs(sys_used[0] - 0.7) > 1e-9 or abs(sys_used[1] - 240 / 1024) > 1e-9:
        bad += 1
        print(f"FAIL: the system pods' requests read {sys_used}, expected 0.7 CPU / 240 MiB "
              "(finished pods and the host's own namespace left out; an init container counted as the larger)", file=sys.stderr)
    for name, alloc, b, used, want in cases:
        got, msg = verdict(alloc, b, used)
        if got != want:
            bad += 1
            print(f"FAIL: {name}: expected {'fit' if want else 'refusal'}, got {msg.strip()}", file=sys.stderr)
        else:
            print(f"ok: {name}")
    if bad:
        sys.exit(1)
    print(f"host-fits-node self-test: {len(cases) + 1} of {len(cases) + 1} checks hold")


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
