# MeshWeaver.Deploy

The public deployment reference for [MeshWeaver](https://github.com/Systemorph/MeshWeaver):

| Path | What |
|---|---|
| `deploy/helm` | the Helm chart that runs a MeshWeaver portal (portal, migration Job, optional registry, KEDA, PDB, ingress) |
| `deploy/helm-db` | the per-instance PostgreSQL chart |
| `deploy/aks` | an example AKS overlay, the cluster manifests, and the chart / drift / observability tooling |
| `deploy/homebrew` | `memex-local` — run the chart on a Mac (`brew tap systemorph/memex && brew install memex-local`) |
| `deploy/build-host` | the standing build host's runner image `mw-build-host` (a self-contained Dockerfile: it fetches its sources by commit) |
| `.github/scripts/build-run.py` | the build host's runner (claims runs, executes stages in a `bwrap` sandbox) |
| `.github/workflows/build-host.yml` | on every platform set the platform's CD hands over: the runner image, then the newest armed set handed to `build-latest` |
| `.github/workflows/build-host-stable.yml` | once a day: the main-passed ceiling handed to `build-stable` |
| `.github/workflows/build-host-deploy-test-switch.yml` | the target shape, **switched off** (`.github/deploy-test-switch.json`), chained off `build-host.yml` (below): deploy `build-<version>`, test ON it (one probe pinned to the version per planned run — per shard of core's suite (stage `test-shard-<k>`), the doc gate (`doc-gate`), each dependent's gate — the shard count and stages read AT THE CANDIDATE commit, never platform main; claimed = deployed, a pinned probe refused 409 = the deploy failed, every run ENDED succeeded = the test), switch `latest` on green naming every run under `runs`. A 503 from `/hosts` is an outage, retried, never an empty list — events, the hosts reader (`complete`, never `ready`) and the probe in `.github/scripts/deploy-test-switch.py` |
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
names the platform commit and the CD run (`platform-set-dispatch.py discover`). The newest ARMED
set that still has no `mw-build-host:<version>` becomes the hand-over; otherwise the OLDEST retained
PROMOTED set that still lacks one does (a BACKFILL). Every set `mw-plugin-test` still carries a
version tag for is a candidate, not only the newest (the image job needs that tag, so a version only
the portal still carries is not one); a version the two repositories place on different commits is
skipped, and refused when it is the newest. When two sets arrive between two
ticks (A, then B), only B is ever the newest, and without the backfill A would never get a runner —
so a `build-stable` hand-over whose main-passed ceiling is A would fail on the missing
`mw-build-host:A`. The bounds: ONE runner image per tick, and a window of the retained sets not older
than the oldest runner `mw-build-host` still carries (at most the 60 newest of them); a set older
than every runner predates the lane and is not rebuilt. When every runner is there, the tick builds
nothing and re-sends the newest armed set to `build-latest` (idempotent at the receiver). A set
therefore reaches `build-latest` within one tick of being armed, and nothing in the platform's
delivery waits on it.

`build-host-stable.yml` does not rely on the backfill alone: before every hand-over it dispatches
`build-host.yml` for exactly its main-passed ceiling — which builds `mw-build-host:<ceiling>` when it
is missing (a ceiling outside the window, or one the backfill has not reached yet) and keeps the
existing digest when it is not — and waits (up to 35 minutes) for that run's image
job to SUCCEED — the job that also mirrors the runner to the fleet registry, digest verified — before
it hands the ceiling to `build-stable`, instead of failing on a missing runner digest.

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

**Deploy → test → switch is chained off this lane, inside this repository.**
`build-host-deploy-test-switch.yml` runs `on: workflow_run` of `build-host.yml` (`types: [completed]`);
it does not listen for `platform-set-sealed`, which nothing sends. Its `select` job starts a candidate
only when every one of these holds:

- the completed build-host run read a set (on `schedule` or a dispatch, never on a pull request or a push) and succeeded;
- the candidate is the newest ARMED set, and its `mw-build-host:<version>` exists;
- `GET /api/builds/hosts` does not list it, so it is not already `v/<version>` and not latest;
- it is newer than latest;
- no earlier automatic run attempted it.

`apply` records each automatic attempt as the artifact `deploy-test-switch-attempt-<version>` before
it sends anything. A candidate that failed is torn down and unlisted again, and that record keeps it
from being redeployed on every tick. A person re-runs it by `workflow_dispatch`, or a newer armed set
supersedes it. The candidate's core and MeshWeaver.Plugins commits come from the platform CD run's
`promotion-record`, the run its staging tag names. The rules are in
`.github/scripts/deploy-test-switch-trigger.py`, whose self-test includes the negative control: an
already-deployed set does not start. With `enabled: false` the lane prints that it is off and starts
nothing.

## Deployment material: what moved here, what is still in the platform

The platform keeps BUILD + SEAL; deployment lives here (core slimming). The inventory, with what
is moved, what is in flight, and what is still owed, is kept in ONE place — the platform's
[Carving Projects Out of Core → Deployment material](https://github.com/Systemorph/MeshWeaver/blob/main/src/MeshWeaver.Documentation/Data/Architecture/CarvingProjectsOutOfCore.md).
The hosting operator's image (`hosting-operator`) is NOT here: its scripts are versioned with the
`Hosting/InstanceAction` plan that names them, so it is built by MeshWeaver.Plugins'
`hosting-operator.yml` — the ONE pipeline that publishes `hosting-operator:main` — which bakes this
repository's charts and manifests in.

## memex-local on a Mac: two channels

`deploy/homebrew/` is the **developer channel** (`brew install memex-local` from the tap
`Systemorph/homebrew-memex`). `deploy/macos/` is the **professional distribution**: a Developer ID signed
and notarized `.pkg` that installs the same CLI under `/opt/memex` with a pinned toolchain used only where
the Mac has none that meets the floor, the optional unattended-host LaunchDaemon, and the managed
preferences an MDM pre-fills — design of record and manual in
[`deploy/macos/README.md`](deploy/macos/README.md).

## Developing

```bash
deploy/aks/scripts/check-chart-invariants.sh     # render every values combination, assert it is coherent
deploy/aks/scripts/check-values-are-read.sh      # every key a values file sets reaches a template
/bin/bash deploy/homebrew/test/run-tests.sh      # memex-local: the CLI, the toolchain resolver, the manifest↔formula drift
/bin/bash deploy/macos/test/run-tests.sh         # the macOS installer (.pkg): payload, plists, probe-first postinstall, signing branches (macOS only)
deploy/macos/build-pkg.sh --version 0.3.1 --arch arm64 --out dist --vendor none --sign never   # an unsigned installer from this tree
dotnet test test/MeshWeaver.Deploy.ChartGuards -p:MeshWeaverRoot=../MeshWeaver
python3 .github/scripts/build-run.py --self-test                 # the build host's runner
python3 .github/scripts/platform-set-dispatch.py --self-test     # the platform's hand-over contract
python3 .github/scripts/deploy-test-switch-trigger.py --self-test # when deploy → test → switch starts, for which set
docker build -f deploy/build-host/Dockerfile --build-arg DEPLOY_COMMIT=$(git rev-parse origin/main) --build-arg PLATFORM_COMMIT=$(git -C ../MeshWeaver rev-parse origin/main) deploy/build-host
```

Working rules for agents and people: [AGENTS.md](AGENTS.md).

Licensed under Apache-2.0 or MIT, at your option — see [LICENSE](LICENSE).
