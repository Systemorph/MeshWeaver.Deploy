#!/usr/bin/env python3
"""Does the build host's declared FLOOR fit the Mac's one node? (first-attach.sh, step 0)

    host-fits-node.py "<allocatable cpu> <allocatable memory>" <values file>
    host-fits-node.py --self-test

A Mac is ONE node, so every replica the chart keeps standing has to fit on it at once. The standing replicas are the
declared floors (`buildHosts.<channel>.minReplicas`, default 0) of every enabled channel — `latest`, `stable` and each
of `buildHosts.extraChannels` — the same channels and the same default the chart's ScaledObject renders
(deploy/helm/templates/build-host/_build-hosts.tpl). The check is the floor TIMES one replica's requests against the
node's allocatable: checking one replica alone passed a 24 GiB VM for a floor of two 20 GiB replicas, and the second
stayed Pending for good. A channel floor of 0 still needs one replica to fit, so the floor counts as at least one.

Exits 1 naming the shortfall; prints the reading on success. Needs PyYAML.
"""
import re
import sys


def cores(v):
    v = str(v)
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)


def gib(v):
    units = {"Ki": 1 / 1048576, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024}
    m = re.fullmatch(r"([0-9.]+)([KMGT]i)?", str(v))
    if not m:
        raise ValueError(f"unreadable memory quantity {v!r}")
    return float(m.group(1)) * (units[m.group(2)] if m.group(2) else 1 / 1073741824)


def floor_of(b):
    """The replicas the chart keeps standing: the sum of the enabled channels' minReplicas (default 0)."""
    names = ["latest", "stable"] + [n for n in (b.get("extraChannels") or []) if n not in ("latest", "stable")]
    return sum(int((b.get(n) or {}).get("minReplicas") or 0) for n in names if (b.get(n) or {}).get("enabled"))


def verdict(alloc, b):
    """(ok, message) for a node allocating `alloc` ("<cpu> <memory>") and the buildHosts block `b`."""
    cpu, mem = alloc.split()
    req = b["resources"]["requests"]
    floor = floor_of(b)
    n = max(floor, 1)
    need_cpu, need_mem = cores(req["cpu"]) * n, gib(req["memory"]) * n
    reading = (f"the host keeps {floor} replica(s) standing at {req['cpu']} CPU / {req['memory']} each "
               f"= {need_cpu:g} CPU / {need_mem:g} GiB; the node allocates {cpu} CPU / {mem}")
    if need_cpu > cores(cpu) or need_mem > gib(mem):
        return False, (f"ERROR: {reading} — a replica would stay Pending; give the Colima VM more "
                       "(colima stop; colima start --cpu N --memory M) or lower the values file's requests or minReplicas")
    return True, f"   {reading}"


def self_test():
    import os
    import yaml
    here = os.path.dirname(os.path.abspath(__file__))
    shipped = yaml.safe_load(open(os.path.join(here, "build-host-mac.values.yaml")))["buildHosts"]
    one = lambda req, **ch: {"resources": {"requests": req}, "extraChannels": ["mac"], "mac": {"enabled": True, **ch}}
    cases = [
        # The shipped values on the documented 16 CPU / 100 GiB VM (k3s allocates a little under the VM size): fits.
        ("documented build VM fits the shipped floor", "16 98Gi", shipped, True),
        # The regression: the previously documented 8 CPU / 24 GiB VM holds ONE 20 GiB replica but not the floor of two.
        ("an 8 CPU / 24 GiB VM refuses a floor of two 20 GiB replicas", "8 24Gi", shipped, False),
        ("CPU alone can fall short", "6 98Gi", shipped, False),
        ("allocatable in Ki and millicores", "16000m 102760448Ki", shipped, True),
        ("a floor of 0 still needs one replica to fit", "2 8Gi", one({"cpu": "4", "memory": "1Gi"}, minReplicas=0), False),
        ("a floor of 0 with room for one passes", "4 8Gi", one({"cpu": "4", "memory": "1Gi"}, minReplicas=0), True),
        ("a disabled channel's floor is not counted", "4 20Gi",
         {**one({"cpu": "4", "memory": "20Gi"}, minReplicas=1), "latest": {"enabled": False, "minReplicas": 3}}, True),
        ("every enabled channel's floor is counted", "8 40Gi",
         {**one({"cpu": "4", "memory": "20Gi"}, minReplicas=2), "latest": {"enabled": True, "minReplicas": 1}}, False),
    ]
    bad = 0
    for name, alloc, b, want in cases:
        got, msg = verdict(alloc, b)
        if got != want:
            bad += 1
            print(f"FAIL: {name}: expected {'fit' if want else 'refusal'}, got {msg.strip()}", file=sys.stderr)
        else:
            print(f"ok: {name}")
    if bad:
        sys.exit(1)
    print(f"host-fits-node self-test: {len(cases)} of {len(cases)} cases hold")


def main(argv):
    if argv[1:] == ["--self-test"]:
        self_test()
        return
    if len(argv) != 3:
        sys.exit(__doc__)
    import yaml
    ok, msg = verdict(argv[1], yaml.safe_load(open(argv[2]))["buildHosts"])
    print(msg, file=sys.stdout if ok else sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv)
