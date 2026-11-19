# Probes — short-lived, governed, self-cleaning pods

A file here is a **probe**: a Pod or a Job that answers one feasibility question about the cluster. Does a user namespace
work on this node pool? Does an NFS mount succeed from a pod? It is run ONLY by the `RunProbe` InstanceAction on the
control instance (MeshWeaver.Plugins#5630), through `deploy/hosting-operator/bin/hosting-probe-run` on the config
repository's aks-ops lane. Never `kubectl apply` it by hand.

## How it runs

1. **Dry run** (`dryRun: true`, no approval). `hosting-probe-run check --manifest probes/<name>.yaml` prints the file's
   digest (`::hosting:: probe_digest=sha256:<hex>`), validates it, and asks the API server for a dry run. Nothing is created.
2. **Run** (one mesh approval, bound to that digest). `hosting-probe-run run --manifest probes/<name>.yaml --sha256 <hex>`
   does the following:
   - refuses if any object of the file already exists;
   - creates the probe and waits at most its own `activeDeadlineSeconds` plus a grace;
   - prints its log, bounded and masked, onto the action node;
   - deletes **everything it created** and reads each object back as gone, on every exit path, a failure and a timeout
     included.

   A cleanup it cannot confirm fails the run, naming what is left.

## What a probe file must be — the script refuses anything else, by name

- **One** Pod or **one** Job. Optionally it also holds ONE `Namespace` named `probe-<name>`, which the run creates and
  deletes; the Pod or Job then lives in it. Without one, the probe runs in the fixed namespace `hosting-probes`, which
  must already exist (applied through `ApplyClusterManifest`).
- `automountServiceAccountToken: false`. The namespace's `default` account, and no projected token. **A probe never
  holds an API token.**
- **No Secret**: no secret or projected-secret volume, no `envFrom` secretRef, no `secretKeyRef`. Its log lands on the
  action node.
- **Pod Security baseline at most**. No `privileged`, no `hostNetwork`/`hostPID`/`hostIPC`, no `hostPath`, no
  `hostPort`, no capability outside baseline's set, no `procMount`, no Unconfined seccomp or AppArmor profile, no unsafe
  sysctl. A probe that needs more (for example bubblewrap with `SYS_ADMIN`) is refused by name: that question needs an
  owner's decision, not a probe.
- `restartPolicy: Never` and `activeDeadlineSeconds` ≤ 900, so the pod ends on its own even if the lane dies before its
  cleanup. A Job also carries `backoffLimit: 0`.
- Committed verbatim: no `<placeholder>`.
