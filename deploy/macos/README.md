# Memex on a Mac — the professional distribution (design + the installer)

**Decided by the maintainer, 2026-10-26** (*"can we do sth more professional [than Homebrew]? … can you
initiate this?"*, then *"ideally it will see what's there and install whatever is missing"*). This page is
the design of record for how the local memex stack (`memex-local`: Colima + k3s + Helm on a Mac) and the
back-lease build host (`memex-local backlease …`, the host sampler, Deploy #17) reach a Mac **without
Homebrew**, and the manual for the installer this directory builds. Homebrew (`deploy/homebrew`, the tap
`Systemorph/homebrew-memex`) **stays the developer channel**; nothing about it changes.

| Part | State |
|---|---|
| §1a the signed, notarized `.pkg` and its CI lane | **implemented here**, builds and installs unsigned; signing and notarization run the moment the maintainer provisions §1f |
| §2 probe-first toolchain (existing first, vendored second) | **implemented here**: `share/tools.manifest` + `share/tools.sh`, the postinstall, `memex-local doctor` |
| §1d the optional unattended-host LaunchDaemon | **implemented here** as the optional, unselected choice; its unattended first run on a real headless Mac is the follow-up's acceptance test |
| §3 managed preferences (MDM) | **implemented here**: `com.memex.local` is read by `backlease enroll` and the daemon postinstall; an example profile ships |
| §1b the menu-bar app "Memex" | **design only** — implementation is the next change set |
| §1e our own VM on the Virtualization framework | **later, optional**, never a precondition |

The directory, and what each file is for, is listed in §6.

## 1. The shape

### 1a. A Developer ID signed and notarized `.pkg`, built by this repository's CI

One installer per architecture — `memex-local-<version>-arm64.pkg` and `memex-local-<version>-x86_64.pkg`
— built by `build-pkg.sh` on a macOS runner (`.github/workflows/macos-pkg.yml`) with Apple's own tools:
`pkgbuild` for the two component packages, `productbuild` for the product archive (the Distribution with
a readable welcome, the repository's licence and a conclusion page), `codesign` for every vendored Mach-O,
`productbuild --sign` with the **Developer ID Installer** certificate, `xcrun notarytool submit --wait`
and `xcrun stapler staple`. The version is the Homebrew tap's series, `0.3.<main commit count>`, so a
package and a formula with the same number are the same tree.

**Why per-architecture and not a universal package.** The vendored tools are published per architecture;
a universal layout would double the payload (today 82 MB per package) to carry binaries that can never
run on the installing Mac, and `hostArchitectures` in the Distribution lets Installer refuse the wrong one
before anything is written. The CI job builds both from one runner (downloads are cross-architecture;
nothing vendored is executed at build time) and installs the one matching the runner.

**What it installs, and where.** Everything lives under **`/opt/memex`** — the Unix convention for a
self-contained vendor package (`/opt/puppetlabs`, `/opt/datadog-agent`), and *not* Homebrew's prefix on
either architecture (`/opt/homebrew` on Apple silicon; `/usr/local` on Intel, which rules `/usr/local/memex`
out). The command reaches PATH through **`/etc/paths.d/memex`** — the mechanism Apple's own and other
`.pkg`-delivered command-line tools use — never a symlink into `/usr/local/bin`, so a Homebrew
`memex-local` and this one never fight over a path; `memex-local doctor` says which one answers.

| Path | What |
|---|---|
| `/opt/memex/bin/memex-local` | the wrapper on PATH (sets `MEMEX_LOCAL_SHARE`, `MEMEX_CHART_DIR`, `MEMEX_PREFIX` — exactly what the formula's env script sets). The prefix is a literal in the postinstall, the daemon plist and the daemon program, so `build-pkg.sh` takes no `--prefix`: relocation would have to render it into all of them together |
| `/opt/memex/bin/<tool>` | **only** a vendored fallback the postinstall ACTIVATED (§2); a symlink into `libexec/vendor` |
| `/opt/memex/libexec/memex-local` | the CLI, byte-identical to `deploy/homebrew/bin/memex-local` |
| `/opt/memex/libexec/vendor/<tool>/bin/<bin>` | the pinned Colima, Lima (with its `share/lima`), kubectl, helm, mkcert — inert until activated |
| `/opt/memex/libexec/uninstall.sh`, `memex-daemon.sh` | the uninstaller; the daemon's program (§1d) |
| `/opt/memex/share/` | `deploy/homebrew/share/*` as the formula installs it: the launchd agent templates, the overlays, the sampler, `tools.manifest`, `tools.sh`; `share/helm` is the chart; `share/doc/{VERSION,BUILD}` |
| `/opt/memex/etc/tools.report` | the postinstall's probe report (§2) |
| `/etc/paths.d/memex` | `/opt/memex/bin` |
| `/Library/LaunchDaemons/com.memex.local.daemon.plist` | the OPTIONAL unattended-host daemon (§1d), only when that choice is selected |

The **LaunchAgents** for the portal's port-forward (`com.memex.local`) and the back-lease sampler
(`com.memex.local.backlease`) are *not* installed by the package: they are per-user, and the package
cannot know which user will run the stack or whether the machine is enrolled. They are installed exactly
as today — by `memex-local up` and `memex-local backlease enroll`, from the templates in `share/` — and
their `PATH` now ends with `/opt/memex/bin`, so an activated fallback is found by an agent too, after
every existing installation (§2's order, in launchd).

**Uninstall:** `memex-local down [--purge]` (the stack, as the user), then
`sudo /opt/memex/libexec/uninstall.sh` (the package: `/opt/memex`, `/etc/paths.d/memex`, the daemon, the
receipts). The user's `~/.memex-local`, the Colima VM and Homebrew are never touched.

**Signing** (`build-pkg.sh` steps 4–6). Notarization scans the payload and requires every Mach-O to carry
a Developer ID with the hardened runtime, so each vendored binary is **re-signed with our Developer ID
Application certificate** — the practice every product bundling third-party tools follows (Docker
Desktop, OrbStack). `limactl` is signed with `entitlements/limactl.plist` (`com.apple.security.virtualization`):
Lima drives the Virtualization framework, which a hardened-runtime binary may only do with that
entitlement; Lima's own releases are ad-hoc signed with exactly it. Lima's Linux guest agents are ELF and
are left alone. The product archive is signed with the Developer ID **Installer** certificate, submitted
with the App Store Connect API key, and stapled; `spctl -a -t install` must accept the result.

The archive is built, signed, notarized, stapled and self-checked under the build's temporary directory
and **moved to `--out` only after every check passed**, so a failure at any step leaves nothing behind.
**The three states a package can be in are in its NAME** — an artifact listing says it:

| Name | Meaning |
|---|---|
| `memex-local-<v>-<arch>.pkg` | signed, notarized, stapled — the only state that is released |
| `…-signed-unnotarized.pkg` | both certificates present, the notary key absent |
| `…-unsigned.pkg` | a signing input absent (`--sign never`, or §1f not yet provisioned) |
| `…-novendor-…` | built with `--vendor none`: no toolchain inside (tests, a developer who wants only the CLI) |

`build-pkg.sh` prints which input is absent (`unsigned — MEMEX_SIGN_INSTALLER_IDENTITY (CI secret
APPLE_DEVELOPER_ID_INSTALLER_P12) absent`) and **exits 0** for an unsigned build; an **error** while
signing or notarizing — inputs present, tool failed, status not `Accepted` — **exits non-zero** and leaves
no package behind. Never silent in either direction (pinned by `test/run-tests.sh`).

### 1b. A menu-bar app "Memex" (SwiftUI) — design

A status item in the menu bar, the face of the same `memex-local` CLI (the app shells out to
`/opt/memex/bin/memex-local`; nothing is reimplemented):

- **Status**: VM (Colima) up/down, k3s node Ready, portal health (`memex-local verify`'s verdict, polled),
  back-lease state (`backlease status`: enrolled, last reading age, heartbeat), the release channel and
  version in use (`channel.state`).
- **Actions**: Start (`up`), Stop (`down`), Update (`update`, with the channel), Open portal (the
  `https://memex.localhost…` URL), Logs (`logs`, a window tailing the output), Doctor (§2's table), and
  **Enroll back-lease** — a sheet with the control URL and the deployment id, pre-filled from the managed
  preferences (§3) when present, calling `backlease enroll`.
- **Updates via Sparkle** with a **signed appcast**: the app checks an `appcast.xml` this repository's
  release lane publishes beside the `.pkg` release assets; each item carries the EdDSA signature Sparkle
  verifies, and the download is the notarized `.pkg` of §1a, so one artifact serves the first install and
  every update. The Sparkle EdDSA private key is a fourth secret the maintainer mints
  (`SPARKLE_EDDSA_PRIVATE_KEY`), not needed until the app exists.
- **Distribution**: the app ships *inside* the §1a package as a third choice (`/Applications/Memex.app`,
  selected by default on a desktop Mac, deselected under an MDM profile that sets `Unattended`), signed
  with the same Developer ID Application certificate and notarized with the package. It is a
  `LSUIElement` (no Dock icon), launched at login by a per-user LaunchAgent the app registers itself
  (`SMAppService`), and it needs no privileges of its own: everything privileged already happened in the
  installer.
- **Not in this change set.** The app is implemented in a later pull request; this one gives it the
  installer, the resolver and the preferences it will call.

### 1c. Enterprise rollout — the same package, pushed by an MDM

An MDM (Jamf, Kandji, Mosyle, Intune, …) pushes the notarized `.pkg` as it pushes any other and, beside
it, a **managed-preferences profile** in the `com.memex.local` domain (`profiles/com.memex.local.mobileconfig.example`).
The keys and who reads them are in §3. A fleet machine therefore installs, resolves its toolchain, and —
with `Unattended` true — starts its stack at boot and enrolls as a back-lease host **without anyone typing
a URL or an id**. Nothing in the package is tenant-specific: the same bytes go to every Mac, and the
profile carries what differs. An MDM can also pre-select or hide Installer choices through the standard
choice-changes mechanism (`installer -applyChoiceChangesXML`, which the CI job exercises).

### 1d. Why not the Mac App Store

The App Store requires the **App Sandbox**, and the sandbox forbids what this product *is*: installing a
**LaunchDaemon** or any privileged helper, writing to `/opt`, `/etc/paths.d` or `/Library/LaunchDaemons`,
mounting host paths into a VM, and running vendored third-party tooling (Colima, Lima, kubectl, helm,
mkcert) as subprocesses outside the container. The precedent is uniform: **Docker Desktop, OrbStack and
Podman Desktop all ship outside the App Store** as notarized direct downloads, and where a VM product does
have an App Store edition — **Parallels Desktop App Store Edition, UTM on the App Store** — it is a
*reduced* edition (no shared networking modes, no privileged features) sold beside the full one. The
right channel for software that owns part of the machine is Developer ID + notarization, which gives the
user the same Gatekeeper assurance (identity, malware scan, revocation) without the sandbox. A fleet does
not need the App Store either: an MDM pushes a `.pkg` natively (§1c).

**The optional root LaunchDaemon — a machine that leases itself unattended.** A headless Mac mini has no
login session, so no LaunchAgent ever loads. The package therefore carries a second, **unselected** choice,
*Unattended host*: `/Library/LaunchDaemons/com.memex.local.daemon.plist` running
`/opt/memex/libexec/memex-daemon.sh` at boot **as the service user** (`UserName` — the stack, the VM and
`~/.memex-local` belong to a user, never to root), `KeepAlive`, logging to `/var/log/memex/daemon.log`.
Its postinstall writes the service user (managed preference `ServiceUser`, else the console user) into the
plist and drives launchd's **persisted** state from the managed preference `Unattended`, in both
directions: true → `launchctl enable` + bootstrap; anything else → bootout whatever an earlier
`Unattended=true` install loaded AND `launchctl disable`, so the installed plist (which carries
`RunAtLoad`/`KeepAlive`) cannot start at the next boot until someone enables it — a daemon that starts a
VM at every boot is armed by a declaration, not by installing. The daemon sets `MEMEX_HEADLESS=1` and runs
`memex-local up`: headless, the CLI holds the port-forward as a **background process** (its pid in
`~/.memex-local/port-forward.pid`) instead of a LaunchAgent no session exists to load, and runs its
usability verification against it like any other `up`. A failed `up` (an image pull before the network
settled, an unreachable registry) is **retried with a growing pause** and is never mistaken for done; once
`up` succeeds the daemon watches the forward's pid and exits when it ends, so `KeepAlive` re-runs `up`,
which re-creates what is missing. `memex-local down` stops the headless forward. 🚨 **Follow-up:** the
headless path is exercised by the test suites against stubs (`install_launchd` under `MEMEX_HEADLESS=1`,
the daemon postinstall's enable/disable) and the package's install by the CI runner; its first unattended
boot on a real Mac mini is the next change set's acceptance test. Nothing here claims that run has happened.

### 1e. Later, optional: our own VM on the Virtualization framework

OrbStack's path — a purpose-built VM on `Virtualization.framework` with its own rootfs, network and file
sharing — would remove Colima and Lima from the payload, start in a second instead of twenty, and own the
host-path mounts and the network stack outright. It is **never a precondition** for anything above: the
package, the probe-first resolver, the daemon, the menu-bar app and the MDM profile are all indifferent to
which VM runs k3s, because every one of them talks to `memex-local`, and `memex-local` talks to the VM
through one seam (`start_colima`, `colima_running`, the port-forward). When the time comes the seam gets a
second implementation and the manifest loses two rows.

### 1f. The signing inputs — what only the maintainer can mint

Signing needs an **Apple Developer Program** membership (the organisation's, with the legal entity
verified — a personal membership cannot issue Developer ID certificates for an organisation). Nothing in
this repository enrolls, mints or stores any of it; the lane runs **unsigned, loudly, until these seven
repository secrets exist** (`.github/workflows/macos-pkg.yml` → *Signing inputs* names each absent one in
a warning annotation and the step summary):

| Secret | What it is | How it is made |
|---|---|---|
| `APPLE_DEVELOPER_ID_APPLICATION_P12` | the **Developer ID Application** certificate + private key, `.p12`, base64 | Certificates, Identifiers & Profiles → Developer ID Application (CSR from Keychain Access); export the identity from Keychain Access as `.p12`; `base64 -i cert.p12 \| pbcopy` |
| `APPLE_DEVELOPER_ID_APPLICATION_P12_PASSWORD` | the `.p12` export password | chosen at export |
| `APPLE_DEVELOPER_ID_INSTALLER_P12` | the **Developer ID Installer** certificate + key, `.p12`, base64 | the same, type Developer ID Installer |
| `APPLE_DEVELOPER_ID_INSTALLER_P12_PASSWORD` | its export password | chosen at export |
| `APPLE_NOTARY_ISSUER_ID` | the App Store Connect API key's **issuer id** (a UUID) | App Store Connect → Users and Access → Integrations → App Store Connect API → Team keys |
| `APPLE_NOTARY_KEY_ID` | the API key's **key id** | same page; role *Developer* is sufficient for notarytool |
| `APPLE_NOTARY_KEY_P8` | the API key itself, `AuthKey_<id>.p8`, base64 | downloadable **once** at creation |

`gh secret set <NAME> --repo Systemorph/MeshWeaver.Deploy` for each. The job imports the two `.p12` into a
temporary keychain for its own duration, reads the identity names from it (they are never typed into the
workflow), writes the `.p8` to a file for `notarytool`, and deletes both at the end. The certificates are
valid for five years; the lane says when they are not (a `codesign`/`productbuild` error exits non-zero).
Later, for §1b: `SPARKLE_EDDSA_PRIVATE_KEY`.

## 2. Probe first, vendor second — "see what's there and install whatever is missing"

The package **carries** pinned Colima, Lima, kubectl, helm and mkcert as a **fallback**, and at install
time **probes the machine first**. One resolver, one precedence list, one manifest, three consumers:

- **`deploy/homebrew/share/tools.manifest`** — the single declaration: per tool its binary (with
  alternative absolute candidates for a keg-only formula such as `socket_vmnet`), its Homebrew formula,
  whether the formula names it directly, its **floor** (the minimum version an existing installation must
  report), the **vendored** version, the version probe; and per vendored tool and architecture the
  **asset**: kind, archive member, **sha256**, URL. Floors and pins change by editing this file, and a
  pull request that bumps a pin re-pins the checksum beside it.
- **`deploy/homebrew/share/tools.sh`** — the resolver both the CLI and the postinstall source
  (bash 3.2 clean). `tool_resolve <tool>` answers one line: *kind* (`existing` | `vendored` | `missing`),
  path, version, floor, *verdict* (`ok` | `below-floor` | `no-version` | `missing`), and the reason an
  existing copy was passed over.
- **The precedence list.** For each tool: (1) an **existing** installation on PATH — the developer's own
  Homebrew copy, a hand install — is used when its reported version meets the floor; it is never
  overwritten and never shadowed. (2) The **vendored** copy under `/opt/memex/libexec/vendor` is used only
  when nothing existing satisfies the floor — missing, or present but too old — and only when it proves
  its own version (a vendored binary that prints none is damaged or incompatible and is never activated).
  (3) Otherwise the tool is **missing** and the message names the floor, the formula and the installer's
  pinned version. The package's own `/opt/memex/bin` and `libexec/vendor` are **excluded** from
  "existing", so an activated fallback can never masquerade as the user's installation on the next probe.
- **The selection is made effective, not merely reported.** `tools_activate` builds a per-process
  **selection directory** — one symlink per resolved tool, to the existing copy or the vendored one — and
  prepends it to `PATH`, so the CLI and everything it runs (`colima` → `limactl`) execute exactly the
  resolved copies whatever else `PATH` holds: a below-floor Homebrew copy ahead of the fallback, a stale
  fallback link after a Homebrew copy appeared. The per-user agents (port-forward, sampler) and the daemon
  run the same resolver (`MEMEX_LOCAL_SHARE` in their plists), and every agent's `PATH` lists
  `/opt/memex/bin` last. `memex-local down` and the exit trap remove the selection directory.

**The postinstall** (`scripts/postinstall`) runs that resolution as root at install time and makes it
physical: for a tool resolved to its vendored copy it **activates** the fallback — a symlink
`/opt/memex/bin/<tool>` into `libexec/vendor` — and for a tool resolved to an existing copy it writes
nothing (and **removes** a fallback it activated earlier, so a Homebrew copy installed since takes over).
It writes `/opt/memex/etc/tools.report` and prints `changed: <n>`; a re-run of the installer, or the
menu-bar app's Update, is therefore **idempotent** — it repairs only what is missing and says `changed: 0`
otherwise. The probe PATH covers both Homebrew prefixes; `brew list --versions` is read for the report
(as the console user — brew refuses root). The same resolution runs inside the CLI: `preflight` calls
`tools_activate`, which prepends the bin directory of every tool resolved to **vendored** — and only those
— so the commands the CLI runs, and the ones they run (`colima` → `limactl`), find the fallback while
every existing installation stays the one in use. `need` refuses a tool below its floor **by name**, with
the floor, `brew install <formula>` and the installer's version.

**`memex-local doctor`** prints the table — per tool: which copy is used (`existing` / `vendored` /
`missing`), from where, at which version, the floor, the verdict, and why an existing copy was passed
over:

```
  TOOL          USES      VERSION   FLOOR    VERDICT     FROM
  colima        existing  0.10.3    0.8.0    ok          /opt/homebrew/bin/colima
  lima          existing  2.2.0     1.0.0    ok          /opt/homebrew/bin/limactl
  kubectl       vendored  1.37.1    1.31.0   ok          /opt/memex/libexec/vendor/kubectl/bin/kubectl  (existing /opt/homebrew/bin/kubectl is 1.20.0, below the floor 1.31.0)
  helm          vendored  4.3.1     3.16.0   ok          /opt/memex/libexec/vendor/helm/bin/helm  (nothing existing)
  …
```

**What the same probe covers beyond the tools.** `memex-local up` was idempotent before this change over
Colima's VM state (`colima_running` → start or skip) and the Helm release (`helm upgrade --install`), and
the postinstall never touches either: re-running the installer repairs the toolchain and nothing else, and
`up` repairs the stack and nothing else.

**What the package does NOT supply — the external prerequisites of `up`.** Ollama and `socket_vmnet` are
**probed and reported, not vendored** — Ollama ships its own installer and updater, `socket_vmnet` is a
root daemon of its own — and the install never fails on them: the report names them (`NOT RESOLVED — …
install it (ollama)`). `up` itself still requires **Ollama** (`preflight` needs it; `MEMEX_OLLAMA_MODEL=none`
skips only the large model's pull, the small e2e model is always pulled) and, in registry mode, the
**Docker CLI** and the **Azure CLI** with an `az login` for the image pull (`image_pull_acr`; the
source-checkout alternative needs the .NET SDK instead). The welcome and conclusion pages say so, and
`doctor` reports each. Vendoring the Docker CLI is a candidate for a later pin in the manifest; `az` is a
cask with its own installer and stays external.

**Privilege.** The postinstall runs as root, but the probe EXECUTES whatever `colima`/`kubectl`/… it
finds — in user-writable Homebrew prefixes. It therefore runs the probe through `sudo -u` as the console
user Installer is serving (else `nobody`) with the wide probe PATH, and keeps a trusted system PATH for
everything it does itself as root (links, the report). A filesystem error in that work fails the install
(`set -e`, an explicit check on `ln`): a report claiming a repaired toolchain that was not written is
exactly what must not happen.

**Manifest ↔ formula drift is a red test.** `deploy/homebrew/test/run-tests.sh` asserts that the
formula's `depends_on` set *is* the manifest's set of `direct` tools (Lima is `indirect`: Homebrew's colima
depends on it), that every manifest tool has a floor, and that `preflight` requires manifest tools only;
`deploy/macos/test/run-tests.sh` asserts the payload is the formula's three install sources file for file.
A Homebrew formula cannot carry a version floor, so the floors are enforced in the one place both channels
run: the CLI's `need`, reading the manifest the formula installs beside it.

**The tests the maintainer named**, all in `test/run-tests.sh` (postinstall level) and
`deploy/homebrew/test/run-tests.sh` (resolver level): every tool present and satisfying → nothing vendored
is activated, `changed: 0`; one missing → only that one activated; one present but below the floor → the
vendored copy is used and the report says `below the floor <floor>`; a re-run in the same state →
`changed: 0`, `bin/` unchanged; satisfying copies appearing later → the fallbacks are removed.

## 3. Managed preferences — the `com.memex.local` domain

| Key | Read by | Meaning |
|---|---|---|
| `ControlUrl` | `memex-local backlease enroll` (when `--control` is absent) | the control instance's https URL this machine reports to |
| `DeploymentId` | `backlease enroll` (when `--deployment` is absent) | this machine's `Deployments/<id>` record on the control instance |
| `RegistryUrl` | the menu-bar app (§1b); reported by `doctor` | the plugin registry the portal consumes (`memex-local registry <url>`) |
| `Channel`, `Profile` | the menu-bar app (§1b); reported by `doctor` | the release-channel label and the host profile (`mac-studio` / `mac-mini`) |
| `ServiceUser` | the daemon's postinstall (§1d) | the account the LaunchDaemon runs the stack as |
| `Unattended` | the daemon's postinstall (§1d) | `true` bootstraps the daemon at install; anything else leaves it unloaded |

`managed_pref <Key>` in the CLI reads `/Library/Managed Preferences/com.memex.local` (what an MDM writes)
first, then the user domain (`defaults write com.memex.local <Key> <value>` by hand). **An explicit flag
always wins**; with neither flag nor preference the command refuses, naming both. The example profile is
`profiles/com.memex.local.mobileconfig.example` — every value in it is a placeholder.

## 4. CI — `.github/workflows/macos-pkg.yml`

Path-filtered to `deploy/macos/**`, `deploy/homebrew/**`, `deploy/helm/**` and itself, like `homebrew.yml`
(not a required check: a pull request touching none of them has nothing for a macOS runner to measure).

| Job | Runner | Event | What |
|---|---|---|---|
| `lint` | Linux | every | parse + `shellcheck -S warning` on every shipped script; the manifest's asset rows are whole (both arches, 64-hex sha256, https) |
| `build` | macOS | every | both suites under `/bin/bash` 3.2; both architectures built **unsigned** with the real vendored toolchain (`actions/cache` keyed on the manifest's hash); the runner-arch package **installed** with `installer` as root, `tools.report` read, `doctor` run, nothing in Homebrew's prefix touched, the daemon absent (not chosen), re-installed → `changed: 0`, the daemon chosen through `-applyChoiceChangesXML` → present and **not loaded**, uninstalled → nothing left; both packages uploaded as artifacts (`-unsigned`) |
| `release` | macOS | `push` to `main` | the seven secrets enumerated by name (absent ones → a warning annotation + the step summary, `signing=false`); when present: a temporary keychain, identities read from it, both architectures built signed + notarized + stapled and attached to the release `memex-local/v<version>` of this repository, its tag pinned to the run's commit (`--target`); otherwise the same unsigned artifacts, and **no release** |

The secrets are consumed only by `release` (`if: github.event_name == 'push'`), which the platform's
`check-pr-secret-preflight.py` excludes from pull-request reachability; every job carries a literal
`timeout-minutes` ≤ 45, no `continue-on-error`, no input-shaped `if:`. A macOS runner has no nested
virtualisation: `memex-local up` is proven on a real Mac, not here.

## 5. Building and testing locally

```bash
deploy/macos/build-pkg.sh --version 0.3.$(git rev-list --count HEAD) --arch arm64 --out dist   # unsigned (no identities) — downloads the toolchain once into dist/.vendor-cache
deploy/macos/build-pkg.sh --version 0.3.1 --arch arm64 --out dist --vendor none --sign never    # the CLI only, ~300 KB, for a quick look
deploy/macos/build-pkg.sh --version 0.3.1 --arch x86_64 --out dist --plan                       # the decision only: vendor, sign, notarize, the file name
/bin/bash deploy/macos/test/run-tests.sh          # 102 tests — macOS only (pkgbuild, pkgutil, plutil)
/bin/bash deploy/homebrew/test/run-tests.sh       # 134 tests — the CLI, the resolver, the manifest↔formula drift
pkgutil --expand-full dist/memex-local-*.pkg /tmp/x && ls /tmp/x/memex-local-cli.pkg/Payload/opt/memex
sudo installer -pkg dist/memex-local-<v>-arm64-unsigned.pkg -target /   # a real install (Gatekeeper refuses an unsigned pkg by double-click; `installer` does not)
```

To sign locally, export the two identities' names and the notary key path as `MEMEX_SIGN_APP_IDENTITY`,
`MEMEX_SIGN_INSTALLER_IDENTITY`, `MEMEX_NOTARY_KEY_PATH`, `MEMEX_NOTARY_KEY_ID`, `MEMEX_NOTARY_ISSUER_ID`
(the header of `build-pkg.sh` maps each to its CI secret).

## 6. Files in this directory

| Path | What |
|---|---|
| `README.md` | this page — the design of record and the manual |
| `build-pkg.sh` | the build: stage → vendor (sha256) → sign → `pkgbuild` ×2 → Distribution → `productbuild` → notarize → staple → self-check |
| `Distribution.xml.in` | the product archive's definition: title, `hostArchitectures`, macOS ≥ 13 check, the two choices, welcome/licence/conclusion |
| `resources/welcome.html`, `conclusion.html` | the Installer pages; `license.html` is generated from the repository's `LICENSE*` at build time |
| `scripts/postinstall` | the CLI component's postinstall — the probe-first activation (§2) |
| `daemon/postinstall` | the unattended-host component's postinstall (§1d) |
| `launchd/com.memex.local.daemon.plist` | the LaunchDaemon template (`__USER__`) |
| `libexec/memex-daemon.sh`, `libexec/uninstall.sh` | the daemon's program; the uninstaller |
| `entitlements/limactl.plist` | the virtualization entitlement `limactl` is re-signed with |
| `profiles/com.memex.local.mobileconfig.example` | the managed-preferences profile an MDM pushes (§3), placeholders only |
| `test/run-tests.sh` | the package suite (§4 `build` runs it under bash 3.2) |
| `../homebrew/share/tools.manifest`, `tools.sh` | the manifest and the resolver — in `share/` so both channels ship them |

Related: [`deploy/homebrew/README.md`](../homebrew/README.md) (the developer channel and the CLI),
`Hosting/BackLease` in MeshWeaver.Plugins (what a leased-back Mac reports), `deploy/backlease/` (a Mac's
first attach as a build host, which `backlease enroll` adopts).
