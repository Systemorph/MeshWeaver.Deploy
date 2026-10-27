#!/usr/bin/env bash
# host-postgres.sh — the standing build host's PREPARED test database server (MeshWeaver.Plugins
# Hosting/StandingBuildHosts.md, "a prepared server, never a prepared run").
#
#   host-postgres.sh start        # once per pod, by the entrypoint, BEFORE the claim loop
#   host-postgres.sh --self-test  # a REAL server in a temp dir: template, per-run role, clone, isolation
#
# What a test needs from PostgreSQL is a database with pgvector in it. A run never builds that from
# scratch: the host starts ONE server when the pod starts and prepares ONE template database
# (`mw_test_template`, pgvector installed, IS_TEMPLATE, ALLOW_CONNECTIONS false). Every database a
# suite opens is `CREATE DATABASE … TEMPLATE mw_test_template` — a file-level clone, milliseconds —
# through MeshWeaver.Plugins src/Testing.Shared/PostgresTestServer.cs (MW_TEST_PG_CONNECTION,
# MW_TEST_PG_TEMPLATE). The runner (build-run.py, TestPostgres) gives every RUN its own LOGIN CREATEDB
# role with a random password and, after the run, drops every database that role owns and the role.
#
# THE BOUNDARY, and why each piece is what it is:
#   * no TCP at all (`listen_addresses=''`): the socket lives in $MW_TEST_PG_SOCKET (default
#     /opt/build-host-pg, 0700, owned by the host's user), which the stage sandbox sees READ-ONLY —
#     connecting to a Unix socket needs no write access to the directory, creating one does;
#   * the data directory and the admin password live under $MW_TEST_PG_ROOT (default /work/.mw-test-pg),
#     which no sandbox binds: a stage cannot read the admin credential, only its own run's;
#   * the run's role is neither superuser nor CREATEROLE and cannot connect to the template, so it can
#     neither change the template the next run clones nor touch another role;
#   * the data directory is re-created on every pod start: nothing a run left behind survives a restart.
# Fails CLOSED: no server binaries, no pgvector, an initdb, a start or the template that fails is exit 1
# naming it, and the host never claims (the entrypoint is `set -e`).
set -euo pipefail

TEMPLATE="${MW_TEST_PG_TEMPLATE_NAME:-mw_test_template}"
ADMIN=mwadmin

pg_bin() {
  local bin="${PG_BIN_DIR:-}"
  if [ -z "$bin" ] && [ -d /usr/lib/postgresql ]; then
    bin="$(find /usr/lib/postgresql -mindepth 2 -maxdepth 2 -type d -name bin | sort -V | tail -n 1)"
  fi
  [ -n "$bin" ] && [ -x "$bin/initdb" ] && [ -x "$bin/pg_ctl" ] && [ -x "$bin/psql" ] \
    || { echo "host-postgres: no PostgreSQL server binaries (${bin:-/usr/lib/postgresql/*/bin})" >&2; return 1; }
  printf '%s' "$bin"
}

# start <root> <socket dir> — initdb fresh, start, prepare the template. Prints nothing on stdout.
start() {
  local root="$1" sock="$2" bin pw
  bin="$(pg_bin)"
  rm -rf "$root"
  mkdir -p "$root" "$sock"
  chmod 0700 "$root" "$sock"
  pw="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  ( umask 077; printf '%s' "$pw" > "$root/admin.pw" )
  "$bin/initdb" -D "$root/data" -U "$ADMIN" -A scram-sha-256 --pwfile="$root/admin.pw" >"$root/initdb.log" 2>&1 \
    || { echo "host-postgres: initdb failed:" >&2; tail -n 20 "$root/initdb.log" >&2; return 1; }
  # Tests open many short databases and LISTEN sessions in parallel lanes: a roomy connection limit;
  # durability is irrelevant for throwaway clones (the data directory dies with the pod).
  "$bin/pg_ctl" -D "$root/data" -l "$root/server.log" -w -t 120 \
    -o "-c listen_addresses='' -k $sock -c unix_socket_permissions=0700 -c max_connections=500 -c fsync=off -c synchronous_commit=off -c full_page_writes=off" start >/dev/null \
    || { echo "host-postgres: the server did not start:" >&2; tail -n 20 "$root/server.log" >&2; return 1; }
  local q=("$bin/psql" -X -q -v ON_ERROR_STOP=1 -h "$sock" -U "$ADMIN" -At)
  PGPASSWORD="$pw" "${q[@]}" -d postgres -c "CREATE DATABASE $TEMPLATE" \
    && PGPASSWORD="$pw" "${q[@]}" -d "$TEMPLATE" -c "CREATE EXTENSION vector" \
    && PGPASSWORD="$pw" "${q[@]}" -d postgres -c "REVOKE ALL ON DATABASE $TEMPLATE FROM PUBLIC" \
    && PGPASSWORD="$pw" "${q[@]}" -d postgres -c "ALTER DATABASE $TEMPLATE IS_TEMPLATE true ALLOW_CONNECTIONS false" \
    || { echo "host-postgres: the template $TEMPLATE could not be prepared (is pgvector installed?)" >&2; return 1; }
  local ext
  ext="$(PGPASSWORD="$pw" "${q[@]}" -d postgres -c "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")"
  printf 'template=%s\nvector=%s\nsocket=%s\nserver=%s\n' "$TEMPLATE" "$ext" "$sock" "$("$bin/postgres" --version)" > "$root/ready"
  echo "host-postgres: prepared server up on the Unix socket in $sock (no TCP), template $TEMPLATE with pgvector $ext" >&2
}

if [ "${1:-}" = "--self-test" ]; then
  # A REAL server, as the runner uses it: start + template, then a per-run role that clones the
  # template, finds pgvector in its clone, and is REFUSED the template itself and role creation.
  st="$(mktemp -d)"
  sock="$(mktemp -d)"
  bin="$(pg_bin)"
  cleanup() { "$bin/pg_ctl" -D "$st/root/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$st" "$sock"; }
  trap cleanup EXIT
  fail() { echo "SELF-TEST FAILED: $1"; exit 1; }
  start "$st/root" "$sock" 2>/dev/null || fail "the prepared server did not start"
  [ -f "$st/root/ready" ] || fail "no ready marker"
  grep -q '^vector=[0-9]' "$st/root/ready" || fail "pgvector is not available to the server"
  pw="$(cat "$st/root/admin.pw")"
  admin=("$bin/psql" -X -q -v ON_ERROR_STOP=1 -h "$sock" -U "$ADMIN" -At -d postgres)
  PGPASSWORD="$pw" "${admin[@]}" -c "CREATE ROLE run_selftest LOGIN CREATEDB PASSWORD 'p4ss'" || fail "a run role could not be created"
  run=("$bin/psql" -X -q -v ON_ERROR_STOP=1 -h "$sock" -U run_selftest -At)
  PGPASSWORD=p4ss "${run[@]}" -d postgres -c "CREATE DATABASE suite_a TEMPLATE $TEMPLATE" || fail "the run role cannot clone the template"
  [ "$(PGPASSWORD=p4ss "${run[@]}" -d suite_a -c "SELECT count(*) FROM pg_extension WHERE extname = 'vector'")" = 1 ] \
    || fail "the clone carries no pgvector"
  PGPASSWORD=p4ss "${run[@]}" -d suite_a -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE TABLE t (v vector(3)); INSERT INTO t VALUES ('[1,2,3]')" \
    || fail "a suite's own DDL does not run on its clone"
  # Negative controls — each MUST be refused.
  if PGPASSWORD=p4ss "${run[@]}" -d "$TEMPLATE" -c "SELECT 1" >/dev/null 2>&1; then fail "the run role connected to the template"; fi
  if PGPASSWORD=p4ss "${run[@]}" -d postgres -c "CREATE ROLE intruder" >/dev/null 2>&1; then fail "the run role created a role"; fi
  if PGPASSWORD=p4ss "${run[@]}" -d postgres -c "ALTER DATABASE $TEMPLATE IS_TEMPLATE false" >/dev/null 2>&1; then fail "the run role altered the template"; fi
  if PGPASSWORD=wrong "${run[@]}" -d postgres -c "SELECT 1" >/dev/null 2>&1; then fail "a wrong password was accepted"; fi
  if "$bin/psql" -X -h 127.0.0.1 -U run_selftest -d postgres -c "SELECT 1" >/dev/null 2>&1; then fail "the server answered on TCP"; fi
  # The runner's teardown shape: every database the role owns, then the role.
  PGPASSWORD="$pw" "${admin[@]}" -c "DROP DATABASE suite_a WITH (FORCE)" -c "DROP ROLE run_selftest" || fail "the run's teardown failed"
  [ "$(PGPASSWORD="$pw" "${admin[@]}" -c "SELECT count(*) FROM pg_roles WHERE rolname = 'run_selftest'")" = 0 ] || fail "the run role survived its teardown"
  echo "host-postgres self-test: passed (prepared template with pgvector; a run role clones it and runs DDL on its clone; the template, role creation, template changes, a wrong password and TCP are refused; teardown leaves nothing)"
  exit 0
fi

case "${1:-}" in
  start) start "${MW_TEST_PG_ROOT:-/work/.mw-test-pg}" "${MW_TEST_PG_SOCKET:-/opt/build-host-pg}" ;;
  *) echo "usage: host-postgres.sh start | --self-test" >&2; exit 2 ;;
esac
