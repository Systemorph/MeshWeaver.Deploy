# The fleet registry's maintenance window

The hosted registry (`templates/registry/`, `registry.enabled`) runs off-the-shelf `distribution`. A
manifest `DELETE` frees no storage: the blobs stay until `registry garbage-collect` runs, and a
collect that runs while the registry accepts pushes can delete the blobs of an upload in progress.
So the collect runs only inside a **read-only window**, and the window is applied by the fleet
cleanup run that holds the registry-maintenance lease (MeshWeaver.Plugins
`Hosting/FleetRegistryCleanup`; MeshWeaver.Plugins#4370, slice 3, the owner's decision 1A).

## The chart values

| key | default | what it renders |
|---|---|---|
| `registry.maintenance.readOnly` | `false` | `true` → `storage.maintenance.readonly.enabled: true` in `memex-registry-config`, and the registry pod template carries `memex-registry/maintenance: read-only`. The ConfigMap's checksum is on the pod template, so the apply ROLLS the registry pods. Pushes and DELETEs are refused; pulls keep working |
| `registry.maintenance.garbageCollect` | `""` | a run id → ONE Job `memex-registry-gc-<run>` running `registry garbage-collect /etc/registry/config.yml` with the registry's image, configuration, ServiceAccount and workload identity, `backoffLimit: 0`. Refused unless `readOnly` is `true` |
| `registry.maintenance.gcDeadlineSeconds` | `1500` | the collect Job's `activeDeadlineSeconds` (60..1500). The window runs inside one operator job, which is cut at 45 minutes. Run to their bounds, both pod-by-pod verifications (300 s each), the collect (1500 s plus a minute) and the collect's deletion (120 s) still leave the restore inside that cut |

With none of them set the chart renders byte-identically to a chart that does not know them
(`deploy/aks/scripts/test-registry-maintenance.sh` holds that, and the rest of this table).

🚨 **No `--delete-untagged`.** The fleet cleanup deletes manifests explicitly, by digest, and keeps
the untagged per-platform manifests beneath every kept index. Those are exactly the children
upstream's `--delete-untagged` has been reported to remove. The collect frees only blobs that no
manifest references.

These values are never a standing setting in a record or an overlay. Only the window script sets
them, and it sets them back.

## The window: `hosting-registry-maintenance`

`hosting-registry-maintenance window --namespace <ns> --release <release> --run <run id>` runs these
steps in this order:

1. **Apply read-only.** It runs `helm upgrade` on the release that renders the registry, with
   `readOnly=true`, over the release's OWN values (`--reset-then-reuse-values`). First it renders the
   upgrade against the cluster (`--dry-run=server`) and compares it with the live manifest. If the
   upgrade would change anything but the window's own keys, the window refuses before any write.
   That happens when the chart in the operator image has moved since the release was last applied,
   and the window must never carry a portal roll. Reconcile the record first.
2. **Verify read-only, pod by pod.** Every live registry pod must:
   - carry the current template's `checksum/config`, which is the read-only template;
   - be Ready;
   - not be terminating.

   There must also be at least as many such pods as the Deployment wants. During a rolling update,
   read-write and read-only pods serve side by side, so the collect may start only when no read-write
   pod is left, not when the apply returns.
3. **Collect.** It applies the same values plus `garbageCollect=<run>`. The ConfigMap does not
   change, so the registry pods do not roll. It follows the Job to `Complete` and reports
   distribution's closing line (`N blobs marked, M blobs and K manifests eligible for deletion`). A
   collect that completes without that line is a failure: its outcome is not claimed from silence.
4. **Apply read-write.** First it deletes every collect Job of the release in the foreground and
   confirms that no collect pod is left. Then it applies `readOnly=false`, `garbageCollect=""`.
5. **Verify read-write**, pod by pod, the same way.

Its facts, in order: `registry_window=read-only`, `registry_gc=complete`,
`registry_gc_summary=<line>`, `registry_window=read-write`, and last `registry_window=closed`.

**Read-write is restored on every exit path.** Any exit before `closed` runs the restore (steps 4
and 5) and ends red. Such exits include a refusal, a failed apply, a pod that never became read-only,
a failed collect, a shell error, or SIGTERM at the job's deadline. The restore reports one of three
facts:

- `registry_window=restored`: read-write is back and verified.
- `registry_window=restore_failed`: it is not. The registry may still refuse pushes.
- `registry_window=unchanged`: the window was refused before its first write, so there was nothing to
  restore.

The restore never applies read-write while a collect pod may still run. A collect beside a read-write
pod is the one outcome worse than a registry left read-only. So a collect pod that cannot be shown
gone is `restore_failed`, and the registry stays read-only.

`hosting-registry-maintenance restore --namespace <ns> --release <release>` is the same restore on
its own. The control plane runs it when the window's own job could not report: it was killed, or it
was not observed to an outcome.

The behaviour suite (`deploy/hosting-operator/test/run-tests.sh`, section
`hosting-registry-maintenance`) holds the order of the writes, the collect waiting for every pod, and
the restore on each exit path, with stubs for `helm` and `kubectl`.

## During the window

- **Pulls work.** Fleet rolls pull as usual. The lease refuses every image-applying action anyway.
- **Pushes are refused.** The CD image mirror and the bundle publish get the registry's read-only
  refusal while the window is open. Today they report it as a red run. Teaching them to recognise it
  and wait for the window to close is still owed (MeshWeaver.Plugins#4370).
- **Cost: two registry rolls per run**, one into the window and one out of it.

## Break glass: read the lease before any hand `helm upgrade`

The registry-maintenance lease holds back image-applying instance actions only. A hand-run
`helm upgrade` or `helm rollback` bypasses it, and two things go wrong:

- It can apply an OLD, otherwise unreferenced tag whose migration image a running cleanup is about to
  delete.
- It replaces the window's values: the registry comes back read-write while a collect runs.

So, before any break-glass `helm upgrade` or `helm rollback` of a release on the cluster that hosts
the fleet registry:

1. Read the lease on the control instance: `{operational space}/RegistryMaintenance/fleet-registry`
   (NodeType `Hosting/RegistryMaintenanceLease`; `get` it through the control instance's MCP server).
2. **While it is held and unexpired, run no hand `helm upgrade` or `helm rollback`.** Wait until the
   holder releases it, or until it lapses at its recorded `expiresAt`.

This is the procedural close of the "migration Job outside any instance action" hole (the owner's
decision 2A on MeshWeaver.Plugins#4370). It is the same class of hole as the tag writers outside
instance actions.

## Upload throughput: the ingress's HTTP/2 receive window

A layer push that "hangs" against the hosted registry is usually a push that is running at a
fraction of the link's speed. Measured on the fleet registry, 2026-11-14:

- The ingress controller offers HTTP/2, and Go clients (`docker buildx imagetools`, containerd,
  crane) use it, so each layer upload is one HTTP/2 stream. ACR answers HTTP/1.1 only, which is why
  the same copy into ACR was never slow.
- With `proxy-request-buffering: off`, nginx grants a stream at most
  max(`client_body_buffer_size`, `http2_body_preread_size`) bytes in flight. Both defaulted to
  64 KiB, so the throughput bound was 64 KiB per round trip. A 4 MiB body to the registry host took
  2.5 s over HTTP/2 and 0.47 s over HTTP/1.1 from the same client at 35 ms RTT. From a CI runner an
  ocean away that is about 0.5 MB/s per layer.
- The storage account was not the limit: an `AppendBlock` of 4 MiB took about 60 ms end to end,
  and bytes kept arriving the whole time. Nothing was stuck, and no timeout was exceeded.
- The .NET 11 portal base brought a 935 MiB (amd64) and an 855 MiB (arm64) layer that cr had never
  seen. At 0.5 MB/s the larger one needs about 30 minutes, so core CD's 25-minute mirror and
  control promotion jobs were cancelled while still copying. The largest layer the old base added
  per build was about 230 MiB, which fit.

The fix is `nginx.ingress.kubernetes.io/client-body-buffer-size: "8m"` on the registry ingress.
Against nginx 1.27.5 the largest stream window granted went from 65,536 bytes (default) to
8,323,073 (8m). It is a throughput setting, not a timeout. The buffer is allocated per request at
min(Content-Length + 1, 8 MiB), only for requests with a body on the registry host, and nothing is
spooled to disk. `deploy/aks/scripts/test-registry-upload-window.sh` (chart gate) keeps it at
4 MiB or more and proves, with a negative control, that it would notice the annotation's removal.
