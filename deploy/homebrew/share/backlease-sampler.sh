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
#          ownerCpuPercent — the OWNER's CPU over the interval (see owner_cpu below);
#          cpuPercent — the host's CPU busy, % of all cores (top, see host_cpu below);
#          netIn/OutBytesPerSec — physical-interface traffic over the interval (netstat -ibn, see net_rate below);
#          sysctl -n vm.loadavg; pmset -g therm (CPU_Speed_Limit → thermal level); df -k $HOME;
#          sysctl -n kern.boottime; pmset -g batt (power source)
#   models ollama's native API: /api/tags (installed) + /api/ps (loaded)
#   vm     kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary (the kubelet summary) and the
#          pod requests + `mw/run` labels of the pods in the memex namespace
#
set -u
# Run the kubectl/ollama the CLI's resolver chose (share/tools.sh; MEMEX_LOCAL_SHARE from the plist's __SHARE__).
if [ -f "${MEMEX_LOCAL_SHARE:-}/tools.sh" ]; then
  # shellcheck source=tools.sh
  . "${MEMEX_LOCAL_SHARE}/tools.sh"
  tools_activate
  trap tools_deactivate EXIT
fi
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

# host.ownerCpuPercent — the CPU the OWNER is using, in percent of ONE core (200 = two cores busy), averaged over the
# interval since the previous sample (MeshWeaver.Plugins Hosting/BackLease §14.5: an idle Mac takes the fleet's work
# up to its slots and yields fast once its owner is active). Every process EXCEPT the fleet's own: Ollama (serve, its
# runners, llama-server), the build VM (Colima profile `build`) and the portal's VM (profile `default`) — each VM
# found as the process holding its profile's boot disk open, never by name — and kernel_task. It is the build
# governor's "others" (share/buildhost-priority.sh, whose sample_cpu and vm_pids it reuses, sourced in a subshell)
# minus Ollama and the portal VM. It is a delta of `ps` CPU time, never an instant snapshot. Prints nothing — and the
# field is ABSENT — when it cannot be measured: no governor library, the first sample, or a gap longer than three
# intervals (a sampler that slept would report an average of a stale window).
GOVERNOR="${MEMEX_BUILD_PRIO_SCRIPT:-}"
[ -n "$GOVERNOR" ] || { [ -f "${MEMEX_LOCAL_SHARE:-}/buildhost-priority.sh" ] && GOVERNOR="${MEMEX_LOCAL_SHARE}/buildhost-priority.sh"; }
[ -n "$GOVERNOR" ] || GOVERNOR="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}/buildhost-priority.sh"
owner_cpu() {
  local prev="$DIR/owner-cpu.prev" cur="$DIR/owner-cpu.cur" stamp="$DIR/owner-cpu.at" now t0 dt reading had_prev=no
  [ -f "$GOVERNOR" ] || return 0
  now="${MEMEX_SAMPLER_NOW:-$(date +%s)}"
  t0="$(cat "$stamp" 2>/dev/null || true)"
  [ -f "$prev" ] && [ -n "$t0" ] && had_prev=yes
  reading="$(${MEMEX_SAMPLER_PS:-ps -A -o pid=,time=,command=} 2>/dev/null | (
      # shellcheck source=buildhost-priority.sh
      MEMEX_BUILD_PRIO_LIB=1 . "$GOVERNOR" >/dev/null 2>&1 || exit 1
      # vm_pids reads the governor's PROFILE: the build VM, then the portal's VM
      excl="$(PROFILE=build vm_pids) $(PROFILE=default vm_pids)"
      sample_cpu "$([ "$had_prev" = yes ] && printf '%s' "$prev" || printf '/dev/null')" "$cur" "$excl") 2>/dev/null)" || return 0
  # a baseline that cannot be rotated is NO reading — the next sample starts over (never a stale baseline divided
  # by one interval, which would report about twice the owner's CPU)
  if ! mv -f "$cur" "$prev" 2>/dev/null || ! printf '%s' "$now" > "$stamp" 2>/dev/null; then
    rm -f "$prev" "$stamp" 2>/dev/null; return 0
  fi
  [ "$had_prev" = yes ] || return 0
  case "$t0" in ''|*[!0-9]*) return 0 ;; esac
  dt=$(( now - t0 ))
  [ "$dt" -ge 1 ] && [ "$dt" -le $(( INTERVAL * 3 )) ] || return 0
  case "$reading" in *[0-9]' '*[0-9]) ;; *) return 0 ;; esac
  # centiseconds of CPU per elapsed second = percent of one core
  printf '%s' $(( ( ${reading% *} - ${reading#* } ) / dt ))
}

# host.cpuPercent — the host's CPU busy, in percent of ALL cores (0–100): 100 minus the idle share of a one-second `top`
# sample (the second of two, so it is a delta, never the since-boot average). Prints nothing — the field is ABSENT —
# when `top` gives no parseable line. Recorded into the fleet's metric history (MeshWeaver.Plugins Hosting/FleetMetricHistory).
host_cpu() {
  ${MEMEX_SAMPLER_TOP:-top -l 2 -n 0 -s 1} 2>/dev/null | awk '/^CPU usage:/ {
      for (i = 1; i <= NF; i++) if ($(i+1) ~ /^idle/) { v = $i; gsub(/%/, "", v); idle = v } }
    END { if (idle != "" && idle + 0 >= 0 && idle + 0 <= 100) printf "%.1f", 100 - idle }'
}

# host.netInBytesPerSec / host.netOutBytesPerSec — bytes received and sent per second over the host's PHYSICAL
# interfaces (en*, the link-level rows of `netstat -ibn`: Ibytes and Obytes), averaged over the interval since the
# previous sample — what lets a home-network burst be lined up with what the machine was doing. Prints "in out", or
# nothing (both fields ABSENT) on the first sample, after a gap longer than three intervals, or when a counter went
# backwards (a reboot or an interface reset): a stale or negative window is not a reading.
net_rate() {
  local prev="$DIR/net.prev" now cur t0 in0 out0 dt
  now="${MEMEX_SAMPLER_NOW:-$(date +%s)}"
  cur="$(${MEMEX_SAMPLER_NETSTAT:-netstat -ibn} 2>/dev/null | awk '$1 ~ /^en[0-9]+$/ && $3 ~ /^<Link#/ && NF >= 11 { i += $7; o += $10; n++ }
    END { if (n > 0) printf "%.0f %.0f", i, o }')"
  [ -n "$cur" ] || { rm -f "$prev"; return 0; }
  t0=""; [ -f "$prev" ] && read -r t0 in0 out0 < "$prev"
  printf '%s %s\n' "$now" "$cur" > "$prev" 2>/dev/null || { rm -f "$prev"; return 0; }
  case "$t0" in ''|*[!0-9]*) return 0 ;; esac
  dt=$(( now - t0 ))
  [ "$dt" -ge 1 ] && [ "$dt" -le $(( INTERVAL * 3 )) ] || return 0
  awk -v a="${cur% *}" -v b="${cur#* }" -v c="$in0" -v d="$out0" -v t="$dt" \
    'BEGIN { if (a < c || b < d) exit; printf "%.0f %.0f", (a - c) / t, (b - d) / t }'
}

health_of() { # name, probe-exit → "ok" or the reason
  if [ "$2" = 0 ]; then printf '"%s":"ok"' "$1"; else printf '"%s":"%s"' "$1" "$3"; fi
}

write_sample() {
  local host models vm m_rc v_rc colima_rc portal_rc tmp
  host="$(sample_host)"
  local owner; owner="$(owner_cpu)"
  # spliced only into a JSON OBJECT: a degraded host reading (anything not ending in `}`) is written as it was
  case "$owner" in ''|*[!0-9]*) ;; *) case "$host" in '{'*'}') host="${host%\}},\"ownerCpuPercent\":$owner}" ;; esac ;; esac
  local cpu; cpu="$(host_cpu)"
  case "$cpu" in ''|*[!0-9.]*) ;; *) case "$host" in '{'*'}') host="${host%\}},\"cpuPercent\":$cpu}" ;; esac ;; esac
  local net; net="$(net_rate)"
  case "$net" in [0-9]*' '[0-9]*) case "$host" in '{'*'}') host="${host%\}},\"netInBytesPerSec\":${net% *},\"netOutBytesPerSec\":${net#* }}" ;; esac ;; esac
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

# library mode (tests source this file): fails CLOSED — executed with the variable set, it stops loudly instead of
# falling through into the sampling loop
if [ "${MEMEX_BACKLEASE_SAMPLER_LIB:-}" = 1 ]; then
  (return 0 2>/dev/null) && return 0
  echo "backlease-sampler.sh: MEMEX_BACKLEASE_SAMPLER_LIB=1 is for sourcing only — not sampling" >&2; exit 2
fi
if [ "${1:-}" = "--once" ]; then write_sample; exit 0; fi
while true; do
  write_sample || true
  sleep "$INTERVAL"
done
