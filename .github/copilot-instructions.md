# Review instructions — MeshWeaver.Deploy

The working rules are in [`AGENTS.md`](../AGENTS.md); the platform's
[review instructions](https://github.com/Systemorph/MeshWeaver/blob/main/.github/copilot-instructions.md)
apply where they concern the same files (workflows, scripts). Always worth flagging in this repo:

- **A value that identifies a live installation.** This repository is PUBLIC and holds reference
  material only: a real host, namespace, database or server name, Key Vault or secret name, tenant
  or subscription id, registry, operator identity, or a client's name — in a values file, a manifest,
  a fixture, a script default or a comment — is a defect. Samples read `portal.example.com`, `acme`.
- **A values key no template reads, or a template reading a key no values file declares.** The
  chart gate checks it; flag it in review anyway when the diff adds one.
- **A gate that can pass on no evidence**: a `continue-on-error:`, an input-shaped `if:` (asking
  whether a secret or variable is set), a path filter on a required workflow, a job without a
  literal `timeout-minutes` ≤ 45, or a check run with no self-test before it.
- **A copied platform script or workflow** where the platform offers a reusable lane or a fetched
  script — a copy keeps none of the fixes made after it.
- **A `kubectl`/`helm`/`az` step presented as the procedure** for operating a running instance;
  operations go through the control instance's `Hosting/InstanceAction`.
