# MeshWeaver.Deploy

The public deployment reference for [MeshWeaver](https://github.com/Systemorph/MeshWeaver):

| Path | What |
|---|---|
| `deploy/helm` | the Helm chart that runs a MeshWeaver portal (portal, migration Job, optional registry, KEDA, PDB, ingress) |
| `deploy/helm-db` | the per-instance PostgreSQL chart |
| `deploy/aks` | an example AKS overlay, the cluster manifests, and the chart / drift / observability tooling |
| `deploy/homebrew` | `memex-local` — run the chart on a Mac (`brew tap systemorph/memex && brew install memex-local`) |
| `test/MeshWeaver.Deploy.ChartGuards` | guards that hold the chart to the platform's code |

Everything here is reference material: defaults, placeholders and example values. An installation's
own values live in its operator's private deployments repository, never here.

## Developing

```bash
deploy/aks/scripts/check-chart-invariants.sh     # render every values combination, assert it is coherent
deploy/aks/scripts/check-values-are-read.sh      # every key a values file sets reaches a template
dotnet test test/MeshWeaver.Deploy.ChartGuards -p:MeshWeaverRoot=../MeshWeaver
```

Working rules for agents and people: [AGENTS.md](AGENTS.md).

Licensed under Apache-2.0 or MIT, at your option — see [LICENSE](LICENSE).
