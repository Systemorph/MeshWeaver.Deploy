# The hosting operator

The `hosting-*` commands every step of a `Hosting/InstanceAction` plan runs, and the image a
lifecycle Job runs them in (`hosting-operator`).

| Path | What it is |
|---|---|
| `bin/` | `run.sh` (the image's ENTRYPOINT: it reads the plan's steps from the Job's ConfigMap and runs them in order), `_common.sh`, one `hosting-*` script per command, and their companions (`_audit.jq`, `_values_half.py`, `_dump_analyze_pod.sh`, `_storage_prune_pod.sh`). |
| `test/` | `run-tests.sh` — the behaviour suite, run inside the image against stub `az`/`kubectl`/`helm`/`psql` (`test/stubs/`) and fixtures (`test/fixtures/`) — and the static checks for RBAC coverage, chart kinds granted and discarded stderr. |
| `manual-keyvault.allow` | The governed verbs that may touch Key Vault (core's `check-manual-keyvault.py`, policy `secrets-write-only-entry`). |
| `Dockerfile` | The image: az, kubectl, helm, crane, the PostgreSQL client, jq, python3 + PyYAML, the scripts, both charts and the drift scripts. |

## Why it lives here

An estate's aks-ops lane reads `bin/` **from git**, at the commit its record's operator pin names.
A client estate's own GitHub App is installed on that client's configuration repository and nothing
else, so it can read no private Systemorph repository. While the scripts lived in the private
MeshWeaver.Plugins, every in-VNet run of such a lane failed at the token step
(MeshWeaver.Plugins#4755). Here, beside the chart the same image bakes, every reader is
credential-free.

The plan that names the commands (`Hosting/InstanceAction`) stays in MeshWeaver.Plugins, and so
does the publishing lane, which also checks that every command the plan emits ships in `bin/`.

## Rules for this tree

- **Reference material only.** This repository is public. No script carries an installation's
  value as a default — a vault, cluster, namespace, host or identity arrives as an argument or from
  the deployment record. Fixtures and stubs use neutral samples (`acme`, `kv-example`,
  `example-aks`, `portal.example.com`).
- **Build context.** The image is built from a staged layout, not this directory (see the
  Dockerfile header): `deploy/helm` → `helm`, `deploy/helm-db` → `helm-db`, `deploy/aks/scripts` →
  `aks/scripts`, `bin` → `aks/operator/bin`.
- **Gates.** `.github/workflows/hosting-operator.yml` here runs on every pull request touching this
  tree, the charts or the cluster manifests: tracked and executable, every script parses,
  shellcheck, the Key Vault gate, the image builds and carries exactly `bin/`'s command set, and the
  behaviour suite passes inside the image (it needs the `<n> passed, 0 failed` line). It publishes
  nothing.
- **Publishing.** MeshWeaver.Plugins `.github/workflows/hosting-operator.yml` checks this repository
  out at its declared deploy ref, re-proves the image and publishes `:main` and the immutable
  `<plugins sha7>-<core sha7>-<deploy sha7>` tag. Its hourly run publishes a change here once the
  input fingerprint (this tree, both charts, the drift scripts) differs from what `:main` carries.

Design of record: MeshWeaver.Plugins `Hosting/HostingOperator` (on the mesh: `get Hosting/HostingOperator`).
