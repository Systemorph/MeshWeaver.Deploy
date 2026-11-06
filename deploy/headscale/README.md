# headscale — the fleet's private network

**One private network that the fleet's machines join with the stock Tailscale client.** Headscale is
the open-source Tailscale control server; the estate runs it behind the cluster's HTTPS ingress, and a
machine joins with:

```bash
tailscale up --login-server https://<host>     # opens a browser: sign in with your work account
```

An admitted account registers the machine with no admin step (OIDC, `oidc.allowed*`). Once joined, the
machines reach each other by private address (`100.64.0.0/10`) and by MagicDNS name
(`<machine>.<dns.baseDomain>`), directly where the networks allow it and through a relay where they do
not. What may reach what is the HuJSON `policy` (default: every member's machines reach each other).

It is the "general network reach to the Macs" need that `Hosting/LocalMeshReach.md` (MeshWeaver.Plugins)
kept apart from the phone app: SSH, screen sharing, a local mesh on another Mac, the build hosts.

## What runs

| Object | Why |
|---|---|
| `Deployment` (one replica, `Recreate`) | the state is a sqlite file on a ReadWriteOnce volume — two pods would corrupt it |
| `PersistentVolumeClaim` (kept on uninstall) | the database AND the Noise private key: losing the key un-registers every machine |
| `ConfigMap` | `config.yaml` + `policy.hujson`; a change rolls the pod (checksum annotation) |
| `Service` + `Ingress` | plain HTTP inside the cluster, TLS at the ingress (cert-manager or a pre-created Secret) |
| `SecretProviderClass` (OIDC via Key Vault) | the client secret is mounted as a FILE and never synced into a Kubernetes Secret |
| `NetworkPolicy` | in: the HTTP port, from the ingress namespace; out: DNS and HTTPS only (issuer, DERP map) |

The pod runs as uid 65532 on a read-only root with no capabilities and no service-account token.
Metrics and gRPC listen on loopback only; the admin CLI talks to the unix socket inside the pod.

## Relays (DERP)

Machines that cannot reach each other directly relay through **Tailscale's public DERP servers**
(`derp.urls`). The traffic stays end-to-end encrypted (WireGuard); the relay sees ciphertext. Running
the fleet's own relay is possible later — Headscale embeds one — but it needs UDP 3478 for STUN, which an
HTTP ingress cannot carry, so it is a separate change (a `LoadBalancer` Service of its own).

## The image

`.github/workflows/headscale-image.yml` mirrors the upstream release, **pinned by digest** in the
`Dockerfile`, into the fleet registry as `headscale:<version>-<short sha>` (immutable; the chart refuses
any other tag shape). The build runs `test/smoke.sh` on the shipped binary; the pull-request job then
proves the smoke can go red, serves the CHART's own rendered config as the pod does, joins a stock
Tailscale client to it, and checks every render refusal.

A version bump: change the `HEADSCALE_IMAGE` digest and `LINE` together, read the release notes for the
minimum Tailscale client, and declare the new line in the platform repository's
`.github/release-channels.json`.

## Installing it

Never `helm install` by hand. An estate vendors this chart byte-identical into its private deployments
repository, writes its overlay (host, ingress class and issuer, OIDC issuer/client/allow-list, Key Vault
names) and applies it with the governed `ApplyChart` InstanceAction from a `Hosting/ChartRelease` record.

## Joining a machine

- **macOS:** `brew install --cask tailscale` (the app, with its network extension) or
  `brew install tailscale` (the open-source daemon, `sudo brew services start tailscale`), then
  `sudo tailscale up --login-server https://<host>`.
- **Linux:** the distribution's `tailscale` package, then the same `tailscale up`.
- Minimum client for Headscale 0.29.7: **v1.80.0**; upstream recommends **v1.104.4 or newer**.
