#!/usr/bin/env bash
# host-postgres.sh — the standing build host's PREPARED test database (MeshWeaver.Plugins
# Hosting/StandingBuildHosts.md, "a prepared server, never a prepared run").
#
#   host-postgres.sh prepare <snapshot>      # IMAGE BUILD, once: the prepared cluster (as uid 1654)
#   eval "$(host-postgres.sh start <snapshot> <dir>)"   # in a STAGE, inside its sandbox
#   host-postgres.sh --self-test             # a REAL prepare + two starts, with negative controls
#
# What a test needs from PostgreSQL is a database with pgvector in it. Nothing builds that per run:
# the image carries a PREPARED CLUSTER — initdb'd UTF8 (as pgvector/pgvector:pg17 initialises; the C
# locale would pick SQL_ASCII, where jsonb refuses non-ASCII \u escapes), with ONE template database
# `mw_test_template` (pgvector installed, IS_TEMPLATE). A stage that needs a server starts it from a
# copy of that snapshot on its own tmpfs — a start, never an initdb, never a container pull — and every
# database a suite opens is `CREATE DATABASE … TEMPLATE mw_test_template` (MeshWeaver.Plugins
# src/Testing.Shared/PostgresTestServer.cs reads MW_TEST_PG_CONNECTION and MW_TEST_PG_TEMPLATE).
#
# WHY INSIDE THE SANDBOX, AND WHY THE SUITES' ROLE IS A SUPERUSER. The suites need a superuser exactly
# as the CI runners' shared server gives them one: `SELECT … FROM pg_database FOR UPDATE` (the
# teardown-wait test) and `DROP DATABASE … WITH (FORCE)`. A superuser of a server running OUTSIDE the
# sandbox could run `COPY … TO PROGRAM` as the host's user — a sandbox escape. Inside the stage's
# sandbox the server is a descendant of the stage: its process tree, its PID namespace, its tmpfs, its
# lifetime. Its socket lives in the stage's own directory (`listen_addresses=''`, no TCP, 0700), so
# nothing outside the sandbox can reach it, and it dies with the stage. The snapshot itself is
# read-only (/opt), so no run can change what the next run starts from.
#
# STALE OR FOREIGN SNAPSHOT: `start` refuses a snapshot without its `ready` marker, or whose
# PG_VERSION is not this image's server major — it never re-initialises in a run.
set -euo pipefail

TEMPLATE=mw_test_template
SUPER=build

pg_bin() {
  local bin="${PG_BIN_DIR:-}"
  if [ -z "$bin" ] && [ -d /usr/lib/postgresql ]; then
    bin="$(find /usr/lib/postgresql -mindepth 2 -maxdepth 2 -type d -name bin | sort -V | tail -n 1)"
  fi
  [ -n "$bin" ] && [ -x "$bin/initdb" ] && [ -x "$bin/pg_ctl" ] && [ -x "$bin/psql" ] \
    || { echo "host-postgres: no PostgreSQL server binaries (${bin:-/usr/lib/postgresql/*/bin})" >&2; return 1; }
  printf '%s' "$bin"
}

server_opts() {  # <socket dir>
  printf '%s' "-c listen_addresses='' -k $1 -c unix_socket_permissions=0700 -c max_connections=500 -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
}

# prepare <snapshot> — initdb, the pgvector template, stopped cleanly, a ready marker. Image build only.
prepare() {
  local snap="$1" bin sock
  bin="$(pg_bin)"
  mkdir -p "$snap"; rm -rf "${snap:?}"/* ; chmod 0700 "$snap"   # the directory may be the image's, owned by us
  "$bin/initdb" -D "$snap/data" -U "$SUPER" -A trust -E UTF8 --locale=C.UTF-8 >"$snap/initdb.log" 2>&1 \
    || { echo "host-postgres: initdb failed:" >&2; tail -n 20 "$snap/initdb.log" >&2; return 1; }
  sock="$(mktemp -d)"
  "$bin/pg_ctl" -D "$snap/data" -l "$snap/prepare.log" -w -t 120 -o "$(server_opts "$sock")" start >/dev/null \
    || { echo "host-postgres: the server did not start:" >&2; tail -n 20 "$snap/prepare.log" >&2; return 1; }
  local q=("$bin/psql" -X -q -v ON_ERROR_STOP=1 -h "$sock" -U "$SUPER" -At)
  "${q[@]}" -d postgres -c "CREATE DATABASE $TEMPLATE" \
    && "${q[@]}" -d "$TEMPLATE" -c "CREATE EXTENSION vector" \
    && "${q[@]}" -d postgres -c "ALTER DATABASE $TEMPLATE IS_TEMPLATE true" \
    || { "$bin/pg_ctl" -D "$snap/data" -m fast stop >/dev/null 2>&1 || true; echo "host-postgres: the template could not be prepared (is pgvector installed?)" >&2; return 1; }
  local vector
  vector="$("${q[@]}" -d "$TEMPLATE" -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'")"
  "$bin/pg_ctl" -D "$snap/data" -m fast -w stop >/dev/null
  rm -rf "$sock" "$snap/data/postmaster.opts"
  printf 'template=%s\nvector=%s\nmajor=%s\n' "$TEMPLATE" "$vector" "$(cat "$snap/data/PG_VERSION")" > "$snap/ready"
  echo "host-postgres: prepared cluster at $snap — template $TEMPLATE with pgvector $vector" >&2
}

# start <snapshot> <dir> — a copy of the snapshot on <dir> (the stage's tmpfs), started on a Unix
# socket in <dir>; prints the export line on stdout.
start() {
  local snap="$1" dir="$2" bin
  bin="$(pg_bin)"
  [ -f "$snap/ready" ] && [ -f "$snap/data/PG_VERSION" ] \
    || { echo "host-postgres: $snap is not a prepared cluster (no ready marker) — the image must carry it; a run never initialises one" >&2; return 1; }
  local want have
  want="$(sed -n 's/^major=//p' "$snap/ready")"; have="$("$bin/postgres" --version | awk '{print $3}' | cut -d. -f1)"
  [ "$want" = "$have" ] && [ "$(cat "$snap/data/PG_VERSION")" = "$have" ] \
    || { echo "host-postgres: the snapshot is PostgreSQL $want but this image's server is $have — a stale snapshot is refused, never upgraded in a run" >&2; return 1; }
  rm -rf "$dir"; mkdir -p "$dir"; chmod 0700 "$dir"
  cp -a "$snap/data" "$dir/data"
  chmod 0700 "$dir/data"
  "$bin/pg_ctl" -D "$dir/data" -l "$dir/server.log" -w -t 60 -o "$(server_opts "$dir")" start >/dev/null \
    || { echo "host-postgres: the server did not start:" >&2; tail -n 20 "$dir/server.log" >&2; return 1; }
  echo "host-postgres: server up on the Unix socket in $dir (no TCP), from the prepared snapshot ($(tr '\n' ' ' < "$snap/ready"))" >&2
  printf "export MW_TEST_PG_CONNECTION='Host=%s;Username=%s;Database=postgres' MW_TEST_PG_TEMPLATE='%s'\n" "$dir" "$SUPER" "$TEMPLATE"
}

if [ "${1:-}" = "--self-test" ]; then
  st="$(mktemp -d)"
  bin="$(pg_bin)"
  cleanup() {
    for d in "$st/a" "$st/b"; do "$bin/pg_ctl" -D "$d/data" -m immediate stop >/dev/null 2>&1 || true; done
    rm -rf "$st"
  }
  trap cleanup EXIT
  fail() { echo "SELF-TEST FAILED: $1"; exit 1; }
  prepare "$st/snap" 2>/dev/null || fail "the prepared cluster could not be built"
  grep -q '^vector=[0-9]' "$st/snap/ready" || fail "pgvector is not in the template"
  [ ! -e "$st/snap/data/postmaster.pid" ] || fail "the snapshot was left running"
  out_a="$(start "$st/snap" "$st/a" 2>/dev/null)" || fail "a stage server did not start from the snapshot"
  out_b="$(start "$st/snap" "$st/b" 2>/dev/null)" || fail "a second stage server did not start beside the first"
  case "$out_a" in *"MW_TEST_PG_CONNECTION='Host=$st/a;"*"MW_TEST_PG_TEMPLATE='$TEMPLATE'"*) ;; *) fail "the export line does not name the stage's socket and the template: $out_a" ;; esac
  q=("$bin/psql" -X -q -v ON_ERROR_STOP=1 -U "$SUPER" -At)
  "${q[@]}" -h "$st/a" -d postgres -c "CREATE DATABASE suite_a TEMPLATE $TEMPLATE" || fail "the template cannot be cloned"
  [ "$("${q[@]}" -h "$st/a" -d suite_a -c "SELECT count(*) FROM pg_extension WHERE extname = 'vector'")" = 1 ] || fail "the clone carries no pgvector"
  [ "$("${q[@]}" -h "$st/a" -d suite_a -c "SHOW server_encoding")" = UTF8 ] || fail "the clone is not UTF8 (jsonb would refuse non-ASCII escapes)"
  [ "$("${q[@]}" -h "$st/a" -d suite_a -c "SELECT '{\"a\":\"\\u00e9\"}'::jsonb ->> 'a'")" = "é" ] || fail "jsonb does not decode a non-ASCII \\u escape"
  "${q[@]}" -h "$st/a" -d suite_a -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE TABLE t (v vector(3)); INSERT INTO t VALUES ('[1,2,3]')" || fail "a suite's DDL does not run on its clone"
  # What the suites need a superuser for, exactly as on the CI runners' shared server.
  "${q[@]}" -h "$st/a" -d postgres -c "BEGIN; SELECT 1 FROM pg_database WHERE datname = 'suite_a' FOR UPDATE; ROLLBACK" >/dev/null || fail "pg_database cannot be row-locked (the teardown-wait test)"
  "${q[@]}" -h "$st/a" -d postgres -c "DROP DATABASE suite_a WITH (FORCE)" || fail "DROP DATABASE WITH (FORCE) refused"
  # Isolation: the two stage servers are separate clusters; the snapshot is untouched by either.
  "${q[@]}" -h "$st/b" -d postgres -c "CREATE DATABASE only_in_b TEMPLATE $TEMPLATE" || fail "the second server cannot clone"
  [ "$("${q[@]}" -h "$st/a" -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = 'only_in_b'")" = 0 ] || fail "two stage servers share state"
  [ ! -e "$st/snap/data/postmaster.pid" ] || fail "a start ran on the snapshot itself instead of a copy"
  # Negative controls — each MUST be refused.
  if "$bin/psql" -X -h 127.0.0.1 -U "$SUPER" -d postgres -c "SELECT 1" >/dev/null 2>&1; then fail "a stage server answered on TCP"; fi
  if start "$st/nowhere" "$st/c" >/dev/null 2>&1; then fail "a start without a prepared snapshot was not refused"; fi
  cp -a "$st/snap" "$st/stale"; sed -i 's/^major=.*/major=9/' "$st/stale/ready"
  if start "$st/stale" "$st/d" >/dev/null 2>&1; then fail "a snapshot of another PostgreSQL major was not refused"; fi
  echo "host-postgres self-test: passed (prepared UTF8 cluster with a pgvector template; two stage servers start from copies, clone the template, run the suites' superuser operations, share nothing and leave the snapshot untouched; TCP, a missing and a stale snapshot are refused)"
  exit 0
fi

case "${1:-}" in
  prepare) prepare "${2:?prepare <snapshot>}" ;;
  start) start "${2:?start <snapshot> <dir>}" "${3:?start <snapshot> <dir>}" ;;
  *) echo "usage: host-postgres.sh prepare <snapshot> | start <snapshot> <dir> | --self-test" >&2; exit 2 ;;
esac
