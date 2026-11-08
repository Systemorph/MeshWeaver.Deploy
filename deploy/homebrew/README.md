# memex-local — Homebrew installer for local memex on Colima k3s (Mac)

A `brew`-installable CLI that stands up the **prod-like memex stack on Colima k3s**,
**1:1 with [`LocalColimaMac.md`](../../src/MeshWeaver.Documentation/Data/Architecture/LocalColimaMac.md)**
— same defaults, same topology, no substitutions. It *orchestrates*
`colima` / `helm` / `kubectl` / `mkcert` / `ollama`; it does **not** reimplement
the chart. The [`deploy/helm`](../helm) chart + values stay the single source of
truth.

> Why: the manual doc has ~14 steps across colima, image build, ingress, mkcert,
> Helm, Ollama, and a launchd port-forward. This packages all of it behind
> `memex-local up` (idempotent) and `memex-local down`.

## Install

```bash
brew tap systemorph/memex          # https://github.com/Systemorph/homebrew-memex
brew trust systemorph/memex        # Homebrew refuses a third-party tap's formulae until the tap is trusted
brew install memex-local
```

The tap is **published by this repository's own CI**: `.github/workflows/homebrew.yml`
installs and tests the formula from every PR that touches `deploy/homebrew/**` or
`deploy/helm/**` (on a macOS runner, under macOS's bash 3.2), and on every merge to
`main` attaches a tarball of exactly those two directories to a release of the tap and
commits the formula with its `sha256`. The version is `0.3.<main commit count>`, so
**`brew upgrade memex-local` follows main**. The formula declares the **exact brew
toolchain `LocalColimaMac.md` §1 installs** — `colima`, `kubernetes-cli` (kubectl),
`helm`, `mkcert`, `ollama`, `socket_vmnet`. It vendors a snapshot of `deploy/helm` so the
install works standalone.

The **Azure CLI** (for the registry-mode image pull's `az acr login`) is *not* a dependency
either: Homebrew moved `azure-cli` from homebrew/core to a cask, a formula cannot depend on
a cask, and `depends_on "azure-cli"` made every install fail with *No available formula
with the name "azure-cli"*. Install it separately (`brew install --cask azure-cli`);
`memex-local` refuses at runtime, naming that command, when `az` is missing.

The **.NET SDK** (10.0) is *not* a dependency: only the local-build image path (Option B,
§3) needs it, `depends_on cask:` is rejected by current Homebrew, and the registry-mode
default pulls the CI-built image instead. Install it separately for Option B
(`brew install --cask dotnet-sdk` or the standalone installer).

**Developing memex-local itself?** Run it straight from the checkout — it resolves the
chart + share assets relative to the script — or install a local tap over the template
formula (`brew tap-new systemorph/memex-dev`, copy `Formula/memex-local.rb` in, `brew
install --HEAD systemorph/memex-dev/memex-local`). A brew install's wrapper pins
`MEMEX_CHART_DIR` to the vendored chart; run-from-checkout uses the live `deploy/helm`
(or `MEMEX_REPO`/`MEMEX_CHART_DIR`) directly.

**Not a developer?** The professional channel is the signed, notarized **macOS installer** built from
[`deploy/macos`](../macos/README.md): the same CLI under `/opt/memex`, with pinned copies of Colima, Lima,
kubectl, helm and mkcert used **only where this Mac has none that meets the floor** — an existing Homebrew
installation is respected, never overwritten, and nothing is written into Homebrew's prefix. Both channels
read ONE toolchain manifest, [`share/tools.manifest`](share/tools.manifest), through
[`share/tools.sh`](share/tools.sh); `memex-local doctor` prints, per tool, which copy is in use, from where,
at which version, and whether it met the floor.

## Two modes — where the plugins come from

| Mode | `memex-local registry status` says | Plugins | Module binaries (Radzen, Analysis, EntityViews, GoogleMaps, Speech, Mcp…) | Image |
|---|---|---|---|---|
| **Registry** (§17, recommended) | `REGISTRY MODE — consumes https://memex.meshweaver.cloud` | installed from the cloud registry, per the instance's **grant** | **land from the registry's bundles** into `/data` | CI-built multi-arch from ACR (`az login`) |
| **Self-registry** (§16) | `SELF-REGISTRY MODE` | served from a `MeshWeaver.Plugins` **checkout** beside `MEMEX_REPO` | 🚨 **never** — a checkout has source, not assemblies (MeshWeaver#2417); the five required ones are blanked | built from source (Option B) |

Registry mode is one command, and needs **no key by default** — an un-keyed registration is
an *open* one, which the default public registry enrols into its default plan, the **free tier**:

```bash
memex-local registry https://memex.meshweaver.cloud [--id my-mac]      # free tier, no key
memex-local registry https://memex.meshweaver.cloud --key mwr_…         # a plan an admin minted a key for
memex-local up        # or `update` on an existing install
```

`~/.memex-local/registry.yaml` (0600) then carries `pluginCatalog.registryUrl` /
`instanceId` — and the bootstrap key, when one was given; `helm_deploy` layers it last. On
first boot the portal registers itself (presenting the key **once** if it has one), is issued
its own instance key (stored encrypted in its database, so it survives every `update`),
installs everything it is granted and lands the modules; `verify --repair` performs the one
activation restart. What it is granted is the **registry's** decision — a plan-scoped grant,
raised by a platform admin there (Instance grants ▸ Plan), never something the install can
ask for — see
[`PluginRegistry.md`](../../src/MeshWeaver.Documentation/Data/Architecture/PluginRegistry.md)
→ *Plans*. `memex-local registry off` returns to serving a checkout.

**What installs at first boot in registry mode is the first-run wizard's decision, not the
chart's.** `helm_deploy` clears the chart's `pluginCatalog.installByDefault` (`["Plugins/*"]`)
in this mode: the registry source is named by its URL, so that pattern named a source the
install does not have (`[DefaultInstall] 1 InstallByDefault pattern(s) name a source this
installation does not have`) while overriding the packages the person ticked in the wizard.
The instance manifest the wizard writes carries the selection instead. Self-registry mode keeps
its per-source `<name>/*` entries.

**A registry-mode install updates its platform from the public GHCR, with no credential**
(MeshWeaver.Deploy#65). `helm_deploy` layers the tracked
[`share/values.local.registry-mode.yaml`](share/values.local.registry-mode.yaml) in this mode,
before the registry file and the overlay (so either can still override it):

| Chart value | Value | Why |
|---|---|---|
| `selfUpdate.registry` | `ghcr.io` | the registry the install already pulls `ghcr.io/systemorph/memex-portal-ai` from; the chart derives the repositories `systemorph/memex-portal-ai` / `systemorph/memex-migration` from the images it runs |
| `selfUpdate.registryIsPublic` | `true` | the portal lists tags through the anonymous token flow and never sends a credential to GHCR; no `portal.imagePullSecret` is needed |
| `selfUpdate.canPatch` | `true` | a standalone local cluster has no control instance to hand a release to, so the portal patches its own deployments |

Before this, the portal kept the image's default, the upstream ACR listed through a managed
identity. A Mac has none, so every check failed with `ManagedIdentityCredential authentication
failed` and the install never rolled. The `mwr_` registration key cannot replace GHCR, because the
fleet registry checks an `mwi_` instance key that is minted inside the pod. The listing needs a
platform that carries `SelfUpdate:RegistryIsPublic`. An older image ignores the key and refuses the
GHCR listing by name, sending nothing; move it once with `memex-local update`, and it updates
itself after that. What rolls is still `Admin/UpdatePolicy` (Settings → Updates). Full account:
`Doc/Architecture/LocalColimaMac` → *Registry mode updates from public GHCR*.

## Use

```bash
memex-local registry <url> [--key mwr_…]  # REGISTRY MODE: consume a remote plugin registry (§17); no key = free tier
memex-local registry status|off          # which mode this install is in / back to self-registry
memex-local up                 # full stack, idempotent (registry mode: ACR image; else local build)
memex-local up --from-acr      # pull the multi-arch image from ACR explicitly (Option A)
memex-local status             # colima / pods / ingress / port-forward / health
memex-local logs               # tail the portal logs (verbose; see "Full logging")
memex-local logs --migration   # tail the migration job
memex-local update             # roll portal+migration to a newer image, refresh forward
memex-local verify             # is the portal USABLE? serves + /login routed + view packs present
memex-local autoroll up        # AUTO-redeploy portal+migration whenever a fresh image is built
memex-local autoroll status    # show the watcher + last-rolled portal/migration digests
memex-local autoroll down      # stop auto-rolling
memex-local observability      # Grafana/Loki/Promtail/Prometheus (§11)
memex-local doctor             # preflight: tools, chart/assets, cluster
memex-local instance up        # ramp a NEW portal that self-registers + installs plugins (§16)
memex-local instance status    # what it registered / installed
memex-local instance down      # remove it and RELEASE its instance id on the registry
memex-local down               # uninstall release (KEEPS the Postgres PVC)
memex-local down --purge       # also delete namespace + PVC (data loss) + ingress-nginx
```

> 🚨 **`memex-local` uses your ambient `kubectl` context.** With a cloud context selected, `up` /
> `update` deploy *there*, and the port-forward breaks. Run `kubectl config use-context colima`
> first — see LocalColimaMac §14.

Open **https://memex.localhost:8443** once `up` finishes.

The **default image path is Option B** (build native arm64 straight into Colima's
Docker store — the *no-cloud-creds* path the doc leads with for un-pushed source),
which needs the MeshWeaver source: set `MEMEX_REPO` to your checkout, or run from
a checkout. **Option A** (`--from-acr`) pulls the now-multi-arch image from
`meshweaver.azurecr.io` (run `az acr login -n meshweaver` first).

### First run — the wizard, the token, the key

A portal with no database serves the **first-run wizard** and nothing else. `memex-local up` (and
`memex-local update` onto an instance that is not set up yet) ends by handing you straight into it:
one line saying so, then the wizard's URL **with its one-time setup token already in it**, opened
in your browser. If you ever reach the page without the token, `memex-local setup` re-prints and
re-opens that link — the page itself says the same, because the chart's tracked defaults set
`InstanceSetup__TokenCommand: "memex-local setup"`.

In the wizard the **Registration key is optional**: leave it empty and the instance registers on
the free plan; a key is a pre-minted coupon a registry administrator hands a customer for a paid
plan, and the administrator can raise the plan later without one. For the database, "PostgreSQL —
the database this deployment already runs" is offered pre-selected (the connection string is never
shown); SQLite stays one click away, and pasting a connection string is only for a server of your
own.

After the wizard the portal restarts and installs the packages you chose. While it does,
`memex-local verify` exits **2** and `up`/`update` print one calm line — *"still installing its
packages — this finishes by itself"*. That is a state, not an error; only exit 1 (a red with a
remedy) means something is broken.

### First-run secrets & OAuth

On first `up`, the CLI generates `~/.memex-local/values.local.yaml` from the
template in [`share/values.local.yaml`](share/values.local.yaml), substituting a
freshly generated Postgres password and AI master key. **Edit that file** to fill
in your Microsoft Entra `ClientId` / `TenantId` / `ClientSecret` (`LocalColimaMac.md`
§9), or uncomment `Authentication__EnableDevLogin` for a no-Azure login. Re-run
`memex-local up` to apply.

## Mac hosts: profiles and release channels

A Mac Studio or Mac mini that hosts an instance runs the **Mac edition**: the same `linux/arm64`
platform image in Colima's VM, sized for the host
([Release Editions](../../src/MeshWeaver.Documentation/Data/Architecture/ReleaseEditions.md)).

**Profiles** (`MEMEX_PROFILE`) set defaults only. An explicit `MEMEX_COLIMA_*` or
`MEMEX_OLLAMA_MODEL` still wins, and an unknown profile is refused.

| Profile | VM | Host-native model | Host memory |
|---|---|---|---|
| `mac-studio` | 12 CPU / 32 GiB | `qwen3.8:27b-mlx` (~18 GB) | 64 GB or more |
| `mac-mini` | 6 CPU / 12 GiB | `qwen3:8b` (~5 GB) | 24 GB or more |
| (unset) | 8 CPU / 16 GiB | `qwen3.8:27b-mlx` on Apple Silicon with ≥ 32 GB left beside the VM (a 48 GB Mac), else `qwen3:8b` | the developer-laptop defaults |

A Mac Studio's chat model is `qwen3.8:27b-mlx` itself. Any further model to offer is listed in `MEMEX_OLLAMA_EXTRA_MODELS`
(`none` pulls none). Pulling a model does not offer it. A Mac serves the fleet only when its owner opts in.

**The owner's opt-in** (MeshWeaver.Plugins `Hosting/BackLease` §13). An enrolled Mac takes NO fleet jobs and serves
NO token requests until its owner says yes and names the instances it takes each from. `fleet join` and
`backlease enroll` ask both questions. The defaults are offered, not applied: jobs from `control.systemorph.com`,
tokens from `*.meshweaver.cloud`, and the models `qwen3.8:27b-mlx qwen3.6`. The residency has no default; type
where the machine is. A headless run passes `--accept-jobs=<patterns>` / `--no-accept-jobs` and
`--accept-tokens=<patterns>` / `--no-accept-tokens` (with `--token-models`, `--residency`), and with no answer
both stay off. **Token slots.** How many requests the machine serves at once is the machine's capacity, not an owner choice.
A Mac Studio is 2. It is measured: Ollama 0.40's MLX runner decodes one request at a time, and one stream saturates
an M5 Max at about 65 tok/s. memex-local writes `Tokens__Slots` whenever tokens are on. `--token-slots <n>` is a
calibrated override (1–32), and `--token-max-concurrency <n>` is the owner's optional ceiling below it. When the
ceiling is unset the Mac serves at its full slot count. `--token-yield-at <pct>` sets the owner-CPU threshold
(percent of one core; the code's default is 200) and is written only when given. `none` clears either. The host
sampler reports `host.ownerCpuPercent`: the owner's CPU over the interval, excluding Ollama, the build VM and the
portal VM. That is what the Mac yields to.

Change an answer later with `memex-local backlease accept jobs|tokens [--from …] [--off] …`,
then `memex-local update`. `backlease status` prints the answers. They are written as
`Hosting__BackLease__Accept__*` into `~/.memex-local/backlease.yaml`. The heartbeat carries them to control,
which enforces them and can never switch them on.

**Channels** (`--channel <label>` on `up`/`update`, or `MEMEX_CHANNEL`) follow a declared
release-channel label instead of `main`. `main` is the promoted set before it is armed. `3-latest` is
the newest armed set, and `3-mac` takes over once its writer ships. The labels are declared in core's
`.github/release-channels.json`. A label is only a **seed**. It is resolved to the version its image
reports (`MESHWEAVER_PLATFORM_VERSION`), the version tag is checked to name the same bytes, and that
version is what gets deployed. The result is recorded in `~/.memex-local/channel.state`. `autoroll up
--acr` follows the label too: the launchd job carries `MEMEX_CHANNEL`.

```bash
MEMEX_PROFILE=mac-studio memex-local profile        # what this host would size and pull
memex-local channel 3-latest                        # the version a label names right now
MEMEX_PROFILE=mac-mini memex-local up --channel 3-latest
```

This covers gaps G1 and G4 of the Mac host gap analysis. A headless host still needs the
remaining gaps closed: a pull credential that is not a person's `az login`, Colima at boot, and a
reachable hostname.

## Join the fleet — one command (`memex-local fleet join`)

A new Mac Studio joins the fleet with ONE command and ONE browser sign-in. The control instance does
the rest (design: MeshWeaver.Plugins `Hosting/MacStudioFleet.md` §2b):

```bash
memex-local fleet join --control https://<control-instance> --id studio-2
```

1. The command runs preflight and reads the macOS account (it never assumes one).
2. It makes a throw-away RSA-3072 key under `~/.memex-local/enrol/` (0600), prints the key's
   **fingerprint**, and opens the control instance's approval page `Hosting/Heartbeats/Enrol/<request>`.
3. A global administrator of the control instance checks that the page shows the same fingerprint and
   presses **Approve**. The page shows an **enrolment code**: the machine's heartbeat key and
   announcement key, sealed to this Mac's throw-away key (RSA-OAEP-SHA256). The code is ciphertext,
   so it is safe to copy.
4. Paste the code into the terminal. The command opens it with macOS's own `openssl`, deletes the
   throw-away key and writes `~/.memex-local/fleet.yaml` (0600). It never prints a key value, only the
   `sha256:` fingerprints, which the control instance's enrolment record shows too.
5. It runs `backlease enroll`, starts Ollama under launchd (`com.memex.local.ollama`, bound to every
   interface so the VM reaches it, restarted after a reboot), and runs `update`. In registry mode,
   `update` pulls the **public GHCR** images anonymously, so no `az login` is needed (`--from-acr`
   still works).

`memex-local fleet status` shows the join and both fingerprints.

**A fleet Mac's template carries the Hosting app.** In registry mode, a joined (`fleet join`) or
back-leased (`backlease enroll`) Mac layers `share/values.local.fleet-host.yaml`. That layer names
`Plugins/Hosting` in `PluginCatalog:InstallByDefault` and keeps the platform baseline with
`InstallPreInstalledPackages=true`. The Mac's first instance therefore installs Hosting by itself; the
heartbeat and the self-update patcher both ship in Hosting. `MEMEX_FLEET_HOST=0` opts out. Inside the
autoupdate agent, a registry-mode re-apply also pulls the public GHCR image (`MEMEX_AUTOUPDATE_IMAGE=0`
opts out). A template that needs a newer platform, such as a package's `minMeshVersion`, therefore lands
with no hand on the machine. What the registry serves the instance is still its plan's decision.
 On the control instance,
`Hosting/Heartbeats/Enrolments` lists every join with the first step not yet measured done.

## Auto-update of the CLI and its chart — on by default

A Homebrew install keeps **itself** current: `memex-local up` and `memex-local update` arm a
launchd agent (`com.memex.local.autoupdate`) on every Homebrew install, and every 6 hours it
runs `brew update` + `brew upgrade memex-local`. When a release changed the vendored chart or
the `share/` assets, the agent re-applies them with `memex-local update --skip-image`, which
keeps the running images and restarts the portal once. A release that changed only the script
restarts nothing. It also refreshes the auto-roll's tick copy, so that agent never keeps
running an old CLI.

```bash
memex-local autoupdate status   # RUNNING / OFF, and the last line of ~/.memex-local/autoupdate.log
memex-local autoupdate off      # stop it; remembered, so `up`/`update` do not re-arm it
memex-local autoupdate on       # resume
```

Why it is the default (2026-10-30): the portal image and the packages already update in
place, but registry mode's in-pod image self-updater is switched on by a values layer that
ships **with the CLI**. A CLI that is never upgraded therefore also leaves the image updater
in detect-only mode. A checkout (run-from-checkout) and a headless `.pkg` host are never armed.
When Homebrew refuses (an outdated Xcode, an untrusted tap), the log line carries Homebrew's
own message, and the agent retries on the next window.

## Auto-update (auto-roll) — the local equivalent of AKS self-update

On AKS the portal self-updates by polling ACR and patching its own Deployment
(`SelfUpdateHostedService`, per [`ReleaseStrategy.md`](../../src/MeshWeaver.Documentation/Data/Architecture/ReleaseStrategy.md)).
That in-pod poll **cannot work locally** — the local images are built straight into
Colima's Docker store, never pushed to a registry the pod can list (the in-pod
`AcrTagLister` would fail the Azure→ACR token exchange). So locally the same
*outcome* is driven from the **host** instead:

```bash
memex-local autoroll up        # arm it (once)
```

This installs a launchd agent (`com.memex.local.autoroll`) that, every 30s:

1. **Pins the portal Deployment to the moving tag** `…/memex-portal-ai:latest`
   (`imagePullPolicy: IfNotPresent` — k3s reuses the Docker-store image, no pull).
2. Watches the **portal** *and* **migration** `…-local:latest` image digests.
   When a build changes one, it first **retags** that fresh `…-local:latest` onto
   the chart ref the cluster actually runs (`…/memex-portal-ai:latest`,
   `…/memex-migration:latest`) — so even a bare `dotnet publish` (which only writes
   `…-local:latest`) goes live — then:
   - **migration first** — re-runs the migration as a one-shot Job against the
     main DB and **waits for it** (the portal's `DbVersionGate` blocks boot until
     the schema is current; if the migration fails it leaves everything on the
     current images and retries — it never rolls the portal onto a schema it
     can't run);
   - then **`rollout restart`** the portal so the rebuilt image goes live.

So after `autoroll up`, **any rebuild** — `memex-local update`, `memex-local up
--build`, or a bare `dotnet publish -t:PublishContainer` of the portal/migration
— auto-deploys within ~30s, with **no manual `kubectl set image`** (the retag in
step 2 is what makes the bare-`publish` case work — without it the roll would
re-run the old chart-ref image).

> **Do you need to install anything first?** No. `autoroll` uses **launchd**
> (built into macOS) plus `docker` + `kubectl`, which are already `memex-local`
> prerequisites (`LocalColimaMac.md` §1). Nothing new to install.

`autoroll status` shows whether the watcher is running and the last-rolled portal
and migration digests; `autoroll down` stops it (the portal stays on its current
image). The log is `~/.memex-local/autoroll.log`. Only the **portal** Deployment is
rolled in place; the **migration** is re-run as a Job (it has no Deployment).

## How each `LocalColimaMac.md` step maps into the CLI

| Doc section | Manual step | CLI implementation (function in `bin/memex-local`) |
|---|---|---|
| §1 Prerequisites | `brew install colima kubectl helm mkcert ollama socket_vmnet` + .NET SDK | Formula `depends_on`; `preflight()` re-checks at runtime |
| §2 Colima k3s | `colima start --kubernetes --cpu 8 --memory 16`; verify context/node | `start_colima()` |
| §3 Image (A) | pull multi-arch from ACR, verify `arm64` in manifest, retag | `image_pull_acr()` (`up --from-acr`) |
| §3 Image (B) | `dotnet publish -t:PublishContainer` portal+migration, `docker tag` to chart name | `image_build_local()` (default) |
| §4 Helm | `helm upgrade --install memex deploy/helm -f values.yaml -f overlay -n memex` | `helm_deploy()` + `ensure_overlay()` |
| §4 Postgres PVC | shipped by the chart (StatefulSet, 10Gi PVC, pgvector pg17) | unchanged — chart is source of truth |
| §5 ingress-nginx | `helm upgrade --install ingress-nginx … ClusterIP, no admission webhooks` | `install_ingress_nginx()` |
| §5 mkcert TLS | `mkcert -install` + mint cert + `kubectl create secret tls memex-portal-tls` | `setup_tls()` |
| §6 Hostname | `memex.localhost` (auto-resolves), port `8443` | constants `HOSTNAME_LOCAL` / `HOST_PORT` |
| §7 Durable access | `port-forward.sh` + `com.memex.local.plist`, `launchctl bootstrap` | `install_launchd()` (assets in `share/`) |
| §10 Local LLM | `brew services start ollama` (loopback 127.0.0.1:11434, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`; memex-local's own agent as the fallback; the VM reaches it through its gateway); `ollama pull qwen3.8:27b-mlx`; the portal calls `http://ollama.memex.svc.cluster.local:11434/v1` — a Host Ollama admits (the short name `ollama:11434` is answered 403) | `setup_ollama()` + `ensure_chat_model()` + tracked defaults (endpoint, `ollama.external`) + `~/.memex-local/llm.auto.yaml` (model, context window, `AI__Tiers__chat`, gateway IP) + `llm_chat_smoke()` (a real completion from inside the pod; `up`/`update` fail on a 403 or any non-200) |
| §11 Observability | `helm upgrade --install loki grafana/loki-stack -f share/values.observability.yaml` (sized for the VM; `fleet join` runs it) | `cmd_observability()` |
| §12 Instance identity | `Portal__InstanceName/Color` | overlay `config.memex_portal` |
| §14 Verify | assert the portal is USABLE — it serves, `/login` is routed, and the DefaultViews/GraphViews packs are present | `verify_usable()` (`up` / `update` / `verify`; behaviour-tested in `test/run-tests.sh`) |
| §14 Troubleshoot (stale forward) | restart the port-forward after a rollout | `cmd_port_forward()`; `update` auto-refreshes it |
| §16 Plugin registry | mount the plugin checkouts + configure them as sources/grants | `plugin_repo_paths()` + the `pluginCatalog.*` `--set`s in `helm_deploy()` |
| §16 New instance | mint an `mwr_` key → own DB → deploy a self-registering portal → verify | `cmd_instance()` (`inst_mint_key`, `inst_registered_id`, `inst_installed_packages`) |
| Self-update (ReleaseStrategy) | in-pod patch per `Admin/UpdatePolicy` (Continuous default) — can't see local-built images | `cmd_update()` = manual roll; `cmd_autoroll()` = host-side auto-roll on a new local build (the local stand-in for the in-pod poll; see "Auto-update" above) |

`down` reverses §7 (launchd) and §4 (Helm release) while **retaining the Postgres
PVC** so data survives — exactly the "survives reboot" property the doc emphasises.
`--purge` is the explicit data-wipe.

## Full logging (verbose) — and what is **not** touched

`LocalColimaMac.md` wants logs turned up locally. The chart's portal `ConfigMap`
passes a `config.memex_portal` key it does not name through verbatim, so the
overlay's level reaches the pod — but `memex-local` also honours `MEMEX_LOG_LEVEL`
at run time, without a re-render. So the verbose level is applied **two
complementary ways**:

1. **Declared in the overlay** (`config.memex_portal.Logging__LogLevel__Default:
   "Debug"`) — rendered into the ConfigMap by the chart's pass-through.
2. **Applied as a deployment-config override** by `apply_logging()` after every
   Helm upgrade: `kubectl set env deployment/memex-portal-deployment
   Logging__LogLevel__Default=$MEMEX_LOG_LEVEL`. A container `env` entry **wins
   over `envFrom`**, and it's idempotent (same value → no rollout).

Set `MEMEX_LOG_LEVEL=Trace` for maximum verbosity. Logs are one command away:
`memex-local logs` (portal), `--migration`, `--postgres`.

> 🚨 This installer **never edits any committed `src/**/appsettings*.json`** — per
> `AGENTS.md` those are production contract. Verbose logging is purely a runtime
> deployment-config override on your local cluster.

## Postgres (durable)

Postgres is the chart's in-cluster `StatefulSet` (`pgvector/pgvector:pg17`) on a
**10Gi PVC** via `volumeClaimTemplates` — unchanged. `down` keeps it; only
`down --purge` deletes it. The password is generated once into the overlay and
reused across the migration/portal secret groups (the chart builds the connection
strings from it).

## Files in this directory

| Path | What |
|---|---|
| `Formula/memex-local.rb` | Homebrew formula (deps + install + caveats + test) |
| `bin/memex-local` | the orchestration CLI (all subcommands) |
| `share/values.local.yaml` | local Helm overlay template (full logging + pg + ingress + ollama + instance id) |
| `share/port-forward.sh` | the §7 port-forward script (started by launchd) |
| `share/com.memex.local.plist` | the §7 launchd login-agent template |
| `share/tools.manifest` | the ONE toolchain declaration: per tool its formula, version floor, vendored pin and probe; per vendored tool the sha256-pinned release asset (read by the CLI, the `.pkg` build and its postinstall; drift against the formula's `depends_on` is a red test) |
| `share/tools.sh` | the resolver: existing installation first (floor met), the `.pkg`'s vendored copy second — `need`, `preflight`, `doctor` |

State lives in `~/.memex-local/` (overlay, mkcert cert/key, port-forward script +
log, ollama log, grafana password) — exactly the paths the doc uses.

## Environment overrides

| Var | Default | Purpose |
|---|---|---|
| `MEMEX_REPO` | auto-detected from checkout | repo root for chart + local build |
| `MEMEX_CHART_DIR` | `$MEMEX_REPO/deploy/helm` or vendored copy | explicit chart dir |
| `MEMEX_LOG_LEVEL` | `Debug` | verbose log level (`Trace` for max) |
| `MEMEX_COLIMA_CPU` / `MEMEX_COLIMA_MEM` | `8` / `16` (or the profile's) | VM sizing (§2) |
| `MEMEX_OLLAMA_MODEL` | `qwen3.8:27b-mlx` / `qwen3:8b` by host memory (or the profile's) | the default chat model `up`/`update` pull and the portal names; `none` skips it |
| `MEMEX_LLM_SMOKE` | `1` | `0` skips the post-install chat smoke (loudly) |
| `MEMEX_PROFILE` | unset | Mac host profile, `mac-studio` or `mac-mini` (below) |
| `MEMEX_CHANNEL` | unset (`main`) | release-channel label `up`/`update`/`autoroll` follow (below) |
| `MEMEX_PORT` | `8443` | host port for the forward (§6/§7) |
| `MEMEX_LOCAL_HOME` | `~/.memex-local` | state directory |

## Validation & what must be verified on a real Mac

Validated here (syntax / correct-by-construction):

- `ruby -c Formula/memex-local.rb` — formula parses.
- `bash -n bin/memex-local share/port-forward.sh` — scripts parse.
- `shellcheck` clean (if installed).
- Every `LocalColimaMac.md` manual step traces to a CLI function (table above).

Cannot be run in this environment — verify on a real Apple-Silicon Mac:

- `brew install` of the formula (needs Homebrew + network).
- `colima start --kubernetes` (needs virtualization + `socket_vmnet`).
- `mkcert -install` (writes the CA to the **System keychain** — interactive auth).
- `brew services start ollama` + `ollama pull qwen3.8:27b-mlx` (downloads the model; needs the Metal GPU
  for performance).
- The local-build image path (`dotnet publish -t:PublishContainer`) and/or the
  ACR pull (`az acr login`).
- The end-to-end `curl https://memex.localhost:8443/` returning `http=200`.

## Publishing a Mac on the private network

`memex-local publish on --sign-in microsoft --tenant <directory id> --client-id <id> < secret-file`
makes the portal also answer at the Mac's **own name on the private network** — the MagicDNS name
`tailscale status --json` reports as `.Self.DNSName` (e.g. `mac-1.<tailnet suffix>`), read by the
command and never typed. A member device on the network opens `https://<that name>` and reaches the Mac
directly: nothing public is involved and no bridge is needed. This is the default; `--tailnet-only` says
it explicitly. Without tailscale, or with tailscale not connected, there is no such name and a tailnet-only
publish refuses, saying why.

It writes `~/.memex-local/publish.yaml` (0600): the name as an additional ingress host (the portal's
links keep `memex.localhost`), the Microsoft sign-in (a NAMED directory only), and invitation-only
admission — the first person to sign in becomes the administrator, everyone after needs an invitation.
The client secret is read from STDIN, never an argument, and never printed. The portal treats the tailnet
name like any non-local host, so the real provider signs people in there; the development sign-in answers
on localhost names only, and never for a request the entry point marked (Google, Apple and LinkedIn are a
follow-up: the chart carries no client secret for them yet). `publish status` reads it back;
`publish off` removes it; `memex-local update` applies either.

While the layer exists the front door (`share/port-forward.sh`) binds **loopback only, on the
unprivileged port alone** (macOS allows an unprivileged low-port bind on wildcard addresses only), so
locally the portal is `https://memex.localhost:<port>` (default 8443). The private network gets its
OWN entry point (`share/network-entry.yaml`, a small in-cluster Caddy behind its own loopback forward on
8446): `tailscale serve --tcp 443 tcp://127.0.0.1:8446` is the one way in, and the proxy has one site per
published name, each marking EVERY request `X-MeshWeaver-Provenance: tailnet` (overwriting whatever the
sender claimed) — there is no unmarked site, and a name the Mac does not publish gets no answer. The
network policy must let no member reach 8443, 8444 (the e2e portal) or 8446 (a user-space network client
delivers to loopback-bound ports too): TCP 443 is the one way in.

### What a member device needs

1. **Join the private network** (MeshWeaver.Deploy `deploy/headscale/README.md`) with MagicDNS on, so the
   Mac's name resolves.
2. **Trust the entry point's CA.** The entry point serves `tls internal`: a certificate from its own
   local CA, which no browser and no WKWebView trusts until the device does. On the published Mac, after
   `memex-local update`:

   ```bash
   memex-local publish ca > memex-entry-ca.crt
   ```

   prints the root certificate (PEM). It lives on the entry point's data volume, so a pod restart or a
   re-publish keeps the same root and a device trusts it once; `publish off` keeps it too
   (`kubectl -n memex delete pvc memex-network-entry-data` forgets it). Install it on the device:
   - **macOS**: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain memex-entry-ca.crt`
     (or open it in Keychain Access and set *When using this certificate: Always Trust*).
   - **iOS / iPadOS**: AirDrop or mail the file, install the profile (Settings → *Profile Downloaded*),
     then turn it on under Settings → General → About → *Certificate Trust Settings*. Safari and the apps'
     WKWebView use that trust store.
   - **Windows / Linux**: import it as a trusted root (`certutil -addstore Root`, or
     `/usr/local/share/ca-certificates/` + `update-ca-certificates`), and in Firefox under its own
     certificate settings.
3. **Register the sign-in redirect URI for the tailnet name** on the provider's app registration —
   for Microsoft `https://<tailnet name>/signin-microsoft` (`publish on` prints the exact URI for every
   name). Without it the provider refuses the sign-in with a redirect-URI mismatch. The command changes
   no app registration itself.

### Also at a public name (optional)

`memex-local publish on --host <name> --sign-in microsoft …` ADDS a public name the private network's
ingress bridge (`deploy/tailnet-bridge/`) forwards to the same port 443 with `Host` = the public name. The
tailnet name is still published beside it when tailscale reports one (without one the public name is
published alone, and the command says so); each name gets its own marked site and needs its own redirect
URI. A public name is never the default.

