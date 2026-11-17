#!/usr/bin/env bash
# hosting-db-reset against a REAL, throwaway PostgreSQL server (MeshWeaver.Plugins#4336).
#
#   bash test/db-reset-real-postgres.sh          # inside the hosting-operator image, with
#                                                # PGHOST / PGUSER / PGPASSWORD naming a DISPOSABLE server
#
# run-tests.sh drives hosting-db-reset against a psql STUB: it proves which statements the script
# issues and what it does when one fails, and nothing about what PostgreSQL does with them. This
# script is the other half. It runs the operator's own hosting-db-reset, inside the image a
# lifecycle Job runs, against a real server that exists only for this run, and reads every claim
# back from that server:
#
#   1. a connected session makes `keep` REFUSE, and nothing changes;
#   2. `keep` renames the database to <db>_kept_<yyyymmddhhmm>, stamps it with the marker comment,
#      and creates an EMPTY <db> with the old owner, encoding and collation; the kept copy still
#      holds the seeded row;
#   3. `show` reads the kept copy back: kept_database / kept_until / kept_verified=true;
#   4. `purge` refuses the copy it just kept (inside the retention), refuses an old copy without the
#      marker, and drops an old copy that carries it.
#
# It does NOT prove what only a live estate can: the Key Vault read of the admin password (the
# password comes from PGPASSWORD here), the plan's quiesce and migrate steps, or the mesh reading
# the ::hosting:: lines back into keptDatabase / keptUntil. Those stay owed to the one real run on a
# throwaway instance that MeshWeaver.Plugins#4336 asks for.
#
# 🚨 It creates and drops databases on PGHOST. Point it ONLY at a server started for this run; it
# refuses a host that does not say so (HOSTING_DBR_REAL_DISPOSABLE=yes).
set -uo pipefail

[ "${HOSTING_DBR_REAL_DISPOSABLE:-}" = yes ] || {
  echo "db-reset-real-postgres: refusing — set HOSTING_DBR_REAL_DISPOSABLE=yes to confirm ${PGHOST:-<no PGHOST>} is a server started for this run" >&2
  exit 2
}
: "${PGHOST:?PGHOST names the throwaway server}" "${PGUSER:?PGUSER is its admin login}" "${PGPASSWORD:?PGPASSWORD is its admin password}"
export PGHOST PGUSER PGPASSWORD

BIN="${HOSTING_BIN:-/opt/hosting/bin}"
DB=acmedb OWNER=acmeowner
pass=0 fail=0
ok()  { pass=$((pass + 1)); echo "  ✓ $*"; }
bad() { fail=$((fail + 1)); echo "  ✗ $*"; }
q()   { psql -d "${2:-postgres}" -v ON_ERROR_STOP=1 -tAc "$1" | tr -d '[:space:]'; }
reset() { "$BIN/hosting-db-reset" "$@" --database "$DB" --server "$PGHOST" --user "$PGUSER" 2>&1; }

# The server answers at all — otherwise every check below would fail for the wrong reason.
for _ in $(seq 1 60); do q "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
[ "$(q "SELECT 1")" = 1 ] || { echo "db-reset-real-postgres: ${PGHOST} does not answer — no verdict"; exit 2; }
echo "server: $(q "SHOW server_version") on ${PGHOST}; client: $(psql --version)"

# Seed: a role that owns the instance's database, and one row the kept copy must still hold.
psql -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE ROLE ${OWNER} LOGIN" \
  -c "CREATE DATABASE ${DB} OWNER ${OWNER} ENCODING 'UTF8' TEMPLATE template0" >/dev/null || { echo "seed failed — no verdict"; exit 2; }
psql -d "$DB" -v ON_ERROR_STOP=1 -q -c "CREATE TABLE mesh_nodes(path text primary key)" \
  -c "INSERT INTO mesh_nodes VALUES ('User/first-admin')" -c "ALTER TABLE mesh_nodes OWNER TO ${OWNER}" >/dev/null || { echo "seed failed — no verdict"; exit 2; }
before_shape="$(q "SELECT pg_get_userbyid(datdba)||'|'||pg_encoding_to_char(encoding)||'|'||datcollate FROM pg_database WHERE datname='${DB}'")"

echo "── 1. a connected session refuses the reset, and nothing changes"
psql -d "$DB" -tAc "SELECT pg_sleep(60)" >/dev/null 2>&1 &
holder=$!
for _ in $(seq 1 30); do [ "$(q "SELECT count(*) FROM pg_stat_activity WHERE datname='${DB}' AND pid <> pg_backend_pid()")" -ge 1 ] && break; sleep 0.2; done
out="$(reset keep)"; rc=$?
if [ "$rc" -ne 0 ] && grep -q "still connected" <<< "$out"; then ok "keep refused while a session is connected"; else bad "keep did not refuse a connected session (rc=${rc}): ${out}"; fi
[ "$(q "SELECT count(*) FROM pg_database WHERE datname ~ '^${DB}_kept_'")" = 0 ] && [ "$(q "SELECT count(*) FROM mesh_nodes" "$DB")" = 1 ] \
  && ok "the refusal changed nothing (no kept copy, the row is still in ${DB})" || bad "the refused keep changed the server"
psql -d postgres -tAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB}' AND pid <> pg_backend_pid()" >/dev/null
wait "$holder" 2>/dev/null

echo "── 2. keep: rename and keep, then an EMPTY database of the same shape"
out="$(reset keep --request Ops/Actions/resetdatabase-proof)"; rc=$?
echo "$out" | sed 's/^/     /'
kept="$(sed -n 's/^::hosting:: kept_database=//p' <<< "$out")"
until_iso="$(sed -n 's/^::hosting:: kept_until=//p' <<< "$out")"
[ "$rc" -eq 0 ] && grep -qx '::hosting:: reset=completed' <<< "$out" && ok "keep reported reset=completed" || bad "keep failed (rc=${rc})"
[[ "$kept" =~ ^${DB}_kept_[0-9]{12}$ ]] && ok "kept_database=${kept}" || bad "kept_database is '${kept}'"
[[ "$until_iso" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$ ]] && ok "kept_until=${until_iso}" || bad "kept_until is '${until_iso}'"
[ "$(q "SELECT count(*) FROM mesh_nodes WHERE path='User/first-admin'" "$kept")" = 1 ] \
  && ok "the kept copy, read from the server, still holds the seeded row" || bad "the kept copy does not hold the seeded row"
comment="$(psql -d postgres -tAc "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname='${kept}'")"
[[ "$comment" == "meshweaver-kept-copy of ${DB}; "*"keptUntil=${until_iso}; request=Ops/Actions/resetdatabase-proof"* ]] \
  && ok "the kept copy carries the marker comment naming the request" || bad "the kept copy's comment is '${comment}'"
[ "$(q "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')" "$DB")" = 0 ] \
  && ok "${DB} exists again and is EMPTY" || bad "${DB} is not empty after the reset"
after_shape="$(q "SELECT pg_get_userbyid(datdba)||'|'||pg_encoding_to_char(encoding)||'|'||datcollate FROM pg_database WHERE datname='${DB}'")"
[ "$after_shape" = "$before_shape" ] && ok "the new ${DB} has the old owner, encoding and collation (${after_shape})" || bad "shape changed: ${before_shape} → ${after_shape}"

echo "── 3. show reads the kept copy back from the server"
out="$(reset show)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "::hosting:: kept_database=${kept}" <<< "$out" && grep -qx "::hosting:: kept_until=${until_iso}" <<< "$out" \
  && grep -qx '::hosting:: kept_verified=true' <<< "$out" && ok "show: kept_database, kept_until and kept_verified=true" || bad "show answered (rc=${rc}): ${out}"

echo "── 4. purge: only a marked copy, only past its retention"
out="$(reset purge --kept "$kept")"; rc=$?
[ "$rc" -ne 0 ] && grep -q "may be purged from ${until_iso}" <<< "$out" && [ "$(q "SELECT count(*) FROM pg_database WHERE datname='${kept}'")" = 1 ] \
  && ok "purge refused the copy inside its retention, naming ${until_iso}" || bad "purge of a fresh copy (rc=${rc}): ${out}"
psql -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}_kept_202601010000" -c "CREATE DATABASE ${DB}_kept_202601010001" \
  -c "COMMENT ON DATABASE ${DB}_kept_202601010000 IS 'meshweaver-kept-copy of ${DB}; keptAt=2026-01-01T00:00Z; keptUntil=2026-01-15T00:00Z; request=proof'" >/dev/null
out="$(reset purge --kept "${DB}_kept_202601010001")"; rc=$?
[ "$rc" -ne 0 ] && grep -q "carries no 'meshweaver-kept-copy' comment" <<< "$out" && [ "$(q "SELECT count(*) FROM pg_database WHERE datname='${DB}_kept_202601010001'")" = 1 ] \
  && ok "purge refused an old copy without the marker" || bad "purge of an unmarked copy (rc=${rc}): ${out}"
out="$(reset purge --kept "${DB}_kept_202601010000")"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "::hosting:: purged=${DB}_kept_202601010000" <<< "$out" && [ "$(q "SELECT count(*) FROM pg_database WHERE datname='${DB}_kept_202601010000'")" = 0 ] \
  && ok "purge dropped the marked copy past its retention, and the server no longer lists it" || bad "purge of a due copy (rc=${rc}): ${out}"

echo "${pass} passed, ${fail} failed (hosting-db-reset against a real PostgreSQL)"
[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
