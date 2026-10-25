# MeshWeaver.Deploy

The public deployment reference for [MeshWeaver](https://github.com/Systemorph/MeshWeaver):

| Path | What |
|---|---|
| `deploy/helm` | the Helm chart that runs a MeshWeaver portal (portal, migration Job, optional registry, KEDA, PDB, ingress) |
| `deploy/helm-db` | the per-instance PostgreSQL chart |
| `deploy/aks` | an example AKS overlay, the cluster manifests, and the chart / drift / observability tooling |
| `deploy/homebrew` | `memex-local` — run the chart on a Mac (`brew tap systemorph/memex && brew install memex-local`) |
| `deploy/build-host` | the standing build host's runner image `mw-build-host` (Dockerfile + `stage-context.sh`) |
| `.github/scripts/build-run.py` | the build host's runner (claims runs, executes stages in a `bwrap` sandbox) |
| `.github/workflows/build-host.yml` | on every platform set the platform's CD hands over: the runner image, then the newest armed set handed to `build-latest` |
| `.github/workflows/build-host-stable.yml` | once a day: the main-passed ceiling handed to `build-stable` |
| `test/MeshWeaver.Deploy.ChartGuards` | guards that hold the chart to the platform's code |

Everything here is reference material: defaults, placeholders and example values. An installation's
own values live in its operator's private deployments repository, never here.

## How a platform set reaches this repository

The platform's CD (`Systemorph/MeshWeaver` `main-cd.yml`) BUILDS and SEALS the product image set
(portal, migration, plugin-test, control) and nothing else — and it dispatches to NO repository.
The platform knows nothing about what consumes its sets (its policy
`dependents-verify-on-their-side`, held by its guard
`PlatformReleaseNotifyGuard.CoreDispatchesToNoRepository`), so this repository PULLS.

Every 15 minutes `build-host.yml` (`schedule`) reads the platform's sealed sets from the registry
they were sealed into: a set is PROMOTED when `mw-plugin-test` carries its version tag and ARMED
when `memex-portal-ai` does, and both tags sit on the manifest whose `staging-<core7>-<run id>` tag
names the platform commit and the CD run (`platform-set-dispatch.py discover`). The newest ARMED,
then the newest PROMOTED, set that still has no `mw-build-host:<version>` becomes the hand-over; when
every runner is there, the tick builds nothing and re-sends the newest armed set to `build-latest`
(idempotent at the receiver). A set therefore reaches `build-latest` within one tick of being armed,
and nothing in the platform's delivery waits on it.

Two events carry the same hand-over explicitly, read through the same contract (schema 1): a
`workflow_dispatch` (a person re-running one) and a `repository_dispatch` of type
`platform-set-sealed` — the shape an event-based emitter (memex, which fans the platform's events
out; never the platform's CD) sends:

```json
{ "schema": 1,
  "run":   { "url": "<the CD run>", "coreSha": "<40 hex>" },
  "set":   { "version": "<x.y.z>", "coreSha": "<40 hex>" },
  "armed": { "armed": true, "version": "<x.y.z>", "coreSha": "<40 hex>", "pluginsSha": "<40 hex>" } }
```

`set` is the set whose runner to build (both fields empty for none), `armed` what an arming decided
(a discovered hand-over names none). Nothing in it is trusted by shape:
`.github/scripts/platform-set-dispatch.py` validates every field, `build-host.yml` checks every
platform commit is on the platform's `main` (compare API) and every version against the registry
before it builds or signs anything, and the hand-over re-reads the newest armed set from the
registry itself. A missed tick heals on the next one.

## Deployment material: what moved here, what is still in the platform

The platform keeps BUILD + SEAL; deployment lives here (core slimming). The inventory, with what
is moved, what is in flight, and what is still owed, is kept in ONE place — the platform's
[Carving Projects Out of Core → Deployment material](https://github.com/Systemorph/MeshWeaver/blob/main/src/MeshWeaver.Documentation/Data/Architecture/CarvingProjectsOutOfCore.md).
The hosting operator's image (`hosting-operator`) is NOT here: its scripts are versioned with the
`Hosting/InstanceAction` plan that names them, so it is built by MeshWeaver.Plugins'
`hosting-operator.yml` — the ONE pipeline that publishes `hosting-operator:main` — which bakes this
repository's charts and manifests in.

## Developing

```bash
deploy/aks/scripts/check-chart-invariants.sh     # render every values combination, assert it is coherent
deploy/aks/scripts/check-values-are-read.sh      # every key a values file sets reaches a template
dotnet test test/MeshWeaver.Deploy.ChartGuards -p:MeshWeaverRoot=../MeshWeaver
python3 .github/scripts/build-run.py --self-test                 # the build host's runner
python3 .github/scripts/platform-set-dispatch.py --self-test     # the platform's hand-over contract
deploy/build-host/stage-context.sh . ../MeshWeaver /tmp/ctx && docker build -f deploy/build-host/Dockerfile /tmp/ctx
```

Working rules for agents and people: [AGENTS.md](AGENTS.md).

Licensed under Apache-2.0 or MIT, at your option — see [LICENSE](LICENSE).
