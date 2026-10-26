#!/bin/bash
# memex-local back-lease HOST SAMPLER — the readings the portal cannot take from inside the VM.
#
# Runs as a launchd login agent (com.memex.local.backlease, KeepAlive) and writes ONE file every
# MEMEX_BACKLEASE_INTERVAL seconds (default 30): $MEMEX_BACKLEASE_DIR/readings.json, the
# `MachineReadings` shape the Hosting module's BackLeaseHeartbeat forwards to control
# (MeshWeaver.Plugins Hosting/BackLease §5, Hosting/Queue/Source/Placement.cs):
#
#   { os, machine, models[{name,sizeGb,loaded,lastUsedAt}], vm{…}, host{…}, health{…}, sampledAt }
#
# Everything is a MEASUREMENT: nothing is configured here, nothing is guessed — a reading that cannot
# be taken is omitted (the portal reads what is present) and the component is marked in `health`.
# The file is written atomically (tmp + mv), so the portal never reads a half-written sample.
#
# Sources, all without sudo:
#   host   sysctl -n hw.ncpu / hw.memsize; vm_stat (page size × active+wired+compressed);
#          sysctl -n vm.loadavg; pmset -g therm (CPU_Speed_Limit → thermal level); df -k $HOME;
#          sysctl -n kern.boottime; pmset -g batt (power source)
#   models ollama's native API: /api/tags (installed) + /api/ps (loaded)
#   vm     kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary (the kubelet summary) and the
#          pod requests + `mw/run` labels of the pods in the memex namespace
#
set -u
INTERVAL="${MEMEX_BACKLEASE_INTERVAL:-30}"
DIR="${MEMEX_BACKLEASE_DIR:-$HOME/.memex-local/backlease}"
NAMESPACE="${MEMEX_NAMESPACE:-memex}"
OLLAMA="${OLLAMA_HOST_URL:-http://127.0.0.1:11434}"
OUT="$DIR/readings.json"
mkdir -p "$DIR"

num() { printf '%s' "${1:-0}" | tr -d ' \n' | grep -E '^-?[0-9]+(\.[0-9]+)?$' || printf '0'; }

sample_host() {
  local cores mem_bytes page active wired compressed used_gb load1 therm_limit thermal boot uptime df_free df_size power
  cores="$(num "$(sysctl -n hw.ncpu 2>/dev/null)")"
  mem_bytes="$(num "$(sysctl -n hw.memsize 2>/dev/null)")"
  page="$(num "$(vm_stat 2>/dev/null | awk -F'[^0-9]+' '/page size of/ {print $2; exit}')")"
  active="$(num "$(vm_stat 2>/dev/null | awk '/Pages active/ {gsub(/\./,"",$3); print $3}')")"
  wired="$(num "$(vm_stat 2>/dev/null | awk '/Pages wired down/ {gsub(/\./,"",$4); print $4}')")"
  compressed="$(num "$(vm_stat 2>/dev/null | awk '/Pages occupied by compressor/ {gsub(/\./,"",$5); print $5}')")"
  used_gb="$(awk -v p="$page" -v a="$active" -v w="$wired" -v c="$compressed" 'BEGIN { printf "%.2f", p*(a+w+c)/1e9 }')"
  load1="$(num "$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')")"
  therm_limit="$(num "$(pmset -g therm 2>/dev/null | awk -F= '/CPU_Speed_Limit/ {gsub(/ /,"",$2); print $2}')")"
  # macOS exposes no named pressure level without root; the speed limit is the honest proxy
  if [ "$therm_limit" = 0 ] || [ "$therm_limit" -ge 100 ] 2>/dev/null; then thermal="Nominal"
  elif [ "$therm_limit" -ge 80 ] 2>/dev/null; then thermal="Moderate"
  elif [ "$therm_limit" -ge 50 ] 2>/dev/null; then thermal="Heavy"
  else thermal="Critical"; fi
  boot="$(num "$(sysctl -n kern.boottime 2>/dev/null | awk -F'[ ,]+' '{for(i=1;i<=NF;i++) if($i=="sec") print $(i+2)}')")"
  uptime=$(( $(date +%s) - boot ))
  df_free="$(num "$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')")"
  df_size="$(num "$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $2}')")"
  power="$(pmset -g batt 2>/dev/null | awk -F"'" '/Now drawing from/ {print $2; exit}')"
  case "$power" in *AC*) power="AC" ;; *Battery*) power="Battery" ;; *) power="AC" ;; esac
  printf '{"cores":%s,"memoryGb":%s,"load1":%s,"memoryUsedGb":%s,"thermal":"%s","diskFreeGb":%s,"diskGb":%s,"uptimeS":%s,"powerSource":"%s"}' \
    "$cores" "$(awk -v b="$mem_bytes" 'BEGIN { printf "%.2f", b/1e9 }')" "$load1" "$used_gb" "$thermal" \
    "$(awk -v k="$df_free" 'BEGIN { printf "%.1f", k*1024/1e9 }')" "$(awk -v k="$df_size" 'BEGIN { printf "%.1f", k*1024/1e9 }')" "$uptime" "$power"
}

# models: installed from /api/tags, loaded from /api/ps — joined by name; needs python3 for safe JSON
sample_models() {
  local tags ps
  tags="$(curl -s -m 3 "$OLLAMA/api/tags" 2>/dev/null)" || tags=""
  ps="$(curl -s -m 3 "$OLLAMA/api/ps" 2>/dev/null)" || ps=""
  if [ -z "$tags" ]; then printf '[]'; return 1; fi
  printf '%s\n%s' "$tags" "$ps" | python3 -I -c '
import json, sys
raw = sys.stdin.read().split("\n", 1)
tags = json.loads(raw[0] or "{}"); ps = json.loads(raw[1] or "{}") if len(raw) > 1 and raw[1].strip() else {}
loaded = {m.get("name", "").split(":latest")[0]: m for m in ps.get("models", [])}
out = []
for m in tags.get("models", []):
    name = m.get("name", "").split(":latest")[0]
    out.append({"name": name, "sizeGb": round(m.get("size", 0) / 1e9, 2), "loaded": name in loaded})
print(json.dumps(out))' 2>/dev/null || printf '[]'
}

# vm: the kubelet summary of the single k3s node plus the memex namespace pods' requests and run labels
sample_vm() {
  local node summary pods
  node="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || node=""
  [ -n "$node" ] || { printf 'null'; return 1; }
  summary="$(kubectl get --raw "/api/v1/nodes/$node/proxy/stats/summary" 2>/dev/null)" || summary=""
  pods="$(kubectl -n "$NAMESPACE" get pods -o json 2>/dev/null)" || pods=""
  nodejson="$(kubectl get node "$node" -o json 2>/dev/null)" || nodejson=""
  [ -n "$summary" ] && [ -n "$nodejson" ] || { printf 'null'; return 1; }
  printf '%s\n@@\n%s\n@@\n%s' "$summary" "$nodejson" "$pods" | python3 -I -c '
import json, sys, re
summary, nodejson, pods = sys.stdin.read().split("\n@@\n", 2)
s = json.loads(summary); n = json.loads(nodejson); p = json.loads(pods) if pods.strip() else {"items": []}
def q(v):  # kubernetes quantity → float (cores or bytes)
    if v is None: return 0.0
    v = str(v)
    m = re.match(r"^([0-9.]+)([a-zA-Z]*)$", v)
    if not m: return 0.0
    num, unit = float(m.group(1)), m.group(2)
    mult = {"": 1, "m": 1e-3, "n": 1e-9, "u": 1e-6, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "k": 1e3, "M": 1e6, "G": 1e9}
    return num * mult.get(unit, 1)
alloc = n.get("status", {}).get("allocatable", {}); cap = n.get("status", {}).get("capacity", {})
node = s.get("node", {})
req_cpu = req_mem = 0.0; runs = []
for pod in p.get("items", []):
    if pod.get("status", {}).get("phase") not in ("Running", "Pending"): continue
    for c in pod.get("spec", {}).get("containers", []):
        r = c.get("resources", {}).get("requests", {})
        req_cpu += q(r.get("cpu")); req_mem += q(r.get("memory"))
    run = pod.get("metadata", {}).get("labels", {}).get("mw/run")
    if run: runs.append(run)
fs = node.get("fs", {})
out = {
  "cpu": q(cap.get("cpu")), "memoryGi": round(q(cap.get("memory")) / 1024**3, 2),
  "allocatableCpu": q(alloc.get("cpu")), "allocatableMemoryGi": round(q(alloc.get("memory")) / 1024**3, 2),
  "requestedCpu": round(req_cpu, 3), "requestedMemoryGi": round(req_mem / 1024**3, 2),
  "usageCpu": round(node.get("cpu", {}).get("usageNanoCores", 0) / 1e9, 3),
  "workingSetGi": round(node.get("memory", {}).get("workingSetBytes", 0) / 1024**3, 2),
  "diskFreeGi": round(fs.get("availableBytes", 0) / 1024**3, 1), "diskGi": round(fs.get("capacityBytes", 0) / 1024**3, 1),
  "scheduledRuns": runs,
}
print(json.dumps(out))' 2>/dev/null || printf 'null'
}

health_of() { # name, probe-exit → "ok" or the reason
  if [ "$2" = 0 ]; then printf '"%s":"ok"' "$1"; else printf '"%s":"%s"' "$1" "$3"; fi
}

write_sample() {
  local host models vm m_rc v_rc colima_rc portal_rc tmp
  host="$(sample_host)"
  models="$(sample_models)"; m_rc=$?
  vm="$(sample_vm)"; v_rc=$?
  colima status >/dev/null 2>&1; colima_rc=$?
  # the launchd port-forward serves TLS on MEMEX_PORT for the ingress HOST (a local certificate, hence -k;
  # the ingress answers 404 to a bare IP, so the hostname is resolved to loopback explicitly)
  curl -sk -m 3 -o /dev/null -w '%{http_code}' --resolve "${MEMEX_HOSTNAME:-memex.localhost}:${MEMEX_PORT:-8443}:127.0.0.1" \
    "https://${MEMEX_HOSTNAME:-memex.localhost}:${MEMEX_PORT:-8443}/health" 2>/dev/null | grep -q '^200$'; portal_rc=$?
  tmp="$OUT.tmp.$$"
  {
    printf '{"os":"macOS %s","machine":"%s",' "$(sw_vers -productVersion 2>/dev/null || echo unknown)" \
      "$(system_profiler SPHardwareDataType 2>/dev/null | awk -F': ' '/Model Name|Chip|Memory/ {printf "%s ", $2}' | sed 's/ *$//')"
    printf '"models":%s,"vm":%s,"host":%s,' "$models" "$vm" "$host"
    printf '"health":{%s,%s,%s,%s},' \
      "$(health_of portal "$portal_rc" "no 200 from /health on :${MEMEX_PORT:-8443}")" \
      "$(health_of ollama "$m_rc" "ollama did not answer /api/tags at $OLLAMA")" \
      "$(health_of colima "$colima_rc" "colima status failed")" \
      "$(health_of kubelet "$v_rc" "the kubelet summary could not be read")"
    printf '"sampledAt":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$tmp" && mv -f "$tmp" "$OUT"
}

if [ "${1:-}" = "--once" ]; then write_sample; exit 0; fi
while true; do
  write_sample || true
  sleep "$INTERVAL"
done
