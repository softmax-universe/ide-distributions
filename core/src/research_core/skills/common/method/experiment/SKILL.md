---
name: experiment
description: Use to test if a Version beats another, by Experiment.
---

# Experiment

An Experiment compares two Versions and ends in the verdict its pre-registered decision rule gives. An improvement Task runs exactly one. Its hypothesis and methods lock at `experiment.start`; after that, a change is a new Experiment.

## Before you design

1. Read the Goal, its directives and the Task's reasoning.
2. `claim.search {query: <lever>, include_retired: true}`: a refuted claim closes a lever until new evidence meets its "what would change our mind". `experiment.search` finds earlier attempts; cite them in `related_experiments`.
3. Choose the baseline. Any Version can be one. When the purpose is a submission, the baseline is our champion in the League (`platform.champion {league}`); if it is not a Version yet, `version.adopt {policy_version, summary}`, which finds only this Player's uploads.
4. Know the cost (`credits` skill): `platform.credits`, then seeds × arms requests of `episodes` episodes each.

## Design with `experiment_create`

- `summary`: at most 40 words.
- `hypothesis {statement, if_true, if_false, motivation}`: a mechanism ("X because Y, so Z"), not a tweak. `if_true` and `if_false` say what we do in each case and must differ.
- `methods {baseline, candidate, procedure, metrics, decision_rule}`: Versions as `V-n`; leave `candidate` empty until it is built, the one Version built for the Experiment fills it. `metrics`: `[{name, direction, role, min_effect?, tolerance?}]` with roles `primary`, `guardrail`, `performance`; a guardrail or performance metric with no `tolerance` refutes on any drop. Every behavioural primary metric gets an anti-gaming guardrail; a rate metric needs its volume counterparts. `decision_rule {text, n_min}`: `n_min` pairs from the noise record (`calibrate-noise`) or the mixin's floors.
- Metric names, directions, rosters and floors are game-specific: see `code/mixin/skills/<coworld>-eval-design/SKILL.md` and `code/mixin/skills/<coworld>-ab/SKILL.md` or the mixin docs. Without them, say so and treat the result as directional.
- `tags`: the lever and the Goal (`G-n`).

Attack the design before starting: does the metric measure the mechanism or a correlate; can this shape show the effect; what else produces the signal; does `n_min` support the effect you expect? Redesign while it is proposed (`experiment.update`).

## Run

1. `experiment.start {experiment}` locks the design; it refuses while a part is missing and names each, and the candidate may still be empty.
2. Build the candidate (`candidate` skill): one conceptual change, `version.create {summary, task, experiment}`, then `upload {version}`.
3. `xp.request {experiment, seeds, episodes, opponents}`: one request per seed and arm, recorded as runs the moment the platform accepts them. The roster fills every seat; name `n_min` seeds (the default stops at 20). In an A/B both arms get the same `opponents` and are paired by seed (`run-eval`).
4. Then `task_wait {task, kind: xp, detail: "X-n's requests", ref: [<request ids>]}` and end your run; the watcher resumes you when the requests are terminal. Never poll. A credits refusal (402) is `task_wait {task, kind: credits, detail}`. The Orchestrator has no wait: nothing wakes it when a build or request ends, so it tells the user and picks the work up on the next message or wake.
5. On resume, `xp.status {experiment}` refreshes the runs, downloads finished results, runs the mixin's `evaluate` and stores the rows. If `evaluate` is backgrounded, `task_wait {task, kind: process, detail: "evaluate of X-n", ref: <pid>}`, then `xp.status {resume}`.
6. Inspect before you trust: failed episodes, dropped rows, the right Versions and seeds, and that the change activated (one replay per arm, `replay-diagnosis`). Mark a bad run `experiment.update_run {run, validity: "invalid: <reason>"}`.

## Close

`experiment.close {experiment}` pairs baseline and candidate rows by seed and applies the rule: `confirmed` (no reason codes), `inconclusive` (`n_short`), or `refuted` (`primary_not_significant`, `guardrail_breach`, `performance_slip`, `performance_slip_without_significance`); `no_primary_metric` alone is `inconclusive`. Give an explicit `verdict` with `measurements`, `interpretation` and `reason` only when the rule cannot be applied as registered. Never move a threshold after seeing data; N was committed with the rule, so do not peek and stop early. `experiment.abandon {experiment, reason}` stops before a verdict; the record stays and a replacement cites it.

## Record

- Claims from the verdict (`writing-claims`): evidence `{type: experiment, ref: X-n, stance}`. A refuted or inconclusive result is a claim too, with what would reopen the lever in the body. Retire what it supersedes.
- `task_complete`: `experiment`, `versions`, `claims`, `submission_candidate` with `submission_reason`. Confirmed against the champion at or above the margin: `task_propose_submission {task, version, reason}` (`submission` skill).
