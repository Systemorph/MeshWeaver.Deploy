#!/bin/bash
# memex-local VM NETWORK SHAPER — a Colima VM's internet traffic yields to the owner's on a person's Mac.
#
# Rule (maintainer, 2026-11-08): "our fleet jobs have the LEAST priority on a person's Mac", and that includes the
# network. A home uplink is small (a few Mbit/s up), and an upload burst that fills it fills the modem's buffer too:
# every other flow then waits behind it (bufferbloat), so a TV stream stalls although the AVERAGE rate is modest.
# The build VM (profile `build`: git, NuGet, image pulls, artefact and module uploads to control) and the local portal's
# VM (profile `default`: registry pulls, sync) are the Mac's bulk senders. This shapes both AT THE ROOT, inside each VM,
# with Linux tc — no sudo on the Mac, nothing on the router:
#
#   • egress  — an HTB token bucket caps the VM's internet-bound traffic at a declared rate (fq_codel under it, so
#               the VM's own flows share the cap fairly and its queue stays short); traffic to the Mac itself (the VM's
#               on-link subnet: the gateway, port forwards, the kube API) is exempt
#   • ingress — the same cap on downloads (an ifb device), so a big image pull cannot fill the downlink either; the
#               receiver-side cap works because TCP senders back off when the shaper drops
#   • DSCP CS1 — every internet-bound packet is marked low-priority (lower effort), so a router or Wi-Fi access point
#               that honours DSCP (WMM maps CS1 to the background access category) deprioritises it as well
#
# The rates come from a declared SHARE of the measured link: `--measure` runs macOS `networkQuality` per DIRECTION,
# MEMEX_NET_SAMPLES (default 3) times each (it saturates the line ~15 s per run), takes the MEDIAN of each direction,
# logs the raw samples, and records the uplink and downlink; the cap is MEMEX_NET_SHARE_PCT (default 25) percent of
# each, never below MEMEX_NET_MIN_KBIT (default 2000) — so a bad probe can never throttle the line to a trickle.
# Measured 2026-11-10 on the second Mac Studio: ONE combined sequential run (`-s -M 25`) recorded 370 kbit/s up and
# 237972 kbit/s down on a symmetric fibre line, and the 256 kbit floor of the time capped its uploads at 256 kbit/s. A
# recorded uplink under 1/50 of the downlink is no real line's shape, so such a record is re-measured, not reused.
# The owner overrides either rate (MEMEX_NET_UP_KBIT / MEMEX_NET_DOWN_KBIT; 0 = no cap in that direction, the DSCP
# marking stays). Everything is recorded in ~/.memex-local/netshape-<profile>.env and persisted INSIDE the VM as a
# systemd unit (mw-netshape.service), so it survives a VM restart.
#
# Usage (memex-local runs these: `memex-local buildhost apply`, `memex-local up`, `memex-local net …`):
#   vm-netshape.sh --apply   [--profile P]   record the rates (measuring once when nothing is declared), install the
#                                            unit in the VM and start it
#   vm-netshape.sh --measure [--profile P]   re-measure the link once (networkQuality) and re-apply
#   vm-netshape.sh --remove  [--profile P]   stop and remove the unit; the VM's traffic is unshaped again
#   vm-netshape.sh --status  [--profile P]   the recorded rates, the unit, the qdiscs and their counters
#   vm-netshape.sh --render                  print the in-VM script for the current rates (tests, review)
#
# Bash 3.2 clean. Sourceable for tests: MEMEX_NETSHAPE_LIB=1 . vm-netshape.sh defines the functions only.
set -u

PROFILE="${MEMEX_NET_PROFILE:-build}"
LOCAL_HOME="${MEMEX_LOCAL_HOME:-$HOME/.memex-local}"
SHARE_PCT_DEFAULT=25
# Used when nothing is declared and the link cannot be measured: a typical home DSL/cable uplink is 5–20 Mbit/s.
FALLBACK_UP_KBIT=4000
FALLBACK_DOWN_KBIT=40000
# The least a cap DERIVED from a measurement may be, each direction (a declared rate is the owner's and is not raised).
MIN_CAP_KBIT_DEFAULT=2000
# networkQuality runs per direction, and how long each may take (seconds).
SAMPLES_DEFAULT=3
SAMPLE_MAX_S=15
# An uplink under 1/ASYMMETRY_MAX of the downlink is not a real line (ADSL is ~1:16, DOCSIS ~1:20): a bad probe.
ASYMMETRY_MAX=50
UNIT=mw-netshape
DSCP_CLASS=CS1

config_file() { printf '%s/netshape-%s.env' "$LOCAL_HOME" "$PROFILE"; }

# Load the recorded configuration. Each line only fills a variable the environment left unset, so a shell's override
# still wins at the next apply.
load_config() {
  local f; f="$(config_file)"
  # shellcheck source=/dev/null
  [ -f "$f" ] && . "$f"
  return 0
}

whole() { case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; return 0; }

# The caps, pure: uplink kbit, downlink kbit (0 = unknown), share percent, explicit up, explicit down (empty =
# not declared). Prints "<up kbit> <down kbit>". A declared rate wins; otherwise share × measured link, never below
# MEMEX_NET_MIN_KBIT (default 2000 — a bad measurement must not throttle the line to a trickle); an unknown link
# takes the fallback.
caps() {
  local link_up="$1" link_down="$2" share="$3" up="$4" down="$5" min="${MEMEX_NET_MIN_KBIT:-$MIN_CAP_KBIT_DEFAULT}"
  if ! whole "$up"; then
    if whole "$link_up" && [ "$link_up" -gt 0 ]; then up=$(( link_up * share / 100 )); else up=$FALLBACK_UP_KBIT; fi
    [ "$up" -ge "$min" ] || up=$min
  fi
  if ! whole "$down"; then
    if whole "$link_down" && [ "$link_down" -gt 0 ]; then down=$(( link_down * share / 100 )); else down=$FALLBACK_DOWN_KBIT; fi
    [ "$down" -ge "$min" ] || down=$min
  fi
  printf '%s %s' "$up" "$down"
}

# One networkQuality run in ONE direction ($1 = up | down): the whole run budget goes to that direction, never shared
# with the other. Prints whole kbit/s, or nothing when the run fails or reports no throughput.
measure_once() {
  local flag key
  if [ "$1" = up ]; then flag=-d; key=ul_throughput; else flag=-u; key=dl_throughput; fi
  networkQuality -c "$flag" -M "${MEMEX_NET_SAMPLE_MAX_S:-$SAMPLE_MAX_S}" 2>/dev/null | python3 -c '
import json, sys
try:
    v = int(json.load(sys.stdin)[sys.argv[1]] / 1000)
    if v > 0: print(v)
except Exception:
    pass' "$key"
}

# The median of whole numbers (the lower-middle mean for an even count); nothing for none.
median() { printf '%s\n' "$@" | grep -E '^[0-9]+$' | sort -n | awk '{ a[NR] = $1 } END { if (NR == 0) exit; if (NR % 2) print a[(NR + 1) / 2]; else print int((a[NR / 2] + a[NR / 2 + 1]) / 2) }'; }

# 0 when an uplink/downlink pair is a plausible line (both > 0 and the uplink at least 1/ASYMMETRY_MAX of the downlink).
plausible_link() { whole "$1" && whole "$2" && [ "$1" -gt 0 ] && [ "$2" -gt 0 ] && [ $(( $1 * ASYMMETRY_MAX )) -ge "$2" ]; }

# Measure the link with macOS networkQuality: MEMEX_NET_SAMPLES runs per direction, the median of each. Logs the raw
# samples on stderr (and keeps them for the record's comment). Prints "<up kbit> <down kbit>", or nothing when
# networkQuality is absent or no run in some direction answered.
measure_link() {
  command -v networkQuality >/dev/null 2>&1 || return 0
  local n="${MEMEX_NET_SAMPLES:-$SAMPLES_DEFAULT}" i=0 v ups="" downs="" mu md
  whole "$n" && [ "$n" -ge 1 ] || n=$SAMPLES_DEFAULT
  while [ "$i" -lt "$n" ]; do
    i=$(( i + 1 ))
    v="$(measure_once up)"; whole "$v" && ups="$ups $v"
    v="$(measure_once down)"; whole "$v" && downs="$downs $v"
  done
  # shellcheck disable=SC2086 # word-splitting the sample lists is the point
  mu="$(median $ups)"; md="$(median $downs)"
  MEASURE_NOTE="networkQuality samples (kbit/s, ${n} per direction): up${ups:- none} -> median ${mu:-none}; down${downs:- none} -> median ${md:-none}"
  echo "$MEASURE_NOTE" >&2
  mkdir -p "$LOCAL_HOME" && printf '%s\n' "$MEASURE_NOTE" > "$LOCAL_HOME/.netshape-last-measure"
  [ -n "$mu" ] && [ -n "$md" ] || return 0
  plausible_link "$mu" "$md" \
    || echo "WARN: the measured uplink ${mu} kbit/s is under 1/${ASYMMETRY_MAX} of the downlink ${md} kbit/s — no real line has that shape; the cap is held at no less than ${MEMEX_NET_MIN_KBIT:-$MIN_CAP_KBIT_DEFAULT} kbit/s. Declare MEMEX_NET_UP_KBIT if the uplink really is that small, or 'memex-local net measure' again." >&2
  printf '%s %s' "$mu" "$md"
}

# The script that runs INSIDE the VM as root (mw-netshape apply|stop). POSIX sh; the rates are baked in.
render() {
  local up="$1" down="$2"
  cat <<SCRIPT
#!/bin/sh
# mw-netshape — written by memex-local (deploy/homebrew/share/vm-netshape.sh). Do not edit: 'memex-local net apply'
# rewrites it. Fleet traffic yields to the owner's: internet-bound traffic is capped at ${up} kbit/s up and
# ${down} kbit/s down (0 = uncapped) and marked DSCP ${DSCP_CLASS}; traffic to the Mac itself is exempt.
set -u
UP_KBIT=${up}
DOWN_KBIT=${down}
IFB=ifb-mw
DEV="\$(ip -4 route show default | awk '{ for (i = 1; i < NF; i++) if (\$i == "dev") { print \$(i + 1); exit } }')"
[ -n "\$DEV" ] || { echo "mw-netshape: no default route" >&2; exit 1; }
# The on-link subnet of the uplink device: the Mac (gateway, port forwards, kube API). Never shaped, never marked.
LOCALNET="\$(ip -4 route show dev "\$DEV" scope link | awk '\$0 ~ /proto kernel/ { print \$1; exit }')"
[ -n "\$LOCALNET" ] || LOCALNET=192.168.5.0/24

clear() {
  tc qdisc del dev "\$DEV" root 2>/dev/null
  tc qdisc del dev "\$DEV" ingress 2>/dev/null
  ip link del "\$IFB" 2>/dev/null
  if iptables -t mangle -S MW-NETSHAPE >/dev/null 2>&1; then
    while iptables -t mangle -D POSTROUTING -j MW-NETSHAPE 2>/dev/null; do :; done
    iptables -t mangle -F MW-NETSHAPE; iptables -t mangle -X MW-NETSHAPE
  fi
  return 0
}

# One HTB tree: 1:10 the Mac (line rate), 1:20 everything else (the cap). \$1 device, \$2 cap kbit, \$3 the u32 match
# field for the Mac's subnet (dst on egress, src on ingress).
shape() {
  tc qdisc add dev "\$1" root handle 1: htb default 20 r2q 100 || return 1
  tc class add dev "\$1" parent 1: classid 1:1 htb rate 10gbit || return 1
  tc class add dev "\$1" parent 1:1 classid 1:10 htb rate 10gbit prio 0 || return 1
  # burst 64k: with HTB's default burst (one MTU) a VM's coarse timer let a 70 Mbit/s cap pass only 9–43 Mbit/s
  # (measured on the first Mac, 2026-11-08); 64 KiB is ~7 ms at that rate and the cap holds at 67 Mbit/s.
  tc class add dev "\$1" parent 1:1 classid 1:20 htb rate "\${2}kbit" ceil "\${2}kbit" burst 64k cburst 64k prio 1 || return 1
  tc qdisc add dev "\$1" parent 1:10 handle 10: fq_codel || return 1
  tc qdisc add dev "\$1" parent 1:20 handle 20: fq_codel || return 1
  tc filter add dev "\$1" parent 1: protocol ip prio 1 u32 match ip "\$3" "\$LOCALNET" flowid 1:10 || return 1
  tc filter add dev "\$1" parent 1: protocol arp prio 2 u32 match u32 0 0 flowid 1:10 || return 1
}

case "\${1:-apply}" in
  stop) clear; echo "mw-netshape: removed from \$DEV"; exit 0 ;;
  apply) ;;
  *) echo "usage: mw-netshape apply|stop" >&2; exit 2 ;;
esac

clear
rc=0
if [ "\$UP_KBIT" -gt 0 ]; then shape "\$DEV" "\$UP_KBIT" dst || rc=1; fi
if [ "\$DOWN_KBIT" -gt 0 ]; then
  modprobe ifb numifbs=0 2>/dev/null
  { ip link add "\$IFB" type ifb && ip link set "\$IFB" up \\
    && tc qdisc add dev "\$DEV" handle ffff: ingress \\
    && tc filter add dev "\$DEV" parent ffff: protocol all prio 1 u32 match u32 0 0 action mirred egress redirect dev "\$IFB" \\
    && shape "\$IFB" "\$DOWN_KBIT" src; } || rc=1
fi
# DSCP ${DSCP_CLASS} on every packet leaving for the internet (pod traffic included: mangle POSTROUTING sees forwarded packets).
iptables -t mangle -N MW-NETSHAPE 2>/dev/null || iptables -t mangle -F MW-NETSHAPE
iptables -t mangle -A MW-NETSHAPE -o "\$DEV" ! -d "\$LOCALNET" -j DSCP --set-dscp-class ${DSCP_CLASS} || rc=1
iptables -t mangle -C POSTROUTING -j MW-NETSHAPE 2>/dev/null || iptables -t mangle -A POSTROUTING -j MW-NETSHAPE || rc=1
[ "\$rc" -eq 0 ] || { echo "mw-netshape: a tc/iptables step failed on \$DEV — traffic is NOT fully shaped" >&2; exit 1; }
echo "mw-netshape: \$DEV up \${UP_KBIT} kbit/s, down \${DOWN_KBIT} kbit/s (0 = uncapped), DSCP ${DSCP_CLASS}; \$LOCALNET exempt"
SCRIPT
}

unit_file() {
  cat <<UNITFILE
[Unit]
Description=memex-local: fleet traffic yields to the owner's network (rate cap + DSCP ${DSCP_CLASS})
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/${UNIT} apply
ExecStop=/usr/local/sbin/${UNIT} stop

[Install]
WantedBy=multi-user.target
UNITFILE
}

vm() { colima ssh -p "$PROFILE" -- "$@"; }

write_config() {
  local link_up="$1" link_down="$2" f
  f="$(config_file)"; mkdir -p "$LOCAL_HOME"
  {
    printf '# written by vm-netshape.sh (memex-local net); the link was measured once — "memex-local net measure" re-measures\n'
    # the raw samples of the measurement this record came from (written by measure_link), when one ran just now
    [ -f "$LOCAL_HOME/.netshape-last-measure" ] && sed 's/^/# /' "$LOCAL_HOME/.netshape-last-measure" && rm -f "$LOCAL_HOME/.netshape-last-measure"
    # A failed measurement (0 0) is NOT a reading and is not recorded: recording it would make every later apply
    # return 0/0 before it looked at another profile's record or measured again.
    if whole "$link_up" && [ "$link_up" -gt 0 ]; then
      printf '[ -n "${MEMEX_NET_LINK_UP_KBIT:-}" ] || MEMEX_NET_LINK_UP_KBIT=%s\n' "$link_up"
      printf '[ -n "${MEMEX_NET_LINK_DOWN_KBIT:-}" ] || MEMEX_NET_LINK_DOWN_KBIT=%s\n' "$link_down"
    fi
    printf '[ -n "${MEMEX_NET_SHARE_PCT:-}" ] || MEMEX_NET_SHARE_PCT=%s\n' "${MEMEX_NET_SHARE_PCT:-$SHARE_PCT_DEFAULT}"
    [ -n "${MEMEX_NET_UP_KBIT:-}" ] && printf '[ -n "${MEMEX_NET_UP_KBIT:-}" ] || MEMEX_NET_UP_KBIT=%s\n' "$MEMEX_NET_UP_KBIT"
    [ -n "${MEMEX_NET_DOWN_KBIT:-}" ] && printf '[ -n "${MEMEX_NET_DOWN_KBIT:-}" ] || MEMEX_NET_DOWN_KBIT=%s\n' "$MEMEX_NET_DOWN_KBIT"
    true
  } > "$f.tmp" && mv "$f.tmp" "$f"
}

# The link: the profile's record, else any other profile's record on this Mac (one uplink), else one measurement.
# An IMPLAUSIBLE record (plausible_link: an uplink under 1/ASYMMETRY_MAX of the downlink — what one bad probe recorded)
# is not reused: it is said, and the link is measured again.
link_rates() {
  local f lu ld
  # (a 0/0 record written before failed measurements stopped being recorded reads as no record)
  if whole "${MEMEX_NET_LINK_UP_KBIT:-}" && whole "${MEMEX_NET_LINK_DOWN_KBIT:-}" && [ "${MEMEX_NET_LINK_UP_KBIT}" -gt 0 ] \
    && [ "${1:-}" != remeasure ]; then
    if plausible_link "$MEMEX_NET_LINK_UP_KBIT" "$MEMEX_NET_LINK_DOWN_KBIT"; then
      printf '%s %s' "$MEMEX_NET_LINK_UP_KBIT" "$MEMEX_NET_LINK_DOWN_KBIT"; return 0
    fi
    echo "the recorded link (${MEMEX_NET_LINK_UP_KBIT} kbit/s up, ${MEMEX_NET_LINK_DOWN_KBIT} kbit/s down) is implausible — measuring again" >&2
  fi
  if [ "${1:-}" != remeasure ]; then
    for f in "$LOCAL_HOME"/netshape-*.env; do
      [ -f "$f" ] || continue
      lu="$(sed -n 's/.*MEMEX_NET_LINK_UP_KBIT=\([0-9]*\)$/\1/p' "$f")"; ld="$(sed -n 's/.*MEMEX_NET_LINK_DOWN_KBIT=\([0-9]*\)$/\1/p' "$f")"
      if plausible_link "$lu" "$ld"; then printf '%s %s' "$lu" "$ld"; return 0; fi
    done
  fi
  [ "${MEMEX_NET_NO_MEASURE:-}" = 1 ] && { printf '0 0'; return 0; }
  echo "measuring the link with networkQuality: ${MEMEX_NET_SAMPLES:-$SAMPLES_DEFAULT} runs per direction, up to ${MEMEX_NET_SAMPLE_MAX_S:-$SAMPLE_MAX_S} s each (it fills the line while it runs)…" >&2
  local m; m="$(measure_link)"
  [ -n "$m" ] && { printf '%s' "$m"; return 0; }
  echo "WARN: networkQuality could not measure the link — using the fallback caps (${FALLBACK_UP_KBIT}/${FALLBACK_DOWN_KBIT} kbit/s); declare MEMEX_NET_UP_KBIT / MEMEX_NET_DOWN_KBIT to set them" >&2
  printf '0 0'
}

apply() {
  local link lu ld c up down tmp
  # The owner's escape hatch is read HERE, so every caller honours it (memex-local up, buildhost apply, `net apply`,
  # first-attach.sh) — never only the callers that remember to check it. `--remove` is not gated: it only undoes.
  if [ "${MEMEX_NET_SHAPE:-on}" = off ]; then
    echo "profile $PROFILE: network shaping is off (MEMEX_NET_SHAPE=off) — nothing applied"; return 0
  fi
  link="$(link_rates "${1:-}")"; lu="${link% *}"; ld="${link#* }"
  c="$(caps "$lu" "$ld" "${MEMEX_NET_SHARE_PCT:-$SHARE_PCT_DEFAULT}" "${MEMEX_NET_UP_KBIT:-}" "${MEMEX_NET_DOWN_KBIT:-}")"
  up="${c% *}"; down="${c#* }"
  write_config "$lu" "$ld"
  tmp="$(mktemp)"
  render "$up" "$down" > "$tmp"
  vm sudo sh -c "cat > /usr/local/sbin/${UNIT}.new && chmod 0755 /usr/local/sbin/${UNIT}.new && mv /usr/local/sbin/${UNIT}.new /usr/local/sbin/${UNIT}" < "$tmp" \
    || { rm -f "$tmp"; echo "ERROR: could not write the shaper into the VM (profile $PROFILE) — is it running?" >&2; return 1; }
  rm -f "$tmp"
  unit_file | vm sudo sh -c "cat > /etc/systemd/system/${UNIT}.service" || return 1
  vm sudo systemctl daemon-reload || return 1
  vm sudo systemctl enable "${UNIT}.service" >/dev/null 2>&1 || return 1
  vm sudo systemctl restart "${UNIT}.service" || { vm sudo journalctl -u "${UNIT}" -n 5 --no-pager >&2; return 1; }
  echo "profile $PROFILE: internet traffic capped at ${up} kbit/s up, ${down} kbit/s down (link ${lu}/${ld} kbit/s, share ${MEMEX_NET_SHARE_PCT:-$SHARE_PCT_DEFAULT} %), DSCP ${DSCP_CLASS}"
}

remove() {
  vm sudo sh -c "systemctl disable --now ${UNIT}.service >/dev/null 2>&1; rm -f /etc/systemd/system/${UNIT}.service /usr/local/sbin/${UNIT}; systemctl daemon-reload" || return 1
  rm -f "$(config_file)"
  echo "profile $PROFILE: the shaper is removed; the VM's traffic is unshaped"
}

status() {
  local f; f="$(config_file)"
  if [ -f "$f" ]; then sed -n 's/.*|| \(MEMEX_NET_[A-Z_]*=.*\)$/  recorded: \1/p' "$f"; else echo "  no recorded rates ($f)"; fi
  vm sh -c "systemctl is-active ${UNIT}.service; sudo /usr/sbin/tc -s class show dev \$(ip -4 route show default | awk '{print \$5; exit}') 2>/dev/null | grep -A2 'class htb 1:20'; sudo /usr/sbin/tc -s class show dev ifb-mw 2>/dev/null | grep -A2 'class htb 1:20'" 2>/dev/null \
    | sed 's/^/  /' || echo "  the VM (profile $PROFILE) is not reachable"
}

[ "${MEMEX_NETSHAPE_LIB:-}" = "1" ] && return 0 2>/dev/null

cmd="${1:---status}"; shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a Colima profile name}"; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac; shift
done
[[ "$PROFILE" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "ERROR: --profile must be a Colima profile name" >&2; exit 2; }
for v in MEMEX_NET_UP_KBIT MEMEX_NET_DOWN_KBIT MEMEX_NET_SHARE_PCT MEMEX_NET_MIN_KBIT MEMEX_NET_SAMPLES MEMEX_NET_SAMPLE_MAX_S; do
  [ -z "${!v:-}" ] || whole "${!v}" || { echo "ERROR: $v must be a whole number (got '${!v}')" >&2; exit 2; }
done
load_config
case "$cmd" in
  --apply) apply ;;
  --measure) apply remeasure ;;
  --remove) remove ;;
  --status) status ;;
  --render) c="$(caps "${MEMEX_NET_LINK_UP_KBIT:-0}" "${MEMEX_NET_LINK_DOWN_KBIT:-0}" "${MEMEX_NET_SHARE_PCT:-$SHARE_PCT_DEFAULT}" "${MEMEX_NET_UP_KBIT:-}" "${MEMEX_NET_DOWN_KBIT:-}")"; render "${c% *}" "${c#* }" ;;
  *) echo "usage: vm-netshape.sh [--apply | --measure | --remove | --status | --render] [--profile P]" >&2; exit 2 ;;
esac
