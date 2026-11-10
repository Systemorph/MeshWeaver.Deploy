#!/usr/bin/env bash
# Tests for memex-local — the local-stack CLI every developer's setup runs.
#
# 🚨 WHY THIS EXISTS. deploy/homebrew/bin/memex-local is ~2000 lines that stand up the whole local
# portal, and until MeshWeaver#2367 NOTHING in CI opened it: no shellcheck, no parse, no behaviour.
# The bug that issue reported was a verification that could not fail — `verify_endpoint` curl'd `/`
# and printed `ok "Portal reachable"` whenever curl exited 0, so a 503, a 404 and a portal rendering
# every control as its `ToString()` all produced the same green line. A gate that cannot fail is not
# a gate, and the only way to know a gate can fail is to make it fail.
#
# WHAT THIS CAN AND CANNOT PROVE, stated plainly so a green run is not read as more than it is.
# memex-local drives colima, docker, helm, kubectl, mkcert and curl against a live Colima VM; a CI
# runner has none of those, and mocking them would only assert that the mocks agree with themselves.
# So this covers the layer that IS testable and is where this class of bug lives:
#
#   • the usability verification — every red state it must report, and that it reports each one
#     with a REMEDY rather than a bare failure
#   • argument handling — an unknown flag must refuse, not be silently ignored
#   • that a green run is actually reachable, so the tests above are not passing vacuously
#
# What it does NOT cover: whether `kubectl exec` finds the module directory on a real pod. That is
# settled by a real `memex-local up`, which is why the probe's shape is kept identical to the
# diagnostic commands in MeshWeaver#2367's own repro.

set -uo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CLI="$HERE/../bin/memex-local"

pass=0; fail=0; RC=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s\n' "$1"; printf '        %s\n' "${2:-}"; fail=$((fail+1)); }

# ---------------------------------------------------------------------------
# A fake toolchain. Each stub answers from an env var, so one test = one state.
#   FAKE_HTTP_ROOT  / FAKE_HTTP_LOGIN — the status code curl reports per path
#   FAKE_MODULES                      — the lines the in-pod probe prints
#   FAKE_EXEC_FAILS                   — kubectl exec exits non-zero (unreadable pod)
#   FAKE_CURL_FAILS                   — curl cannot connect at all (no port-forward bound)
# ---------------------------------------------------------------------------
STUBS="$(mktemp -d)"
trap 'rm -rf "$STUBS"' EXIT

cat > "$STUBS/colima" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

# curl is called as: curl [--cacert X] -sS -o /dev/null -w '%{http_code}' --max-time 15 <url>
# 🚨 The FAKE_CURL_FAILS arm emulates the one curl behaviour that matters here and that a
# stub which only ever exits 0 can never reach: on a CONNECTION failure curl still WRITES its
# %{http_code} — the literal '000' — and THEN exits non-zero. probe_http's `|| printf '000'`
# therefore APPENDED a second '000', and the resulting '000000' matched no arm of the caller's
# case statement. That is how "nothing is listening on :8443" got reported as a serving-portal
# fault with the INGRESS as its remedy. The old stub is why this suite never saw it.
# 🚨 The stub must honour the -w FORMAT, not just the URL. awaiting_setup asks for
# %{redirect_url} as well as %{http_code}, and a stub that answered a status code to both made the
# setup branch unreachable — the suite would have gone green over a function it never ran.
cat > "$STUBS/curl" <<'EOF'
#!/usr/bin/env bash
url="${!#}"
fmt=""
prev=""
for a in "$@"; do
  [ "$prev" = "-w" ] && fmt="$a"
  prev="$a"
done
if [ -n "${FAKE_CURL_FAILS:-}" ]; then printf '000'; exit 7; fi
# The enrolment poll (fleet join, enrolment v2): the signed body is kept, and the next scripted answer
# ("<status> <json>" per line of $FAKE_POLL_DIR/answers; the last one repeats) is written with ' <status>'.
case "$url" in
  */api/enrol/poll)
    if [ -n "${FAKE_POLL_DIR:-}" ]; then
      n=$(( $(find "$FAKE_POLL_DIR" -name 'body.*' | wc -l) + 1 ))
      cat > "$FAKE_POLL_DIR/body.$n"
      line="$(sed -n "${n}p" "$FAKE_POLL_DIR/answers")"; [ -n "$line" ] || line="$(tail -1 "$FAKE_POLL_DIR/answers")"
      printf '%s %s' "${line#* }" "${line%% *}"; exit 0
    fi ;;
esac
case "$fmt" in
  *redirect_url*)
    # Only an instance awaiting setup redirects the ROOT to the wizard.
    case "$url" in
      *//*/) printf '%s' "${FAKE_REDIRECT_ROOT:-}" ;;
      *)     printf '' ;;
    esac
    ;;
  *)
    case "$url" in
      */login) printf '%s' "${FAKE_HTTP_LOGIN:-200}" ;;
      */setup) printf '%s' "${FAKE_HTTP_SETUP:-200}" ;;
      *)       printf '%s' "${FAKE_HTTP_ROOT:-200}" ;;
    esac
    ;;
esac
EOF

# kubectl exec — the in-pod module probe; kubectl logs — the portal's boot module report, which is
# what tells an ACTIVATED pack from bytes lying on disk. Everything else is a no-op success.
#
# 🚨 The report DEFAULTS to "all three packs loaded" so the existing red states keep isolating the
# one cause each of them is about. FAKE_NO_MODULE_LOAD is a separate flag rather than an empty
# FAKE_MODULE_LOAD, because an empty value and an unset one are the same thing to the stub — and
# "the report is absent" is a state a test has to be able to set on purpose.
cat > "$STUBS/kubectl" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "exec" ]; then
    [ -n "${FAKE_EXEC_FAILS:-}" ] && exit 1
    if [ -n "${FAKE_PROBE_IMAGE_ROOT:-}" ]; then
      # Exercise the CLI's actual in-pod probe against a temporary image layout.
      # Only replace the container's absolute /app path with this test's root.
      previous=""; script=""
      for arg in "$@"; do
        [ "$previous" = "-c" ] && script="$arg"
        previous="$arg"
      done
      script="$(printf '%s' "$script" | sed "s@/app/modules@$FAKE_PROBE_IMAGE_ROOT/modules@g")"
      Modules__Root="$FAKE_PROBE_DATA_ROOT" sh -c "$script" sh "${!#}"
      exit $?
    fi
    # 🚨 Report ONLY the modules the probe actually asked about — the real in-pod script loops over
    # the list it is handed as its last argument, so a pack the caller does not name is a pack
    # nothing looks at. Echoing the whole fixture instead made "a missing <pack> is caught" pass for
    # a pack that was not in VIEW_PACK_MODULES at all: a control that could not fail.
    packs=" ${!#} "
    printf '%s\n' "${FAKE_MODULES:-}" | while IFS= read -r line; do
      case "$line" in
        module=*)
          name="${line#module=}"; name="${name%% *}"
          case "$packs" in *" $name "*) printf '%s\n' "$line" ;; esac ;;
        ?*) printf '%s\n' "$line" ;;
      esac
    done
    exit 0
  fi
  if [ "$a" = "logs" ]; then
    [ -n "${FAKE_NO_MODULE_LOAD:-}" ] && exit 0
    if [ -n "${FAKE_MODULE_LOAD:-}" ]; then printf '%s\n' "$FAKE_MODULE_LOAD"; exit 0; fi
    for m in MeshWeaver.Blazor.Views MeshWeaver.Blazor.Graph MeshWeaver.Blazor.EntityViews; do
      printf '[ModuleLoad] %s ← /app/modules/%s/%s.dll (source=appsettings, mvid=…, written=…)\n' "$m" "$m" "$m"
    done
    exit 0
  fi
done
exit 0
EOF

chmod +x "$STUBS"/colima "$STUBS"/curl "$STUBS"/kubectl
export PATH="$STUBS:$PATH"

# 🚨 THREE packs, not two: EntityViews joined VIEW_PACK_MODULES because a portal missing it can
# draw no input control at all, and check 3 was calling that portal usable (Plugins#1483).
PACKS_PRESENT='module=MeshWeaver.Blazor.Views present
module=MeshWeaver.Blazor.Graph present
module=MeshWeaver.Blazor.EntityViews present'

# Run `memex-local verify [$VERIFY_FLAGS]` in a given state, leaving output in OUT and status in RC.
# 🚨 Not `OUT=$(run_verify …)`: a command substitution is a SUBSHELL, so an RC assigned inside one
# never reaches the caller — the first draft of this file did exactly that and every assertion read
# a stale RC=0, i.e. the suite reported "exited 0" for runs that had exited 1. A test harness that
# cannot see the exit code it is asserting on is the same defect as the gate it is testing.
OUT=""
VERIFY_FLAGS=""
run_verify() {
  # shellcheck disable=SC2086  # VERIFY_FLAGS is a deliberate word-split of flags under test
  OUT="$(env "$@" "$CLI" verify $VERIFY_FLAGS 2>&1)"; RC=$?
}

# A red state must EXIT NON-ZERO and SAY WHAT TO DO. "It failed" without a remedy sends the
# developer to the source; that is the whole complaint in MeshWeaver#2367.
reports() {
  local what="$1" phrase="$2"; shift 2
  local out
  run_verify "$@"; out="$OUT"
  if [ "$RC" -eq 0 ]; then bad "$what" "exited 0 — it should have reported a problem. Said: $out"; return; fi
  case "$out" in
    *"$phrase"*) ;;
    *) bad "$what" "failed, but never said '${phrase}'. Said: ${out}"; return ;;
  esac
  case "$out" in
    *Remedy:*) ok "$what" ;;
    *) bad "$what" "reported the problem but named no remedy: ${out}" ;;
  esac
}

echo "── the verification can FAIL (each red state, with a remedy) ─────"

reports "a 503 portal is not 'reachable'" "HTTP 503" \
  FAKE_HTTP_ROOT=503 FAKE_HTTP_LOGIN=503 FAKE_MODULES="$PACKS_PRESENT"

reports "no answer at all is reported" "did NOT answer" \
  FAKE_HTTP_ROOT=000 FAKE_HTTP_LOGIN=000 FAKE_MODULES="$PACKS_PRESENT"

reports "a deleted login page is caught" "NOT ROUTED" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=404 FAKE_MODULES="$PACKS_PRESENT"

# 🚨 The single most common red state a developer meets, and the one §14 misnamed. `up` and
# `update` both restart the launchd port-forward and go STRAIGHT into the verification, so the
# probes routinely arrive before :8443 is bound. Real curl writes '000' and exits non-zero, the
# fallback appended a second '000', and '000000' fell through every arm of the case statement to
# the generic "that is not a serving portal" — which names the INGRESS. The developer is then sent
# to `memex-local logs` to read a healthy pod, for a socket that is missing on their own Mac.
reports "a connection failure is 'no answer', not a serving-portal fault" "did NOT answer" \
  FAKE_CURL_FAILS=1 FAKE_MODULES="$PACKS_PRESENT"

run_verify FAKE_CURL_FAILS=1 FAKE_MODULES="$PACKS_PRESENT"
case "$OUT" in
  *000000*) bad "a connection failure reports ONE status, not a doubled one" \
              "reported HTTP 000000 — probe_http concatenated curl's own '000' with its fallback: $OUT" ;;
  *) ok "a connection failure reports ONE status, not a doubled one" ;;
esac
case "$OUT" in
  *port-forward*) ok "a connection failure names the port-forward as the remedy" ;;
  *) bad "a connection failure names the port-forward as the remedy" \
       "nothing is listening on the host; sending the developer at the ingress wastes the session: $OUT" ;;
esac

# 🚨 Check 2 must not diagnose the sign-in ROUTE when nothing answered at all — it cannot know
# anything about /login through a transport that is not there, and "Remedy: memex-local logs"
# sends the developer to a healthy pod for a second time in the same output. One cause must
# produce one diagnosis; the transport is check 1's to name and check 2 defers to it.
case "$OUT" in
  *"/login answered HTTP 000"*) bad "a connection failure does not also indict /login" \
       "check 2 judged the sign-in route through a transport that never connected: $OUT" ;;
  *) ok "a connection failure does not also indict /login" ;;
esac

# ── an instance AWAITING FIRST-RUN SETUP ────────────────────────────────────────
# 🚨 It has no mesh, therefore no view packs, so every check below would condemn a portal that is
# working exactly as designed — a false RED on the first path a new user takes. verify_usable must
# recognise the state and say what to do about it.
# `reports` is for RED states — it demands a non-zero exit. Awaiting setup is a green state with
# an instruction, so it is asserted directly.
run_verify FAKE_HTTP_ROOT=302 FAKE_REDIRECT_ROOT="https://memex.localhost:8443/setup" \
  FAKE_HTTP_SETUP=200 FAKE_MODULES=""
if [ "$RC" -eq 0 ]; then ok "awaiting setup exits 0 — it is a state, not a failure"
else bad "awaiting setup exits 0 — it is a state, not a failure" "exited $RC: $OUT"; fi
case "$OUT" in
  *"AWAITING FIRST-RUN SETUP"*) ok "awaiting setup is reported, not condemned" ;;
  *) bad "awaiting setup is reported, not condemned" "said nothing about setup: $OUT" ;;
esac
case "$OUT" in
  *"memex-local setup"*) ok "awaiting setup points at the command that answers it" ;;
  *) bad "awaiting setup points at the command that answers it" "named no next step: $OUT" ;;
esac

# 🚨 THE NEGATIVE CONTROL, and it is not hypothetical: the first draft of this probe asked only
# "does /setup answer 200", and a CONFIGURED portal answers 200 there too — its Blazor SPA fallback
# serves almost any path. Every red state below then short-circuited into "awaiting setup" and the
# whole verification passed on a broken portal. The root REDIRECT is what discriminates.
run_verify FAKE_HTTP_ROOT=200 FAKE_HTTP_SETUP=200 FAKE_MODULES=""
case "$OUT" in
  *"AWAITING FIRST-RUN SETUP"*)
    bad "a configured portal is NOT mistaken for one awaiting setup" \
        "/setup answering 200 through the SPA fallback was read as setup mode: $OUT" ;;
  *) ok "a configured portal is NOT mistaken for one awaiting setup" ;;
esac
if [ "$RC" -ne 0 ]; then ok "…and its missing view packs are still caught"
else bad "…and its missing view packs are still caught" "exited 0 on a portal with no view packs: $OUT"; fi

reports "a missing view pack is caught" "MeshWeaver.Blazor.Views" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES='module=MeshWeaver.Blazor.Views missing
module=MeshWeaver.Blazor.Graph present'

reports "both view packs missing is caught" "ToString()" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES='module=MeshWeaver.Blazor.Views missing
module=MeshWeaver.Blazor.Graph missing'

# 🚨 The pack whose absence this check could not see (Plugins#1483). EntityViews is every input
# control in the product — text, number, date, choice, plus the Editor/EditForm/Property skins — so
# a portal without it can be read and not operated: no quiz can be answered, no coupon redeemed, no
# dialog field filled in. It was not in VIEW_PACK_MODULES, so this fixture's `missing` line was
# simply ignored and the run went green.
reports "a missing EntityViews is caught — a portal nobody can type into is not usable" \
  "MeshWeaver.Blazor.EntityViews" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES='module=MeshWeaver.Blazor.Views present
module=MeshWeaver.Blazor.Graph present
module=MeshWeaver.Blazor.EntityViews missing'

reports "landed-but-pending-restart is not green" "restart is PENDING" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES="$PACKS_PRESENT
pending-restart=yes"

# 🚨 The check that cannot read its own input must FAIL, never pass quietly. An unreadable pod
# used to be indistinguishable from a healthy one, which is the skip-trapdoor shape AGENTS.md bans.
reports "an unreadable pod fails the check, not passes it" "verified NOTHING" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 FAKE_EXEC_FAILS=1

# 🚨 THE STATE THIS CHECK USED TO CALL GREEN (Plugins#1483). The pack resolves on disk — it is in
# the image's own modules/ folder — and the running process never asked for it, so every control it
# draws still renders as its ToString(). "Resolvable" and "loaded" are different questions, and the
# probe was printing the answer to the first under the name of the second. The boot module report
# is what separates them, and it must be READ, not assumed.
reports "a pack on disk but NOT in the boot report is not green" "NOT LOADED" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 FAKE_MODULES="$PACKS_PRESENT" \
  FAKE_MODULE_LOAD='[ModuleLoad] MeshWeaver.Blazor.Views ← /app/MeshWeaver.Blazor.Views.dll (source=appsettings)
[ModuleLoad] MeshWeaver.Blazor.Graph ← /data/modules/MeshWeaver.Blazor.Graph@ab/MeshWeaver.Blazor.Graph.dll (source=store)'

# 🚨 And the same rule one level up: with NO boot report visible the check learned nothing about
# activation, and "learned nothing" is never a pass — the skip-trapdoor shape AGENTS.md bans, in the
# one place where the evidence is a log that can scroll away.
reports "no boot report means the check FAILS, never passes" "verified nothing" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 FAKE_MODULES="$PACKS_PRESENT" FAKE_NO_MODULE_LOAD=1

# 🚨 "Still installing" must read differently from "will never arrive" — and must still not be a
# success, because the portal cannot render at the moment the command returns. With a wait budget
# and no `[DefaultInstall] reconciled` line in the log, that is the state, and the message has to
# say so rather than accusing the install of having failed.
VERIFY_FLAGS="--wait 1"
reports "an install still in progress says so, and is still not green" "still installing" \
  FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES='module=MeshWeaver.Blazor.Views missing
module=MeshWeaver.Blazor.Graph missing'
# 🚨 …and it is a DISTINCT status from "broken" (first-run walk-through, 2026-10-27): `up` and
# `update` used to read this 1 and die with "NOT USABLE / err" on a portal that was honestly still
# installing its packages and finished minutes later by itself. 2 is what lets the two callers
# print one calm line instead, while a genuine red (1) still dies.
run_verify FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES='module=MeshWeaver.Blazor.Views missing
module=MeshWeaver.Blazor.Graph missing'
if [ "$RC" -eq 2 ]; then ok "'still installing' exits 2 — distinct from a broken portal's 1"
else bad "'still installing' exits 2 — distinct from a broken portal's 1" "exited $RC: $OUT"; fi
VERIFY_FLAGS=""
# The negative control for the status: a portal whose reconcile HAS reported and still lacks a pack
# is broken, not installing, and must keep exiting 1 — otherwise every red would read as "not yet".
run_verify FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES='module=MeshWeaver.Blazor.Views missing
module=MeshWeaver.Blazor.Graph missing'
if [ "$RC" -eq 1 ]; then ok "a settled missing pack still exits 1, never 2"
else bad "a settled missing pack still exits 1, never 2" "exited $RC: $OUT"; fi

echo "── …and a green run is reachable (so the above is not vacuous) ───"

run_verify FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 FAKE_MODULES="$PACKS_PRESENT"
if [ "$RC" -ne 0 ]; then
  bad "a healthy portal verifies green" "exited ${RC}: ${OUT}"
elif case "$OUT" in *"view packs loaded"*) false ;; *) true ;; esac; then
  bad "a healthy portal verifies green" "no 'view packs loaded' line in: ${OUT}"
else
  ok "a healthy portal verifies green"
fi

# MeshWeaver#6025: Modules__Root=/data does not move image DLLs out of /app/modules.
# Run the real probe body with a temporary /app substitute; the ordinary module
# fixture above would pass even if the CLI never searched the image directory.
image_root="$STUBS/image"; data_root="$STUBS/data"
mkdir -p "$data_root" "$image_root/modules"
for module in MeshWeaver.Blazor.Views MeshWeaver.Blazor.Graph MeshWeaver.Blazor.EntityViews; do
  mkdir -p "$image_root/modules/$module"
  : > "$image_root/modules/$module/$module.dll"
done
run_verify FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_PROBE_IMAGE_ROOT="$image_root" FAKE_PROBE_DATA_ROOT="$data_root"
if [ "$RC" -eq 0 ] && [[ "$OUT" == *"view packs loaded:"* ]]; then
  ok "image view packs are found when Modules__Root points to empty /data"
else
  bad "image view packs are found when Modules__Root points to empty /data" "exited $RC: $OUT"
fi

# A 302 to the sign-in page is the NORMAL anonymous response — treating it as red would make the
# gate fire on every healthy install and get it deleted within a day.
run_verify FAKE_HTTP_ROOT=302 FAKE_HTTP_LOGIN=200 FAKE_MODULES="$PACKS_PRESENT"
if [ "$RC" -eq 0 ]; then ok "a 302 to sign-in is healthy, not a failure"
else bad "a 302 to sign-in is healthy, not a failure" "exited ${RC}: ${OUT}"; fi

echo "── argument handling ─────────────────────────────────────────────"

out="$(env FAKE_MODULES="$PACKS_PRESENT" "$CLI" verify --nope 2>&1)"; rc=$?
if [ "$rc" -eq 0 ]; then bad "an unknown verify flag refuses" "exited 0: $out"
else case "$out" in *"unknown flag"*) ok "an unknown verify flag refuses" ;;
     *) bad "an unknown verify flag refuses" "refused without saying why: $out" ;; esac; fi

out="$(env FAKE_MODULES="$PACKS_PRESENT" "$CLI" verify --wait forever 2>&1)"; rc=$?
if [ "$rc" -eq 0 ]; then bad "--wait must be numeric" "exited 0: $out"
else case "$out" in *"whole seconds"*) ok "--wait must be numeric" ;;
     *) bad "--wait must be numeric" "refused without saying why: $out" ;; esac; fi

echo "── the help text is TEXT, not code ───────────────────────────────"

# 🚨 `cmd_help` is `cat <<EOF` — an UNQUOTED heredoc, so a backtick in the prose is COMMAND
# SUBSTITUTION. The usage text mentioned the autoroll `main` tag, and `main` is this script's own
# entry function: `memex-local help` re-entered main → cmd_help → main → … forking on every level.
# `memex-local help` was an unbounded fork bomb on main until MeshWeaver#2367, and nothing noticed
# because nothing in CI ever ran this script.
#
# Asserted STATICALLY, not by running `help` and waiting: a regression here HANGS, and a hang is not
# a failure signal — a suite that waits for it just times out with nothing to read. (The delimiter
# cannot simply be quoted: the text interpolates ${HOSTNAME_LOCAL}, ${HOST_PORT}, ${E2E_PORT}.)
heredoc="$(awk '/^  cat <<EOF$/ { on=1; next } on && /^EOF$/ { exit } on' "$CLI")"
bare="$(printf '%s\n' "$heredoc" | grep -c '[^\\]`' || true)"
if [ "$bare" -eq 0 ]; then ok "no unescaped backtick in the help heredoc (it would re-enter main())"
else bad "no unescaped backtick in the help heredoc" \
  "${bare} line(s) run a command instead of printing it: $(printf '%s\n' "$heredoc" | grep -n '[^\\]`')"; fi

echo "── macOS bash 3.2 safety (the runner is bash 5 — assert it statically) ──"

# 🚨 This tool runs ONLY on macOS, whose /bin/bash is 3.2, where `set -u` treats "${x[@]}" on an
# EMPTY array as an unbound variable and kills the script. CI runs bash 5, which tolerates it — so a
# runner can never reach this by executing the code, and the assertion has to be static or it does
# not exist. It was not theoretical: helm_deploy expanded next_args/plugin_args unguarded, both of
# which are empty on the paths the script itself calls normal (--from-acr, no plugin checkout), so
# the deploy died on "unbound variable" for them.
# The safe form ${x[@]+"${x[@]}"} CONTAINS the unsafe spelling, so lines carrying the guard marker
# `[@]+"` are dropped along with whole-line comments, and whatever still matches is unguarded. (A
# line holding a guarded AND an unguarded expansion would slip through; grep beats a portable-regex
# argument with sed for a check that has to behave identically on BSD and GNU. "$@" is a special
# parameter, not an array, and is always safe.)
unsafe="$(grep -vE '^[[:space:]]*#' "$CLI" \
  | grep -v '\[@\]+"' \
  | grep -E '"\$\{[A-Za-z_][A-Za-z0-9_]*\[@\]\}"' || true)"
if [ -z "$unsafe" ]; then ok 'no unguarded "${array[@]}" (bash 3.2 + set -u would abort)'
else bad 'no unguarded "${array[@]}"' "use \${x[@]+\"\${x[@]}\"} on: ${unsafe}"; fi

echo "── up/update actually RUN the verification ───────────────────────"

# The whole point of MeshWeaver#2367 is that these two reported success on an unusable portal.
# Deleting the call is the easy regression, and it leaves no trace anywhere else.
for c in cmd_up cmd_update; do
  # No `{` in the pattern: it opens an interval in ERE, and awk implementations disagree about
  # whether `\{` is an error, a warning or a literal. `^cmd_up\(\)` is unambiguous everywhere.
  body="$(awk -v f="^${c}\\\\(\\\\)" '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
  case "$body" in
    *"verify_usable --wait"*) ok "${c} runs the usability verification" ;;
    *) bad "${c} runs the usability verification" "no verify_usable call in ${c}()" ;;
  esac
  # 🚨 …and reads its THREE-WAY answer. Status 2 ("still installing") must reach ONE calm line and
  # a clean exit, never the `die "… NOT USABLE …"` a genuine red gets — that die on a portal that
  # finished by itself minutes later is what the first-run walk-through (2026-10-27) met.
  case "$body" in
    *"2) still_installing_note; return 0 ;;"*) ok "${c} treats 'still installing' (2) as a calm state, not NOT USABLE" ;;
    *) bad "${c} treats 'still installing' (2) as a calm state, not NOT USABLE" "no status-2 arm in ${c}()" ;;
  esac
  case "$body" in
    *"NOT USABLE"*) ok "${c} still dies on a genuine red (1)" ;;
    *) bad "${c} still dies on a genuine red (1)" "no NOT USABLE die left in ${c}()" ;;
  esac
  # And both hand the person INTO the wizard — one shared line, then cmd_setup with the token in
  # the URL — when the portal is waiting to be set up. `update` used to end on "update complete"
  # and leave the person to find the token themselves.
  case "$body" in
    *"awaiting_setup"*"hand_into_setup"*) ok "${c} hands an instance awaiting setup into the wizard" ;;
    *) bad "${c} hands an instance awaiting setup into the wizard" "no awaiting_setup → hand_into_setup in ${c}()" ;;
  esac
done
body="$(awk -v f='^hand_into_setup\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
case "$body" in
  *cmd_setup*) ok "the hand-off runs cmd_setup (URL with the token, opened)" ;;
  *) bad "the hand-off runs cmd_setup (URL with the token, opened)" "$body" ;;
esac
body="$(awk -v f='^still_installing_note\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
case "$body" in
  *"finishes by itself"*"memex-local verify"*) ok "the still-installing line says it finishes by itself and how to re-check" ;;
  *) bad "the still-installing line says it finishes by itself and how to re-check" "$body" ;;
esac

echo "── first-run copy: the key is optional, the token is in the link ──"

# cmd_setup is what `up`/`update` hand into; what it prints is the ONLY guidance a person has
# before the page. The walk-through stalled twice on exactly these two words: "token" (where is
# it?) and "key" (do I need one?).
body="$(awk -v f='^cmd_setup\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
case "$body" in
  *"OPTIONAL"*"free plan"*) ok "cmd_setup says the Registration key is optional and empty means the free plan" ;;
  *) bad "cmd_setup says the Registration key is optional and empty means the free plan" "not in cmd_setup()" ;;
esac
case "$body" in
  *"already in that URL"*) ok "cmd_setup says the token is already in the link it opens" ;;
  *) bad "cmd_setup says the token is already in the link it opens" "not in cmd_setup()" ;;
esac
case "$body" in
  *"already runs"*"pre-selected"*) ok "cmd_setup names the deployment's own PostgreSQL as the pre-selected database" ;;
  *) bad "cmd_setup names the deployment's own PostgreSQL as the pre-selected database" "not in cmd_setup()" ;;
esac

# The page-side half of the token hint: the portal renders "On this install, run `memex-local
# setup`" from InstanceSetup:TokenCommand, which the TRACKED defaults layer carries so every install
# gets it on `brew upgrade` (the generated overlay never reaches existing installs).
DEFAULTS="$HERE/../share/values.local.defaults.yaml"
if grep -q '^    InstanceSetup__TokenCommand: "memex-local setup"$' "$DEFAULTS"; then
  ok "values.local.defaults.yaml names 'memex-local setup' as the token command the setup page shows"
else bad "values.local.defaults.yaml names 'memex-local setup' as the token command the setup page shows" "$(grep -n TokenCommand "$DEFAULTS")"; fi
# …and says this install runs on a Mac, which the wizard's package step reads
# (InstanceSetup:HostPlatform) to pre-check the Mac-only packages (Google, iMessage, WhatsApp).
if grep -q '^    InstanceSetup__HostPlatform: "mac"$' "$DEFAULTS"; then
  ok "values.local.defaults.yaml declares InstanceSetup__HostPlatform: mac for the package step"
else bad "values.local.defaults.yaml declares InstanceSetup__HostPlatform: mac for the package step" "$(grep -n HostPlatform "$DEFAULTS")"; fi
# …and keeps each person's Claude Code login on the persisted /data hostPath: the chart default
# (empty) puts it on the container's writable layer, where a restart or an update deletes it.
if grep -q '^    ClaudeCode__ConfigDirRoot: "/data/' "$DEFAULTS"; then
  ok "values.local.defaults.yaml keeps Claude Code logins under the persisted /data"
else bad "values.local.defaults.yaml keeps Claude Code logins under the persisted /data" "$(grep -n ClaudeCode__ConfigDirRoot "$DEFAULTS")"; fi

echo "── a gate's own INPUT must be reachable (static — needs a live pod) ──"

# 🚨 §14 decides the boot reconcile has SETTLED by grepping the pod log for
# `[DefaultInstall] reconciled`. That call is LogInformation on a MeshWeaver.PluginCatalog.*
# category, and the portal image's appsettings.json caps the whole `MeshWeaver` prefix at Warning
# — so the line never reaches stdout on ANY install. The wait could therefore never settle early:
# it burned its full --wait budget (600 s on `update`) and then reported "the boot reconcile has
# not reported completion", which is a statement about the LOGGING CONFIG, not about the install.
# apply_logging set only Logging__LogLevel__Default, and a Default override does not lift a more
# specific prefix. Same defect class as the unreadable pod above: the check could not read its own
# input. Asserted statically — proving it dynamically needs a running portal.
body="$(awk -v f='^apply_logging\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
case "$body" in
  *Logging__LogLevel__MeshWeaver.PluginCatalog=Information*)
    ok "apply_logging lifts the category the DefaultInstall probe greps" ;;
  *) bad "apply_logging lifts the category the DefaultInstall probe greps" \
       "appsettings caps 'MeshWeaver' at Warning, so [DefaultInstall] reconciled is suppressed and the wait can never settle: ${body}" ;;
esac

# 🚨 `launchctl bootstrap` returns when launchd has ACCEPTED the job, not when the kubectl
# port-forward inside it has bound the socket. Both cmd_up and cmd_update go straight from
# install_launchd into verify_usable, so the verification routinely probes a socket that does not
# exist yet and reports a portal that is in fact fine. Waiting on the port is waiting on the
# actual precondition — and any HTTP status at all, 503 included, proves the socket is bound and
# is check 1's business to judge, so the wait cannot mask a genuinely broken portal.
body="$(awk -v f='^install_launchd\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
case "$body" in
  *wait_port_forward*) ok "install_launchd waits for the socket to actually bind" ;;
  *) bad "install_launchd waits for the socket to actually bind" \
       "it returns as soon as launchd accepts the job, and every caller verifies immediately after: ${body}" ;;
esac

echo "── registry mode is a state machine over ONE file ────────────────"

# `memex-local registry` decides where a local install gets its plugins from — its own checkout
# (which can never land a module binary, MeshWeaver#2417) or a remote registry — by writing or
# removing ~/.memex-local/registry.yaml. Driven through the whole cycle against a scratch home; a
# secret that is printed back, a wrong-shaped key that is accepted, or a file readable by everyone
# are each a defect this suite exists to catch.
RHOME="$STUBS/home"
reg() { OUT="$(env MEMEX_LOCAL_HOME="$RHOME" "$CLI" registry "$@" 2>&1)"; RC=$?; }

reg status
if [ "$RC" -eq 0 ] && case "$OUT" in *SELF-REGISTRY*) true ;; *) false ;; esac; then
  ok "a fresh install reports self-registry mode"
else bad "a fresh install reports self-registry mode" "exited ${RC}: ${OUT}"; fi

# No key is the DEFAULT: an open registration the registry enrols into its default plan (the free
# tier). The file must then carry NO secrets block at all — an empty PluginCatalog__BootstrapKey
# would still render into the chart's secret and read as "a key was given".
reg https://memex.example.test --id open-box
rfile="$RHOME/registry.yaml"
if [ "$RC" -eq 0 ] && case "$OUT" in *"OPEN registration"*) true ;; *) false ;; esac; then
  ok "a registry URL without a key is an OPEN (free-tier) registration"
else bad "a registry URL without a key is an OPEN (free-tier) registration" "exited ${RC}: ${OUT}"; fi
if [ -f "$rfile" ] && ! grep -q "PluginCatalog__BootstrapKey\|^secrets:" "$rfile"; then
  ok "an open registration writes no secrets block"
else bad "an open registration writes no secrets block" "$(cat "$rfile" 2>/dev/null)"; fi
reg status
case "$OUT" in *"OPEN"*"free tier"*) ok "status names the open registration and the free tier" ;;
  *) bad "status names the open registration and the free tier" "$OUT" ;; esac
reg off >/dev/null 2>&1 || true

reg https://memex.example.test --key mwi_this-is-an-instance-key
if [ "$RC" -ne 0 ] && case "$OUT" in *"REGISTRATION key"*) true ;; *) false ;; esac; then
  ok "an instance key (mwi_) is refused by name, not by a 401 on first boot"
else bad "an instance key (mwi_) is refused by name" "exited ${RC}: ${OUT}"; fi

reg not-a-url --key mwr_x
if [ "$RC" -ne 0 ] && case "$OUT" in *"expected a URL"*) true ;; *) false ;; esac; then
  ok "a non-URL first argument refuses"
else bad "a non-URL first argument refuses" "exited ${RC}: ${OUT}"; fi

reg https://memex.example.test --key mwr_x --id "Not Valid"
if [ "$RC" -ne 0 ] && case "$OUT" in *"--id must be"*) true ;; *) false ;; esac; then
  ok "an instance id outside the registry's alphabet refuses"
else bad "an instance id outside the registry's alphabet refuses" "exited ${RC}: ${OUT}"; fi

reg https://memex.example.test/ --key mwr_abcdefghijklmnop --id my-box
if [ "$RC" -eq 0 ] && [ -f "$rfile" ]; then ok "registry <url> --key --id writes the registry file"
else bad "registry <url> --key --id writes the registry file" "exited ${RC}: ${OUT}"; fi
if grep -q 'registryUrl: "https://memex.example.test"$' "$rfile" 2>/dev/null; then
  ok "the URL is stored without its trailing slash"
else bad "the URL is stored without its trailing slash" "$(cat "$rfile" 2>/dev/null)"; fi
if grep -q 'instanceId: "my-box"$' "$rfile" 2>/dev/null \
   && grep -q 'PluginCatalog__BootstrapKey: "mwr_abcdefghijklmnop"$' "$rfile" 2>/dev/null \
   && grep -q '^pluginCatalog:$' "$rfile" 2>/dev/null && grep -q '^secrets:$' "$rfile" 2>/dev/null; then
  ok "the file carries pluginCatalog.{registryUrl,instanceId} and the bootstrap-key secret"
else bad "the file carries pluginCatalog.{registryUrl,instanceId} and the bootstrap-key secret" "$(cat "$rfile" 2>/dev/null)"; fi
# 🚨 WITHOUT --id the file must carry NO instanceId, because the portal's first-run wizard asks for
# the name and mints the guid it registers under. An id here is deployment configuration, which
# OUTRANKS the instance manifest — so a defaulted <user>-<host> would let the wizard ask, register a
# guid, and then be silently overridden by a value the operator never chose. Ids are claimed
# globally and never re-issued, so guessing one is a permanent claim made on their behalf.
reg https://memex.example.test
if [ "$RC" -eq 0 ] && [ -f "$rfile" ]; then ok "registry <url> with no --id still writes the file"
else bad "registry <url> with no --id still writes the file" "exited ${RC}: ${OUT}"; fi
if grep -q 'instanceId:' "$rfile" 2>/dev/null; then
  bad "no --id leaves the instance id to the wizard" "the file pre-claims an id: $(cat "$rfile" 2>/dev/null)"
else ok "no --id leaves the instance id to the wizard"; fi
if grep -q 'registryUrl: "https://memex.example.test"$' "$rfile" 2>/dev/null; then
  ok "…and still records the registry the wizard should default to"
else bad "…and still records the registry the wizard should default to" "$(cat "$rfile" 2>/dev/null)"; fi

# Restore the pinned-id file for the mode/status assertions that follow.
reg https://memex.example.test/ --key mwr_abcdefghijklmnop --id my-box

case "$(ls -l "$rfile" 2>/dev/null | cut -c1-10)" in
  -rw-------) ok "the registry file is 0600 (it holds a secret)" ;;
  *) bad "the registry file is 0600 (it holds a secret)" "$(ls -l "$rfile" 2>/dev/null)" ;;
esac

reg status
if [ "$RC" -eq 0 ] && case "$OUT" in *"REGISTRY MODE"*"my-box"*) true ;; *) false ;; esac; then
  ok "status reports registry mode and the instance id"
else bad "status reports registry mode and the instance id" "exited ${RC}: ${OUT}"; fi
case "$OUT" in
  *mwr_abcdefghijklmnop*) bad "status never prints the bootstrap key" "$OUT" ;;
  *) ok "status never prints the bootstrap key" ;;
esac

# In registry mode a missing view pack must send the developer to the REGISTRY (grants, the key),
# not to a plugins checkout this install no longer mounts.
reports "in registry mode a missing pack names the registry, not a checkout" "memex.example.test" \
  MEMEX_LOCAL_HOME="$RHOME" FAKE_HTTP_ROOT=200 FAKE_HTTP_LOGIN=200 \
  FAKE_MODULES="module=MeshWeaver.Blazor.Views missing"

reg off
if [ "$RC" -eq 0 ] && [ ! -f "$rfile" ]; then ok "registry off removes the file"
else bad "registry off removes the file" "exited ${RC}: ${OUT}"; fi
reg status
case "$OUT" in *SELF-REGISTRY*) ok "after registry off the install is back in self-registry mode" ;;
  *) bad "after registry off the install is back in self-registry mode" "$OUT" ;; esac

reg --bogus
if [ "$RC" -ne 0 ]; then ok "an unknown registry argument refuses"
else bad "an unknown registry argument refuses" "exited 0: $OUT"; fi

# Static: the two halves of the mode switch. helm_deploy must layer the registry file in one mode
# and the self-registry blanking layer in the other; up/update must default to the CI-built image
# in registry mode — a source build there would run a portal the registry never baked against.
body="$(awk -v f='^helm_deploy\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
case "$body" in
  *'-f "$REGISTRY_FILE"'*"values.local.self-registry.yaml"*)
    ok "helm_deploy layers the registry file OR the self-registry defaults, by mode" ;;
  *) bad "helm_deploy layers the registry file OR the self-registry defaults, by mode" "$body" ;;
esac
# MeshWeaver.Deploy#65: registry mode also layers the tracked self-update layer (public GHCR, no
# credential, canPatch) BEFORE the per-install registry file, so that file and the overlay still win.
case "$body" in
  *"values.local.registry-mode.yaml"*'-f "$REGISTRY_FILE"'*)
    ok "helm_deploy layers the registry-mode self-update values before the registry file" ;;
  *) bad "helm_deploy layers the registry-mode self-update values before the registry file" "$body" ;;
esac
SHARE_DIR_T="$(cd "$(dirname "$CLI")/../share" && pwd)"
if grep -q '^  registry: "ghcr.io"' "$SHARE_DIR_T/values.local.registry-mode.yaml" \
   && grep -q '^  registryIsPublic: true' "$SHARE_DIR_T/values.local.registry-mode.yaml" \
   && grep -q '^  canPatch: true' "$SHARE_DIR_T/values.local.registry-mode.yaml"; then
  ok "the registry-mode layer self-updates from public ghcr.io and may patch itself"
else bad "the registry-mode layer self-updates from public ghcr.io and may patch itself" "$(cat "$SHARE_DIR_T/values.local.registry-mode.yaml")"; fi
# 🚨 In registry mode the chart's installByDefault ["Plugins/*"] must be CLEARED (2026-10-27, the
# maintainer's laptop): the registry source is named by its URL, so the chart's pattern names a
# source the install does not have — `[DefaultInstall] 1 InstallByDefault pattern(s) name a source
# this installation does not have: [Plugins/*]` — while overriding the wizard's own selection with a
# wildcard. The wizard's manifest decides; self-registry mode keeps its per-source "<name>/*" entries.
# shellcheck disable=SC2016  # the single-quoted text is the source being searched for
case "$body" in
  *'mode_args=(-f "$share_dir/values.local.registry-mode.yaml" -f "$REGISTRY_FILE" --set-json '"'"'pluginCatalog.installByDefault=[]'"'"')'*)
    ok "helm_deploy clears the chart's installByDefault in registry mode (the wizard decides)" ;;
  *) bad "helm_deploy clears the chart's installByDefault in registry mode (the wizard decides)" "no --set-json clear in the registry_mode arm: $body" ;;
esac
# shellcheck disable=SC2016  # the single-quoted text is the source being searched for
case "$body" in
  *'--set "pluginCatalog.installByDefault[$i]=$n/*"'*)
    ok "helm_deploy still assembles the per-source installByDefault entries for self-registry mode" ;;
  *) bad "helm_deploy still assembles the per-source installByDefault entries for self-registry mode" "$body" ;;
esac
# The rendered proof, when helm is on PATH (the Linux CI lane installs it; a Mac without helm says
# so rather than passing): with the clear, NO PluginCatalog__InstallByDefault__N key reaches the
# ConfigMap; without it, the chart's own entry does — so the control proves the probe can see one.
if command -v helm >/dev/null 2>&1; then
  CHART="$HERE/../../helm"; REGTEST="$STUBS/registry-mode-values.yaml"
  printf 'pluginCatalog:\n  registryUrl: "https://registry.example"\n' > "$REGTEST"
  render() { helm template memex "$CHART" -f "$CHART/values.yaml" -f "$HERE/../share/values.local.defaults.yaml" -f "$REGTEST" "$@" 2>&1; }
  with_clear="$(render --set-json 'pluginCatalog.installByDefault=[]' | grep -c 'PluginCatalog__InstallByDefault__' || true)"
  without_clear="$(render | grep -c 'PluginCatalog__InstallByDefault__' || true)"
  if [ "$without_clear" -ge 1 ] && [ "$with_clear" -eq 0 ]; then
    ok "rendered: registry mode with the clear emits no InstallByDefault key (control without it: $without_clear)"
  else
    bad "rendered: registry mode with the clear emits no InstallByDefault key" "with clear: $with_clear key(s); without: $without_clear"
  fi
  fh="$(render -f "$HERE/../share/values.local.fleet-host.yaml" --set-json 'pluginCatalog.installByDefault=[]')"
  # (here-strings, not printf | grep -q: under pipefail an early-exiting grep SIGPIPEs the printf)
  if grep -q 'PluginCatalog__InstallByDefault__0: "Plugins/Hosting"' <<<"$fh" \
     && grep -q 'PluginCatalog__InstallPreInstalledPackages: "true"' <<<"$fh"; then
    ok "rendered: the fleet-host template names Plugins/Hosting exactly and keeps the platform baseline"
  else
    bad "rendered: fleet-host template" "$(printf '%s\n' "$fh" | grep -E 'InstallByDefault|InstallPreInstalled' | head -5)"
  fi
else
  printf '  skip  rendered registry-mode InstallByDefault check — helm is not on PATH here (the static check above still ran)\n'
fi
for c in cmd_up cmd_update; do
  body="$(awk -v f="^${c}\\\\(\\\\)" '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
  case "$body" in
    *'registry_mode && image_source="ghcr"'*) ok "${c} defaults to the public GHCR image in registry mode (no Azure sign-in)" ;;
    *) bad "${c} defaults to the public GHCR image in registry mode" "no registry-mode GHCR default in ${c}()" ;;
  esac
done

echo "── Mac host profiles (Doc/Architecture/ReleaseEditions G4) ───────"

prof() { OUT="$(env "$@" MEMEX_LOCAL_HOME="$RHOME" "$CLI" profile 2>&1)"; RC=$?; }
prof MEMEX_PROFILE=mac-studio
case "$RC:$OUT" in 0:*"12 CPU / 32 GiB"*"qwen3.8:27b-mlx"*) ok "mac-studio sizes a 12/32 VM with qwen3.8:27b-mlx" ;;
  *) bad "mac-studio sizes a 12/32 VM with qwen3.8:27b-mlx" "exited $RC: $OUT" ;; esac
case "$OUT" in *"extra:     <none>"*) ok "a mac-studio pulls no extra model: its chat model is qwen3.8 itself" ;;
  *) bad "mac-studio extra model" "$OUT" ;; esac
prof MEMEX_PROFILE=mac-mini
case "$RC:$OUT" in 0:*"6 CPU / 12 GiB"*"qwen3:8b"*) ok "mac-mini sizes a 6/12 VM with qwen3:8b" ;;
  *) bad "mac-mini sizes a 6/12 VM with qwen3:8b" "exited $RC: $OUT" ;; esac
# No profile: the model follows the host's memory — qwen3.8 on Apple Silicon with >= 32 GiB beside the VM
# (a 48 GB Mac with the default 16 GiB VM), the fallback below that line and on Intel.
prof MEMEX_PROFILE= MEMEX_HOST_MEM_GIB=48 MEMEX_HOST_ARCH=arm64
case "$RC:$OUT" in 0:*"8 CPU / 16 GiB"*"qwen3.8:27b-mlx"*) ok "no profile, 48 GB Apple Silicon: 8/16 VM, default chat model qwen3.8:27b-mlx" ;;
  *) bad "no profile, 48 GB Apple Silicon: 8/16 VM, default chat model qwen3.8:27b-mlx" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE= MEMEX_HOST_MEM_GIB=47 MEMEX_HOST_ARCH=arm64
case "$RC:$OUT" in 0:*"model:     qwen3:8b"*) ok "no profile, 47 GB: the fallback qwen3:8b (one GiB under the line)" ;;
  *) bad "no profile, 47 GB: the fallback qwen3:8b" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE= MEMEX_HOST_MEM_GIB=128 MEMEX_HOST_ARCH=x86_64
case "$RC:$OUT" in 0:*"model:     qwen3:8b"*) ok "no profile, Intel Mac: the fallback qwen3:8b (MLX needs Apple Silicon)" ;;
  *) bad "no profile, Intel Mac: the fallback qwen3:8b" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE=mac-mini MEMEX_COLIMA_MEM=20 MEMEX_OLLAMA_MODEL=none
case "$RC:$OUT" in 0:*"6 CPU / 20 GiB"*"model:     none"*) ok "an explicit MEMEX_COLIMA_MEM / MEMEX_OLLAMA_MODEL beats the profile" ;;
  *) bad "an explicit MEMEX_COLIMA_MEM / MEMEX_OLLAMA_MODEL beats the profile" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE=mac-studo
case "$RC:$OUT" in 0:*) bad "an unknown profile refuses (never read as the default)" "exited 0: $OUT" ;;
  *"unknown MEMEX_PROFILE"*) ok "an unknown profile refuses (never read as the default)" ;;
  *) bad "an unknown profile refuses (never read as the default)" "refused without saying why: $OUT" ;; esac

echo "── local model: the host-safe Ollama endpoint, never 403 (2026-11-08) ──"
# REGRESSION for the chat 403 on every install: the portal called http://ollama:11434/v1 and Ollama, bound
# to loopback, refused the Host header `ollama:11434`. ollama_host_admitted states Ollama's rule; the
# endpoint memex-local configures must pass it, and the old one must FAIL it (the negative control that
# proves the rule can say no). `memex-local doctor` re-measures the same pair against the live Ollama.
fn_body() { awk "/^$1\\(\\) \\{/,/^}\$/" "$CLI"; }
lm() { env HOME="$LMH/home" MEMEX_LOCAL_HOME="$LMH/home/.memex-local" MEMEX_LOCAL_LIB=1 PATH="$LMH/stubs:$STUBS:$PATH" "$@"; }
LMH="$(mktemp -d)"; mkdir -p "$LMH/stubs" "$LMH/home/.memex-local"
EP="$(lm bash -c '. "$0"; printf %s "$OLLAMA_ENDPOINT"' "$CLI")"
[ "$EP" = "http://ollama.memex.svc.cluster.local:11434/v1" ] && ok "the portal's Ollama endpoint is the Service's cluster FQDN ($EP)" || bad "endpoint" "$EP"
lm bash -c '. "$0"; ollama_host_admitted "$(url_hostport "$OLLAMA_ENDPOINT")"' "$CLI" \
  && ok "Ollama's loopback Host rule ADMITS the configured endpoint's host (ends in .local)" || bad "endpoint host refused by the rule" "$EP"
lm bash -c '. "$0"; ollama_host_admitted ollama:11434' "$CLI" \
  && bad "the rule admits 'ollama:11434' — the regression this guards would pass unseen" "" \
  || ok "negative control: the rule REFUSES 'ollama:11434' (the 403 every install got)"
for h in 192.168.5.2:11434 localhost:11434 127.0.0.1:11434 ollama.internal:11434 host.lima.internal:11434; do
  lm bash -c '. "$0"; ollama_host_admitted "$1"' "$CLI" "$h" && ok "the rule admits $h" || bad "the rule refuses $h" ""
done
for h in ollama.example.com:11434 8.8.8.8:11434 ollama.memex.svc:11434; do
  lm bash -c '. "$0"; ollama_host_admitted "$1"' "$CLI" "$h" && bad "the rule admits $h" "" || ok "the rule refuses $h"
done
DEF="$HERE/../share/values.local.defaults.yaml"
grep -qx "    OpenAICompatible__Endpoint: \"$EP\"" "$DEF" && ok "the tracked defaults layer carries the same endpoint (existing installs get it on brew upgrade)" \
  || bad "defaults endpoint differs from OLLAMA_ENDPOINT" "$(grep -n OpenAICompatible__Endpoint "$DEF")"
TPL="$HERE/../share/values.local.yaml"
grep -Eq '^ +OpenAICompatible__(Endpoint|Models__0):' "$TPL" && bad "the overlay template still SETS the endpoint/model (an overlay wins over every layer)" "$(grep -n OpenAICompatible "$TPL")" \
  || ok "the overlay template sets no Ollama endpoint or model (the layers below answer them)"
grep -v '^ *#' "$DEF" "$TPL" | grep -q 'ollama:11434' && bad "a values file still names the 403 endpoint ollama:11434" "$(grep -n 'ollama:11434' "$DEF" "$TPL")" || ok "no values file names the short Service name"

# migrate_overlay_llm: the exact stale template lines leave an EXISTING overlay; an owner's choice stays.
OVL="$LMH/home/.memex-local/values.local.yaml"
cat > "$OVL" <<'YAML'
config:
  memex_portal:
    OpenAICompatible__Endpoint: "http://ollama:11434/v1"
    OpenAICompatible__Models__0: "qwen3.6-code"
    OpenAICompatible__DiscoverModels: "false"
ingress:
  host: "memex.localhost"
ollama:
  external:
    enabled: true
    host: "192.168.5.2"
    port: 11434
YAML
OUT="$(lm bash -c 'set -euo pipefail; . "$0"; migrate_overlay_llm; echo MIG_DONE' "$CLI" 2>&1)"
case "$OUT" in *MIG_DONE*) ;; *) bad "migrate_overlay_llm aborted" "$OUT" ;; esac
if ! grep -q 'ollama:11434\|qwen3.6-code\|192.168.5.2' "$OVL" && grep -q 'memex.localhost' "$OVL" && grep -q 'DiscoverModels' "$OVL" \
   && [ -f "$OVL.bak-llm" ]; then ok "an existing overlay loses exactly the stale endpoint, model and gateway lines (copy kept), nothing else"
else bad "overlay migration" "$(cat "$OVL")"; fi
cat > "$OVL" <<'YAML'
config:
  memex_portal:
    OpenAICompatible__Endpoint: "http://10.0.0.7:11434/v1"
    OpenAICompatible__Models__0: "llama3.3:70b"
YAML
cp "$OVL" "$LMH/before"; rm -f "$OVL.bak-llm"
lm bash -c '. "$0"; migrate_overlay_llm' "$CLI" >/dev/null 2>&1
cmp -s "$OVL" "$LMH/before" && [ ! -f "$OVL.bak-llm" ] && ok "an owner's own endpoint and model are never touched (no copy written)" || bad "owner choice clobbered" "$(cat "$OVL")"

# write_llm_auto_layer: model + Ollama's window + chat tier + the VM gateway; the owner's model wins.
cat > "$LMH/stubs/curl" <<'STUB'
#!/bin/bash
case "$*" in
  *api/generate*qwen3.8*|*api/generate*qwen3.6-code*|*api/generate*llama3.3*) echo '{"done":true}' ;;
  *api/generate*)           exit 22 ;;
  # /api/ps: the context each LOADED model is served with — deliberately not the trained window
  # (qwen3.8 is trained for 262144; this fake server serves it at 65536, as a smaller Mac would).
  # The real /api/ps shape: each model's nested `details` object sits BETWEEN its name and its context.
  *api/ps*) echo '{"models":[{"name":"qwen3.8:27b-mlx","size":1,"details":{"format":"safetensors","family":""},"context_length":65536,"runner":"mlx"},{"name":"qwen3.6-code:latest","details":{"family":"qwen"},"context_length":131072},{"name":"llama3.3:70b","details":{},"context_length":32768}]}' ;;
  *api/tags*)               echo "{\"models\":[${FAKE_TAGS:-}]}" ;;
  *) exit 0 ;;
esac
STUB
cat > "$LMH/stubs/colima" <<'STUB'
#!/bin/bash
case "$*" in *"ip route"*) echo "default via 192.168.5.9 dev eth0" ;; esac; exit 0
STUB
chmod +x "$LMH/stubs/"*
AUTO="$LMH/home/.memex-local/llm.auto.yaml"; : > "$OVL"
lm env MEMEX_OLLAMA_MODEL= MEMEX_HOST_MEM_GIB=128 MEMEX_HOST_ARCH=arm64 FAKE_TAGS='{"name":"qwen3.8:27b-mlx"},{"name":"qwen3.6-code:latest"}' \
  bash -c 'set -euo pipefail; . "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
if grep -qx '    OpenAICompatible__Models__0: "qwen3.8:27b-mlx"' "$AUTO" \
   && grep -qx '    AI__Tiers__chat: "Provider/OpenAICompatible/qwen3.8:27b-mlx"' "$AUTO" \
   && grep -qx '    OpenAICompatible__ContextWindowTokens__0__Tokens: "65536"' "$AUTO" \
   && grep -qx '    OpenAICompatible__Models__1: "qwen3.6-code"' "$AUTO" \
   && grep -qx '    OpenAICompatible__ContextWindowTokens__1__Tokens: "131072"' "$AUTO" \
   && grep -qx '    host: "192.168.5.9"' "$AUTO"; then
  ok "llm.auto.yaml: qwen3.8 as Models__0 + default chat tier, the window Ollama SERVES it with (65536, not the trained 262144), the old alias kept as Models__1, the VM's own gateway"
else bad "auto layer (default)" "$(cat "$AUTO")"; fi
printf 'config:\n  memex_portal:\n    OpenAICompatible__Models__0: "llama3.3:70b"\n' > "$OVL"
lm env MEMEX_OLLAMA_MODEL= FAKE_TAGS='{"name":"llama3.3:70b"}' bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -qx '    AI__Tiers__chat: "Provider/OpenAICompatible/llama3.3:70b"' "$AUTO" && grep -qx '    OpenAICompatible__ContextWindowTokens__0__Tokens: "32768"' "$AUTO" \
  && ok "an owner's explicit model is the one the chat tier names and the window is declared for (never clobbered by qwen3.8)" || bad "auto layer (owner model)" "$(cat "$AUTO")"
: > "$OVL"
printf 'config:\n  memex_portal:\n    OpenAICompatible__Models__0: "missing:1b"\n' > "$OVL"
lm env MEMEX_OLLAMA_MODEL= bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -q 'ContextWindowTokens' "$AUTO" && bad "a model Ollama cannot load still got a window declared (a guess)" "$(cat "$AUTO")" \
  || ok "a model Ollama cannot load gets NO window (undeclared and logged, never guessed)"
: > "$OVL"
lm env MEMEX_OLLAMA_MODEL=none MEMEX_EMBED_MODEL=none bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -q 'Models__0\|AI__Tiers' "$AUTO" && bad "MEMEX_OLLAMA_MODEL=none still names a model" "$(cat "$AUTO")" || ok "MEMEX_OLLAMA_MODEL=none names no model"
# …and writes no empty `memex_portal:` (a YAML null that helm merges by DELETING every tracked default).
if command -v helm >/dev/null 2>&1; then
  R="$(helm template memex "$HERE/../../helm" -f "$HERE/../../helm/values.yaml" -f "$DEF" -f "$AUTO" 2>&1)"
  grep -q 'InstanceSetup__TokenCommand: "memex-local setup"' <<<"$R" && grep -q 'OpenAICompatible__Endpoint: "http://ollama.memex.svc.cluster.local' <<<"$R" \
    && ok "a no-model llm.auto.yaml leaves every tracked default in the render" || bad "no-model layer erased the defaults" "$(cat "$AUTO")"
else
  grep -q 'memex_portal:' "$AUTO" && bad "a no-model layer writes memex_portal:" "$(cat "$AUTO")" || ok "a no-model llm.auto.yaml writes no memex_portal map"
fi
# RESIDENCY: the install's declared processing location lands on the local models; undeclared writes none.
RESF="$LMH/home/.memex-local/residency"; : > "$OVL"; rm -f "$RESF"
lm env MEMEX_OLLAMA_MODEL= MEMEX_HOST_MEM_GIB=128 MEMEX_HOST_ARCH=arm64 FAKE_TAGS='{"name":"qwen3.8:27b-mlx"}' bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -q 'DataResidency' "$AUTO" && bad "an undeclared install writes a residency (a guess)" "$(cat "$AUTO")" \
  || ok "an undeclared install writes NO residency line (the models read as Unknown, never a guessed Eu)"
OUT="$(lm env MEMEX_ACCEPT_NONINTERACTIVE=1 bash -c 'set -euo pipefail; . "$0"; residency_onboard ""' "$CLI" 2>&1 </dev/null)"
[ ! -f "$RESF" ] && ok "a headless up with no --residency declares nothing and records nothing (the next interactive up asks)" || bad "headless onboarding wrote a residency" "$(cat "$RESF" 2>/dev/null) $OUT"
lm bash -c 'set -euo pipefail; . "$0"; cmd_residency ch' "$CLI" >/dev/null 2>&1
[ "$(cat "$RESF" 2>/dev/null)" = "CH" ] && ok "memex-local residency ch persists the declaration as CH" || bad "residency CH not persisted" "$(cat "$RESF" 2>/dev/null)"
lm env MEMEX_OLLAMA_MODEL= MEMEX_HOST_MEM_GIB=128 MEMEX_HOST_ARCH=arm64 FAKE_TAGS='{"name":"qwen3.8:27b-mlx"}' bash -c 'set -euo pipefail; . "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -qx '    OpenAICompatible__DataResidency: "CH"' "$AUTO" && ok "a declared CH lands in llm.auto.yaml as OpenAICompatible__DataResidency" || bad "declared CH missing from llm.auto.yaml" "$(cat "$AUTO")"
if command -v helm >/dev/null 2>&1; then
  printf 'config:\n  memex_portal:\n    OpenAICompatible__DataResidency: "Eu"\n' > "$OVL"
  R="$(helm template memex "$HERE/../../helm" -f "$HERE/../../helm/values.yaml" -f "$DEF" -f "$AUTO" -f "$OVL" 2>&1)"
  grep -q 'OpenAICompatible__DataResidency: "Eu"' <<<"$R" && ! grep -q 'OpenAICompatible__DataResidency: "CH"' <<<"$R" \
    && ok "the overlay's own residency still wins over the declaration in the render" || bad "overlay residency did not win" "$(grep -n DataResidency <<<"$R")"
  : > "$OVL"
fi
lm env MEMEX_RESIDENCY=eu MEMEX_OLLAMA_MODEL= MEMEX_HOST_MEM_GIB=128 MEMEX_HOST_ARCH=arm64 FAKE_TAGS='{"name":"qwen3.8:27b-mlx"}' bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -qx '    OpenAICompatible__DataResidency: "Eu"' "$AUTO" && ok "MEMEX_RESIDENCY=eu wins for the run and is spelled Eu (the one EU-only value)" || bad "MEMEX_RESIDENCY" "$(cat "$AUTO")"
for v in 'C1' 'Switzerland-x' 'Switzerland' 'C H'; do
  lm bash -c 'set -euo pipefail; . "$0"; cmd_residency "$1"' "$CLI" "$v" >/dev/null 2>&1 && bad "residency '$v' accepted" "" || ok "residency '$v' is refused"
done
[ "$(cat "$RESF")" = "CH" ] && ok "a refused value leaves the declaration as it was" || bad "refused value changed the declaration" "$(cat "$RESF")"
lm env MEMEX_ACCEPT_NONINTERACTIVE=1 bash -c 'set -euo pipefail; . "$0"; residency_onboard ""' "$CLI" >/dev/null 2>&1 </dev/null
[ "$(cat "$RESF")" = "CH" ] && ok "a later headless up keeps the declaration" || bad "headless up lost the declaration" "$(cat "$RESF")"
lm bash -c 'set -euo pipefail; . "$0"; residency_onboard none' "$CLI" >/dev/null 2>&1
[ -f "$RESF" ] && [ -z "$(tr -d '[:space:]' < "$RESF")" ] && ok "--residency none records 'asked, none declared'" || bad "residency none" "$(cat "$RESF" 2>/dev/null)"
printf 'CH\n' > "$RESF"
# The macOS region SUGGESTS (never applies): an EU member state → Eu, CH → CH, else the ISO code.
mkdir -p "$LMH/defstub"
for pair in 'de_CH:CH' 'en_CH@currency=CHF:CH' 'de_DE:Eu' 'fr_FR:Eu' 'zh_Hans_CN:CN' 'en_US_POSIX:US' 'en:'; do
  loc="${pair%%:*}"; want="${pair#*:}"
  printf '#!/bin/bash\necho "%s"\n' "$loc" > "$LMH/defstub/defaults"; chmod +x "$LMH/defstub/defaults"
  got="$(env PATH="$LMH/defstub:$PATH" MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_HOME="$LMH/home/.memex-local" bash -c '. "$0"; residency_suggest' "$CLI" 2>/dev/null)"
  [ "$got" = "$want" ] && ok "region $loc suggests '${want:-<nothing>}'" || bad "region $loc suggests '$got', want '$want'" ""
done
# An interactive first up ASKS, the region's suggestion as the default; Enter takes it.
rm -f "$RESF"; printf '#!/bin/bash\necho "de_CH"\n' > "$LMH/defstub/defaults"
OUT="$(printf '\n' | env PATH="$LMH/defstub:$PATH" MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_HOME="$LMH/home/.memex-local" HOME="$LMH/home" \
  bash -c '. "$0"; accept_interactive() { return 0; }; residency_onboard ""' "$CLI" 2>&1)"
grep -q 'Where is this machine? (residency, e.g. CH, Eu' <<<"$OUT" && grep -q '\[CH\]' <<<"$OUT" && [ "$(cat "$RESF" 2>/dev/null)" = "CH" ] \
  && ok "the first interactive up asks 'Where is this machine?' with the region's CH as the default" || bad "interactive onboarding" "$OUT / $(cat "$RESF" 2>/dev/null)"
rm -f "$RESF"
OUT="$(printf 'C1\nch\n' | env PATH="$LMH/defstub:$PATH" MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_HOME="$LMH/home/.memex-local" HOME="$LMH/home" \
  bash -c 'set -euo pipefail; . "$0"; accept_interactive() { return 0; }; residency_onboard ""; echo ONBOARD_DONE' "$CLI" 2>&1)"
grep -q 'ONBOARD_DONE' <<<"$OUT" && grep -q "'C1' is not a residency" <<<"$OUT" && [ "$(cat "$RESF")" = "CH" ] \
  && ok "a typo at the prompt is asked again (never the end of the install); the next answer is declared" || bad "interactive typo" "$OUT / $(cat "$RESF" 2>/dev/null)"
OUT="$(printf 'Global\n' | env PATH="$LMH/defstub:$PATH" MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_HOME="$LMH/home/.memex-local" HOME="$LMH/home" \
  bash -c '. "$0"; accept_interactive() { return 0; }; residency_onboard ""' "$CLI" 2>&1)"
[ "$(cat "$RESF")" = "CH" ] && ok "an install that already answered is not asked again" || bad "asked again" "$OUT"
# llm use --residency: any residency accept_check_residency accepts; an invalid one refused; default = the declaration.
mkdir -p "$LMH/llmstub"
cat > "$LMH/llmstub/curl" <<'STUB'
#!/bin/bash
case "$*" in *v1/models*) echo '{"data":[{"id":"qwen3.8:27b-mlx"}]}' ;; *) exit 0 ;; esac
STUB
chmod +x "$LMH/llmstub/curl"
lmu() { env HOME="$LMH/home" MEMEX_LOCAL_HOME="$LMH/home/.memex-local" MEMEX_LOCAL_LIB=1 MEMEX_OLLAMA_MODEL=qwen3.8:27b-mlx PATH="$LMH/llmstub:$LMH/stubs:$STUBS:$PATH" "$@"; }
LLMF="$LMH/home/.memex-local/llm.yaml"
lmu bash -c 'set -euo pipefail; . "$0"; cmd_llm use --residency CH' "$CLI" >/dev/null 2>&1 \
  && grep -qx '    OpenAICompatible__DataResidency: "CH"' "$LLMF" && ok "llm use --residency CH is accepted and written" || bad "llm use --residency CH" "$(cat "$LLMF" 2>/dev/null)"
rm -f "$LLMF"
lmu bash -c 'set -euo pipefail; . "$0"; cmd_llm use --residency C1' "$CLI" >/dev/null 2>&1 \
  && bad "llm use --residency C1 accepted" "" || { [ ! -f "$LLMF" ] && ok "llm use --residency C1 is refused and writes nothing"; } || bad "refused but wrote" ""
OUT="$(lmu bash -c 'set -euo pipefail; . "$0"; cmd_llm use' "$CLI" 2>&1)"
! grep -q '^    OpenAICompatible__DataResidency:' "$LLMF" && grep -q 'residency CH' <<<"$OUT" \
  && ok "llm use without --residency defaults to the install declaration (CH) and copies none into llm.yaml (the auto layer carries it)" || bad "llm use default" "$OUT / $(cat "$LLMF")"
# An llm layer from before this change (llm use wrote "Eu" without a flag) outranks the declaration: named.
printf 'config:\n  memex_portal:\n    OpenAICompatible__DataResidency: "Eu"\n' > "$LLMF"
OUT="$(lm env MEMEX_OLLAMA_MODEL=none bash -c '. "$0"; write_llm_auto_layer' "$CLI" 2>&1)"
grep -q "stamps residency 'Eu', which WINS over the install declaration 'CH'" <<<"$OUT" && ok "a stale llm-layer residency that disagrees with the declaration is named on every up/update" || bad "stale llm residency not named" "$OUT"
printf 'config:\n  memex_portal:\n    OpenAICompatible__DataResidency: "CH"\n' > "$LLMF"
OUT="$(lm env MEMEX_OLLAMA_MODEL=none bash -c '. "$0"; write_llm_auto_layer' "$CLI" 2>&1)"
grep -q 'WINS over' <<<"$OUT" && bad "an agreeing llm-layer residency is warned about" "$OUT" || ok "…and an agreeing one is not"
rm -f "$LLMF"
# backlease accept tokens: the residency defaults to the install declaration.
G="$(lm bash -c 'set -euo pipefail; . "$0"; accept_load; printf %s "$ACC_TOK_RES"' "$CLI" 2>/dev/null)"
[ "$G" = "CH" ] && ok "backlease accept tokens: the residency defaults to the install declaration (CH)" || bad "backlease residency default" "'$G'"
OUT="$(lm bash -c '. "$0"; cmd_residency' "$CLI" 2>&1)"
grep -q 'residency: CH' <<<"$OUT" && ok "memex-local residency prints the declaration" || bad "residency print" "$OUT"
B="$(fn_body cmd_status)"; grep -q '( cmd_residency ) || warn' <<<"$B" && ok "status prints the declared residency, and an invalid one never aborts the rest of status" || bad "status residency" ""
printf 'C1\n' > "$RESF"
OUT="$(lm bash -c '. "$0"; ( cmd_residency ) || warn "invalid"; echo STATUS_GOES_ON' "$CLI" 2>&1)"
grep -q STATUS_GOES_ON <<<"$OUT" && ok "…proven: a corrupted declaration leaves the status run going" || bad "status aborted" "$OUT"
printf 'CH\n' > "$RESF"
B="$(fn_body cmd_up)"; grep -q 'residency_onboard "$residency"' <<<"$B" && ok "up runs the residency onboarding" || bad "up onboarding" ""
B="$(fn_body cmd_update)"; grep -q 'if \[ -n "$residency" \] || \[ -n "${MEMEX_RESIDENCY:-}" \]; then residency_onboard "$residency"; fi' <<<"$B" \
  && ok "update persists an explicit --residency or MEMEX_RESIDENCY (never asks)" || bad "update residency persistence" ""
lm env MEMEX_RESIDENCY=at bash -c 'set -euo pipefail; . "$0"; residency_onboard ""' "$CLI" >/dev/null 2>&1
[ "$(cat "$RESF")" = "AT" ] && ok "MEMEX_RESIDENCY is persisted by the onboarding (AT)" || bad "env not persisted" "$(cat "$RESF")"
printf 'CH\n' > "$RESF"
# An invalid declaration FAILS the run (never a quiet un-declare through a $( ) that swallowed the die).
printf 'C1\n' > "$RESF"
OUT="$(lm env MEMEX_OLLAMA_MODEL= FAKE_TAGS='{"name":"qwen3.8:27b-mlx"}' bash -c '. "$0"; write_llm_auto_layer; echo WROTE' "$CLI" 2>&1)"
! grep -q WROTE <<<"$OUT" && grep -q "'C1' is not a residency" <<<"$OUT" && ok "a corrupted residency file fails up/update loudly instead of writing the layer as Unknown" || bad "corrupted residency swallowed" "$OUT"
OUT="$(lm env MEMEX_RESIDENCY=x9 bash -c '. "$0"; accept_load; echo LOADED' "$CLI" 2>&1)"
! grep -q LOADED <<<"$OUT" && ok "an invalid MEMEX_RESIDENCY fails the backlease answers too" || bad "invalid env swallowed by accept_load" "$OUT"
# End to end through the CLI's own dispatcher (not the sourced function).
EH="$(mktemp -d)"
OUT="$(env MEMEX_LOCAL_HOME="$EH" HOME="$EH" PATH="$STUBS:$PATH" "$CLI" residency ch 2>&1)"; RC=$?
[ "$RC" = 0 ] && [ "$(cat "$EH/residency" 2>/dev/null)" = "CH" ] && ok "'memex-local residency ch' through main() declares CH" || bad "CLI residency dispatch" "rc=$RC $OUT"
OUT="$(env MEMEX_LOCAL_HOME="$EH" HOME="$EH" PATH="$STUBS:$PATH" "$CLI" residency 2>&1)"
grep -q 'residency: CH' <<<"$OUT" && ok "'memex-local residency' through main() prints it" || bad "CLI residency print" "$OUT"
env MEMEX_LOCAL_HOME="$EH" HOME="$EH" PATH="$STUBS:$PATH" "$CLI" residency C1 >/dev/null 2>&1 && bad "CLI accepted residency C1" "" \
  || { [ "$(cat "$EH/residency")" = "CH" ] && ok "'memex-local residency C1' through main() is refused and changes nothing"; } || bad "CLI refusal changed the file" ""
rm -rf "$EH"
rm -f "$RESF"
# The default is not in Ollama (pull failed / Ollama too old): name a model Ollama HAS, never a 404.
lm env MEMEX_OLLAMA_MODEL= MEMEX_HOST_MEM_GIB=128 MEMEX_HOST_ARCH=arm64 FAKE_TAGS='{"name":"qwen3.6-code:latest"}' bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -qx '    OpenAICompatible__Models__0: "qwen3.6-code"' "$AUTO" && ok "a failed qwen3.8 pull keeps the model the install already ran (qwen3.6-code), never one that answers 404" || bad "auto layer names a missing model" "$(cat "$AUTO")"
lm env MEMEX_OLLAMA_MODEL= MEMEX_HOST_MEM_GIB=128 MEMEX_HOST_ARCH=arm64 FAKE_TAGS='{"name":"qwen3:8b"},{"name":"qwen3.6-code:latest"}' bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -qx '    OpenAICompatible__Models__0: "qwen3:8b"' "$AUTO" && ok "…and prefers the fallback qwen3:8b when Ollama has it" || bad "fallback preference" "$(cat "$AUTO")"
# IPv6 literals, bracketed (what a Host header carries) and bare.
for h in '[::1]:11434' '::1'; do
  lm bash -c '. "$0"; ollama_host_admitted "$1"' "$CLI" "$h" && ok "the rule admits $h" || bad "the rule refuses $h" ""
done
# fleet join's MEMEX_SKIP_OLLAMA=1 is honoured by update (Ollama is set up once per join, not twice).
B="$(fn_body cmd_update)"
grep -q 'if \[ "${MEMEX_SKIP_OLLAMA:-}" != "1" \]; then setup_ollama; fi' <<<"$B" && grep -q 'MEMEX_SKIP_OLLAMA=1 cmd_update' "$CLI" \
  && ok "fleet join sets Ollama up once: update honours MEMEX_SKIP_OLLAMA=1" || bad "MEMEX_SKIP_OLLAMA" ""
# helm_deploy layers llm.auto.yaml right after the tracked defaults and before every other layer.
HD="$(awk '/^helm_deploy\(\) \{/,/^}$/' "$CLI")"
grep -A3 -- '-f "$share_dir/values.local.defaults.yaml"' <<<"$HD" | grep -q -- '-f "$LLM_AUTO_FILE"' \
  && grep -q '^  migrate_overlay_llm$' <<<"$HD" && grep -q '^  write_llm_auto_layer$' <<<"$HD" \
  && ok "helm_deploy migrates the overlay, writes llm.auto.yaml, and layers it right after the tracked defaults" || bad "helm_deploy layering" ""

# The chat smoke: the pod's answer decides. A 403 FAILS and names the Host-header cause; 200 passes.
cat > "$LMH/stubs/kubectl" <<'STUB'
#!/bin/bash
case "$*" in
  *"-- env") [ -n "${FAKE_EXEC_FAILS:-}" ] && exit 1
             printf 'OpenAICompatible__Endpoint=%s\nOpenAICompatible__Models__0=qwen3.8:27b-mlx\n' "${FAKE_EP-http://ollama.memex.svc.cluster.local:11434/v1}" ;;
  *"sh -c"*) printf '%s %s\n%s\n' "${FAKE_SMOKE_CODE:-200}" "${FAKE_CURL_RC:-0}" "${FAKE_SMOKE_BODY-{\"choices\":[{\"message\":{\"content\":\"OK\"}}]}}" ;;
esac
exit 0
STUB
chmod +x "$LMH/stubs/kubectl"
OUT="$(lm env FAKE_SMOKE_CODE=200 bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 0 ] && grep -q "HTTP 200" <<<"$OUT" && ok "chat smoke: HTTP 200 from inside the pod passes" || bad "smoke 200" "rc=$RC $OUT"
OUT="$(lm env FAKE_SMOKE_CODE=403 FAKE_EP=http://ollama:11434/v1 bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "refused the Host header 'ollama:11434'" <<<"$OUT" && grep -q "svc.cluster.local" <<<"$OUT" \
  && ok "chat smoke: a 403 FAILS and names Ollama's Host-header check and the endpoint to use" || bad "smoke 403" "rc=$RC $OUT"
for c in 404 500 000; do
  OUT="$(lm env FAKE_SMOKE_CODE=$c bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
  [ "$RC" = 1 ] && ok "chat smoke: HTTP $c fails" || bad "smoke $c passed" "$OUT"
done
OUT="$(lm env FAKE_SMOKE_CODE=200 FAKE_CURL_RC=28 bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "incomplete" <<<"$OUT" && ok "chat smoke: a 200 whose transfer did not finish (curl exit 28) FAILS" || bad "smoke curl rc ignored" "rc=$RC $OUT"
OUT="$(lm env FAKE_SMOKE_CODE=200 FAKE_SMOKE_BODY='{"error":"x"}' bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && ok "chat smoke: a 200 that is not a chat completion (no choices) FAILS" || bad "smoke body ignored" "rc=$RC $OUT"
OUT="$(lm env FAKE_EP= MEMEX_OLLAMA_MODEL=qwen3:8b bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "carries no OpenAICompatible__Endpoint" <<<"$OUT" && ok "chat smoke: a model configured here but invisible in the pod FAILS (never a vacuous skip)" || bad "invisible config skipped" "rc=$RC $OUT"
OUT="$(lm env FAKE_EP= MEMEX_OLLAMA_MODEL=none bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 0 ] && grep -q "skipped" <<<"$OUT" && ok "chat smoke: skips (loudly) only when no model was chosen" || bad "none skip" "rc=$RC $OUT"
SO="$(fn_body setup_ollama)"
grep -q '^  if ! ollama_has_model qwen-small; then' <<<"$SO" && ! grep -q '^  ollama pull' <<<"$SO" \
  && ok "setup_ollama (which update runs) never pulls unconditionally: a model already present is not fetched, a failed pull warns" || bad "unconditional pull in setup_ollama" "$(grep -n 'ollama pull' <<<"$SO")"
grep -q "grep -q 'OLLAMA_KV_CACHE_TYPE' \"\$OLLAMA_PLIST_DST\"" <<<"$SO" && ok "an older memex-local Ollama agent without the tuning is rewritten and restarted" || bad "stale agent tuning" ""
OUT="$(lm env FAKE_EXEC_FAILS=1 bash -c '. "$0"; llm_chat_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && ok "chat smoke: a pod whose environment cannot be read FAILS (never a skipped pass)" || bad "smoke exec failure passed" "rc=$RC $OUT"
OUT="$(lm env FAKE_SMOKE_CODE=403 bash -c '. "$0"; llm_chat_smoke_or_die; echo SURVIVED' "$CLI" 2>&1)"; RC=$?
[ "$RC" != 0 ] && ! grep -q SURVIVED <<<"$OUT" && ok "the smoke GATE stops the command on a 403 (up/update/fleet join fail loudly)" || bad "gate passed a 403" "$OUT"
# Each body is captured FIRST: `awk … | grep -q` under pipefail fails when grep exits early and awk
# takes SIGPIPE on a long function (cmd_update) — green on one runner, red on the next.
for c in cmd_up cmd_update; do
  B="$(fn_body "$c")"
  grep -q '^  llm_chat_smoke_or_die$' <<<"$B" && ok "$c runs the chat smoke gate" || bad "$c has no smoke gate" ""
done
B="$(fn_body cmd_doctor)"
grep -q 'llm_host_probe' <<<"$B" && grep -q 'llm_chat_smoke' <<<"$B" \
  && ok "doctor re-measures the Host rule on the live Ollama and runs the chat smoke" || bad "doctor checks" ""
B="$(fn_body cmd_status)"; grep -q 'llm_chat_smoke' <<<"$B" && ok "status runs the chat smoke" || bad "status smoke" ""
B="$(fn_body cmd_update)"; grep -q 'setup_ollama' <<<"$B" && ok "update pulls the default chat model too (existing installs get qwen3.8)" || bad "update setup_ollama" ""
grep -q '<key>OLLAMA_FLASH_ATTENTION</key><string>1</string><key>OLLAMA_KV_CACHE_TYPE</key><string>q8_0</string>' "$CLI" \
  && ok "memex-local's own Ollama agent carries the tuning env (flash attention, q8_0 KV cache)" || bad "agent tuning env" ""
lm bash -c '. "$0"; version_ge 0.40.14 0.32.12 && ! version_ge 0.31.9 0.32.12 && version_ge 0.32.12 0.32.12' "$CLI" \
  && ok "version_ge orders Ollama releases numerically" || bad "version_ge" ""
echo "── local embeddings: bge-m3 on this Mac's Ollama, never a cloud provider (2026-11-08) ──"
# REGRESSION for "Content indexing is not active on this deployment": no install configured Embedding__*,
# so free-text search fell back to ILIKE and an agent searching by meaning searched until its round ran out.
: > "$OVL"
lm env MEMEX_OLLAMA_MODEL=qwen3:8b FAKE_TAGS='{"name":"qwen3:8b"}' bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
EB="$(awk '/^  memex_portal:/,/^  memex_migration:/' "$AUTO")"; EM="$(awk '/^  memex_migration:/,/^ollama:/' "$AUTO")"
grep -qx '    Embedding__Provider: "Ollama"' <<<"$EB" && grep -qx "    Embedding__Endpoint: \"$EP\"" <<<"$EB" && grep -qx '    Embedding__Model: "bge-m3"' <<<"$EB" \
  && ok "llm.auto.yaml points the PORTAL's embeddings at bge-m3 on this Mac's Ollama (host-safe endpoint)" || bad "portal embedding keys" "$(cat "$AUTO")"
grep -qx '    Embedding__Provider: "Ollama"' <<<"$EM" && grep -qx "    Embedding__Endpoint: \"$EP\"" <<<"$EM" && grep -qx '    Embedding__Model: "bge-m3"' <<<"$EM" \
  && ok "…and the MIGRATION Job names the same model (it sizes the vector columns and runs the backfill)" || bad "migration embedding keys" "$(cat "$AUTO")"
lm env MEMEX_OLLAMA_MODEL=none MEMEX_EMBED_MODEL=none bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
grep -q 'Embedding__\|memex_portal:' "$AUTO" && bad "MEMEX_EMBED_MODEL=none still configures embeddings" "$(cat "$AUTO")" || ok "MEMEX_EMBED_MODEL=none configures no embeddings (and writes no empty memex_portal map)"
lm env MEMEX_OLLAMA_MODEL=none bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
if ! grep -q 'Models__0' "$AUTO" && grep -qx '    Embedding__Model: "bge-m3"' "$AUTO"; then ok "no chat model still keeps local embeddings"; else bad "no-chat layer names a chat model or lost embeddings" "$(cat "$AUTO")"; fi
if command -v helm >/dev/null 2>&1; then
  lm env MEMEX_OLLAMA_MODEL=qwen3:8b FAKE_TAGS='{"name":"qwen3:8b"}' bash -c '. "$0"; write_llm_auto_layer' "$CLI" >/dev/null 2>&1
  R="$(helm template memex "$HERE/../../helm" -f "$HERE/../../helm/values.yaml" -f "$DEF" -f "$AUTO" 2>&1)"
  [ "$(grep -c 'Embedding__Model: "bge-m3"' <<<"$R")" -ge 2 ] && grep -q 'InstanceSetup__TokenCommand: "memex-local setup"' <<<"$R" \
    && ok "the render carries Embedding__Model=bge-m3 in BOTH the portal and migration ConfigMaps, defaults intact" || bad "render embedding" "$(grep -n Embedding__ <<<"$R")"
fi
for m in bge-m3 nomic-embed-text mxbai-embed-large; do
  d="$(lm bash -c '. "$0"; embed_model_dimensions "$1"' "$CLI" "$m")"; [ -n "$d" ] && ok "the platform sizes $m (${d}-dim)" || bad "unsized $m" ""
done
OUT="$(lm env MEMEX_EMBED_MODEL=all-minilm bash -c '. "$0"; ensure_embed_model; echo SURVIVED' "$CLI" 2>&1)"; RC=$?
[ "$RC" != 0 ] && ! grep -q SURVIVED <<<"$OUT" && grep -q "not an embedding model the platform sizes" <<<"$OUT" \
  && ok "an embedding model the platform cannot size is REFUSED (never a 1536-dim schema every insert then fails)" || bad "unsized model accepted" "$OUT"
grep -q '^  ensure_embed_model$' <<<"$(fn_body setup_ollama)" && ok "setup_ollama (up, update, fleet join) pulls the embedding model" || bad "no embed pull" ""
grep -q 'memex_migration:' <<<"$(fn_body cmd_llm)" && bad "'llm use' still writes a provider-less migration endpoint" "" || ok "'llm use' leaves embeddings to the per-install layer (no provider-less migration endpoint)"

# The embedding smoke: a real vector of the platform's width, from inside the pod.
cat > "$LMH/stubs/kubectl" <<'STUB'
#!/bin/bash
case "$*" in
  *"-- env") [ -n "${FAKE_EXEC_FAILS:-}" ] && exit 1
             printf 'Embedding__Provider=%s\nEmbedding__Endpoint=%s\nEmbedding__Model=%s\n' "${FAKE_EPROV-Ollama}" "${FAKE_EP-http://ollama.memex.svc.cluster.local:11434/v1}" "${FAKE_EMODEL-bge-m3}" ;;
  *"sh -c"*) printf '%s\n%s\n' "${FAKE_SMOKE_CODE:-200}" "${FAKE_SMOKE_BODY-{\"data\":[{\"embedding\":[$(seq -s, 1 ${FAKE_DIM:-1024})]}]}}" ;;
esac
exit 0
STUB
chmod +x "$LMH/stubs/kubectl"
OUT="$(lm bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 0 ] && grep -q "1024-dim vector from bge-m3" <<<"$OUT" && ok "embedding smoke: a 1024-dim bge-m3 vector from inside the pod passes" || bad "embed smoke 200" "rc=$RC $OUT"
OUT="$(lm env FAKE_DIM=768 bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "sizes its vector columns 1024" <<<"$OUT" && ok "embedding smoke: a vector of the WRONG width FAILS" || bad "embed width" "rc=$RC $OUT"
for c in 403 404 500 000; do
  OUT="$(lm env FAKE_SMOKE_CODE=$c bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
  [ "$RC" = 1 ] && ok "embedding smoke: HTTP $c fails" || bad "embed smoke $c passed" "$OUT"
done
OUT="$(lm env FAKE_SMOKE_BODY='{"error":"x"}' bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && ok "embedding smoke: a 200 with no vector FAILS" || bad "embed body ignored" "rc=$RC $OUT"
OUT="$(lm env FAKE_EPROV=AzureFoundry bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "embeds on this Mac" <<<"$OUT" && ok "embedding smoke: a CLOUD embedding provider on a Mac install FAILS" || bad "cloud provider passed" "rc=$RC $OUT"
OUT="$(lm env FAKE_EPROV=OpenAICompatible FAKE_EP=https://api.openai.com/v1 bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "is not on this Mac" <<<"$OUT" && ok "embedding smoke: an OpenAI-compatible provider pointed at a CLOUD host FAILS (the endpoint decides, not the name)" || bad "cloud endpoint passed" "rc=$RC $OUT"
OUT="$(lm env FAKE_EP=http://192.168.5.2:11434/v1 bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "embedding smoke: the VM's gateway to the Mac is on this Mac" || bad "gateway endpoint refused" "rc=$RC $OUT"
OUT="$(lm env FAKE_EMODEL=custom-embed FAKE_DIM=1024 bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "sizes its vector columns 1536" <<<"$OUT" && ok "embedding smoke: an overlay model the table does not size is held to the platform's 1536 default (never a skipped width check)" || bad "unsized overlay model passed" "rc=$RC $OUT"
OUT="$(lm env FAKE_EMODEL=custom-embed FAKE_DIM=1536 bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "embedding smoke: …and passes when it returns 1536" || bad "unsized 1536 refused" "rc=$RC $OUT"
OUT="$(lm env FAKE_EP= bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && grep -q "carries no Embedding__Endpoint" <<<"$OUT" && ok "embedding smoke: configured here but invisible in the pod FAILS" || bad "invisible embed skipped" "rc=$RC $OUT"
OUT="$(lm env FAKE_EP= MEMEX_EMBED_MODEL=none bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 0 ] && grep -q "OFF" <<<"$OUT" && ok "embedding smoke: skips (loudly) only on MEMEX_EMBED_MODEL=none" || bad "embed none skip" "rc=$RC $OUT"
OUT="$(lm env FAKE_EXEC_FAILS=1 bash -c '. "$0"; embed_smoke' "$CLI" 2>&1)"; RC=$?
[ "$RC" = 1 ] && ok "embedding smoke: a pod whose environment cannot be read FAILS" || bad "embed exec failure passed" "rc=$RC $OUT"
for c in cmd_up cmd_update; do
  grep -q '^  embed_smoke_or_die$' <<<"$(fn_body "$c")" && ok "$c runs the embedding smoke gate" || bad "$c has no embedding gate" ""
done
grep -q 'embed_smoke' <<<"$(fn_body cmd_doctor)" && grep -q 'embed_smoke' <<<"$(fn_body cmd_status)" && ok "doctor and status run the embedding smoke" || bad "doctor/status embed" ""
rm -rf "$LMH"

echo "── the OWNER: collected at onboarding, the instance's global administrator (2026-11-08) ──"
OWH="$(mktemp -d)"; mkdir -p "$OWH/home/.memex-local" "$OWH/stubs"
# `defaults` answers the managed preferences from FAKE_PREF_<Key>; nothing set = no preference.
cat > "$OWH/stubs/defaults" <<'STUB'
#!/bin/bash
k="${!#}"; v="FAKE_PREF_$k"; [ -n "${!v:-}" ] && { echo "${!v}"; exit 0; }; exit 1
STUB
chmod +x "$OWH/stubs/defaults"
# Stubbed from the FIRST owner test on: a developer's own Platform SSO registration must never become a test's owner.
# `app-sso platform -s` answers from FAKE_PSSO (a file holding its output) or fails with FAKE_PSSO_FAILS.
cat > "$OWH/stubs/app-sso" <<'STUB'
#!/bin/bash
[ -n "${FAKE_PSSO_FAILS:-}" ] && { echo "app-sso: error" >&2; exit 1; }
[ -n "${FAKE_PSSO:-}" ] && { cat "$FAKE_PSSO"; exit 0; }
printf 'Device Configuration:\n (null)\n\nLogin Configuration:\n (null)\n\nUser Configuration:\n (null)\n'
STUB
chmod +x "$OWH/stubs/app-sso"
ow() { env HOME="$OWH/home" MEMEX_LOCAL_HOME="$OWH/home/.memex-local" MEMEX_LOCAL_LIB=1 PATH="$OWH/stubs:$STUBS:$PATH" "$@"; }
OOV="$OWH/home/.memex-local/values.local.yaml"
seed_overlay() { cat > "$OOV" <<'YAML'
secrets:
  memex_portal:
    OpenAICompatible__ApiKey: "ollama"
config:
  memex_portal:
    Portal__InstanceName: "Local"
ingress:
  host: "memex.localhost"
YAML
}
seed_overlay
OUT="$(ow env MEMEX_OWNER=alice MEMEX_OWNER_EMAIL=alice@example.com bash -c 'set -euo pipefail; . "$0"; ensure_owner_admin "" "" require; echo DONE' "$CLI" 2>&1)"
if grep -q DONE <<<"$OUT" \
   && awk '/^config:/{c=1} /^ingress:/{c=0} c' "$OOV" | grep -qx '    Auth__GlobalAdmins__0: "alice"' \
   && awk '/^config:/{c=1} /^ingress:/{c=0} c' "$OOV" | grep -qx '    Instance__Owner__Id: "alice"' \
   && awk '/^config:/{c=1} /^ingress:/{c=0} c' "$OOV" | grep -qx '    Instance__Owner__Email: "alice@example.com"' \
   && ! awk '/^secrets:/{c=1} /^config:/{c=0} c' "$OOV" | grep -q 'Owner\|GlobalAdmins' \
   && grep -qx '    Portal__InstanceName: "Local"' "$OOV" && grep -qx '  host: "memex.localhost"' "$OOV"; then
  ok "the owner lands in config.memex_portal of the instance's OWN overlay (admin key + id + e-mail), secrets and the rest untouched"
else bad "owner write" "$OUT
$(cat "$OOV")"; fi
cp "$OOV" "$OWH/before"
ow env MEMEX_OWNER=alice MEMEX_OWNER_EMAIL=alice@example.com bash -c '. "$0"; ensure_owner_admin "" "" require' "$CLI" >/dev/null 2>&1
cmp -s "$OOV" "$OWH/before" && ok "a re-run with the same owner changes nothing (idempotent)" || bad "owner re-run changed the overlay" "$(diff "$OWH/before" "$OOV")"
ow bash -c '. "$0"; ensure_owner_admin "" "" require' "$CLI" >/dev/null 2>&1 && cmp -s "$OOV" "$OWH/before" \
  && ok "an unattended re-run with no source keeps the owner the overlay records (no prompt, no failure)" || bad "re-run without source" "$(cat "$OOV")"
OUT="$(ow bash -c '. "$0"; cmd_owner --id bob --email bob@example.com' "$CLI" 2>&1)"
[ "$(grep -c 'Auth__GlobalAdmins__0' "$OOV")" = 1 ] && grep -qx '    Auth__GlobalAdmins__0: "bob"' "$OOV" && grep -qx '    Instance__Owner__Email: "bob@example.com"' "$OOV" \
  && grep -q "was 'alice'" <<<"$OUT" && ok "'memex-local owner --id … --email …' changes the owner in place (one admin key, never a second)" || bad "owner change" "$OUT
$(cat "$OOV")"
seed_overlay
OUT="$(ow bash -c 'set -euo pipefail; . "$0"; ensure_owner_admin "" "" require; echo SURVIVED' "$CLI" 2>&1 </dev/null)"; RC=$?
[ "$RC" != 0 ] && ! grep -q SURVIVED <<<"$OUT" && grep -q "no OWNER" <<<"$OUT" && ! grep -q GlobalAdmins "$OOV" \
  && ok "an UNATTENDED install with no owner FAILS, naming how to give one (never an instance without an administrator)" || bad "ownerless install passed" "rc=$RC $OUT"
OUT="$(ow bash -c '. "$0"; ensure_owner_admin; echo SURVIVED' "$CLI" 2>&1 </dev/null)"
grep -q SURVIVED <<<"$OUT" && grep -q "NO OWNER" <<<"$OUT" && ok "an update of an existing ownerless install warns loudly but does not halt the roll" || bad "update ownerless" "$OUT"
OUT="$(ow env FAKE_PREF_OwnerId=carol FAKE_PREF_OwnerEmail=carol@example.com bash -c '. "$0"; ensure_owner_admin "" "" require' "$CLI" 2>&1 </dev/null)"
grep -qx '    Auth__GlobalAdmins__0: "carol"' "$OOV" && ok "an MDM's managed preferences OwnerId / OwnerEmail supply the owner of an unattended install" || bad "managed pref owner" "$OUT"
for bad_id in 'a b' 'x;rm' ''\''q'\'''; do
  OUT="$(ow bash -c '. "$0"; ensure_owner_admin "$1" "" require; echo SURVIVED' "$CLI" "$bad_id" 2>&1 </dev/null)"
  grep -q SURVIVED <<<"$OUT" && bad "owner id '$bad_id' accepted" "$OUT" || ok "an owner id like '$bad_id' is refused"
done
OUT="$(ow bash -c '. "$0"; ensure_owner_admin carol not-an-email require; echo SURVIVED' "$CLI" 2>&1 </dev/null)"
grep -q SURVIVED <<<"$OUT" && bad "a malformed e-mail accepted" "$OUT" || ok "a malformed owner e-mail is refused"
grep -q 'Auth__GlobalAdmins\|Instance__Owner' "$HERE/../share/"*.yaml && bad "a SHARED values file names the owner (it belongs to each install's own overlay)" "$(grep -n 'Auth__GlobalAdmins\|Instance__Owner' "$HERE/../share/"*.yaml)" \
  || ok "no shared values file (defaults, template, mode layers) names an owner or an admin"
B="$(fn_body cmd_up)"; grep -B1 '^  ensure_overlay$' <<<"$B" | grep -q '^  OWNER_REQUIRED=require-fresh$' && ok "up REQUIRES an owner on a fresh install before anything is deployed" || bad "up owner requirement" ""
# require-fresh: an install never DEPLOYED fails on EVERY ownerless `up` (a re-run of a failed first `up` too —
# an existing overlay is not a deployed install); a deployed ownerless one — which the unattended daemon re-runs
# `up` on at every start — warns, so a running Mac is never halted by its own restart.
seed_overlay; rm -f "$OWH/home/.memex-local/deployed"; : > "$OWH/home/.memex-local/port-forward.sh"   # a standalone port-forward does not count
for attempt in first re-run; do
  OUT="$(ow bash -c '. "$0"; ensure_owner_admin "" "" require-fresh; echo SURVIVED' "$CLI" 2>&1 </dev/null)"
  grep -q SURVIVED <<<"$OUT" && bad "an ownerless never-deployed install came up on the $attempt" "$OUT" || ok "up on a never-deployed install with no owner fails (the $attempt, overlay present)"
done
: > "$OWH/home/.memex-local/deployed"
OUT="$(ow bash -c '. "$0"; ensure_owner_admin "" "" require-fresh; echo SURVIVED' "$CLI" 2>&1 </dev/null)"
grep -q SURVIVED <<<"$OUT" && grep -q "NO OWNER" <<<"$OUT" && ok "up on a DEPLOYED ownerless install (the daemon's restart) warns loudly and carries on" || bad "deployed ownerless up" "$OUT"
rm -f "$OWH/home/.memex-local/deployed"
# A deployed install from BEFORE the marker, on the daemon's boot-time `up` (no cluster yet): its ingress
# certificate — written only past this gate — identifies it, so its restart is not halted.
: > "$OWH/home/.memex-local/memex.localhost.pem"
OUT="$(ow env PATH="$OWH/stubs:$PATH" bash -c 'colima_running() { return 1; }; . "$0"; colima_running() { return 1; }; ensure_owner_admin "" "" require-fresh; echo SURVIVED' "$CLI" 2>&1 </dev/null)"
grep -q SURVIVED <<<"$OUT" && ok "a pre-marker deployed install (its certificate present, no cluster yet) warns at boot and is not halted" || bad "pre-marker deployed install halted" "$OUT"
rm -f "$OWH/home/.memex-local/memex.localhost.pem"
grep -q ': > "$DEPLOYED_MARKER"' <<<"$(fn_body helm_deploy)" && ok "helm_deploy marks the install deployed" || bad "deployed marker" ""
B="$(fn_body cmd_up)"
[ "$(grep -n '^  ensure_overlay$' <<<"$B" | cut -d: -f1)" -lt "$(grep -n 'setup_tls' <<<"$B" | head -1 | cut -d: -f1)" ] \
  && ok "the certificate that marks a pre-marker install is written only AFTER the owner gate in up" || bad "cert before gate" ""
# The owner is id AND e-mail where one is required: an id without an e-mail fails the install.
seed_overlay
OUT="$(ow env MEMEX_OWNER=erin bash -c '. "$0"; ensure_owner_admin "" "" require; echo SURVIVED' "$CLI" 2>&1 </dev/null)"
grep -q SURVIVED <<<"$OUT" && bad "an install with an owner id but no e-mail came up" "$OUT" || ok "an install whose owner has no e-mail fails (id AND e-mail are the owner)"
# A lone managed OwnerEmail (no OwnerId) was recorded for nobody: never attributed.
seed_overlay
ow env FAKE_PREF_OwnerEmail=nobody@example.com bash -c '. "$0"; cmd_owner --id frank' "$CLI" >/dev/null 2>&1 </dev/null
! grep -q 'nobody@example.com' "$OOV" && ok "a lone managed OwnerEmail without OwnerId is ignored" || bad "lone pref email used" "$(cat "$OOV")"
# A managed preference's OwnerEmail belongs to its OwnerId: a different explicit owner never inherits it.
seed_overlay
OUT="$(ow env FAKE_PREF_OwnerId=carol FAKE_PREF_OwnerEmail=carol@example.com bash -c '. "$0"; cmd_owner --id bob' "$CLI" 2>&1 </dev/null)"
grep -qx '    Auth__GlobalAdmins__0: "bob"' "$OOV" && ! grep -q 'carol@example.com' "$OOV" \
  && ok "an MDM's OwnerEmail (for carol) is never written for a different owner (bob)" || bad "pref email inherited" "$(cat "$OOV")"
echo "── the DEVICE'S WORK IDENTITY: an Intune-managed Mac's Entra account is its owner and sign-in default ──"
cat > "$OWH/psso-registered.txt" <<'TXT'
Time: 2026-11-08 10:23:19 +0000

Device Configuration:
 {
    accountDisplayName = "Example";
    loginUserName = "device-not-a-user@example.com";
}

Login Configuration:
 (null)

User Configuration:
 {
    created = "2026-11-01 08:00:00 +0000";
    loginUserName = "jane.roe@example.com";
    uniqueIdentifier = "00000000-0000-0000-0000-000000000000";
}

SSO Tokens:
 (null)
TXT
OUT="$(ow env FAKE_PREF_UserPrincipalName=jane.roe@example.com bash -c '. "$0"; work_identity' "$CLI" 2>&1)"
[ "$OUT" = "$(printf 'jane.roe@example.com\tmanaged preference UserPrincipalName (com.memex.local)')" ] \
  && ok "work identity: the Intune managed preference UserPrincipalName is read first" || bad "work identity (pref)" "$OUT"
OUT="$(ow env FAKE_PSSO="$OWH/psso-registered.txt" bash -c '. "$0"; work_identity' "$CLI" 2>&1)"
[ "$OUT" = "$(printf 'jane.roe@example.com\tPlatform SSO (app-sso platform -s)')" ] \
  && ok "work identity: a Platform SSO registration's USER loginUserName (never the device block's)" || bad "work identity (PSSO)" "$OUT"
OUT="$(ow env FAKE_PREF_UserPrincipalName=max.muster@example.com FAKE_PSSO="$OWH/psso-registered.txt" bash -c '. "$0"; work_identity' "$CLI" 2>&1)"
[ "${OUT%%$'\t'*}" = "max.muster@example.com" ] && ok "work identity: the managed preference wins over Platform SSO" || bad "work identity order" "$OUT"
OUT="$(ow bash -c '. "$0"; work_identity; echo "[end]"' "$CLI" 2>&1)"
[ "$OUT" = "[end]" ] && ok "work identity: an unregistered Mac (no preference, PSSO all (null)) has none" || bad "work identity (absent)" "$OUT"
OUT="$(ow env FAKE_PSSO_FAILS=1 bash -c 'set -euo pipefail; . "$0"; work_identity; echo "[end]"' "$CLI" 2>&1)"
[ "$OUT" = "[end]" ] && ok "work identity: a failing app-sso is no identity, never an error" || bad "work identity (failure)" "$OUT"
OUT="$(ow env FAKE_PREF_UserPrincipalName='{{userprincipalname}}' bash -c '. "$0"; work_identity; echo "[end]"' "$CLI" 2>&1)"
[ "$OUT" = "[end]" ] && ok "work identity: an Intune token left unsubstituted is no identity" || bad "work identity (raw token)" "$OUT"
seed_overlay
OUT="$(ow env FAKE_PREF_UserPrincipalName=jane.roe@example.com bash -c 'set -euo pipefail; . "$0"; ensure_owner_admin "" "" require; echo DONE' "$CLI" 2>&1 </dev/null)"
grep -q DONE <<<"$OUT" && grep -qx '    Auth__GlobalAdmins__0: "jane.roe"' "$OOV" && grep -qx '    Instance__Owner__Email: "jane.roe@example.com"' "$OOV" \
  && grep -qx '    Authentication__Microsoft__LoginHint: "jane.roe@example.com"' "$OOV" && grep -qx '    Authentication__Microsoft__DomainHint: "example.com"' "$OOV" \
  && ! awk '/^secrets:/{c=1} /^config:/{c=0} c' "$OOV" | grep -q 'Hint' \
  && ok "an unattended install on a managed Mac: the work identity is the owner (id = UPN prefix) and the Microsoft sign-in hint" || bad "work identity owner" "$OUT
$(cat "$OOV")"
cp "$OOV" "$OWH/before"
ow env FAKE_PREF_UserPrincipalName=jane.roe@example.com bash -c '. "$0"; ensure_owner_admin "" "" require' "$CLI" >/dev/null 2>&1 </dev/null
cmp -s "$OOV" "$OWH/before" && ok "the work-identity owner and hint are idempotent" || bad "work identity re-run" "$(diff "$OWH/before" "$OOV")"
seed_overlay
OUT="$(ow env FAKE_PREF_UserPrincipalName=jane.roe@example.com MEMEX_OWNER=bob MEMEX_OWNER_EMAIL=bob@example.com bash -c '. "$0"; ensure_owner_admin "" "" require' "$CLI" 2>&1 </dev/null)"
grep -qx '    Auth__GlobalAdmins__0: "bob"' "$OOV" && ! grep -q 'jane.roe' "$OOV" && ! grep -q 'LoginHint' "$OOV" \
  && ok "an explicit owner wins over the work identity, and no hint names a different person" || bad "explicit owner vs work identity" "$OUT
$(cat "$OOV")"
seed_overlay
OUT="$(ow env FAKE_PREF_UserPrincipalName=jane.roe@example.com bash -c '. "$0"; cmd_owner --id bob --email bob@example.com' "$CLI" 2>&1 </dev/null)"
! grep -q 'jane.roe' "$OOV" && ok "'owner --id bob' on a Mac managed for jane: jane's e-mail is never attributed to bob" || bad "work email inherited" "$(cat "$OOV")"
seed_overlay
OUT="$(ow bash -c '. "$0"; ensure_owner_admin "" "" require; echo SURVIVED' "$CLI" 2>&1 </dev/null)"
! grep -q SURVIVED <<<"$OUT" && ! grep -q 'Hint' "$OOV" && ok "no work identity: the owner rule is unchanged (an unattended install without one fails)" || bad "no identity changed the rule" "$OUT"
seed_overlay
ow env FAKE_PREF_UserPrincipalName=jane.roe@example.com bash -c '. "$0"; ensure_owner_admin "" "" require' "$CLI" >/dev/null 2>&1 </dev/null
OUT="$(ow env FAKE_PREF_UserPrincipalName=jane.roe@example.com bash -c '. "$0"; cmd_owner status' "$CLI" 2>&1 </dev/null)"
grep -q "work identity: jane.roe@example.com (from managed preference UserPrincipalName" <<<"$OUT" && grep -q "sign-in hint: jane.roe@example.com" <<<"$OUT" \
  && ok "'owner status' names the work identity, its source and the sign-in hint" || bad "owner status work identity" "$OUT"
OUT="$(ow bash -c '. "$0"; cmd_owner status' "$CLI" 2>&1 </dev/null)"
grep -q "work identity: none" <<<"$OUT" && ok "'owner status' says when the Mac has no work identity" || bad "owner status none" "$OUT"
# The config section without a memex_portal map, and NOT last in the file: the map goes INSIDE config.
printf 'secrets:\n  memex_portal:\n    X: "1"\nconfig:\n  other: "y"\ningress:\n  host: "memex.localhost"\n' > "$OOV"
ow bash -c '. "$0"; overlay_set_portal_config Auth__GlobalAdmins__0 dave' "$CLI" >/dev/null 2>&1
EXP="$(printf 'secrets:\n  memex_portal:\n    X: "1"\nconfig:\n  other: "y"\n  memex_portal:\n    Auth__GlobalAdmins__0: "dave"\ningress:\n  host: "memex.localhost"')"
[ "$(cat "$OOV")" = "$EXP" ] && ok "a config section with no memex_portal map gets one INSIDE it (never appended under the last section)" || bad "map insertion" "$(cat "$OOV")"
printf 'ingress:\n  host: "memex.localhost"\n' > "$OOV"
ow bash -c '. "$0"; overlay_set_portal_config Auth__GlobalAdmins__0 dave' "$CLI" >/dev/null 2>&1
tail -3 "$OOV" | tr '\n' '|' | grep -q '^config:|  memex_portal:|    Auth__GlobalAdmins__0: "dave"|$' && ok "an overlay with no config section gets one" || bad "no config section" "$(cat "$OOV")"
# A different owner never inherits the previous owner's e-mail.
seed_overlay
ow env MEMEX_OWNER=alice MEMEX_OWNER_EMAIL=alice@example.com bash -c '. "$0"; ensure_owner_admin' "$CLI" >/dev/null 2>&1
ow bash -c '. "$0"; cmd_owner --id bob' "$CLI" >/dev/null 2>&1 </dev/null
grep -qx '    Auth__GlobalAdmins__0: "bob"' "$OOV" && ! grep -q 'alice@example.com' "$OOV" \
  && ok "changing the owner by id alone drops the previous owner's e-mail (never attributed to the new owner)" || bad "email inherited" "$(cat "$OOV")"
# The flags themselves, executed: fleet join --owner/--owner-email, and first-attach --owner/--owner-email.
FA="$HERE/../../backlease/first-attach.sh"
FAO="$(mktemp -d)"; mkdir -p "$FAO/bin"
cat > "$FAO/bin/memex-local" <<STUB
#!/bin/bash
echo "\$*" >> "$FAO/calls"
case "\$*" in "owner status") [ -f "$FAO/has" ] && echo " ok  instance OWNER (global administrator): bob <bob@example.com>" || echo "warn no OWNER"; exit 0 ;; owner\ *) touch "$FAO/has"; exit 0 ;; esac
exit 0
STUB
chmod +x "$FAO/bin/memex-local"
FA_ARGS=(--api https://ops.example.test/api/builds --registry r.example.test --vault v --worker-secret w --pull-secret p --pull-user u --version 1.2.3
  --tester "sha256:$(printf 'a%.0s' $(seq 64))" --portal "sha256:$(printf 'b%.0s' $(seq 64))" --runner "sha256:$(printf 'c%.0s' $(seq 64))")
OUT="$(PATH="$FAO/bin:$PATH" bash "$FA" "${FA_ARGS[@]}" </dev/null 2>&1)"; RC=$?
[ "$RC" = 2 ] && grep -q "this Mac has no OWNER" <<<"$OUT" && ! grep -q "^owner --" "$FAO/calls" \
  && ok "first-attach (executed) refuses a Mac with no owner before any change" || bad "first-attach ownerless" "rc=$RC $OUT"
OUT="$(PATH="$FAO/bin:$PATH" MEMEX_HEADLESS=1 bash "$FA" "${FA_ARGS[@]}" --owner bob --owner-email bob@example.com </dev/null 2>&1)"
grep -qx 'owner --id bob --email bob@example.com' "$FAO/calls" && grep -q "owner: bob" <<<"$OUT" \
  && ok "first-attach --owner/--owner-email (executed) records the owner through memex-local and names who answers the opt-in" || bad "first-attach --owner" "$OUT
$(cat "$FAO/calls")"
# an owner line FOLLOWED by more output (the real CLI names the work identity next): `status | grep -q` under pipefail
# reads that as 141 (SIGPIPE) and refused an owned Mac as ownerless (eiger, 2026-11-08)
cat > "$FAO/bin/memex-local" <<STUB
#!/bin/bash
case "\$*" in "owner status") echo " ok  instance OWNER (global administrator): bob <bob@example.com>"; sleep 0.2
  for i in \$(seq 2000); do echo "    this Mac's work identity: bob@example.com (line \$i)"; done; exit 0 ;; esac
exit 0
STUB
OUT="$(PATH="$FAO/bin:$PATH" MEMEX_HEADLESS=1 bash "$FA" "${FA_ARGS[@]}" </dev/null 2>&1)"
! grep -q "has no OWNER" <<<"$OUT" && grep -q "owner: bob" <<<"$OUT" \
  && ok "first-attach reads an owner the CLI reports before further output (no SIGPIPE under pipefail)" || bad "first-attach owner SIGPIPE" "$OUT"
rm -rf "$FAO"
B="$(fn_body cmd_fleet)"; grep -A1 'OWNER_REQUIRED=require' <<<"$B" | grep -q 'ensure_overlay' \
  && [ "$(grep -n 'OWNER_REQUIRED=require' <<<"$B" | head -1 | cut -d: -f1)" -lt "$(grep -n 'cmd_backlease enroll' <<<"$B" | head -1 | cut -d: -f1)" ] \
  && ok "fleet join collects the owner FIRST — before the opt-in questions that owner answers" || bad "fleet join owner order" ""
FA="$HERE/../../backlease/first-attach.sh"
[ "$(grep -n 'owner status' "$FA" | head -1 | cut -d: -f1)" -lt "$(grep -n 'Accept fleet jobs' "$FA" | head -1 | cut -d: -f1)" ] \
  && grep -q 'this Mac has no OWNER' "$FA" && ok "first-attach refuses a Mac with no owner, before the opt-in and before any change" || bad "first-attach owner" ""
rm -rf "$OWH"

echo "── release-channel labels: a seed, resolved to a VERSION (G1) ────"

# docker / az stubs for the channel path ONLY (a separate dir, so no other test sees a docker).
#   FAKE_LABEL_ENV    — the env lines `docker image inspect` reports for the label's image
#   FAKE_LABEL_ID / FAKE_VERSION_ID — the image ids of the label and of the version tag
#   FAKE_PULL_FAILS   — a pull of a ref containing this string fails
CHSTUBS="$(mktemp -d)"
printf '#!/usr/bin/env bash\nexit 0\n' > "$CHSTUBS/az"
cat > "$CHSTUBS/docker" <<'STUB'
#!/usr/bin/env bash
ref="${!#}"
case "$1" in
  pull)
    if [ -n "${FAKE_PULL_FAILS:-}" ]; then case "$ref" in *"$FAKE_PULL_FAILS"*) exit 1 ;; esac; fi
    exit 0 ;;
  image)
    case "$*" in
      *".Config.Env"*) printf '%s\n' "${FAKE_LABEL_ENV:-}" ;;
      *"{{.Id}}"*) case "$ref" in *:3-*|*:3.*-*) printf '%s\n' "${FAKE_LABEL_ID:-sha256:aaa}" ;;
                                  *) printf '%s\n' "${FAKE_VERSION_ID:-sha256:aaa}" ;; esac ;;
    esac
    exit 0 ;;
esac
exit 0
STUB
chmod +x "$CHSTUBS/az" "$CHSTUBS/docker"
chan() { OUT="$(env PATH="$CHSTUBS:$PATH" MEMEX_LOCAL_HOME="$RHOME" "$@" 2>&1)"; RC=$?; }

chan FAKE_LABEL_ENV="PATH=/usr/bin
MESHWEAVER_PLATFORM_VERSION=3.1.10050" "$CLI" channel 3-mac
case "$RC:$OUT" in 0:3.1.10050) ok "a label resolves to the version its image reports" ;;
  *) bad "a label resolves to the version its image reports" "exited $RC: $OUT" ;; esac

chan FAKE_LABEL_ENV="PATH=/usr/bin" "$CLI" channel 3-latest
case "$RC:$OUT" in 0:*) bad "a label whose image reports NO version refuses" "exited 0: $OUT" ;;
  *"reports no MESHWEAVER_PLATFORM_VERSION"*) ok "a label whose image reports NO version refuses" ;;
  *) bad "a label whose image reports NO version refuses" "refused without saying why: $OUT" ;; esac

chan FAKE_LABEL_ENV="MESHWEAVER_PLATFORM_VERSION=3.1.10050" FAKE_LABEL_ID=sha256:aaa FAKE_VERSION_ID=sha256:bbb "$CLI" channel 3-mac
case "$RC:$OUT" in 0:*) bad "a label and a version tag naming DIFFERENT bytes refuse" "exited 0: $OUT" ;;
  *"DIFFERENT images"*) ok "a label and a version tag naming DIFFERENT bytes refuse" ;;
  *) bad "a label and a version tag naming DIFFERENT bytes refuse" "refused without saying why: $OUT" ;; esac

chan FAKE_LABEL_ENV="MESHWEAVER_PLATFORM_VERSION=3.1.10050" FAKE_PULL_FAILS=":3-enterprise" "$CLI" channel 3-enterprise
case "$RC:$OUT" in 0:*) bad "an unwritten (declared-only) label names its cause" "exited 0: $OUT" ;;
  *"declared and written"*) ok "an unwritten (declared-only) label names its cause" ;;
  *) bad "an unwritten (declared-only) label names its cause" "refused without saying why: $OUT" ;; esac

for notlabel in 3.1.10050 3.1.0-mac staging-abc1234-12 abc1234 main 3-Mac; do
  chan "$CLI" channel "$notlabel"
  case "$RC:$OUT" in 0:*) bad "'$notlabel' is refused as a channel label" "exited 0: $OUT" ;;
    *"not a channel label"*) ok "'$notlabel' is refused as a channel label" ;;
    *) bad "'$notlabel' is refused as a channel label" "refused without saying why: $OUT" ;; esac
done
for label in 3-latest 3.1-latest 3.0.0-latest 3-mac 3-enterprise 3.1-stable; do
  chan FAKE_LABEL_ENV="MESHWEAVER_PLATFORM_VERSION=3.1.10050" "$CLI" channel "$label"
  case "$RC" in 0) ok "'$label' is accepted as a channel label" ;;
    *) bad "'$label' is accepted as a channel label" "exited $RC: $OUT" ;; esac
done
rm -rf "$CHSTUBS"

# Static: up/update hand the channel to the pull, and --tag (an explicit version) clears it.
for c in cmd_up cmd_update; do
  body="$(awk -v f="^${c}\\\\(\\\\)" '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
  # shellcheck disable=SC2016  # the single-quoted text is the source being searched for
  case "$body" in
    *'--channel) shift; channel='*'--tag) shift; acr_tag='*'channel="" ;;'*'image_pull_acr "$acr_tag" "$channel"'*)
      ok "${c} accepts --channel, lets --tag override it, and resolves it in the pull" ;;
    *) bad "${c} accepts --channel, lets --tag override it, and resolves it in the pull" "not wired in ${c}()" ;;
  esac
done
# Static: the autoroll launchd job carries the channel, or the watcher would silently follow `main`.
# shellcheck disable=SC2016  # a literal '${CHANNEL}' is the text being searched for
case "$(cat "$CLI")" in
  *'<key>MEMEX_CHANNEL</key><string>${CHANNEL}</string>'*) ok "autoroll's launchd job carries MEMEX_CHANNEL" ;;
  *) bad "autoroll's launchd job carries MEMEX_CHANNEL" "the plist never sets it — the watcher would follow main" ;;
esac

# The port-forward agent gets the port the CLI chose. launchd does not inherit the invoking shell's
# environment, so MEMEX_PORT=9443 used to start the forward on 8443 while the CLI verified 9443.
# The template must carry the token, install_launchd must fill it, and a render with the same three
# substitutions must leave no token behind — so a token one side has and the other lacks is red.
PLIST_TEMPLATE="$HERE/../share/com.memex.local.plist"
case "$(cat "$PLIST_TEMPLATE")" in
  *'<key>MEMEX_PORT</key>'*'<string>__PORT__</string>'*) ok "the launchd template carries MEMEX_PORT as __PORT__" ;;
  *) bad "the launchd template carries MEMEX_PORT as __PORT__" "launchd would start the forward on the default port" ;;
esac
install_body="$(awk -v f='^install_launchd\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
# shellcheck disable=SC2016  # a literal '${HOST_PORT}' is the text being searched for
case "$install_body" in
  *'-e "s|__PORT__|${HOST_PORT}|g"'*) ok "install_launchd substitutes __PORT__ with the chosen HOST_PORT" ;;
  *) bad "install_launchd substitutes __PORT__ with the chosen HOST_PORT" "not in install_launchd(): ${install_body}" ;;
esac
# shellcheck disable=SC2016  # a literal '${share}' is the text being searched for
case "$install_body" in
  *'-e "s|__SHARE__|${share}|g"'*) ok "install_launchd substitutes __SHARE__ so the agent can run the toolchain resolver" ;;
  *) bad "install_launchd substitutes __SHARE__" "not in install_launchd()" ;;
esac
rendered="$(PF_SCRIPT=/x/pf.sh PF_LOG=/x/pf.log HOST_PORT=9443 SHARE=/x/share bash -c '
  sed -e "s|__SCRIPT__|${PF_SCRIPT}|g" -e "s|__LOG__|${PF_LOG}|g" -e "s|__PORT__|${HOST_PORT}|g" -e "s|__SHARE__|${SHARE}|g" "$1"' _ "$PLIST_TEMPLATE")"
case "$rendered" in
  *__SCRIPT__*|*__LOG__*|*__PORT__*|*__SHARE__*) bad "the rendered launchd plist has no token left" "$(grep -n -E '__(SCRIPT|LOG|PORT|SHARE)__' <<<"$rendered")" ;;
  *'<key>MEMEX_PORT</key>'*'<string>9443</string>'*) ok "MEMEX_PORT=9443 reaches the rendered launchd agent" ;;
  *) bad "MEMEX_PORT=9443 reaches the rendered launchd agent" "$rendered" ;;
esac

echo "── toolchain: ONE manifest, floors, existing-first then vendored (share/tools.sh) ──"

# The manifest the CLI, the .pkg build and the .pkg postinstall all read, pinned against the formula:
# every `direct` tool row is a depends_on, every depends_on is a tool row. Lima is `indirect` (brew's
# colima depends on it) and so is NOT expected in the formula.
MANIFEST="$HERE/../share/tools.manifest"
FORMULA="$HERE/../Formula/memex-local.rb"
formula_deps="$(grep -E '^  depends_on "' "$FORMULA" | sed 's/^  depends_on "\([^"]*\)".*/\1/' | sort)"   # the LINE form only — a depends_on quoted in a comment is not a dependency
manifest_direct="$(awk '$1 == "tool" && $5 == "direct" { print $4 }' "$MANIFEST" | sort)"
if [ "$formula_deps" = "$manifest_direct" ]; then ok "the formula's depends_on set IS the manifest's direct tool set (no drift)"
else bad "formula depends_on == manifest direct tools" "formula: $(printf '%s' "$formula_deps" | tr '\n' ' ') / manifest: $(printf '%s' "$manifest_direct" | tr '\n' ' ')"; fi
for name in $(awk '$1 == "tool" { print $2 }' "$MANIFEST"); do
  floor="$(awk -v n="$name" '$1 == "tool" && $2 == n { print $6 }' "$MANIFEST")"
  case "$floor" in -|[0-9]*.[0-9]*.[0-9]*) ok "manifest: $name has a floor ($floor)" ;; *) bad "manifest: $name has a floor" "got '$floor'" ;; esac
done
# preflight's `need` list must be manifest tools (the floors apply to every tool preflight requires).
pre_body="$(awk -v f='^preflight\\(\\)' '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
for t in colima kubectl helm mkcert ollama; do
  case "$pre_body" in *"need $t "*) ok "preflight needs $t (a manifest tool, floor-checked)" ;; *) bad "preflight needs $t" "$pre_body" ;; esac
done
case "$pre_body" in *"tools_activate"*) ok "preflight activates the vendored fallbacks the resolution chose" ;; *) bad "preflight calls tools_activate" "$pre_body" ;; esac

# The resolver, in the four states the maintainer named. Stubs print each tool's real version banner.
TT="$(mktemp -d)"
TEX="$TT/existing"; TPRE="$TT/prefix"; mkdir -p "$TEX" "$TPRE/libexec/vendor"
# 🚨 The resolver scans EVERY directory on PATH, so a PATH ending in /usr/bin would let a RUNNER's own
# kubectl/helm (an Ubuntu runner ships /usr/bin/kubectl 1.37.1) satisfy a floor the fixture meant to
# fail. The tests therefore run with the fixtures plus a directory holding ONLY the utilities the
# code needs, linked from the system — never the system directories themselves.
TSYS="$TT/sys"; mkdir -p "$TSYS"
for u in bash sh awk cut head tail grep sed tr mktemp ln rm mkdir dirname basename readlink ls wc cat date id uname stat sort env sleep kill chmod cp install touch true false printf nohup openssl curl; do
  for d in /usr/bin /bin /usr/sbin /opt/homebrew/bin /usr/local/bin; do [ -x "$d/$u" ] && { ln -sf "$d/$u" "$TSYS/$u"; break; }; done
done
tstub() { printf '#!/bin/bash\nprintf "%%s\\n" "%s"\n' "$3" > "$1/$2"; chmod +x "$1/$2"; }
tvend() { mkdir -p "$TPRE/libexec/vendor/$1/bin"; tstub "$TPRE/libexec/vendor/$1/bin" "$2" "$3"; }
tvend colima colima "colima version 0.10.3"; tvend lima limactl "limactl version 2.2.1"; tvend kubectl kubectl "Client Version: v1.37.1"
tvend helm helm "v4.3.1+gabc"; tvend mkcert mkcert "v1.4.4"
texisting_all() {
  rm -rf "$TEX"; mkdir -p "$TEX"
  tstub "$TEX" colima "colima version 0.10.3"; tstub "$TEX" limactl "limactl version 2.2.0"; tstub "$TEX" kubectl "Client Version: v1.37.0"
  tstub "$TEX" helm "v4.2.4+g"; tstub "$TEX" mkcert "v1.4.4"; tstub "$TEX" ollama "ollama version is 0.33.2"; tstub "$TEX" socket_vmnet ""
}
# resolve <tool> → the resolver's tab-separated line, with the fake PATH and prefix.
resolve() { OUT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tool_resolve "$2"' _ "$HERE/../share" "$1" 2>&1)"; RC=$?; }
texisting_all
for t in colima lima kubectl helm mkcert ollama socket_vmnet; do resolve "$t"; case "$RC:$OUT" in 0:"$t	existing	$TEX/"*"	ok	") ;; *) bad "all present and satisfying → $t resolves to the EXISTING copy" "rc=$RC: $OUT"; continue ;; esac; done
ok "all present and satisfying → every tool resolves to the existing copy (nothing vendored chosen)"
resolve kubectl; case "$OUT" in *"	1.37.0	1.31.0	ok	") ok "the existing kubectl reports 1.37.0 against the floor 1.31.0" ;; *) bad "kubectl version + floor" "$OUT" ;; esac
# tools_activate → "<selection dir> | <PATH>" ; the selection dir holds one link per resolved tool.
activate() { ACT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; printf "%s|%s" "$MEMEX_TOOLS_SELECTED" "$PATH"' _ "$HERE/../share")"; SEL="${ACT%%|*}"; APATH="${ACT#*|}"; }
activate
[ "$APATH" = "$SEL:$TEX:$TSYS" ] && ok "tools_activate prepends ONE selection directory to PATH" || bad "tools_activate prepends the selection dir" "$APATH"
[ "$(readlink "$SEL/colima")" = "$TEX/colima" ] && [ "$(readlink "$SEL/kubectl")" = "$TEX/kubectl" ] && ok "every selection link points at the EXISTING copy when every tool is existing" || bad "selection links → existing" "$(ls -l "$SEL")"
[ "$(ls "$SEL" | wc -l | tr -d ' ')" = "7" ] && ok "the selection holds exactly the 7 manifest tools" || bad "selection count" "$(ls "$SEL")"
rm -rf "$SEL"
rm -f "$TEX/helm"
resolve helm; case "$RC:$OUT" in 0:"helm	vendored	$TPRE/libexec/vendor/helm/bin/helm	4.3.1	3.16.0	ok	nothing existing") ok "one missing (helm) → the VENDORED copy, 'nothing existing'" ;; *) bad "one missing → vendored" "rc=$RC: $OUT" ;; esac
resolve colima; case "$OUT" in colima*existing*) ok "…and colima (present) still resolves to the existing copy" ;; *) bad "colima unaffected" "$OUT" ;; esac
activate
[ "$(readlink "$SEL/helm")" = "$TPRE/libexec/vendor/helm/bin/helm" ] && [ "$(readlink "$SEL/colima")" = "$TEX/colima" ] && ok "the selection links helm → VENDORED and colima → existing" || bad "selection mixes vendored + existing" "$(ls -l "$SEL")"
rm -rf "$SEL"
# Activating TWICE in one process (up → observability) must not turn the links into self-references.
ACT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; tools_activate; readlink "$MEMEX_TOOLS_SELECTED/colima"; "$MEMEX_TOOLS_SELECTED/colima" version; rm -rf "$MEMEX_TOOLS_SELECTED"' _ "$HERE/../share" 2>&1)"
case "$ACT" in "$TEX/colima"*"colima version 0.10.3"*) ok "a second tools_activate keeps the links pointing at the real executables (no self-reference)" ;; *) bad "second activation self-links" "$ACT" ;; esac
# A CHILD inheriting the parent's selection on PATH gets its OWN directory and never removes the parent's.
ACT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; parent="$MEMEX_TOOLS_SELECTED"; bash -c ". \"$1/tools.sh\"; tools_activate; echo child=\$MEMEX_TOOLS_SELECTED; readlink \"\$MEMEX_TOOLS_SELECTED/helm\"; tools_deactivate" _ "$1"; echo "parent=$parent"; [ -d "$parent" ] && echo parent-kept; rm -rf "$parent"' _ "$HERE/../share" 2>&1)"
child="$(sed -n 's/^child=//p' <<<"$ACT")"; parent="$(sed -n 's/^parent=//p' <<<"$ACT")"
if [ -n "$child" ] && [ "$child" != "$parent" ] && grep -q '/helm$' <<<"$ACT" && grep -q '^parent-kept$' <<<"$ACT"; then
  ok "a child process activating under an inherited selection owns its own dir, links to the real executable, and leaves the parent's dir alone"
else bad "child selection ownership" "$ACT"; fi
# The selection must be FIRST even when PATH already carries it later (an agent prepended Homebrew's dirs).
ACT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; sel="$MEMEX_TOOLS_SELECTED"; export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; tools_activate; printf "%s|%s" "$sel" "$PATH"; rm -rf "$sel"' _ "$HERE/../share")"
sel="${ACT%%|*}"; apath="${ACT#*|}"
case "$apath" in "$sel:/opt/homebrew/bin:/usr/local/bin:$TEX:$TSYS") ok "re-activation moves the selection dir back to the FRONT of PATH (once, not twice)" ;; *) bad "selection first on re-activation" "$apath" ;; esac
# A stale fallback link in <prefix>/bin AHEAD on PATH must not win: the selection dir precedes everything.
mkdir -p "$TPRE/bin"; ln -sfn "$TPRE/libexec/vendor/colima/bin/colima" "$TPRE/bin/colima"
ACT="$(env PATH="$TPRE/bin:$TEX:$TSYS" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; command -v colima; rm -rf "$MEMEX_TOOLS_SELECTED"' _ "$HERE/../share")"
case "$ACT" in */memex-tools.*/colima) ok "with a stale fallback FIRST on PATH, 'colima' still resolves through the selection (→ the existing copy)" ;; *) bad "selection beats a stale fallback" "$ACT" ;; esac
rm -rf "${TPRE:?}/bin"
tstub "$TEX" kubectl "Client Version: v1.20.0"
resolve kubectl; case "$RC:$OUT" in 0:"kubectl	vendored	"*"	1.37.1	1.31.0	ok	existing $TEX/kubectl is 1.20.0, below the floor 1.31.0") ok "one below the floor (kubectl 1.20.0) → the vendored copy, and the note says why" ;; *) bad "below floor → vendored with reason" "rc=$RC: $OUT" ;; esac
activate
[ "$(readlink "$SEL/kubectl")" = "$TPRE/libexec/vendor/kubectl/bin/kubectl" ] && ok "a below-floor existing kubectl EARLIER on PATH is overridden: the selection links the vendored one" || bad "selection overrides below-floor existing" "$(ls -l "$SEL")"
rm -rf "$SEL"
tstub "$TEX" kubectl "no version here"
resolve kubectl; case "$OUT" in *"reports no version"*) ok "an existing copy that reports no version is passed over, and the note says so" ;; *) bad "no-version existing passed over" "$OUT" ;; esac
# A VENDORED copy that prints no version is damaged: never substituted by the pin, never activated.
tvend kubectl kubectl ""
resolve kubectl; case "$RC:$OUT" in 1:"kubectl	existing	$TEX/kubectl	?	1.31.0	no-version	"*"vendored $TPRE/libexec/vendor/kubectl/bin/kubectl reports no version (damaged or incompatible — not activated)") ok "a vendored copy reporting NO version is refused (not read as its pin), exit 1, the note says why" ;; *) bad "vendored no-version refused" "rc=$RC: $OUT" ;; esac
tvend kubectl kubectl "Client Version: v1.37.1"
texisting_all; rm -f "$TEX/ollama"
resolve ollama; case "$RC:$OUT" in 1:"ollama	missing	-	-	0.5.0	missing	not on PATH and not vendored — brew install ollama") ok "missing with nothing vendored → kind missing, exit 1, the brew formula named" ;; *) bad "missing, not vendored" "rc=$RC: $OUT" ;; esac
tstub "$TEX" helm "v3.2.0"; rm -rf "$TPRE/libexec/vendor/helm"
resolve helm; case "$RC:$OUT" in 1:"helm	existing	$TEX/helm	3.2.0	3.16.0	below-floor	existing $TEX/helm is 3.2.0, below the floor 3.16.0") ok "below the floor with nothing vendored → verdict below-floor, exit 1" ;; *) bad "below floor, nothing vendored" "rc=$RC: $OUT" ;; esac
tvend helm helm "v4.3.1+gabc"
# A below-floor copy FIRST on PATH must not hide a satisfying one behind it; the skipped one is named.
texisting_all; TEX2="$TT/existing2"; mkdir -p "$TEX2"; tstub "$TEX" helm "v3.2.0"; tstub "$TEX2" helm "v4.2.4+g"
OUT="$(env PATH="$TEX:$TEX2:$TSYS" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tool_resolve helm' _ "$HERE/../share" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:"helm	existing	$TEX2/helm	4.2.4	3.16.0	ok	skipped: existing $TEX/helm is 3.2.0, below the floor 3.16.0") ok "helm 3.2.0 first and 4.2.4 second on PATH → the satisfying second copy is chosen, the skipped one named" ;; *) bad "scan past a below-floor first copy" "rc=$RC: $OUT" ;; esac
rm -rf "$TEX2"; texisting_all
# A re-run resolves identically (pure: no state is written by resolving).
resolve helm; first="$OUT"; resolve helm; [ "$OUT" = "$first" ] && ok "resolving twice gives the same answer (the resolver writes nothing)" || bad "resolver pure" "$first vs $OUT"
# `need` through the CLI's own definition: a tool below its floor is refused BY NAME with floor + remedy.
texisting_all; tstub "$TEX" helm "v3.2.0"; rm -rf "$TPRE/libexec/vendor/helm"
OUT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_LIB=1 bash -c '. "$1"; need helm helm' _ "$CLI" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "need refuses a tool below its floor" "exited 0: $OUT" ;;
  *"required tool 'helm' is below-floor"*"floor 3.16.0"*"brew install helm"*"Memex installer, which carries 4.3.1"*) ok "need refuses helm 3.2.0 by name, with the floor, the brew remedy and the installer's pinned version" ;;
  *) bad "need's refusal names floor + remedies" "rc=$RC: $OUT" ;; esac
OUT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_LIB=1 bash -c '. "$1"; need colima colima && echo accepted' _ "$CLI" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:accepted) ok "need accepts an existing tool at/above its floor" ;; *) bad "need accepts a satisfying tool" "rc=$RC: $OUT" ;; esac
# A name no runner has (an Ubuntu runner HAS /usr/bin/docker, which made this pass vacuously once).
OUT="$(env PATH="$TEX:$TSYS" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_LIB=1 bash -c '. "$1"; need frobnicator-xyz frobnicator' _ "$CLI" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "need still refuses a non-manifest tool that is absent" "exited 0" ;; *"required tool 'frobnicator-xyz' not found"*"brew install frobnicator"*) ok "need keeps the presence-only check for a non-manifest tool" ;; *) bad "need non-manifest" "$OUT" ;; esac

# `doctor` prints the table: per tool, which copy, from where, which version, whether it met the floor.
texisting_all; rm -f "$TEX/helm"; tvend helm helm "v4.3.1+gabc"; tstub "$TEX" kubectl "Client Version: v1.20.0"
# doctor also probes the cluster: the fake colima exits 0 and kubectl answers anything, so it runs through.
OUT="$(env PATH="$TEX:$STUBS:$TSYS" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_HOME="$TT/home" "$CLI" doctor 2>&1)"; RC=$?
case "$OUT" in *"TOOL          USES      VERSION   FLOOR    VERDICT     FROM"*) ok "doctor prints the toolchain table header" ;; *) bad "doctor table header" "$OUT" ;; esac
case "$OUT" in *"colima        existing  0.10.3    0.8.0    ok          $TEX/colima"*) ok "doctor: colima — existing, 0.10.3, floor 0.8.0, ok, from the existing path" ;; *) bad "doctor colima row" "$OUT" ;; esac
case "$OUT" in *"helm          vendored  4.3.1     3.16.0   ok          $TPRE/libexec/vendor/helm/bin/helm  (nothing existing)"*) ok "doctor: helm — vendored, 4.3.1, from libexec/vendor, because nothing existing" ;; *) bad "doctor helm row" "$OUT" ;; esac
# The suite's own stub kubectl (STUBS, no version) is also on this PATH and is named after the 1.20.0 copy.
case "$OUT" in *"kubectl       vendored  1.37.1    1.31.0   ok "*"(existing $TEX/kubectl is 1.20.0, below the floor 1.31.0"*) ok "doctor: kubectl — vendored over an existing 1.20.0, the reason printed" ;; *) bad "doctor kubectl row" "$OUT" ;; esac
case "$OUT" in *"every tool resolves at or above its floor"*) ok "doctor's verdict line: every tool resolves" ;; *) bad "doctor verdict line" "$OUT" ;; esac
case "$OUT" in *"Managed preferences (com.memex.local)"*) ok "doctor reports the managed-preference domain" ;; *) bad "doctor managed prefs section" "$OUT" ;; esac
rm -rf "$TT"

echo "── managed preferences pre-fill backlease enroll (fleet rollout) ──"
# An MDM profile in the com.memex.local domain supplies ControlUrl + DeploymentId; `defaults` is stubbed,
# `launchctl` is stubbed, HOME is a scratch dir so no real LaunchAgent is touched.
MP="$(mktemp -d)"; mkdir -p "$MP/stubs" "$MP/home"
cat > "$MP/stubs/defaults" <<'STUB'
#!/bin/bash
key="${!#}"
case "$key" in ControlUrl) printf 'https://control.example.test\n' ;; DeploymentId) printf 'mac-mini-07\n' ;; *) exit 1 ;; esac
STUB
printf '#!/bin/bash\nexit 0\n' > "$MP/stubs/launchctl"
chmod +x "$MP/stubs/defaults" "$MP/stubs/launchctl"
mp_env() { env PATH="$MP/stubs:$STUBS:$PATH" HOME="$MP/home" MEMEX_LOCAL_HOME="$MP/home/.memex-local" "$@"; }
mp_env "$CLI" registry https://memex.example.test --id ci-mp >/dev/null 2>&1
OUT="$(mp_env "$CLI" backlease enroll 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*"reports to https://control.example.test as 'mac-mini-07'"*) ok "backlease enroll with NO flags takes ControlUrl + DeploymentId from the managed preferences" ;;
  *) bad "enroll pre-filled from managed preferences" "rc=$RC: $OUT" ;; esac
grep -q 'Hosting__ReportTo: "https://control.example.test"' "$MP/home/.memex-local/backlease.yaml" 2>/dev/null && grep -q 'Hosting__Deployment: "mac-mini-07"' "$MP/home/.memex-local/backlease.yaml" \
  && ok "the back-lease layer carries the preference values" || bad "back-lease layer values" "$(cat "$MP/home/.memex-local/backlease.yaml" 2>/dev/null)"
OUT="$(mp_env "$CLI" backlease enroll --control https://other.example.test --deployment flag-wins 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*"reports to https://other.example.test as 'flag-wins'"*) ok "an explicit flag wins over the managed preference" ;; *) bad "flag wins over preference" "rc=$RC: $OUT" ;; esac
OUT="$(env PATH="$STUBS:$PATH" HOME="$MP/home" MEMEX_LOCAL_HOME="$MP/home/.memex-local" "$CLI" backlease enroll 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "no flag and no preference → refused" "exited 0" ;; *"--control must be the control instance's https URL"*"managed preference com.memex.local ControlUrl"*) ok "no flag and no preference → refused, naming the flag AND the preference key" ;; *) bad "refusal names flag + preference" "$OUT" ;; esac

echo "── the owner's opt-in: jobs and tokens are OFF until the owner says from whom (Hosting/BackLease §13) ──"
BLF="$MP/home/.memex-local/backlease.yaml"
has() { grep -qF "$1" "$BLF" 2>/dev/null; }
# a headless enrolment with no answer: both switches declared OFF, no source written
mp_env "$CLI" backlease enroll --control https://control.example.test --deployment mac-a </dev/null >/dev/null 2>&1
has 'Hosting__BackLease__Accept__Jobs__Enabled: "false"' && has 'Hosting__BackLease__Accept__Tokens__Enabled: "false"' && ! has 'Accept__Jobs__From__0' && ! has 'Accept__Tokens__From__0' \
  && ok "headless enrolment with no answer declares both switches OFF — the offered defaults are never applied" || bad "no answer = off" "$(cat "$BLF")"
# the flags opt in, with exactly the sources named
OUT="$(mp_env "$CLI" backlease enroll --control https://control.example.test --deployment mac-a --accept-jobs=control.systemorph.com --job-channels mac --job-repos Systemorph/MeshWeaver.Plugins \
  --accept-tokens='*.meshweaver.cloud' --token-models qwen3.8:27b-mlx --token-max-concurrency 1 --residency CH </dev/null 2>&1)"; RC=$?
[ "$RC" = 0 ] && has 'Hosting__BackLease__Accept__Jobs__Enabled: "true"' && has 'Hosting__BackLease__Accept__Jobs__From__0: "control.systemorph.com"' \
  && has 'Hosting__BackLease__Accept__Jobs__Channels__0: "mac"' && has 'Hosting__BackLease__Accept__Jobs__Repos__0: "Systemorph/MeshWeaver.Plugins"' \
  && has 'Hosting__BackLease__Accept__Tokens__Enabled: "true"' && has 'Hosting__BackLease__Accept__Tokens__From__0: "*.meshweaver.cloud"' \
  && has 'Hosting__BackLease__Accept__Tokens__Models__0: "qwen3.8:27b-mlx"' && has 'Hosting__BackLease__Accept__Tokens__MaxConcurrency: "1"' && has 'Hosting__BackLease__Accept__Tokens__Residency: "CH"' \
  && ok "the opt-in flags write every Accept key, sources and models as given" || bad "opt-in flags" "rc=$RC $OUT $(cat "$BLF")"
case "$OUT" in *"accepts JOBS from: control.systemorph.com"*"accepts TOKEN requests from: *.meshweaver.cloud"*) ok "enrolment prints what the Mac accepts and from whom" ;; *) bad "enroll prints the opt-in" "$OUT" ;; esac
# re-running without answers keeps them (idempotent: the current answers are the defaults)
mp_env "$CLI" backlease enroll --control https://control.example.test --deployment mac-a </dev/null >/dev/null 2>&1
has 'Hosting__BackLease__Accept__Tokens__From__0: "*.meshweaver.cloud"' && has 'Hosting__BackLease__Accept__Jobs__Enabled: "true"' \
  && ok "re-enrolling with no answer keeps the owner's answers" || bad "re-enroll keeps answers" "$(cat "$BLF")"
# withdrawing one class touches nothing else
mp_env "$CLI" backlease enroll --control https://control.example.test --deployment mac-a --host-load-max 85 </dev/null >/dev/null 2>&1
OUT="$(mp_env "$CLI" backlease accept tokens --off </dev/null 2>&1)"; RC=$?
[ "$RC" = 0 ] && has 'Hosting__BackLease__Accept__Tokens__Enabled: "false"' && has 'Hosting__BackLease__Accept__Jobs__Enabled: "true"' \
  && has 'Hosting__BackLease__HostLoadMax: "85%"' && has 'Hosting__ReportTo: "https://control.example.test"' && has 'Hosting__Deployment: "mac-a"' \
  && ok "accept tokens --off withdraws tokens only; jobs, the ceilings and the enrolment stand" || bad "accept --off" "rc=$RC $OUT $(cat "$BLF")"
mp_env "$CLI" backlease accept tokens --from '*.meshweaver.cloud' --models qwen3.8:27b-mlx --residency CH </dev/null >/dev/null 2>&1
has 'Hosting__BackLease__Accept__Tokens__Enabled: "true"' && ok "accept tokens --from … switches tokens back on" || bad "accept tokens on" "$(cat "$BLF")"
# refusals: a bad pattern, tokens without models, a headless accept with nothing to say
OUT="$(mp_env "$CLI" backlease accept jobs --from 'not a host!' </dev/null 2>&1)" && bad "a bad pattern accepted" "$OUT" \
  || case "$OUT" in *"is not an instance pattern"*) ok "a malformed instance pattern is refused, never widened" ;; *) bad "bad pattern message" "$OUT" ;; esac
OUT="$(mp_env "$CLI" backlease accept jobs --from meshweaver </dev/null 2>&1)" && bad "a single-label pattern accepted" "$OUT" || ok "a bare single-label name is not a pattern"
OUT2="$(mp_env "$CLI" backlease accept tokens --from '*' --models= </dev/null 2>&1)" && bad "tokens without models accepted" "$OUT2" \
  || case "$OUT2" in *"name the models"*) ok "tokens switched on without models is refused" ;; *) bad "tokens without models message" "$OUT2" ;; esac
OUT="$(mp_env "$CLI" backlease accept jobs </dev/null 2>&1)" && bad "headless accept with no flags accepted" "$OUT" \
  || case "$OUT" in *"give --from"*) ok "a headless 'accept jobs' with no flags is refused — nothing is assumed" ;; *) bad "headless accept message" "$OUT" ;; esac
# --dry-run validates by the same rules and writes nothing (first-attach.sh checks its answers this way first)
before="$(cat "$BLF")"
mp_env "$CLI" backlease accept jobs --off --dry-run </dev/null >/dev/null 2>&1 && [ "$(cat "$BLF")" = "$before" ] \
  && ok "accept --dry-run validates and writes nothing" || bad "dry run wrote" "$(diff <(printf '%s' "$before") "$BLF")"
mp_env "$CLI" backlease accept tokens --from 'bad pattern!' --dry-run </dev/null >/dev/null 2>&1 && bad "dry run accepted a bad pattern" "" \
  || ok "accept --dry-run refuses a malformed answer"
FA="$HERE/../../backlease/first-attach.sh"
[ "$(grep -n -- '--dry-run >/dev/null' "$FA" | head -1 | cut -d: -f1)" -lt "$(grep -n '^echo "== 0/6 preconditions"' "$FA" | cut -d: -f1)" ] \
  && ok "first-attach validates both opt-in answers BEFORE it changes any infrastructure" || bad "first-attach validates late" ""
grep -q "case \"\$acc_jobs\" in '') ;;" "$FA" && grep -q '\[ "${#jobs_args\[@\]}" -eq 0 \] ||' "$FA" \
  && ok "first-attach leaves an unanswered class at its saved answer (a re-run never resets consent)" || bad "first-attach preserves answers" ""
grep -q 'saved_list ${P}__Jobs__Channels)" \] || jobs_args+=(--channels mac)' "$FA" \
  && ok "first-attach narrows jobs to channel mac only when no channel list is saved (a saved list is the owner's)" || bad "first-attach keeps saved channels" ""
# step 6: the host FOLLOWS the sealed set from then on, unless attached pinned
grep -q 'follow-set.py" --install --profile "$profile" --follow "$follow" --api "$api" --chart "$chart"' "$FA" \
  && [ "$(grep -n '^echo "== 6/6 follow' "$FA" | cut -d: -f1)" -gt "$(grep -n '^helm upgrade --install build-host-mac' "$FA" | cut -d: -f1)" ] \
  && ok "first-attach installs the set follower after the runners exist (step 6)" || bad "first-attach follow step" ""
grep -q 'follow-set.py" --uninstall --profile "$profile"' "$FA" && ok "first-attach --follow off removes the follower (a pinned host stays pinned)" || bad "first-attach follow off" ""
OUT="$(bash "$FA" "${FA_ARGS[@]}" --follow 'Not A Channel' </dev/null 2>&1)"; case "$OUT" in *"--follow must be a channel name or off"*) ok "first-attach refuses a --follow that is not a channel or off" ;; *) bad "first-attach --follow validation" "$OUT" ;; esac
OUT="$(bash "$FA" --token-models 2>&1)"; case "$OUT" in *"--token-models needs model names"*) ok "first-attach names a value flag given last without its value" ;; *) bad "first-attach bare \$2" "$OUT" ;; esac
OUT="$(bash "$FA" --accept-jobs= 2>&1)"; case "$OUT" in *"--accept-jobs needs instance patterns"*) ok "first-attach reads --accept-jobs=<patterns> as --accept-jobs <patterns> (an empty one is named, not skipped)" ;; *) bad "first-attach equals form" "$OUT" ;; esac
OUT="$(bash "$FA" --accept-tokens='*.meshweaver.cloud' 2>&1)"; case "$OUT" in *"unknown argument"*) bad "first-attach refuses --accept-tokens=…" "$OUT" ;; *) ok "first-attach takes --accept-tokens=<patterns> (no unknown-argument refusal)" ;; esac
# a bare `*` is a pattern, never a glob over the working directory's files
(cd "$HERE" && mp_env "$CLI" backlease accept tokens --from '*' --models qwen3.8:27b-mlx </dev/null >/dev/null 2>&1)
has 'Hosting__BackLease__Accept__Tokens__From__0: "*"' && ! has 'Accept__Tokens__From__1' && ok "'*' is written as the pattern '*', never expanded to file names" || bad "star pattern globbed" "$(grep Tokens__From "$BLF")"
OUT="$(mp_env "$CLI" backlease status </dev/null 2>&1)"
case "$OUT" in *"accepts JOBS from"*) ok "backlease status prints the opt-in" ;; *) bad "status prints opt-in" "$OUT" ;; esac
rm -rf "$MP"

echo "── fleet join: the enrolment code opens only with this Mac's key (MacStudioFleet §2b) ──"
FJ="$(mktemp -d)"; mkdir -p "$FJ/home/.memex-local/enrol"
fj_lib() { env HOME="$FJ/home" MEMEX_LOCAL_HOME="$FJ/home/.memex-local" MEMEX_LOCAL_LIB=1 bash -c ". \"$CLI\"; $1"; }
# The pure helpers: request encoding, the key fingerprint, the value fingerprint format.
req="$(fj_lib 'fleet_request studio-2 host-a jroe C02X AAAA')"
dec="$(printf '%s' "$req" | tr '_-' '/+' | awk '{ n = length($0) % 4; if (n) $0 = $0 substr("===", 1, 4 - n); print }' | base64 -d 2>/dev/null)"
[ "$dec" = '{"v":1,"deployment":"studio-2","host":"host-a","user":"jroe","machine":"C02X","key":"AAAA"}' ] \
  && ok "fleet_request is base64url of the v1 JSON the control page parses" || bad "fleet_request" "$dec"
case "$req" in *[+/=]*) bad "fleet_request is URL-safe" "$req" ;; *) ok "fleet_request is URL-safe (no + / =)" ;; esac
[ "$(fj_lib 'fleet_fact "a<b>c;d"')" = "abcd" ] && ok "fleet_fact keeps only the characters the control page admits" || bad "fleet_fact" "$(fj_lib 'fleet_fact "a<b>c;d"')"
[ "$(fj_lib 'fleet_value_fingerprint abc')" = "sha256:$(printf abc | shasum -a 256 | cut -c1-12)" ] \
  && ok "fleet_value_fingerprint is the control instance's sha256:<12 hex>" || bad "fleet_value_fingerprint" "$(fj_lib 'fleet_value_fingerprint abc')"
KEY="$FJ/home/.memex-local/enrol/studio-2.key.pem"
( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$KEY" 2>/dev/null )
fp="$(fj_lib "fleet_key_fingerprint '$KEY'")"
want="$(openssl pkey -in "$KEY" -pubout -outform DER 2>/dev/null | shasum -a 256 | cut -c1-32)"
[ "$(printf '%s' "$fp" | tr -d ' ')" = "$want" ] && [ "$(printf '%s' "$fp" | awk '{print NF}')" = "8" ] \
  && ok "the key fingerprint is the shasum of the DER key in eight groups of four (what the control page shows)" || bad "key fingerprint" "$fp vs $want"
# Seal a payload exactly as the control instance does (RSA-OAEP, SHA-256 + MGF1-SHA-256) to the Mac's key.
# (the public key goes through a file: bash 3.2's process substitution inside $(…) is not reliable)
seal() { openssl pkey -in "$2" -pubout -out "$FJ/seal-pub.pem" 2>/dev/null
  printf '%s' "$1" | openssl pkeyutl -encrypt -pubin -inkey "$FJ/seal-pub.pem" \
  -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256 2>/dev/null | base64 | tr -d '\n'; }
M="$(printf 'a%.0s' $(seq 64))"; C="$(printf 'b%.0s' $(seq 64))"
# (payload built first: bash 3.2 splits escaped quotes nested inside "$(…)")
P2='{"v":1,"d":"studio-2","m":"'"$M"'","c":"'"$C"'"}'; P9='{"v":1,"d":"studio-9","m":"'"$M"'","c":"'"$C"'"}'
sealed="$(seal "$P2" "$KEY")"; printf 'mwenrol1.%s\n' "$sealed" > "$FJ/code.txt"
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home" MEMEX_LOCAL_HOME="$FJ/home/.memex-local" "$CLI" fleet join --control https://control.example.test --id studio-2 --no-open --no-update --no-accept-jobs --no-accept-tokens --code-file "$FJ/code.txt" --owner owner1 --owner-email owner1@example.com 2>&1)"; RC=$?
grep -qx '    Auth__GlobalAdmins__0: "owner1"' "$FJ/home/.memex-local/values.local.yaml" && ok "fleet join --owner/--owner-email (executed) records the owner in the overlay" || bad "fleet join --owner flag" "$(grep -n Owner "$FJ/home/.memex-local/values.local.yaml")"
case "$RC:$OUT" in 0:*"joined as 'studio-2'"*) ok "fleet join opens a code sealed to its key and joins" ;; *) bad "fleet join with a good code" "rc=$RC: $OUT" ;; esac
OUT2="$(env PATH="$STUBS:$PATH" HOME="$FJ/home2" MEMEX_LOCAL_HOME="$FJ/home2/.memex-local" "$CLI" fleet join --control https://control.example.test --id studio-3 --no-open --no-update --no-accept-jobs --no-accept-tokens --code-file "$FJ/code.txt" 2>&1 </dev/null)"; RC2=$?
case "$RC2:$OUT2" in 0:*) bad "an unattended fleet join without an owner joined" "$OUT2" ;; *"no OWNER"*) ok "an unattended fleet join without an owner FAILS before anything is joined" ;; *) bad "ownerless join" "rc=$RC2 $OUT2" ;; esac
case "$OUT" in *"$M"*|*"$C"*) bad "fleet join never prints a key value" "a value is in the output" ;; *) ok "fleet join prints no key value (fingerprints only)" ;; esac
case "$OUT" in *"Hosting/Heartbeats/Enrol/"*) ok "fleet join prints the control instance's approval link" ;; *) bad "approval link" "$OUT" ;; esac
F="$FJ/home/.memex-local/fleet.yaml"
grep -q "Hosting__ModuleReportSecret: \"$M\"" "$F" 2>/dev/null && grep -q "Hosting__ControlInbox__Secret: \"$C\"" "$F" \
  && grep -q 'Hosting__ControlInbox__Url: "https://control.example.test/api/hooks/Hosting/PlatformBuilds"' "$F" \
  && ok "the fleet layer carries both keys and the control inbox URL" || bad "fleet layer content" "$(sed 's/"[ab]\{64\}"/<value>/' "$F" 2>/dev/null)"
# (ls, not stat: GNU `stat -f` is the FILESYSTEM report and prints to stdout, so a BSD-then-GNU fallback mis-reads)
fmode="$(ls -l "$F" 2>/dev/null | cut -c1-10)"
[ "$fmode" = "-rw-------" ] && ok "the fleet layer is 0600" || bad "fleet layer mode" "$fmode"
[ ! -e "$KEY" ] && ok "the throw-away key is deleted once the code is opened" || bad "throw-away key removed" "still at $KEY"
# A code sealed to ANOTHER key is refused by name; one issued for another id is refused by name.
( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$KEY" 2>/dev/null ); OTHER="$FJ/other.pem"
( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$OTHER" 2>/dev/null )
sealed="$(seal "$P2" "$OTHER")"; printf 'mwenrol1.%s\n' "$sealed" > "$FJ/wrong.txt"
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home" MEMEX_LOCAL_HOME="$FJ/home/.memex-local" MEMEX_OWNER=owner1 MEMEX_OWNER_EMAIL=owner1@example.com "$CLI" fleet join --control https://control.example.test --id studio-2 --no-open --no-update --no-accept-jobs --no-accept-tokens --code-file "$FJ/wrong.txt" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "a code for another key is refused" "exited 0" ;; *"does not open with this Mac's key"*) ok "a code sealed to another key is refused by name" ;; *) bad "wrong-key refusal" "$OUT" ;; esac
sealed="$(seal "$P9" "$KEY")"; printf 'mwenrol1.%s\n' "$sealed" > "$FJ/other-id.txt"
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home" MEMEX_LOCAL_HOME="$FJ/home/.memex-local" MEMEX_OWNER=owner1 MEMEX_OWNER_EMAIL=owner1@example.com "$CLI" fleet join --control https://control.example.test --id studio-2 --no-open --no-update --no-accept-jobs --no-accept-tokens --code-file "$FJ/other-id.txt" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "a code for another id is refused" "exited 0" ;; *"issued for 'studio-9'"*) ok "a code issued for another id is refused by name" ;; *) bad "other-id refusal" "$OUT" ;; esac
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home" MEMEX_LOCAL_HOME="$FJ/home/.memex-local" MEMEX_OWNER=owner1 MEMEX_OWNER_EMAIL=owner1@example.com "$CLI" fleet join --control https://control.example.test --id Bad_Id --no-open 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "a bad id is refused" "exited 0" ;; *"--id must be lower-case"*) ok "an id that cannot be a record id is refused" ;; *) bad "bad id refusal" "$OUT" ;; esac
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home" MEMEX_LOCAL_HOME="$FJ/home/.memex-local" "$CLI" fleet status 2>&1)"
case "$OUT" in *"JOINED as 'studio-2'"*"sha256:"*) ok "fleet status shows the join and the key fingerprints" ;; *) bad "fleet status" "$OUT" ;; esac
case "$OUT" in *"$M"*|*"$C"*) bad "fleet status never prints a key value" "a value is in the output" ;; *) ok "fleet status prints no key value" ;; esac
# Unattended (a device-management run): no code yet → the link is printed and kept, exit 3, nothing joined.
( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$FJ/home/.memex-local/enrol/studio-3.key.pem" 2>/dev/null )
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home" MEMEX_LOCAL_HOME="$FJ/home/.memex-local" MEMEX_OWNER=owner1 MEMEX_OWNER_EMAIL=owner1@example.com "$CLI" fleet join --control https://control.example.test --id studio-3 --no-open --no-update --no-accept-jobs --no-accept-tokens --wait 0 --code-file "$FJ/none.code" 2>&1)"; RC=$?
case "$RC:$OUT" in 3:*"WAITING for the approval on control"*) ok "unattended: no code yet → polls within its wait, says what it waits for, exit 3" ;; *) bad "unattended join waits" "rc=$RC: $OUT" ;; esac
grep -q '/Hosting/Heartbeats/Enrol/' "$FJ/home/.memex-local/enrol/studio-3.link" 2>/dev/null && ok "unattended: the approval link is kept in enrol/<id>.link" || bad "link kept" "$(cat "$FJ/home/.memex-local/enrol/studio-3.link" 2>/dev/null)"
[ -f "$FJ/home/.memex-local/enrol/studio-3.key.pem" ] && ok "unattended: the throw-away key is kept for the run that brings the code" || bad "key kept while waiting" "gone"
grep -q 'mode_args+=(-f "$FLEET_FILE")' "$CLI" && ok "helm_deploy layers the fleet file" || bad "helm_deploy layers the fleet file" "no -f \$FLEET_FILE"

echo "── fleet join v2: both answers up front, the signed poll, the itemised code (enrolment v2) ──"
# The v2 request: owner and both answers, base64url of the JSON MachineEnrolment.Parse reads.
req="$(fj_lib 'ACC_JOBS_ENABLED=true; ACC_JOBS_FROM="control.example.test"; ACC_JOBS_CHANNELS="mac"; ACC_TOK_ENABLED=false; ACC_TOK_RES=CH; fleet_request_v2 studio-5 host-a jroe C02X AAAA jroe jane.roe@example.com')"
dec="$(printf '%s' "$req" | tr '_-' '/+' | awk '{ n = length($0) % 4; if (n) $0 = $0 substr("===", 1, 4 - n); print }' | base64 -d 2>/dev/null)"
[ "$dec" = '{"v":2,"deployment":"studio-5","host":"host-a","user":"jroe","machine":"C02X","key":"AAAA","owner":{"id":"jroe","email":"jane.roe@example.com"},"jobs":{"on":true,"from":["control.example.test"],"channels":["mac"],"repos":[]},"tokens":{"on":false,"from":[],"models":[],"residency":"CH"}}' ] \
  && ok "fleet_request_v2 carries the owner and both explicit answers" || bad "fleet_request_v2" "$dec"
# Managed preferences answer the two questions (an Intune profile): a plist array or a comma string.
PREFS="$(fj_lib 'managed_pref() { case "$1" in AcceptJobs) echo 1 ;; AcceptJobsFrom) printf "(\n    \"control.example.test\",\n    \"*.example.test\"\n)" ;; AcceptTokens) echo false ;; esac; }; fleet_participation_from_prefs; echo "$ACC_JOBS_ANSWERED $ACC_JOBS_ENABLED [$ACC_JOBS_FROM] $ACC_TOK_ANSWERED $ACC_TOK_ENABLED"')"
[ "$PREFS" = 'yes true [control.example.test *.example.test] yes false' ] && ok "the Intune profile's AcceptJobs/AcceptJobsFrom/AcceptTokens answer both questions" || bad "participation from managed prefs" "$PREFS"
# Unattended with no answer: FAILS before any key or link exists — an unanswered question is never read as no.
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home4" MEMEX_LOCAL_HOME="$FJ/home4/.memex-local" MEMEX_OWNER=owner1 MEMEX_OWNER_EMAIL=owner1@example.com "$CLI" fleet join --control https://control.example.test --id studio-4 --no-open --no-update --wait 0 2>&1 </dev/null)"; RC=$?
case "$RC:$OUT" in 0:*|3:*) bad "an unanswered join is refused" "rc=$RC $OUT" ;; *"has not answered whether this Mac takes BUILD JOBS and whether it GENERATES TOKENS"*) ok "unattended with no answers: the join FAILS naming both questions" ;; *) bad "unanswered join" "rc=$RC $OUT" ;; esac
[ ! -e "$FJ/home4/.memex-local/enrol/studio-4.link" ] && ok "…and nothing was sent (no key, no link)" || bad "unanswered join sent a request" "link exists"
# The POLL: pending, then issuing, then ready with an itemised code; every item opens, lands by file, and none is printed.
KEY5="$FJ/home5/.memex-local/enrol/studio-5.key.pem"; mkdir -p "$(dirname "$KEY5")" "$FJ/poll"
( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$KEY5" 2>/dev/null )
openssl pkey -in "$KEY5" -pubout -out "$FJ/pub5.pem" 2>/dev/null
TV="tskey-auth-$(printf 't%.0s' $(seq 20))"; EV="$(printf 'e%.0s' $(seq 40))"
code2='{"v":2,"d":"studio-5","f":{"loginServer":"https://hs.example.test","tenant":"<tenant-id>","clientId":"c1"},"s":{"m":"'"$(seal "$M" "$KEY5")"'","c":"'"$(seal "$C" "$KEY5")"'","t":"'"$(seal "$TV" "$KEY5")"'","e":"'"$(seal "$EV" "$KEY5")"'"}}'
CODE2="mwenrol2.$(printf '%s' "$code2" | base64 | tr -d '\n=' | tr '+/' '-_')"
{ printf '200 {"state":"pending"}\n'; printf '200 {"state":"issuing","waiting":["t","e"]}\n'; printf '200 {"state":"ready","code":"%s"}\n' "$CODE2"; } > "$FJ/poll/answers"
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home5" MEMEX_LOCAL_HOME="$FJ/home5/.memex-local" MEMEX_OWNER=owner1 MEMEX_OWNER_EMAIL=owner1@example.com FAKE_POLL_DIR="$FJ/poll" MEMEX_FLEET_POLL_EVERY=0 \
  "$CLI" fleet join --control https://control.example.test --id studio-5 --no-open --no-update --no-accept-jobs --no-accept-tokens --wait 1 2>&1 </dev/null)"; RC=$?
case "$RC:$OUT" in 0:*"not approved yet"*"waiting for: t,e"*"the code is ready"*"joined as 'studio-5'"*) ok "fleet join polls pending → issuing → ready and continues by itself" ;; *) bad "polling join" "rc=$RC: $OUT" ;; esac
case "$OUT" in *"$M"*|*"$C"*|*"$TV"*|*"$EV"*) bad "the polling join never prints a value" "a value is in the output" ;; *) ok "the polling join prints no value (fingerprints only)" ;; esac
grep -q "Hosting__ModuleReportSecret: \"$M\"" "$FJ/home5/.memex-local/fleet.yaml" && ok "items m and c land in the fleet layer" || bad "v2 fleet layer" ""
PA="$FJ/home5/.memex-local/enrol/studio-5.preauth"
[ "$(cat "$PA" 2>/dev/null)" = "$TV" ] && [ "$(ls -l "$PA" | cut -c1-10)" = "-rw-------" ] && [ "$(cat "$FJ/home5/.memex-local/enrol/studio-5.login-server")" = "https://hs.example.test" ] \
  && ok "not root: item t is handed to the device bootstrap as a 0600 file, with the login server beside it" || bad "pre-auth hand-off" "$(ls -l "$FJ/home5/.memex-local/enrol")"
[ ! -e "$FJ/home5/.memex-local/enrol/studio-5.items" ] && [ ! -e "$KEY5" ] && ok "the opened items and the throw-away key are removed at the end" || bad "items/key cleanup" "$(ls "$FJ/home5/.memex-local/enrol")"
# Every poll is signed with the throw-away key over 'mwenrol-poll\n{fp}\n{ts}', fp = the full SHA-256 of the key's DER.
B1="$FJ/poll/body.1"; pfp="$(sed -n 's/.*"fp":"\([0-9a-f]*\)".*/\1/p' "$B1")"; pts="$(sed -n 's/.*"ts":\([0-9]*\).*/\1/p' "$B1")"
sed -n 's/.*"sig":"\([^"]*\)".*/\1/p' "$B1" | base64 -d > "$FJ/sig.bin" 2>/dev/null
[ "$(find "$FJ/poll" -name 'body.*' | wc -l | tr -d ' ')" = 3 ] && [ "${#pfp}" = 64 ] && [ -n "$pts" ] && ok "three signed polls, each naming the key by its full SHA-256" || bad "poll bodies" "$(cat "$B1")"
printf 'mwenrol-poll\n%s\n%s' "$pfp" "$pts" | openssl dgst -sha256 -verify "$FJ/pub5.pem" -signature "$FJ/sig.bin" >/dev/null 2>&1 \
  && [ "$pfp" = "$(openssl pkey -pubin -in "$FJ/pub5.pem" -outform DER 2>/dev/null | shasum -a 256 | cut -c1-64)" ] && ok "the poll signs exactly 'mwenrol-poll\\n{fp}\\n{ts}' with the throw-away key (RSA-SHA256)" || bad "poll signature shape" "missing"
# A refused join stops loudly, naming why.
printf '200 {"state":"refused","why":"superseded by a later approval"}\n' > "$FJ/poll/answers"; rm -f "$FJ/poll"/body.*
( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$FJ/home5/.memex-local/enrol/studio-6.key.pem" 2>/dev/null )
OUT="$(env PATH="$STUBS:$PATH" HOME="$FJ/home5" MEMEX_LOCAL_HOME="$FJ/home5/.memex-local" MEMEX_OWNER=owner1 MEMEX_OWNER_EMAIL=owner1@example.com FAKE_POLL_DIR="$FJ/poll" MEMEX_FLEET_POLL_EVERY=0 \
  "$CLI" fleet join --control https://control.example.test --id studio-6 --no-open --no-update --no-accept-jobs --no-accept-tokens --wait 1 2>&1 </dev/null)"; RC=$?
case "$RC:$OUT" in 0:*|3:*) bad "a refused join stops" "rc=$RC" ;; *"REFUSED this join: superseded by a later approval"*) ok "a refused join stops, naming control's reason" ;; *) bad "refused join" "rc=$RC $OUT" ;; esac
grep -q 'drop="$FLEET_DIR/fleet-ca.key"' "$CLI" && grep -q '( cmd_ca adopt --sha256 "$sha" )' "$CLI" \
  && ok "item a takes the ONE §6 path: the 0600 drop enrol/fleet-ca.key, then ca adopt --sha256 <caCertSha256> (the bootstrap adopts the same drop if this fails)" || bad "ca adopt path" "missing"
# The owner's answers reach backlease enroll and first-attach as ONE argv word per flag — a multi-pattern list is never
# split or globbed (executed, in a directory holding a file a glob would match).
mkdir -p "$FJ/globdir" && : > "$FJ/globdir/evil.example.test"
ARGV="$(cd "$FJ/globdir" && fj_lib 'ACC_JOBS_ENABLED=true; ACC_JOBS_FROM="control.example.test *.example.test"; ACC_JOBS_CHANNELS="mac"; ACC_TOK_ENABLED=false; fleet_accept_argv first-attach; printf "[%s]\n" "${FLEET_ACCEPT_ARGV[@]}"')"
[ "$ARGV" = "$(printf '[--accept-jobs=control.example.test *.example.test]\n[--no-accept-tokens]')" ] \
  && ok "first-attach gets the multi-pattern answer as one word, unsplit and unglobbed (no --job-* flags)" || bad "accept argv for first-attach" "$ARGV"
ARGV="$(fj_lib 'ACC_JOBS_ENABLED=true; ACC_JOBS_FROM="a.example b.example"; ACC_JOBS_CHANNELS="mac"; ACC_TOK_ENABLED=true; ACC_TOK_FROM="*.example.test"; ACC_TOK_MODELS="qwen3.6 qwen3.8:27b-mlx"; ACC_TOK_RES=CH; fleet_accept_argv; printf "[%s]\n" "${FLEET_ACCEPT_ARGV[@]}"')"
[ "$(printf '%s\n' "$ARGV" | wc -l | tr -d ' ')" = 5 ] && printf '%s' "$ARGV" | grep -qx '\[--token-models=qwen3.6 qwen3.8:27b-mlx\]' \
  && ok "backlease enroll gets every answer as one word per flag" || bad "accept argv for backlease" "$ARGV"
grep -q 'if \[ -s "$dir/k" \] && \[ -n "$preg" \]; then cp "$dir/k"' "$CLI" && ok "a registry key without the registry URL is left out (first-attach stays pass-both-or-neither)" || bad "k without pluginsRegistry" "missing"
! grep -q 'SC2046' "$CLI" || bad "no unquoted accept substitution remains" "$(grep -n SC2046 "$CLI")"
grep -q -- '--auth-key="file:$dir/t"' "$CLI" && ok "as root the pre-auth key goes to tailscale by FILE reference on the root daemon's socket" || bad "tailscale auth by file" "missing"
grep -q 'cmd_internal on --tenant "$tenant" --client-id "$cid" < "$dir/e"' "$CLI" && grep -q '< "$dir/n" )' "$CLI" \
  && ok "the sign-in and DNS-01 secrets reach internal on / cert config on STDIN" || bad "secrets on stdin" "missing"
grep -q '\[ "$ACC_JOBS_ENABLED" = true \] || { info "build host: jobs are OFF' "$CLI" && grep -q -- '--secrets-from "$secrets" --set-from latest' "$CLI" \
  && ok "the build host is attached ONLY on a jobs opt-in, with its values from files (no vault)" || bad "build host gating" "missing"
rm -rf "$FJ"

echo "── a fleet Mac's template carries Hosting (fleet join / backlease, registry mode) ──"
FH="$(mktemp -d)"; mkdir -p "$FH/home/.memex-local"
fh_lib() { env HOME="$FH/home" MEMEX_LOCAL_HOME="$FH/home/.memex-local" MEMEX_LOCAL_LIB=1 "$@" bash -c ". \"$CLI\"; fleet_host && echo yes || echo no"; }
[ "$(fh_lib)" = "no" ] && ok "not registered, not joined: no fleet-host template" || bad "fleet_host off by default" "$(fh_lib)"
printf 'pluginCatalog:\n  registryUrl: "https://memex.example.test"\n' > "$FH/home/.memex-local/registry.yaml"
[ "$(fh_lib)" = "no" ] && ok "registry mode alone (a personal Mac) is not a fleet host" || bad "registry alone" "$(fh_lib)"
: > "$FH/home/.memex-local/backlease.yaml"
[ "$(fh_lib)" = "yes" ] && ok "a back-leased Mac in registry mode is a fleet host (Hosting by template)" || bad "backlease → fleet host" "$(fh_lib)"
[ "$(fh_lib MEMEX_FLEET_HOST=0)" = "no" ] && ok "MEMEX_FLEET_HOST=0 opts out" || bad "opt-out" "$(fh_lib MEMEX_FLEET_HOST=0)"
rm "$FH/home/.memex-local/backlease.yaml"; : > "$FH/home/.memex-local/fleet.yaml"
[ "$(fh_lib)" = "yes" ] && ok "a joined Mac (fleet join) is a fleet host" || bad "fleet join → fleet host" "$(fh_lib)"
# The real decision, exercised (MEMEX_LOCAL_LIB=1 sources the functions): registry mode inside the agent
# follows GHCR; outside the agent, outside registry mode, with a channel, or opted out, --skip-image stays.
ais() { env HOME="$FH/home" MEMEX_LOCAL_HOME="$FH/home/.memex-local" MEMEX_LOCAL_LIB=1 "$@" bash -c ". \"$CLI\"; autoupdate_image_source \"\$0\" \"\$1\"" "$AIS_SRC" "$AIS_CH"; }
AIS_SRC=skip AIS_CH=""
[ "$(ais MEMEX_IN_AUTOUPDATE=1)" = "ghcr" ] && ok "inside the agent, a registry-mode --skip-image re-apply follows the public GHCR image" || bad "agent + registry → ghcr" "$(ais MEMEX_IN_AUTOUPDATE=1)"
[ "$(ais)" = "skip" ] && ok "a person's --skip-image is honoured (outside the agent)" || bad "outside agent stays skip" "$(ais)"
[ "$(ais MEMEX_IN_AUTOUPDATE=1 MEMEX_AUTOUPDATE_IMAGE=0)" = "skip" ] && ok "MEMEX_AUTOUPDATE_IMAGE=0 opts out" || bad "opt-out" "$(ais MEMEX_IN_AUTOUPDATE=1 MEMEX_AUTOUPDATE_IMAGE=0)"
AIS_CH="3-latest"; [ "$(ais MEMEX_IN_AUTOUPDATE=1)" = "skip" ] && ok "an install following a release channel is not moved to GHCR" || bad "channel stays" "$(ais MEMEX_IN_AUTOUPDATE=1)"; AIS_CH=""
AIS_SRC=build; [ "$(ais MEMEX_IN_AUTOUPDATE=1)" = "build" ] && ok "only --skip-image is affected (an explicit source is kept)" || bad "explicit source kept" "$(ais MEMEX_IN_AUTOUPDATE=1)"; AIS_SRC=skip
rm -f "$FH/home/.memex-local/registry.yaml"
[ "$(ais MEMEX_IN_AUTOUPDATE=1)" = "skip" ] && ok "not in registry mode: the agent's re-apply stays chart-only" || bad "self-registry stays skip" "$(ais MEMEX_IN_AUTOUPDATE=1)"
printf 'pluginCatalog:\n  registryUrl: "https://memex.example.test"\n' > "$FH/home/.memex-local/registry.yaml"
grep -q 'chosen="$(autoupdate_image_source "$image_source" "$channel")"' "$CLI" && ok "cmd_update applies the decision" || bad "cmd_update uses autoupdate_image_source" "missing"
grep -q 'mode_args+=(-f "$share_dir/values.local.fleet-host.yaml")' "$CLI" && ok "helm_deploy layers the fleet-host template" || bad "helm_deploy layers fleet-host" "missing"
rm -rf "$FH"

echo "── registry mode pulls the public GHCR anonymously; Ollama runs under launchd ──"
grep -q 'image_pull_ghcr() {' "$CLI" && ! awk '/^image_pull_ghcr\(\) \{/,/^}$/' "$CLI" | grep -q 'az ' \
  && ok "image_pull_ghcr exists and calls no az" || bad "image_pull_ghcr without az" "$(awk '/^image_pull_ghcr\(\) \{/,/^}$/' "$CLI")"
awk '/^setup_ollama\(\) \{/,/^}$/' "$CLI" | grep -q 'install_ollama_agent' && ! awk '/^setup_ollama\(\) \{/,/^}$/' "$CLI" | grep -q 'nohup ollama' \
  && ok "setup_ollama starts Ollama under launchd, not as a nohup'd process" || bad "setup_ollama under launchd" "still nohup or no agent"
grep -q '<key>OLLAMA_HOST</key><string>${1:-127.0.0.1}:11434</string>' "$CLI" && grep -q '<key>KeepAlive</key><true/>' "$CLI" \
  && grep -q 'install_ollama_agent 127.0.0.1' <<<"$(awk '/^setup_ollama\(\) \{/,/^}$/' "$CLI")" \
  && ok "the Ollama agent binds the Mac's LOOPBACK by default (unauthenticated: never LAN-wide by default) and is kept alive" || bad "ollama agent plist" "bind or KeepAlive"
# ollama_vm_reach: proven from inside the VM; LAN-wide only as a loud fallback, refusable.
OV="$(mktemp -d)"; mkdir -p "$OV/stubs" "$OV/home/Library/LaunchAgents" "$OV/home/.memex-local"
cat > "$OV/stubs/colima" <<STUB
#!/bin/bash
case "\$*" in status*) exit 0 ;; *"ip route"*) echo "default via 192.168.5.2 dev eth0" ;; *"curl"*) exit "\${FAKE_VM_REACH_RC:-0}" ;; esac
exit 0
STUB
cat > "$OV/stubs/launchctl" <<STUB
#!/bin/bash
echo "\$@" >> "$OV/lc.log"; exit 0
STUB
cat > "$OV/stubs/ollama" <<'STUB'
#!/bin/bash
exit 0
STUB
chmod +x "$OV/stubs/"*
ovr() { env PATH="$OV/stubs:$STUBS:$PATH" HOME="$OV/home" MEMEX_LOCAL_HOME="$OV/home/.memex-local" MEMEX_LOCAL_LIB=1 "$@"; }
OUT="$(ovr bash -c '. "$0"; ollama_vm_reach' "$CLI" 2>&1)"
case "$OUT" in *"reaches Ollama at 192.168.5.2:11434"*) ok "ollama_vm_reach proves the VM reaches the loopback Ollama from INSIDE the VM" ;; *) bad "vm reach ok" "$OUT" ;; esac
touch "$OV/home/Library/LaunchAgents/com.memex.local.ollama.plist"
OUT="$(ovr env FAKE_VM_REACH_RC=7 bash -c '. "$0"; ollama_vm_reach' "$CLI" 2>&1)"
P="$OV/home/Library/LaunchAgents/com.memex.local.ollama.plist"
case "$OUT" in *"EVERY interface"*) grep -q '<string>0.0.0.0:11434</string>' "$P" && ok "unreachable on loopback: the agent moves to every interface, LOUDLY" || bad "lan fallback plist" "$(cat "$P")" ;; *) bad "lan fallback" "$OUT" ;; esac
rm -f "$P"; touch "$P"
OUT="$(ovr env FAKE_VM_REACH_RC=7 MEMEX_OLLAMA_ALLOW_LAN=0 bash -c '. "$0"; ollama_vm_reach' "$CLI" 2>&1)"
case "$OUT" in *"refuses a LAN-wide bind"*) [ ! -s "$P" ] && ok "MEMEX_OLLAMA_ALLOW_LAN=0 refuses the LAN-wide bind (and says the portal has no model)" || bad "allow lan 0 wrote" "$(cat "$P")" ;; *) bad "allow lan 0" "$OUT" ;; esac
lsof_says() { printf '%s\n' "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME" "ollama 1 u 3u IPv4 0 0t0 TCP $1 (LISTEN)" > "$OV/lsof.out"; }
cat > "$OV/stubs/lsof" <<STUB
#!/bin/bash
cat "$OV/lsof.out" 2>/dev/null
STUB
chmod +x "$OV/stubs/lsof"
lsof_says '127.0.0.1:11434'; ovr bash -c '. "$0"; ollama_listens_lan' "$CLI" && bad "loopback listener read as LAN" "" || ok "an Ollama on 127.0.0.1 is read as loopback-only"
lsof_says '*:11434'; ovr bash -c '. "$0"; ollama_listens_lan' "$CLI" && ok "an Ollama on every interface (*:11434) is read as LAN-reachable" || bad "*:11434 not LAN" ""
: > "$OV/lsof.out"; ovr bash -c '. "$0"; ollama_listens_lan' "$CLI" && bad "no listener read as LAN" "" || ok "no listener is not LAN-reachable"
SO="$(awk '/^setup_ollama\(\) \{/,/^}$/' "$CLI")"
grep -q 'if ollama_listens_lan; then' <<<"$SO" && grep -q 'rebinding it to 127.0.0.1' <<<"$SO" && grep -q 'NOT run by memex-local' <<<"$SO" && grep -q 'ollama_vm_reach || true' <<<"$SO" \
  && ok "an ALREADY running Ollama is checked: memex-local's own is rebound to loopback, a foreign one is named (refused under ALLOW_LAN=0); a failed VM reach never aborts the join" || bad "existing ollama migration" "$SO"
# setup_ollama end to end against an ALREADY running Ollama (curl answers) with a foreign LAN listener:
# warned and carried on by default (never an errexit abort), refused under MEMEX_OLLAMA_ALLOW_LAN=0;
# and memex-local's own agent is rebound, then RE-CHECKED (a foreign process may still hold the port).
cat > "$OV/stubs/curl" <<'STUB'
#!/bin/bash
exit 0
STUB
chmod +x "$OV/stubs/curl"
lsof_says '*:11434'; rm -f "$OV/home/Library/LaunchAgents/com.memex.local.ollama.plist"
OUT="$(ovr env MEMEX_OLLAMA_MODEL=none bash -c 'set -euo pipefail; . "$0"; setup_ollama; echo SETUP_DONE' "$CLI" 2>&1)"
case "$OUT" in *"NOT run by memex-local"*SETUP_DONE*) ok "a foreign LAN Ollama is named and the setup carries on (no errexit abort)" ;; *) bad "foreign ollama default" "$OUT" ;; esac
OUT="$(ovr env MEMEX_OLLAMA_MODEL=none MEMEX_OLLAMA_ALLOW_LAN=0 bash -c 'set -euo pipefail; . "$0"; setup_ollama; echo SETUP_DONE' "$CLI" 2>&1)"
case "$OUT" in *SETUP_DONE*) bad "ALLOW_LAN=0 continued past a foreign LAN Ollama" "$OUT" ;; *"refuses to continue"*) ok "MEMEX_OLLAMA_ALLOW_LAN=0 refuses to continue with a foreign LAN Ollama" ;; *) bad "allow lan 0 foreign" "$OUT" ;; esac
touch "$OV/home/Library/LaunchAgents/com.memex.local.ollama.plist"
OUT="$(ovr env MEMEX_OLLAMA_MODEL=none bash -c 'set -euo pipefail; . "$0"; setup_ollama; echo SETUP_DONE' "$CLI" 2>&1)"
case "$OUT" in *"rebinding it to 127.0.0.1"*"NOT run by memex-local"*SETUP_DONE*) grep -q '<string>127.0.0.1:11434</string>' "$OV/home/Library/LaunchAgents/com.memex.local.ollama.plist" && ok "memex-local's own agent is rebound to loopback, then the port is RE-CHECKED (a listener still beyond loopback is named)" || bad "rebind plist" "" ;; *) bad "rebind then recheck" "$OUT" ;; esac
rm -rf "$OV"
FJ0="$(awk '/^cmd_fleet\(\) \{/,/^}$/' "$CLI")"
grep -q '( cmd_observability ) || warn' <<<"$FJ0" && ok "a failing observability install fails its own step loudly, never the whole join" || bad "observability subshell" ""
# The emptyDir Loki of an earlier install is migrated ONCE (immutable volumeClaimTemplates); a
# persistent one, or none, is left alone. kubectl is pinned to the local cluster.
OM="$(mktemp -d)"; mkdir -p "$OM/stubs"
cat > "$OM/stubs/kubectl" <<STUB
#!/bin/bash
echo "\$@" >> "$OM/k.log"
case "\$*" in
  *"get statefulset loki -o jsonpath"*) printf '%s' "\${FAKE_TEMPLATES:-}"; exit "\${FAKE_READ_RC:-0}" ;;
  *"get statefulset loki"*) exit "\${FAKE_STS_RC:-0}" ;;
esac
exit 0
STUB
chmod +x "$OM/stubs/kubectl"
om() { env PATH="$OM/stubs:$STUBS:$PATH" HOME="$OM" MEMEX_LOCAL_HOME="$OM/.memex-local" MEMEX_LOCAL_LIB=1 "$@"; }
OUT="$(om bash -c 'set -euo pipefail; . "$0"; observability_migrate_loki; echo DONE' "$CLI" 2>&1)"
grep -q -- '--context colima -n monitoring delete statefulset loki' "$OM/k.log" && case "$OUT" in *"discarded"*DONE*) true ;; *) false ;; esac \
  && ok "an emptyDir Loki is recreated once, announced (its logs are discarded), on the LOCAL cluster" || bad "loki migration" "$OUT / $(cat "$OM/k.log")"
: > "$OM/k.log"; om env FAKE_TEMPLATES=storage bash -c 'set -euo pipefail; . "$0"; observability_migrate_loki' "$CLI" >/dev/null 2>&1
! grep -q 'delete' "$OM/k.log" && ok "a Loki that already has its claim template is left alone" || bad "persistent loki deleted" "$(cat "$OM/k.log")"
: > "$OM/k.log"; om env FAKE_STS_RC=1 bash -c 'set -euo pipefail; . "$0"; observability_migrate_loki' "$CLI" >/dev/null 2>&1
! grep -q 'delete' "$OM/k.log" && ok "no Loki yet: nothing to migrate" || bad "no loki deleted" "$(cat "$OM/k.log")"
: > "$OM/k.log"; om env FAKE_READ_RC=1 bash -c 'set -euo pipefail; . "$0"; observability_migrate_loki' "$CLI" >/dev/null 2>&1
! grep -q 'delete' "$OM/k.log" && ok "a FAILED read of the claim templates deletes nothing (it is not 'no template')" || bad "failed read deleted" "$(cat "$OM/k.log")"
rm -rf "$OM"
grep -q 'helm --kube-context "${MEMEX_KUBE_CONTEXT:-colima}" upgrade --install loki grafana/loki-stack' "$CLI" && ok "the observability release is pinned to the local cluster's context, like the migration" || bad "observability helm context" ""
grep -q 'storage.tsdb.retention.size: 6GB' "$HERE/../share/values.observability.yaml" && grep -q 'persistentVolume: { size: 8Gi }' "$HERE/../share/values.observability.yaml" \
  && ok "Prometheus is capped by SIZE too (6 GB on the chart's 8Gi volume — an existing PVC is never asked to grow)" || bad "prometheus size cap" ""
# Observability: sized values, layered by the command; fleet join installs it by default.
OBS="$HERE/../share/values.observability.yaml"
grep -q 'retention: 7d' "$OBS" && grep -q 'retention_period: 168h' "$OBS" && grep -q 'alertmanager: { enabled: false }' "$OBS" && [ "$(grep -c 'limits: { memory:' "$OBS")" = 4 ] \
  && ok "observability is sized for the VM: memory limits on all four, 7 days of logs and metrics, no alertmanager" || bad "observability sizing" "$(cat "$OBS")"
OB="$(awk '/^cmd_observability\(\) \{/,/^}$/' "$CLI")"; grep -q 'values.observability.yaml' <<<"$OB" && ok "memex-local observability layers the sized values" || bad "observability values layer" "missing"
FJ="$(awk '/^cmd_fleet\(\) \{/,/^}$/' "$CLI")"
grep -q 'with_obs="yes"' <<<"$FJ" && grep -q 'cmd_observability' <<<"$FJ" && grep -q -- '--no-observability) with_obs="no"' <<<"$FJ" \
  && ok "fleet join installs observability by default (--no-observability skips it)" || bad "fleet join observability" "missing"

echo "── headless host: install_launchd holds the forward as a process, no LaunchAgent ──"
HL="$(mktemp -d)"; mkdir -p "$HL/stubs" "$HL/home"
printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "$FAKE_LAUNCHCTL_LOG"\nexit 0\n' > "$HL/stubs/launchctl"; chmod +x "$HL/stubs/launchctl"
: > "$HL/launchctl.log"
# The forward script runs for real against the stubs (colima ok, kubectl ok, the forward exec exits) and the curl stub answers 200.
OUT="$(env PATH="$HL/stubs:$STUBS:$PATH" HOME="$HL/home" MEMEX_LOCAL_HOME="$HL/home/.memex-local" MEMEX_HEADLESS=1 FAKE_LAUNCHCTL_LOG="$HL/launchctl.log" MEMEX_PF_BIND_BUDGET=2 MEMEX_LOCAL_LIB=1 \
  bash -c '. "$1"; install_launchd' _ "$CLI" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*"Headless host: holding the port-forward"*"port-forward started (pid "*) ok "MEMEX_HEADLESS=1: install_launchd starts the forward as a background process and says so" ;; *) bad "headless install_launchd" "rc=$RC: $OUT" ;; esac
[ -s "$HL/home/.memex-local/port-forward.pid" ] && ok "the forward's pid is recorded in port-forward.pid for the daemon to watch" || bad "pid file written" "$(ls "$HL/home/.memex-local")"
[ ! -s "$HL/launchctl.log" ] && ok "headless: launchctl is never called (no GUI session to bootstrap into)" || bad "headless: no launchctl" "$(cat "$HL/launchctl.log")"
[ ! -e "$HL/home/Library/LaunchAgents/com.memex.local.plist" ] && ok "headless: no LaunchAgent plist is written" || bad "headless: no plist" ""
OUT="$(env PATH="$HL/stubs:$STUBS:$PATH" HOME="$HL/home" MEMEX_LOCAL_HOME="$HL/home/.memex-local" MEMEX_HEADLESS=1 MEMEX_LOCAL_LIB=1 bash -c '. "$1"; uninstall_launchd' _ "$CLI" 2>&1)"
[ ! -e "$HL/home/.memex-local/port-forward.pid" ] && ok "uninstall_launchd (down) removes the headless forward's pid record" || bad "down removes pid" "$OUT"
# The daemon program drives exactly this: MEMEX_HEADLESS=1, retries `up`, then watches the pid.
DAEMON="$HERE/../../macos/libexec/memex-daemon.sh"
case "$(cat "$DAEMON")" in *'export MEMEX_HEADLESS=1'*'CLI="${MEMEX_LOCAL_CLI:-/opt/memex/bin/memex-local}"'*'while :; do'*'if stopped; then'*'"$CLI" up && break'*'port-forward.pid'*) ok "memex-daemon.sh invokes THE package's CLI with MEMEX_HEADLESS=1, checks the stop marker immediately before EVERY 'up' (after each retry pause too), then watches port-forward.pid" ;; *) bad "memex-daemon.sh lifecycle" "" ;; esac
case "$(cat "$DAEMON")" in *'stopped() { [ -f "$stop_marker" ]; }'*'if stopped; then'*'exit 0'*) ok "memex-daemon.sh exits 0 (no KeepAlive restart) while the stop marker from 'memex-local down' exists" ;; *) bad "daemon honours the stop marker" "" ;; esac
# Text form (this suite also runs on Linux, which has no plutil; the macOS suite lints the plist).
case "$(tr -d ' \n\t' < "$HERE/../../macos/launchd/com.memex.local.daemon.plist")" in
  *'<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>'*) ok "the daemon plist restarts only on a FAILED exit (KeepAlive.SuccessfulExit=false)" ;;
  *) bad "KeepAlive.SuccessfulExit=false" "$(grep -n -A3 KeepAlive "$HERE/../../macos/launchd/com.memex.local.daemon.plist")" ;;
esac
# `down` writes the stop marker BEFORE killing the forward; an explicit `up` removes it.
: > "$HL/home/.memex-local/port-forward.pid"; printf '%s' "$$" > "$HL/home/.memex-local/port-forward.pid"
OUT="$(env PATH="$HL/stubs:$STUBS:$PATH" HOME="$HL/home" MEMEX_LOCAL_HOME="$HL/home/.memex-local" MEMEX_LOCAL_LIB=1 bash -c '. "$1"; kill() { :; }; uninstall_launchd' _ "$CLI" 2>&1)"
[ -f "$HL/home/.memex-local/daemon.stopped" ] && ok "'down' (uninstall_launchd) leaves the daemon.stopped marker for the daemon" || bad "down writes daemon.stopped" "$OUT"
OUT="$(env PATH="$HL/stubs:$STUBS:$PATH" HOME="$HL/home" MEMEX_LOCAL_HOME="$HL/home/.memex-local" MEMEX_HEADLESS=1 FAKE_LAUNCHCTL_LOG="$HL/launchctl.log" MEMEX_PF_BIND_BUDGET=2 MEMEX_LOCAL_LIB=1 bash -c '. "$1"; install_launchd' _ "$CLI" 2>&1)"
[ ! -f "$HL/home/.memex-local/daemon.stopped" ] && ok "an explicit 'up' (install_launchd) removes the stop marker" || bad "up removes daemon.stopped" "$OUT"
case "$(cat "$DAEMON")" in *'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/memex/bin"'*) ok "memex-daemon.sh lists /opt/memex/bin LAST (existing installations first)" ;; *) bad "daemon PATH order" "$(grep -n 'export PATH' "$DAEMON")" ;; esac
# The 'up' above started a REAL forward loop (nohup … &). Stop it the way 'down' does — removing the
# directory does not end a process — or every run leaves one behind (measured: 16 on one build host).
env PATH="$HL/stubs:$STUBS:$PATH" HOME="$HL/home" MEMEX_LOCAL_HOME="$HL/home/.memex-local" MEMEX_HEADLESS=1 MEMEX_LOCAL_LIB=1 \
  bash -c '. "$1"; uninstall_launchd' _ "$CLI" >/dev/null 2>&1
pgrep -f "$HL/home/.memex-local/port-forward.sh" >/dev/null 2>&1 && pkill -f "$HL/home/.memex-local/port-forward.sh" 2>/dev/null
sleep 1
pgrep -f "$HL/home/.memex-local/port-forward.sh" >/dev/null 2>&1 \
  && bad "the headless tests leave a port-forward process running" "$(pgrep -fl "$HL/home/.memex-local/port-forward.sh")" \
  || ok "the headless tests leave no port-forward process behind"
rm -rf "$HL"

echo "── auto-update: a Homebrew install follows the tap by default ────"
# A fake keg: the CLI copied under …/Cellar/memex-local/<v>/libexec, which is what makes it a brew
# install, plus a brew stub that reads/writes the installed version and the keg's share/ in a state dir.
AU="$(mktemp -d)"; mkdir -p "$AU/stubs" "$AU/home" "$AU/prefix/bin" "$AU/prefix/opt/memex-local/libexec/share"
KEG="$AU/prefix/Cellar/memex-local/0.3.1/libexec"; mkdir -p "$KEG"; cp "$CLI" "$KEG/memex-local"
printf 'chart v1\n' > "$AU/prefix/opt/memex-local/libexec/share/values.yaml"
cp "$CLI" "$AU/prefix/opt/memex-local/libexec/memex-local"
printf '0.3.1\n' > "$AU/version"
printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "$FAKE_LAUNCHCTL_LOG"\nexit 0\n' > "$AU/stubs/launchctl"
cat > "$AU/stubs/brew" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  --prefix) if [ -n "${2:-}" ]; then printf '%s/opt/%s\n' "$FAKE_PREFIX" "$2"; else printf '%s\n' "$FAKE_PREFIX"; fi ;;
  list)     printf 'memex-local %s\n' "$(cat "$FAKE_PREFIX/../version")" ;;
  update)   exit 0 ;;
  upgrade)  [ -n "${FAKE_UPGRADE_FAILS:-}" ] && { echo "Error: Your Xcode (26.5) is too outdated."; exit 1; }
            [ -n "${FAKE_NEW_VERSION:-}" ] && printf '%s\n' "$FAKE_NEW_VERSION" > "$FAKE_PREFIX/../version"
            [ -n "${FAKE_NEW_CHART:-}" ] && printf '%s\n' "$FAKE_NEW_CHART" > "$FAKE_PREFIX/opt/memex-local/libexec/share/values.yaml"
            exit 0 ;;
esac
EOF
printf '#!/bin/bash\nprintf "%%s|%%s\\n" "$*" "${MEMEX_IN_AUTOUPDATE:-}" >> "$FAKE_UPDATE_LOG"\n' > "$AU/prefix/bin/memex-local"
chmod +x "$AU/stubs/"* "$AU/prefix/bin/memex-local"
au() {  # au <script> <lib-call…> — run a function of <script> with the fake toolchain
  local script="$1"; shift
  : > "$AU/launchctl.log"; : > "$AU/update.log"
  OUT="$(env PATH="$AU/stubs:$STUBS:$PATH" HOME="$AU/home" MEMEX_LOCAL_HOME="$AU/home/.memex-local" FAKE_PREFIX="$AU/prefix" \
    FAKE_LAUNCHCTL_LOG="$AU/launchctl.log" FAKE_UPDATE_LOG="$AU/update.log" MEMEX_LOCAL_SHARE="$HERE/../share" MEMEX_LOCAL_LIB=1 "$@" \
    bash -c '. "$1"; shift; "$@"' _ "$script" "${AU_CALL[@]}" 2>&1)"; RC=$?
}
PL="$AU/home/Library/LaunchAgents/com.memex.local.autoupdate.plist"

AU_CALL=(autoupdate_default); au "$CLI"
[ ! -f "$PL" ] && ok "a checkout is never armed (not a Homebrew install)" || bad "a checkout is never armed" "$OUT"

AU_CALL=(autoupdate_default); au "$KEG/memex-local"
case "$(cat "$PL" 2>/dev/null)" in
  *'<string>com.memex.local.autoupdate</string>'*"<string>$AU/prefix/bin/memex-local</string><string>autoupdate</string><string>_tick</string>"*)
    ok "a Homebrew install is armed by default, and launchd runs the relinked bin/ entry (not the keg)" ;;
  *) bad "a Homebrew install is armed by default" "plist: $(cat "$PL" 2>/dev/null) out: $OUT" ;;
esac
grep -q "^load $PL" "$AU/launchctl.log" && ok "the agent is loaded into launchd" || bad "the agent is loaded" "$(cat "$AU/launchctl.log")"

AU_CALL=(cmd_autoupdate off); au "$KEG/memex-local"
AU_CALL=(autoupdate_default); au "$KEG/memex-local"
[ ! -f "$PL" ] && ok "'autoupdate off' is remembered: up/update do not re-arm it" || bad "off is remembered" "$OUT"
rm -f "$AU/home/.memex-local/autoupdate.off"

AU_CALL=(autoupdate_default); au "$KEG/memex-local" MEMEX_IN_AUTOUPDATE=1
[ ! -s "$AU/launchctl.log" ] && ok "the agent's own 'update' never re-arms (that would unload the running job)" || bad "no re-arm inside the agent" "$(cat "$AU/launchctl.log")"
AU_CALL=(autoupdate_default); au "$KEG/memex-local" MEMEX_HEADLESS=1
[ ! -s "$AU/launchctl.log" ] && ok "a headless host is never armed" || bad "headless is not armed" "$(cat "$AU/launchctl.log")"

rm -f "$AU/home/.memex-local/autoupdate.checked"
AU_CALL=(cmd_autoupdate _tick); au "$KEG/memex-local" FAKE_UPGRADE_FAILS=1
case "$RC:$OUT" in
  0:*"brew upgrade FAILED"*"Homebrew said: Error: Your Xcode (26.5) is too outdated."*) ok "a refused upgrade keeps Homebrew's own answer and stays on the current version" ;;
  *) bad "a refused upgrade keeps Homebrew's answer" "rc=$RC $OUT" ;;
esac

AU_CALL=(cmd_autoupdate _tick); au "$KEG/memex-local" FAKE_NEW_VERSION=0.3.2
case "$OUT" in *"is current"*|"") ok "the poll is throttled (a second tick inside the interval does nothing)" ;; *) bad "the poll is throttled" "$OUT" ;; esac

rm -f "$AU/home/.memex-local/autoupdate.checked"
AU_CALL=(cmd_autoupdate _tick); au "$KEG/memex-local" FAKE_NEW_VERSION=0.3.2
case "$OUT" in *"upgraded 0.3.1 -> 0.3.2"*"chart and share assets unchanged"*) ok "a script-only release upgrades without re-applying the chart" ;; *) bad "script-only release" "$OUT" ;; esac
[ ! -s "$AU/update.log" ] && ok "… and restarts nothing" || bad "script-only release restarts nothing" "$(cat "$AU/update.log")"

rm -f "$AU/home/.memex-local/autoupdate.checked"
mkdir -p "$AU/home/.memex-local"; : > "$AU/home/.memex-local/autoroll-tick"
AU_CALL=(cmd_autoupdate _tick); au "$KEG/memex-local" FAKE_NEW_VERSION=0.3.3 FAKE_NEW_CHART="chart v2"
case "$(cat "$AU/update.log")" in "update --skip-image|1") ok "a release that changed the chart re-applies it once with update --skip-image, flagged as the agent" ;;
  *) bad "a changed chart is re-applied" "update.log=$(cat "$AU/update.log") out=$OUT" ;; esac
cmp -s "$AU/home/.memex-local/autoroll-tick" "$AU/prefix/opt/memex-local/libexec/memex-local" \
  && ok "the auto-roll's tick copy is refreshed from the new keg" || bad "the auto-roll tick copy is refreshed" "$OUT"
rm -rf "$AU"

# Static: both entry points arm it, and the acr poll cannot store az's preview warning as a digest.
for c in cmd_up cmd_update; do
  body="$(awk -v f="^${c}\\\\(\\\\)" '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
  case "$body" in *'autoupdate_default'*) ok "${c} arms auto-update" ;; *) bad "${c} arms auto-update" "no autoupdate_default call in ${c}()" ;; esac
done
grep -q 'az acr manifest show-metadata .*--only-show-errors 2>&1' "$CLI" \
  && ok "the acr poll passes --only-show-errors (a preview WARNING was stored as the digest)" \
  || bad "the acr poll passes --only-show-errors" "$(grep -n 'az acr manifest show-metadata' "$CLI")"

echo "── the login agent opens the setup wizard when a first start is waiting ──"
PF="$HERE/../share/port-forward.sh"
SO="$(mktemp -d)"; mkdir -p "$SO/stubs" "$SO/home"
# Only the function is exercised: the script itself starts Colima and execs the forward.
awk '/^open_setup_if_waiting\(\) \{/{on=1} on{print} on&&/^}$/{exit}' "$PF" > "$SO/fn.sh"
cat > "$SO/stubs/curl" <<'EOF'
#!/usr/bin/env bash
case "$*" in *redirect_url*) printf '%s' "${FAKE_REDIRECT:-}" ;; *) printf '%s' "${FAKE_ROOT_CODE:-302}" ;; esac
EOF
printf '#!/usr/bin/env bash\nprintf "info: Banner\\n  Token: %%s\\n" "$FAKE_TOKEN"\n' > "$SO/stubs/kubectl"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "$FAKE_OPEN_LOG"\n' > "$SO/stubs/open"
# the served certificate is trusted unless a test says otherwise (FAKE_UNTRUSTED)
printf '#!/usr/bin/env bash\n[ "$1" = verify-cert ] && [ -z "${FAKE_UNTRUSTED:-}" ]\n' > "$SO/stubs/security"
chmod +x "$SO/stubs/"*
so_run() {
  : > "$SO/open.log"
  OUT="$(env PATH="$SO/stubs:$PATH" HOME="$SO/home" MEMEX_LOCAL_HOME="$SO/home/.memex-local" FAKE_OPEN_LOG="$SO/open.log" \
    HOST_PORT=8443 KUBE_CONTEXT=colima "$@" bash -c '. "$1"; open_setup_if_waiting' _ "$SO/fn.sh" 2>&1)"
}
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok1
[ "$(cat "$SO/open.log")" = "https://memex.localhost:8443/setup?token=tok1" ] \
  && ok "a waiting first start opens the wizard with its token" || bad "a waiting first start opens the wizard" "open: $(cat "$SO/open.log") out: $OUT"
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok1
[ ! -s "$SO/open.log" ] && ok "the same token is not reopened (KeepAlive restarts the agent)" || bad "same token not reopened" "$(cat "$SO/open.log")"
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok2
[ "$(cat "$SO/open.log")" = "https://memex.localhost:8443/setup?token=tok2" ] \
  && ok "a new portal process (new token) opens it again" || bad "a new token reopens" "$(cat "$SO/open.log")"
so_run FAKE_REDIRECT="https://memex.localhost:8443/login" FAKE_TOKEN=tok3
[ ! -s "$SO/open.log" ] && ok "a set-up instance opens nothing" || bad "a set-up instance opens nothing" "$(cat "$SO/open.log")"
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok4 MEMEX_HEADLESS=1
[ ! -s "$SO/open.log" ] && ok "a headless host opens nothing" || bad "a headless host opens nothing" "$(cat "$SO/open.log")"
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok9 FAKE_UNTRUSTED=1
[ ! -s "$SO/open.log" ] && case "$OUT" in *"NOT opening the browser"*"not trusted"*) true ;; *) false ;; esac \
  && ok "the login agent never opens the wizard into an untrusted certificate (defect 10), and says why" || bad "agent opens into an untrusted cert" "open: $(cat "$SO/open.log") out: $OUT"
case "$OUT" in *tok9*) bad "the agent's untrusted-certificate line never carries the token" "$OUT" ;; *) ok "the agent's untrusted-certificate line never carries the token" ;; esac
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok9
[ "$(cat "$SO/open.log")" = "https://memex.localhost:8443/setup?token=tok9" ] && ok "…and opens it on a later start, once the certificate is trusted (no marker was written)" || bad "agent retries after trust" "$(cat "$SO/open.log")"
grep -q '^open_setup_if_waiting &$' "$PF" && ok "the agent runs it in the background, before the forward" || bad "the agent runs it in the background" ""
named=""; for m in "$SO/home/.memex-local/"setup-opened.*; do case "$m" in *tok*) named="$m" ;; esac; done
if [ -n "$named" ]; then bad "the marker never names the token" "$named"
else ok "the marker is named by a digest, never the token itself"; fi
printf '#!/usr/bin/env bash\nexit 1\n' > "$SO/stubs/open"
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok5
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "$FAKE_OPEN_LOG"\n' > "$SO/stubs/open"
so_run FAKE_REDIRECT="https://memex.localhost:8443/setup" FAKE_TOKEN=tok5
[ "$(cat "$SO/open.log")" = "https://memex.localhost:8443/setup?token=tok5" ] \
  && ok "a failed browser launch writes no marker, so the next start retries" || bad "a failed launch is retried" "$(cat "$SO/open.log")"
# The CLI's `up`/`update` hand-off honours the same marker; an explicit `memex-local setup` always opens.
setup_body="$(awk '/^cmd_setup\(\)/{on=1} on{print} on&&/^}$/{exit}' "$CLI")"
# shellcheck disable=SC2016  # literal source text
case "$setup_body" in *'setup-opened.$(printf '"'"'%s'"'"' "$token" | shasum -a 256'*'"$open_browser" = "auto" ] && [ -f "$marker" ]'*) ok "cmd_setup --auto skips a wizard the login agent already opened (same digest marker)" ;;
  *) bad "cmd_setup --auto shares the agent's marker" "" ;; esac
grep -q '^  cmd_setup --auto$' "$CLI" && ok "hand_into_setup (up/update) uses --auto; manual 'memex-local setup' still reopens" || bad "hand_into_setup uses --auto" ""
rm -rf "$SO"
echo "── down --purge removes the portal's node data, not only the namespace ──"
# Behaviour, not text: run cmd_down against recording stubs (colima/helm/kubectl log their argv).
DP="$(mktemp -d)"; mkdir -p "$DP/stubs" "$DP/home"
cat > "$DP/stubs/colima" <<'EOF'
#!/usr/bin/env bash
printf 'colima %s\n' "$*" >> "$FAKE_TOOL_LOG"
[ "$1" = "ssh" ] && [ -n "${FAKE_SSH_FAILS:-}" ] && exit 1
exit 0
EOF
printf '#!/usr/bin/env bash\nprintf "%%s %%s\\n" "$(basename "$0")" "$*" >> "$FAKE_TOOL_LOG"\nexit 0\n' > "$DP/stubs/helm"
cp "$DP/stubs/helm" "$DP/stubs/kubectl"; chmod +x "$DP/stubs/"*
down_run() {  # down_run [ENV=…] -- <cmd_down args>
  : > "$DP/tools.log"
  OUT="$(env PATH="$DP/stubs:$PATH" HOME="$DP/home" MEMEX_LOCAL_HOME="$DP/home/.memex-local" FAKE_TOOL_LOG="$DP/tools.log" \
    MEMEX_LOCAL_LIB=1 "$@" bash -c '. "$1"; shift; uninstall_launchd() { :; }; cmd_down "$@"' _ "$CLI" ${DOWN_ARGS[@]+"${DOWN_ARGS[@]}"} 2>&1)"; RC=$?
}
DOWN_ARGS=(--purge); down_run
if grep -q '^colima ssh -- sudo rm -rf /var/lib/memex-portal-data$' "$DP/tools.log"; then
  ok "down --purge deletes the portal's node data (instance.json survived a namespace delete)"
else bad "down --purge deletes the portal's node data" "tools: $(tr '\n' ';' < "$DP/tools.log") out: $OUT"; fi
DOWN_ARGS=(); down_run
# The absence of a delete counts only if cmd_down actually ran: it must exit 0 AND reach the tools
# (an aborted invocation — e.g. bash 3.2 + set -u on an empty array — leaves an empty log too).
if [ "$RC" -ne 0 ] || ! grep -q '^helm ' "$DP/tools.log"; then
  bad "plain down keeps the node data" "cmd_down did not run: rc=$RC tools: $(tr '\n' ';' < "$DP/tools.log") out: $OUT"
elif grep -q 'rm -rf' "$DP/tools.log"; then bad "plain down keeps the node data" "tools: $(tr '\n' ';' < "$DP/tools.log")"
else ok "plain down keeps the portal's node data (only --purge deletes it)"; fi
DOWN_ARGS=(--purge); down_run FAKE_SSH_FAILS=1
case "$RC:$OUT" in
  0:*"remove it by hand: colima ssh -- sudo rm -rf /var/lib/memex-portal-data"*) ok "a failed node delete warns with the exact command to finish it" ;;
  *) bad "a failed node delete warns with the recovery command" "rc=$RC $OUT" ;;
esac
rm -rf "$DP"
chart_path="$(grep -A1 'name: memex-data' "$HERE/../../helm/templates/memex-portal/deployment.yaml" | grep -o 'path: [^ ]*' | head -1 | cut -d' ' -f2)"
cli_path="$(sed -n 's/^readonly PORTAL_DATA_DIR="\(.*\)"$/\1/p' "$CLI")"
if [ -n "$cli_path" ] && grep -q "path: $cli_path\$" "$HERE/../../helm/templates/memex-portal/deployment.yaml"; then
  ok "PORTAL_DATA_DIR ($cli_path) is the hostPath the chart mounts"
else bad "PORTAL_DATA_DIR is the chart's hostPath" "cli='$cli_path' chart='$chart_path'"; fi

echo "── publish: a real sign-in, the tailnet name (and an optional public name), the front door on loopback (MacStudioFleet §6) ──"
PB="$(mktemp -d)"; mkdir -p "$PB/stubs" "$PB/home"
# The tailscale stub logs its arguments and answers `status --json` with FAKE_TS_JSON (nothing when unset:
# not connected, so no tailnet name).
cat > "$PB/stubs/tailscale" <<STUB
#!/bin/bash
echo "\$@" >> "$PB/ts.log"
case "\$*" in *"status --json"*) [ -n "\${FAKE_TS_JSON:-}" ] && printf '%s' "\$FAKE_TS_JSON" ;; esac
exit 0
STUB
chmod +x "$PB/stubs/tailscale"
# Self is the MagicDNS name of THIS Mac (mixed case, trailing dot as tailscale prints it); the peer's name
# must never be picked up.
TS_JSON='{"BackendState":"Running","Peer":{"nodekey:aa":{"DNSName":"other-mac.tailnet.example."}},"Self":{"HostName":"Mac-1","DNSName":"Mac-1.Tailnet.Example."}}'
pb() { env PATH="$PB/stubs:$STUBS:$PATH" HOME="$PB/home" MEMEX_LOCAL_HOME="$PB/home/.memex-local" "$@"; }
LAYER="$PB/home/.memex-local/publish.yaml"
refuse_pub() { local why="$1" want="$2"; shift 2
  OUT="$(printf 's3cret' | pb "$CLI" publish on "$@" 2>&1)"; RC=$?
  case "$RC:$OUT" in 0:*) bad "publish refuses $why" "exited 0" ;; *"$want"*) ok "publish refuses $why" ;; *) bad "publish refuses $why" "$OUT" ;; esac
  [ ! -f "$LAYER" ] || { bad "a refused publish writes no layer ($why)" "$(cat "$LAYER")"; rm -f "$LAYER"; }; }
refuse_pub "a tailnet-only publish without a tailnet name" "no tailnet name to publish" --sign-in microsoft --tenant t-1 --client-id c-1
case "$OUT" in *"reports no MagicDNS name"*) ok "a missing tailnet name is SAID (why there is none)" ;; *) bad "missing tailnet name is said" "$OUT" ;; esac
refuse_pub "--tailnet-only with --host" "exclude each other"       --tailnet-only --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1
refuse_pub "a localhost name"          "--host must be"            --host memex.localhost --sign-in microsoft --tenant t-1 --client-id c-1
refuse_pub "no sign-in provider"       "needs a real sign-in"      --host mac-1.example.com --client-id c-1
refuse_pub "an unknown provider"       "--sign-in must be microsoft" --host mac-1.example.com --sign-in dev --client-id c-1
refuse_pub "a provider the chart does not wire yet" "is not wired yet" --host mac-1.example.com --sign-in google --client-id c-1
refuse_pub "a multi-directory tenant"  "a named directory"         --host mac-1.example.com --sign-in microsoft --tenant common --client-id c-1
refuse_pub "a missing client id"       "--client-id <id> is required" --host mac-1.example.com --sign-in microsoft --tenant t-1
OUT="$(printf '' | pb "$CLI" publish on --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "publish refuses an empty secret" "exited 0" ;; *"no client secret on STDIN"*) ok "publish refuses an empty secret" ;; *) bad "publish refuses an empty secret" "$OUT" ;; esac
OUT="$(printf 's3cret-value\n' | pb "$CLI" publish on --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "publish on writes the layer" || bad "publish on writes the layer" "rc=$RC: $OUT"
case "$OUT" in *"no tailnet name: publishing the public name mac-1.example.com only"*) ok "a public name without a tailnet name is published alone, and that is said" ;; *) bad "public-only is said" "$OUT" ;; esac
case "$OUT" in *s3cret-value*) bad "publish never prints the secret" "$OUT" ;; *) ok "publish never prints the secret" ;; esac
[ "$(stat -c %a "$LAYER" 2>/dev/null || stat -f %Lp "$LAYER")" = 600 ] && ok "the layer is 0600" || bad "the layer is 0600" "$(ls -l "$LAYER")"
grep -q '^    - "mac-1.example.com"$' "$LAYER" && grep -q 'Authentication__Microsoft__ClientId: "c-1"' "$LAYER" \
  && grep -q 'Authentication__Microsoft__TenantId: "t-1"' "$LAYER" && grep -q 'Features__Onboarding__InvitationOnly: "true"' "$LAYER" \
  && grep -q 'Authentication__Microsoft__ClientSecret: "s3cret-value"' "$LAYER" \
  && ok "the layer: additional host, provider + tenant, invitation-only, the secret" || bad "the layer's content" "$(sed 's/ClientSecret: .*/ClientSecret: <redacted>/' "$LAYER")"
grep -q -- 'serve --bg --tcp 443 tcp://127.0.0.1:8446' "$PB/ts.log" && ok "publish on serves the network the ENTRY POINT's port only (not the front door)" || bad "tailscale serve" "$(cat "$PB/ts.log" 2>/dev/null)"
case "$(pb "$CLI" publish status 2>&1)" in *"https://mac-1.example.com, sign-in: Microsoft (tenant t-1), admission: invitation-only"*) ok "publish status reads the layer back" ;; *) bad "publish status" "$(pb "$CLI" publish status 2>&1)" ;; esac
# The front door: loopback only while published, every interface otherwise (kubectl is the suite's stub).
PF="$HERE/../share/port-forward.sh"
pfaddr() { env PATH="$PB/stubs:$STUBS:$PATH" MEMEX_LOCAL_HOME="$PB/home/.memex-local" MEMEX_HEADLESS=1 MEMEX_PORT=8443 bash -c '
  colima() { return 0; }; kubectl() { case "$*" in *port-forward*) printf "%s\n" "$*" ;; *) return 0 ;; esac; }; export -f colima kubectl
  sed "s#^\( *\)exec kubectl#\1kubectl#; s#^exec kubectl#kubectl#" "$1" | bash 2>/dev/null | grep port-forward | grep -v memex-network-entry | head -n 1' _ "$PF" 2>/dev/null | grep -o -- '--address .*'; }
[ "$(pfaddr)" = "--address 127.0.0.1,::1 8443:443" ] && ok "published: the front door binds loopback only, on the unprivileged port alone" || bad "published front door" "$(pfaddr)"
E="$PB/home/.memex-local/network-entry.yaml"
grep -q 'header_up X-MeshWeaver-Provenance tailnet' "$E" && grep -q '^    https://mac-1.example.com:8443 {$' "$E" && ! grep -q '__[A-Z]*__' "$E" \
  && ok "the entry point marks every request and serves the public name only" || bad "network entry manifest" "$(head -40 "$E" 2>/dev/null)"
grep -q 'svc/memex-network-entry' "$PF" && grep -q -- '"${entry_port}:8443"' "$PF" && ok "published: the entry point has its own loopback forward" || bad "entry forward" "missing in port-forward.sh"
# The entry port may not collide with the front door or the e2e portal (traffic there carries no mark).
OUT="$(printf 's3cret' | env MEMEX_ENTRY_PORT=8444 PATH="$PB/stubs:$STUBS:$PATH" HOME="$PB/home" MEMEX_LOCAL_HOME="$PB/home/.memex-local" "$CLI" publish on --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "publish refuses an entry port on the e2e port" "exited 0" ;; *"collides with port 8444"*) ok "publish refuses an entry port that collides with the e2e portal's" ;; *) bad "entry port collision" "$OUT" ;; esac
OUT="$(printf 's3cret' | env MEMEX_ENTRY_PORT=8443 PATH="$PB/stubs:$STUBS:$PATH" HOME="$PB/home" MEMEX_LOCAL_HOME="$PB/home/.memex-local" "$CLI" publish on --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "publish refuses the front door's port" "exited 0" ;; *"collides with port 8443"*) ok "publish refuses an entry port that collides with the front door's" ;; *) bad "front-door collision" "$OUT" ;; esac
grep -q 'collides' /dev/null; [ -f "$LAYER" ] && grep -q 'mac-1.example.com' "$LAYER" && ok "(the layer from the earlier publish is untouched by the refusals)" || true
# Stopping the published forward stops BOTH forwards and the entry retry loop (no orphan holds a port).
# kubectl/colima are exported FUNCTIONS: the script prepends /opt/homebrew/bin to PATH, so a PATH stub
# would lose to a real kubectl.
SUP="$(mktemp -d "${TMPDIR:-/tmp}/pfsup.XXXXXX")"; mkdir -p "$SUP/home/.memex-local"; : > "$SUP/home/.memex-local/publish.yaml"
env MEMEX_LOCAL_HOME="$SUP/home/.memex-local" MEMEX_HEADLESS=1 MEMEX_PORT=8443 bash -c '
  colima() { return 0; }; kubectl() { case "$*" in *port-forward*) exec sleep 307 ;; *) return 0 ;; esac; }; export -f colima kubectl
  exec bash "$1"' _ "$PF" >/dev/null 2>&1 &
SPID=$!; sleep 2
kids_before=$(pgrep -f "sleep 307" | wc -l | tr -d ' ')
kill -TERM "$SPID"; wait "$SPID" 2>/dev/null; sleep 1
kids_after=$(pgrep -f "sleep 307" | wc -l | tr -d ' ')
[ "$kids_before" -ge 2 ] && [ "$kids_after" = 0 ] && ok "stopping the published forward stops both forwards (${kids_before} → 0)" || bad "supervisor stops both forwards" "before=$kids_before after=$kids_after"
grep -q '<key>MEMEX_LOCAL_HOME</key>' "$HERE/../share/com.memex.local.plist" && grep -q -- '-e "s|__LOCAL_HOME__|${LOCAL_HOME}|g"' "$CLI" \
  && ok "the LaunchAgent carries the state directory the forward reads publish.yaml from" || bad "MEMEX_LOCAL_HOME reaches the LaunchAgent" "plist or sed missing"
# An empty overlay placeholder for a key the layer owns would ERASE the sign-in (the overlay is layered last).
OV="$PB/home/.memex-local/values.local.yaml"
printf 'config:\n  memex_portal:\n    Authentication__Provider: ""\n    Authentication__Microsoft__ClientId: ""\n    Authentication__Microsoft__TenantId: ""\n    KeepMe: "x"\n' > "$OV"; chmod 600 "$OV"
OUT="$(printf 's3cret-value\n' | pb "$CLI" publish on --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
! grep -q 'Authentication__Microsoft__ClientId: ""' "$OV" && ! grep -q 'Authentication__Microsoft__TenantId: ""' "$OV" \
  && grep -q 'Authentication__Provider: ""' "$OV" && grep -q 'KeepMe: "x"' "$OV" \
  && ok "publish on removes ONLY the empty overlay placeholders of the keys it owns" || bad "overlay placeholders" "$(cat "$OV")"
case "$OUT" in *"removed empty overlay placeholder"*"Authentication__Microsoft__ClientId"*) ok "publish on says which placeholders it removed" ;; *) bad "placeholder report" "$OUT" ;; esac
[ "$(ls -l "$OV" | cut -c1-10)" = "-rw-------" ] && ok "the overlay keeps its 0600 mode" || bad "overlay mode" "$(ls -l "$OV")"
printf 'config:\n  memex_portal:\n    Authentication__Microsoft__ClientId: "mine"\n' > "$OV"
printf 's3cret-value\n' | pb "$CLI" publish on --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1 >/dev/null 2>&1
grep -q 'Authentication__Microsoft__ClientId: "mine"' "$OV" && ok "a REAL overlay value is kept (and still wins)" || bad "real overlay value kept" "$(cat "$OV")"
rm -f "$OV"
# publish status checks what the portal ACTUALLY got (the rendered config), not the layer.
cat > "$PB/stubs/kubectl" <<'STUB'
#!/bin/bash
case "$*" in
  *"get configmap memex-portal-config"*ClientId*) printf '%s' "${FAKE_CLIENT_ID:-}" ;;
  *"get configmap memex-portal-config"*TenantId*) printf '%s' "${FAKE_TENANT_ID:-}" ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$PB/stubs/kubectl"
case "$(pb env FAKE_CLIENT_ID=c-1 FAKE_TENANT_ID=t-1 "$CLI" publish status 2>&1)" in *"running portal's config carries the Microsoft client id and tenant"*) ok "publish status confirms the rendered config carries the sign-in" ;; *) bad "status: rendered config ok" "$(pb env FAKE_CLIENT_ID=c-1 FAKE_TENANT_ID=t-1 "$CLI" publish status 2>&1)" ;; esac
case "$(pb env FAKE_CLIENT_ID= FAKE_TENANT_ID= "$CLI" publish status 2>&1)" in *"does NOT carry this layer's Microsoft sign-in"*) ok "publish status warns when the rendered config lost the client id" ;; *) bad "status: rendered config missing" "$(pb "$CLI" publish status 2>&1)" ;; esac
rm -f "$PB/stubs/kubectl"
# A user-space tailscaled under the person's home: its socket is used without being told.
TSOCK="$(mktemp -d "${TMPDIR:-/tmp}/tss.XXXXXX")"; mkdir -p "$PB/home/.tailscale"
python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$PB/home/.tailscale/tailscaled.sock" 2>/dev/null
if [ -S "$PB/home/.tailscale/tailscaled.sock" ]; then
  : > "$PB/ts.log"; pb env -u MEMEX_TAILSCALE_SOCKET MEMEX_LOCAL_LIB=1 bash -c '. "$0"; publish_tailscale status' "$CLI" >/dev/null 2>&1
  grep -q -- "--socket=$PB/home/.tailscale/tailscaled.sock status" "$PB/ts.log" && ok "a user-space tailscaled's socket under the home is used automatically" || bad "tailscale socket auto-detect" "$(cat "$PB/ts.log")"
  python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$TSOCK/root.sock" 2>/dev/null
  : > "$PB/ts.log"; pb env -u MEMEX_TAILSCALE_SOCKET MEMEX_TAILSCALED_ROOT_SOCKET="$TSOCK/root.sock" MEMEX_LOCAL_LIB=1 bash -c '. "$0"; publish_tailscale status' "$CLI" >/dev/null 2>&1
  grep -q -- "--socket=$TSOCK/root.sock status" "$PB/ts.log" && ok "the fleet's ROOT tailscaled daemon socket is preferred over a user-space one" || bad "root socket preferred" "$(cat "$PB/ts.log")"
  : > "$PB/ts.log"; pb env MEMEX_TAILSCALE_SOCKET=/custom.sock MEMEX_LOCAL_LIB=1 bash -c '. "$0"; publish_tailscale status' "$CLI" >/dev/null 2>&1
  grep -q -- "--socket=/custom.sock status" "$PB/ts.log" && ok "MEMEX_TAILSCALE_SOCKET still wins" || bad "explicit socket wins" "$(cat "$PB/ts.log")"
  rm -f "$PB/home/.tailscale/tailscaled.sock"
else
  printf '  skip  tailscale socket auto-detect — this host cannot create a unix socket here\n'
fi
rm -rf "$TSOCK"
# The network entry point: the caddy binary carries a FILE capability, so a pod with every capability dropped
# must add NET_BIND_SERVICE back or the exec is refused (CrashLoopBackOff); the bridge strips the file cap instead.
grep -q 'capabilities: { drop: \["ALL"\], add: \["NET_BIND_SERVICE"\] }' "$HERE/../share/network-entry.yaml" \
  && ok "the network entry point adds NET_BIND_SERVICE back (caddy's file capability)" || bad "network-entry capabilities" "$(grep -n capabilities "$HERE/../share/network-entry.yaml")"
grep -q 'setcap -r /usr/bin/caddy' "$HERE/../../tailnet-bridge/Dockerfile" && grep -q 'test -z "$(getcap /usr/local/bin/caddy)"' "$HERE/../../tailnet-bridge/Dockerfile" \
  && ok "the bridge image strips caddy's file capability and its test stage proves it" || bad "bridge caddy file cap" "missing in deploy/tailnet-bridge/Dockerfile"
# The tailnet name: read from `tailscale status --json` (.Self.DNSName, dot stripped, lower-cased), never typed.
tsname() { pb env FAKE_TS_JSON="$1" MEMEX_LOCAL_LIB=1 bash -c '. "$0"; tailnet_name' "$CLI" 2>&1; }
[ "$(tsname "$TS_JSON" 2>/dev/null)" = "mac-1.tailnet.example" ] && ok "the tailnet name is .Self.DNSName, trailing dot stripped, lower-cased (not a peer's)" || bad "tailnet name read" "$(tsname "$TS_JSON")"
case "$(tsname '{"BackendState":"Running","Self":{"DNSName":"mac-1."}}')" in *"without a domain"*) ok "a DNSName without a domain is no tailnet name, and that is said" ;; *) bad "domainless DNSName" "$(tsname '{"BackendState":"Running","Self":{"DNSName":"mac-1."}}')" ;; esac
OUT="$(tsname '{"BackendState":"Stopped","Self":{"DNSName":"mac-1.tailnet.example."}}')"
case "$OUT" in *"not running on this Mac (BackendState: Stopped)"*) case "$OUT" in *"mac-1.tailnet.example"*) bad "a stopped backend's name is not published" "$OUT" ;; *) ok "a stopped backend's last DNSName is no tailnet name, and that is said" ;; esac ;; *) bad "stopped backend" "$OUT" ;; esac
# tailscale ABSENT: no tailnet name, and the output says so (a PATH without the stub and without any real tailscale).
NOTS="$(mktemp -d)"; for t in bash env awk sed tr grep cat head paste mktemp dirname; do p="$(command -v "$t")" && ln -s "$p" "$NOTS/$t"; done
OUT="$(env PATH="$NOTS" HOME="$PB/home" MEMEX_LOCAL_HOME="$PB/home/.memex-local" MEMEX_LOCAL_LIB=1 bash -c '. "$0"; n="$(tailnet_name)"; printf "name=[%s]\n" "$n"' "$CLI" 2>&1)"
case "$OUT" in *"tailscale is not installed — no tailnet name"*"name=[]"*) ok "tailscale absent: no tailnet name, and the output says so" ;; *) bad "tailscale absent" "$OUT" ;; esac
rm -rf "$NOTS"
# Tailnet-only (the default): the machine's own name is the ONLY published name, no public host anywhere.
: > "$PB/ts.log"
OUT="$(printf 's3cret-value\n' | pb env FAKE_TS_JSON="$TS_JSON" "$CLI" publish on --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "publish on without --host publishes on the private network only" || bad "tailnet-only publish" "rc=$RC: $OUT"
[ "$(awk '/^  additionalHosts:/{on=1;next} on&&/^    - /{print;next} on{exit}' "$LAYER")" = '    - "mac-1.tailnet.example"' ] \
  && ok "tailnet-only: the ingress gets the tailnet name and nothing else" || bad "tailnet-only hosts" "$(grep -A3 additionalHosts "$LAYER")"
grep -q '^    https://mac-1.tailnet.example:8443 {$' "$E" && grep -q 'header_up X-MeshWeaver-Provenance tailnet' "$E" && grep -q 'caddyfile-hosts: "mac-1.tailnet.example"' "$E" \
  && ok "tailnet-only: the entry point serves the tailnet name, marked" || bad "tailnet-only entry" "$(sed -n '/Caddyfile/,/^---/p' "$E")"
grep -q -- 'serve --bg --tcp 443 tcp://127.0.0.1:8446' "$PB/ts.log" && ok "tailnet-only: tailscale serve still hands TCP 443 to the entry point" || bad "tailnet-only serve" "$(cat "$PB/ts.log")"
case "$OUT" in *"redirect URI on the app: https://mac-1.tailnet.example/signin-microsoft"*"publish ca"*) ok "tailnet-only: says the redirect URI for the tailnet name and how a member trusts the CA" ;; *) bad "tailnet-only guidance" "$OUT" ;; esac
case "$(pb "$CLI" publish status 2>&1)" in *"PUBLISHED — https://mac-1.tailnet.example, sign-in: Microsoft (tenant t-1)"*) ok "publish status names the tailnet name" ;; *) bad "tailnet-only status" "$(pb "$CLI" publish status 2>&1)" ;; esac
OUT="$(printf 's3cret-value\n' | pb env FAKE_TS_JSON="$TS_JSON" "$CLI" publish on --tailnet-only --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
[ "$RC" = 0 ] && grep -q '^    - "mac-1.tailnet.example"$' "$LAYER" && ok "--tailnet-only says the default explicitly" || bad "--tailnet-only" "rc=$RC: $OUT"
# Both names: --host ADDS the public name beside the tailnet name — one Caddy site for both, one ingress list.
OUT="$(printf 's3cret-value\n' | pb env FAKE_TS_JSON="$TS_JSON" "$CLI" publish on --host mac-1.example.com --sign-in microsoft --tenant t-1 --client-id c-1 2>&1)"; RC=$?
[ "$RC" = 0 ] && [ "$(awk '/^  additionalHosts:/{on=1;next} on&&/^    - /{print;next} on{exit}' "$LAYER" | sed 's/^ *- *//; s/"//g' | paste -sd, -)" = "mac-1.tailnet.example,mac-1.example.com" ] \
  && ok "public + tailnet: the ingress lists both names" || bad "both names: hosts" "rc=$RC $(grep -A4 additionalHosts "$LAYER")"
CF="$(sed -n '/Caddyfile: |/,/^---/p' "$E")"
[ "$(printf '%s\n' "$CF" | grep -c '^    https://.*:8443 {$')" = 2 ] && [ "$(printf '%s\n' "$CF" | grep -c 'header_up X-MeshWeaver-Provenance tailnet')" = 2 ] \
  && printf '%s\n' "$CF" | grep -A6 '^    https://mac-1.tailnet.example:8443 {$' | grep -q 'tls_server_name mac-1.tailnet.example$' \
  && printf '%s\n' "$CF" | grep -A6 '^    https://mac-1.example.com:8443 {$' | grep -q 'tls_server_name mac-1.example.com$' \
  && ! printf '%s\n' "$CF" | grep -q '__HOST\|site:' && grep -q 'caddyfile-hosts: "mac-1.tailnet.example mac-1.example.com"' "$E" \
  && ok "public + tailnet: one marked Caddy site per name (no unmarked site), each with its own upstream SNI" || bad "two-name Caddyfile" "$CF"
case "$OUT" in *"https://mac-1.tailnet.example/signin-microsoft"*"https://mac-1.example.com/signin-microsoft"*) ok "public + tailnet: a redirect URI for each name" ;; *) bad "both redirect URIs" "$OUT" ;; esac
# The CA lives on a volume (a restart keeps the root a member trusted) and `publish off` keeps it.
grep -q 'strategy: { type: Recreate, rollingUpdate: null }' "$HERE/../share/network-entry.yaml" && ok "Recreate clears the server-defaulted rollingUpdate (an earlier entry point upgrades)" || bad "Recreate strategy" "$(grep -n strategy "$HERE/../share/network-entry.yaml")"
grep -q 'name: XDG_DATA_HOME, value: /data' "$HERE/../share/network-entry.yaml" && grep -q 'claimName: memex-network-entry-data' "$HERE/../share/network-entry.yaml" \
  && ok "the entry point's CA lives on its data volume, not in /tmp" || bad "CA volume" "$(grep -n 'XDG_DATA_HOME\|claimName' "$HERE/../share/network-entry.yaml")"
# publish ca: the root certificate out of the entry point, PEM only on stdout; refuses anything else.
cat > "$PB/stubs/kubectl" <<'STUB'
#!/bin/bash
echo "$@" >> "${KLOG:-/dev/null}"
case "$*" in *"--context colima -n memex exec deploy/memex-network-entry -- cat /data/caddy/pki/authorities/local/root.crt"*) printf '%s' "${FAKE_CA:-}" ;; esac
exit 0
STUB
chmod +x "$PB/stubs/kubectl"
PEM=$'-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----'
[ "$(pb env FAKE_CA="$PEM" "$CLI" publish ca 2>/dev/null)" = "$PEM" ] && ok "publish ca prints the LOCAL cluster's (context colima) entry-point root certificate (PEM only, on stdout)" || bad "publish ca" "$(pb env FAKE_CA="$PEM" "$CLI" publish ca 2>&1)"
OUT="$(pb env FAKE_CA="" "$CLI" publish ca 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "publish ca refuses a missing CA" "exited 0" ;; *"no root certificate"*) ok "publish ca refuses when the entry point has no CA (and says where it looked)" ;; *) bad "publish ca missing" "$OUT" ;; esac
: > "$PB/klog"
pb env KLOG="$PB/klog" "$CLI" publish off >/dev/null 2>&1
grep -q 'delete deployment/memex-network-entry service/memex-network-entry configmap/memex-network-entry' "$PB/klog" && ! grep -q 'pvc\|-f ' "$PB/klog" \
  && ok "publish off deletes the entry point by name and keeps its CA volume" || bad "publish off keeps the CA" "$(cat "$PB/klog")"
rm -f "$PB/stubs/kubectl"
[ ! -f "$LAYER" ] && ok "publish off removes the layer" || bad "publish off" "layer still there"
grep -q -- 'serve --tcp 443 off' "$PB/ts.log" && ok "publish off stops the tailscale serve" || bad "publish off serve" "$(cat "$PB/ts.log")"
[ "$(pfaddr)" = "--address 0.0.0.0,:: 80:80 443:443 8443:443" ] && ok "not published: the front door binds every interface, all three mappings (unchanged)" || bad "unpublished front door" "$(pfaddr)"
rm -rf "$PB"

echo "── cert: a trusted certificate for the private-network name, per name in the entry point ──"
CT="$(mktemp -d)"; mkdir -p "$CT/home"
ct() { env PATH="$STUBS:$PATH" HOME="$CT/home" MEMEX_LOCAL_HOME="$CT/home/.memex-local" "$@"; }
CFG=(--domain studio-2.net.example.test --email ops@example.test --client-id cid-1 --tenant ten-1 --subscription sub-1 --resource-group dns --zone net.example.test)
OUT="$(printf 'dns-s3cret\n' | ct "$CLI" cert config "${CFG[@]}" 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "cert config writes the configuration" || bad "cert config" "rc=$RC: $OUT"
case "$OUT" in *dns-s3cret*) bad "cert config never prints the secret" "$OUT" ;; *) ok "cert config never prints the secret" ;; esac
[ "$(ls -l "$CT/home/.memex-local/certs/dns01.secret" | cut -c1-10)" = "-rw-------" ] && [ "$(cat "$CT/home/.memex-local/certs/dns01.secret")" = "dns-s3cret" ] \
  && ok "the DNS identity's secret is kept 0600 apart from the configuration" || bad "secret file" "$(ls -l "$CT/home/.memex-local/certs/")"
! grep -q dns-s3cret "$CT/home/.memex-local/certs/dns01.env" && grep -q '^AZURE_ZONE_NAME=net.example.test$' "$CT/home/.memex-local/certs/dns01.env" \
  && ok "the configuration names the zone and identity, nothing secret" || bad "dns01.env" "$(cat "$CT/home/.memex-local/certs/dns01.env")"
OUT="$(printf 'x\n' | ct "$CLI" cert config --domain studio-2.example.test --email e --client-id c --tenant t --subscription s --resource-group r --zone net.example.test 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "a domain outside the zone is refused" "exited 0" ;; *"is not inside the zone"*) ok "a domain outside the zone the identity may write is refused" ;; *) bad "zone refusal" "$OUT" ;; esac
LA="$(ct env MEMEX_LOCAL_LIB=1 bash -c '. "$0"; cert_lego_args' "$CLI" | tr '\n' ' ')"
case "$LA" in "run --accept-tos --email ops@example.test --dns azuredns --domains studio-2.net.example.test --key-type EC256 --renew-days 30 --path $CT/home/.memex-local/certs/lego ") ok "lego v5: one 'run' obtains or renews (within 30 days), DNS-01 in Azure DNS, EC256" ;; *) bad "lego args" "$LA" ;; esac
# The entry point serves the trusted certificate for a name that has one, and its local CA for the others.
mkdir -p "$CT/home/.memex-local/certs/lego/certificates"
printf 'CERT\n' > "$CT/home/.memex-local/certs/lego/certificates/studio-2.net.example.test.crt"; printf 'KEY\n' > "$CT/home/.memex-local/certs/lego/certificates/studio-2.net.example.test.key"
R="$(ct env MEMEX_LOCAL_LIB=1 bash -c '. "$0"; render_network_entry "$1" studio-2.net.example.test mac-1.example.com' "$CLI" "$HERE/../share/network-entry.yaml")"
grep -q 'tls /certs/studio-2.net.example.test.crt /certs/studio-2.net.example.test.key' <<<"$R" && ok "a name with a trusted certificate is served with it" || bad "trusted cert site" "$(grep -n 'tls ' <<<"$R")"
[ "$(grep -c '^[[:space:]]*tls internal$' <<<"$R")" = 1 ] && ok "a name without one keeps the entry point's local CA" || bad "local CA site" "$(grep -n 'tls ' <<<"$R")"
grep -q 'secretName: memex-network-entry-certs, optional: true' <<<"$R" && grep -q 'memex-local/certs-digest: "[0-9a-f]\{16\}"' <<<"$R" \
  && ok "the certificates mount optionally, and a renewal changes the pod's digest (restart onto it)" || bad "certs mount/digest" "$(grep -n 'certs' <<<"$R")"
! grep -q '__TLS__\|__CERTS_DIGEST__' <<<"$R" && ok "no placeholder is left in the rendered entry point" || bad "placeholders left" "$(grep -n '__' <<<"$R")"
R0="$(ct env MEMEX_LOCAL_LIB=1 bash -c '. "$0"; render_network_entry "$1" other.example.com' "$CLI" "$HERE/../share/network-entry.yaml")"
grep -q 'memex-local/certs-digest: "none"' <<<"$R0" && grep -q 'tls internal' <<<"$R0" && ok "with no certificate at all, the entry point is unchanged (local CA)" || bad "no-cert render" "$(grep -n 'tls \|digest' <<<"$R0")"
case "$(ct "$CLI" cert status 2>&1)" in *"studio-2.net.example.test"*) ok "cert status names the configured domain" ;; *) bad "cert status" "$(ct "$CLI" cert status 2>&1)" ;; esac
rm -rf "$CT"

echo "── internal on: the fleet default — the private-network name, its certificate, sign-in, over the network only ──"
IN="$(mktemp -d)"; mkdir -p "$IN/stubs" "$IN/home"
cat > "$IN/stubs/tailscale" <<STUB
#!/bin/bash
echo "\$@" >> "$IN/ts.log"
case "\$*" in *"status --json"*) printf '%s' '{"BackendState":"Running","Self":{"DNSName":"studio-2.net.example.test."}}' ;; esac
exit 0
STUB
chmod +x "$IN/stubs/tailscale"
cat > "$IN/stubs/kubectl" <<STUB
#!/bin/bash
echo "\$@" >> "$IN/k.log"
case "\$*" in
  *"configmap memex-portal-config"*"Microsoft__ClientId"*) printf '%s' "\${FAKE_CM_ID:-}" ;;
  *"configmap memex-portal-config"*"EnableDevLogin"*) printf '%s' "\${FAKE_CM_DEV:-}" ;;
  *"configmap memex-portal-config"*"InvitationOnly"*) printf '%s' "\${FAKE_CM_INV-true}" ;;
  *"rollout status"*) exit "\${FAKE_ROLLOUT_RC:-0}" ;;
esac
exit 0
STUB
chmod +x "$IN/stubs/kubectl"
inn() { env PATH="$IN/stubs:$STUBS:$PATH" HOME="$IN/home" MEMEX_LOCAL_HOME="$IN/home/.memex-local" "$@"; }
IL="$IN/home/.memex-local/internal.yaml"
OUT="$(printf '' | inn "$CLI" internal on --tenant t-1 --client-id c-1 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "internal on refuses an empty secret" "exited 0" ;; *"empty or too short"*) ok "internal on refuses an EMPTY secret (a failed 'az … |' pipes nothing) and writes nothing" ;; *) bad "empty secret refusal" "$OUT" ;; esac
[ ! -f "$IL" ] && ok "a refused internal on writes no layer" || bad "no layer on refusal" "$(cat "$IL")"
OUT="$(printf 's3cret\n' | inn "$CLI" internal on --tenant t-1 --client-id c-1 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "a too-short secret is refused" "exited 0" ;; *"too short"*) ok "a too-short secret is refused" ;; *) bad "short secret" "$OUT" ;; esac
OUT="$(printf 'abcdefghijklmnopqrstuvwxyz0123\n' | inn "$CLI" internal on --tenant common --client-id c-1 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "a multi-directory tenant is refused" "exited 0" ;; *"a named directory"*) ok "a multi-directory tenant is refused" ;; *) bad "tenant refusal" "$OUT" ;; esac
OUT="$(printf 'abcdefghijklmnopqrstuvwxyz0123\n' | inn "$CLI" internal on --tenant t-1 --client-id c-1 2>&1)"; RC=$?
[ "$RC" = 0 ] && ok "internal on writes the layer" || bad "internal on" "rc=$RC: $OUT"
case "$OUT" in *abcdefghijklmnopqrstuvwxyz0123*) bad "internal on never prints the secret" "$OUT" ;; *) ok "internal on never prints the secret" ;; esac
grep -q '^#Host: studio-2.net.example.test$' "$IL" && grep -q 'Authentication__Microsoft__ClientId: "c-1"' "$IL" && grep -q 'Authentication__Microsoft__TenantId: "t-1"' "$IL" \
  && grep -q 'Features__Onboarding__InvitationOnly: "true"' "$IL" && [ "$(ls -l "$IL" | cut -c1-10)" = "-rw-------" ] \
  && ok "the layer: the private-network name (read from tailscale), Microsoft sign-in, invitation-only, 0600" || bad "internal layer" "$(sed 's/ClientSecret: .*/ClientSecret: <redacted>/' "$IL")"
grep -q 'Authentication__EnableDevLogin: "false"' "$IL" && ok "internal only turns the passwordless development sign-in OFF explicitly" || bad "dev login off" "$(grep -c DevLogin "$IL")"
[ -s "$IN/k.log" ] && ! grep -v -- '^--context colima ' "$IN/k.log" | grep -q . \
  && ok "every kubectl call of internal on is pinned to the LOCAL cluster (--context colima): a private key never lands in another context" || bad "kubectl context" "$(cat "$IN/k.log")"
! grep -q -- 'serve --bg' "$IN/ts.log" && case "$OUT" in *"NOT opened yet"*) true ;; *) false ;; esac \
  && ok "the private network is NOT opened while the running portal lacks the sign-in (no exposure before update)" || bad "serve before sign-in" "$(cat "$IN/ts.log")"
printf 'abcdefghijklmnopqrstuvwxyz0123\n' | inn env FAKE_CM_ID=c-1 FAKE_CM_DEV=true "$CLI" internal on --tenant t-1 --client-id c-1 >/dev/null 2>&1
! grep -q -- 'serve --bg' "$IN/ts.log" && ok "nor while the running portal still has the development sign-in on" || bad "serve with dev login" "$(cat "$IN/ts.log")"
printf 'abcdefghijklmnopqrstuvwxyz0123\n' | inn env FAKE_CM_ID=c-1 FAKE_CM_DEV=false FAKE_CM_INV=false "$CLI" internal on --tenant t-1 --client-id c-1 >/dev/null 2>&1
! grep -q -- 'serve --bg' "$IN/ts.log" && ok "nor while the running portal is not invitation-only" || bad "serve without invitation-only" "$(cat "$IN/ts.log")"
printf 'abcdefghijklmnopqrstuvwxyz0123\n' | inn env FAKE_CM_ID=c-1 FAKE_CM_DEV=false FAKE_ROLLOUT_RC=1 "$CLI" internal on --tenant t-1 --client-id c-1 >/dev/null 2>&1
! grep -q -- 'serve --bg' "$IN/ts.log" && ok "nor before the portal's rollout has completed (the configmap changes before the pods)" || bad "serve before rollout" "$(cat "$IN/ts.log")"
printf 'abcdefghijklmnopqrstuvwxyz0123\n' | inn env FAKE_CM_ID=c-1 FAKE_CM_DEV=false "$CLI" internal on --tenant t-1 --client-id c-1 >/dev/null 2>&1
grep -q -- 'serve --bg --tcp 443 tcp://127.0.0.1:8443' "$IN/ts.log" && ok "once the portal carries the sign-in, tailscale serve forwards TCP 443 to the loopback front door (TLS passthrough)" || bad "tailscale serve" "$(cat "$IN/ts.log")"
grep -q 'internal_args=(--set-string "config.memex_portal.Authentication__EnableDevLogin=false"' "$CLI" && grep -q -- '--set-string "config.memex_portal.Features__Onboarding__InvitationOnly=true")' "$CLI" && grep -q '    -f "$OVERLAY" \\$' "$CLI" \
  && awk '/-f "\$OVERLAY" \\$/{o=NR} /internal_args\[@\]\+/{i=NR} END{exit !(o && i==o+1)}' "$CLI" \
  && ok "helm: dev sign-in forced off and invitation-only forced on AFTER the overlay (an overlay cannot undo either)" || bad "dev login after overlay" "order"
case "$OUT" in *"https://studio-2.net.example.test/signin-microsoft"*"/auth/callback/Microsoft"*"/auth/ea/callback"*) ok "internal on names the three redirect URIs to register" ;; *) bad "redirect URIs" "$OUT" ;; esac
grep -q 'mode_args+=(-f "$INTERNAL_FILE")' "$CLI" && ok "helm_deploy layers the internal-only file" || bad "helm layer" "missing"
M="$(inn env MEMEX_LOCAL_LIB=1 bash -c '. "$0"; internal_ingress_manifest studio-2.net.example.test "nginx.ingress.kubernetes.io/proxy-body-size: \"50m\""' "$CLI")"
grep -q 'name: memex-portal-net$' <<<"$M" && grep -q 'secretName: memex-portal-net-tls' <<<"$M" && grep -q 'host: "studio-2.net.example.test"' <<<"$M" \
  && grep -q 'service: { name: memex-portal-service, port: { number: 8080 } }' <<<"$M" && grep -q '    nginx.ingress.kubernetes.io/proxy-body-size: "50m"' <<<"$M" \
  && ok "a SEPARATE Ingress for the name: its own TLS Secret, the portal's annotations, the portal service" || bad "internal ingress manifest" "$M"
# The front door binds loopback only while internal-only (the network reaches it through tailscale serve).
PFI="$HERE/../share/port-forward.sh"
pfi=$(env MEMEX_LOCAL_HOME="$IN/home/.memex-local" MEMEX_HEADLESS=1 MEMEX_PORT=8443 bash -c '
  colima() { return 0; }; kubectl() { case "$*" in *port-forward*) printf "%s\n" "$*" ;; *) return 0 ;; esac; }; export -f colima kubectl
  sed "s#^\( *\)exec kubectl#\1kubectl#; s#^exec kubectl#kubectl#" "$1" | bash 2>/dev/null | grep port-forward | head -n 1' _ "$PFI" 2>/dev/null | grep -o -- '--address .*')
[ "$pfi" = "--address 127.0.0.1,::1 8443:443" ] && ok "internal only: the front door binds loopback only" || bad "internal front door" "$pfi"
inn "$CLI" internal off >/dev/null 2>&1
[ ! -f "$IL" ] && grep -q -- 'serve --tcp 443 off' "$IN/ts.log" && ok "internal off removes the layer and the tailscale serve" || bad "internal off" "$(cat "$IN/ts.log")"
rm -rf "$IN"

echo "── buildhost: build runners sized from the host, KEDA, no helm by hand ──"
lib() { env MEMEX_LOCAL_LIB=1 PATH="$STUBS:$PATH" bash -c 'set -euo pipefail; . "$0"; shift; "$@"' "$CLI" _ "$@"; }
P="$(lib buildhost_plan 18 128 8 16 0)"
[ "$P" = "$(printf 'vm_cpu=16\nvm_mem=104\nrunners_min=1\nrunners_max=4\nrunner_cpu=4\nrunner_cpu_request=3500m\nrunner_mem_request=20\nrunner_mem_limit=24\nstage_slots=4\nstage_total=16')" ] \
  && ok "an 18-core / 128 GiB Mac: build VM 16 CPU / 104 GiB, runners 1…4 × 4 CPU (request 3.5: system pods fit), 20–24 GiB, 4 stage slots = 16 stage containers" || bad "plan 18/128" "$P"
P="$(lib buildhost_plan 18 128 8 16 23)"
grep -q '^vm_mem=81$' <<<"$P" && grep -q '^runner_mem_limit=19$' <<<"$P" && grep -q '^runner_mem_request=15$' <<<"$P" \
  && ok "the largest local model's memory is kept out of the build VM (23 GiB model → 81 GiB VM, 19 GiB runners)" || bad "plan with model" "$P"
P="$(lib buildhost_plan 12 36 4 8 0)"
grep -q '^runners_max=2$' <<<"$P" && grep -q '^runner_mem_limit=8$' <<<"$P" && ok "a smaller Mac gets fewer runners (12 cores → 2), never an over-committed one" || bad "plan small" "$P"
P="$(lib buildhost_plan 5 64 4 8 0 2>&1)" && bad "a 5-core host is refused" "$P" || { grep -q 'at least 6' <<<"$P" && ok "a host without room for one runner is refused, with the reason" || bad "few cores reason" "$P"; }
P="$(lib buildhost_plan 18 32 8 16 0 2>&1)" && bad "a memory-starved host is refused" "$P" || { grep -q 'needs at least 8 GiB' <<<"$P" && ok "a host without memory for one runner is refused, with the arithmetic" || bad "low memory reason" "$P"; }
P="$(env MEMEX_BUILD_MIN_WARM=9 MEMEX_LOCAL_LIB=1 bash -c '. "$0"; buildhost_plan 18 128 8 16 0' "$CLI")"
grep -q '^runners_min=4$' <<<"$P" && ok "the warm floor never exceeds the ceiling" || bad "min clamp" "$P"
M="$(env MEMEX_LOCAL_LIB=1 PATH="$STUBS:$PATH" bash -c '. "$0"; ollama() { printf "%s\n" "NAME ID SIZE MODIFIED" "qwen3.6:latest abc 23.4 GB 2 days ago" "qwen2.5:0.5b def 397 MB 3 days ago"; }; largest_model_gib' "$CLI")"
[ "$M" = 24 ] && ok "the largest model's size is read from 'ollama list' and rounded UP (23.4 GB → 24)" || bad "largest model" "$M"
# apply against recording stubs: a missing VM is created at the plan; an installed release is sized; never the portal's context.
BH="$(mktemp -d)"; mkdir -p "$BH/stubs"
# launchctl, taskpolicy and lsof are stubbed too: apply loads the priority governor, and a test on a developer's
# Mac must never load a real launchd agent or touch a real VM's policy.
for t in launchctl taskpolicy lsof; do printf '#!/bin/bash\necho "%s $*" >> "%s/log"\nexit 0\n' "$t" "$BH" > "$BH/stubs/$t"; done
for t in colima helm kubectl; do cat > "$BH/stubs/$t" <<STUB
#!/bin/bash
# Version probes (the toolchain resolver's floor check) are answered at the manifest's version; nothing reaches a real binary.
case "\$*" in *version*) case "$t" in colima) echo "colima version 0.10.3" ;; kubectl) echo "Client Version: v1.37.1" ;; helm) echo "v4.3.1+gabc" ;; esac; exit 0 ;; esac
echo "$t \$*" >> "$BH/log"
case "$t \$*" in
  "colima list"*) printf '%s' "\${FAKE_COLIMA_LIST:-}" ;;
  "helm "*" status build-host-mac"*) exit "\${FAKE_RELEASE_RC:-1}" ;;
esac
exit 0
STUB
done; chmod +x "$BH/stubs/"*
# PATH holds the stubs and the system directories ONLY (no Homebrew), and MEMEX_PREFIX names an empty
# prefix: the resolver can find no real colima, helm or kubectl — a test must never start a VM.
mkdir -p "$BH/prefix"
bh() { env PATH="$BH/stubs:$STUBS:/usr/bin:/bin:/usr/sbin:/sbin" MEMEX_PREFIX="$BH/prefix" HOME="$BH" MEMEX_LOCAL_HOME="$BH/.memex-local" MEMEX_NET_NO_MEASURE=1 MEMEX_HOST_CORES=18 MEMEX_HOST_MEM_GIB=128 MEMEX_MODEL_GIB=0 MEMEX_COLIMA_CPU=8 MEMEX_COLIMA_MEM=16 MEMEX_CHART_DIR="$HERE/../../helm" "$@"; }
OUT="$(bh "$CLI" buildhost apply 2>&1)"
grep -q 'colima start -p build --cpu 16 --memory 104 --disk 100 --runtime containerd --kubernetes' "$BH/log" && grep -q 'kubectl --context colima-build get crd scaledobjects.keda.sh' "$BH/log" \
  && case "$OUT" in *"first attach installs it"*) true ;; *) false ;; esac \
  && ok "apply creates the build VM at the plan (containerd, k3s), checks KEDA in colima-build, and names the first attach for a missing release" || bad "apply create" "$OUT / $(cat "$BH/log")"
grep -q 'colima ssh -p build -- sudo systemctl restart mw-netshape.service' "$BH/log" && [ -f "$BH/.memex-local/netshape-build.env" ] \
  && ok "apply also shapes the build VM's network (mw-netshape.service in the VM) and records the rates" || bad "apply netshape" "$(grep netshape "$BH/log")"
: > "$BH/log"
OUT="$(bh FAKE_COLIMA_LIST='{"name":"build","status":"Running","cpus":16,"memory":107374182400}' FAKE_RELEASE_RC=0 "$CLI" buildhost apply 2>&1)"
grep -q 'helm --kube-context colima-build -n build-mac upgrade build-host-mac .* --reuse-values --set buildHosts.mac.minReplicas=1 --set buildHosts.mac.maxReplicas=4 --set-string buildHosts.resources.requests.cpu=3500m --set-string buildHosts.resources.requests.memory=20Gi --set-string buildHosts.resources.limits.cpu=4 --set-string buildHosts.resources.limits.memory=24Gi --set buildHosts.stageSlots=4' "$BH/log" \
  && ! grep -q 'colima start\|colima stop' "$BH/log" \
  && ok "an installed release is sized in place (--reuse-values, its own context); a VM that covers the plan is not restarted" || bad "apply size" "$OUT / $(cat "$BH/log")"
: > "$BH/log"
OUT="$(bh FAKE_COLIMA_LIST=$'{"name":"default","cpus":8,"memory":17179869184}\n{"name":"build","status":"Running","cpus":8,"memory":25769803776}' FAKE_RELEASE_RC=0 "$CLI" buildhost apply 2>&1)"
! grep -q 'colima stop' "$BH/log" && grep -q 'maxReplicas=2 ' "$BH/log" && case "$OUT" in *"apply --resize"*) true ;; *) false ;; esac \
  && ok "a smaller VM is NOT restarted without --resize: the runners are sized to what it has, and the resize is named" || bad "apply no resize" "$OUT / $(cat "$BH/log")"
: > "$BH/log"
bh FAKE_COLIMA_LIST='{"name":"build","status":"Running","cpus":8,"memory":25769803776}' FAKE_RELEASE_RC=0 "$CLI" buildhost apply --resize >/dev/null 2>&1
grep -q 'colima stop -p build' "$BH/log" && grep -q 'colima start -p build --cpu 16 --memory 104' "$BH/log" && grep -q 'maxReplicas=4 ' "$BH/log" \
  && ok "--resize restarts the build VM at the plan and sizes the runners to it" || bad "apply resize" "$(cat "$BH/log")"
: > "$BH/log"
bh FAKE_COLIMA_LIST='{"name":"build","status":"Stopped","cpus":16,"memory":107374182400}' FAKE_RELEASE_RC=0 "$CLI" buildhost apply >/dev/null 2>&1
grep -q '^colima start -p build$' "$BH/log" && grep -q 'upgrade build-host-mac' "$BH/log" && ok "a STOPPED build VM is started before its release is sized (never a helm call against a stopped cluster)" || bad "stopped vm" "$(cat "$BH/log")"
grep -q 'kubectl --context colima-build -n keda wait --for=condition=Available deployment --all' "$BH/log" && ok "KEDA is checked for Available deployments, not only its CRD" || bad "keda health" "$(cat "$BH/log")"
grep -q '^launchctl bootstrap gui/[0-9]* .*/Library/LaunchAgents/com.memex.local.buildhost-priority.plist' "$BH/log" \
  && grep -q '<key>MEMEX_BUILD_PROFILE</key><string>build</string>' "$BH/Library/LaunchAgents/com.memex.local.buildhost-priority.plist" \
  && [ -x "$BH/.memex-local/buildhost-priority.sh" ] \
  && ok "apply loads the priority governor (launchd agent for profile build, script copied to ~/.memex-local)" || bad "apply priority governor" "$(cat "$BH/log")"
OUT="$(bh MEMEX_HOST_MEM_GIB=abc "$CLI" buildhost plan 2>&1)" && bad "a non-numeric probe was used" "$OUT" || { case "$OUT" in *"instead of a whole number"*) ok "a host probe that is not a whole number stops the plan (never sized from 0)" ;; *) bad "probe validation" "$OUT" ;; esac; }
rm -rf "$BH"

echo "── buildhost priority: fleet jobs at the lowest priority, every idle core otherwise ──"
GOV="$HERE/../share/buildhost-priority.sh"
# Isolated from the machine's own governor: an empty MEMEX_LOCAL_HOME (no recorded buildhost-priority.env to
# load) and the policy defaults pinned, so an installed override cannot change what these tests expect.
GOVHOME="$(mktemp -d)"
gov() { env -u COLIMA_HOME -u MEMEX_BUILD_PROFILE MEMEX_LOCAL_HOME="$GOVHOME" MEMEX_BUILD_PRIO_YIELD_AT=200 MEMEX_BUILD_PRIO_RESUME_BELOW=150 \
  MEMEX_BUILD_PRIO_RESUME_AFTER=15 MEMEX_BUILD_PRIO_OLLAMA_BUSY=10 MEMEX_BUILD_PRIO_INTERVAL=2 MEMEX_BUILD_PRIO_LIB=1 \
  bash -c 'set -u; . "$0"; shift; "$@"' "$GOV" _ "$@"; }
[ "$(gov decide full 250 0 0 2)" = "yield 0" ] && ok "others above two cores → the build VM yields at once" || bad "yield on others" "$(gov decide full 250 0 0 2)"
[ "$(gov decide full 100 12 0 2)" = "yield 0" ] && ok "Ollama computing → the build VM yields, however idle the rest" || bad "yield on ollama" "$(gov decide full 100 12 0 2)"
[ "$(gov decide full 199 9 0 2)" = "full 0" ] && ok "below both thresholds the build VM keeps every idle core" || bad "full when idle" "$(gov decide full 199 9 0 2)"
[ "$(gov decide yield 100 0 10 2)" = "yield 12" ] && [ "$(gov decide yield 100 0 14 2)" = "full 0" ] \
  && ok "back to full only after the rest stayed calm for 15 s (hysteresis, no flapping)" || bad "resume hysteresis" "$(gov decide yield 100 0 14 2)"
[ "$(gov decide yield 160 0 14 2)" = "yield 0" ] && ok "between the resume and yield thresholds the calm clock restarts" || bad "calm reset" "$(gov decide yield 160 0 14 2)"
GT="$(mktemp -d)"
printf '  0 10:00.00 kernel_task\n  10 0:01.00 /usr/bin/foo\n  20 1:00:00.00 VZ\n  30 0:00.50 /opt/homebrew/bin/ollama runner\n' | gov sample_cpu /dev/null "$GT/a" "20" > "$GT/r1"
printf '  0 20:00.00 kernel_task\n  10 0:03.50 /usr/bin/foo\n  20 2:00:00.00 VZ\n  30 0:01.50 /opt/homebrew/bin/ollama runner\n  40 0:00.20 /opt/homebrew/Cellar/ollama/0.40.14/libexec/lib/ollama/llama-server --model x\n' | gov sample_cpu "$GT/a" "$GT/b" "20" > "$GT/r2"
[ "$(cat "$GT/r1")" = "0 0" ] && [ "$(cat "$GT/r2")" = "370 120" ] \
  && ok "CPU is the delta since the last sample (h:mm:ss parsed), excluding the build VM and kernel_task; Ollama (serve, runner, llama-server) counted apart" || bad "sample_cpu" "$(cat "$GT/r1") / $(cat "$GT/r2")"
mkdir -p "$GT/home/.colima/_lima/colima-build" "$GT/stubs"; : > "$GT/home/.colima/_lima/colima-build/disk"
printf '#!/bin/bash\n[ "$1" = -t ] && case "$2" in */colima-build/disk) echo 4242 ;; esac\n' > "$GT/stubs/lsof"; chmod +x "$GT/stubs/lsof"
[ "$(env PATH="$GT/stubs:$PATH" HOME="$GT/home" MEMEX_LOCAL_HOME="$GOVHOME" COLIMA_HOME= MEMEX_BUILD_PRIO_LIB=1 bash -c '. "$0"; vm_pids' "$GOV")" = 4242 ] \
  && ok "the build VM process is the one holding the build profile's disk open — never found by name" || bad "vm_pids" ""
[ "$(env -u COLIMA_HOME HOME="$GT/home" MEMEX_LOCAL_HOME="$GOVHOME" MEMEX_BUILD_PRIO_LIB=1 MEMEX_BUILD_PROFILE=default bash -c '. "$0"; instance_dir default' "$GOV")" = "$GT/home/.colima/_lima/colima" ] \
  && ok "profile default maps to Lima instance colima, any other to colima-<profile>" || bad "instance_dir" ""
rm -rf "$GT"
OUT="$(env MEMEX_LOCAL_HOME="$GOVHOME" bash "$GOV" --install --profile 'Bad Name' 2>&1)" && bad "a bad profile was accepted" "$OUT" || ok "the governor refuses a profile that is not a Colima profile name"
GH="$(mktemp -d)"; mkdir -p "$GH/stubs"
for t in launchctl taskpolicy lsof; do printf '#!/bin/bash\necho "%s $*" >> "%s/log"\nexit 0\n' "$t" "$GH" > "$GH/stubs/$t"; done; chmod +x "$GH/stubs/"*
gh_env() { env PATH="$GH/stubs:$PATH" HOME="$GH" MEMEX_LOCAL_HOME="$GH/.ml" "$@"; }
OUT="$(gh_env COLIMA_HOME="/srv/my colima" MEMEX_BUILD_PRIO_YIELD_AT=300 MEMEX_HEADLESS=1 bash "$GOV" --install --profile build 2>&1)"
GP="$(cut -d' ' -f1 "$GH/.ml/buildhost-priority.pid" 2>/dev/null)"
[ -n "$GP" ] && kill -0 "$GP" 2>/dev/null && ! grep -q 'bootstrap' "$GH/log" 2>/dev/null && [ ! -e "$GH/Library/LaunchAgents/com.memex.local.buildhost-priority.plist" ] \
  && ok "headless (no GUI session) the governor runs as a background process recorded in a pid file, no LaunchAgent" || bad "headless install" "$OUT / $(cat "$GH/log" 2>/dev/null)"
CF="$(gh_env MEMEX_BUILD_PRIO_LIB=1 env -u COLIMA_HOME -u MEMEX_BUILD_PRIO_YIELD_AT bash -c '. "$0"; echo "$COLIMA_HOME|$YIELD_AT|$PROFILE"' "$GOV")"
[ "$CF" = "/srv/my colima|300|build" ] && [ "$(gh_env MEMEX_BUILD_PRIO_YIELD_AT=500 MEMEX_BUILD_PRIO_LIB=1 bash -c '. "$0"; echo "$YIELD_AT"' "$GOV")" = 500 ] \
  && ok "the install's COLIMA_HOME, profile and overrides are recorded apart from the state and reload at every start; a shell's override still wins" || bad "recorded config" "$CF"
gh_env bash "$GOV" --install --profile build >/dev/null 2>&1
[ -n "$GP" ] && ! kill -0 "$GP" 2>/dev/null && [ -e "$GH/Library/LaunchAgents/com.memex.local.buildhost-priority.plist" ] \
  && ok "switching to a GUI session stops the headless governor before the agent loads (one governor per Mac)" || bad "headless → gui" "$(cat "$GH/log")"
gh_env MEMEX_HEADLESS=1 bash "$GOV" --install --profile build >/dev/null 2>&1
GP="$(cut -d' ' -f1 "$GH/.ml/buildhost-priority.pid" 2>/dev/null)"
[ ! -e "$GH/Library/LaunchAgents/com.memex.local.buildhost-priority.plist" ] && grep -q 'bootout' "$GH/log" \
  && ok "switching to headless unloads and removes the LaunchAgent, so a later login cannot start a second governor" || bad "gui → headless" "$(cat "$GH/log")"
gh_env bash "$GOV" --uninstall >/dev/null 2>&1
[ -n "$GP" ] && ! kill -0 "$GP" 2>/dev/null && [ ! -e "$GH/.ml/buildhost-priority.pid" ] && [ ! -e "$GH/.ml/buildhost-priority.env" ] \
  && ok "--uninstall stops the headless process and removes the recorded configuration" || { bad "headless uninstall" ""; [ -n "$GP" ] && kill "$GP" 2>/dev/null; }
sleep 300 & SP=$!
echo "$SP Mon_Jan_1_00:00:00_2001" > "$GH/.ml/buildhost-priority.pid"
gh_env bash "$GOV" --uninstall >/dev/null 2>&1
echo "$SP $(ps -o lstart= -p "$SP" | tr -s ' ' '_' | sed 's/^_//; s/_$//')" > "$GH/.ml/buildhost-priority.pid"
gh_env bash "$GOV" --uninstall >/dev/null 2>&1
kill -0 "$SP" 2>/dev/null && ok "a pid record that names another process (a reused pid after a reboot) is never signalled" || bad "stale pid signalled" ""
kill "$SP" 2>/dev/null
rm -rf "$GH" "$GOVHOME"
grep -q 'MEMEX_HEADLESS=1 .*buildhost-priority.sh" --install' "$HERE/../../macos/libexec/memex-daemon.sh" \
  && ok "the unattended daemon starts the governor headless after every boot, once installed" || bad "daemon starts governor" ""

FJB="$(awk '/^cmd_fleet\(\) \{/,/^}$/' "$CLI")"
grep -q '( cmd_buildhost apply ) || warn' <<<"$FJB" && grep -q -- '--no-buildhost) with_build="no"' <<<"$FJB" \
  && ok "fleet join runs buildhost apply by default (--no-buildhost skips it), and a failure there never ends the join" || bad "fleet join buildhost" ""
# back-lease: the owner DECLARES how full a leased Mac may get.
[ "$(lib percent_arg 85 --x)" = 85 ] && [ "$(lib percent_arg 85% --x)" = 85 ] && ok "a threshold is a whole percentage (85 or 85%)" || bad "percent ok" ""
lib percent_arg 5 --x >/dev/null 2>&1 && bad "5% accepted" "" || ok "a threshold below 10% is refused"
lib percent_arg 99 --x >/dev/null 2>&1 && bad "99% accepted" "" || ok "a threshold above 95% is refused"
lib percent_arg 0.7 --x >/dev/null 2>&1 && bad "0.7 accepted" "" || ok "a fraction is refused (whole percentages only)"
BL="$(mktemp -d)"
env MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_HOME="$BL" HOME="$BL" bash -c '. "$0"; write_backlease_file https://control.example.test mac-1 30 "" 85 60 90 80' "$CLI" >/dev/null 2>&1
grep -q 'Hosting__BackLease__HostLoadMax: "85%"' "$BL/backlease.yaml" && grep -q 'Hosting__BackLease__HostMemoryMax: "60%"' "$BL/backlease.yaml" && grep -q 'Hosting__BackLease__VmCpuMax: "90%"' "$BL/backlease.yaml" && grep -q 'Hosting__BackLease__VmMemoryMax: "80%"' "$BL/backlease.yaml" \
  && ok "the back-lease layer declares the host load/memory and VM CPU/memory ceilings for the heartbeat" || bad "backlease thresholds" "$(ls "$BL"; cat "$BL"/backlease.yaml 2>/dev/null)"
rm -rf "$BL"

echo "── further instances: the executor, its contract, the layers ─────"
# share/instances.py against stub kubectl/helm/dig (the cluster this runner does not have). What it proves: the
# job contract (claim once, Refused with a reason, result + inventory shape), the per-instance layers (none of the
# first instance's back-lease/fleet/publish/bridge keys), the bridge layer on the first instance only, and the
# capacity refusal. What it cannot: a real helm release on a real VM — that is the live proof on a Mac.
IX="$(mktemp -d)"; mkdir -p "$IX/stubs" "$IX/home/.memex-local"
cat > "$IX/stubs/kubectl" <<'EOF'
#!/usr/bin/env bash
# answers the reads share/instances.py makes; logs every call
[ "${1:-}" = version ] && { echo "Client Version: v1.37.1"; exit 0; }
printf '%s\n' "$*" >> "$IX_LOG/kubectl.log"
a=" $* "
case "$a" in
  *" get nodes "*) printf '{"items":[{"status":{"allocatable":{"cpu":"%s","memory":"%s"}}}]}' "${FAKE_ALLOC_CPU:-8}" "${FAKE_ALLOC_MEM:-16341600Ki}" ;;
  *" get pods -A "*) printf '{"items":[{"status":{"phase":"Running"},"spec":{"containers":[{"resources":{"requests":{"cpu":"%s","memory":"%s"}}}]}}]}' "${FAKE_REQ_CPU:-300m}" "${FAKE_REQ_MEM:-240Mi}" ;;
  *" -n memex get configmap memex-portal-config "*) printf '{"data":{"Authentication__Microsoft__ClientId":"00000000-client","Authentication__Microsoft__TenantId":"00000000-tenant","Hosting__Deployment":"mac-1","Hosting__BackLease__Enabled":"true","Embedding__Provider":"OpenAICompatible","Embedding__Endpoint":"http://ollama.memex.svc.cluster.local:11434/v1","Embedding__Model":"bge-m3","OpenAICompatible__DataResidency":"CH"}}' ;;
  *" -n memex get secret memex-portal-secrets "*) printf '{"data":{"Authentication__Microsoft__ClientSecret":"%s"}}' "$(printf 'sign-in-secret-value-0123' | base64)" ;;
  *" -n memex get deployment memex-portal-deployment "*) printf '{"spec":{"template":{"spec":{"containers":[{"name":"memex-portal","image":"ghcr.io/systemorph/memex-portal-ai:latest"}]}}}}' ;;
  *" -n memex get jobs "*) printf '{"items":[{"metadata":{"creationTimestamp":"2026-11-08T00:00:00Z"},"spec":{"template":{"spec":{"containers":[{"image":"ghcr.io/systemorph/memex-migration:latest"}]}}}}]}' ;;
  *" get namespace "*) [ -e "$IX_LOG/ns-exists" ] && printf '{}' || exit 1 ;;
  *" create namespace "*) printf 'apiVersion: v1\nkind: Namespace\n' ;;
  *" get deployment memex-portal-deployment "*) printf '{"spec":{"replicas":1,"template":{"spec":{"containers":[{"name":"memex-portal","image":"ghcr.io/systemorph/memex-portal-ai:latest"}]}}},"status":{"readyReplicas":1}}' ;;
  *" get --raw "*) printf 'Healthy\ntiming: 3ms\n' ;;
  *" exec "*) printf 'mac-1-proof\n' ;;
  *" get pods "*) printf '{"items":[]}' ;;
  *" get pv "*) printf '{"items":[]}' ;;
  *" apply "*) cat >/dev/null ;;
  *) : ;;
esac
exit 0
EOF
printf '#!/usr/bin/env bash\n[ "${1:-}" = version ] && { echo v4.3.1; exit 0; }\nprintf "%%s\\n" "$*" >> "$IX_LOG/helm.log"\nexit 0\n' > "$IX/stubs/helm"
printf '#!/usr/bin/env bash\nexit 0\n' > "$IX/stubs/dig"
chmod +x "$IX/stubs/"*
cat > "$IX/home/.memex-local/registry.yaml" <<'EOF'
pluginCatalog:
  registryUrl: "https://registry.example.com"
  instanceId: "mac-1"
EOF
cat > "$IX/home/.memex-local/backlease.yaml" <<'EOF'
config:
  memex_portal:
    Hosting__Deployment: "mac-1"
    Hosting__ReportTo: "https://control.example.com"
EOF
IXB="$IX/home/.memex-local/instances"
ix() { env PATH="$IX/stubs:$STUBS:$PATH" HOME="$IX/home" MEMEX_LOCAL_HOME="$IX/home/.memex-local" IX_LOG="$IX" MEMEX_LOCAL_SHARE="$HERE/../share" "$@"; }
job() {  # job <jobId> <json>
  mkdir -p "$IXB/jobs"; printf '%s' "$2" > "$IXB/jobs/.$1.tmp" && mv "$IXB/jobs/.$1.tmp" "$IXB/jobs/$1.json"; }
res() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["status"]+"|"+d["message"])' "$IXB/results/$1.json" 2>/dev/null; }
runonce() { ix "$CLI" instances agent run-once >"$IX/agent.out" 2>&1; }
SPEC='{"hostName":"proof.mac-1.net.example.com","displayName":"Proof","owner":{"id":"owner1","email":"owner1@example.com"},"resources":{"cpu":2,"memoryGi":6,"storageGi":20},"packages":["Plugins/Hosting","Plugins/Notes"],"ai":{"chatModel":"","embeddings":true}}'
J() { printf '{"schema":1,"jobId":"%s","operation":"%s","name":"%s","requestedBy":"owner1","requestedAt":"2026-11-08T12:00:00Z"%s}' "$1" "$2" "$3" "${4:-}"; }

# Refusals — each a Refused result naming why, never a silent drop.
job local-instance-bad-name-create-a "$(J local-instance-bad-name-create-a create Bad_Name ",\"spec\":$SPEC")"
job local-instance-memex-create-a "$(J local-instance-memex-create-a create memex ",\"spec\":$SPEC")"
job local-instance-kube-x-create-a "$(J local-instance-kube-x-create-a create kube-x ",\"spec\":$SPEC")"
job local-instance-pkg-create-a "$(J local-instance-pkg-create-a create proof ",\"spec\":${SPEC/Plugins\/Notes/not a package}")"
job local-instance-proof-remove-a "$(J local-instance-proof-remove-a remove proof)"
job local-instance-proof-wipe-a "$(J local-instance-proof-wipe-a wipe proof)"
job local-instance-json-a '{"schema":1, "jobId": '
job 'Bad Name' "$(J x stop proof)"
runonce
for c in "local-instance-bad-name-create-a|is not ^[a-z]" "local-instance-memex-create-a|reserved" "local-instance-kube-x-create-a|reserved" \
         "local-instance-pkg-create-a|is not <Source>/<Package>" "local-instance-proof-remove-a|confirm equal to the name" \
         "local-instance-proof-wipe-a|is not one of create" "local-instance-json-a|not valid JSON"; do
  id="${c%%|*}"; want="${c#*|}"; r="$(res "$id")"
  case "$r" in "Refused|"*"$want"*) ok "a job is Refused with its reason: $id ($want)" ;; *) bad "refusal $id" "got '$r' — $(cat "$IX/agent.out")" ;; esac
done
# The bridge is writable by the first instance's pod: a planted symlink is never followed.
printf 'secret-on-the-mac' > "$IX/outside.txt"; ln -s "$IX/outside.txt" "$IXB/jobs/local-instance-link-stop-a.json"; runonce
case "$(res local-instance-link-stop-a)" in "Refused|"*"symbolic link is never followed"*) ok "a job that is a symbolic link is Refused, never read" ;; *) bad "symlink job" "$(res local-instance-link-stop-a)" ;; esac
grep -rq 'secret-on-the-mac' "$IXB" && bad "a symlinked job's target leaked into the bridge" "" || ok "the symlink's target never reaches a result"
mv "$IXB/results" "$IX/results-real"; ln -s "$IX" "$IXB/results"; job local-instance-x-stop-a "$(J local-instance-x-stop-a stop proof)"
ix "$CLI" instances agent run-once >"$IX/agent.out" 2>&1
grep -q 'symbolic link' "$IX/agent.out" && [ ! -e "$IX/local-instance-x-stop-a.json" ] && ok "a results/ replaced by a symlink stops the executor (nothing written through it)" || bad "results symlink" "$(cat "$IX/agent.out")"
rm "$IXB/results" "$IXB/jobs/local-instance-x-stop-a.json"; mv "$IX/results-real" "$IXB/results"
[ -f "$IXB/results/invalid-bad-name.json" ] && ok "a job file whose name is not a job id is answered Refused too (invalid-<name>)" || bad "invalid file name" "$(ls "$IXB/results")"
[ -z "$(find "$IXB/jobs" "$IXB/running" -name '*.json' 2>/dev/null)" ] && ok "every refused job left jobs/ and running/" || bad "leftovers" "$(ls -R "$IXB")"

# A create that succeeds against the stubs — result shape, claim-once, the layers.
rm -f "$IXB"/results/*
job local-instance-proof-create-a "$(J local-instance-proof-create-a create proof ",\"spec\":$SPEC")"
runonce
R="$IXB/results/local-instance-proof-create-a.json"
python3 - "$R" <<'PY2' && ok "a create ends Succeeded with the contract's result shape (steps, log, url, actionsForOwner, finishedAt)" || bad "result shape" "$(cat "$R" 2>/dev/null; cat "$IX/agent.out")"
import json, sys
d = json.load(open(sys.argv[1]))
want = {"schema", "jobId", "operation", "name", "status", "message", "startedAt", "updatedAt", "finishedAt", "steps", "log", "url", "actionsForOwner"}
assert want <= set(d), set(d) ^ want
assert d["schema"] == 1 and d["status"] == "Succeeded" and d["finishedAt"], d
assert d["url"] == "https://proof.mac-1.net.example.com", d["url"]
assert all(set(s) == {"name", "status", "detail"} and s["status"] in ("Done", "Running", "Failed", "Skipped", "Pending") for s in d["steps"]), d["steps"]
assert any("signin-microsoft" in a and "00000000-client" in a for a in d["actionsForOwner"]), d["actionsForOwner"]
assert any("dns.extraRecords" in a for a in d["actionsForOwner"]), d["actionsForOwner"]
assert len(d["log"]) <= 200
PY2
grep -rq 'sign-in-secret-value-0123' "$IXB" && bad "a secret reached the bridge" "$(grep -rl 'sign-in-secret-value-0123' "$IXB")" || ok "no secret reaches the bridge (results, inventory)"
[ ! -e "$IXB/running/local-instance-proof-create-a.json" ] && ok "a terminal job leaves running/" || bad "running left" ""
before="$(cat "$R")"; job local-instance-proof-create-a "$(J local-instance-proof-create-a create proof ",\"spec\":$SPEC")"; runonce
[ "$(cat "$R")" = "$before" ] && [ ! -e "$IXB/jobs/local-instance-proof-create-a.json" ] && [ "$(grep -c '^upgrade' "$IX/helm.log")" = 1 ] \
  && ok "a job whose id already has a terminal result is not re-run (one helm upgrade, result untouched)" || bad "re-run" "$(grep -c '^upgrade' "$IX/helm.log") helm runs"
ST="$IX/home/.memex-local/instance-state/proof"
python3 - "$ST/values.json" "$ST/secret-values.json" <<'PY2' && ok "the instance's own layers: owner keys, registry id <machine>-<name>, its host + TLS, Hosting first then the spec's packages, resources, a local data volume — and none of the first instance's back-lease/fleet/bridge keys" || bad "instance layers" "$(cat "$ST/values.json")"
import json, sys
v = json.load(open(sys.argv[1])); s = json.load(open(sys.argv[2]))
c = v["config"]["memex_portal"]
assert c["Auth__GlobalAdmins__0"] == "owner1" and c["Instance__Owner__Id"] == "owner1" and c["Instance__Owner__Email"] == "owner1@example.com"
assert v["pluginCatalog"]["instanceId"] == "mac-1-proof" and v["pluginCatalog"]["homeUrl"] == "https://proof.mac-1.net.example.com"
assert v["pluginCatalog"]["registryUrl"] == "https://registry.example.com"
assert v["ingress"]["host"] == "proof.mac-1.net.example.com" and v["ingress"]["tlsSecret"] == "memex-proof-tls"
assert [c["PluginCatalog__InstallByDefault__%d" % i] for i in range(2)] == ["Plugins/Hosting", "Plugins/Notes"]
assert c["PluginCatalog__InstallPreInstalledPackages"] == "true"
assert v["resources"]["portal"]["requests"] == {"cpu": "2", "memory": "6Gi"}
assert v["persistDataVolume"] is False and v["persistence"]["data"]["create"] is True and v["persistence"]["data"]["size"] == "20Gi"
assert c["Authentication__Microsoft__ClientId"] == "00000000-client" and c["Authentication__EnableDevLogin"] == "false"
assert c["Embedding__Endpoint"] == "http://ollama.memex-proof.svc.cluster.local:11434/v1", c["Embedding__Endpoint"]
assert c["OpenAICompatible__Endpoint"].startswith("http://ollama.memex-proof.svc.cluster.local"), c["OpenAICompatible__Endpoint"]
text = json.dumps(v)
for banned in ("Hosting__BackLease", "Hosting__Deployment", "Hosting__ReportTo", "Hosting__Fleet", "Hosting__LocalInstances", "host-instances", "backlease", "extraVolumes", "Bootstrap"):
    assert banned not in text, banned
p = s["secrets"]["memex_portal"]
assert p["Ai__KeyProtection__MasterKey"] and p["memex_postgres_password"] and p["Authentication__Microsoft__ClientSecret"]
PY2
[ "$(stat -c %a "$ST/secrets.json" 2>/dev/null || stat -f %Lp "$ST/secrets.json")" = 600 ] && [ "$(stat -c %a "$ST/secret-values.json" 2>/dev/null || stat -f %Lp "$ST/secret-values.json")" = 600 ] \
  && ok "the instance's secrets are 0600 and live outside the mounted bridge" || bad "secret modes" "$(ls -l "$ST")"
H="$(grep '^upgrade' "$IX/helm.log" | head -1)"
case "$H" in
  *backlease.yaml*|*fleet.yaml*|*publish.yaml*|*internal.yaml*|*instances-host.yaml*|*values.local.yaml\ *|*self-registry*) bad "a first-instance layer reached the instance's helm call" "$H" ;;
  *"--install memex-proof "*"-n memex-proof"*"values.local.defaults.yaml"*"registry-mode.yaml"*"fleet-host.yaml"*"instance-state/proof/values.json"*"secret-values.json"*) ok "helm: release + namespace memex-proof, layers defaults → model → registry mode → fleet host → the instance's own (no back-lease, fleet, publish, internal, bridge or overlay)" ;;
  *) bad "helm layering" "$H" ;;
esac
python3 - "$IXB/inventory.json" <<'PY2' && ok "inventory.json: the contract's shape, the instance listed with its health and registry id, the first instance not listed" || bad "inventory" "$(cat "$IXB/inventory.json" 2>/dev/null)"
import json, sys
d = json.load(open(sys.argv[1]))
assert {"schema", "sampledAt", "machine", "networkName", "agentVersion", "capacity", "instances"} <= set(d), set(d)
assert d["machine"] == "mac-1" and set(d["capacity"]) == {"vmCpu", "vmMemoryGi", "requestedCpu", "requestedMemoryGi"}
assert [i["name"] for i in d["instances"]] == ["proof"], d["instances"]
i = d["instances"][0]
want = {"name", "namespace", "release", "hostName", "url", "state", "ready", "health", "healthDetail", "nameResolves", "certificateExpires", "image", "owner", "resources", "packages", "createdAt", "registeredAs"}
assert want <= set(i), set(i) ^ want
assert i["state"] == "Running" and i["health"] == "Healthy" and i["nameResolves"] is False and i["registeredAs"] == "mac-1-proof", i
PY2
touch "$IX/ns-exists"
job local-instance-proof2-create-a "$(J local-instance-proof2-create-a create proof ",\"spec\":$SPEC")"; runonce
case "$(res local-instance-proof2-create-a)" in "Refused|"*"already exists"*) ok "creating a name that exists is Refused" ;; *) bad "duplicate create" "$(res local-instance-proof2-create-a)" ;; esac
job local-instance-proof-remove-b "$(J local-instance-proof-remove-b remove proof ',"confirm":"proof"')"; runonce
case "$(res local-instance-proof-remove-b)" in "Succeeded|"*) ;; *) bad "remove" "$(res local-instance-proof-remove-b)" ;; esac
grep -q 'registry claim' "$IXB/results/local-instance-proof-remove-b.json" && grep -q 'delete namespace memex-proof' "$IX/kubectl.log" && [ ! -d "$ST" ] \
  && ok "remove deletes the namespace and the instance's state, and says the registry claim is not released" || bad "remove effects" "$(cat "$IXB/results/local-instance-proof-remove-b.json")"
rm -f "$IX/ns-exists"
# Capacity: a VM with 1.5 CPU left refuses a 2-CPU instance, naming the numbers.
job local-instance-big-create-a "$(J local-instance-big-create-a create big ",\"spec\":${SPEC/proof.mac/big.mac}")"
ix FAKE_ALLOC_CPU=2 FAKE_REQ_CPU=500m "$CLI" instances agent run-once >/dev/null 2>&1
case "$(res local-instance-big-create-a)" in "Refused|not enough room"*"free 1.50 CPU"*"needs 2 CPU"*) ok "capacity: a create the VM has no room for is Refused, naming the numbers" ;; *) bad "capacity" "$(res local-instance-big-create-a)" ;; esac
[ ! -d "$IX/home/.memex-local/instance-state/big" ] && ok "a refused create leaves no state behind" || bad "refused create left state" ""
# The CLI refuses the same things directly.
OUT="$(ix "$CLI" instances remove proof 2>&1)"; RC=$?
[ "$RC" = 3 ] && ok "the CLI refuses a remove with no --confirm (exit 3)" || bad "cli remove" "rc=$RC $OUT"
# The bridge layer: the first instance only.
ix MEMEX_LOCAL_LIB=1 bash -c '. "$0"; write_instances_layer' "$CLI" 2>/dev/null
IF="$IX/home/.memex-local/instances-host.yaml"
grep -q 'Hosting__LocalInstances__Root: "/host-instances"' "$IF" && grep -q 'Hosting__LocalInstances__Machine: "mac-1"' "$IF" \
  && grep -q 'name: host-instances' "$IF" && grep -q "path: \"$IXB\"" "$IF" && grep -q 'mountPath: "/host-instances"' "$IF" \
  && grep -q 'name: backlease-readings' "$IF" && grep -q 'mountPath: /host-readings, readOnly: true' "$IF" \
  && ok "the first instance's bridge layer: the RW mount at /host-instances, the three keys, and the back-lease readings volume restated (a later -f replaces extraVolumes)" || bad "bridge layer" "$(cat "$IF")"
HD="$(awk '/^helm_deploy\(\) \{/,/^}$/' "$CLI")"
grep -q 'mode_args+=(-f "$INSTANCES_FILE")' <<<"$HD" && [ "$(grep -n 'mode_args+=(-f "$BACKLEASE_FILE")' <<<"$HD" | cut -d: -f1)" -lt "$(grep -n 'mode_args+=(-f "$INSTANCES_FILE")' <<<"$HD" | cut -d: -f1)" ] \
  && ok "helm_deploy layers the bridge on the first instance, after back-lease" || bad "bridge in helm_deploy" ""
# share/boot-progress.py — the terminal's reading of the portal's /_boot/progress (what changed between
# two readings, the heartbeat with the ETA, errors, the ready line); pure, so its self-test needs no cluster.
OUT="$(python3 -I "$HERE/../share/boot-progress.py" --self-test 2>&1)" && ok "boot-progress.py prints phases, landed modules, errors and the ETA heartbeat from the portal's own report" || bad "boot-progress.py self-test" "$OUT"
grep -q 'instances-host' "$HERE/../share/instances.py" && bad "instances.py names the bridge layer" "" || ok "a created instance never gets the bridge layer (instances.py does not know it)"
grep -c 'ensure_instances_agent  ' "$CLI" | grep -qx 2 && ok "up and update install the executor agent (registry mode)" || bad "agent install" "$(grep -n ensure_instances_agent "$CLI")"
rm -rf "$IX"

echo "── back-lease sampler: host.ownerCpuPercent is the owner's CPU over the interval (Hosting/BackLease §14.5) ──"
OC="$(mktemp -d)"; mkdir -p "$OC/stubs" "$OC/dir" "$OC/home/.colima/_lima/colima-build" "$OC/home/.colima/_lima/colima"
: > "$OC/home/.colima/_lima/colima-build/disk"; : > "$OC/home/.colima/_lima/colima/disk"
# the build VM is pid 20 (holds the build profile's disk), the portal VM pid 21 (the default profile's)
printf '#!/bin/bash\n[ "$1" = -t ] && case "$2" in */colima-build/disk) echo 20 ;; */_lima/colima/disk) echo 21 ;; esac\n' > "$OC/stubs/lsof"; chmod +x "$OC/stubs/lsof"
printf '#!/bin/bash\ncat "%s/ps.now"\n' "$OC" > "$OC/stubs/fakeps"; chmod +x "$OC/stubs/fakeps"
SAMPLER="$HERE/../share/backlease-sampler.sh"
oc() { env PATH="$OC/stubs:$PATH" HOME="$OC/home" COLIMA_HOME= MEMEX_LOCAL_HOME="$OC/none" MEMEX_LOCAL_SHARE= MEMEX_BUILD_PRIO_SCRIPT="${OC_GOV-$GOV}" \
  MEMEX_BACKLEASE_DIR="$OC/dir" MEMEX_BACKLEASE_INTERVAL=30 MEMEX_SAMPLER_PS="$OC/stubs/fakeps" MEMEX_SAMPLER_NOW="$1" MEMEX_BACKLEASE_SAMPLER_LIB=1 \
  bash -c '. "$0"; owner_cpu' "$SAMPLER"; }
printf '  0 1:00.00 kernel_task\n  10 0:10.00 /Applications/Safari\n  20 1:00:00.00 VZ build\n  21 30:00.00 VZ portal\n  30 0:05.00 /opt/homebrew/bin/ollama serve\n  31 0:05.00 /opt/homebrew/Cellar/ollama/0.40.14/libexec/lib/ollama/llama-server --model x\n' > "$OC/ps.now"
[ -z "$(oc 1000)" ] && ok "the first sample reports NO ownerCpuPercent (absent, never a guess)" || bad "first sample" "$(oc 1000)"
# 30 s later: Safari +30 s CPU (100 %), a new owner process +15 s (50 %); the VMs, Ollama and kernel_task all busy
printf '  0 9:00.00 kernel_task\n  10 0:40.00 /Applications/Safari\n  11 0:15.00 /usr/bin/make\n  20 2:00:00.00 VZ build\n  21 45:00.00 VZ portal\n  30 1:05.00 /opt/homebrew/bin/ollama serve\n  31 9:05.00 /opt/homebrew/Cellar/ollama/0.40.14/libexec/lib/ollama/llama-server --model x\n' > "$OC/ps.now"
[ "$(oc 1030)" = 150 ] && ok "ownerCpuPercent = the owner's CPU-time delta over the interval, excluding Ollama (serve + llama-server), both VMs and kernel_task" || bad "ownerCpuPercent value" "$(oc 1030)"
[ -z "$(oc 1200)" ] && ok "a gap longer than three intervals reports nothing (a stale window is not a reading)" || bad "stale gap" "$(oc 1200)"
[ -z "$(OC_GOV="$OC/missing.sh" oc 1230)" ] && ok "no governor library to measure with → the field is absent" || bad "no governor" ""
# the splice: only into a JSON object — a degraded host reading stays valid JSON
osp() { env HOSTJSON="$1" MEMEX_BACKLEASE_DIR="$OC/dir" MEMEX_BACKLEASE_SAMPLER_LIB=1 bash -c '. "$0"; sample_host() { printf "%s" "$HOSTJSON"; }; owner_cpu() { printf 150; }; host_cpu() { :; }; net_rate() { :; }
  sample_models() { printf "[]"; }; sample_vm() { printf null; }; colima() { return 1; }; curl() { return 1; }; sw_vers() { echo 1; }; system_profiler() { :; }
  OUT="$1"; write_sample; cat "$OUT"' "$SAMPLER" "$OC/out.json"; }
osp '{"cores":2}' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["host"]=={"cores":2,"ownerCpuPercent":150}' 2>/dev/null \
  && ok "ownerCpuPercent is spliced into the host object" || bad "splice into object" "$(osp '{"cores":2}')"
osp 'null' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["host"] is None' 2>/dev/null \
  && ok "a degraded (non-object) host reading stays valid JSON — nothing is spliced into it" || bad "splice into null" "$(osp null)"
# a baseline that cannot be rotated is no reading, and the stamp never runs ahead of the baseline it measures from
rm -f "$OC/dir/owner-cpu."*; printf '  10 0:50.00 /Applications/Safari\n' > "$OC/ps.now"
oc 2000 >/dev/null; chmod 555 "$OC/dir"; r1="$(oc 2030)"; chmod 755 "$OC/dir"
printf '  10 1:50.00 /Applications/Safari\n' > "$OC/ps.now"
r2="$(oc 2060)"
[ -z "$r1" ] && [ "$r2" = 100 ] && ok "a failed baseline rotation reports nothing, and the next reading divides by the baseline's own age (100 %, never doubled)" || bad "rotation failure" "r1=$r1 r2=$r2"
OUT="$(MEMEX_BACKLEASE_SAMPLER_LIB=1 MEMEX_BACKLEASE_DIR="$OC/dir" bash "$SAMPLER" 2>&1)"; RC=$?
[ "$RC" = 2 ] && case "$OUT" in *"for sourcing only"*) true ;; *) false ;; esac \
  && ok "library mode fails CLOSED: executed with the variable set, the sampler stops loudly instead of sampling" || bad "lib guard fails open" "rc=$RC $OUT"
rm -rf "$OC"

echo "── back-lease sampler: host.cpuPercent and the network rate (Hosting/FleetMetricHistory) ──"
NR="$(mktemp -d)"; mkdir -p "$NR/dir"
SAMPLER="$HERE/../share/backlease-sampler.sh"
printf 'Processes: 1 total\nCPU usage: 10.00%% user, 5.00%% sys, 85.00%% idle \nProcesses: 1 total\nCPU usage: 30.50%% user, 7.25%% sys, 62.25%% idle \n' > "$NR/top.out"
hc() { env MEMEX_BACKLEASE_DIR="$NR/dir" MEMEX_SAMPLER_TOP="cat $1" MEMEX_BACKLEASE_SAMPLER_LIB=1 bash -c '. "$0"; host_cpu' "$SAMPLER"; }
[ "$(hc "$NR/top.out")" = 37.8 ] && ok "cpuPercent = 100 − idle of the SECOND top sample (a delta, never the boot average)" || bad "cpuPercent" "$(hc "$NR/top.out")"
printf 'garbage\n' > "$NR/top.bad"
[ -z "$(hc "$NR/top.bad")" ] && ok "an unparseable top reports no cpuPercent (absent, never 0)" || bad "cpu garbage" "$(hc "$NR/top.bad")"
ns() { # $1 = now, $2 = en0 Ibytes, $3 = en0 Obytes
  printf 'Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll\nlo0   16384 <Link#1>                       9     0      99999        9     0      99999     0\nen0   1500  <Link#11>   aa:bb:cc:dd:ee:ff  100   0  %s  100   0  %s  0\nen0   1500  192.168.1     192.168.1.10   100   0  555  100   0  555  -\nen1   1500  <Link#12>   aa:bb:cc:dd:ee:00  1     0  1000  1   0  2000  0\nutun0 1380  <Link#20>                      5     0      77777        5     0      77777     0\n' "$2" "$3" > "$NR/netstat.out"
  env MEMEX_BACKLEASE_DIR="$NR/dir" MEMEX_BACKLEASE_INTERVAL=30 MEMEX_SAMPLER_NOW="$1" MEMEX_SAMPLER_NETSTAT="cat $NR/netstat.out" MEMEX_BACKLEASE_SAMPLER_LIB=1 \
    bash -c '. "$0"; net_rate' "$SAMPLER"; }
[ -z "$(ns 1000 1000000 500000 2>&1)" ] && ok "the first network sample reports nothing (no window yet), and says nothing on stderr" || bad "net first" "$(ns 1000 1000000 500000 2>&1)"
[ "$(ns 1030 4000000 1100000)" = "100000 20000" ] && ok "netIn/Out = the en* link-level byte delta over the interval (loopback, utun and address rows excluded)" || bad "net rate" "$(ns 1030 4000000 1100000)"
[ -z "$(ns 1200 5000000 1200000)" ] && ok "a gap longer than three intervals reports nothing" || bad "net gap" "$(ns 1200 5000000 1200000)"
[ -z "$(ns 1230 100 100)" ] && ok "a counter that went backwards (reboot, reset) reports nothing, never a negative rate" || bad "net reset" "$(ns 1230 100 100)"
[ "$(ns 1260 3100 2100)" = "100 67" ] && ok "after a reset the next window measures again" || bad "net after reset" "$(ns 1260 3100 2100)"
ws() { env HOSTJSON="$1" MEMEX_BACKLEASE_DIR="$NR/dir" MEMEX_BACKLEASE_SAMPLER_LIB=1 bash -c '. "$0"; sample_host() { printf "%s" "$HOSTJSON"; }; owner_cpu() { :; }; host_cpu() { printf 12.5; }; net_rate() { printf "100 200"; }
  sample_models() { printf "[]"; }; sample_vm() { printf null; }; colima() { return 1; }; curl() { return 1; }; sw_vers() { echo 1; }; system_profiler() { :; }
  OUT="$1"; write_sample; cat "$OUT"' "$SAMPLER" "$NR/out.json"; }
ws '{"cores":2}' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["host"]=={"cores":2,"cpuPercent":12.5,"netInBytesPerSec":100,"netOutBytesPerSec":200}' 2>/dev/null \
  && ok "cpuPercent and the network rate are spliced into the host object" || bad "net/cpu splice" "$(ws '{"cores":2}')"
ws 'null' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["host"] is None' 2>/dev/null \
  && ok "a degraded host reading stays valid JSON with the new fields too" || bad "net/cpu splice into null" "$(ws null)"
wsj() { env HOSTJSON='{"cores":2}' NET="$1" MEMEX_BACKLEASE_DIR="$NR/dir" MEMEX_BACKLEASE_SAMPLER_LIB=1 bash -c '. "$0"; sample_host() { printf "%s" "$HOSTJSON"; }; owner_cpu() { :; }; host_cpu() { :; }; net_rate() { printf "%s" "$NET"; }
  sample_models() { printf "[]"; }; sample_vm() { printf null; }; colima() { return 1; }; curl() { return 1; }; sw_vers() { echo 1; }; system_profiler() { :; }
  OUT="$1"; write_sample; cat "$OUT"' "$SAMPLER" "$NR/out.json"; }
bad_net=0; for v in '1 2junk' 'x 2' '1 ' ' 2' '12'; do
  wsj "$v" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["host"]=={"cores":2}' 2>/dev/null || bad_net=1
done
[ "$bad_net" = 0 ] && ok "a malformed network reading (non-numeric, half-empty, one token) splices nothing and the JSON stays valid" || bad "net guard" "$(wsj '1 2junk')"
rm -rf "$NR"

echo "── back-lease sampler: versions — memex-local's keg and the build host's set (Hosting MachineReadings.Versions) ──"
VR="$(mktemp -d)"; mkdir -p "$VR/stubs" "$VR/dir"
SAMPLER="$HERE/../share/backlease-sampler.sh"
# kubectl stub: answers for the build profile's context (the value in $VR/bh) and for the bare `colima` context
# of MEMEX_BUILD_PROFILE=default (the value in $VR/bh.default); any other context fails
printf '#!/bin/bash\ncase " $* " in *" --context colima-build "*) cat "%s/bh" 2>/dev/null ;; *" --context colima "*) cat "%s/bh.default" 2>/dev/null ;; *) exit 1 ;; esac\n' "$VR" "$VR" > "$VR/stubs/kubectl"; chmod +x "$VR/stubs/kubectl"
# sv SHARE [PROFILE] — PROFILE is MEMEX_BUILD_PROFILE, empty (→ build) unless given, never the ambient value
sv() { env PATH="$VR/stubs:$PATH" MEMEX_LOCAL_SHARE="$1" MEMEX_BUILD_PROFILE="${2:-}" MEMEX_BACKLEASE_DIR="$VR/dir" MEMEX_BACKLEASE_SAMPLER_LIB=1 bash -c '. "$0"; sample_versions' "$SAMPLER"; }
printf '3.1.12855' > "$VR/bh"
[ "$(sv /opt/homebrew/Cellar/memex-local/0.3.440/libexec/share)" = '{"memex-local":"0.3.440","build-host":"3.1.12855"}' ] \
  && ok "versions carry the keg's memex-local version and the build host's platform-version annotation" || bad "versions" "$(sv /opt/homebrew/Cellar/memex-local/0.3.440/libexec/share)"
[ "$(sv /some/checkout/deploy/homebrew/share)" = '{"build-host":"3.1.12855"}' ] \
  && ok "a share outside a Homebrew keg reports no memex-local version (absent, never the 0.3.0 series constant)" || bad "no keg" "$(sv /some/checkout/deploy/homebrew/share)"
: > "$VR/bh"
[ "$(sv '')" = '{}' ] && ok "no keg and no build host → an empty versions object, still valid JSON" || bad "empty versions" "$(sv '')"
printf '3.1.1"bad' > "$VR/bh"
[ "$(sv '')" = '{}' ] && ok "a value that is not version-shaped is dropped, never spliced into the JSON" || bad "version guard" "$(sv '')"
# a multi-line value whose FIRST line is version-shaped: a line-by-line match would pass it and splice the rest
printf '3.1.12855\n","injected":"x' > "$VR/bh"
[ "$(sv '')" = '{}' ] && ok "a multi-line value is dropped whole, even when its first line is version-shaped" || bad "multi-line version guard" "$(sv '')"
# MEMEX_BUILD_PROFILE=default reads the bare `colima` context, the same rule as memex-local's build_ctx
: > "$VR/bh"; printf '3.1.12900' > "$VR/bh.default"
[ "$(sv '' default)" = '{"build-host":"3.1.12900"}' ] && ok "MEMEX_BUILD_PROFILE=default reads the build host from the bare colima context" || bad "default profile" "$(sv '' default)"
rm -f "$VR/bh.default"
vw() { env PATH="$VR/stubs:$PATH" MEMEX_BUILD_PROFILE= MEMEX_LOCAL_SHARE=/opt/homebrew/Cellar/memex-local/0.3.440/libexec/share MEMEX_BACKLEASE_DIR="$VR/dir" MEMEX_BACKLEASE_SAMPLER_LIB=1 bash -c '. "$0"; sample_host() { printf "{\"cores\":2}"; }; owner_cpu() { :; }; host_cpu() { :; }; net_rate() { :; }
  sample_models() { printf "[]"; }; sample_vm() { printf null; }; colima() { return 1; }; curl() { return 1; }; sw_vers() { echo 1; }; system_profiler() { :; }
  OUT="$1"; write_sample; cat "$OUT"' "$SAMPLER" "$VR/out.json"; }
printf '3.1.12855' > "$VR/bh"
vw | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["versions"]=={"memex-local":"0.3.440","build-host":"3.1.12855"}' 2>/dev/null \
  && ok "the written readings.json carries the versions object" || bad "versions in readings.json" "$(vw)"
rm -rf "$VR"

echo "── token slots: the machine's capacity; the owner's ceiling is optional (Hosting/BackLease §14.5) ──"
TS="$(mktemp -d)"; mkdir -p "$TS/home"
printf '#!/bin/bash\nexit 0\n' > "$TS/launchctl"; chmod +x "$TS/launchctl"
ts() { env PATH="$TS:$STUBS:$PATH" HOME="$TS/home" MEMEX_LOCAL_HOME="$TS/home/.memex-local" "$@"; }
TSF="$TS/home/.memex-local/backlease.yaml"; tsh() { grep -qF "$1" "$TSF" 2>/dev/null; }
ts "$CLI" registry https://memex.example.test --id ci-ts >/dev/null 2>&1
ts env MEMEX_PROFILE=mac-studio "$CLI" backlease enroll --control https://control.example.test --deployment studio-1 --accept-tokens='*.meshweaver.cloud' --token-models qwen3.8:27b-mlx --residency CH </dev/null >/dev/null 2>&1
tsh 'Hosting__BackLease__Accept__Tokens__Slots: "2"' && ! tsh 'Tokens__MaxConcurrency' && ! tsh 'Tokens__YieldAtPercent' \
  && ok "a mac-studio with tokens on gets Slots 2 and NO owner ceiling (the whole machine); no YieldAtPercent unless asked" || bad "mac-studio slots" "$(grep Tokens "$TSF")"
ts env MEMEX_PROFILE= MEMEX_HOST_MODEL_NAME="Mac Studio" "$CLI" backlease accept tokens --slots none </dev/null >/dev/null 2>&1
tsh 'Hosting__BackLease__Accept__Tokens__Slots: "2"' && ok "with no profile, Mac Studio hardware is measured as 2 slots" || bad "hardware slots" "$(grep Tokens "$TSF")"
ts env MEMEX_PROFILE= MEMEX_HOST_MODEL_NAME="MacBook Pro" "$CLI" backlease accept tokens --slots none </dev/null >/dev/null 2>&1
! tsh 'Tokens__Slots' && ok "a host nobody measured writes no Slots (the code's default applies)" || bad "no slots on other hosts" "$(grep Tokens "$TSF")"
ts "$CLI" backlease accept tokens --slots 4 --max-concurrency 1 --yield-at 150 </dev/null >/dev/null 2>&1
tsh 'Tokens__Slots: "4"' && tsh 'Tokens__MaxConcurrency: "1"' && tsh 'Tokens__YieldAtPercent: "150"' \
  && ok "--slots (calibrated), --max-concurrency (owner ceiling) and --yield-at are written as given" || bad "slots overrides" "$(grep Tokens "$TSF")"
ts "$CLI" backlease accept tokens --max-concurrency none --yield-at none </dev/null >/dev/null 2>&1
! tsh 'Tokens__MaxConcurrency' && ! tsh 'Tokens__YieldAtPercent' && tsh 'Tokens__Slots: "4"' \
  && ok "'none' clears the owner ceiling and the yield threshold; the calibrated slots stand" || bad "none clears" "$(grep Tokens "$TSF")"
OUT="$(ts "$CLI" backlease accept tokens --slots 40 </dev/null 2>&1)" && bad "40 slots accepted" "$OUT" || ok "slots outside 1–32 are refused"
# a layer from the first opt-in release (tokens on, MaxConcurrency 1 by default, no Slots): update adds the slots and keeps the ceiling
sed -i '' -e '/Tokens__Slots/d' -e 's/Tokens__MaxConcurrency: .*/Tokens__MaxConcurrency: "1"/' "$TSF" 2>/dev/null
grep -q 'Tokens__MaxConcurrency' "$TSF" || printf '    Hosting__BackLease__Accept__Tokens__MaxConcurrency: "1"\n' >> "$TSF"
ts env MEMEX_PROFILE=mac-studio MEMEX_LOCAL_LIB=1 bash -c '. "$0"; backlease_migrate_accept' "$CLI" >/dev/null 2>&1
tsh 'Tokens__Slots: "2"' && tsh 'Tokens__MaxConcurrency: "1"' \
  && ok "update gives a pre-slots layer its measured slots and keeps the recorded ceiling (never widened without the owner)" || bad "pre-slots migration" "$(grep Tokens "$TSF")"
OUT="$(ts "$CLI" backlease accept tokens --yield-at 5 </dev/null 2>&1)" && bad "yield-at 5 accepted" "$OUT" || ok "a yield threshold outside 10–3200 % is refused"
rm -rf "$TS"

echo "── vm network shaper: fleet traffic yields to the owner's network ──"
NS="$HERE/../share/vm-netshape.sh"
NSHOME="$(mktemp -d)"
ns() { env -u MEMEX_NET_UP_KBIT -u MEMEX_NET_DOWN_KBIT -u MEMEX_NET_SHARE_PCT -u MEMEX_NET_LINK_UP_KBIT -u MEMEX_NET_LINK_DOWN_KBIT \
  MEMEX_LOCAL_HOME="$NSHOME" MEMEX_NETSHAPE_LIB=1 bash -c 'set -u; . "$0"; shift; "$@"' "$NS" _ "$@"; }
[ "$(ns caps 40000 200000 25 '' '')" = "10000 50000" ] && ok "the caps are the declared share of the measured link, each direction" || bad "share caps" "$(ns caps 40000 200000 25 '' '')"
[ "$(ns caps 40000 200000 25 3000 0)" = "3000 0" ] && ok "a declared rate wins over the share; 0 means uncapped" || bad "declared caps" "$(ns caps 40000 200000 25 3000 0)"
[ "$(ns caps 0 0 25 '' '')" = "4000 40000" ] && ok "an unknown link takes the fallback caps, never no cap" || bad "fallback caps" "$(ns caps 0 0 25 '' '')"
[ "$(ns caps 500 800 10 '' '')" = "2000 2000" ] && ok "a share below the floor is floored (2000 kbit/s: a bad probe must not throttle the line)" || bad "floor" "$(ns caps 500 800 10 '' '')"
R="$(env MEMEX_LOCAL_HOME="$NSHOME" MEMEX_NET_UP_KBIT=3000 MEMEX_NET_DOWN_KBIT=20000 bash "$NS" --render)"
sh -n <<<"$R" && ok "the in-VM script parses as POSIX sh" || bad "render parse" ""
case "$R" in *'UP_KBIT=3000'*'DOWN_KBIT=20000'*'match ip "$3" "$LOCALNET" flowid 1:10'*'stop) clear'*'mirred egress redirect dev "$IFB"'*'--set-dscp-class CS1'*) ok "the in-VM script caps both directions, exempts the Mac's subnet, marks DSCP CS1 and can remove itself" ;; *) bad "render content" "$R" ;; esac
OUT="$(env MEMEX_LOCAL_HOME="$NSHOME" MEMEX_NET_UP_KBIT=fast bash "$NS" --render 2>&1)" && bad "a non-numeric rate was accepted" "$OUT" || ok "a rate that is not a whole number is refused"
OUT="$(env MEMEX_LOCAL_HOME="$NSHOME" bash "$NS" --apply --profile 'Bad Name' 2>&1)" && bad "a bad profile was accepted" "$OUT" || ok "the shaper refuses a profile that is not a Colima profile name"
mkdir -p "$NSHOME/.ml"; printf '[ -n "${MEMEX_NET_LINK_UP_KBIT:-}" ] || MEMEX_NET_LINK_UP_KBIT=20000\n[ -n "${MEMEX_NET_LINK_DOWN_KBIT:-}" ] || MEMEX_NET_LINK_DOWN_KBIT=100000\n' > "$NSHOME/.ml/netshape-build.env"
[ "$(env -u MEMEX_NET_LINK_UP_KBIT -u MEMEX_NET_LINK_DOWN_KBIT MEMEX_LOCAL_HOME="$NSHOME/.ml" MEMEX_NET_PROFILE=default MEMEX_NETSHAPE_LIB=1 MEMEX_NET_NO_MEASURE=1 bash -c '. "$0"; link_rates' "$NS")" = "20000 100000" ] \
  && ok "another profile's measurement is reused (one uplink per Mac — the line is measured once)" || bad "link reuse" ""
# a 0/0 record (a failed measurement, as recorded before) is no reading: the sibling's measurement is reused instead
printf '[ -n "${MEMEX_NET_LINK_UP_KBIT:-}" ] || MEMEX_NET_LINK_UP_KBIT=0\n[ -n "${MEMEX_NET_LINK_DOWN_KBIT:-}" ] || MEMEX_NET_LINK_DOWN_KBIT=0\n' > "$NSHOME/.ml/netshape-default.env"
[ "$(env MEMEX_NET_LINK_UP_KBIT=0 MEMEX_NET_LINK_DOWN_KBIT=0 MEMEX_LOCAL_HOME="$NSHOME/.ml" MEMEX_NET_PROFILE=default MEMEX_NETSHAPE_LIB=1 MEMEX_NET_NO_MEASURE=1 bash -c '. "$0"; link_rates' "$NS")" = "20000 100000" ] \
  && ok "a recorded 0/0 link does not stick: another profile's measurement is reused" || bad "0/0 record sticks" ""
# apply against a stub VM: a failed measurement records NO link (so the next apply measures or reuses again), and
# MEMEX_NET_SHAPE=off — read by the shaper itself, so first-attach.sh honours it too — touches neither the VM nor the record
NSV="$(mktemp -d)"; mkdir -p "$NSV/bin" "$NSV/home"
printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/colima.log"; cat >/dev/null; exit 0\n' "$NSV" > "$NSV/bin/colima"; chmod +x "$NSV/bin/colima"
nsapply() { env -u MEMEX_NET_UP_KBIT -u MEMEX_NET_DOWN_KBIT -u MEMEX_NET_LINK_UP_KBIT -u MEMEX_NET_LINK_DOWN_KBIT PATH="$NSV/bin:$PATH" \
  MEMEX_LOCAL_HOME="$NSV/home" MEMEX_NET_NO_MEASURE=1 "$@" bash "$NS" --apply --profile build </dev/null 2>&1; }
OUT="$(nsapply env)"
{ [ -s "$NSV/colima.log" ] && [ -f "$NSV/home/netshape-build.env" ] && ! grep -q 'MEMEX_NET_LINK_UP_KBIT' "$NSV/home/netshape-build.env"; } \
  && ok "apply installs the shaper in the VM and records no link when the measurement failed" || bad "apply after a failed measurement" "$OUT | $(cat "$NSV/home/netshape-build.env" 2>/dev/null)"
rm -f "$NSV/colima.log" "$NSV/home/netshape-build.env"
OUT="$(nsapply env MEMEX_NET_SHAPE=off)"; RC=$?
{ [ "$RC" = 0 ] && [ ! -e "$NSV/colima.log" ] && [ ! -e "$NSV/home/netshape-build.env" ]; } && case "$OUT" in *"MEMEX_NET_SHAPE=off"*) true ;; *) false ;; esac \
  && ok "MEMEX_NET_SHAPE=off is honoured by the shaper itself: no VM call, no record, exit 0" || bad "MEMEX_NET_SHAPE=off in vm-netshape.sh" "rc=$RC $OUT"
case "$(cat "$HERE/../../backlease/first-attach.sh")" in *'homebrew/share/vm-netshape.sh" --apply'*) ok "first-attach.sh shapes through vm-netshape.sh --apply (so the switch above covers it)" ;; *) bad "first-attach shaper call" "" ;; esac
rm -rf "$NSV"
# `memex-local net --profile build` (no subcommand) is `status` for that profile, as vm-netshape.sh defaults a bare flag
NSS="$(mktemp -d)"; printf '#!/bin/bash\nprintf "%%s" "$*"\n' > "$NSS/vm-netshape.sh"
netbare() { env NSS_DIR="$NSS" MEMEX_LOCAL_LIB=1 bash -c '. "$0"; resolve_share() { printf "%s" "$NSS_DIR"; }; shift_net_cmd default --profile build' "$CLI" 2>&1; }
[ "$(netbare)" = "--status --profile build" ] \
  && ok "'net --profile build' with no subcommand runs status for that profile" || bad "net bare flag" "$(netbare)"
rm -rf "$NSS"
# The measurement (2026-11-10, the second Mac Studio: one combined run recorded 370 kbit/s up on a symmetric fibre
# line and capped uploads at a trickle). networkQuality is stubbed: each run answers the next line of a script, per
# direction (-d = upload only, -u = download only); "fail" answers nothing.
NQ="$(mktemp -d)"; mkdir -p "$NQ/bin" "$NQ/home"
cat > "$NQ/bin/networkQuality" <<'NQEOF'
#!/bin/bash
case " $* " in *" -d "*) dir=up ;; *" -u "*) dir=down ;; *) echo "combined run: $*" >> "$NQ_DIR/combined"; dir=down ;; esac
n=$(( $(cat "$NQ_DIR/n.$dir" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$NQ_DIR/n.$dir"
v="$(sed -n "${n}p" "$NQ_DIR/$dir")"
[ "$v" = fail ] || [ -z "$v" ] && exit 1
if [ "$dir" = up ]; then printf '{"ul_throughput": %s}' "$(( v * 1000 ))"; else printf '{"dl_throughput": %s}' "$(( v * 1000 ))"; fi
NQEOF
chmod +x "$NQ/bin/networkQuality"
nq() { rm -f "$NQ"/n.* "$NQ/combined"; printf '%s\n' $1 > "$NQ/up"; printf '%s\n' $2 > "$NQ/down"; shift 2
  env -u MEMEX_NET_LINK_UP_KBIT -u MEMEX_NET_LINK_DOWN_KBIT -u MEMEX_NET_MIN_KBIT PATH="$NQ/bin:$PATH" NQ_DIR="$NQ" MEMEX_LOCAL_HOME="$NQ/home" \
    MEMEX_NETSHAPE_LIB=1 "$@" bash -c '. "$0"; measure_link' "$NS" 2>"$NQ/err"; }
M="$(nq "370 980000 950000" "937000 940000 fail")"
[ "$M" = "950000 938500" ] && [ ! -e "$NQ/combined" ] \
  && ok "the link is the MEDIAN of per-direction runs: one bad upload probe (370 kbit/s) cannot set the cap" || bad "median of samples" "got '$M' $(cat "$NQ/combined" 2>/dev/null)"
grep -q 'up 370 980000 950000 -> median 950000; down 937000 940000 -> median 938500' "$NQ/err" \
  && ok "the raw samples are logged, per direction, with the median taken" || bad "samples logged" "$(cat "$NQ/err")"
[ "$(cat "$NQ/n.up")" = 3 ] && [ "$(cat "$NQ/n.down")" = 3 ] \
  && ok "three runs per direction by default (MEMEX_NET_SAMPLES)" || bad "sample count" "up=$(cat "$NQ/n.up") down=$(cat "$NQ/n.down")"
M="$(nq "fail fail fail" "937000 940000 941000")"
[ -z "$M" ] && grep -q 'up none -> median none' "$NQ/err" \
  && ok "no upload run answered → no reading (the fallback caps apply), and the log says so" || bad "all upload runs fail" "got '$M' $(cat "$NQ/err")"
M="$(nq "370 400 380" "237972 240000 239000")"
[ "$M" = "380 239000" ] && grep -q 'WARN: the measured uplink 380 kbit/s is under 1/50' "$NQ/err" \
  && ok "an implausible asymmetry (uplink < 1/50 of the downlink) is warned about by name" || bad "asymmetry warn" "got '$M' $(cat "$NQ/err")"
[ "$(ns caps 380 239000 25 '' '')" = "2000 59750" ] && ok "a cap derived from a bad uplink is held at the floor (MEMEX_NET_MIN_KBIT, default 2000), never 95 kbit/s" || bad "floor on derived cap" "$(ns caps 380 239000 25 '' '')"
[ "$(MEMEX_NET_MIN_KBIT=500 ns caps 380 239000 25 '' '')" = "500 59750" ] && ok "the floor is the owner's to set (MEMEX_NET_MIN_KBIT)" || bad "floor override" ""
[ "$(ns caps 380 239000 25 300 '')" = "300 59750" ] && ok "a DECLARED rate is not raised by the floor (it is the owner's)" || bad "declared below floor" ""
# the record the second Mac Studio carries (370/237972) is not reused: it is re-measured (and here, with measuring
# off, it reads as no link → fallback), and a sibling profile's implausible record is not borrowed either
[ "$(env MEMEX_NET_LINK_UP_KBIT=370 MEMEX_NET_LINK_DOWN_KBIT=237972 MEMEX_LOCAL_HOME="$NQ/home" MEMEX_NETSHAPE_LIB=1 MEMEX_NET_NO_MEASURE=1 bash -c '. "$0"; link_rates' "$NS" 2>"$NQ/err")" = "0 0" ] \
  && grep -q 'implausible — measuring again' "$NQ/err" && ok "an implausible RECORD (370 up / 237972 down) is said and measured again, never reused" || bad "implausible record reused" "$(cat "$NQ/err")"
printf '[ -n "${MEMEX_NET_LINK_UP_KBIT:-}" ] || MEMEX_NET_LINK_UP_KBIT=370\n[ -n "${MEMEX_NET_LINK_DOWN_KBIT:-}" ] || MEMEX_NET_LINK_DOWN_KBIT=237972\n' > "$NQ/home/netshape-build.env"
[ "$(env -u MEMEX_NET_LINK_UP_KBIT -u MEMEX_NET_LINK_DOWN_KBIT MEMEX_LOCAL_HOME="$NQ/home" MEMEX_NET_PROFILE=default MEMEX_NETSHAPE_LIB=1 MEMEX_NET_NO_MEASURE=1 bash -c '. "$0"; link_rates' "$NS" 2>/dev/null)" = "0 0" ] \
  && ok "another profile's implausible record is not borrowed" || bad "implausible sibling borrowed" ""
# end to end through --measure: the record keeps the raw samples as a comment
NQV="$NQ/vm"; mkdir -p "$NQV"; printf '#!/bin/bash\ncat >/dev/null; exit 0\n' > "$NQ/bin/colima"; chmod +x "$NQ/bin/colima"
rm -f "$NQ"/n.*; printf '%s\n' 900000 920000 910000 > "$NQ/up"; printf '%s\n' 930000 940000 950000 > "$NQ/down"
OUT="$(env -u MEMEX_NET_UP_KBIT -u MEMEX_NET_DOWN_KBIT -u MEMEX_NET_LINK_UP_KBIT -u MEMEX_NET_LINK_DOWN_KBIT PATH="$NQ/bin:$PATH" NQ_DIR="$NQ" MEMEX_LOCAL_HOME="$NQ/home" bash "$NS" --measure --profile build </dev/null 2>&1)"
grep -q 'MEMEX_NET_LINK_UP_KBIT=910000' "$NQ/home/netshape-build.env" && grep -q '^# networkQuality samples .*up 900000 920000 910000 -> median 910000' "$NQ/home/netshape-build.env" \
  && case "$OUT" in *"capped at 227500 kbit/s up, 235000 kbit/s down"*) true ;; *) false ;; esac \
  && ok "--measure records the medians, keeps the raw samples in the record, and caps at the share of the medians" || bad "--measure record" "$OUT | $(cat "$NQ/home/netshape-build.env")"
rm -rf "$NQ"
rm -rf "$NSHOME"

echo "── the second Mac Studio's defects, pinned (2026-11-09) ─────────────"
OB="$(mktemp -d)"; mkdir -p "$OB/stubs" "$OB/brewbin" "$OB/home/.memex-local" "$OB/sys"
# a PATH with the system's tools but NO kubectl/colima (and none of Homebrew's): what the person's shell had
for f in /usr/bin/* /bin/*; do b="$(basename "$f")"; case "$b" in kubectl|colima|helm|mkcert|security|open) continue ;; esac; [ -e "$OB/sys/$b" ] || ln -s "$f" "$OB/sys/$b"; done
# kubectl that serves the setup banner; curl that says the portal answers
printf '#!/usr/bin/env bash\ncase "$*" in *logs*) printf "info: Setup\\n  Token: tok-ob-123\\n" ;; *version*) echo "Client Version: v1.37.1" ;; esac\nexit 0\n' > "$OB/brewbin/kubectl"
printf '#!/usr/bin/env bash\nprintf 302\n' > "$OB/stubs/curl"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "$FAKE_OPEN_LOG"\n' > "$OB/stubs/open"
printf '#!/usr/bin/env bash\n[ "$1" = verify-cert ] && [ -z "${FAKE_UNTRUSTED:-}" ]\n' > "$OB/stubs/security"
chmod +x "$OB/stubs/"* "$OB/brewbin/"*
PGPW="pgsecret-$(openssl rand -hex 12)"
printf 'secrets:\n  memex_postgres_password: "%s"\n' "$PGPW" > "$OB/home/.memex-local/values.local.yaml"
obrun() { OUT="$(env -i HOME="$OB/home" MEMEX_LOCAL_HOME="$OB/home/.memex-local" PATH="$OB/stubs:$OB/sys" FAKE_OPEN_LOG="$OB/open.log" "$@" bash "$CLI" setup 2>&1)"; RC=$?; : ; }
# Defect 5 (the PATH half is memex_toolchain_path, #217): a command that needs kubectl/colima and finds none FAILS
# naming it — never an empty answer. Sourced, so no toolchain directory is added: the PATH is exactly this one.
obsrc() { OUT="$(env -i HOME="$OB/home" MEMEX_LOCAL_HOME="$OB/home/.memex-local" PATH="$OB/stubs:$OB/sys" MEMEX_LOCAL_LIB=1 bash -c '. "$0"; '"$1" "$CLI" 2>&1)"; RC=$?; }
obsrc 'cmd_setup --no-open'
case "$RC:$OUT" in 0:*) bad "setup without kubectl fails" "$OUT" ;; *"required tool 'kubectl' not found"*"brew install kubernetes-cli"*) ok "setup without kubectl FAILS naming kubectl (defect 5: it used to print nothing)" ;; *) bad "setup names kubectl" "rc=$RC $OUT" ;; esac
for c in verify status logs; do
  obsrc "cmd_$c"
  case "$RC:$OUT" in 0:*) bad "$c without the tools fails" "$OUT" ;; *"required tool '"*"' not found"*) ok "$c without colima/kubectl FAILS naming the missing tool" ;; *) bad "$c names the tool" "rc=$RC $OUT" ;; esac
done
cp "$OB/brewbin/kubectl" "$OB/stubs/kubectl"
# Defect 7: setup never prints the in-cluster Postgres password — only where it is and its fingerprint.
obrun FAKE_UNTRUSTED=1
case "$OUT" in *"$PGPW"*) bad "setup never prints the Postgres password" "the value is in the output" ;; *) ok "setup never prints the in-cluster Postgres password (defect 7)" ;; esac
case "$OUT" in *"Password=<memex_postgres_password in $OB/home/.memex-local/values.local.yaml>"*"its fingerprint: sha256:"*) ok "…it names where the password is, with its fingerprint" ;; *) bad "password pointer" "$OUT" ;; esac
# Defect 10: the browser is never opened into an untrusted certificate.
[ ! -s "$OB/open.log" ] && case "$OUT" in *"not opening the browser"*"not trusted"*) true ;; *) false ;; esac \
  && ok "setup does NOT open the wizard while the served certificate is untrusted, and says what is missing (defect 10)" || bad "no open when untrusted" "open: $(cat "$OB/open.log") $OUT"
: > "$OB/open.log"; obrun
[ "$(cat "$OB/open.log")" = "https://memex.localhost:8443/setup?token=tok-ob-123" ] && ok "…and opens it once the certificate is trusted" || bad "open when trusted" "$(cat "$OB/open.log") $OUT"
! grep -nE '(^|[^_])open "\$\{url\}\?token' "$CLI" && ok "no code path opens the token URL without the trust check" || bad "unguarded open" "$(grep -n 'open "${url}?token' "$CLI")"

# Participation (contract §5): the build host only on the owner's YES to jobs; no extra token models after a NO.
pb() { env MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_HOME="$OB/home/.memex-local" HOME="$OB/home" bash -c '. "$0"; '"$1" "$CLI" 2>&1; }
BL="$OB/home/.memex-local/backlease.yaml"
rm -f "$BL"
[ "$(pb 'participation_build_host yes && echo BUILD || echo NONE')" = NONE ] && ok "no recorded answer → no build host (no answer is not a yes)" || bad "unanswered builds" ""
[ "$(pb 'ACC_JOBS_ANSWERED=yes; ACC_JOBS_ENABLED=true; participation_build_host yes && echo BUILD || echo NONE')" = BUILD ] && ok "no layer, but --accept-jobs on this join → build host" || bad "flag yes" ""
printf 'config:\n  memex_portal:\n    Hosting__BackLease__Accept__Jobs__Enabled: "false"\n    Hosting__BackLease__Accept__Tokens__Enabled: "false"\n' > "$BL"
[ "$(pb 'participation_build_host yes && echo BUILD || echo NONE')" = NONE ] && ok "jobs NO recorded → no build host" || bad "jobs no builds" ""
[ "$(pb 'ACC_JOBS_ANSWERED=yes; ACC_JOBS_ENABLED=true; participation_build_host yes && echo BUILD || echo NONE')" = NONE ] && ok "…the recorded NO wins over a stale flag value" || bad "recorded no" ""
OUT="$(pb 'MEMEX_HOST_CORES=8 MEMEX_HOST_MEM_GIB=64 cmd_buildhost apply; echo SURVIVED')"
case "$OUT" in *SURVIVED*) bad "buildhost apply refused after a NO to jobs" "$OUT" ;; *"owner said NO to fleet jobs"*) ok "buildhost apply is REFUSED after the owner's NO to jobs" ;; *) bad "buildhost refusal" "$OUT" ;; esac
[ "$(pb 'participation_answer Tokens')" = false ] && ok "the recorded token answer is read" || bad "token answer" ""
printf 'config:\n  memex_portal:\n    Hosting__BackLease__Accept__Jobs__Enabled: "true"\n' > "$BL"
[ "$(pb 'participation_build_host yes && echo BUILD || echo NONE')" = BUILD ] && ok "jobs YES recorded → build host" || bad "jobs yes" ""
[ "$(pb 'participation_build_host no && echo BUILD || echo NONE')" = NONE ] && ok "--no-buildhost wins over a YES" || bad "no-buildhost" ""
grep -q '^        if participation_build_host "\$with_build"; then$' "$CLI" && ok "fleet join installs the build host only through participation_build_host" || bad "join gate" ""
case "$(fn_body setup_ollama)" in *'"$(participation_answer Tokens)" = false'*"are not pulled"*) ok "setup_ollama pulls no extra token models after the owner's NO to tokens" ;; *) bad "extras gate" "" ;; esac
rm -f "$BL"

# Defect 4 (Mac half): `memex-local ca adopt` — the fleet local CA, name-constrained, key never printed.
MINT="$HERE/../../macos/fleet-ca/mint-fleet-ca.sh"
CA="$OB/ca"; mkdir -p "$CA/caroot" "$CA/bin" "$CA/home/.memex-local/enrol"
bash "$MINT" --key-file "$CA/fleet.key" --cert-out "$CA/fleet.pem" >/dev/null 2>&1
bash "$MINT" --key-file "$CA/other.key" --cert-out "$CA/other.pem" >/dev/null 2>&1
FSHA="$(openssl x509 -in "$CA/fleet.pem" -outform DER | shasum -a 256 | cut -d' ' -f1)"
KEYB64="$(cat "$CA/fleet.key")"
# mkcert: CAROOT from the environment; a leaf "issued" by signing nothing — the test is about what adopt does
cat > "$CA/bin/mkcert" <<'MK'
#!/usr/bin/env bash
case "$1" in -CAROOT) printf '%s\n' "$CAROOT"; exit 0 ;; esac
printf '%s\n' "$*" >> "$FAKE_MKCERT_LOG"
[ -f "$CAROOT/rootCA-key.pem" ] || exit 1
c=""; k=""; prev=""; for a in "$@"; do [ "$prev" = -cert-file ] && c="$a"; [ "$prev" = -key-file ] && k="$a"; prev="$a"; done
printf 'leaf\n' > "$c"; printf 'leafkey\n' > "$k"
MK
chmod +x "$CA/bin/mkcert"
adopt() { OUT="$(env PATH="$CA/bin:$PATH" CAROOT="$CA/caroot" HOME="$CA/home" MEMEX_LOCAL_HOME="$CA/home/.memex-local" FAKE_MKCERT_LOG="$CA/mkcert.log" MEMEX_KUBE_CONTEXT=none "$@" 2>&1)"; RC=$?; }
printf 'old\n' > "$CA/caroot/rootCA.pem"; printf 'oldkey\n' > "$CA/caroot/rootCA-key.pem"
cp "$CA/fleet.key" "$CA/home/.memex-local/enrol/fleet-ca.key"; chmod 600 "$CA/home/.memex-local/enrol/fleet-ca.key"
# the forms the minted fleet CA is stored in: the key as ONE line of base64 PKCS#8 DER (the vault object, enrolment
# item `a`), the certificate permitting DNS:localhost alone (RFC 5280: it covers every name below localhost)
[ "$(wc -l < "$CA/fleet.key" | tr -d ' ')" = 1 ] && ! grep -q -- '-----BEGIN' "$CA/fleet.key" \
  && openssl x509 -in "$CA/fleet.pem" -noout -text | grep -q '^ *DNS:localhost$' && ! openssl x509 -in "$CA/fleet.pem" -noout -text | grep -q 'DNS:\.localhost' \
  && ok "the adopted pair is in the stored form: one line of base64 PKCS#8 DER, a CA permitting DNS:localhost alone" || bad "stored form" "$(cat "$CA/fleet.key" | wc -l)"
adopt "$CLI" ca adopt --cert "$CA/fleet.pem" --sha256 "$FSHA"
case "$RC:$OUT" in 0:*"fleet local CA adopted"*"re-issued under the fleet CA"*) ok "ca adopt installs the fleet CA (the join's drop, the declared sha256) and re-issues the portal's certificate" ;; *) bad "ca adopt" "rc=$RC $OUT" ;; esac
case "$OUT" in *"$KEYB64"*|*"PRIVATE KEY"*) bad "ca adopt never prints the key" "$OUT" ;; *) ok "ca adopt never prints the key (fingerprints only)" ;; esac
[ "$(openssl x509 -in "$CA/caroot/rootCA.pem" -outform DER | shasum -a 256 | cut -d' ' -f1)" = "$FSHA" ] \
  && [ "$(openssl pkey -in "$CA/caroot/rootCA-key.pem" -pubout -outform DER | shasum -a 256)" = "$(openssl x509 -in "$CA/fleet.pem" -noout -pubkey | openssl pkey -pubin -outform DER | shasum -a 256)" ] \
  && ok "mkcert's CAROOT now holds the fleet CA and its key" || bad "caroot contents" "$(ls -la "$CA/caroot")"
[ "$(ls -l "$CA/caroot/rootCA-key.pem" | cut -c1-10)" = "-r--------" ] && ok "the CA key is 0400" || bad "key mode" "$(ls -l "$CA/caroot/rootCA-key.pem")"
ls "$CA/caroot"/replaced-*/rootCA.pem >/dev/null 2>&1 && [ "$(cat "$CA/caroot"/replaced-*/rootCA.pem)" = old ] && ok "the previous local CA is moved aside, never deleted" || bad "previous CA kept" "$(ls -R "$CA/caroot")"
[ ! -e "$CA/home/.memex-local/enrol/fleet-ca.key" ] && ok "the join's key drop is removed once adopted" || bad "drop removed" ""
grep -q -- "-cert-file $CA/home/.memex-local/memex.localhost.pem -key-file $CA/home/.memex-local/memex.localhost-key.pem memex.localhost \*.memex.localhost" "$CA/mkcert.log" \
  && ok "the portal's certificate (memex.localhost, *.memex.localhost) is re-issued under the adopted CA" || bad "re-issue" "$(cat "$CA/mkcert.log")"
adopt bash -c 'cat "$1" | "$2" ca adopt --key-stdin --cert "$3" --sha256 "$4"' _ "$CA/fleet.key" "$CLI" "$CA/fleet.pem" "$FSHA"
case "$RC:$OUT" in 0:*"adopted"*) ok "--key-stdin adopts (idempotent: the same CA again)" ;; *) bad "key stdin" "rc=$RC $OUT" ;; esac
[ "$(ls -d "$CA/caroot"/replaced-* | wc -l | tr -d ' ')" = 1 ] && ok "re-adopting the same CA moves nothing aside" || bad "re-adopt aside" "$(ls "$CA/caroot")"
chmod 600 "$CA/other.key"
adopt "$CLI" ca adopt --key-file "$CA/other.key" --cert "$CA/fleet.pem" --sha256 "$FSHA"
case "$RC:$OUT" in 0:*) bad "a key of another CA is refused" "$OUT" ;; *"does not belong to the certificate"*) ok "a key that does not belong to the declared certificate is REFUSED" ;; *) bad "wrong key" "rc=$RC $OUT" ;; esac
adopt "$CLI" ca adopt --key-file "$CA/fleet.key" --cert "$CA/other.pem" --sha256 "$FSHA"
case "$RC:$OUT" in 0:*) bad "a certificate other than the declared one is refused" "$OUT" ;; *"not the declared"*) ok "a certificate whose sha256 is not the declared one is REFUSED" ;; *) bad "wrong cert" "rc=$RC $OUT" ;; esac
chmod 644 "$CA/fleet.key"
adopt "$CLI" ca adopt --key-file "$CA/fleet.key" --cert "$CA/fleet.pem" --sha256 "$FSHA"
case "$RC:$OUT" in 0:*) bad "a key file readable by others is refused" "$OUT" ;; *"not 0600"*) ok "a key file others can read is REFUSED" ;; *) bad "key mode refusal" "rc=$RC $OUT" ;; esac
chmod 600 "$CA/fleet.key"
adopt "$CLI" ca adopt --key-file "$CA/fleet.key" --cert "$CA/fleet.pem"
case "$RC:$OUT" in 0:*) bad "no declared fingerprint is refused" "$OUT" ;; *"no declared fleet CA fingerprint"*) ok "without a declared fingerprint (--sha256 / FleetCaSha256) nothing is adopted" ;; *) bad "no sha" "rc=$RC $OUT" ;; esac
# a CA that is NOT name-constrained to the loopback names — the right key for it, still refused
mkca() { printf '[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=t\n[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign\n%s\n' "$2" > "$CA/$1.cnf"
  openssl ecparam -name prime256v1 -genkey -noout -out "$CA/$1.ec" 2>/dev/null
  openssl req -new -x509 -key "$CA/$1.ec" -config "$CA/$1.cnf" -extensions v3 -days 2 -out "$CA/$1.pem" 2>/dev/null
  ( umask 077; openssl pkcs8 -topk8 -nocrypt -in "$CA/$1.ec" -outform DER | openssl base64 -A > "$CA/$1.key" ); }
mkca wide 'nameConstraints=critical,permitted;DNS:localhost,permitted;DNS:example.com'
adopt "$CLI" ca adopt --key-file "$CA/wide.key" --cert "$CA/wide.pem" --sha256 "$(openssl x509 -in "$CA/wide.pem" -outform DER | shasum -a 256 | cut -d' ' -f1)"
case "$RC:$OUT" in 0:*) bad "a CA permitting more than the loopback names is refused" "$OUT" ;; *"permit 'DNS:example.com'"*) ok "a CA that permits a name beyond localhost is REFUSED" ;; *) bad "wide CA" "rc=$RC $OUT" ;; esac
mkca bare ''
adopt "$CLI" ca adopt --key-file "$CA/bare.key" --cert "$CA/bare.pem" --sha256 "$(openssl x509 -in "$CA/bare.pem" -outform DER | shasum -a 256 | cut -d' ' -f1)"
case "$RC:$OUT" in 0:*) bad "an unconstrained CA is refused" "$OUT" ;; *"no CRITICAL Name Constraints"*) ok "an UNCONSTRAINED CA is refused" ;; *) bad "bare CA" "rc=$RC $OUT" ;; esac
mkca soft 'nameConstraints=permitted;DNS:localhost'
adopt "$CLI" ca adopt --key-file "$CA/soft.key" --cert "$CA/soft.pem" --sha256 "$(openssl x509 -in "$CA/soft.pem" -outform DER | shasum -a 256 | cut -d' ' -f1)"
case "$RC:$OUT" in 0:*) bad "non-critical constraints are refused" "$OUT" ;; *"no CRITICAL Name Constraints"*) ok "Name Constraints that are not CRITICAL are refused" ;; *) bad "soft CA" "rc=$RC $OUT" ;; esac
[ "$(env MEMEX_LOCAL_LIB=1 bash -c '. "$0"; norm_sha256 "$1"' "$CLI" "sha256:$(printf '%s' "$FSHA" | tr a-f A-F | sed 's/../&:/g; s/:$//')")" = "$FSHA" ] \
  && ok "a fingerprint with colons, upper case or a sha256: prefix is normalised" || bad "norm_sha256" ""
# setup_tls does not ask for a trust change when the CA is already trusted (the adopted fleet CA)
case "$(fn_body setup_tls)" in *'ca_cert_trusted "$caroot/rootCA.pem"; then'*'no trust change needed'*'MEMEX_CA_TRUST:-}" = defer'*'TRUST_STORES=nss mkcert -install'*'else'*'mkcert -install'*) ok "up runs 'mkcert -install' only when the local CA is not already trusted (and never under MEMEX_CA_TRUST=defer)" ;; *) bad "setup_tls trust" "$(fn_body setup_tls)" ;; esac
rm -rf "$OB"

echo "── the CLI still knows every command it documents ────────────────"

help_out="$("$CLI" help 2>&1)"
for cmd in up down status setup logs update registry verify port-forward observability doctor profile channel cert internal backlease buildhost net fleet llm residency relay publish autoupdate instances ca; do
  case "$help_out" in
    *"$cmd"*) ;;
    *) bad "help documents '$cmd'" "not in the usage text"; continue ;;
  esac
  if grep -q "^    ${cmd})" "$CLI"; then ok "help documents '$cmd', and main dispatches it"
  else bad "help documents '$cmd'" "no dispatch arm in main()"; fi
done

echo "── the toolchain is found from the install, not the caller's PATH ──"
KEG="$(mktemp -d)"; mkdir -p "$KEG/Cellar/memex-local/9.9.9/libexec" "$KEG/bin"; cp "$CLI" "$KEG/Cellar/memex-local/9.9.9/libexec/memex-local"
cp -R "$(dirname "$CLI")/../share" "$KEG/Cellar/memex-local/9.9.9/libexec/share" 2>/dev/null || true
KB="$(cd "$KEG/bin" && pwd -P)"
TP="$(env -i HOME="$HOME" PATH=/usr/bin:/bin MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_SHARE="$(cd "$(dirname "$CLI")/../share" && pwd)" bash -c '. "$0"; memex_toolchain_path; printf "%s" "$PATH"' "$KEG/Cellar/memex-local/9.9.9/libexec/memex-local" 2>/dev/null)"
case ":$TP:" in *":$KB:"*) ok "an installed keg finds its Homebrew prefix's bin/ even from a shell without it on PATH (memex-local setup finds kubectl)" ;; *) bad "toolchain PATH from the keg" "$TP" ;; esac
TP2="$(env -i HOME="$HOME" PATH="$KB:/usr/bin:/bin" MEMEX_LOCAL_LIB=1 MEMEX_LOCAL_SHARE="$(cd "$(dirname "$CLI")/../share" && pwd)" bash -c '. "$0"; memex_toolchain_path; memex_toolchain_path; printf "%s" "$PATH"' "$KEG/Cellar/memex-local/9.9.9/libexec/memex-local" 2>/dev/null)"
[ "$(printf '%s' "$TP2" | tr ':' '\n' | grep -cx "$KB")" = "1" ] && ok "the prefix's bin/ is added once (idempotent), and a caller's PATH keeps its order" || bad "toolchain PATH idempotent" "$TP2"
TP3="$(env -i HOME="$HOME" PATH=/usr/bin:/bin MEMEX_LOCAL_LIB=1 bash -c '. "$0"; memex_toolchain_path; printf "%s" "$PATH"' "$CLI" 2>/dev/null)"
want="/usr/bin:/bin"; [ "$(uname -s)" = Darwin ] && [ -d /usr/local/bin ] && want="$want:/usr/local/bin"
[ "$TP3" = "$want" ] && ok "a source checkout gets no prefix (only a Mac's /usr/local/bin): the test stubs and a Linux runner are untouched" || bad "checkout PATH unchanged" "$TP3"
rm -rf "$KEG"
grep -q 'need kubectl kubernetes-cli' <(sed -n '/^cmd_setup() {/,/^}/p' "$CLI") && grep -q 'could not read the portal log with kubectl' "$CLI" \
  && ok "setup resolves kubectl and FAILS naming it — never an empty token" || bad "setup kubectl loud" "missing"
! grep -q 'Password=\${pg_pw}' "$CLI" && grep -q 'Password=<memex_postgres_password in' "$CLI" \
  && ok "setup never prints the in-cluster Postgres password" || bad "postgres password masked" "still printed"

echo "── setup --apply: the first-run setup through its API, consent explicit ──"
SAPY="$(env MEMEX_LOCAL_LIB=1 bash -c '. "$0"; printf "%s" "$SETUP_ANSWERS_PY"' "$CLI")"
SA="$(mktemp -d)"
if env SETUP_NAME=studio-2 SETUP_COMPANY=Acme SETUP_OWNER_NAME="Jane Roe" SETUP_OWNER_EMAIL=jane.roe@example.com SETUP_ACCEPT=no \
     SETUP_EMBED_ENDPOINT=http://e/v1 SETUP_EMBED_MODEL=m python3 -I -c "$SAPY" "$SA/a.json" 2>"$SA/err"; then bad "no consent → nothing sent" "accepted"
else grep -q 'identity.consent' "$SA/err" && [ ! -e "$SA/a.json" ] && ok "setup --apply without --accept-terms sends nothing and names identity.consent" || bad "consent named" "$(cat "$SA/err")"; fi
env SETUP_NAME=studio-2 SETUP_COMPANY= SETUP_OWNER_NAME= SETUP_OWNER_EMAIL=jane.roe@example.com SETUP_ACCEPT=yes \
  SETUP_EMBED_ENDPOINT=http://e/v1 SETUP_EMBED_MODEL=m python3 -I -c "$SAPY" "$SA/b.json" 2>"$SA/err2" \
  && bad "missing company/owner → refused" "accepted" || { grep -q 'identity.company' "$SA/err2" && grep -q 'identity.ownerName' "$SA/err2" && ok "every missing answer is named by its field" || bad "missing fields named" "$(cat "$SA/err2")"; }
env SETUP_NAME=studio-2 SETUP_COMPANY=Acme SETUP_OWNER_NAME="Jane Roe" SETUP_OWNER_EMAIL=jane.roe@example.com SETUP_ACCEPT=yes SETUP_REGISTRY=https://registry.example.com \
  SETUP_EMBED_ENDPOINT=http://e/v1 SETUP_EMBED_MODEL=m python3 -I -c "$SAPY" "$SA/c.json" \
  && python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); i=d["identity"]; assert i["consent"] is True and i["name"]=="studio-2" and i["company"]=="Acme" and i["registry"]=="https://registry.example.com"; assert d["embedding"]=={"endpoint":"http://e/v1","model":"m"}; assert "storage" not in d' "$SA/c.json" \
  && ok "a complete answer carries identity + consent + this install's embeddings, and leaves storage to the wizard's pre-selected default" || bad "complete answers" "$(cat "$SA/c.json" 2>/dev/null)"
grep -q 'X-Setup-Token: %s' "$CLI" && ! grep -qE 'curl[^\n]*X-Setup-Token: \$' "$CLI" && ok "the setup token travels in a 0600 header file, never on a command line" || bad "token header file" "missing"
rm -rf "$SA"

# fleet join: the enrolment's registry-plan coupon (item r) becomes this install's registration key — so its portal
# registers on, or redeems itself up to, the fleet's plan without a global administrator raising it by hand.
RK="$(mktemp -d)"; mkdir -p "$RK/home/.memex-local" "$RK/items"
rk() { env HOME="$RK/home" MEMEX_LOCAL_HOME="$RK/home/.memex-local" MEMEX_LOCAL_LIB=1 bash -c ". \"$CLI\"; fleet_apply_registry_key \"\$0\"" "$RK/items" 2>&1; }
rk_file() { cat "$RK/home/.memex-local/registry.yaml" 2>/dev/null; }
rk_open() { env HOME="$RK/home" MEMEX_LOCAL_HOME="$RK/home/.memex-local" MEMEX_LOCAL_LIB=1 bash -c ". \"$CLI\"; write_registry_file \"\$0\" '' \"\$1\"" "$1" "$2"; }
rk_open https://registry.example.test 595714be-0000-4000-8000-000000000001
printf 'mwr_fleet-coupon-NEVER-PRINTED' > "$RK/items/r"
RKO="$(rk)"
rk_file | grep -q '^    PluginCatalog__BootstrapKey: "mwr_fleet-coupon-NEVER-PRINTED"$' \
  && ok "fleet join: the coupon (item r) is written as the registry file's bootstrap key" || bad "coupon written" "$(rk_file)"
rk_file | grep -q 'registryUrl: "https://registry.example.test"' && rk_file | grep -q 'instanceId: "595714be-0000-4000-8000-000000000001"' \
  && ok "…keeping the registry URL and the pinned instance id" || bad "url and id kept" "$(rk_file)"
# GNU first: GNU `stat -f` is the FILESYSTEM report and exits 0, so a BSD-first fallback never reaches `-c` on Linux
[ "$(stat -c '%a' "$RK/home/.memex-local/registry.yaml" 2>/dev/null || stat -f '%Lp' "$RK/home/.memex-local/registry.yaml")" = "600" ] \
  && ok "…in a 0600 file" || bad "registry file mode" "$(ls -l "$RK/home/.memex-local/registry.yaml")"
case "$RKO" in *NEVER-PRINTED*|*mwr_*) bad "…and the coupon is never printed" "said: $RKO" ;; *"registry plan:"*sha256:*) ok "…and the coupon is never printed (a fingerprint only)" ;; *) bad "coupon fingerprint said" "said: $RKO" ;; esac
[ "$(printf '%s' "$RKO" | grep -o 'sha256:[0-9a-f]*')" = "$(printf 'sha256:%s' "$(printf '%s' 'mwr_fleet-coupon-NEVER-PRINTED' | shasum -a 256 | cut -c1-12)")" ] \
  && ok "…and the fingerprint said is the one of the key written" || bad "fingerprint of the written key" "said: $RKO"
printf '  mwr_fleet-coupon-NEVER-PRINTED \n' > "$RK/items/r"; RKO="$(rk)"
rk_file | grep -q '^    PluginCatalog__BootstrapKey: "mwr_fleet-coupon-NEVER-PRINTED"$' \
  && [ "$(printf '%s' "$RKO" | grep -o 'sha256:[0-9a-f]*' | tail -n1)" = "$(printf 'sha256:%s' "$(printf '%s' 'mwr_fleet-coupon-NEVER-PRINTED' | shasum -a 256 | cut -c1-12)")" ] \
  && ok "fleet join: a padded drop is trimmed, and the fingerprint is of the trimmed key" || bad "trimmed key" "$RKO / $(rk_file)"
env HOME="$RK/home" MEMEX_LOCAL_HOME="$RK/home/.memex-local" MEMEX_LOCAL_LIB=1 bash -c ". \"$CLI\"; write_registry_file https://registry.example.test mwr_older-key-NEVER-PRINTED ''"
printf 'mwr_fleet-coupon-NEVER-PRINTED' > "$RK/items/r"; RKO="$(rk)"
rk_file | grep -q 'BootstrapKey: "mwr_fleet-coupon-NEVER-PRINTED"' && case "$RKO" in *"replacing the registration key already in"*) true ;; *) false ;; esac \
  && case "$RKO" in *NEVER-PRINTED*|*mwr_*) false ;; *) true ;; esac \
  && ok "fleet join: a key already in the file is replaced by the coupon, and that is SAID (fingerprints only)" || bad "replace said" "$RKO / $(rk_file)"
RKO="$(rk)"
case "$RKO" in *"replacing"*) bad "re-applying the same coupon is silent" "said: $RKO" ;; *) ok "…while re-applying the same coupon says nothing about replacing" ;; esac
rk_open https://registry.example.test ""
RKO="$(rk)"
rk_file | grep -q 'PluginCatalog__BootstrapKey: "mwr_' && ! rk_file | grep -q 'instanceId:' \
  && ok "fleet join: with no pinned id the wizard's id stays in charge (no id is invented)" || bad "no id invented" "$(rk_file)"
rk_open https://registry.example.test ""; rm -f "$RK/items/r"; RKO="$(rk)"
! rk_file | grep -q 'BootstrapKey' && case "$RKO" in *"no coupon in the code"*) true ;; *) false ;; esac \
  && ok "fleet join: a code without item r leaves the registry file as it was, and says so" || bad "no r" "$RKO / $(rk_file)"
printf 'mwi_not-a-registration-key' > "$RK/items/r"; RKO="$(rk)"
! rk_file | grep -q 'BootstrapKey' && case "$RKO" in *"not a registration key"*) true ;; *) false ;; esac \
  && ok "fleet join: an item r that is not mwr_ is refused and nothing is written" || bad "non-mwr r" "$RKO / $(rk_file)"
rm -f "$RK/home/.memex-local/registry.yaml"; printf 'mwr_fleet-coupon-NEVER-PRINTED' > "$RK/items/r"; RKO="$(rk)"
[ ! -e "$RK/home/.memex-local/registry.yaml" ] && case "$RKO" in *"not in REGISTRY MODE"*) true ;; *) false ;; esac \
  && ok "fleet join: outside registry mode the coupon is not kept, and that is said" || bad "not registry mode" "$RKO"
grep -q '        fleet_apply_registry_key "$items"' "$CLI" && ok "fleet join applies the coupon with the other items" || bad "join applies r" "missing"
rm -rf "$RK"
echo "── kubelet's root on the portal VM's data disk (2026-11-10) ──────────"
KR="$(mktemp -d)"; mkdir -p "$KR/stubs" "$KR/home/.memex-local" "$KR/colima/default"
cat > "$KR/stubs/colima" <<'KEOF'
#!/bin/bash
echo "$*" >> "$KR_DIR/colima.log"
case "$1" in
  status) [ "${KR_RUNNING:-yes}" = yes ] ;;
  ssh) cat >/dev/null
       # KR_SSH_EMPTY_FIRST=N: the first N reads see no pods/ yet (kubelet still starting)
       n=$(( $(grep -c '^ssh' "$KR_DIR/colima.log") ))
       [ -n "${KR_SSH_ERR:-}" ] && { echo "$KR_SSH_ERR" >&2; exit 1; }
       [ "$n" -le "${KR_SSH_EMPTY_FIRST:-0}" ] && exit 1
       printf '%s' "${KR_SSH-81G free of 98G}" ;;
  *) exit 0 ;;
esac
KEOF
printf '#!/bin/bash\necho "$*" >> "$KR_DIR/kubectl.log"; exit 0\n' > "$KR/stubs/kubectl"; chmod +x "$KR/stubs/colima" "$KR/stubs/kubectl"
kr() { env PATH="$KR/stubs:$PATH" HOME="$KR/home" COLIMA_HOME="$KR/colima" MEMEX_LOCAL_HOME="$KR/home/.memex-local" KR_DIR="$KR" MEMEX_LOCAL_LIB=1 "$@"; }
printf 'cpu: 12\nkubernetes:\n  enabled: true\n  version: v1.35.0+k3s1\n  k3sArgs:\n    - --disable=traefik\n\n  port: 0\nnetwork:\n  address: false\n' > "$KR/old.yaml"
# a NEW profile: both args, because a --k3s-arg replaces colima's default list (--disable=traefik)
rm -f "$KR/colima.log"; OUT="$(kr KR_RUNNING=no bash -c '. "$0"; start_colima' "$CLI" 2>&1)"
grep -q -- 'start --kubernetes --cpu .* --k3s-arg=--disable=traefik --k3s-arg=--kubelet-arg=root-dir=/var/lib/rancher/kubelet$' "$KR/colima.log" \
  && ok "a new portal VM starts with kubelet's root on the data disk (and traefik still disabled)" || bad "start_colima k3s args" "$(cat "$KR/colima.log")"
# an EXISTING profile without root-dir is detected, and the warning names the repair
cp "$KR/old.yaml" "$KR/colima/default/colima.yaml"
OUT="$(kr bash -c '. "$0"; kubelet_root_check' "$CLI" 2>&1)"
case "$OUT" in *"ROOT disk"*"memex-local kubelet-root apply"*) ok "an existing profile without root-dir is warned about, naming 'kubelet-root apply'" ;; *) bad "kubelet root warn" "$OUT" ;; esac
for f in cmd_doctor cmd_verify; do fn_body "$f" | grep -q kubelet_root_check && ok "$f reports where kubelet's root is" || bad "$f kubelet check" "missing"; done
sed -n '/^cmd_fleet() {/,/^}/p' "$CLI" | sed -n '/Preflight (MacStudioFleet/,/throw-away key for this join/p' | grep -q kubelet_root_check \
  && ok "fleet join's preflight reports where kubelet's root is" || bad "fleet join preflight kubelet" "missing"
# the repair refuses unattended without --yes, and changes nothing
OUT="$(kr bash -c '. "$0"; cmd_kubelet_root apply' "$CLI" </dev/null 2>&1)"; RC=$?
[ "$RC" != 0 ] && cmp -s "$KR/old.yaml" "$KR/colima/default/colima.yaml" && case "$OUT" in *"--yes"*) true ;; *) false ;; esac \
  && ok "kubelet-root apply without a terminal refuses unless --yes, and edits nothing" || bad "apply needs --yes" "rc=$RC $OUT"
rm -f "$KR/colima.log" "$KR/kubectl.log"
OUT="$(kr bash -c '. "$0"; cmd_kubelet_root apply --yes' "$CLI" </dev/null 2>&1)"; RC=$?
A="$(kr bash -c '. "$0"; colima_k3s_args "$1"' "$CLI" "$KR/colima/default/colima.yaml")"
{ [ "$RC" = 0 ] && [ "$A" = "$(printf -- '--disable=traefik\n--kubelet-arg=root-dir=/var/lib/rancher/kubelet')" ] \
  && compgen -G "$KR/colima/default/colima.yaml.bak-*" >/dev/null && grep -q '^  port: 0$' "$KR/colima/default/colima.yaml" \
  && [ "$(grep -c '' "$KR/colima/default/colima.yaml")" = "$(( $(grep -c '' "$KR/old.yaml") + 1 ))" ] \
  && [ "$(grep -E '^(stop|start)' "$KR/colima.log" | tr '\n' ' ')" = "stop start " ]; } \
  && case "$OUT" in *"portal and everything in it is DOWN"*"81G free of 98G"*) true ;; *) false ;; esac \
  && ok "kubelet-root apply adds the arg (one line, the rest untouched, a backup beside it), says the portal is down, restarts the VM and proves the data disk" \
  || bad "kubelet-root apply" "rc=$RC args=[$A] log=$(tr '\n' ' ' < "$KR/colima.log") $OUT"
grep -q '^ssh -- sudo sh -c test -d /var/lib/rancher/kubelet/pods' "$KR/colima.log" \
  && ok "kubelet-root apply reads the root-owned kubelet dir AS ROOT (an unprivileged test -d read 'permission denied' as absent — moench)" \
  || bad "kubelet-root apply sudo" "$(grep '^ssh' "$KR/colima.log")"
cp "$KR/old.yaml" "$KR/colima/default/colima.yaml"; rm -f "$KR/colima.log"
OUT="$(KR_SSH_EMPTY_FIRST=2 MEMEX_KUBELET_ROOT_WAIT_S=0 kr bash -c '. "$0"; cmd_kubelet_root apply --yes' "$CLI" </dev/null 2>&1)"; RC=$?
[ "$RC" = 0 ] && [ "$(grep -c '^ssh -- sudo' "$KR/colima.log")" = 3 ] \
  && ok "kubelet-root apply waits for kubelet to create pods/ instead of failing on the first read" \
  || bad "kubelet-root apply wait" "rc=$RC $(grep -c '^ssh' "$KR/colima.log") reads $OUT"
cp "$KR/old.yaml" "$KR/colima/default/colima.yaml"; rm -f "$KR/colima.log"
OUT="$(KR_SSH_ERR='sudo: a password is required' MEMEX_KUBELET_ROOT_WAIT_TRIES=2 MEMEX_KUBELET_ROOT_WAIT_S=0 kr bash -c '. "$0"; cmd_kubelet_root apply --yes' "$CLI" </dev/null 2>&1)"; RC=$?
[ "$RC" != 0 ] && case "$OUT" in *"UNKNOWN"*"a password is required"*) true ;; *) false ;; esac && case "$OUT" in *"did not take the root-dir"*) false ;; *) true ;; esac \
  && ok "a read that ERRORS is reported as unknown with its answer, never as 'kubelet did not take the root-dir'" \
  || bad "kubelet-root apply read error" "rc=$RC $OUT"
cp "$KR/old.yaml" "$KR/colima/default/colima.yaml"; rm -f "$KR/colima.log"
OUT="$(MEMEX_KUBELET_ROOT_WAIT_TRIES=0 kr bash -c '. "$0"; cmd_kubelet_root apply --yes' "$CLI" </dev/null 2>&1)"; RC=$?
[ "$RC" = 0 ] && [ "$(grep -c '^ssh -- sudo' "$KR/colima.log")" = 1 ] \
  && ok "MEMEX_KUBELET_ROOT_WAIT_TRIES=0 still reads once (no verdict without a read)" || bad "wait tries 0" "rc=$RC $OUT"
cp "$KR/colima/default/colima.yaml" "$KR/after.yaml"; rm -f "$KR/colima.log"
OUT="$(kr bash -c '. "$0"; cmd_kubelet_root apply --yes' "$CLI" </dev/null 2>&1)"; RC=$?
[ "$RC" = 0 ] && cmp -s "$KR/after.yaml" "$KR/colima/default/colima.yaml" && ! grep -qE '^(stop|start)' "$KR/colima.log" 2>/dev/null \
  && ok "kubelet-root apply is idempotent: a profile that has the arg is left alone and the VM is not restarted" || bad "apply idempotent" "rc=$RC $OUT"
# the VM restarted but kubelet did not take the root-dir → loud, never a green line
cp "$KR/old.yaml" "$KR/colima/default/colima.yaml"
OUT="$(kr KR_SSH= bash -c '. "$0"; cmd_kubelet_root apply --yes' "$CLI" </dev/null 2>&1)"; RC=$?
[ "$RC" != 0 ] && case "$OUT" in *"did not take the root-dir"*) true ;; *) false ;; esac \
  && ok "a restart after which kubelet's root is not on the data disk FAILS, naming it" || bad "apply unverified" "rc=$RC $OUT"
cp "$KR/old.yaml" "$KR/colima/default/colima.yaml"; sed -i.x 's/    - --disable=traefik/    - --kubelet-arg=root-dir=\/var\/lib\/kubelet-elsewhere/' "$KR/colima/default/colima.yaml"; rm -f "$KR/colima/default/colima.yaml.x" "$KR/colima.log"
OUT="$(kr bash -c '. "$0"; cmd_kubelet_root apply --yes' "$CLI" </dev/null 2>&1)"; RC=$?
[ "$RC" != 0 ] && grep -q 'kubelet-elsewhere' "$KR/colima/default/colima.yaml" && ! grep -qE '^stop' "$KR/colima.log" 2>/dev/null \
  && ok "a profile with a DIFFERENT root-dir is not overwritten and the VM is not restarted" || bad "different root-dir" "rc=$RC $OUT"
rm -rf "$KR"

echo "── fleet join waits for this Mac's first heartbeat (2026-11-10) ────────"
HB="$(mktemp -d)"; mkdir -p "$HB/stubs" "$HB/home/.memex-local"
cat > "$HB/stubs/kubectl" <<'KEOF'
#!/bin/bash
r="$(cat "$HB_DIR/restarts" 2>/dev/null || echo 0)"
case "$*" in
  *"rollout restart"*) echo $(( r + 1 )) > "$HB_DIR/restarts" ;;
  *" logs "*) f="$HB_DIR/log.$r"; [ -f "$f" ] || f="$HB_DIR/log.last"; cat "$f" 2>/dev/null ;;
esac
exit 0
KEOF
chmod +x "$HB/stubs/kubectl"
REC='info: MeshWeaver.PluginCatalog.InstanceAutoRegistrationService[0]
      [DefaultInstall] reconciled: 3 installed, 9 up to date, 0 failed [DefaultViews, GraphViews, Plugins/Hosting] — MODULES LANDED ahead of content: [MeshWeaver.Hosting] — MODULE SET proposed: sequence 4 — the landed modules load at the next restart'
POSTED='dbug: Hosting[0]
      [BackLease] heartbeat armed on mesh/x: every 00:00:30 to https://control.example.test/api/hooks/Hosting/Heartbeats, readings from /host-readings/readings.json
dbug: Hosting[0]
      [BackLease] heartbeat posted to https://control.example.test/api/hooks/Hosting/Heartbeats: HTTP 200'
hbw() { rm -f "$HB/restarts" "$HB"/log.*; printf '%s\n' "$1" > "$HB/log.0"; printf '%s\n' "$2" > "$HB/log.1"; printf '%s\n' "$2" > "$HB/log.last"; shift 2
  OUT="$(env PATH="$HB/stubs:$PATH" HOME="$HB/home" MEMEX_LOCAL_HOME="$HB/home/.memex-local" HB_DIR="$HB" MEMEX_HEARTBEAT_POLL_SEC=0 MEMEX_LOCAL_LIB=1 "$@" \
    bash -c '. "$0"; fleet_wait_heartbeat "${HBW_MIN:-1}"' "$CLI" 2>&1)"; RC=$?; RESTARTS="$(cat "$HB/restarts" 2>/dev/null || echo 0)"; }
hbw "$REC" "$REC
$POSTED"
[ "$RC" = 0 ] && [ "$RESTARTS" = 1 ] && case "$OUT" in *"restarting the portal once"*"heartbeat posted to https://control.example.test/api/hooks/Hosting/Heartbeats HTTP 200"*) true ;; *) false ;; esac \
  && ok "Hosting landed for the next restart → ONE restart → ends ok naming the heartbeat's POST and its HTTP 200" || bad "restart then heartbeat" "rc=$RC restarts=$RESTARTS $OUT"
hbw "$REC" "$(printf '%s\n' "$REC" | sed 's/sequence 4/sequence 5/')" HBW_MIN=0
[ "$RC" != 0 ] && [ "$RESTARTS" = 1 ] && case "$OUT" in *"not restarting a second time"*"no heartbeat after 0 min"*"memex-local fleet heartbeat"*) true ;; *) false ;; esac \
  && ok "the portal is restarted at most ONCE; a wait that runs out FAILS naming the last state and the way to resume" || bad "single restart + timeout" "rc=$RC restarts=$RESTARTS $OUT"
TIER='warn: MeshWeaver.PluginCatalog.InstanceAutoRegistrationService[0]
      [DefaultInstall] 1 package(s) the registry declares in this instance'"'"'s default set are REFUSED by its plan tier and are not installed: ⛔ Not installed on this instance: Hosting needs plan tier enterprise, this instance is on free.. Raise the instance'"'"'s plan on the registry to receive them; nothing on this instance can.
      [DefaultInstall] reconciled: 0 installed, 9 up to date, 0 failed [DefaultViews, GraphViews]'
hbw "$TIER" "$TIER"
[ "$RC" != 0 ] && [ "$RESTARTS" = 0 ] && case "$OUT" in *"REFUSES Hosting"*"plan tier enterprise"*"is on free"*"Remedy:"*) true ;; *) false ;; esac \
  && ok "a plan tier that refuses Hosting STOPS the wait at once, naming the tier, the plan and the remedy — no restart" || bad "tier refusal" "rc=$RC restarts=$RESTARTS $OUT"
hbw "" "" HBW_MIN=0
[ "$RC" != 0 ] && case "$OUT" in *"last state: reconciling"*) true ;; *) false ;; esac && ok "no reconcile at all: the bounded wait ends red, never hangs" || bad "no reconcile" "rc=$RC $OUT"
hbw "$(printf '%s\n' "$REC" | sed 's/ — MODULE SET.*//')
$POSTED" "" 
[ "$RC" = 0 ] && [ "$RESTARTS" = 0 ] && ok "Hosting already loaded (no restart pending): no restart, ends at the heartbeat" || bad "no restart needed" "rc=$RC restarts=$RESTARTS $OUT"
hbw "$(printf '%s\n' "$POSTED" | sed '$d' | sed '$d')" "" MEMEX_LOG_LEVEL=Information
[ "$RC" = 0 ] && case "$OUT" in *"ARMED"*"Debug line"*"level Information"*) true ;; *) false ;; esac \
  && ok "at a log level that hides the Debug 'posted' line, an armed heartbeat ends it and says to confirm on control" || bad "armed at Information" "rc=$RC $OUT"
hs() { printf '%s\n' "$1" | env MEMEX_LOCAL_LIB=1 bash -c '. "$0"; heartbeat_state' "$CLI"; }
[ "$(hs '[DefaultInstall] reconciled: 1 installed, 0 up to date, 0 failed [HostingExtras] — MODULE SET proposed: sequence 2 — the landed modules load at the next restart' | cut -d'|' -f1)" = absent ] \
  && ok "a package that merely starts with 'Hosting' is not Hosting" || bad "Hosting id match" "$(hs '[DefaultInstall] reconciled: 1 installed, 0 up to date, 0 failed [HostingExtras]')"
[ "$(hs '[BackLease] heartbeat posted to https://c.example.test/x: HTTP 401' | cut -d'|' -f1)" != posted ] && ok "a POST answered 401 is not a heartbeat that reached control" || bad "401 counted" ""
[ "$(hs '[BackLease] heartbeat NOT accepted by https://c.example.test/x: HTTP 401 unknown deployment' )" = "rejected|https://c.example.test/x: HTTP 401 unknown deployment" ] \
  && ok "a heartbeat control did not accept is reported with control's answer" || bad "rejected detail" "$(hs '[BackLease] heartbeat NOT accepted by https://c.example.test/x: HTTP 401 unknown deployment')"
sed -n '/^cmd_fleet() {/,/^}/p' "$CLI" | sed -n '/^    join)/,/^      ;;/p' | grep -q 'fleet_wait_heartbeat "\$hb_min"' \
  && ok "fleet join's tail waits for the heartbeat" || bad "join tail wiring" "missing"
OUT="$(env PATH="$HB/stubs:$PATH" HOME="$HB/home" MEMEX_LOCAL_HOME="$HB/home/.memex-local" "$CLI" fleet heartbeat --wait x 2>&1)" && bad "--wait x accepted" "$OUT" || ok "fleet heartbeat refuses a --wait that is not whole minutes"
rm -rf "$HB"

echo "── the tap tarball carries every directory the formula installs (2026-11-10) ──────"
TB="$(mktemp -d)"
if out="$(bash "$HERE/../../../deploy/homebrew/scripts/make-tarball.sh" 0.0.0-test "$TB/t.tar.gz" HEAD 2>&1)"; then
  E="$(tar -tzf "$TB/t.tar.gz")"
  grep -qxF "memex-local-0.0.0-test/deploy/backlease/first-attach.sh" <<<"$E" \
    && ok "the tarball ships deploy/backlease/first-attach.sh (fleet join's build-host attach skipped without it on the second Mac Studio)" \
    || bad "tarball backlease" "$(grep -c '' <<<"$E") entries, no first-attach.sh"
  miss=""
  for d in $(grep -oE 'Dir\["deploy/[A-Za-z0-9_.-]+' "$HERE/../../../deploy/homebrew/Formula/memex-local.rb" | sed 's/^Dir\["//' | sort -u); do
    grep -qE "^memex-local-0.0.0-test/$d/." <<<"$E" || miss="$miss $d"
  done
  [ -z "$miss" ] && ok "every Dir[] the formula installs from is in the tarball" || bad "tarball dirs" "missing:$miss"
else
  bad "make-tarball" "$out"
fi
rm -rf "$TB"

echo
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
