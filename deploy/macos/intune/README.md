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
| participation | the owner's two answers, `AcceptJobs` and `AcceptTokens` (below), read BEFORE anything is installed | — **fails** (exit 1, the missing key named) when either is absent or not a boolean, or a YES lacks its `…From` (tokens: also `AcceptTokensModels`) |
| Homebrew | `/opt/homebrew`, owned by that person (the documented untar install, no prompt) | — |
| tools | `colima docker kubectl helm ollama lego` (lego: `memex-local cert issue`, the internal name's certificate) and `systemorph/memex/memex-local` (the tap tapped AND trusted first), as that person. Every command run as that person gets `/opt/homebrew/bin` and `/usr/local/bin` on PATH | — |
| shell | `eval "$(/opt/homebrew/bin/brew shellenv)"` appended ONCE to the person's `~/.zprofile` (left alone when it already loads Homebrew), so their own `memex-local` finds `kubectl` | — |
| tailscale binaries | `tailscale` + `tailscaled` fetched BY ROOT from Homebrew's bottle (URL and sha256 from the formula API, verified) into the root-owned `/Library/MeshWeaver/tailscale` — what runs as root never comes from the user-owned Homebrew tree | — |
| tailscaled | a ROOT LaunchDaemon `com.meshweaver.fleet.tailscaled` on `/var/run/tailscaled.socket` (the Tailscale app does not run before login; this does). memex-local uses this socket by itself | — |
| network | `tailscale up --auth-key=file:<FLEET_PREAUTH_KEY_FILE>` (by file reference, never an argument; deleted only after the join succeeded) | no key yet (control mints it at enrolment) |
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
| `AcceptJobsFrom` | list (array or comma string), required on YES | the instances whose jobs it takes (`control.example.com`, `*.example.cloud`, `*`) |
| `AcceptJobsChannels`, `AcceptJobsRepos` | list, optional | narrow the jobs to these release channels / `owner/repo`s |
| `AcceptTokens` | boolean, **required** | the Mac serves local models to the fleet's token queue. NO ⇒ no token models beyond the portal's own chat model |
| `AcceptTokensFrom`, `AcceptTokensModels` | list, both required on YES | whose token requests, which models |
| `AcceptTokensResidency` | string, optional | where the Mac is (`CH`, `Eu`) — stamped on what it serves |

**Operator SSH — optional**: `OperatorSshKeys`, a list of OpenSSH PUBLIC keys (a profile array, one key per
element; the reconcile writes it as an array, never comma-joined; a newline-separated string is read too; environment
`FLEET_OPERATOR_SSH_KEYS`, newline-separated). The log names each key by its SHA256 fingerprint. Until bootstrap 1.5.0
Remote Login and the operator's key were set up by hand on each Mac (owner, 2026-11-12: "i try to avoid manual steps").

The environment variables `FLEET_ACCEPT_JOBS`, `FLEET_ACCEPT_JOBS_FROM`, `FLEET_ACCEPT_JOBS_CHANNELS`,
`FLEET_ACCEPT_JOBS_REPOS`, `FLEET_ACCEPT_TOKENS`, `FLEET_ACCEPT_TOKENS_FROM`, `FLEET_ACCEPT_TOKENS_MODELS`,
`FLEET_ACCEPT_TOKENS_RESIDENCY` and `FLEET_CA_SHA256` override them.

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
| `--check --spec <f>` | Read-only. One line per object: `OK`, `DRIFT`, `HELD`, `PENDING`, `INFO` or `ERROR`, and every non-OK line says what `--apply` WOULD do. Exit 0 in sync (PENDING allowed), 2 drift, 1 error |
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
`DeviceManagementScripts.Read.All`, `DeviceManagementManagedDevices.Read.All`, `Group.Read.All`, `Device.Read.All`),
whose federated credentials name that repository's `main` and `pull_request` subjects; the `appIdentity` line then
reads INFO (a different application), and the apply identity's grants are verified when a person runs `--apply`.
⚠️ The Azure CLI's own sign-in carries NO Intune scopes (measured 2026-11-09: every `deviceManagement` read →
403), so a person's `az login` reads the groups and nothing else; the reconcile needs the app identity.

**What each object is** (spec section → Intune object):

| Spec | Intune / directory object | Notes |
|---|---|---|
| `appIdentity.requiredRoles` | the app identity's Graph application permissions | **verified only** (from the token's `roles` claim): `DeviceManagementConfiguration.ReadWrite.All`, `DeviceManagementScripts.ReadWrite.All`, `DeviceManagementManagedDevices.Read.All`, `Group.Read.All`, `Device.Read.All`, `GroupMember.ReadWrite.All`. A missing one is drift no `--apply` repairs: an admin-consented grant is the owner's act |
| `groups.studios` / `groups.noBootstrap` | two ASSIGNED security groups: the Studios, and the hand-attached Studios the bootstrap must not touch | the app identity cannot create a group (no `Group.Create`); an absent group is reported with the body a person creates |
| `groups.*.devices` `[{serial, name}]` | group membership | serial → `managedDevices` (macOS) → `azureADDeviceId` → the directory `device` object → `POST /groups/{id}/members/$ref`. A Mac not yet enrolled is `PENDING`; a member the spec does not declare is reported and kept |
| `scripts[]` | `deviceShellScript` | content = `source.path` at `source.commit` (a full sha) with `source.sha256`; `runAsAccount`, `executionFrequency`, `retryCount`, `blockExecutionNotifications`, include + exclude |
| `customProfiles[]` | `macOSCustomConfiguration`, device channel, domain `com.memex.local` | the payload is BUILT from `plist` + `keys` and compared by parsed value, not bytes. Keys: `ControlUrl`, `RegistryUrl`, `LoginServer`, `OwnerId`, `OwnerEmail`, the unattended first-run answers `Organisation`, `OwnerName`, `AcceptTerms`, `FleetCaSha256`, and the participation answers `AcceptJobs`, `AcceptJobsFrom`, `AcceptJobsChannels`, `AcceptJobsRepos`, `AcceptTokens`, `AcceptTokensFrom`, `AcceptTokensModels`, `AcceptTokensResidency`. `AcceptJobs` / `AcceptTokens` must be explicit booleans (validation refuses a spec without them), and `AcceptTokens: true` needs `AcceptTokensModels` (the bootstrap refuses tokens on without models, so validation does too); a list is written as ONE comma-separated string, the form `memex-local --accept-jobs=…` / `--token-models` take, so the bootstrap passes `defaults read` output through unchanged |
| `trustedRootCertificates[]` | `macOSTrustedRootCertificate` | the fleet local CA's PUBLIC certificate (`certFile`, PEM or DER, next to the spec) and its DER sha256. Until the CA is minted, `sha256` is empty and the line is `PENDING — CA not minted yet`. Once declared, the `com.memex.local` keys must carry the same value as `FleetCaSha256` (what `memex-local ca adopt` reads); validation refuses either without the other |
| `settingsPolicies[]` | settings-catalog policy (the DDM night-window software-update policy) | `settings` maps `settingDefinitionId` → value; `--apply` rewrites those values in the LIVE tree and PUTs it back (the tree itself is created in the admin center, then its id is recorded). Each Apple release bumps `enforcement_targetosversion` + `enforcement_targetlocaldatetime` here |
| `compliancePolicies[]` | `deviceCompliancePolicy` | existing policies by id (assignments, declared `properties`), new ones created from `properties` |

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
Until ABM exists, the declared serial list is the safer choice: a Mac joins the fleet because someone named it.

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
