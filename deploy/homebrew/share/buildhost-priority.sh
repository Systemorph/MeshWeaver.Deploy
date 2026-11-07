#!/bin/bash
# memex-local BUILD-HOST PRIORITY GOVERNOR — fleet jobs run at the LOWEST priority on a leased Mac.
#
# A Mac that leases capacity to the fleet runs the fleet's build jobs in its own Colima profile (`build`,
# a Virtualization.framework VM). The owner's own work, the local memex portal (the `default` profile's VM)
# and Ollama must always win when the machine is contended, and the builds must still use every core
# that is idle ("fill the computer"). This agent does both, without sudo, by switching the build VM's
# process between two macOS task policies:
#
#   full   — the VM's default policy: its vCPUs use every idle core (measured on an 18-core M5 Max:
#            a 16-vCPU VM took 15.5 cores on an idle host)
#   yield  — PRIO_DARWIN_BG (`taskpolicy -b -p <pid>`): CPU at the lowest band and disk IO throttled.
#            Measured with 18 busy host processes competing: the VM got 0.22 cores (the host processes
#            16.8), against 7.7 cores (host 8.8) at its default policy
#
# It yields while anything ELSE wants the machine — everything but the build VM using more than
# MEMEX_BUILD_PRIO_YIELD_AT percent of one core (default 200 = two cores, the two the build plan keeps
# for macOS and Ollama), or an Ollama process computing (≥ MEMEX_BUILD_PRIO_OLLAMA_BUSY percent) — and
# returns to full once the rest has stayed under MEMEX_BUILD_PRIO_RESUME_BELOW for
# MEMEX_BUILD_PRIO_RESUME_AFTER seconds.
#
# WHY NOT nice / renice, a QoS clamp, or background permanently (all measured on the first leased Mac,
# macOS 27.2, 2026-11-07 — MeshWeaver.Plugins Hosting/MacStudioAttachRunbook §5):
#   • `renice -n 20` changes nothing. Eighteen busy loops at nice 20 against eighteen at nice 0 got the
#     same CPU (54.7 vs 55.2 core-seconds in 8 s); the throughput/latency tiers (`taskpolicy -t/-l`) the same.
#     The macOS scheduler ranks by QoS, not by nice.
#   • a QoS clamp (`taskpolicy -c utility`) is the right policy — it yields under contention AND fills an
#     idle host (16.6 of 18 cores) — but it applies only at spawn and is inherited only by CHILDREN: the
#     VM process is an XPC service launchd starts for limactl, so a clamp on `colima start` reaches the
#     hostagent (PRI 20) and not the VM (PRI 31). There is no call that clamps a RUNNING process.
#   • PRIO_DARWIN_BG permanently caps the VM near 5 cores on an idle 18-core host (a 16-vCPU VM: 4.7
#     cores) — it would cut idle build capacity by about 70 %. Hence the switch: background only while
#     something else wants the cores.
#
# Usage (memex-local runs these; first-attach.sh runs --install too):
#   buildhost-priority.sh --install [--profile build]   copy this script to ~/.memex-local, write and load
#                                                        the launchd agent com.memex.local.buildhost-priority
#   buildhost-priority.sh --uninstall                   unload it and give the VM its default policy back
#   buildhost-priority.sh --status                      the agent, the VM process, its mode and thread priorities
#   buildhost-priority.sh                               the governor loop (what the agent runs)
#
# Bash 3.2 clean (macOS's /bin/bash: no associative arrays). Sourceable for tests:
# MEMEX_BUILD_PRIO_LIB=1 . buildhost-priority.sh defines the functions without running anything.
set -u

LABEL="com.memex.local.buildhost-priority"
PROFILE="${MEMEX_BUILD_PROFILE:-build}"
LOCAL_HOME="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}"
INTERVAL="${MEMEX_BUILD_PRIO_INTERVAL:-2}"
YIELD_AT="${MEMEX_BUILD_PRIO_YIELD_AT:-200}"
RESUME_BELOW="${MEMEX_BUILD_PRIO_RESUME_BELOW:-150}"
RESUME_AFTER="${MEMEX_BUILD_PRIO_RESUME_AFTER:-15}"
OLLAMA_BUSY="${MEMEX_BUILD_PRIO_OLLAMA_BUSY:-10}"
STATE_FILE="${MEMEX_BUILD_PRIO_STATE:-$LOCAL_HOME/buildhost-priority.state}"
LOG_FILE="${MEMEX_BUILD_PRIO_LOG:-$LOCAL_HOME/buildhost-priority.log}"
LOG_MAX_BYTES=1048576

# Colima's Lima instance directory for a profile: `default` → colima, anything else → colima-<profile>.
instance_dir() {
  local p="$1" base="${COLIMA_HOME:-$HOME/.colima}/_lima"
  if [ "$p" = default ]; then printf '%s/colima' "$base"; else printf '%s/colima-%s' "$base" "$p"; fi
}

# The build VM's process: the one holding the profile's boot disk open — never a guess by name, since the
# portal's VM is the same Virtualization.framework executable. Empty when the VM is not running.
vm_pids() {
  local disk; disk="$(instance_dir "$PROFILE")/disk"
  [ -e "$disk" ] || return 0
  lsof -t "$disk" 2>/dev/null | sort -u | tr '\n' ' ' | sed 's/ $//'
}

# The decision, pure: mode (full|yield), the others' and Ollama's CPU (percent of one core), the seconds
# the others have been calm, the elapsed seconds. Prints "<mode> <calm seconds>".
decide() {
  local mode="$1" others="$2" ollama="$3" calm="$4" dt="$5"
  if [ "$others" -ge "$YIELD_AT" ] || [ "$ollama" -ge "$OLLAMA_BUSY" ]; then
    printf 'yield 0'
  elif [ "$mode" = yield ]; then
    if [ "$others" -lt "$RESUME_BELOW" ]; then
      calm=$(( calm + dt ))
      if [ "$calm" -ge "$RESUME_AFTER" ]; then printf 'full 0'; else printf 'yield %s' "$calm"; fi
    else
      printf 'yield 0'
    fi
  else
    printf 'full 0'
  fi
}

# One CPU sample: reads `ps -A -o pid=,time=,command=` on stdin and the previous sample (pid centiseconds)
# from $1; writes the new sample to $2 and prints "<others centiseconds> <ollama centiseconds>" consumed
# since the previous sample. Ollama is every process whose command line names it — `ollama serve`, a `runner`, and the
# `…/lib/ollama/llama-server` that does the inference since Ollama 0.40 (268 % CPU on a 0.5b model, measured). Excluded: the build VM's own processes ($3, space separated) and kernel_task
# (pid 0 — its time includes the VM's own IO, which would make the VM's work count as contention).
# A process absent from the previous sample counts from zero; the first sample (no previous file) is 0 0.
sample_cpu() {
  awk -v prevf="$1" -v outf="$2" -v excl="$3" '
    function cs(t,   n, p, s, i) { n = split(t, p, ":"); s = 0; for (i = 1; i < n; i++) s = s * 60 + p[i]; return int((s * 60 + p[n]) * 100 + 0.5) }
    BEGIN {
      first = 1
      while ((getline line < prevf) > 0) { split(line, f, " "); prev[f[1]] = f[2]; first = 0 }
      close(prevf)
      ne = split(excl, e, " "); for (i = 1; i <= ne; i++) skip[e[i]] = 1
      printf "" > outf
    }
    {
      pid = $1; t = cs($2); print pid, t >> outf
      if (pid == 0 || (pid in skip)) next
      d = t - ((pid in prev) ? prev[pid] : 0); if (d < 0) d = 0
      if (first) d = 0
      others += d
      cmd = $0; sub(/^[ \t]*[0-9]+[ \t]+[0-9:.]+[ \t]+/, "", cmd)
      if (tolower(cmd) ~ /ollama/) ollama += d
    }
    END { printf "%d %d\n", others, ollama }'
}

now_s() { date +%s; }
log() {
  if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then : > "$LOG_FILE"; fi
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# Apply a mode to every VM process. Returns non-zero when taskpolicy refused one.
apply_mode() {
  local mode="$1" pids="$2" p rc=0 flag=-B
  [ "$mode" = yield ] && flag=-b
  for p in $pids; do taskpolicy "$flag" -p "$p" >/dev/null 2>&1 || rc=1; done
  return "$rc"
}

write_state() {
  mkdir -p "$(dirname "$STATE_FILE")"
  printf 'mode=%s\nsince=%s\nvm_pids=%s\nothers_pct=%s\nollama_pct=%s\nupdated=%s\nprofile=%s\n' \
    "$1" "$2" "$3" "$4" "$5" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PROFILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
}

run() {
  local mode=full calm=0 pids="" applied_pids="" since prev cur t0 t1 dt reading others ollama out newmode
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  prev="$(mktemp)"; cur="$(mktemp)"
  # On stop (launchctl bootout, uninstall, logout) the VM gets its default policy back: a governor that is
  # gone must never leave the builds capped at background.
  trap 'apply_mode full "$applied_pids"; write_state stopped "$since" "$applied_pids" 0 0; rm -f "$prev" "$cur"; log "stopped: build VM back at full"; exit 0' TERM INT HUP
  log "started: profile $PROFILE, yield at ${YIELD_AT}% (others) or ${OLLAMA_BUSY}% (ollama), resume below ${RESUME_BELOW}% for ${RESUME_AFTER}s, every ${INTERVAL}s"
  t0="$(now_s)"
  ps -A -o pid=,time=,command= 2>/dev/null | sample_cpu /dev/null "$prev" "" >/dev/null
  while :; do
    sleep "$INTERVAL"
    pids="$(vm_pids)"
    t1="$(now_s)"; dt=$(( t1 - t0 )); [ "$dt" -ge 1 ] || dt=1; t0="$t1"
    reading="$(ps -A -o pid=,time=,command= 2>/dev/null | sample_cpu "$prev" "$cur" "$pids")"
    mv "$cur" "$prev"; cur="$(mktemp)"
    others=$(( ${reading% *} / dt )); ollama=$(( ${reading#* } / dt ))
    out="$(decide "$mode" "$others" "$ollama" "$calm" "$dt")"
    newmode="${out% *}"; calm="${out#* }"
    if [ -z "$pids" ]; then
      [ -z "$applied_pids" ] || log "build VM ($PROFILE) not running"
      applied_pids=""; mode="$newmode"
      write_state "$mode" "$since" "" "$others" "$ollama"
      continue
    fi
    if [ "$newmode" != "$mode" ] || [ "$pids" != "$applied_pids" ]; then
      if apply_mode "$newmode" "$pids"; then
        [ "$pids" != "$applied_pids" ] && log "build VM process $pids (profile $PROFILE)"
        [ "$newmode" != "$mode" ] && { since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; log "$newmode: others ${others}%, ollama ${ollama}%"; }
        mode="$newmode"; applied_pids="$pids"
      else
        log "ERROR: taskpolicy refused to set $newmode on $pids — retrying next tick"
        applied_pids=""
      fi
    fi
    write_state "$mode" "$since" "$applied_pids" "$others" "$ollama"
  done
}

plist() {
  local script="$1"
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- memex-local build-host priority governor: fleet jobs run at the lowest priority on this Mac.
     Written by buildhost-priority.sh --install (memex-local buildhost apply, first-attach.sh); removed by --uninstall. -->
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>${script}</string></array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>MEMEX_BUILD_PROFILE</key><string>${PROFILE}</string>
        <key>MEMEX_LOCAL_HOME</key><string>${LOCAL_HOME}</string>
    </dict>
    <!-- Interactive: the governor itself must run promptly when the machine is contended — a launchd agent's
         default policy ranks it BELOW the build VM it has to demote. It sleeps between ticks. -->
    <key>ProcessType</key><string>Interactive</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${LOG_FILE}</string>
    <key>StandardErrorPath</key><string>${LOG_FILE}</string>
</dict>
</plist>
PLIST
}

install_agent() {
  local self dst plist_dst
  self="${BASH_SOURCE[0]}"; dst="$LOCAL_HOME/buildhost-priority.sh"
  plist_dst="$HOME/Library/LaunchAgents/${LABEL}.plist"
  mkdir -p "$LOCAL_HOME" "$HOME/Library/LaunchAgents"
  [ "$self" -ef "$dst" ] 2>/dev/null || install -m 0755 "$self" "$dst"
  plist "$dst" > "$plist_dst"
  launchctl bootout "gui/$(id -u)" "$plist_dst" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist_dst" || { echo "ERROR: launchctl could not load $plist_dst" >&2; return 1; }
  echo "loaded launchd agent $LABEL (profile $PROFILE; state $STATE_FILE, log $LOG_FILE)"
}

uninstall_agent() {
  local plist_dst="$HOME/Library/LaunchAgents/${LABEL}.plist"
  if [ -f "$plist_dst" ]; then
    launchctl bootout "gui/$(id -u)" "$plist_dst" >/dev/null 2>&1 || true
    rm -f "$plist_dst"
  fi
  # bootout sends TERM, whose trap restores the default policy; do it here too in case the agent was not running.
  apply_mode full "$(vm_pids)" || true
  echo "removed launchd agent $LABEL; the build VM runs at its default policy"
}

status() {
  local pids p
  if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then echo "agent: $LABEL loaded"; else echo "agent: $LABEL NOT loaded"; fi
  [ -f "$STATE_FILE" ] && sed 's/^/  /' "$STATE_FILE" || echo "  no state yet ($STATE_FILE)"
  pids="$(vm_pids)"
  [ -n "$pids" ] || { echo "build VM ($PROFILE): not running"; return 0; }
  for p in $pids; do
    # Thread priorities: 4 = background (yield), 31 = the default band (full).
    echo "build VM process $p: thread priorities $(ps -M -p "$p" 2>/dev/null | awk 'NR > 1 { for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+[A-Z]?$/ && $(i-1) ~ /^[A-Z+<>]+$/) { gsub(/[A-Z]/, "", $i); c[$i]++; break } } END { for (k in c) printf "%s×%s ", c[k], k }')"
  done
}

[ "${MEMEX_BUILD_PRIO_LIB:-}" = "1" ] && return 0 2>/dev/null

cmd="${1:-run}"; shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a Colima profile name}"; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac; shift
done
[[ "$PROFILE" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "ERROR: --profile must be a Colima profile name" >&2; exit 2; }
case "$cmd" in
  run) run ;;
  --install) install_agent ;;
  --uninstall) uninstall_agent ;;
  --status) status ;;
  *) echo "usage: buildhost-priority.sh [--install [--profile P] | --uninstall | --status]" >&2; exit 2 ;;
esac
