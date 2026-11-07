# A Mac as a fleet build host

A Mac that leases capacity back to the fleet runs the fleet's build jobs in a Colima profile of its own
(`build`: a Virtualization.framework VM, containerd, k3s, the `build-host-mac` runners under KEDA). The
portal that `memex-local` runs stays in its own profile (`default`), and Ollama runs natively on the host.

| What | Where |
|---|---|
| Size the build VM and the runners from the host | `memex-local buildhost plan \| apply [--resize] \| status` (`deploy/homebrew/bin/memex-local`) |
| First attach: AppArmor profile, Secrets, the `build-host-mac` release | `first-attach.sh` (this directory), header for the steps |
| The runners' values | `build-host-mac.values.yaml` |
| Fleet jobs at the lowest priority | `deploy/homebrew/share/buildhost-priority.sh` (below) |

## Fleet jobs run at the lowest priority on a leased Mac

The owner's own work, the local portal and Ollama always win when the machine is contended. The builds
still use every core that is idle. Both `memex-local buildhost apply` (so `fleet join` too) and
`first-attach.sh` load a launchd agent, `com.memex.local.buildhost-priority`, that switches the build VM's
process between two macOS task policies:

| Mode | Policy | When |
|---|---|---|
| **full** | the VM's default policy: its vCPUs take every idle core | nothing else wants the machine |
| **yield** | `PRIO_DARWIN_BG` (`taskpolicy -b -p <pid>`): CPU in the lowest band, disk IO throttled | everything except the build VM uses more than two cores (`MEMEX_BUILD_PRIO_YIELD_AT`, default 200 %), or Ollama is computing (≥ 10 %) |

The agent goes back to full after the rest has stayed under 150 % for 15 s. It samples every 2 s. It finds
the VM process as the one holding the build profile's boot disk open, never by name, because the portal's
VM runs the same executable. It follows the VM across restarts. When the agent stops, it gives the VM its
default policy back, so a removed governor never leaves the builds capped.

```
memex-local buildhost priority status    # the agent, mode, VM process, thread priorities (4 = yield, 31 = full)
memex-local buildhost priority install   # (re)load it; apply does this
memex-local buildhost priority off       # remove it; the VM runs at its default policy
```

State: `~/.memex-local/buildhost-priority.state`. Log, transitions only: `~/.memex-local/buildhost-priority.log`.
Overrides set in the installing shell (`COLIMA_HOME`, `MEMEX_BUILD_PRIO_*`) are written into the agent's
environment.

**Headless Macs.** On a Mac without a GUI login session (the `.pkg`'s *Unattended host*, see
`deploy/macos/README.md`), no LaunchAgent can load. There the governor runs as a background process of the
service user, with its pid in `~/.memex-local/buildhost-priority.pid`. The unattended daemon
(`memex-daemon.sh`) starts it again after every boot once it has been installed. `MEMEX_HEADLESS=1` forces
this mode.

### Why a switch, and not nice or a fixed background policy

All of these were measured on an 18-core M5 Max running macOS 27.2 (2026-11-07):

| Option | Measured | Verdict |
|---|---|---|
| `nice` / `renice -n 20` | 18 busy loops at nice 20 got the same CPU as 18 at nice 0 (54.7 vs 55.2 core-seconds in 8 s). `taskpolicy -t`/`-l` (throughput and latency tiers) gave the same result | no effect: the macOS scheduler ranks by QoS, not by nice |
| QoS clamp `taskpolicy -c utility` | yields under contention (3.3 vs 117 core-seconds) and fills an idle host (16.6 of 18 cores) | the right policy, but it can only be set at spawn and only children inherit it. The VM is an XPC service that launchd starts for `limactl`, so a clamp on `colima start` reaches the hostagent (PRI 20) and not the VM (PRI 31). Nothing clamps a running process |
| `PRIO_DARWIN_BG` all the time | a 16-vCPU VM got 4.7 cores on an idle host (15.5 at its default) | yields fully, but it would cut idle build capacity by about 70 % |
| **the switch (this agent)** | 18 busy host loops vs a busy 16-vCPU VM: VM 0.22 cores and host 16.8 when yielding, against VM 7.7 and host 8.8 at the default policy. Idle: the VM keeps every core | the owner wins under contention, and the builds fill the machine otherwise |

Interactive applications already run above the VM's default band (user-interactive and user-initiated QoS
rank above PRI 31). What the VM used to compete with as an equal was other default-band work: terminal
builds, the portal's VM, and Ollama. A Homebrew `ollama serve` runs at PRI 20, *below* the VM, and
since Ollama 0.40 its inference runs in `…/lib/ollama/llama-server`. The agent counts every process
whose command line names Ollama.

Inside the build VM there is nothing more to lower. The host schedules the whole VM as one process, so
priorities inside the guest do not reach the host, and the VM runs only k3s, KEDA and the build runners.
