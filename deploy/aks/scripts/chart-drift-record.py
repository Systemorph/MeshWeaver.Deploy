#!/usr/bin/env python3
"""
The RECORD half of Chart Drift (MeshWeaver#4685): find the ONE committed Deployment record that owns
<namespace>/<release> and write the facts of it the check reads.

    chart-drift-record.py --records DIR --cluster CLUSTER --namespace NS --release NAME --out FILE

DIR is the config repository's `mesh/Deployments/` folder — the same records the overlays under
`deployments/aks/<ns>/` are generated from. A record owns the pair when its content names
`namespace: NS` and `helmRelease: NAME` AND it occupies the AUDITED cluster (MeshWeaver.Deploy#144):

  * its `status` is one that occupies a cluster — Live (blank reads Live, as DeploymentContent says),
    Provisioning or Suspended (a suspended release still stands in its namespace) — and
  * its `cluster` names CLUSTER (compared without regard to letter case, as Azure names are).

Records of one configuration repository legitimately share the conventional `namespace`/`helmRelease`
names: a Planned instance with no cluster yet, a Retired or Decommissioned reference record, an
instance on another estate's cluster. None of those runs on the audited cluster, so none is a
candidate; each is NAMED as left out, so a wrong status or cluster reads as what it is.

🚨 Two refusals keep the narrowing from guessing (an undecidable comparison is a failure):
  * a status this script does not know — the vocabulary is open, so an unknown value is never read as
    "occupies" or "does not": it is named and the run is red;
  * a record that occupies a cluster but names none — whether it runs HERE cannot be decided.

Written, and nothing else of the record:
  * {"record", "imageRepository", "updatePolicy", "updatePattern", "pinnedImageTag"} — the image the
    portal may run (policy `chart-drift-compares-image-to-record`; chart-drift-compare.py);
  * "vaultValuesKeys" — the NAMES of the keys the Key Vault values half feeds the chart's own Secret
    (policy `one-values-half-per-release`; chart-drift-render.py renders a placeholder half of exactly
    that shape). Names only — the record holds no value of them, and neither does this file.

🚨 Exactly ONE record must own the pair. NONE means the namespace runs a release no record declares,
and TWO means the records disagree about who owns it; either way the comparison cannot be decided,
and an undecidable comparison is a FAILURE (exit 1, naming the records), never a skip.
"""
import argparse, json, os, sys

IMAGE_FIELDS = ("imageRepository", "updatePolicy", "updatePattern", "pinnedImageTag")

# The record lifecycle (DeploymentContent.Status, platform `MeshWeaver.Deployment.Contract`), split by
# whether a release of that record stands on its cluster. Compared case-insensitively, trimmed.
OCCUPYING = frozenset({"live", "provisioning", "suspended"})
NOT_OCCUPYING = frozenset({"planned", "retired", "decommissioned", "torndown"})


def text(v):
    return v.strip() if isinstance(v, str) else ""


def content_of(doc):
    c = doc.get("content") if isinstance(doc, dict) else None
    return c if isinstance(c, dict) else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True)
    ap.add_argument("--cluster", default="")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--release", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    if not os.path.isdir(a.records):
        print(f"::error::the Deployment records folder '{a.records}' does not exist — the record "
              f"cannot be read, so the running image cannot be tested against it.")
        return 1

    cluster = (a.cluster or "").strip()
    if not cluster:
        print("::error::no audited cluster was named (--cluster) — several records may share the "
              f"namespace '{a.namespace}' and release '{a.release}' across clusters, so which one owns "
              "the release on THIS cluster cannot be decided without it. The Audit plan passes the "
              "record's cluster; a caller that does not is a caller bug, never a reason to count every "
              "record.")
        return 1

    owners, unreadable, left_out, undecidable = [], [], [], []
    for name in sorted(os.listdir(a.records)):
        if not name.endswith(".json") or name == "index.json":
            continue
        path = os.path.join(a.records, name)
        try:
            doc = json.load(open(path))
        except Exception as e:  # noqa: BLE001 — named below, never swallowed
            unreadable.append(f"{name} ({e})")
            continue
        c = content_of(doc)
        if text(c.get("namespace")) != a.namespace or text(c.get("helmRelease")) != a.release:
            continue
        rid = f"Deployments/{name[:-5]}"
        status = text(c.get("status")) or "Live"
        on = text(c.get("cluster"))
        if status.lower() in NOT_OCCUPYING:
            left_out.append(f"{rid} (status {status})")
        elif status.lower() not in OCCUPYING:
            undecidable.append(f"{rid} has status '{status}', which this check does not know — "
                               "whether its release stands on a cluster cannot be decided")
        elif not on:
            undecidable.append(f"{rid} is {status} but names no cluster — whether it runs on "
                               f"'{cluster}' cannot be decided")
        elif on.lower() != cluster.lower():
            left_out.append(f"{rid} (cluster {on})")
        else:
            owners.append((rid, c))

    if unreadable:
        # An unreadable record could be the owner; deciding without it would be deciding on a subset.
        print("::error::these Deployment records could not be parsed, so which record owns "
              f"{a.namespace}/{a.release} cannot be decided: " + "; ".join(unreadable))
        return 1
    skipped = ("; left out as not running on this cluster: " + ", ".join(left_out)) if left_out else ""
    if undecidable:
        print(f"::error::which Deployment record owns namespace '{a.namespace}' release "
              f"'{a.release}' on cluster '{cluster}' cannot be decided: " + "; ".join(undecidable)
              + skipped + ". Fix the records, not this check.")
        return 1
    if len(owners) != 1:
        which = ", ".join(o[0] for o in owners) or "none"
        print(f"::error::{len(owners)} Deployment record(s) own namespace '{a.namespace}' release "
              f"'{a.release}' on cluster '{cluster}' ({which}) — exactly one must, or the cluster "
              f"cannot be tested against a record{skipped}. Fix the records, not this check.")
        return 1
    for line in left_out:
        print(f"left out (not running on cluster '{cluster}'): {line}")

    record, c = owners[0]
    facts = {"record": record}
    for f in IMAGE_FIELDS:
        v = c.get(f)
        facts[f] = v.strip() if isinstance(v, str) and v.strip() else None
    # A declaration that is present but not a list of names is MALFORMED, never "no keys": read as
    # empty it would let the render prove an empty values-half shape and pass on an invalid record.
    keys = c.get("vaultValuesKeys")
    if keys is None:
        keys = []
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        print(f"::error::{record} declares vaultValuesKeys as {type(keys).__name__} "
              f"{'' if isinstance(keys, list) else '(not a list)'}— it must be a list of key names. "
              f"Refusing rather than reading a malformed declaration as 'no values half'.")
        return 1
    facts["vaultValuesKeys"] = sorted({k.strip() for k in keys if k.strip()})
    with open(a.out, "w") as out:
        json.dump(facts, out, indent=2)
        out.write("\n")
    print(f"record {record}: image repository {facts['imageRepository'] or '—'}, "
          f"policy {facts['updatePolicy'] or '—'}, pattern {facts['updatePattern'] or '—'}, "
          f"pin {facts['pinnedImageTag'] or '—'}; {len(facts['vaultValuesKeys'])} declared "
          f"values-half key(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
