#!/usr/bin/env bash
#
# The fleet registry's ingress must let a layer upload run at line rate
# (templates/registry/ingress.yaml). Checked from the render alone, with no cluster:
#
#   1. The registry ingress renders exactly once, with request buffering OFF and no body limit.
#      These two are what make the receive window this check is about the one that applies.
#   2. client-body-buffer-size is present, and at least 4 MiB. Over HTTP/2 with buffering off,
#      nginx grants each upload stream at most max(client_body_buffer_size, 64 KiB) in flight, so
#      without it every layer is held to 64 KiB per round trip. That is about 0.5 MB/s from a CI
#      runner, and a 935 MiB layer cannot reach cr inside core CD's 25-minute mirror.
#   3. NEGATIVE CONTROL: the same check, run on a render with the annotation removed, must report
#      it missing. A check that cannot fail is not a check.
#
# NO SKIP-TRAPDOOR: helm, python3 and PyYAML are asserted; a render that yields nothing is RED.
set -uo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SELF_DIR/../../.." && pwd)"
CHART="$REPO/deploy/helm"
EXAMPLE="$CHART/values.registry.example.yaml"
MIN_BYTES=$((4 * 1024 * 1024))

command -v helm >/dev/null 2>&1 || { echo "::error::helm is not on PATH"; exit 1; }
python3 -c "import yaml" >/dev/null 2>&1 || { echo "::error::python3 with PyYAML is required (pip install pyyaml)"; exit 1; }
[ -f "$EXAMPLE" ] || { echo "::error::$EXAMPLE is missing — nothing to render"; exit 1; }

# Prints nothing when the registry ingress satisfies 1 and 2, otherwise one line per problem.
CHECK='
import os, re, sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if isinstance(d, dict)]
ing = [d for d in docs if d.get("kind") == "Ingress" and (d.get("metadata") or {}).get("name") == "memex-registry"]
if len(ing) != 1:
    print(f"expected exactly one memex-registry Ingress, rendered {len(ing)}"); sys.exit(0)
a = (ing[0]["metadata"].get("annotations") or {})
p = "nginx.ingress.kubernetes.io/"
if a.get(p + "proxy-request-buffering") != "off": print("proxy-request-buffering is not \"off\"")
if a.get(p + "proxy-body-size") != "0": print("proxy-body-size is not \"0\"")
v = a.get(p + "client-body-buffer-size")
if v is None:
    print("client-body-buffer-size is missing: every HTTP/2 layer upload is held to 64 KiB per round trip")
else:
    m = re.fullmatch(r"(\d+)([kKmM]?)", str(v))
    if not m:
        print(f"client-body-buffer-size {v!r} is not an nginx size")
    else:
        n = int(m.group(1)) * {"": 1, "k": 1024, "m": 1024 * 1024}[m.group(2).lower()]
        floor = int(os.environ["MIN_BYTES"])
        if n < floor:
            print(f"client-body-buffer-size {v!r} is {n} bytes, below the {floor} a layer upload needs")
'

out="$(helm template t "$CHART" -f "$EXAMPLE" 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ -n "$out" ] || { echo "::error::the registry example does not render: $out"; exit 1; }

fail=0
verdict="$(printf '%s' "$out" | MIN_BYTES=$MIN_BYTES python3 -c "$CHECK")" || { echo "::error::the inspection itself failed, so there is no evidence"; exit 1; }
if [ -z "$verdict" ]; then
  echo "✅ the registry ingress streams uploads unbuffered with a receive buffer of at least $MIN_BYTES bytes"
else
  while IFS= read -r line; do echo "::error::registry ingress: $line"; done <<< "$verdict"
  fail=1
fi

# Negative control: drop the annotation from the same render and the check must catch it.
stripped="$(printf '%s\n' "$out" | grep -v 'nginx.ingress.kubernetes.io/client-body-buffer-size')"
control="$(printf '%s' "$stripped" | MIN_BYTES=$MIN_BYTES python3 -c "$CHECK")" || { echo "::error::the negative control's inspection failed"; exit 1; }
if printf '%s' "$control" | grep -q "client-body-buffer-size is missing"; then
  echo "✅ negative control: a render without the annotation is reported"
else
  echo "::error::negative control: removing client-body-buffer-size was NOT reported (got: ${control:-<nothing>}) — this check cannot fail"
  fail=1
fi

exit $fail
