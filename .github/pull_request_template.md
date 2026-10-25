## What & why

<!-- One or two sentences: what changes and why. `Refs #N` for an issue this does not fully close. -->

## Merge preconditions

- [ ] **Gates green** — `Chart invariants`, `Observability values`, `Bundle registry scripts`,
      `Registry retention`, `Chart guards (vs platform)` and `Workflow hygiene (vs platform)`.
- [ ] **Review answered** — every automatic-review thread has a reply ON the thread
      (`Automatic review answered`).
- [ ] **Nothing identifies a live installation** — no real host, namespace, secret name, tenant,
      subscription or client name, here or in the commit messages.
- [ ] **Merged `origin/main` before the last push.**

## Effect on running instances

<!-- A merged chart change is live at the next operator build or deploy. Name what changes for a
     running portal, and what has to be rolled or recycled. "None" is an answer. -->
