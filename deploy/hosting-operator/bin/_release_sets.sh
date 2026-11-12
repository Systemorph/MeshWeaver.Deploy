# shellcheck shell=bash
# shellcheck disable=SC2154  # namespace/database/values/image are the CALLER's (the contract below)
# _release_sets.sh — the ONE place a release's per-instance `--set` flags are composed. Sourced,
# never executed, by BOTH readers of a record's render:
#
#   hosting-deploy        what a Provision / Roll / Reconcile installs (helm template + helm upgrade)
#   hosting-chart-drift   what an Audit compares the cluster against
#
# 🚨 WHY ONE FILE (MeshWeaver#5844). The drift audit used to render the committed overlay, while the
# deploy renders the record's values PLUS these flags — most visibly `selfUpdate.azureClientId`,
# which the chart turns into the portal's inline AZURE_CLIENT_ID. So the audit called values every
# deploy installs "pending deletions" — findings about an input no deploy reads, not about the cluster.
# A second copy of these lines in the audit would drift from this one the same way, so there is no
# second copy: change a flag HERE and both readers move together.
#
# CONTRACT. The caller has set, and validated:
#   namespace   the release's namespace          database   the instance's database name
#   values      the record's rendered values     image      the portal image to deploy, or empty
# and on return:
#   sets        the `--set` arguments, in order (an array)
#   image       unchanged when given; otherwise the image the namespace is already running when the
#               values carry none (never a chart default)
# Every refusal is hosting::die, so this file is sourced at top level — never inside $(…), where a
# die would end only the subshell.

# Per-instance identity, derived from the record. Everything else comes from the template.
host="${HOSTING_HOST:-}"
pg_host="${HOSTING_PG_HOST:-}"

# 🚨 A DEPLOY MUST NOT ROLL THE BUILD. Two things write the portal image: this upgrade (from
# portal.image) and the in-portal self-updater, which patches the live Deployment on every
# publication event. If the record pins a tag the plan passes it as --image and that is a
# deliberate roll. Otherwise: keep the image the install is ALREADY RUNNING, so the upgrade is a
# no-op on the build; and on a first install with nothing running and no pin, REFUSE — the chart
# default is ghcr :latest, which on this fleet is a release tag that cannot boot.
#
# 🚨 The question is whether the values carry the KEY portal.image, not whether they carry a
# `portal:` BLOCK. HelmValues renders the block for the pull Secret alone (`portal:` +
# `  imagePullSecret: "registry-pull"`) whenever the record names one and pins no tag. The old test
# (`grep -q '^portal:'`) read that block as "the values carry an image", skipped the keep-running
# step, and helm fell through to the chart default. Measured on fabrikam, 2026-09-21 11:25Z
# (Systemorph/Memex#458): a Reconcile, documented never to move the image, took the instance from
# 3.0.0+67cbbe0ee (2026-09-08) back to ghcr.io/systemorph/memex-portal-ai:latest = 3.0.0-rc13
# (2026-08-31). Every record with an imagePullSecret and no pin (fabrikam, build) had this exposure.
# HelmValues emits a fixed shape (top-level keys at column 0, children indented by two, strings
# always double-quoted), so the key is found by its position under `portal:`. An empty `image: ""`
# does not count as an image. Pure bash on purpose: an awk regex with POSIX classes passed on the
# macOS awk and matched NOTHING on the ubuntu runner's mawk — a guard whose answer depends on which
# awk the image happens to ship is not a guard.
values_block_key() { # <block> <key> <file> → prints <block>.<key> when set and non-empty
  local block="$1" key="$2" line value in_block=false
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|' '*|$'\t'*|'#'*) ;;
      *) # a top-level key: we are inside <block>: only when the line is exactly `<block>:`
         value="${line#"${block}":}"; value="${value//[[:space:]]/}"
         if [[ "$line" == "${block}:"* && -z "$value" ]]; then in_block=true; else in_block=false; fi
         continue ;;
    esac
    if $in_block && [[ "$line" == "  ${key}:"* ]]; then
      value="${line#"  ${key}:"}"; value="${value//[[:space:]]/}"; value="${value//\"/}"
      [ -n "$value" ] && { printf '%s' "$value"; return 0; }
    fi
  done < "$3"
  return 1
}
values_block_image() { values_block_key "$1" image "$2"; } # <block> <file> → <block>.image
values_carry_portal_image() { values_block_image portal "$1" >/dev/null; }

# ── the ingress TLS Secret — the RECORD's, never a name invented over it ──
# 🚨 `--set` beats every `-f`, so a flag composed here OVERRIDES what the record rendered. It used to
# be `${HOSTING_TLS_SECRET:-${namespace}-tls}` unconditionally, which replaced a record's own
# `ingress.tlsSecret` with `<namespace>-tls`. Measured on an enterprise estate's instance, 2026-11-11 from 09:02Z: the
# record (and its overlay) name `keyvault-memex-portal`, the Secret the Key Vault CSI driver creates
# from the instance's certificate; the first green Roll set `memex-tls`, a Secret that did not
# exist, and nginx served "Kubernetes Ingress Controller Fake Certificate" on all three ingresses
# (portal, grpc, mcp) — "local SSL certificate memex/memex-tls was not found". So, in order:
#   HOSTING_TLS_SECRET   an explicit operator override (a hand-run, a behaviour test) — still wins;
#   ingress.tlsSecret    what the record rendered (HelmValues.TlsSecret: stated, else {namespace}-tls);
#   {namespace}-tls      only for values that carry no ingress block at all (a pre-record values file).
# A record that names no secret renders `{namespace}-tls` itself, so for it nothing changes.
tls_secret="${HOSTING_TLS_SECRET:-}"
[ -n "$tls_secret" ] || tls_secret="$(values_block_key ingress tlsSecret "$values" || true)"
[ -n "$tls_secret" ] || tls_secret="${namespace}-tls"
hosting::safe_name tls-secret "$tls_secret"

sets=(
  --set "ingress.tlsSecret=${tls_secret}"
  --set "config.memex_migration.MEMEX_DATABASENAME=${database}"
  --set "config.memex_portal.MEMEX_DATABASENAME=${database}"
)
[ -n "$host" ] && sets+=( --set "ingress.host=${host}" )

# migration_image_for <portal image> — the memex-migration image paired with a portal image: the SAME
# tag, on the values' own migration repository when the record rendered one, else `memex-migration`
# beside the portal's repository in the same registry path.
# 🚨 It used to be `${image/memex-portal-ai/memex-migration}`, which is right for exactly ONE portal
# repository and a silent no-op for every other: on `meshweaver.azurecr.io/memex-control` it yielded
# the PORTAL image, helm rendered the migration Job to run the portal (control, revision 4,
# 2026-10-06 06:05Z: Job memex-migration-4 Failed), and every later Roll then refused in
# hosting-migrate — "runs no memex-migration image — nothing to retarget". The record knows the
# repository (HelmValues.MigrationImage reads MigrationImageRepository), but it renders it only with
# a tag, so a Reconcile/Roll file names none and the derivation here is what decides.
# Prints nothing and returns 1 when the result would not be a plain memex-migration reference — a
# digest-pinned portal image has no tag to carry — so a caller refuses instead of deploying a guess.
migration_image_for() {
  local portal="$1" repo tag mig
  [[ "$portal" =~ ^[A-Za-z0-9][A-Za-z0-9./_-]*/[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || return 1
  tag="${portal##*:}"
  repo="$(values_block_image migration "$values" || true)"
  if [[ "$repo" =~ /memex-migration:[^/]*$ ]]; then repo="${repo%:*}"; else repo="${portal%:*}"; repo="${repo%/*}/memex-migration"; fi
  mig="${repo}:${tag}"
  [[ "$mig" =~ ^[A-Za-z0-9][A-Za-z0-9./_-]*/memex-migration:[A-Za-z0-9][A-Za-z0-9._-]*$ ]] && [ "$mig" != "$portal" ] || return 1
  printf '%s' "$mig"
}
if [ -z "$image" ] && ! values_carry_portal_image "$values"; then
  # 🚨 THREE ANSWERS, not two (MeshWeaver#4722) — and this one is the most expensive to get wrong
  # in the whole file. The read used to be `2>/dev/null || true`, so a Forbidden on `deployments`
  # left $running empty and the else-branch below announced that "namespace <ns> runs no portal
  # yet (first install)" and asked for an image tag to be pinned on the record. Following that
  # remedy PINS A TAG, which is a deliberate roll of the build — exactly what the comment above
  # this block exists to prevent. A refusal must stop here instead, with nothing to act on wrongly.
  hosting::probe any kubectl -n "$namespace" get deploy memex-portal-deployment \
      -o jsonpath='{.spec.template.spec.containers[?(@.name=="memex-portal")].image}'
  case $? in
    2) hosting::die_refused "deployment/memex-portal-deployment in ${namespace}, which decides whether this upgrade keeps the image the install is already running" ;;
  esac
  running="$HOSTING_PROBE_OUT"
  if [ -n "$running" ]; then
    image="$running"
    hosting::log "image     keeping the running ${image}"
  else
    hosting::die "no image to deploy: the record pins no tag, the values file carries none, and namespace ${namespace} runs no portal yet (first install). Pin an image tag on the Deployment record for the first install."
  fi
fi
if [ -n "$image" ]; then
  migration_image="$(migration_image_for "$image")" \
    || hosting::die "cannot pair a memex-migration image with the portal image '${image}' (it is not a plain registry/…/repository:tag reference) — refusing to render a migration Job that would run something else"
  sets+=( --set "portal.image=${image}" --set "migration.image=${migration_image}" )
fi
if [ -n "$pg_host" ]; then
  sets+=(
    --set "config.memex_portal.MEMEX_HOST=${pg_host}"
    --set "config.memex_migration.MEMEX_HOST=${pg_host}"
    --set "config.memex_migration.MEMEX_JDBCCONNECTIONSTRING=jdbc:postgresql://${pg_host}:5432/${database}"
  )
fi

# ── the portal's Azure workload identity — the client id of the identity hosting-federate binds ──
# 🚨 Federation is HALF of workload identity (Systemorph/MeshWeaver.Plugins#2994). hosting-federate
# binds `system:serviceaccount:<ns>:memex-portal-sa` to $AZ_PORTAL_IDENTITY on every Provision, but
# the chart turns that into a usable credential only when `selfUpdate.azureClientId` is set: it is
# what renders the ServiceAccount's `azure.workload.identity/client-id` annotation, the pod's
# `azure.workload.identity/use` label and AZURE_CLIENT_ID. Nothing rendered it on the record-driven
# path — HelmValues has no client id to render, because the record carries the identity's NAME
# (operator.environment AZ_PORTAL_IDENTITY), never its id — so every instance provisioned from a
# record on an ACR imageRepository booted with no token exchange, and its self-updater failed EVERY
# check with `CredentialUnavailableException … WorkloadIdentityCredential authentication
# unavailable` (the control instance's Admin/UpdatePolicy, measured 2026-10-06 and 2026-10-07).
# memex and memex-cloud only worked because a pre-record values file had set the key; the vault
# half now refuses structure (Memex#295), so their next Reconcile would have lost it as well.
#
# So the id is READ here, from the identity the same step federates to, with the identity the
# operator already holds over it (Managed Identity Contributor, backups.bicep) — never written into
# the record, the vault half or the repository. No identity named → no workload identity, said by
# name: a local or non-Azure install legitimately has none, and an instance on a non-ACR registry
# authenticates with its instance key instead.
if [ -n "${AZ_PORTAL_IDENTITY:-}" ]; then
  hosting::need_env AZ_RESOURCE_GROUP "the resource group holding the portal's managed identity (the one hosting-federate binds this namespace to)"
  hosting::safe_name portal-identity "$AZ_PORTAL_IDENTITY"
  hosting::safe_name resource-group "$AZ_RESOURCE_GROUP"
  hosting::probe output az identity show -g "$AZ_RESOURCE_GROUP" -n "$AZ_PORTAL_IDENTITY" --query clientId -o tsv
  case $? in
    0) ;;
    2) hosting::die "REFUSED, not absent: this operator's Azure identity may not read managed identity ${AZ_PORTAL_IDENTITY} in ${AZ_RESOURCE_GROUP} — so its client id is UNKNOWN and the pods would come up with no workload identity. It needs read on that identity (backups.bicep grants Managed Identity Contributor). Refused: ${HOSTING_PROBE_ERR}" ;;
    *) hosting::die "could not read the client id of the portal identity ${AZ_PORTAL_IDENTITY} in ${AZ_RESOURCE_GROUP} — without it the pods get no workload identity and the self-updater cannot list the registry. az said: ${HOSTING_PROBE_ERR:-nothing}" ;;
  esac
  portal_client_id="${HOSTING_PROBE_OUT//[[:space:]]/}"
  [[ "$portal_client_id" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || hosting::die "managed identity ${AZ_PORTAL_IDENTITY} answered a client id that is not a GUID ('${portal_client_id}') — refusing to interpolate it into the release"
  sets+=( --set "selfUpdate.azureClientId=${portal_client_id}" )
  hosting::log "identity  pods federate as ${AZ_PORTAL_IDENTITY} (client ${portal_client_id})"
  hosting::say portal_identity "$portal_client_id"
else
  hosting::log "identity  AZ_PORTAL_IDENTITY is not set — the pods get NO Azure workload identity (selfUpdate.azureClientId stays empty); a self-updater listing an Azure Container Registry cannot authenticate. Name the identity in the record's operator.environment if this instance needs one."
  hosting::say portal_identity none
fi
