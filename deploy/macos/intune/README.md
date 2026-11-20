# Fleet bootstrap for the device-management layer (Intune)

`fleet-bootstrap.sh` brings a Mac Studio of the fleet to "joined". The device-management layer runs it
**as root** on the fleet's Mac Studio group (an Intune macOS shell script, run as the signed-in system
context, repeated on Intune's schedule). Every run is idempotent: it does only what is missing, prints
one line per step, and appends the same lines to `/Library/Logs/MeshWeaver/fleet-bootstrap.log`. Design:
MeshWeaver.Plugins `Hosting/MacStudioFleet.md` §2b and §2b.2.

| Step | What it does | Waits (exit 0) when |
|---|---|---|
| console user | the person the Mac's memex runs as (`stat -f%Su /dev/console`). If that person's password must be changed (an expired password, or a device-management "change at next sign-in" policy whose moment has passed — read from `pwpolicy -getaccountpolicies` and `dscl … accountPolicyData`), the run's FIRST line says so with the remedy: every non-interactive SSH login fails until the person changes it at the console | no one is logged in |
| operator SSH | only when `OperatorSshKeys` is configured (below): Remote Login on (`launchctl enable system/com.openssh.sshd` + `bootstrap` of `/System/Library/LaunchDaemons/ssh.plist`, which needs no privacy grant; `systemsetup -setremotelogin on` — which needs Full Disk Access — only as the fallback), the console user added to `com.apple.access_ssh` when Remote Login is restricted to that group, and each declared key appended to the console user's `~/.ssh/authorized_keys` AS that user (`~/.ssh` 0700, the file 0600; only when absent — matched on type + key, any comment or options; no line removed; a symlinked or foreign-owned file refused). `sshd_config` is never touched. Not configured: nothing changes, and the run says so | — **fails** (exit 1, after the rest of the run) when an entry is refused (not one single-line `ssh-ed25519` / `ecdsa-sha2-*` / `ssh-rsa` public key without options — its content is never logged; the valid ones are still installed) or Remote Login cannot be turned on |
| energy enabler | until Intune's Energy Saver profile (`com.apple.MCX`, `com.apple.EnergySaver.desktop.ACPower`) is in `/Library/Managed Preferences/com.apple.MCX.plist`: ONLY `pmset -c sleep 0` when AC system sleep is not already 0. A Mac on macOS's default `sleep 1` cannot RECEIVE the profile — measured on two fleet Macs (2026-11-12), every device command arrived during PowerNap and mdmclient answered *"NotNow … reason: Not supported during DarkWake"*. Once the profile is installed it decides and the bootstrap sets nothing. Reading `energy`: `ok` / `waiting` / `enabler` / `failed` (1.8.0) | — **fails** (exit 1, after the rest of the run) when `pmset` refuses |
| bootstrap token | `profiles status -type bootstraptoken`, every run. Escrowed: said. NOT escrowed: said in the log, in the reading, and in a notification to the console user (`launchctl asuser` + `osascript`, the text as an argument): on the first such run *sign out and back in to finish fleet setup* (a supervised Mac may escrow at a secure-token user's next sign-in — not yet established on the fleet's user-enrolled Macs), on every later run still un-escrowed `sudo profiles install -type bootstraptoken`. Escrow takes the person's admin password, so the bootstrap never does it and never asks for, reads or handles a password. Without it Intune's macOS updates need an admin password at the Mac (owner, 2026-11-12: "i'll forget for sure") | — never fails the run |
| participation | the owner's two answers, `AcceptJobs` and `AcceptTokens` (below), read BEFORE anything is installed | — **fails** (exit 1, the missing key named) when either is absent or not a boolean, or a YES lacks its `…From` (tokens: also `AcceptTokensModels`) |
| Homebrew | `/opt/homebrew`, owned by that person (the documented untar install, no prompt) | — |
| tools | `colima docker kubectl helm ollama lego` (lego: `memex-local cert issue`, the internal name's certificate) and `systemorph/memex/memex-local` (the tap tapped AND trusted first), as that person. Every command run as that person gets `/opt/homebrew/bin` and `/usr/local/bin` on PATH | — |
| shell | `eval "$(/opt/homebrew/bin/brew shellenv)"` appended ONCE to the person's `~/.zprofile` (left alone when it already loads Homebrew), so their own `memex-local` finds `kubectl` | — |
| tailscale binaries | `tailscale` + `tailscaled` fetched BY ROOT from Homebrew's bottle (URL and sha256 from the formula API, verified) into the root-owned `/Library/MeshWeaver/tailscale` — what runs as root never comes from the user-owned Homebrew tree | — |
| tailscaled | a ROOT LaunchDaemon `com.meshweaver.fleet.tailscaled` on `/var/run/tailscaled.socket` (the Tailscale app does not run before login; this does). memex-local uses this socket by itself | — |
| network | `tailscale up --auth-key=file:<FLEET_PREAUTH_KEY_FILE>` (by file reference, never an argument; deleted only after the join succeeded) | no key yet (control mints it at enrolment) |
| join poller | once the join is FILED (1.9.0): the approval page is opened in the console user's browser and a notification posted (once per request), and a root LaunchDaemon `com.meshweaver.fleet.join-poller` runs a root-owned copy of this script (`/Library/MeshWeaver/bin/fleet-bootstrap.sh poll-join`) every 30 s: ONE poll of control (`fleet join --wait 0`). The Approve collected → the full run continues at once (network, setup, finalisation), then the poller removes itself; refused, or 24 h without an Approve → it removes itself (the hourly run keeps polling). Before 1.9.0 an Approve waited for the next hourly run | — |
| local CA | a FLEET Mac does not wait: when the fleet local CA (`FleetCaSha256`) is in the System keychain and trusted (its trusted-root profile), `up` runs with `MEMEX_CA_TRUST=defer` until its key arrives with the enrolment code (`~/.memex-local/enrol/fleet-ca.key`), and then `memex-local ca adopt` makes it mkcert's CA and re-issues the portal's certificate | no fleet CA and the mkcert CA (`<CAROOT>/rootCA.pem`, a PUBLIC certificate) is not yet trusted: a person runs `mkcert -install` once (macOS allows the trust change only with a person at the UI, even as root) |
| memex | registry mode (once — never re-written); the local CA created without the keychain prompt; `memex-local up` until `memex-local verify` passes, `memex-local fleet join --wait <JoinWaitMinutes, default 10>` (it POLLS control for its own code, signed with its throw-away key, and resumes next run; a code dropped at `~/.memex-local/enrol/<id>.code` is the fallback; the code's pre-auth key then joins the network at once) with the owner's answers (`--accept-jobs=…` or `--no-accept-jobs --no-buildhost`, `--accept-tokens=… --token-models … [--residency …]` or `--no-accept-tokens`), and a joined Mac's unfinished back-lease enrolment resumed from its stored credentials | no `ControlUrl` yet, or the approval is pending (the link is in the log and in `~/.memex-local/enrol/<id>.link`) |

**Configuration** comes from the managed-preference domain `com.memex.local`, deployed as a configuration
profile. Its keys are `ControlUrl`, `DeploymentId`, `RegistryUrl`, `LoginServer`, and the machine's OWNER,
`OwnerId` + `OwnerEmail`. The owner is required: it becomes the instance's global administrator, and
`memex-local up` refuses to run unattended without one. For a user-affinity device, set them to `{{partialupn}}`
(when MeshWeaver user ids are UPN prefixes) and `{{mail}}`. The environment
variables `FLEET_CONTROL_URL`, `FLEET_MACHINE_ID`, `FLEET_REGISTRY_URL` and `FLEET_LOGIN_SERVER` override
them. Nothing secret is configured.

`FleetCaSha256` names the fleet local CA's certificate (64 hex, its SHA-256): the certificate itself is a
trusted-root profile, minted once on the owner's go with `deploy/macos/fleet-ca/mint-fleet-ca.sh` (EC P-256,
critical Name Constraints: `DNS:localhost` — which covers every name below it — `127.0.0.1`, `::1`; the key goes
straight into a vault object and reaches each Mac sealed in its enrolment code, item `a`). Sharing one key is
tolerable only because macOS ENFORCES those constraints on the root. Measured on macOS 27 (2026-11-09) with Apple's
evaluator (`security verify-cert -c <leaf> -r <fleet CA> -p ssl -n <name>`): a leaf for `memex.localhost` signed by
the CA verifies; a leaf for `example.com` signed by the same CA is refused, *"Certificate violates name constraints
placed on issuing CA"*. Re-measure on the first profile-trusted fleet Mac (Safari and Chrome) before relying on it.

**Participation — required, never defaulted** (the owner's answers, Mac onboarding contract §5):

| Key | Type | Meaning |
|---|---|---|
| `AcceptJobs` | boolean, **required** | the Mac takes the fleet's build-queue jobs. NO ⇒ no build host at all (no build VM, runners or priority agent; `fleet join --no-accept-jobs --no-buildhost`) |
| `AcceptJobs<Class>` | boolean, optional (1.14.0) | the jobs answer for ONE model class — `<Class>` is the Mac's model name without spaces (`AcceptJobsMacStudio`, `AcceptJobsMacMini`), the same resolution as `GpuWiredReserveMb<Class>`: the environment `FLEET_ACCEPT_JOBS`, else the class key, else the class-less `AcceptJobs`; the `participation` log line names the key that decided. **Kept on every run**, not only at the join: once a Mac is joined and finished, a NO while it still accepts jobs (its back-lease layer), runs its build VM (Colima profile `build`) or has a build-host agent (`com.memex.local.buildhost-follow.build`, `-idle`, `-priority`) takes it out — as the person, each step only when needed and logged: `memex-local backlease accept jobs --off`, the follow agent booted out and its plist removed, `memex-local buildhost priority off`, `colima stop -p build`, `memex-local update` only when the recorded answer changed (an update that fails stays owed in `fleet-state/jobs.update-pending` and is retried), and LAST `memex-local buildhost idle off` — the idle governor starts the build VM for CI and unloads models to do it, and a memex-local older than this change re-installs it on `accept`/`update` (memex-local now installs none on a jobs-NO Mac; its idle-only models are then not served — offer them in `AcceptTokensModels` to serve them outright). Only the build VM is stopped: never the portal VM, Ollama, a model server or any other process. A YES on a Mac that is out is LOGGED with the commands that opt in (`memex-local backlease accept jobs --from …`, then a fleet join — one Approve on control — and `memex-local buildhost attach`) and never re-attached by the run: a build host needs the items a join issues. Reading `jobs`: `in` / `out` / `taken-out` / `differs` / `failed`. The fleet declares `AcceptJobsMacStudio` = false (owner, 2026-11-19: the Mac Studios are a model cluster); the minis keep `AcceptJobs` |
| `AcceptJobsFrom` | list (array or comma string), required on YES | the instances whose jobs it takes (`control.example.com`, `*.example.cloud`, `*`) |
| `AcceptJobsChannels`, `AcceptJobsRepos` | list, optional | narrow the jobs to these release channels / `owner/repo`s |
| `AcceptTokens` | boolean, **required** | the Mac serves local models to the fleet's token queue. NO ⇒ no token models beyond the portal's own chat model |
| `AcceptTokensFrom`, `AcceptTokensModels` | list, both required on YES | whose token requests, which models |
| `AcceptTokensResidency` | string, optional | where the Mac is (`CH`, `Eu`) — stamped on what it serves |
| `BuildVmCpus<Class>`, `BuildMaxRunners<Class>` | integer, optional | the FLEET DEFAULT size of the build host for one model class — `<Class>` is the Mac's model name without spaces (`BuildVmCpusMacMini` = 8, `BuildMaxRunnersMacMini` = 2 for the Mac minis). A class with no key (the Mac Studios) keeps the automatic sizing; the class-less `BuildVmCpus` / `BuildMaxRunners` apply to every class. The Mac owner's own setting (`memex-local buildhost set vm-cpus …`) wins over both; `memex-local buildhost settings` names where each value came from |
| `GpuWiredReserveMb<Class>`, `GpuWiredReserveMb` | integer MiB ≥ 4096, optional | the GPU wired-memory limit (1.13.0): the bootstrap sets `iogpu.wired_limit_mb` = RAM − this reserve, as root, now and at every boot (LaunchDaemon `com.meshweaver.fleet.gpu-wired-limit`, RunAtLoad), so a model larger than macOS's default share (about 3/4 of the RAM) loads with no `sudo` at the Mac. A RESERVE, not a limit, so one key is right on every RAM size of a class. The class key wins over the class-less one, as for the build-host size. Refused (reading `gpu-wired-limit` = `refused`, nothing set): not a whole number, under 4096, or a limit not above macOS's default. A live value ABOVE the declared limit (raised by hand) is not lowered under a running model: the reading says `deferred` and the daemon applies the declared value at the next boot. Absent: nothing is set, and a daemon an earlier declaration installed is removed (the live value stays until the next reboot). The fleet declares `GpuWiredReserveMbMacStudio` = 8192 (128 GB Studio → 122880 MiB); the 64 GB minis keep macOS's default — they run the build VM beside their models |

**Readings** — the run log is root-only, so the bootstrap also leaves one world-readable line per reading in
`/Library/MeshWeaver/fleet-state/` (root-owned, 0644, `<verdict> <UTC time> <detail>`, nothing secret):
`operator-ssh` (`ok` / `not-configured` / `failed`), `gpu-wired-limit` (`ok` / `deferred` / `not-configured` / `unsupported` / `refused` / `failed`, 1.13.0), `jobs` (`in` / `out` / `taken-out` / `differs` / `failed`, 1.14.0) and `bootstrap-token` (`escrowed` / `not-escrowed signin|command
since …` / `unsupported` / `unknown`). Since 1.7.0 EVERY step leaves one: `participation`, `homebrew`, `devtools`,
`tools`, `tailscale`, `tailscaled`, `network`, `up`, `setup`, `join` (`waiting-approval` with the approval link /
`failed` with control's refusal or the last error line / `skipped` naming the missing prerequisite / `ok`), and once
joined `doctor`, `heartbeat`, `buildhost` (from `memex-local fleet doctor`, run as the person). **`stage`** names the step
the onboarding is stuck on — the first failed one, else the first still waiting — with when and why, or `done`. A join
that is not filed or not finished for any reason but the Approve (which is control's to give) also reaches the person as
a notification naming it. `memex-local fleet doctor` reads them: on a Mac not joined yet it prints the `join` and
`stage` readings.

**A fleet Mac joins BEFORE its first-run setup** (1.7.0). The setup refuses an instance with no sign-in route —
right for every other install — but a fleet Mac's sign-in (its own Entra app, `internal on`) is delivered BY the
enrolment. Measured on the first Mac mini (2026-11-12): every run answered the setup, was refused (*"signIn: Choose at
least one way to sign in"*), and the join was never filed. So when the portal is still in first-run setup and the Mac
has not joined, the bootstrap runs `fleet join --no-update --heartbeat-wait 0` first; the Approve's items install the
sign-in as host configuration (which the setup accepts — `configuredElsewhere`), and a later run answers the setup and
finishes (back-lease enrolment + update). The earlier Macs passed this step because a person answered the browser
wizard before the join. Since 1.10.0 a pre-setup join that is collected goes straight on in the SAME run: the setup
is answered (the sign-in is installed by then), the bootstrap waits up to `FLEET_SETUP_WAIT_S` (600 s) for the portal to
restart configured, and the finalisation (back-lease enrolment + update) follows — on the first Mac mini that was an
hour's wait for nothing. **The sign-in reaches the Mac even when the join comes first** (1.11.0): a bootstrap-run join has no private-network name
yet (the root daemon takes the key by file only after the join), so memex-local (FLEET_SIGNIN_HANDOVER) hands the
sign-in over by file (`~/.memex-local/enrol/<id>.signin`, 0600, with `<id>.signin-facts`), and the bootstrap installs it
with `memex-local internal on` once the network is up, before the setup (reading `signin`). A memex-local that predates
the hand-over is upgraded from the tap. A Mac that JOINED but whose sign-in never landed (no `internal.yaml`, no
hand-over, the portal still in setup) sets the join aside and files a FRESH join by itself — the first Mac mini lost its
sign-in this way (2026-11-12). Every step rewrites its own reading whoever runs it (the network step's reading had stayed at a
stale WAITING after the join's own call brought the tailnet up).

**Operator SSH — optional**: `OperatorSshKeys`, a list of OpenSSH PUBLIC keys (a profile array, one key per
element; the reconcile writes it as an array, never comma-joined; a newline-separated string is read too; environment
`FLEET_OPERATOR_SSH_KEYS`, newline-separated). The log names each key by its SHA256 fingerprint. Until bootstrap 1.5.0
Remote Login and the operator's key were set up by hand on each Mac (owner, 2026-11-12: "i try to avoid manual steps").

The environment variables `FLEET_ACCEPT_JOBS`, `FLEET_ACCEPT_JOBS_FROM`, `FLEET_ACCEPT_JOBS_CHANNELS`,
`FLEET_ACCEPT_JOBS_REPOS`, `FLEET_ACCEPT_TOKENS`, `FLEET_ACCEPT_TOKENS_FROM`, `FLEET_ACCEPT_TOKENS_MODELS`,
`FLEET_ACCEPT_TOKENS_RESIDENCY` and `FLEET_CA_SHA256` override them.

**The loader — a bootstrap bump in minutes, not hours** (2026-11-12). The Intune script is `fleet-loader.sh`, a STABLE
loader of about 100 lines that changes only when its own behaviour does. Each run it reads the bootstrap's pin from the
`com.memex.local` profile — `BootstrapCommit` (40 hex) and `BootstrapSha256` (64 hex) — fetches
`deploy/macos/intune/fleet-bootstrap.sh` from this repository at that commit, verifies its sha256, keeps it
root-owned in `/Library/MeshWeaver/loader/` and runs it with the run's arguments. Bytes that do not match the pin are
REFUSED (logged). With no pin, a failed fetch or a refusal it runs its EMBEDDED copy — the bootstrap the reconcile
appends to the uploaded script after the line `__FLEET_EMBEDDED_BOOTSTRAP__` (`scripts[].source.embed`) — and else the
last verified copy; with none of them it FAILS (exit 1). Decisions go to `/Library/Logs/MeshWeaver/fleet-loader.log`
and the reading `loader`. Why: the Intune agent re-runs a CACHED script until its next full sync (8 h) and `syncDevice`
does not change that, but a PROFILE arrives within a minute of a `syncDevice` — so a bump is now a change of the
profile's two keys, which `--apply` writes and syncs: every awake Mac runs the new bootstrap at its next hourly run.

**Registering it** is the reconcile's job (section "Intune objects as code" below): the spec pins the script to a full commit of this
repository and the file's sha256, `--apply` uploads exactly those bytes (refusing any that hash differently), and
`--check` names a live copy that differs. A new version is a new pin in the spec, never a moving reference. It runs
as the system context, every hour (`PT1H`), 3 retries, with the Studios group included and the hand-attached
machines' group excluded. The version is `FLEET_BOOTSTRAP_VERSION` in the file; the run log names it on every line.

**Tests**: `deploy/macos/test/run-tests.sh` (the plist lints, the console-user and machine-id rules, and
a dry run on a bare Mac that plans every step, changes nothing and waits). `FLEET_BOOTSTRAP_DRY_RUN=1`
prints every changing command instead of running it.

## Intune objects as code (`intune-reconcile.py` + a declared spec)

A new Mac Studio needs exactly two human steps: the owner enrols it in Intune, and a global administrator presses
**Approve** on control. Everything Intune has to carry for that — the groups, the bootstrap, the `com.memex.local`
keys, the fleet CA's trust, the update window, the compliance carve-out, and the Mac's own group membership — is a
DECLARED spec reconciled by `intune-reconcile.py` (Python standard library only).

| Command | What it does |
|---|---|
| `--check --spec <f>` | Read-only. One line per object: `OK`, `DRIFT`, `HELD`, `PENDING`, `AWAITING`, `INFO` or `ERROR`, and every non-OK line says what `--apply` WOULD do. Exit 0 in sync (PENDING allowed), 2 drift, 1 error |
| `--apply --spec <f>` | Creates and patches to match. **Never deletes**: an assignment, group member or object the spec does not name is reported as kept. Assignments are written as the union of live and declared; the only target ever dropped is one the spec lists under `assign.remove`. An object with `hold` is never written |
| `--validate --spec <f>` | The spec's shape, no Graph call (CI) |
| `--self-test` | Against an in-memory Graph: create, no-op, drift, exclusion assignment, declared removal, held objects, device membership, a pinned source that does not match its sha256 |

**This repository holds the tool and an EXAMPLE** (`fleet.spec.example.json`, placeholders only). The installation's
real spec — object ids, hosts, serial numbers, the owner — lives in the estate's private configuration repository,
whose workflow calls [`intune-drift-check.yml`](../../../.github/workflows/intune-drift-check.yml) so the drift report
is written to a private log. `.github/workflows/intune-reconcile.yml` runs the self-test and validates the example here.

**Credential** (first that is present; the token is never printed): `INTUNE_CLIENT_ID` + `INTUNE_TENANT_ID` with
`INTUNE_CLIENT_SECRET`, or with `AZURE_FEDERATED_TOKEN_FILE` (workload identity federation — what the drift check
uses); otherwise `az account get-access-token --resource https://graph.microsoft.com --tenant <spec.tenant>`.
🔐 The scheduled drift check must NOT sign in as the apply identity: its federated credential would have to trust the
private repository's pull-request subject, and every pull-request branch could then mint a token that WRITES device
management. It signs in as a separate READ-ONLY identity (`DeviceManagementConfiguration.Read.All`,
`DeviceManagementScripts.Read.All`, `DeviceManagementManagedDevices.Read.All`, `Group.Read.All`, `Device.Read.All`, and
`DeviceManagementServiceConfig.Read.All` once the spec declares an `enrollmentProgram`, `DeviceManagementApps.Read.All`
once it declares `apps`),
whose federated credentials name that repository's `main` and `pull_request` subjects; the `appIdentity` line then
reads INFO (a different application), and the apply identity's grants are verified when a person runs `--apply`.
⚠️ The Azure CLI's own sign-in carries NO Intune scopes (measured 2026-11-09: every `deviceManagement` read →
403), so a person's `az login` reads the groups and nothing else; the reconcile needs the app identity.

**What each object is** (spec section → Intune object):

| Spec | Intune / directory object | Notes |
|---|---|---|
| `appIdentity.requiredRoles` | the app identity's Graph application permissions | **verified only** (from the token's `roles` claim): `DeviceManagementConfiguration.ReadWrite.All`, `DeviceManagementScripts.ReadWrite.All`, `DeviceManagementManagedDevices.Read.All`, `Group.Read.All`, `Device.Read.All`, `GroupMember.ReadWrite.All`. A missing one is drift no `--apply` repairs: an admin-consented grant is the owner's act |
| `groups.studios` / `groups.noBootstrap` | two ASSIGNED security groups: the Studios, and the hand-attached Studios the bootstrap must not touch | the app identity cannot create a group (no `Group.Create`); an absent group is reported with the body a person creates |
| `groups.*.users` `[{upn, id}]` | group membership of PEOPLE (e.g. the Mac admins group Platform SSO names) | by the user's directory object `id` → `POST /groups/{id}/members/$ref` (`GroupMember.ReadWrite.All`). The id is DECLARED: resolving a UPN app-only needs `User.Read.All`, which the app identity does not hold, so a user without one is `PENDING` with the `az ad user show` that reads it. A group declares users OR devices, never both |
| `groups.*.devices` `[{serial, name}]` | group membership | serial → `managedDevices` (macOS) → `azureADDeviceId` → the directory `device` object → `POST /groups/{id}/members/$ref`. A Mac not yet enrolled is `PENDING`; a member the spec does not declare is reported and kept |
| `scripts[]` | `deviceShellScript` | content = `source.path` at `source.commit` (a full sha) with `source.sha256`; `runAsAccount`, `executionFrequency`, `retryCount`, `blockExecutionNotifications`, include + exclude |
| `customProfiles[]` | `macOSCustomConfiguration`, device channel, domain `com.memex.local` | the payload is BUILT from `plist` + `keys` and compared by parsed value, not bytes. Keys: `ControlUrl`, `RegistryUrl`, `LoginServer`, `OwnerId`, `OwnerEmail`, the unattended first-run answers `Organisation`, `OwnerName`, `AcceptTerms`, `FleetCaSha256`, and the participation answers `AcceptJobs`, `AcceptJobsFrom`, `AcceptJobsChannels`, `AcceptJobsRepos`, `AcceptTokens`, `AcceptTokensFrom`, `AcceptTokensModels`, `AcceptTokensResidency`. `AcceptJobs` / `AcceptTokens` must be explicit booleans (validation refuses a spec without them), and `AcceptTokens: true` needs `AcceptTokensModels` (the bootstrap refuses tokens on without models, so validation does too); a list is written as ONE comma-separated string, the form `memex-local --accept-jobs=…` / `--token-models` take, so the bootstrap passes `defaults read` output through unchanged. **Any other `payloadType` is written VERBATIM** from `keys` (lists stay arrays, dictionaries stay dictionaries); two are validated by shape because a mistake in them installs and does nothing: `com.apple.extensiblesso` (Platform SSO, section "Mac admins through an Entra group") and `com.apple.TCC.configuration-profile-policy` (PPPC — validation refuses a profile that claims to ALLOW `ScreenCapture` or `ListenEvent`, which Apple lets an MDM only deny or leave to a standard user) |
| `trustedRootCertificates[]` | `macOSTrustedRootCertificate` | the fleet local CA's PUBLIC certificate (`certFile`, PEM or DER, next to the spec) and its DER sha256. Until the CA is minted, `sha256` is empty and the line is `PENDING — CA not minted yet`. Once declared, the `com.memex.local` keys must carry the same value as `FleetCaSha256` (what `memex-local ca adopt` reads); validation refuses either without the other |
| `settingsPolicies[]` | settings-catalog policy (the DDM night-window software-update policy) | `settings` maps `settingDefinitionId` → value; `--apply` rewrites those values in the LIVE tree and PUTs it back (the tree itself is created in the admin center, then its id is recorded). Each Apple release bumps `enforcement_targetosversion` + `enforcement_targetlocaldatetime` here |
| `apps[]` | an Intune macOS app (`macOSPkgApp`, `macOSLobApp`, `macOSDmgApp`), Graph beta `deviceAppManagement/mobileApps` | section "Apps" below: read by id or exact name (absent = `AWAITING`: the package is uploaded by a person, never by the reconcile), declared `properties` compared, a pinned post-install TEMPLATE rendered per client (secrets from the environment, one alias line per Mac of a group), assignments with their `intent`. Needs `DeviceManagementApps.ReadWrite.All` (apply) / `.Read.All` (check) |
| `compliancePolicies[]` | `deviceCompliancePolicy` | existing policies by id (assignments, declared `properties`), new ones created from `properties` |
| `enrollmentProgram` | the Apple Business Manager ADE token (`depOnboardingSetting`), a `depMacOSEnrollmentProfile`, its default flag, and each declared serial's profile assignment | section below. Needs `DeviceManagementServiceConfig.ReadWrite.All` on the apply identity and `.Read.All` on the drift identity |

**A Mac newly added to a group is synced at once.** When an `--apply` adds a declared Mac to a group, it asks that Mac
to sync right after (one `SYNCED … newly added to a group` line): its first bootstrap and its profiles come at the next
check-in, not at the agent's own cadence.

**A settings-catalog policy can be CREATED from the spec** (`settingsPolicies[].create`): `groups` maps each group
collection definition id to its child setting ids, and the reconcile builds the tree from each setting's OWN definition
(`GET beta/deviceManagement/configurationSettings/{id}`: a choice setting carries the declared option id, a simple one an
integer or a string as its value definition says), POSTs it with `platforms` / `technologies`, assigns it, and records
nothing by hand. The example spec declares the fleet's **FileVault** policy this way: FileVault on, deferred to the next
sign-in with no bypass, the personal recovery key escrowed to Intune (`com.apple.security.fderecoverykeyescrow_location`)
and rotated every 12 months. A Mac whose FileVault a person already turned on keeps its key until it rotates (or the
person stores it through Company Portal); a new Mac is encrypted at its first sign-in after the policy lands.

**Device sync after an apply.** The Intune agent re-runs its CACHED copy of a script until its next FULL sync —
every 28800 s (8 h): measured on the first Mac mini (2026-11-12), the hourly run after an `--apply` logged
*"Cached script policy result matches"* and ran the old bootstrap. So an `--apply` that CHANGED a script, profile,
certificate or settings policy then asks every Mac that object reaches (its include groups' members, minus its exclude
groups') to sync — `POST managedDevices/{id}/syncDevice` (an empty POST: Graph answers 411 without `Content-Length:
0`), one `SYNCED` line per Mac. `--sync` does only that, for every object the spec declares. It needs the application
role `DeviceManagementManagedDevices.PrivilegedOperations.All`, declared in `appIdentity.requiredRoles` so `--check`
reports it missing; it is assigned to the app's service principal as an appRoleAssignment — `az ad app permission
admin-consent` alone did not create it.

🚨 **Measured since (2026-11-12): `syncDevice` delivers PROFILES, not a new SCRIPT.** It wakes the Mac's MDM channel
(mdmclient), and a pending profile installs within a minute — but the Intune agent (the sidecar that runs the script)
fetched its script policies only at enrolment and then every 8 h (10:11Z … 18:12Z on the first Mac mini), whatever
`syncDevice` said; four syncs in between fetched nothing. A bootstrap bump therefore reaches a Mac at its agent's next
full sync, or sooner only when a person checks in through Company Portal ("Check status"). The reading
`/Library/MeshWeaver/fleet-state/version` (1.11.1) names the bootstrap that actually ran.

**The build host after a pre-setup join (1.12.0).** A fleet Mac joins BEFORE its first-run setup (its sign-in comes with
the enrolment), and that join runs without the update: no build VM exists yet, so memex-local KEEPS the build items it
opened (`~/.memex-local/enrol/buildhost`, 0700/0600). Every bootstrap run on a joined, finished Mac then runs
`memex-local buildhost attach` (as the person) while those items exist: it creates the build VM, writes the Mac's sizing,
attaches the release from them and removes them. Before 1.12.0 nothing finished it, and the first Mac mini said YES to
jobs and had no runners until a person attached it by hand (2026-11-14). The `buildhost` and `heartbeat` readings are
also readable again: 1.11.1 computed them with an inline `$(case …)`, which macOS's `/bin/bash` 3.2 cannot parse, and
wrote `" echo ok ;; *) echo red ;; esac) …"` instead of the verdict.

### The enrollment program (Apple Business Manager → Automated Device Enrollment)

With the `enrollmentProgram` section a fleet Mac enrols **zero-touch**: assigned to the fleet's MDM server in Apple
Business Manager, it enrols at Setup Assistant and comes up **supervised**. Its **bootstrap token**: Apple documents
that on macOS 26 or later the Mac creates one during device-management enrolment, and otherwise tries to generate and
store one whenever a secure-token user logs in, where the MDM supports it (Apple Platform Deployment, *Use secure token,
bootstrap token, and volume ownership in deployments*); that Intune receives it so on the fleet's Macs is NOT
established, so the bootstrap's `bootstrap-token` reading (above) stays the check. Graph **beta** only (the v1.0
endpoint carries none of these routes), read against the Microsoft Graph beta reference:

| What | Graph | `--check` | `--apply` |
|---|---|---|---|
| the token, by `token.tokenName` (or `token.id`) | `GET beta/deviceManagement/depOnboardingSettings` | `OK` with its expiry, last successful sync and synced device count; **`AWAITING`** when no token of that name exists; `PENDING` inside `renewWarnDays` (default 30) of expiry; `DRIFT` when expired or Apple reported a sync error | **never written**: the token is uploaded and renewed by a person (below) |
| the profile `profiles[]` (`platform: macOS`) | `GET/POST/PATCH …/depOnboardingSettings/{id}/enrollmentProfiles` (`#microsoft.graph.depMacOSEnrollmentProfile`) | each declared `settings` property compared with the live value | creates it, or PATCHes the declared settings |
| `default: true` | `POST …/enrollmentProfiles/{id}/setDefaultProfile` (no body) | drift while the profile's `isDefault` is not true | makes it the token's default macOS profile (a Mac ABM assigns later enrols with it even before the next run) |
| `assign.devicesOf` (declared group keys; their `devices[].serial`) | `GET …/importedAppleDeviceIdentities`, `POST …/enrollmentProfiles/{id}/updateDeviceProfileAssignment` `{"deviceIds": [serials]}` | per serial: `OK` (with its `enrollmentState` and `isSupervised`), `DRIFT` (no or another profile), `PENDING` (ABM has not assigned the serial to this MDM server) | ONE call for every drifting serial |

**A missing token is never red.** Until the owner has exchanged the token, every enrollment-program line is `AWAITING`
(counted as pending, exit 0) and nothing else in the spec changes its verdict. A credential that cannot READ the
enrollment program is different and IS red (`ERROR … needs DeviceManagementServiceConfig.Read.All`): a check that read
nothing never reads as one that found nothing. Validation refuses a setting that is not a `depMacOSEnrollmentProfile`
property, a non-boolean boolean, `isDefault` in `settings` (declare `default`), more than one default, and
`adminAccountPassword` (a secret never lives in the spec).

**What stays manual (the owner, in two portals):** Intune admin center → Devices → Enrollment → Apple → Enrollment
program tokens → Add: download Intune's public key; Apple Business Manager → Preferences → Your MDM Servers → Add: upload
it, download the server token; back in Intune, upload it **named exactly as `token.tokenName`**. Then, in ABM, assign the
fleet's serials to that MDM server. The token **expires after a year**: renew it in ABM and upload it to the SAME token
in Intune (a new token would orphan the profile and its assignments); the reconcile's `PENDING … renew within N days`
line starts 30 days before.

**A Mac already enrolled through Company Portal** (user enrolment) had no escrowed bootstrap token — measured on all
three fleet Macs on 2026-11-12 (MeshWeaver.Plugins `Hosting/MacStudioOnboarding` row 0b). Once ABM lists its serial under this MDM server and the profile is assigned (`OK enrollment assignment …`),
`sudo profiles renew -type enrollment` on the Mac re-runs the enrolment through ADE. NOT established: whether that
converts a user-enrolled Mac to an ADE enrolment (the fleet profile, supervision) WITHOUT an
erase on the fleet's macOS versions; the fallback is an erase and a fresh Setup Assistant. Also NOT established: whether
Intune honours the per-pane booleans (`appleIdDisabled`, …) alone or expects `enabledSkipKeys` too — read the created
profile back in the admin center after the first `--apply`.

### The console password that expired at enrolment (2026-11-09)

Read-only Graph on 2026-11-09: no macOS configuration profile and no macOS settings-catalog policy in the tenant
carries a password rule. The ONLY password rule reaching every Mac is the tenant's macOS **compliance** policy:
`passwordRequired: true`, `passwordRequiredType: deviceDefault`, no expiry days, no minimum length, assigned to
**all licensed users AND all devices**. Intune delivers a compliance password rule to a Mac as a passcode payload;
the second Studio's local password was marked expired right after enrolment, and every non-interactive SSH login
failed. NOT established: which passcode key macOS evaluated to expire it — no device-side profile dump was taken.

The fix, declared and **held** (it changes the tenant-wide policy, so it waits for the owner's go):
1. the tenant policy is assigned to **all devices** only, with **Fleet — Mac Studios** EXCLUDED, and its
   all-licensed-users assignment is a declared removal. Intune does not evaluate a DEVICE-group exclusion against a
   USER-targeted assignment, so excluding the Studios while the policy still reaches their enrolling user would
   change nothing; for a macOS-only policy the all-devices target already covers every enrolled Mac;
2. a Studios compliance policy without a password rule (`storageRequireEncryption`, Gatekeeper), assigned to the
   Studios, so they keep a compliance verdict.
Lifting the hold is a reviewed change to the spec (delete `hold`), never a command-line flag.

### Mac admins through an Entra group (Platform SSO)

**What is declared** (`customProfiles[]`, `payloadType: com.apple.extensiblesso`): macOS **Platform SSO** through the
Microsoft Enterprise SSO plug-in that Company Portal carries — extension `com.microsoft.CompanyPortalMac.ssoextension`,
team `UBF8T346G9`, `Type: Redirect`, the Entra URLs, `ScreenLockedBehavior: DoNotHandle`, and a `PlatformSSO`
dictionary: `AuthenticationMethod: Password`, `UseSharedDeviceKeys: true`, `EnableCreateUserAtLogin: true` (an Entra
user signs in at the login window and gets a local account), `NewUserAuthorizationMode` / `UserAuthorizationMode:
Groups` with `AdministratorGroups` naming the admins' Entra group, `TokenToUserMapping` (`preferred_username`, `name`),
`LoginPolicy: [AttemptAuthentication]`, and `NonPlatformSSOAccounts` — the local accounts Platform SSO must leave alone.
The admins' group is a `groups.*.users` group (above). It also carries the Redirect plug-in's `ExtensionData`
(`browser_sso_interaction_enabled`, `disable_explicit_app_prompt`, `AppPrefixAllowList`), so on a Mac it reaches it
REPLACES the hand-registered Redirect profile of section "Microsoft sign-in" — which must then no longer reach that Mac:
two `com.apple.extensiblesso` payloads for the same extension on one Mac conflict.

**Facts it rests on** (read 2026-11-20):

| Fact | Source |
|---|---|
| Intune's documented route is the settings catalog (Authentication → Extensible Single Sign On), with Registration Token `{{DEVICEREGISTRATION}}`; "any undocumented Platform SSO settings aren't supported"; Company Portal 5.2404.0+, macOS 13+ (14+ recommended) | [Configure Platform SSO for macOS](https://learn.microsoft.com/en-us/intune/intune-service/configuration/platform-sso-macos), [scenarios](https://learn.microsoft.com/en-us/intune/device-configuration/settings-catalog/configure-platform-sso-scenarios-macos) |
| 🚨 **Microsoft supports only `Standard` and `Admin`** for the two authorization modes — `Groups` is Apple's, NOT supported by Microsoft today | [scenarios](https://learn.microsoft.com/en-us/intune/device-configuration/settings-catalog/configure-platform-sso-scenarios-macos) |
| Apple: `AdministratorGroups` is an array of "the group name"; "the system requests membership during authentication"; `UserAuthorizationMode` applies "each time the user authenticates"; the keys arrived in macOS 14, `NonPlatformSSOAccounts` / `LoginPolicy` in 15 | [Apple com.apple.extensiblesso schema](https://github.com/apple/device-management/blob/release/mdm/profiles/com.apple.extensiblesso.yaml), [Apple Platform Deployment: Platform SSO](https://support.apple.com/guide/deployment/dep7bbb05313/web) |
| Creating the account at the login window works only with `Password` or `SmartCard`, needs `UseSharedDeviceKeys`, and a network connection (the validation refuses the other combinations) | the Apple schema and guide above |
| With `Password` the Entra password REPLACES the local one and they stay in sync; `NonPlatformSSOAccounts` (macOS 15+) exempts an account from the registration prompt and the login policies | [scenarios](https://learn.microsoft.com/en-us/intune/device-configuration/settings-catalog/configure-platform-sso-scenarios-macos) |
| Microsoft's other-MDM guide lists no registration token among its required settings; Apple marks `RegistrationToken` optional (silent registration only) | [macOS Platform SSO integration guide](https://learn.microsoft.com/en-us/entra/identity/devices/macos-psso-integration-guide) |
| An app identity cannot create a group without `Group.Create` (or higher); `GroupMember.ReadWrite.All` adds members; resolving a UPN needs `User.Read.All` | [Create group](https://learn.microsoft.com/en-us/graph/api/group-post-groups?view=graph-rest-1.0), [Add members](https://learn.microsoft.com/en-us/graph/api/group-post-members?view=graph-rest-1.0), [Get user](https://learn.microsoft.com/en-us/graph/api/user-get?view=graph-rest-1.0) |

**Why a custom profile and not the settings catalog.** The reconcile can build a settings-catalog tree only from flat
group → simple/choice children; Platform SSO is a nested tree with string collections (`URLs`,
`AdministratorGroups`), and its setting definition ids were not readable here (the person's `az login` carries no Intune
scope). A custom profile is the same Apple payload, fully declared and compared by value. What it gives up: Intune's
`{{DEVICEREGISTRATION}}` is not a documented custom-profile variable, so the payload carries NO `RegistrationToken` and
each account registers interactively (the "Registration required" notification → sign-in and MFA). If the pre-shipping
test below fails, the fallback is the settings-catalog policy Microsoft documents, created in the admin center with the
same values, its id recorded under `settingsPolicies[]`.

**Not established** (each is read on the first Mac, at our desk, before any Mac ships):
- whether Entra delivers group membership the Platform SSO extension matches against `AdministratorGroups` — by display
  name, by object id, or not at all (Microsoft documents nothing; declare BOTH the name and the group's object id once it
  exists). The failure mode is safe — an Entra user gets a STANDARD account, nobody gets admin by mistake — and the
  test is: sign in at the login window as a member, then `dseditgroup -o checkmember -m <short name> admin`;
- whether registration can be completed over Screen Sharing (no source says either way);
- whether the extension registers the device with no `RegistrationToken` in an Intune custom profile.

**Unattended boot and the owner account.** FileVault is off on the fleet's startup volumes, so the login window is
reached with no unlock and `FileVaultPolicy` is not declared. The Mac's local OWNER account (the one its memex runs as)
must be listed in `NonPlatformSSOAccounts`: with `Password`, a registered account's local password would become the
Entra password — breaking anything that relies on the local one. Members of the admins' group then sign in with their
OWN Entra accounts, created at the login window (over Screen Sharing, with Remote Management on).

### Apps: TeamViewer Host per client

`apps[]` declares an Intune macOS app the reconcile READS and ASSIGNS; it never uploads app content (the package is a
vendor's or a client's licensed file). A missing app is `AWAITING` — counted as pending, never red — with the admin
center path to upload it, named exactly as `displayName`. Present, it compares the declared `properties`, the
post-install script and the assignments (each with the declared `intent`, `required` / `available` / `uninstall`; a
declared target live with another intent is rewritten, an undeclared one is kept with its own).

**TeamViewer Host for a client's remote support** (alongside Screen Sharing over the tailnet; the licence is the
CLIENT's): one `macOSPkgApp` per client, assigned `required` to that client's group of Macs, plus that client's PPPC
profile (`customProfiles[]`, `com.apple.TCC.configuration-profile-policy`). Per client and per machine:

- the post-install script is the pinned TEMPLATE `teamviewer/teamviewer-host-postinstall.sh`, rendered by the reconcile:
  `@@NAME@@` ← `postInstallScript.params`; `@@DEVICE_ALIASES@@` ← one `<serial> <name>` line per Mac of the
  `aliasesFrom` group (each Mac finds its own alias by serial; the device alias is `ALIAS_PREFIX` + name). Refused
  before anything is written: a param value with a quote, a newline or `@@` (it sits inside a single-quoted assignment
  run as root); an `aliasesFrom` that is not a DEVICE group (a `devices` list, possibly still empty, and no users); and a
  rendered script that still carries a `@@NAME@@` the spec does not fill (it would be installed verbatim and then match
  the drift check forever);
- the client's **assignment id** is a SECRET: the spec names only the environment variable (`secrets.ASSIGNMENT_ID`)
  that carries it when a person runs `--apply`. `--check` puts the live script's `ASSIGNMENT_ID='…'` line back to its
  placeholder before comparing, so the drift check never needs the secret and never prints it; an `--apply` that would
  rewrite the script without the variable set is an `ERROR` naming it, and writes nothing;
- the script runs `/Applications/TeamViewerHost.app/Contents/Helpers/TeamViewer_Assignment -assignment_id <id>
  -device_alias <alias> --reassign` (TeamViewer Corporate / Tensor, 15+:
  [Assign a device via command line](https://www.teamviewer.com/en/global/support/knowledge-base/teamviewer-remote/deployment/mass-deployment-user-guide/assign-a-device-via-command-line-8-10/)),
  three attempts, logging to `/Library/Logs/MeshWeaver/teamviewer-assignment.log` — Intune does not report a failed
  post-install script ([unmanaged PKG apps](https://learn.microsoft.com/en-us/intune/intune-service/apps/macos-unmanaged-pkg):
  agent 2309.007+ for scripts, each under 15,360 characters). A changed assignment id reaches a Mac only with a
  reinstall: the script runs after an install, not on its own schedule.

**What a person does, per client:** download the client's TeamViewer HOST package for macOS from the client's
Management Console (the rollout configuration's installer ZIP holds separate Host and full-client packages:
[mass deployment on macOS](https://www.teamviewer.com/hk/global/support/knowledge-base/teamviewer-classic/deployment/mass-deployment-on-macos/));
upload the Host `.pkg` in the admin center as a macOS app (PKG) named exactly as `displayName`, leaving the scripts
empty; then `--apply` with the assignment-id variable set, which writes the rendered script and the assignment.

🚨 **Screen Recording cannot be pre-granted by MDM.** Apple: for `ScreenCapture` "a profile can't grant access … it
can only deny it", and `AllowStandardUserToSetSystemService` (valid only for `ScreenCapture` and `ListenEvent`) merely
lets a non-admin flip the switch ([Apple PPPC schema](https://github.com/apple/device-management/blob/release/mdm/profiles/com.apple.TCC.configuration-profile-policy.yaml)).
TeamViewer says the same: "users must still grant Screen Recording"
([remote control a Mac](https://www.teamviewer.com/pl/global/support/knowledge-base/teamviewer-remote/remote-control/remote-control-a-mac/)).
So the PPPC profile grants **Accessibility** and leaves Screen Recording to ONE approval per Mac by a person at the
console — at our desk, before the Mac ships. **Not established:** TeamViewer Host's bundle ids and designated
requirements (TeamViewer publishes no PPPC file); the declared ones (`com.teamviewer.TeamViewerHost`,
`com.teamviewer.desktop`, team `H7UGFBUGV6`) are verified with `codesign -dr - /Applications/TeamViewerHost.app` on the
first installed copy before the first Mac ships — a wrong requirement installs and grants nothing. Also not
established: whether TeamViewer Host connects at the login window with nobody signed in, and whether macOS's periodic
screen-recording re-confirmation (macOS 15+) reaches it; `forceBypassScreenCaptureAlert`
(`com.apple.applicationaccess`, 15.1+) is not declared.

### Device membership without a spec line: the dynamic-group alternative (evaluated, not created)

A dynamic device group would add a Studio without anyone naming its serial. What the directory offers for these
Macs (read 2026-11-09): `deviceOSType = MacMDM`, `deviceModel = Mac17,14`, `manufacturer = Apple`,
`enrollmentProfileName` empty, `deviceOwnership = Personal` (Company Portal enrolment). So:
- `(device.deviceOSType -eq "MacMDM") and (device.deviceModel -in [...])` would work today, but the list of Mac
  Studio model identifiers has to grow with every generation, and it would ALSO capture any person's Mac Studio that
  is not a fleet machine — the fleet bootstrap would then run as root on it;
- `device.enrollmentProfileName -eq "<ADE profile>"` is the right rule once the Studios enrol through Apple
  Business Manager (automated device enrolment with a fleet-only enrolment profile): membership then follows the
  purchase, nothing is named, nothing is mis-captured. Without ABM it matches nothing.
Until ABM exists, the declared serial list is the safer choice: a Mac joins the fleet because someone named it. With the
enrollment program declared (section above), the same serial list also decides which Macs ABM's token enrols with the
fleet profile; the dynamic rule remains possible later, and is not adopted here.

## Microsoft sign-in: the device's work account, silently

A managed Mac is signed in to macOS with a LOCAL account, while Company Portal registered it with the
person's Entra work account; a managed iPhone may carry a private Apple ID while it is MDM-enrolled for the
work account. Microsoft sign-in on these devices must use the WORK account — and never silently pick a
personal Microsoft account. Three pieces, committed here as profiles and Graph bodies; the fleet objects the reconcile declares (section
above) are the ones it creates and checks — these sign-in pieces are registered by hand from the bodies below:

| # | Profile (source of truth) | Graph body | What it does |
|---|---|---|---|
| 1 | `profiles/microsoft-enterprise-sso.mobileconfig` (macOS) | `graph/microsoft-enterprise-sso.json` → `POST /beta/deviceManagement/deviceConfigurations` | Microsoft Enterprise SSO plug-in, **Redirect** type, extension `com.microsoft.CompanyPortalMac.ssoextension`, team `UBF8T346G9`, the Microsoft Entra URLs (public, US and China clouds). `browser_sso_interaction_enabled = 1` (Safari, ASWebAuthenticationSession and apps that do not use MSAL may bootstrap the shared credential), `disable_explicit_app_prompt = 1`, and `AppPrefixAllowList` restating the macOS default (`com.apple.`, `com.microsoft.`) plus `cloud.meshweaver.`. **Requires** the Mac enrolled and Company Portal installed (it carries the extension). |
| 2 | `../../ios/intune/profiles/microsoft-enterprise-sso.mobileconfig` (iOS / iPadOS) | `../../ios/intune/graph/microsoft-enterprise-sso.json` → same endpoint | The same plug-in on iOS: extension `com.microsoft.azureauthenticator.ssoextension` (team `SGGM6D27TK`, which iOS ignores), the same URLs and keys, plus `Enable_SSO_On_All_ManagedApps = 1` so an Intune-deployed Memex uses it; `AppPrefixAllowList` restates the iOS default `com.apple.` and adds `cloud.meshweaver.`. **Requires Microsoft Authenticator** on the device: `../../ios/intune/graph/microsoft-authenticator-app.json` (an `iosStoreApp`, `POST /beta/deviceAppManagement/mobileApps`) assigned **required** with `assign-authenticator-required-pilot.json`. Where Apple Business Manager syncs Authenticator as a VPP app, assign that synced app instead of creating a second one. |
| 3 | `profiles/com.memex.local.mobileconfig` (macOS) | `graph/com.memex.local.json` (`macOSCustomConfiguration`, device channel; `payload` is the file's base64, pinned by `deploy/macos/test/run-tests.sh`) | The managed preference `com.memex.local` `UserPrincipalName = {{userprincipalname}}`. `memex-local` reads it as the device's **work identity** (below). |
| 4 | `../../ios/intune/app-config/memex.appconfig.xml` | `../../ios/intune/graph/memex-app-configuration.json` → `POST /beta/deviceAppManagement/mobileAppConfigurations` | Managed app configuration for Memex (`cloud.meshweaver.memex`): `loginHint` and `userPrincipalName` = `{{userprincipalname}}`. The app reads them from `NSUserDefaults` `com.apple.configuration.managed` and offers that account first at Microsoft sign-in. Reaches the app only once Intune deploys Memex (App Store release or custom app); replace `targetedMobileApps` with that app's Intune id. |

Assignments (all to the pilot group `b892bd0f-86ed-46ce-8593-3b9860f7a557`, never all devices or all users):
`graph/assign-pilot.json` and `../../ios/intune/graph/assign-pilot.json` → `POST …/deviceConfigurations/{id}/assign`;
`assign-app-configuration-pilot.json` → `POST …/mobileAppConfigurations/{id}/assign`;
`assign-authenticator-required-pilot.json` → `POST …/mobileApps/{id}/assign`.

**Order.** iOS: Authenticator first (required app), then the SSO profile, then — once Memex is
Intune-deployed — its app configuration. macOS: Company Portal is already there on an enrolled Mac; the SSO
profile and the `com.memex.local` profile are independent, and `memex-local update` picks the identity up at
its next run.

**Keys verified** against Microsoft Learn, *Microsoft Enterprise SSO plug-in for Apple devices* and the Graph
beta `macOSRedirectSingleSignOnExtension` / `iosDeviceFeaturesConfiguration` (`iosSingleSignOnExtension`; the
older `singleSignOnExtension` is deprecated) references, 2026-11-08.

### The device's work identity in `memex-local`

`work_identity` (deploy/homebrew/bin/memex-local) answers, first that answers and without admin rights:

1. the managed preference `UserPrincipalName` in `com.memex.local` (profile 3) — device store, the user's
   managed store (`/Library/Managed Preferences/<user>/`), then the user domain;
2. Platform SSO — the `User Configuration` block's `loginUserName` of `app-sso platform -s`, when the Mac is
   registered (never the device block's);
3. nothing. A failed or empty read, or a token Intune left unsubstituted, is "no identity" and changes nothing.

Deliberately **not** read, measured on a managed Mac on 2026-11-08: Company Portal's defaults (no UPN in
them), its logs (they carried a DIFFERENT person's UPN), the Intune MDM / device-registration certificates in
the keychain (they name the device id only), and `profiles show -type enrollment` (root only).

With an identity and no explicit owner (`--owner`, `MEMEX_OWNER`, `OwnerId`, or the overlay's recorded one),
the install's OWNER — its global administrator — is the work account: id = the UPN's prefix, e-mail = the
UPN (paired: never attributed to a different id). When the owner's e-mail IS the work account, the overlay
also gets `Authentication__Microsoft__LoginHint` (the UPN) and `Authentication__Microsoft__DomainHint` (its
domain), which the portal sends as `login_hint` / `domain_hint`. `memex-local owner status` names the work
identity and where it came from. Tests: `deploy/homebrew/test/run-tests.sh` ("the DEVICE'S WORK IDENTITY").
