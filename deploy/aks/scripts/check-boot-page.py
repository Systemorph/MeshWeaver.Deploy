#!/usr/bin/env python3
"""Assert the boot-page wiring in a rendered chart (deploy/aks/scripts/check-chart-invariants.sh).

  check-boot-page.py <case> <rendered.yaml>
  case = enabled | disabled | override

enabled  — the ConfigMap, Deployment and Service exist, the portal Ingress names the boot page as its
           default backend, and the nginx config answers 503 with Retry-After and no-store and no
           body-size limit (an upload the Ingress admits must get the retryable 503, never a 413).
disabled — nothing of the boot page renders and no Ingress names it.
override — a hand-written default-backend in ingress.annotations wins over the boot page.
"""
import sys
import yaml

KEY = "nginx.ingress.kubernetes.io/default-backend"


def main(case, path):
    docs = [d for d in yaml.safe_load_all(open(path)) if d]
    boot = [d for d in docs if d.get("metadata", {}).get("name") == "memex-boot-page"]
    kinds = sorted(d["kind"] for d in boot)
    ingresses = [d for d in docs if d["kind"] == "Ingress"]
    backends = {i["metadata"]["name"]: (i["metadata"].get("annotations") or {}).get(KEY) for i in ingresses}
    errors = []
    if case == "enabled":
        if kinds != ["ConfigMap", "Deployment", "Service"]:
            errors.append(f"boot page objects are {kinds}, want ConfigMap, Deployment, Service")
        if "memex-boot-page" not in backends.values():
            errors.append(f"no Ingress names memex-boot-page as its default backend: {backends}")
        cm = next((d for d in boot if d["kind"] == "ConfigMap"), {"data": {}})
        conf, html = cm["data"].get("default.conf", ""), cm["data"].get("boot.html", "")
        for needle in ("return 503;", 'Retry-After "5"', 'Cache-Control "no-store"', "client_max_body_size 0;"):
            if needle not in conf:
                errors.append(f"default.conf lacks `{needle}`")
        for needle in ('http-equiv="refresh"', 'class="spin"'):
            if needle not in html:
                errors.append(f"boot.html lacks `{needle}`")
        svc = next((d for d in boot if d["kind"] == "Service"), None)
        if svc and not any(p.get("port") == 8080 for p in svc["spec"]["ports"]):
            errors.append("the boot page Service does not expose port 8080")
    elif case == "disabled":
        if boot:
            errors.append(f"boot page disabled, yet it rendered {kinds}")
        if "memex-boot-page" in backends.values():
            errors.append(f"boot page disabled, yet an Ingress names it: {backends}")
    elif case == "override":
        if "memex-boot-page" in backends.values() or "custom-backend" not in backends.values():
            errors.append(f"a hand-written default-backend must win: {backends}")
    else:
        errors.append(f"unknown case {case}")
    for e in errors:
        print(f"::error::boot page ({case}): {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
