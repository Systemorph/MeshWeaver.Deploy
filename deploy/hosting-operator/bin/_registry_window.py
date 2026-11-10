#!/usr/bin/env python3
"""The two judgements hosting-registry-maintenance makes about the registry, kept out of bash.

  _registry_window.py scope <live manifest> <next manifest>
      Prints nothing and exits 0 when the release about to be applied differs from the live one ONLY in
      the maintenance window's own keys; otherwise prints every other difference and exits 1. The window
      is applied by `helm upgrade --reset-then-reuse-values` against the chart baked into this image, so
      a chart that moved since the release was last applied would otherwise ride along — a portal roll,
      a changed Secret — inside a registry maintenance window. What the window may change, and nothing else:
        * the ConfigMap memex-registry-config: storage.maintenance.readonly in config.yml;
        * the Deployment memex-registry-deployment: the pod template's checksum/config and
          memex-registry/maintenance annotations;
        * any Job of component memex-registry-gc (the collect itself) — added or removed.

  _registry_window.py pods <read-only|read-write> <deployment json> <pods json>
      Prints `ok <n>` when EVERY live registry pod was started from the Deployment's CURRENT template
      and that template is the requested side of the window; otherwise prints why not and exits 1.
      Pod by pod, because during a rolling update read-write and read-only pods serve side by side: a
      collect may start only when no read-write pod is left, not when the apply returned.
"""
import json
import sys

import yaml

GC_COMPONENT = "memex-registry-gc"
CONFIG_MAP = "memex-registry-config"
DEPLOYMENT = "memex-registry-deployment"
MAINTENANCE_ANNOTATION = "memex-registry/maintenance"
CHECKSUM = "checksum/config"


def documents(path):
    with open(path, encoding="utf-8") as handle:
        return [d for d in yaml.safe_load_all(handle.read()) if isinstance(d, dict) and d.get("kind")]


def key(doc):
    meta = doc.get("metadata") or {}
    return f'{doc.get("kind")}/{meta.get("namespace") or ""}/{meta.get("name")}'


def component(doc):
    return ((doc.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/component", "")


def normalized(doc):
    """The document with the window's own keys removed — what must be equal on both sides."""
    doc = json.loads(json.dumps(doc))
    name = (doc.get("metadata") or {}).get("name")
    if doc.get("kind") == "ConfigMap" and name == CONFIG_MAP:
        data = doc.get("data") or {}
        if "config.yml" in data:
            config = yaml.safe_load(data["config.yml"]) or {}
            maintenance = ((config.get("storage") or {}).get("maintenance")) or {}
            maintenance.pop("readonly", None)
            data["config.yml"] = config
    if doc.get("kind") == "Deployment" and name == DEPLOYMENT:
        annotations = (((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations") or {}
        annotations.pop(CHECKSUM, None)
        annotations.pop(MAINTENANCE_ANNOTATION, None)
    return doc


def scope(live_path, next_path):
    live = {key(d): d for d in documents(live_path) if component(d) != GC_COMPONENT}
    nxt = {key(d): d for d in documents(next_path) if component(d) != GC_COMPONENT}
    problems = []
    for k in sorted(set(live) | set(nxt)):
        if k not in nxt:
            problems.append(f"{k} would be REMOVED")
        elif k not in live:
            problems.append(f"{k} would be ADDED")
        elif normalized(live[k]) != normalized(nxt[k]):
            problems.append(f"{k} would CHANGE")
    if problems:
        print("; ".join(problems))
        return 1
    return 0


def ready(pod):
    for condition in (pod.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def pods(mode, deployment_path, pods_path):
    with open(deployment_path, encoding="utf-8") as handle:
        deployment = json.load(handle)
    with open(pods_path, encoding="utf-8") as handle:
        listed = (json.load(handle) or {}).get("items") or []
    template = ((deployment.get("spec") or {}).get("template") or {}).get("metadata") or {}
    annotations = template.get("annotations") or {}
    want = annotations.get(CHECKSUM)
    side = annotations.get(MAINTENANCE_ANNOTATION) or "read-write"
    if not want:
        print(f"the Deployment {DEPLOYMENT} carries no {CHECKSUM} on its pod template — a pod's configuration cannot be told")
        return 1
    if side != mode:
        print(f"the Deployment's pod template is {side}, not {mode} — the apply did not reach it")
        return 1
    meta = deployment.get("metadata") or {}
    status = deployment.get("status") or {}
    if status.get("observedGeneration") != meta.get("generation"):
        print(f"the Deployment's controller has not observed generation {meta.get('generation')} yet "
              f"(observed {status.get('observedGeneration')})")
        return 1
    replicas = (deployment.get("spec") or {}).get("replicas")
    replicas = 1 if replicas is None else replicas
    live = [p for p in listed if ((p.get("status") or {}).get("phase")) not in ("Succeeded", "Failed")]
    behind = []
    for pod in live:
        name = (pod.get("metadata") or {}).get("name") or "(unnamed)"
        pod_annotations = (pod.get("metadata") or {}).get("annotations") or {}
        if (pod.get("metadata") or {}).get("deletionTimestamp"):
            behind.append(f"{name} is terminating and still serving")
        elif pod_annotations.get(CHECKSUM) != want:
            behind.append(f"{name} was started from another configuration")
        elif (pod_annotations.get(MAINTENANCE_ANNOTATION) or "read-write") != mode:
            behind.append(f"{name} is not {mode}")
        elif not ready(pod):
            behind.append(f"{name} is not Ready")
    if behind:
        print("; ".join(behind))
        return 1
    if len(live) < max(replicas, 1):
        print(f"{len(live)} live registry pod(s) for {replicas} desired — a pod that is not listed cannot be called {mode}")
        return 1
    print(f"ok {len(live)}")
    return 0


def main(argv):
    if len(argv) == 4 and argv[1] == "scope":
        return scope(argv[2], argv[3])
    if len(argv) == 5 and argv[1] == "pods" and argv[2] in ("read-only", "read-write"):
        return pods(argv[2], argv[3], argv[4])
    print("usage: _registry_window.py scope <live> <next> | pods <read-only|read-write> <deployment.json> <pods.json>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
