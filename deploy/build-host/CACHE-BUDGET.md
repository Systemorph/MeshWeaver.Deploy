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

**Since 2026-11-11 (the owner's go): an 80 GiB budget over the unchanged 20Gi claim, with the warm tree back.**
With the 18 GiB budget the claim allowed, the deployment tree (~20 GiB) could never be kept, and every run on the Macs
built COLD ("workspace: COLD worktree … no deployment tree yet").

A `local-path` claim's request is nominal: nothing enforces it, and a bound claim cannot be expanded (a changed
`spec.resources` makes the next `helm upgrade` fail). So the budget is decoupled from the claim, not the claim raised:

- `cache.sizeEnforced: false` (only accepted for `storageClass: local-path`) lets `cache.maxGi` exceed `cache.size`;
  the runner's budget is then the only bound;
- the Mac values default to `maxGi: 80` with `treeGi 24, slicesGi 30, nugetGi 8, refsetsGi 3, preparedGi 2` (67 GiB,
  13 GiB margin);
- each Mac's OWN budget is sized from its build disk by `memex-local buildhost apply`: `min(80, disk × 85 % − images 12
  − system 4 − one run 20)`, at least 10. One run, plus the 15 % the claim guard keeps free, always still fits. The tree
  share is 24 GiB only when the budget is at least 45 GiB, otherwise 0: a small disk builds cold rather than thrash a tree it
  cannot keep. Examples: a 197 GiB disk gets 80 (3 runs), 100 GiB gets 49 (1 run), 60 GiB gets 15 (1 run, no tree);
- `buildhost apply` writes that sizing (runners, resources, stage slots, the budget and its shares) to
  `~/.memex-local/buildhost-follow/<profile>/sizing.yaml`. The release follower layers that file over the Mac values on
  every full upgrade. Before this, a follower upgrade reset a Mac to the values file's defaults.

Previously (the 18 GiB budget, 2026-11-11 morning): The build-run.py self-test `budget/the measured Mac` pins this arithmetic.
