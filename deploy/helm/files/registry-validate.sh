#!/bin/sh
# registry-validate — docker_auth's `ext_auth` hook for the fleet registry (cr.meshweaver.cloud).
#
# Contract (cesanta/docker_auth 1.14.0, auth_server/authn/ext_auth.go): stdin is "<user> <password>"
# with NO trailing newline (so `read` returns non-zero at EOF while still assigning — hence no
# `set -e` and no test of read's status); exit 0 = authenticated, 1 = denied, 2 = no match (try the
# next authenticator), 3 = error. On exit 0, stdout may carry a JSON object whose `labels` key is
# a map of label name → list of values; the ACL then matches `${labels:<name>}` placeholders
# against every value (auth_server/authz/acl.go).
#
# The PASSWORD is a MeshWeaver instance key. It is presented ONCE, as a bearer, to the portal's
# key→token exchange (`POST /api/instances/token`, the SyncTokenPayloads contract) with an empty
# body: a 200 authenticates and its `scope` — the caller's CURRENT licence entries, as the grant
# holds them (`Plugins/*`, `Reinsurance/UWDeepfield`, `Plugins/*@pro`) — becomes the labels the
# ACL authorizes plugin repositories with. 🚨 This is the only place the scope can be learned:
# docker_auth's `ext_authz` hook receives the marshalled request (account, type, name, actions,
# labels) and NOT the credential, so it cannot ask memex anything; and a label may never carry
# the key itself, because docker_auth logs every token's labels at Info (server.go, "New token
# for … %+v"). A scope is not a secret.
#
# Labels emitted, both in the registry's lowercase name grammar:
#   package  one per licence entry — `<source>/<package>`, or `<source>/*` for a whole-source
#            entry (plan-scoped or not): the ACL grants `pull` on `plugins/${labels:package}`,
#            i.e. on the bundle repositories `plugins/<source>/<package>`.
#   source   one per PLAN-LESS whole-source entry — `<source>`: the ACL grants `pull` on
#            `plugins/${labels:source}`, the publication INDEX. A plan-scoped `Source/*@plan`
#            licenses that source's packages by tier and never the publication whole (which
#            carries every plan's bundles), so it contributes no `source` label — same rule as
#            PluginGrant.AllowsWholeSource.
# Which package a plan-scoped entry covers is decided where the package's tier is known — the
# catalog and the landing — not here; the edge grants the source's bundle repositories and
# nothing above them.
#
# 401/403 from the exchange denies. Anything that is not a definite 200 or a definite 401/403 is
# an ERROR — never a pass, and never a quiet deny that would read as "wrong key" to the caller.
#
# 🚨 Which error is NOT one bucket (MeshWeaver#7484). docker_auth answers every error exit with
# the same HTTP 500, so a ten-second stall, a retryable 503 and a crashing exchange used to reach
# the caller as one bare 500 with nothing to tell them apart. docker_auth DOES pass the exit code
# through — the 500's body reads `Authentication failed (bad return code from command: N)` — and
# logs this hook's stderr at Error (`Ext command error: N <stderr>`), so each indefinite outcome
# gets its own code and its own line:
#
#   exit  outcome      meaning                                               retry?
#   3     error        this hook cannot work: no URL, a 200 without a scope, an unexpected
#                      status (3xx/4xx other than 401/403), an unclassified wget failure
#   4     no-answer    the exchange did not answer within EXCHANGE_ANSWER_LIMIT   yes, then look at the exchange
#   5     unreachable  DNS or the connection failed — the request never arrived   yes, then look at the network
#   6     unavailable  the exchange answered 503: it could not reach a verdict  yes — it says so (Retry-After)
#   7     server-error the exchange answered another 5xx (502 = it faulted)       look at the exchange's log
#
# docker_auth's ext_auth contract (auth_server/authn/ext_auth.go, examples/reference.yml):
# "0 - allow, 1 - deny, 2 - no match, other - error" — every code above 2 is an error, so 4–7
# are inside the contract and docker_auth treats them exactly as it treats 3.
#
# The limit is DERIVED from the exchange's own budgets, never chosen: the hook must outwait the
# exchange's slowest answer, or the exchange's correct 503 arrives after the hook has hung up and
# is reported as a stall (MeshWeaver#7484 — the hook waited ten seconds, exactly one budget). The
# exchange (`POST /api/instances/token`, MeshWeaver.Plugins InstanceTokenEndpoints) runs, in
# sequence, each bounded by its own documented budget (core, src/MeshWeaver.PluginCatalog):
#   10 s  the key resolution — index, instance and grant reads under ONE deadline
#         (InstanceRegistryAuthenticator.ReadBudget, Doc/Architecture/InstanceKeyResolution,
#         "One budget for the whole resolution"); a non-verdict is a 503 + Retry-After by then;
#   15 s  the plan ladder, only for a resolved key (PlanTierLadder.Read, bounded by
#         MeshOperationOptions.QueryInitialBudget; 1-minute snapshot, so usually instant);
#   10 s  the token-signing key, only for a resolved key (SyncTokenSigningKeyService.ReadTimeout;
#         1-minute cache, so usually instant);
# plus 5 s for the answer to cross the portal's ingress. 10 + 15 + 10 + 5 = 40. A normal answer
# takes ~0.2 s; the limit is reached only when the exchange itself is stuck. The registry's own
# ingress gives /auth 3600 s (templates/registry/ingress.yaml), so the caller is not cut first.
# `timeout` bounds the WHOLE request (busybox wget -T is a per-read idle limit only).
#
# Busybox only (Alpine): wget -S prints the status line to stderr, the body goes to a file. On an
# error status busybox prints only the status line — no Retry-After header, no body.
#
#   MEMEX_VALIDATION_URL   the exchange URL (registry.validationUrl)   required
EXCHANGE_ANSWER_LIMIT=40
read -r user password
if [ -z "$password" ]; then
  exit 1
fi
url="${MEMEX_VALIDATION_URL:-}"
if [ -z "$url" ]; then
  echo "registry-validate: MEMEX_VALIDATION_URL is not set" >&2
  exit 3
fi
body=$(mktemp) || exit 3
wlog=$(mktemp) || exit 3
trap 'rm -f "$body" "$wlog"' EXIT
# An empty JSON body is a valid request (~0.2 s) — NOT the plugin catalog, which renders in full:
# measured 14–18 s for a valid key.
# wget's stderr carries the RESPONSE status line and its own error line — never the request, so
# never the key; it is safe to quote from below.
timeout "$EXCHANGE_ANSWER_LIMIT" wget -T "$EXCHANGE_ANSWER_LIMIT" -qS -O "$body" --post-data='{}' \
  --header='Content-Type: application/json' --header="Authorization: Bearer $password" "$url" 2>"$wlog"
rc=$?
status=$(sed -n 's/^ *HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' "$wlog" | tail -n 1)
said=$(grep '^wget: ' "$wlog" | tail -n 1)
fail() {
  echo "registry-validate: $1 — $2 from $url for account '$user'${said:+ ($said)}" >&2
  exit "$3"
}
case "$status" in
  200) ;;
  401|403) exit 1 ;;
  503) fail unavailable "the exchange answered 503: it reached no verdict on the key and asks for a retry" 6 ;;
  5[0-9][0-9]) fail server-error "the exchange answered $status" 7 ;;
  ?*) fail error "the exchange answered $status, which is neither a grant nor a refusal" 3 ;;
  *)
    # No status line at all: the request got no answer.
    case "$rc:$said" in
      143:*|124:*|*'timed out'*)
        fail no-answer "no answer within ${EXCHANGE_ANSWER_LIMIT}s (the exchange's own budgets sum to less)" 4 ;;
      *"bad address"*|*"can't connect"*|*"resolve"*|*"Connection refused"*|*"Network is unreachable"*)
        fail unreachable "the exchange could not be reached" 5 ;;
      *) fail error "wget exited $rc with no status line" 3 ;;
    esac ;;
esac

# The exchange answers System.Text.Json Web defaults: compact, camelCase, `"scope":["…","…"]`.
# A scope entry is `Source/Package[@plan]`; neither half can carry `"`, `,` or `]`.
if ! grep -q '"scope":\[' "$body"; then
  echo "registry-validate: the exchange answered 200 without a scope array for account '$user'" >&2
  exit 3
fi
scope=$(sed -n 's/.*"scope":\[\([^]]*\)\].*/\1/p' "$body" | tr ',' '\n' | tr -d '" ')
packages=""
sources=""
for entry in $scope; do
  plan=""
  case "$entry" in
    *@*) plan="${entry##*@}"; entry="${entry%@*}" ;;
  esac
  case "$entry" in
    */*) source="${entry%%/*}"; package="${entry#*/}" ;;
    *) source="$entry"; package="*" ;;
  esac
  source=$(printf '%s' "$source" | tr '[:upper:]' '[:lower:]')
  package=$(printf '%s' "$package" | tr '[:upper:]' '[:lower:]')
  # Only what the registry's name grammar can spell becomes a label; anything else is dropped,
  # which denies rather than matching something unintended.
  case "$source" in *[!a-z0-9._-]*|'') continue ;; esac
  case "$package" in '*') ;; *[!a-z0-9._-]*|'') continue ;; esac
  packages="$packages\"$source/$package\","
  if [ "$package" = '*' ] && [ -z "$plan" ]; then
    sources="$sources\"$source\","
  fi
done
labels=""
if [ -n "$packages" ]; then
  labels="\"package\":[${packages%,}]"
fi
if [ -n "$sources" ]; then
  labels="${labels:+$labels,}\"source\":[${sources%,}]"
fi
printf '{"labels":{%s}}\n' "$labels"
exit 0
