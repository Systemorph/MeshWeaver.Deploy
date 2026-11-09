#!/usr/bin/env python3
"""Assert the boot-page wiring in a rendered chart (deploy/aks/scripts/check-chart-invariants.sh).

  check-boot-page.py <case> <rendered.yaml>
  case = enabled | disabled | override

enabled  — the ConfigMap, Deployment and Service exist, the portal Ingress names the boot page as its
           default backend, and the nginx config answers 503 with Retry-After and no-store and no
           body-size limit (an upload the Ingress admits must get the retryable 503, never a 413).
           The page renders the portal's own /_boot/progress: the config proxies it (resolved per
           request) to `memex-portal-boot`, a Service over the portal pods that publishes NOT-ready
           addresses, the pod mounts the config as an entrypoint template with local resolvers on,
           and the page's checksum covers what is mounted.
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
        conf, html = cm["data"].get("default.conf.template", ""), cm["data"].get("boot.html", "")
        for needle in ("return 503;", 'Retry-After "5"', 'Cache-Control "no-store"', "client_max_body_size 0;",
                       "location = /_boot/progress", "resolver ${NGINX_LOCAL_RESOLVERS}",
                       "http://memex-portal-boot.", "error_page 502 503 504 = @unreachable;"):
            if needle not in conf:
                errors.append(f"default.conf.template lacks `{needle}`")
        for needle in ('http-equiv="refresh"', 'class="spin"', 'fetch("/_boot/progress"'):
            if needle not in html:
                errors.append(f"boot.html lacks `{needle}`")
        dep = next((d for d in boot if d["kind"] == "Deployment"), None)
        if dep:
            c = dep["spec"]["template"]["spec"]["containers"][0]
            mounts = {m["mountPath"]: m.get("subPath") for m in c.get("volumeMounts", [])}
            if mounts.get("/etc/nginx/templates/default.conf.template") != "default.conf.template":
                errors.append(f"the nginx config is not mounted as the entrypoint template: {mounts}")
            if {"name": "NGINX_ENTRYPOINT_LOCAL_RESOLVERS", "value": "1"} not in (c.get("env") or []):
                errors.append("NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 is not set, so ${NGINX_LOCAL_RESOLVERS} stays empty")
        nr = next((d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "memex-portal-boot"), None)
        portal = next((d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "memex-portal-service"), None)
        if not nr:
            errors.append("no memex-portal-boot Service — the boot page has no way to the booting portal")
        else:
            if nr["spec"].get("publishNotReadyAddresses") is not True:
                errors.append("memex-portal-boot does not publish not-ready addresses — it would be as empty as the portal Service while booting")
            if portal and nr["spec"]["selector"] != portal["spec"]["selector"]:
                errors.append(f"memex-portal-boot selects {nr['spec']['selector']}, the portal Service {portal['spec']['selector']}")
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
