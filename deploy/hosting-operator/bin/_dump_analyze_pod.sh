# shellcheck shell=sh
# The IN-POD half of hosting-dump-analyze — NOT run by the operator. hosting-dump-analyze reads this
# file and hands it to the analysis Job as `sh -c`, in the instance's namespace, on the analyser image
# (Debian, GNU coreutils/findutils), with the dump claim mounted READ-ONLY at $MW_ROOT.
#
# It prints ONLY sentinel lines (`@@MW-DUMP <word>\t…`) and the text of a FIXED set of dotnet-dump
# commands; hosting-dump-analyze parses nothing else:
#
#   @@MW-DUMP listing                         then one `<bytes>\t<mtime epoch>\t<relative path>` per dump
#   @@MW-DUMP selected\t<rel>\t<bytes>\t<mtime epoch>
#   @@MW-DUMP section\t<name>\t<command>      then that command's output (at most $MW_SECTION_BYTES)
#   @@MW-DUMP end\t<name>\t<exit>\t<bytes the command printed in all>
#   @@MW-DUMP done                            the analysis reached its end
#   @@MW-DUMP refused\t<sentence>             it refused, and why — then exit non-zero
#
# 🚨 The selected path is resolved and must lie UNDER the mount: a symlink (file or directory) cannot
# walk the read out of the dump volume. The operator validated the selector's shape before the Job
# existed; this is the second half, where the filesystem is.
#
# Inside the `clrstack -all` section ONE more marker may appear, between a thread's frames:
#
#   @@MW-CUT <n>                              <n> frames of THIS thread were cut here (bound_stacks)
#
# Inputs (env): MW_DUMP | MW_POD | MW_AROUND (at most one), MW_DOTNET_DUMP_VERSION, MW_CMD_TIMEOUT,
# MW_SECTION_BYTES, MW_STACK_HEAD, MW_STACK_TAIL; MW_ROOT (default /dumps) and MW_TOOLS (default
# /tmp/dd) exist for the test.
set -u
root="${MW_ROOT:-/dumps}"
tools="${MW_TOOLS:-/tmp/dd}"
tab="$(printf '\t')"
emit() { printf '@@MW-DUMP %s\n' "$*"; }
refuse() { emit "refused${tab}$*"; exit 3; }

rootreal="$(realpath -e "$root")" || refuse "the dump root is not mounted"
emit "listing"
find "$rootreal" -mindepth 2 -maxdepth 2 -type f -name 'coredump.*' -printf '%s\t%Ts\t%P\n' | sort -t "$tab" -k2,2nr -k3,3

# The newest dump matching extra find predicates, relative to the root.
newest() {
  find "$rootreal" -mindepth 2 -maxdepth 2 -type f -name 'coredump.*' "$@" -printf '%Ts\t%P\n' \
    | sort -t "$tab" -k1,1nr | head -1 | cut -f2
}

rel=""
if [ -n "${MW_DUMP:-}" ]; then
  rel="$MW_DUMP"
elif [ -n "${MW_POD:-}" ]; then
  # A pod's live directory and its parked ones (`<pod>.<epoch>`, crash-dump-retention.sh step 0).
  rel="$(newest \( -path "$rootreal/$MW_POD/*" -o -path "$rootreal/$MW_POD.*/*" \))"
  [ -n "$rel" ] || refuse "pod $MW_POD left no dump on the volume"
elif [ -n "${MW_AROUND:-}" ]; then
  at="$(date -u -d "$MW_AROUND" +%s)" || refuse "cannot read the instant $MW_AROUND"
  best="" bestd=""
  # createdump names the file coredump.<pid>.<epoch> (DOTNET_DbgMiniDumpName …/coredump.%p.%t).
  candidates="$(find "$rootreal" -mindepth 2 -maxdepth 2 -type f -name 'coredump.*.*' -printf '%P\n' | sort)"
  while IFS= read -r f; do
    t="${f##*.}"
    case "$t" in ''|*[!0-9]*) continue ;; esac
    d=$((t - at)); [ "$d" -lt 0 ] && d=$((0 - d))
    if [ "$d" -le 1800 ] && { [ -z "$bestd" ] || [ "$d" -lt "$bestd" ]; }; then best="$f"; bestd="$d"; fi
  done <<EOF
$candidates
EOF
  [ -n "$best" ] || refuse "no dump on the volume was written within 30 minutes of $MW_AROUND"
  rel="$best"
else
  rel="$(newest)"
  [ -n "$rel" ] || refuse "the dump volume holds no dump"
fi

file="$rootreal/$rel"
{ [ -f "$file" ] && [ ! -L "$file" ]; } || refuse "$rel is not a dump file on the dump volume"
real="$(realpath -e "$file")" || refuse "$rel does not resolve"
case "$real" in "$rootreal"/*) ;; *) refuse "$rel resolves outside the dump volume" ;; esac
emit "selected${tab}${rel}${tab}$(stat -c %s "$real")${tab}$(stat -c %Y "$real")"

rm -f /tmp/mw-dd-threads.txt
export HOME=/tmp DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
dotnet tool install --tool-path "$tools" dotnet-dump --version "$MW_DOTNET_DUMP_VERSION" >/tmp/mw-dd-install.log 2>&1 \
  || refuse "could not install dotnet-dump $MW_DOTNET_DUMP_VERSION from nuget.org: $(tail -3 /tmp/mw-dd-install.log | tr '\n' ' ')"

# section <head|stat> <name> <command…>. `head` keeps the first $MW_SECTION_BYTES of the output.
# `stat` is for `dumpheap -stat`, which lists its types SMALLEST first and closes the table with its
# `Total … objects, … bytes` line, so the types that hold the heap are at the table's END. It keeps
# the output THROUGH that Total line — dropping what SOS prints after it (the "Fragmented blocks"
# table, which names object addresses and whose rows would displace the statistics) — and of that,
# the last $MW_SECTION_BYTES from the first whole line. Either way the full byte count is reported on
# the `end` line, so a cut is never silent.
section() {
  keep="$1" name="$2"; shift 2
  emit "section${tab}${name}${tab}$*"
  timeout "$MW_CMD_TIMEOUT" "$tools/dotnet-dump" analyze "$real" -c "setsymbolserver -ms" -c "$*" -c exit </dev/null >/tmp/mw-dd-out.txt 2>&1
  rc=$?
  size="$(stat -c %s /tmp/mw-dd-out.txt)"
  [ "$name" != clrthreads ] || cp /tmp/mw-dd-out.txt /tmp/mw-dd-threads.txt
  if [ "$keep" = stacks ]; then
    bound_stacks /tmp/mw-dd-out.txt >/tmp/mw-dd-bounded.txt
    head -c "$MW_SECTION_BYTES" /tmp/mw-dd-bounded.txt
  elif [ "$keep" = stat ]; then
    sed '/^[[:space:]]*Total [0-9,]* objects/q' /tmp/mw-dd-out.txt >/tmp/mw-dd-stat.txt
    if [ "$(stat -c %s /tmp/mw-dd-stat.txt)" -gt "$MW_SECTION_BYTES" ]; then
      tail -c "$MW_SECTION_BYTES" /tmp/mw-dd-stat.txt | sed 1d
    else
      cat /tmp/mw-dd-stat.txt
    fi
  else
    head -c "$MW_SECTION_BYTES" /tmp/mw-dd-out.txt
  fi
  printf '\n'
  emit "end${tab}${name}${tab}${rc}${tab}${size}"
}

# bound_stacks <clrstack -all output>: every thread whose stack is longer than MW_STACK_HEAD +
# MW_STACK_TAIL frames keeps its first MW_STACK_HEAD (innermost — where it died) and its last
# MW_STACK_TAIL (outermost — the code that STARTED the descent), and one `@@MW-CUT <n>` line stands
# for the frames between them. Every other line passes through unchanged.
#
# 🚨 WHY. A StackOverflowException leaves one thread thousands of frames deep. Measured on the first
# such read (memex-cloud, the 2026-10-23 22:40Z dump, MeshWeaver#5388): `clrstack -all` printed
# 14,692,132 bytes, ~7,140 frames of them on ONE thread, and the plain byte cap of this section cut
# INSIDE that thread — so the read lost the overflowing thread's outermost frames (the entry into the
# recursion: who started it) AND every thread printed after it (47 of 119 managed threads reported).
# Bounded per thread, a 7,000-frame thread costs MW_STACK_HEAD + MW_STACK_TAIL + 1 lines.
#
# Pure POSIX sh, two passes (count, then print), so it needs nothing the image does not already
# carry for this script. A frame is the condenser's shape: `<hex SP> <hex IP> <call site>`.
is_frame() {
  case "${1#"${1%%[! ]*}"}" in
    [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]*" "*) return 0 ;;
  esac
  return 1
}
bound_stacks() {
  sh_head="${MW_STACK_HEAD:-200}" sh_tail="${MW_STACK_TAIL:-100}"
  n=-1
  while IFS= read -r l || [ -n "$l" ]; do
    case "$l" in
      "OS Thread Id: "*) [ "$n" -lt 0 ] || printf '%s\n' "$n"; n=0 ;;
      *) if [ "$n" -ge 0 ] && is_frame "$l"; then n=$((n + 1)); fi ;;
    esac
  done <"$1" >/tmp/mw-dd-counts.txt
  [ "$n" -lt 0 ] || printf '%s\n' "$n" >>/tmp/mw-dd-counts.txt
  total=0 i=0
  exec 3</tmp/mw-dd-counts.txt
  while IFS= read -r l || [ -n "$l" ]; do
    case "$l" in
      "OS Thread Id: "*) IFS= read -r total <&3 || total=0; i=0; printf '%s\n' "$l"; continue ;;
    esac
    if [ "$total" -gt $((sh_head + sh_tail)) ] && is_frame "$l"; then
      i=$((i + 1))
      if [ "$i" -le "$sh_head" ] || [ "$i" -gt $((total - sh_tail)) ]; then
        printf '%s\n' "$l"
      elif [ "$i" -eq $((sh_head + 1)) ]; then
        printf '@@MW-CUT %s\n' $((total - sh_head - sh_tail))
      fi
    else
      printf '%s\n' "$l"
    fi
  done <"$1"
  exec 3<&-
}

# FIXED. None of these prints an object's fields, a string's value or the environment: `gcheapstat`
# prints per-heap generation sizes, `dumpheap -stat` one row per TYPE (method table, object count,
# total bytes, type name) — never an object, never an address of one. The ONE object read is an
# EXCEPTION: `printexception -nested` prints the exception a thread is in the middle of — its type,
# message, own stack trace and inner exceptions — and that text goes through the same key=value
# redaction as every other section.
section head eeversion eeversion
section head threadpool threadpool
section head gcheapstat gcheapstat
section stat dumpheap dumpheap -stat
section head syncblk syncblk
section head clrthreads clrthreads
# The exceptions `clrthreads` names in its Exception column (`<type> <address>`), at most three, by
# ADDRESS — so it is the faulting thread's exception whichever thread the debugger starts on. When
# it names none: the debugger's current thread. An address is hex digits only before it is passed.
pe_addrs="$(sed -n 's/.*[[:space:]][A-Za-z_][A-Za-z0-9_.+`]*Exception[[:space:]]\{1,\}\(0*[0-9a-fA-F]\{8,16\}\)\([[:space:]].*\)\{0,1\}$/\1/p' /tmp/mw-dd-threads.txt 2>/dev/null | head -3)"
if [ -n "$pe_addrs" ]; then
  for a in $pe_addrs; do
    case "$a" in *[!0-9a-fA-F]*) continue ;; esac
    section head printexception printexception -nested "$a"
  done
else
  section head printexception printexception -nested
fi
section stacks clrstack clrstack -all
section head dumpasync dumpasync --coalesce
emit "done"
