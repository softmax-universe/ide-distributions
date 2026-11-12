---
name: submission
description: Use when a Version is uploaded or submitted to a League.
---

# Submission

A policy reaches a League in three steps, each its own tool: a Version (`version.create` or `version.adopt`), `upload {version}`, `submit {version, league}`. Upload takes any Version; submit takes any uploaded Version. Only the Orchestrator submits.

## The status ladder

`version.read` shows one computed status: `built` → `uploaded` → `submitted` → `qualifying` → `competing` → `champion`. The last four come from the platform's submission states (`pending`, `processing`, `placed`, `rejected`) and membership states. The Champion is the policy version that represents the Player in the League, never the League's winner; `platform.champion {league}` reads it, and the watcher syncs it every ten minutes.

## Upload

`upload {version: V-7}` sends the exact built bytes and, for a file policy, checks the platform's hash against the artifact digest. It records an intent first and asks the platform on a retry, so a crash after the platform accepted the bytes never uploads twice; it sends again only when the platform has no such version, or on `user_requested: true`. An uploaded Version answers without sending; an adopted one is already uploaded.

## A user asked

For an unconditional request, submit in the same turn. Run `version.create` on the code they mean (`code/` for what is on `main`), `upload`, then `submit {version, league, user_requested: true, cites: <their message>, reason}`. No setting, quality bar or Task applies. Warn about what is missing (no Experiment, not merged) and submit anyway. `cites` is a record, never a gate: without it the submission still runs and the result asks you to record the reference.

When the request sets a condition on submission, verify it first. Refresh the Experiment with
`xp.status {experiment}`. If you already have the episode IDs and need a score tally, use the
bulk results view in the `softmax-api-routes` skill. Submit only when the user's condition is met.

## Your own initiative

`submit` without `user_requested` applies the settings and refuses with every shortfall named, each ending "a user request overrides this":

- `submissions.mode`: `ask` means ask the user first (`ui.ask`; from a wake, a `decision` `ui.tell`); `autonomous` submits.
- `submissions.until` (an open window) and `submissions.max_count`; `paused` stops all of it.
- `submissions.quality_bar`: the Version is merged into `main` and a confirmed Experiment compares it with our champion in the League at a primary effect of at least `submissions.min_margin_over_champion`. With no champion, a merged Version whose mixin `check` passes is enough.

The bar is on your initiative, not on the user: they can turn it off (`settings_update {key: submissions.quality_bar, value: false, cites}`) or lower the margin. Meet a shortfall rather than argue with it: get the Task reviewed and merged, run the Experiment against the champion (`experiment`).

## Researchers

A Researcher never submits. `task_propose_submission {task, version, reason}` records the proposal; a management wake applies the rule above, and in mode `ask` the Orchestrator asks the user. `task_complete` carries `submission_candidate: yes|no` with `submission_reason`. Waiting for a placed submission is `task_wait {task, kind: submission, detail: "V-n in <league>", ref: <its policy version id>}`.

## Options and after

- `auto_champion`: whether the platform makes it the Player's champion when it starts competing: `always` (default: unless the champion is a later upload), `never`, or `lineage` (only if the champion is an earlier version of the same policy).
- Every autonomous submission sends a notice.
- After `submit`, say where it stands in platform terms (`submitted`, `qualifying`, `competing`, `champion`) and nothing else. A `rejected` submission is a finding: read its `notes` (`GET /v2/league-submissions?policy_version_id=` through `softmax_api`), record a claim, fix the Version.
- A policy this Player uploaded outside this workspace becomes a Version with `version.adopt {policy_version | label, summary}`, so it can be submitted and compared. The tool sees only this Player's uploads; another Player's policy is refused.
