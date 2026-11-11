# The build host's cache budget

A standing build host keeps one cache volume (`/cache`, `MW_BUILD_CACHE`) that every replica on the node mounts. The cache
now has **one total budget**, and each area that the runner can evict has a **share** of it. The chart refuses shares that
would sum above the budget, so the cache stays within its declared size by construction. The runner is
`.github/scripts/build-run.py`, section *the host's cache is BUDGETED*. The chart side is
`deploy/helm/templates/build-host/_build-hosts.tpl`.

## Why

Readings from the second Mac build host on 2026-11-11, taken read-only:

| what | size |
|---|---|
| the VM's data disk (`/dev/vdb1`) | 98 GiB, 59 used, 35 free |
| the cache claim `build-cache-mac` | **declared 20Gi**, holding **43 GiB** |
| `deploy/mac-tree` (the deployment tree) | 20 GiB |
| `module-slices` | 14 GiB (its bound was 32, more than the whole claim) |
| `nuget` | 6.1 GiB |
| `refsets` | 2.0 GiB |
| `prepared` | 1.4 GiB |
| `git` (the masters) | 0.4 GiB |
| containerd images | ~15 GiB (four generations of three images; the image GC is a separate change) |

With 34 GiB free, the disk guard (Deploy #248) refused every claim: one run needs `MW_BUILD_RUN_DISK_GIB` = 20, and
`MW_BUILD_DISK_HEADROOM_PERCENT` = 15 % of the disk must stay free. The claim's class is `local-path`, which never enforces
a claim's capacity. The only bounded area was the slices, and their bound was larger than the claim.

## What `deploy/<lane>-tree` is

This is the warm workspace (MeshWeaver.Plugins `Hosting/BuildQueue.md` §7.3b, `Hosting/OneBuildTree.md`). It holds **one
persistent worktree per repository** for the host's lane: `<cache>/deploy/<lane>-tree/<repo>`. Each is a `git worktree` of
the bare master `<cache>/git/<repo>.git`, built in place by the tip's `warmBuild`, with `obj/` and `bin/` kept. Beside it:

- `.platform/<repo>` is core's worktree, built in place for that repository's builds;
- `.host/<repo>/` holds the state, the integrity manifests and the admin path;
- `.locks/<repo>.lock` is the lock. There is one writer (exclusive) and any number of readers (shared, held for a run's
  whole life).

**Generations no longer pile up:**

- the older runner kept one keyed tree per platform set and image (`<lane>-<16-char key>`). `retire_legacy_deployments`
  removes those once they are unused and quiet;
- the §7.3 copies under `<cache>/shared` are removed by `retire_legacy_shared`;
- a new platform set rebuilds the one tree whole, in place.

**What still grew with no bound** until this change:

1. **Every repository the host ever ran keeps its tree forever.** `warm_main_when_idle` warms every repository listed in
   the deployment directory, and nothing removed one.
2. **`obj/` and `bin/` are kept across in-place advances.** A project deleted upstream keeps its outputs until the next
   whole rebuild, which only a change of build identity triggers.
3. The MeshWeaver.Plugins tree alone is about 18–20 GiB with `obj/` and `bin/`, so on a 20Gi claim it can never fit beside
   anything else.

## The budget

| variable (runner) | chart value | default |
|---|---|---|
| `MW_BUILD_CACHE_MAX_GIB` | `buildHosts.cache.maxGi` | the declared `cache.size` less 10 %, never above it; with no size and no `maxGi` (a claim made elsewhere, or `cache.medium: host`) there is no budget and only the slice bound applies, as before |
| `MW_BUILD_CACHE_TREE_GIB` | `cache.shares.treeGi` | 50 % — the deployment trees. **0 keeps no tree and starts no warm build** |
| `MW_BUILD_SLICE_CACHE_MAX_GIB` | `cache.shares.slicesGi` (older name: `sliceCacheMaxGi`) | 20 % (at least 1; 32 when there is no budget) |
| `MW_BUILD_CACHE_NUGET_GIB` | `cache.shares.nugetGi` | 15 % |
| `MW_BUILD_CACHE_REFSETS_GIB` | `cache.shares.refsetsGi` | 5 % |
| `MW_BUILD_CACHE_PREPARED_GIB` | `cache.shares.preparedGi` | 5 % |

The 10 % margin and the 5 % no share takes hold what no area covers: the masters, `outputs`, the plugins layer and the
markers. Those are measured and named, never evicted by the budget.

**Invariants, enforced by the chart (`helm template` fails and says why):**

- the budget is a whole number of GiB, never above the declared size;
- no share is above the budget;
- the shares together are not above the budget;
- shares without a budget are refused rather than ignored.

The runner adds one more safety net. If the variables it is given sum above the budget, it scales them down
proportionally and says so. `deploy/aks/scripts/test-build-hosts.sh` §3h renders the fixtures and the **shipped** Mac
values, and checks every share against the budget. A negative control proves that the check fails.

## Eviction

A budget pass runs at most every 10 minutes per host, while it is idle, and before the disk guard's reading. Only one
replica runs it at a time (`<cache>/.cache-budget/.lock`). In each area the least recently used entries go first, down to
90 % of the share. The runner never evicts:

| area | unit | never evicted while |
|---|---|---|
| slices | one module output | a reader holds its lock, or it was used in the last 10 min (`bound_slice_cache`, unchanged) |
| tree | one deployment tree (its platform tree and host state with it), oldest **use** first. A run's reader stamps `.host/<repo>/used` | its writer lock is not free: a run reads it, or a warm build writes it. Removal deregisters both worktrees from their masters (`worktree prune`). A tree removed for its share is **not rebuilt** at the same build identity and share for a day (`<cache>/.cache-budget/trees/`). A rebuild would only be removed again |
| refsets | one staged reference set | it is the one this host's own images key, or it was used within a run's deadline + 10 min (`stage_refset` stamps `.complete`) |
| prepared | one installed prepared input | it was used within a run's deadline + 10 min (each run that reads one stamps `.complete`) |
| nuget | one package, oldest first | any run is in flight on this cache (a disk reservation exists), any deployment tree is held, a warm build is running, or the package is younger than an hour. This check runs under the disk admission lock, so no replica claims meanwhile. The warm-NuGet and package-seed markers are removed with the packages, so the closure is prepared again |

Sizes come from the entries' completion markers and the trees' integrity manifests. They are recorded host-side in
`<cache>/.cache-budget/sizes.json`, never inside an entry a stage reads, so a pass does not hash or walk an unchanged
tree.

## What the disk guard now says

The refusal line carries the cache by area against its budget, and the rest of the disk when the cache is on the same
filesystem. A reading then needs no ssh:

```
disk: claiming nothing — the work volume (/work) has 34.0 GiB free of 97.9 GiB; a run needs 20 GiB (MW_BUILD_RUN_DISK_GIB)
and 15 % must stay free for the kubelet's eviction threshold (MW_BUILD_DISK_HEADROOM_PERCENT) — retried every idle pass;
the cache holds 43.9 GiB of its 18.0 GiB budget (MW_BUILD_CACHE_MAX_GIB) — tree 20.0/0.0, slices 14.0/5.0, nuget 6.1/8.0,
refsets 2.0/2.0, prepared 1.4/2.0, other 0.4 GiB; the rest of that disk (work volumes, images, system) holds 20.0 GiB
```

The budget pass runs before the guard reads the disk, so this line appears only when the budget alone did not make room.

## The Mac (`deploy/backlease/build-host-mac.values.yaml`)

The Mac's claim stays at 20Gi. A `local-path` claim cannot be resized, and raising `size` would only make the next
`helm upgrade` fail. The budget is 18 GiB. The MeshWeaver.Plugins deployment tree does not fit in that, so the Mac keeps
**no tree**:

- `treeGi: 0`: no warm build starts, and every run takes a cold worktree off the master, which the run says;
- it spends the budget on what does fit: `nugetGi` 8 (6.1 measured), `slicesGi` 5, `refsetsGi` 2 and `preparedGi` 2. That
  is 17 GiB, leaving 1 GiB plus the 2 GiB margin for the masters.

To bring the warm tree back, the cache must be declared large enough for it: a new claim, or `cache.medium: host` with an
explicit `maxGi`. That is a decision about the disk, made in a pull request, never a silent overrun.

**The first budget pass on a host whose cache already holds the measured 43 GiB:**

- the tree goes whole (its share is 0): 20 GiB;
- the slices go down to the low-water mark of their 5 GiB share (4.5 GiB): about 9.5 GiB;
- NuGet (6.1 of 8), the reference sets (2.0 of 2) and the prepared inputs (1.4 of 2) stay. A reference set the host's
  images no longer key goes if it is past its deadline.

That frees about 29.5 GiB. The cache drops to about 14.4 GiB and the disk to about 63.5 GiB free. The guard then passes:
63.5 − 20 ≥ 14.7 for the first run, and 63.5 − 2 × 20 = 23.5 ≥ 14.7 for a second replica's run. A third replica's
reservation does not fit (3.5 < 14.7), so two runs at a time is what this disk holds with the current images.

The build-run.py self-test `budget/the measured Mac` pins this arithmetic.
