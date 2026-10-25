# AGENTS.md — MeshWeaver.Deploy

Guidance for AI agents (Claude Code, Copilot, …) working in this repository.

This repository is MeshWeaver's **public deployment reference**: the Helm chart that runs a
MeshWeaver portal, the per-instance database chart, an example AKS overlay and the cluster
manifests, the chart drift tooling, and `memex-local` (the Homebrew CLI that runs the chart on a
Mac). It left the platform repository so that the platform stays framework only (core slimming).

> 📖 **The fleet's working rules live in the platform's
> [`AGENTS.md`](https://github.com/Systemorph/MeshWeaver/blob/main/AGENTS.md)** — worktrees, the
> merge-latest-before-push rule, root cause only, gates that never test their own inputs, REST not
> GraphQL, closing keywords, no client names in anything published, policy not prose. Everything
> there applies here unchanged; this file carries only what is specific to this repository.

> 🔓 **This repository is PUBLIC, and that is the point of it.** Every reader — the hosting
> operator's image build, the estate's deploy lanes, the Homebrew tap, the chart guards — reads it
> with **no credential**. So it holds REFERENCE material only: defaults, placeholders, example
> values, `portal.example.com`. **An installation's real values never land here** — hosts,
> namespaces, database or server names, Key Vault secret names, tenant or subscription ids,
> operator identities. Those live in the estate's private deployments repository. Before every
> commit ask: *would this line identify a live installation?* If yes, it does not belong here.

## What lives where

| Path | What it is | Who reads it |
|---|---|---|
| `deploy/helm/` | the portal chart (+ `values.registry.example.yaml`) | the hosting operator image (baked in), the estate's deploy lanes, `memex-local` |
| `deploy/helm-db/` | the per-instance database chart | the hosting operator image |
| `deploy/aks/values.aks.yaml` | the generic AKS overlay (placeholders only) | the chart gate; first-time setup |
| `deploy/aks/manifests/` | cluster manifests the governed `ApplyClusterManifest` applies, incl. the operator's ClusterRole | the estate's aks-ops lane; the operator's RBAC tests |
| `deploy/aks/scripts/` | chart invariants, values-are-read, chart-drift render/compare/record, observability checks, fixtures, setup examples | the chart gate; the operator image (drift scripts) |
| `deploy/homebrew/` | `memex-local` — CLI, formula, local values, tests | `homebrew.yml` → the public tap `Systemorph/homebrew-memex` |
| `deploy/build-host/`, `.github/scripts/build-run.py`, `build-host-announcement.py`, `platform-set-dispatch.py`, `.github/build-hosts.json` | the standing build host's runner image and runner, and the hand-over of the platform's sets to `build-latest` / `build-stable` | `build-host.yml` (on the platform's `platform-set-sealed` dispatch), `build-host-stable.yml` (daily) |
| `scripts/check-config-key-coverage.py` | overlay ↔ chart key check | the estate's `config-key-coverage` |
| `test/MeshWeaver.Deploy.ChartGuards/` | the guards that hold the chart to the platform's code (probe paths, drain deadline, ConfigMap keys, the migration Job, …) | `chart-guards.yml`, against a platform checkout |

## The platform is read, never vendored

Some checks compare this chart with the platform's code or scripts (`ProbeEndpoints`, the drain
deadline in `ServiceDefaults`, the bundle publisher, the registry retention record). They check the
public `Systemorph/MeshWeaver` out at a DECLARED ref (`MW_PLATFORM_REF`, fail-closed — an empty ref
is red, never the default branch by accident) and read it there. Nothing of the platform is copied
into this tree.

## No client name in anything published — this repository is PUBLIC

The chart, the manifests and the scripts hold reference material only (see the callout above). The
same holds for the TEXT around them: an issue, a pull-request title or body, a review reply and a
commit message are readable by anyone, and GitHub keeps the edit history of every one, so rewording
afterwards removes nothing. Write a role and a neutral sample (`an enterprise client`, `acme`,
`portal.example.com`), never a client's name, a client's staff name or a client infrastructure
identifier (tenant, subscription, vault, registry, host). Commit messages are scanned by nothing —
for them this rule is the only control. Policy `issue-text-name-scan` in the platform's AGENTS.md.

## The chart and the operator image

`deploy/helm/` is the ONE home of the portal chart, `deploy/helm-db/` of the database chart. The
hosting operator's image bakes both from this repository's `main`, and every estate deploy lane
reads them here — so a merged chart change is live at the next operator build or deploy, with no
second review. Never fork a copy into the platform or an estate repository; a value an
installation needs is an overlay in the estate's PRIVATE deployments repository, never a default
here. Operations on a running instance go through the control instance's Hosting API
(`Hosting/InstanceAction`), never `kubectl`/`helm` from a workstation — the platform's AGENTS.md
→ *Deployment* and the [`/deployment`](https://github.com/Systemorph/MeshWeaver/blob/main/.claude/skills/deployment/SKILL.md) skill.

## The instances (which mesh am I talking to?)

| Instance | URL | MCP server | Role |
|---|---|---|---|
| **The public instance** | its URL is in your MCP configuration | `memex` | Public cloud and the plugin registry. |
| **The control instance** | its URL is in your MCP configuration | `systemorph` / `control` | Owns the `Deployments/<name>` records and executes `Hosting/InstanceAction`s — the ONLY authoritative deployment record. |
| **Local** | `http://memex.localhost` | `memexlocal` | `memex-local` on your Mac. |

Read the `meshweaver://reference/tools` MCP resource (`tools-reference`) for the full tool and
query-syntax reference before your first mesh call; confirm which portal a server talks to before
any mutation.

## 📚 Docs and skills live upstream

Read MeshWeaver documentation through the MCP (`get Doc/Architecture/DeploymentAKS`,
`get Doc/Architecture/OperatingFromThePortal`, or free-text `search <topic> partitions:all`). This
repository carries no skills of its own; load the platform's when the task calls for it:

| Skill | Load it when |
|---|---|
| [/deployment](https://github.com/Systemorph/MeshWeaver/blob/main/.claude/skills/deployment/SKILL.md) | rolling a portal, patching an environment, verifying the running image |
| [/new-deployment](https://github.com/Systemorph/MeshWeaver/blob/main/.claude/skills/new-deployment/SKILL.md) | standing up a new instance |
| [/ci](https://github.com/Systemorph/MeshWeaver/blob/main/.claude/skills/ci/SKILL.md) | authoring a workflow or a gate |
| [/pullrequest](https://github.com/Systemorph/MeshWeaver/blob/main/.claude/skills/pullrequest/SKILL.md) | opening, reviewing or merging a pull request |
| [/new-repo](https://github.com/Systemorph/MeshWeaver/blob/main/.claude/skills/new-repo/SKILL.md) | auditing this repository's wiring (CI callers, secrets, protection) |

## Fleet rules carried verbatim

These blocks are the fleet's baseline, copied byte for byte from the platform's AGENTS.md and
checked against it (`check-shared-rules.py`, register `.github/shared-rules.json` in the platform).
Edit them there, never here.

<!-- shared-rule:begin finish-autonomously -->
🚨 **Finish AUTONOMOUSLY — a task ends MERGED, DEPLOYED and VERIFIED, with no second "go" from anyone** (maintainer, 2026-09-09 and 2026-10-18: *"see that it is all being merged automatically"*, *"clearly stated to finish autonomously"*). A request to change something authorizes the whole delivery: worktree → change → local gates → push → pull request → every review thread answered → green → merged → deployed through the supported pipeline → the changed behaviour verified on the target instance → worktrees removed. Never hand back "shall I merge?", a bare pull-request link, or "green and waiting". Once the review is answered, arm auto-merge on a pull request into the DEFAULT branch (it lands when the required checks pass, never before — so never arm a stacked pull request, whose feature-branch base requires no checks) and leave ONE background REST watcher (a script polling at ≥ 60 s, never a model loop) that re-arms a pull request only when nothing deliberate disarmed it — never one a person disarmed, and after a merged-tree guard disarmed it only once the default branch is merged in and the checks are green again — lands the halves of a cross-repo change in the order the pair gates demand (a platform ADDITION before the dependent that uses it, a DELETING half only after the dependent adaptation has merged), and wakes you only for what a script cannot decide: a thread to answer, a red to fix at the root. A red the workflow itself calls infra — no runner, no verdict in time, a timeout — is waited out or re-run, never a reason to stop. Stop and ask ONLY when the next step is **fatal** (a required gate red on your own change would ship broken), **system-changing** beyond the normal deploy (branch protection, credentials, migrations, a hand-roll), or **destructive** (deleting data, rewriting a shared branch); then state the blocker, the last completed stage, and what production actually runs now — unchanged, or which deploy steps already landed. Autonomy never licenses bypassing a check, an access control or the CD pipeline.
<!-- shared-rule:end finish-autonomously -->

<!-- shared-rule:begin merge-latest-before-push -->
🚨 **Merge the latest default branch before EVERY push** (maintainer, 2026-10-03). Before each push to a branch with an open or planned pull request, run `git fetch origin && git merge origin/<default branch>`, resolve any conflicts, re-run this repo's local gates, and only then push. A branch pushed on a stale base turns DIRTY, or conflicts semantically, the moment the default branch moves — and GitHub runs no checks on a DIRTY pull request, so it keeps showing its stale green while nothing drains it (measured 2026-10-03: nine conflicting pull requests at once). Merging first does not license extra pushes: never push onto an in-flight PR run, a push during an in-flight review makes that review STALE, and a branch that is merely behind needs no push of its own. Batch — ONE merge-and-push per round.<!-- shared-rule:end merge-latest-before-push -->

<!-- shared-rule:begin rest-not-graphql -->
🚨 **Read GitHub through REST, not GraphQL.** GraphQL exhausts first, and the limit that bites is the **SECONDARY** one — `gh api /rate_limit` keeps reporting `5000/5000 remaining, 0 used` while every GraphQL call is refused with *"API rate limit already exceeded"*. The primary quota is therefore NOT the signal; that pairing IS the secondary limit. `gh pr view --json …`, `gh pr checks` and `gh run watch` all issue GraphQL, so a watcher built on them takes the whole session's GitHub access down with it, for every concurrent agent. REST does everything a PR watcher needs: `gh api "repos/{o}/{r}/actions/runs?head_sha={sha}"` filtered on `.name` for one workflow's `status`/`conclusion`; `.../commits/{sha}/check-runs` and `.../commits/{sha}/check-suites` for the checks; `.../pulls/{n}` for `state`, `merged` and `mergeable_state`. Reserve GraphQL for what REST cannot express — merge-queue entries and `dequeuePullRequest` — and never put one in a loop. 🚨 **Fast retries EXTEND a secondary limit**: on a refusal STOP for at least 5 minutes, do not retry, and do not switch to a different GraphQL query hoping it is cheaper. Poll no faster than 60 s and run ONE watcher, not several. Two REST caveats, both measured: `pulls/{n}` answers `mergeable: null` / `mergeable_state: "unknown"` until GitHub recomputes after a push — re-read, never read that as a verdict; and a check SUITE an installed App never posts runs to stays `queued` forever, so wait on the ONE workflow you care about by name, never on all suites.<!-- shared-rule:end rest-not-graphql -->

<!-- shared-rule:begin file-it-and-move-on -->
🚨 **The defect you did not come for: FILE AN ISSUE AND MOVE ON.** Debugging one thing exposes
another — an error swallowed, a gate never armed, a log line that lies. Ask in order: does it
**block** the task in hand (then fix it, and say so); is it **actively harmful** right now (then stop
and tell a person); otherwise it is **incidental** — file an issue and return to what you were doing.
You are not blocked by it, and turning aside costs twice: the task is abandoned mid-air and the
defect is investigated without the context of whoever owns it. **The issue is worth only the evidence
in it** — you will never be as close to this again — so it carries the instrument readings with exact
identifiers, how you got there, and **what you did NOT establish**; an issue that overstates its
certainty sends the next person down a branch you had already excluded. Never file one you have not
checked is real against the DEPLOYED artifact: a stale checkout or a truncated query produces a
confident finding about a defect that does not exist, and someone else pays to disprove it.
🚨 **An agent does NOT open a plain ticket — it files into BUG TRIAGE**, fleet-wide: a node of
TYPE `Feedback/Feedback` on the control instance (memex.systemorph.com — the MCP server named
`systemorph`, not `memex`), which reaches the triage agent and may become a GitHub issue *from
there*. Triage decides the repo, the priority, and whether it becomes a ticket at all — you do not.
A ticket you open yourself still reaches the pool (the organisation's Issues webhook feeds every new
issue into the same inbox, `Hosting/Triage` in MeshWeaver.Plugins), but only AFTER you have picked its
repo and made it public — the two calls triage exists to make, and on the public core repository a
ticket cannot be unpublished. The pooling is the
whole point: ONE inbox per portal, never one queue per repo. 🚨 **The NAMESPACE is your OWN
partition: `create` with `namespace: "{user}/Feedback"`** (the node lands at
`{user}/Feedback/<slug>`, e.g. `rbuergi/Feedback/<slug>`). `Feedback/<id>`,
`Feedback/_Submissions/<id>` and `Feedback/Inbox/<id>` answer `Access denied: Create permission
required` **by design** — that refusal names the wrong namespace, never a closed route, so it is no
reason to drop the finding. **File it SUBMITTED — OMIT `status` (absent means `New`), never a
`Draft`** — the draft-and-preview stage exists so a person can vet words written in their name, and
an agent reporting its own finding has nothing to preview and usually no chat to preview it in.
Write node paths in the message without an `@` prefix, then `get` the node back by its exact path:
that read is the verdict AND the wake its hand-over waits on. Full rule, the two failure modes,
triage and where the finding goes: [Incidental Findings](https://memex.meshweaver.cloud/Doc/Architecture/IncidentalFindings) (`get Doc/Architecture/IncidentalFindings`).<!-- shared-rule:end file-it-and-move-on -->

<!-- shared-rule:begin conserve-work-products -->
**🗂️ ALWAYS conserve work products — a design, an architecture decision, an investigation finding, a manual produced while working gets COMMITTED to this repo in the same change set, every time.** The durable form is <!--slot:doc-home-->a markdown page in this repo, beside the chart, manifest or script it describes (or in `README.md`)<!--/slot--> — issue comments, PR bodies, chat replies and rendered artifact pages are *pointers* to the committed page, never a substitute for it. A finding that lives only in an issue thread or a terminal is invisible to the next session<!--slot:reach--><!--/slot-->. Maintainer directive, 2026-08-30<!-- shared-rule:end conserve-work-products --> — fleet-wide (the platform rule lives in MeshWeaver's AGENTS.md); platform-wide designs go to core's `Doc/Architecture`, repo-specific ones stay here.

## Work in worktrees

`git worktree add .worktrees/<name> -b feat/<name> origin/main`. Never edit, build or commit on the
primary checkout, never `git stash` (use `git diff > patch` + `git apply`). `.worktrees/` is
gitignored.

## Gates

Every job caps at a literal `timeout-minutes` ≤ 45 and carries no input-shaped `if:` and no
`continue-on-error:`. The REQUIRED ones run on every pull request, with no path filter:

1. `chart-gate.yml` — `Chart invariants`, `Observability values (rendered + binary-validated)`,
   `Bundle registry scripts (executed)`, `Registry retention (this chart against core's record)`.
2. `chart-guards.yml` — `Chart guards (vs platform)`: the C# guards built and run against the
   declared platform ref.
3. `repo-hygiene.yml` — `Workflow hygiene (vs platform)`: the platform's workflow guards
   (timeouts, ignored YAML keys, secret preflight) fetched at `main`, self-tested, run on this tree.
4. `review-answered.yml` — `review-answered / Automatic review answered`: the automatic review has
   landed on the head and every thread it opened has a reply ON the thread (core's reusable lane).

The thin callers of the platform's other lanes, none of them a required context:
`request-review-on-change.yml` (one Copilot review per push that changed the diff — the ruleset
does not review on push), `auto-arm.yml` (arms auto-merge; needs the `meshweaver-cloud` App
secrets) and `retry-known-transients.yml` (one automatic rerun of a named infrastructure
transient). Each calls its lane `@main`; never paste a lane's body in here.

3. `build-host.yml` — `Build host (runner image + self-tests)`: the runner image built against the
   platform's `main` with every self-test, on every pull request. Not yet in the ruleset: adding it
   is an owner's change to `main pr protection`. (Its publishing jobs run only on
   the two events that carry a hand-over; `preflight` is red there on a missing credential.)

`homebrew.yml` is path-filtered to `deploy/homebrew/**` and `deploy/helm/**` and therefore NOT a
required check (a skipped required check reads as satisfied): `memex-local (Linux — …)` and
`brew install + test (macOS)` on a change there, and the tap publish on `main`.

A chart change is effective at the next deploy that reads this repository's `main`, and the
operator image picks it up on its next build — so a bad chart change is live on merge. Treat a red
gate as a stop.

## Git workflow

When the work is finished, finish it: worktree → change → local gates → `git fetch origin && git
merge origin/main` → push → pull request → answer every review thread ON the thread → green →
merged. Never push to `main`, never merge on red or pending. Commit and pull-request attribution
follows the platform's AGENTS.md.

## Local commands

```bash
deploy/aks/scripts/check-chart-invariants.sh          # needs helm + PyYAML
deploy/aks/scripts/check-values-are-read.sh
deploy/aks/scripts/test-chart-drift-compare.sh
dotnet build test/MeshWeaver.Deploy.ChartGuards -c Release -warnaserror -p:MeshWeaverRoot=<platform checkout>
```
