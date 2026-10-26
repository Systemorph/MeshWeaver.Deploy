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
VERIFY_FLAGS=""

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
done

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
for c in cmd_up cmd_update; do
  body="$(awk -v f="^${c}\\\\(\\\\)" '$0 ~ f { on=1 } on { print } on && /^}$/ { exit }' "$CLI")"
  case "$body" in
    *'registry_mode && image_source="acr"'*) ok "${c} defaults to the ACR image in registry mode" ;;
    *) bad "${c} defaults to the ACR image in registry mode" "no registry-mode default in ${c}()" ;;
  esac
done

echo "── Mac host profiles (Doc/Architecture/ReleaseEditions G4) ───────"

prof() { OUT="$(env "$@" MEMEX_LOCAL_HOME="$RHOME" "$CLI" profile 2>&1)"; RC=$?; }
prof MEMEX_PROFILE=mac-studio
case "$RC:$OUT" in 0:*"12 CPU / 32 GiB"*"qwen3.6"*) ok "mac-studio sizes a 12/32 VM with qwen3.6" ;;
  *) bad "mac-studio sizes a 12/32 VM with qwen3.6" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE=mac-mini
case "$RC:$OUT" in 0:*"6 CPU / 12 GiB"*"qwen3:8b"*) ok "mac-mini sizes a 6/12 VM with qwen3:8b" ;;
  *) bad "mac-mini sizes a 6/12 VM with qwen3:8b" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE=
case "$RC:$OUT" in 0:*"8 CPU / 16 GiB"*"qwen3.6"*) ok "no profile keeps the laptop defaults (8/16, qwen3.6)" ;;
  *) bad "no profile keeps the laptop defaults (8/16, qwen3.6)" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE=mac-mini MEMEX_COLIMA_MEM=20 MEMEX_OLLAMA_MODEL=none
case "$RC:$OUT" in 0:*"6 CPU / 20 GiB"*"model:     none"*) ok "an explicit MEMEX_COLIMA_MEM / MEMEX_OLLAMA_MODEL beats the profile" ;;
  *) bad "an explicit MEMEX_COLIMA_MEM / MEMEX_OLLAMA_MODEL beats the profile" "exited $RC: $OUT" ;; esac
prof MEMEX_PROFILE=mac-studo
case "$RC:$OUT" in 0:*) bad "an unknown profile refuses (never read as the default)" "exited 0: $OUT" ;;
  *"unknown MEMEX_PROFILE"*) ok "an unknown profile refuses (never read as the default)" ;;
  *) bad "an unknown profile refuses (never read as the default)" "refused without saying why: $OUT" ;; esac

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
resolve() { OUT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tool_resolve "$2"' _ "$HERE/../share" "$1" 2>&1)"; RC=$?; }
texisting_all
for t in colima lima kubectl helm mkcert ollama socket_vmnet; do resolve "$t"; case "$RC:$OUT" in 0:"$t	existing	$TEX/"*"	ok	") ;; *) bad "all present and satisfying → $t resolves to the EXISTING copy" "rc=$RC: $OUT"; continue ;; esac; done
ok "all present and satisfying → every tool resolves to the existing copy (nothing vendored chosen)"
resolve kubectl; case "$OUT" in *"	1.37.0	1.31.0	ok	") ok "the existing kubectl reports 1.37.0 against the floor 1.31.0" ;; *) bad "kubectl version + floor" "$OUT" ;; esac
# tools_activate → "<selection dir> | <PATH>" ; the selection dir holds one link per resolved tool.
activate() { ACT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; printf "%s|%s" "$MEMEX_TOOLS_SELECTED" "$PATH"' _ "$HERE/../share")"; SEL="${ACT%%|*}"; APATH="${ACT#*|}"; }
activate
[ "$APATH" = "$SEL:$TEX:/usr/bin:/bin" ] && ok "tools_activate prepends ONE selection directory to PATH" || bad "tools_activate prepends the selection dir" "$APATH"
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
ACT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; tools_activate; readlink "$MEMEX_TOOLS_SELECTED/colima"; "$MEMEX_TOOLS_SELECTED/colima" version; rm -rf "$MEMEX_TOOLS_SELECTED"' _ "$HERE/../share" 2>&1)"
case "$ACT" in "$TEX/colima"*"colima version 0.10.3"*) ok "a second tools_activate keeps the links pointing at the real executables (no self-reference)" ;; *) bad "second activation self-links" "$ACT" ;; esac
# A CHILD inheriting the parent's selection on PATH gets its OWN directory and never removes the parent's.
ACT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; parent="$MEMEX_TOOLS_SELECTED"; bash -c ". \"$1/tools.sh\"; tools_activate; echo child=\$MEMEX_TOOLS_SELECTED; readlink \"\$MEMEX_TOOLS_SELECTED/helm\"; tools_deactivate" _ "$1"; echo "parent=$parent"; [ -d "$parent" ] && echo parent-kept; rm -rf "$parent"' _ "$HERE/../share" 2>&1)"
child="$(sed -n 's/^child=//p' <<<"$ACT")"; parent="$(sed -n 's/^parent=//p' <<<"$ACT")"
if [ -n "$child" ] && [ "$child" != "$parent" ] && grep -q '/helm$' <<<"$ACT" && grep -q '^parent-kept$' <<<"$ACT"; then
  ok "a child process activating under an inherited selection owns its own dir, links to the real executable, and leaves the parent's dir alone"
else bad "child selection ownership" "$ACT"; fi
# The selection must be FIRST even when PATH already carries it later (an agent prepended Homebrew's dirs).
ACT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; sel="$MEMEX_TOOLS_SELECTED"; export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; tools_activate; printf "%s|%s" "$sel" "$PATH"; rm -rf "$sel"' _ "$HERE/../share")"
sel="${ACT%%|*}"; apath="${ACT#*|}"
case "$apath" in "$sel:/opt/homebrew/bin:/usr/local/bin:$TEX:/usr/bin:/bin") ok "re-activation moves the selection dir back to the FRONT of PATH (once, not twice)" ;; *) bad "selection first on re-activation" "$apath" ;; esac
# A stale fallback link in <prefix>/bin AHEAD on PATH must not win: the selection dir precedes everything.
mkdir -p "$TPRE/bin"; ln -sfn "$TPRE/libexec/vendor/colima/bin/colima" "$TPRE/bin/colima"
ACT="$(env PATH="$TPRE/bin:$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tools_activate; command -v colima; rm -rf "$MEMEX_TOOLS_SELECTED"' _ "$HERE/../share")"
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
OUT="$(env PATH="$TEX:$TEX2:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" bash -c '. "$1/tools.sh"; tool_resolve helm' _ "$HERE/../share" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:"helm	existing	$TEX2/helm	4.2.4	3.16.0	ok	skipped: existing $TEX/helm is 3.2.0, below the floor 3.16.0") ok "helm 3.2.0 first and 4.2.4 second on PATH → the satisfying second copy is chosen, the skipped one named" ;; *) bad "scan past a below-floor first copy" "rc=$RC: $OUT" ;; esac
rm -rf "$TEX2"; texisting_all
# A re-run resolves identically (pure: no state is written by resolving).
resolve helm; first="$OUT"; resolve helm; [ "$OUT" = "$first" ] && ok "resolving twice gives the same answer (the resolver writes nothing)" || bad "resolver pure" "$first vs $OUT"
# `need` through the CLI's own definition: a tool below its floor is refused BY NAME with floor + remedy.
texisting_all; tstub "$TEX" helm "v3.2.0"; rm -rf "$TPRE/libexec/vendor/helm"
OUT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_LIB=1 bash -c '. "$1"; need helm helm' _ "$CLI" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "need refuses a tool below its floor" "exited 0: $OUT" ;;
  *"required tool 'helm' is below-floor"*"floor 3.16.0"*"brew install helm"*"Memex installer, which carries 4.3.1"*) ok "need refuses helm 3.2.0 by name, with the floor, the brew remedy and the installer's pinned version" ;;
  *) bad "need's refusal names floor + remedies" "rc=$RC: $OUT" ;; esac
OUT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_LIB=1 bash -c '. "$1"; need colima colima && echo accepted' _ "$CLI" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:accepted) ok "need accepts an existing tool at/above its floor" ;; *) bad "need accepts a satisfying tool" "rc=$RC: $OUT" ;; esac
# A name no runner has (an Ubuntu runner HAS /usr/bin/docker, which made this pass vacuously once).
OUT="$(env PATH="$TEX:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_LIB=1 bash -c '. "$1"; need frobnicator-xyz frobnicator' _ "$CLI" 2>&1)"; RC=$?
case "$RC:$OUT" in 0:*) bad "need still refuses a non-manifest tool that is absent" "exited 0" ;; *"required tool 'frobnicator-xyz' not found"*"brew install frobnicator"*) ok "need keeps the presence-only check for a non-manifest tool" ;; *) bad "need non-manifest" "$OUT" ;; esac

# `doctor` prints the table: per tool, which copy, from where, which version, whether it met the floor.
texisting_all; rm -f "$TEX/helm"; tvend helm helm "v4.3.1+gabc"; tstub "$TEX" kubectl "Client Version: v1.20.0"
# doctor also probes the cluster: the fake colima exits 0 and kubectl answers anything, so it runs through.
OUT="$(env PATH="$TEX:$STUBS:/usr/bin:/bin" MEMEX_PREFIX="$TPRE" MEMEX_LOCAL_HOME="$TT/home" "$CLI" doctor 2>&1)"; RC=$?
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
rm -rf "$MP"

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
rm -rf "$HL"

echo "── the CLI still knows every command it documents ────────────────"

help_out="$("$CLI" help 2>&1)"
for cmd in up down status setup logs update registry verify port-forward observability doctor profile channel backlease relay; do
  case "$help_out" in
    *"$cmd"*) ;;
    *) bad "help documents '$cmd'" "not in the usage text"; continue ;;
  esac
  if grep -q "^    ${cmd})" "$CLI"; then ok "help documents '$cmd', and main dispatches it"
  else bad "help documents '$cmd'" "no dispatch arm in main()"; fi
done

echo
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
