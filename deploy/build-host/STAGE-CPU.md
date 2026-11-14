# The CPU a stage is allowed

Each stage of a build-host run runs in its own `bwrap` sandbox (`.github/scripts/build-run.py`, `Sandbox.argv`). The
runner tells every stage's .NET runtime how many CPUs that stage may use, as `DOTNET_PROCESSOR_COUNT`. It does not leave
the runtime to find out for itself, because inside the sandbox it cannot.

## Why the runtime cannot see the limit

The sandbox unshares the cgroup namespace (`--unshare-cgroup-try`). It mounts a fresh `/proc` and `/dev`, but no `/sys`.
The .NET runtime reads its CPU limit from the cgroup files under `/sys/fs/cgroup`. With no `/sys` it finds no limit, and
`Environment.ProcessorCount` returns the VM's whole CPU count. The kernel still enforces the runner container's
`limits.cpu`.

The same runner, measured locally on Colima (cgroup v2, a container with `--cpus 4` on an 8-CPU VM):

| what the process sees | `Environment.ProcessorCount` |
|---|---|
| `/sys` visible | 4 |
| `/sys` not mounted (what the sandbox does) | 8 (the VM's count) |
| `/sys` not mounted, `DOTNET_PROCESSOR_COUNT=4` | 4 |

On a Mac build host the VM has 16 CPUs and each runner replica is limited to 4 (`deploy/backlease/build-host-mac.values.yaml`).
So every stage sized itself for 16 CPUs. Core's NodeType compile lane (`IoPoolOptions.CompileCpu` =
`Environment.ProcessorCount`) ran 16 compile slots on 4 CPUs, as did the GC heaps and the thread pool's minimum. With
`stageSlots: 4`, up to four such stages ran inside the same 4 CPUs. The build-mac gate overran its cap
(MeshWeaver.Plugins#4478, seen on MeshWeaver.Plugins#5400).

The allow-list (`ENV_ALLOW`, behind `--clearenv`) does not pass `DOTNET_PROCESSOR_COUNT` through from the runner's own
environment. That is deliberate: the number belongs to each stage, not to the host process.

## The rule

The runner sets `DOTNET_PROCESSOR_COUNT` in the explicit environment of every sandboxed process
(`stage_processor_count`, `Host.stage_processor_env`):

- **A declared count wins.** `MW_BUILD_STAGE_PROCESSOR_COUNT` (chart `buildHosts.stageProcessorCount`) is a whole number
  in [1, 1024], and every stage gets it. An invalid declaration stops the runner at start, and the refusal names the
  variable. It is never clamped.
- **Otherwise** the count is `max(1, floor(cgroup CPUs / stages that may run at once))`. The CPUs come from the
  container's cgroup (`cgroup_capacity`: v2 `cpu.max`, or the v1 quota). The number of stages that may run at once is
  read from the scheduler's declared rules (`next_stages`):
  - a test stage of a `parallel` run gets the host's `MW_BUILD_STAGE_SLOTS`;
  - a `build` stage always runs alone, so it gets 1;
  - every stage of a serial run gets 1;
  - the warm build, the warm NuGet fill and a prepared input run with no stage beside them, so they also get 1.
- **A cgroup with no CPU limit** gives the visible CPU count. **An unreadable cgroup** falls back to the visible count
  too. The log line names which case applied. It never presents an unmeasured count as a measured limit.

Each stage's log carries one line naming the count and its source, right after the stage's header line. For example:

```
[00:00:00] DOTNET_PROCESSOR_COUNT=1 — the cgroup's 4 CPU(s) / 4 stage(s) at once
[00:00:00] DOTNET_PROCESSOR_COUNT=2 — declared MW_BUILD_STAGE_PROCESSOR_COUNT=2
```

For a NodeType compile, core's IoPool reports its compile lane's size in the same stage log (its `CPU lane` line). That
size follows `DOTNET_PROCESSOR_COUNT`, so on a 4-CPU replica with four slots it should read 1 for a test stage and 4 for
the build stage. That reading is to be confirmed on the next build-mac gate run. The self-test proves only what the
runner hands to the stage.

## Proof

These cases are in `build-run.py --self-test`, under the `cpu/` prefix:

- 4 / 4 gives 1; 4 / 1 gives 4; 16 / 4 gives 4;
- with no CPU limit, the visible count divided by the slots;
- with an unreadable cgroup, the visible count divided by the slots, and the source line says so;
- the floor is never below 1;
- a declared count wins;
- an invalid declaration is refused, and so is a host that declares one at start.

The end-to-end cases record what the sandbox was handed and what the stage process itself saw:

- a fanned-out run on 4 CPUs with 4 slots gives the build stage 4 and each test stage 1, and logs exactly one line per stage;
- a serial run gives every stage 4;
- a declared 2 gives every stage 2.

The negative control is the same run with the injection switched off. The sandbox is then handed no
`DOTNET_PROCESSOR_COUNT`, and the stage sees none. The real `bwrap` argv sets the host's value after `--clearenv`, and
a `DOTNET_PROCESSOR_COUNT` in the runner's own environment never reaches a stage.
