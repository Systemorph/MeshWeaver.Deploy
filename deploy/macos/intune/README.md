# Fleet bootstrap for the device-management layer (Intune)

`fleet-bootstrap.sh` brings a Mac Studio of the fleet to "joined". The device-management layer runs it
**as root** on the fleet's Mac Studio group (an Intune macOS shell script, run as the signed-in system
context, repeated on Intune's schedule). Every run is idempotent: it does only what is missing, prints
one line per step, and appends the same lines to `/Library/Logs/MeshWeaver/fleet-bootstrap.log`. Design:
MeshWeaver.Plugins `Hosting/MacStudioFleet.md` §2b and §2b.2.

| Step | What it does | Waits (exit 0) when |
|---|---|---|
| console user | the person the Mac's memex runs as (`stat -f%Su /dev/console`). If that person's password must be changed (an expired password, or a device-management "change at next sign-in" policy whose moment has passed — read from `pwpolicy -getaccountpolicies` and `dscl … accountPolicyData`), the run's FIRST line says so with the remedy: every non-interactive SSH login fails until the person changes it at the console | no one is logged in |
| participation | the owner's two answers, `AcceptJobs` and `AcceptTokens` (below), read BEFORE anything is installed | — **fails** (exit 1, the missing key named) when either is absent or not a boolean, or a YES lacks its `…From` (tokens: also `AcceptTokensModels`) |
| Homebrew | `/opt/homebrew`, owned by that person (the documented untar install, no prompt) | — |
| tools | `colima docker kubectl helm ollama` and `systemorph/memex/memex-local` (the tap tapped AND trusted first), as that person. Every command run as that person gets `/opt/homebrew/bin` and `/usr/local/bin` on PATH | — |
| shell | `eval "$(/opt/homebrew/bin/brew shellenv)"` appended ONCE to the person's `~/.zprofile` (left alone when it already loads Homebrew), so their own `memex-local` finds `kubectl` | — |
| tailscale binaries | `tailscale` + `tailscaled` fetched BY ROOT from Homebrew's bottle (URL and sha256 from the formula API, verified) into the root-owned `/Library/MeshWeaver/tailscale` — what runs as root never comes from the user-owned Homebrew tree | — |
| tailscaled | a ROOT LaunchDaemon `com.meshweaver.fleet.tailscaled` on `/var/run/tailscaled.socket` (the Tailscale app does not run before login; this does). memex-local uses this socket by itself | — |
| network | `tailscale up --auth-key=file:<FLEET_PREAUTH_KEY_FILE>` (by file reference, never an argument; deleted only after the join succeeded) | no key yet (control mints it at enrolment) |
| local CA | a FLEET Mac does not wait: when the fleet local CA (`FleetCaSha256`) is in the System keychain and trusted (its trusted-root profile), `up` runs with `MEMEX_CA_TRUST=defer` until its key arrives with the enrolment code (`~/.memex-local/enrol/fleet-ca.key`), and then `memex-local ca adopt` makes it mkcert's CA and re-issues the portal's certificate | no fleet CA and the mkcert CA (`<CAROOT>/rootCA.pem`, a PUBLIC certificate) is not yet trusted: a person runs `mkcert -install` once (macOS allows the trust change only with a person at the UI, even as root) |
| memex | registry mode (once — never re-written); the local CA created without the keychain prompt; `memex-local up` until `memex-local verify` passes, `memex-local fleet join --code-file ~/.memex-local/enrol/<id>.code` with the owner's answers (`--accept-jobs=…` or `--no-accept-jobs --no-buildhost`, `--accept-tokens=… --token-models … [--residency …]` or `--no-accept-tokens`), and a joined Mac's unfinished back-lease enrolment resumed from its stored credentials | no `ControlUrl` yet, or the approval and the enrolment code are pending (the link is in the log and in `~/.memex-local/enrol/<id>.link`) |

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
straight into a vault object and reaches each Mac sealed in its enrolment code, item `a`).

**Participation — required, never defaulted** (the owner's answers, Mac onboarding contract §5):

| Key | Type | Meaning |
|---|---|---|
| `AcceptJobs` | boolean, **required** | the Mac takes the fleet's build-queue jobs. NO ⇒ no build host at all (no build VM, runners or priority agent; `fleet join --no-accept-jobs --no-buildhost`) |
| `AcceptJobsFrom` | list (array or comma string), required on YES | the instances whose jobs it takes (`control.example.com`, `*.example.cloud`, `*`) |
| `AcceptJobsChannels`, `AcceptJobsRepos` | list, optional | narrow the jobs to these release channels / `owner/repo`s |
| `AcceptTokens` | boolean, **required** | the Mac serves local models to the fleet's token queue. NO ⇒ no token models beyond the portal's own chat model |
| `AcceptTokensFrom`, `AcceptTokensModels` | list, both required on YES | whose token requests, which models |
| `AcceptTokensResidency` | string, optional | where the Mac is (`CH`, `Eu`) — stamped on what it serves |

The environment variables `FLEET_ACCEPT_JOBS`, `FLEET_ACCEPT_JOBS_FROM`, `FLEET_ACCEPT_JOBS_CHANNELS`,
`FLEET_ACCEPT_JOBS_REPOS`, `FLEET_ACCEPT_TOKENS`, `FLEET_ACCEPT_TOKENS_FROM`, `FLEET_ACCEPT_TOKENS_MODELS`,
`FLEET_ACCEPT_TOKENS_RESIDENCY` and `FLEET_CA_SHA256` override them.

**Registering it**: upload this file's content at a tagged version as the Intune shell script. Set it to
run as the signed-in system context, repeat it (for example daily), and assign it to the Mac Studio group.
The version is `FLEET_BOOTSTRAP_VERSION` in the file; the run log names it on every line.

**Tests**: `deploy/macos/test/run-tests.sh` (the plist lints, the console-user and machine-id rules, and
a dry run on a bare Mac that plans every step, changes nothing and waits). `FLEET_BOOTSTRAP_DRY_RUN=1`
prints every changing command instead of running it.

## Microsoft sign-in: the device's work account, silently

A managed Mac is signed in to macOS with a LOCAL account, while Company Portal registered it with the
person's Entra work account; a managed iPhone may carry a private Apple ID while it is MDM-enrolled for the
work account. Microsoft sign-in on these devices must use the WORK account — and never silently pick a
personal Microsoft account. Three pieces, committed here and created in Intune only on the maintainer's go
(nothing in this repository calls Graph):

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
