# Fleet bootstrap for the device-management layer (Intune)

`fleet-bootstrap.sh` brings a Mac Studio of the fleet to "joined". The device-management layer runs it
**as root** on the fleet's Mac Studio group (an Intune macOS shell script, run as the signed-in system
context, repeated on Intune's schedule). Every run is idempotent: it does only what is missing, prints
one line per step, and appends the same lines to `/Library/Logs/MeshWeaver/fleet-bootstrap.log`. Design:
MeshWeaver.Plugins `Hosting/MacStudioFleet.md` §2b and §2b.2.

| Step | What it does | Waits (exit 0) when |
|---|---|---|
| console user | the person the Mac's memex runs as (`stat -f%Su /dev/console`) | no one is logged in |
| Homebrew | `/opt/homebrew`, owned by that person (the documented untar install, no prompt) | — |
| tools | `colima docker kubectl helm ollama tailscale` and `systemorph/memex/memex-local`, as that person | — |
| tailscaled | a ROOT LaunchDaemon `com.meshweaver.fleet.tailscaled` on `/var/run/tailscaled.socket` (the Tailscale app does not run before login; this does). memex-local uses this socket by itself | — |
| network | `tailscale up` with a single-use pre-auth key from `FLEET_PREAUTH_KEY_FILE` (read, deleted, never printed) | no key yet (control mints it at enrolment) |
| memex | registry mode, `memex-local up`, then `memex-local fleet join --code-file ~/.memex-local/enrol/<id>.code` | no `ControlUrl` yet, or the approval and the enrolment code are pending (the link is in the log and in `~/.memex-local/enrol/<id>.link`) |

**Configuration** comes from the managed-preference domain `com.memex.local`, deployed as a configuration
profile. Its keys are `ControlUrl`, `DeploymentId`, `RegistryUrl` and `LoginServer`. The environment
variables `FLEET_CONTROL_URL`, `FLEET_MACHINE_ID`, `FLEET_REGISTRY_URL` and `FLEET_LOGIN_SERVER` override
them. Nothing secret is configured.

**Registering it**: upload this file's content at a tagged version as the Intune shell script. Set it to
run as the signed-in system context, repeat it (for example daily), and assign it to the Mac Studio group.
The version is `FLEET_BOOTSTRAP_VERSION` in the file; the run log names it on every line.

**Tests**: `deploy/macos/test/run-tests.sh` (the plist lints, the console-user and machine-id rules, and
a dry run on a bare Mac that plans every step, changes nothing and waits). `FLEET_BOOTSTRAP_DRY_RUN=1`
prints every changing command instead of running it.
