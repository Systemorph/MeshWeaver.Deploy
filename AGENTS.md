# AGENTS.md — MeshWeaver.Deploy

Guidance for AI agents (Claude Code, Copilot, …) working in this repository.

This repository is MeshWeaver's **public deployment reference**: the Helm chart that runs a
MeshWeaver portal, the per-instance database chart, an example AKS overlay and the cluster
manifests, the chart drift tooling, and `memex-local` (the Homebrew CLI that runs the chart on a
Mac). It left the platform repository so that the platform stays framework only (core slimming).

> 📖 **The fleet's working rules live in the platform's
> [`AGENTS.md`](https://github.com/Systemorph/MeshWeaver/blob/main/AGENTS.md)** — worktrees, the
> merge-latest-before-push rule, root cause only, gates that never test their own inputs, REST not
> GraphQL, closing keywords, no client names in anything published, policy not prose. Everything
> there applies here unchanged; this file carries only what is specific to this repository.

> 🔓 **This repository is PUBLIC, and that is the point of it.** Every reader — the hosting
> operator's image build, the estate's deploy lanes, the Homebrew tap, the chart guards — reads it
> with **no credential**. So it holds REFERENCE material only: defaults, placeholders, example
> values, `portal.example.com`. **An installation's real values never land here** — hosts,
> namespaces, database or server names, Key Vault secret names, tenant or subscription ids,
> operator identities. Those live in the estate's private deployments repository. Before every
> commit ask: *would this line identify a live installation?* If yes, it does not belong here.

## What lives where

| Path | What it is | Who reads it |
|---|---|---|
| `deploy/helm/` | the portal chart (+ `values.registry.example.yaml`) | the hosting operator image (baked in), the estate's deploy lanes, `memex-local` |
| `deploy/helm-db/` | the per-instance database chart | the hosting operator image |
| `deploy/aks/values.aks.yaml` | the generic AKS overlay (placeholders only) | the chart gate; first-time setup |
| `deploy/aks/manifests/` | cluster manifests the governed `ApplyClusterManifest` applies, incl. the operator's ClusterRole | the estate's aks-ops lane; the operator's RBAC tests |
| `deploy/aks/scripts/` | chart invariants, values-are-read, chart-drift render/compare/record, observability checks, fixtures, setup examples | the chart gate; the operator image (drift scripts) |
| `deploy/homebrew/` | `memex-local` — CLI, formula, local values, tests | `homebrew.yml` → the public tap `Systemorph/homebrew-memex` |
| `deploy/build-host/`, `.github/scripts/build-run.py`, `build-host-announcement.py`, `platform-set-dispatch.py`, `.github/build-hosts.json` | the standing build host's runner image and runner, and the hand-over of the platform's sets to `build-latest` / `build-stable` | `build-host.yml` (on the platform's `platform-set-sealed` dispatch), `build-host-stable.yml` (daily) |
| `scripts/check-config-key-coverage.py` | overlay ↔ chart key check | the estate's `config-key-coverage` |
| `test/MeshWeaver.Deploy.ChartGuards/` | the guards that hold the chart to the platform's code (probe paths, drain deadline, ConfigMap keys, the migration Job, …) | `chart-guards.yml`, against a platform checkout |

## The platform is read, never vendored

Some checks compare this chart with the platform's code or scripts (`ProbeEndpoints`, the drain
deadline in `ServiceDefaults`, the bundle publisher, the registry retention record). They check the
public `Systemorph/MeshWeaver` out at a DECLARED ref (`MW_PLATFORM_REF`, fail-closed — an empty ref
is red, never the default branch by accident) and read it there. Nothing of the platform is copied
into this tree.

## Work in worktrees

`git worktree add .worktrees/<name> -b feat/<name> origin/main`. Never edit, build or commit on the
primary checkout, never `git stash` (use `git diff > patch` + `git apply`). `.worktrees/` is
gitignored.

## Gates

Every job caps at a literal `timeout-minutes` ≤ 45 and carries no input-shaped `if:` and no
`continue-on-error:`. The REQUIRED ones run on every pull request, with no path filter:

1. `chart-gate.yml` — `Chart invariants`, `Observability values (rendered + binary-validated)`,
   `Bundle registry scripts (executed)`, `Registry retention (this chart against core's record)`.
2. `chart-guards.yml` — `Chart guards (vs platform)`: the C# guards built and run against the
   declared platform ref.

3. `build-host.yml` — `Build host (runner image + self-tests)`: the runner image built against the
   platform's `main` with every self-test, on every pull request. Not yet in the ruleset: adding it
   is an owner's change to `main pr protection`. (Its publishing jobs run only on
   the two events that carry a hand-over; `preflight` is red there on a missing credential.)

`homebrew.yml` is path-filtered to `deploy/homebrew/**` and `deploy/helm/**` and therefore NOT a
required check (a skipped required check reads as satisfied): `memex-local (Linux — …)` and
`brew install + test (macOS)` on a change there, and the tap publish on `main`.

A chart change is effective at the next deploy that reads this repository's `main`, and the
operator image picks it up on its next build — so a bad chart change is live on merge. Treat a red
gate as a stop.

## Git workflow

When the work is finished, finish it: worktree → change → local gates → `git fetch origin && git
merge origin/main` → push → pull request → answer every review thread ON the thread → green →
merged. Never push to `main`, never merge on red or pending. Commit and pull-request attribution
follows the platform's AGENTS.md.

## Local commands

```bash
deploy/aks/scripts/check-chart-invariants.sh          # needs helm + PyYAML
deploy/aks/scripts/check-values-are-read.sh
deploy/aks/scripts/test-chart-drift-compare.sh
dotnet build test/MeshWeaver.Deploy.ChartGuards -c Release -warnaserror -p:MeshWeaverRoot=<platform checkout>
```
