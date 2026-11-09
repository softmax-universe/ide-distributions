# Smaller task lists and conditional candidate commits

Measured November 9, 2026, against distribution main `3122d66e`, Kimi K3.1 Pro on Moonshot
through OpenRouter, and the pinned Hermes schema sanitizers from image
`sha256:d27689bc6e8581ec884a7227c9101fdaebf6c853cff8859020dac14f21452b4d`.

## Changes

`task.list` returned complete records, including long research plans and every task message.
Return an index instead: IDs, titles, human summaries, type/Goal, status/outcome, priority,
holder/wait/dependencies, stuck state and revision. Full `task.read` and store reads remain
unchanged. Keep `updated` so listing then assigning still has the same revision guard.

An observed three-task list shrank from **25,081 to 7,778 JSON characters (69.0%)**, preserving
IDs and statuses. Wait details and human summaries remain; this is not a fixed-size response cap.

The candidate skill listed validation and commit as separate steps. Combine them with `&&` after
diff review, rather than spending a model round solely to commit. Keep the independent build and
artifact inspection. Clarify that a returned updated record confirms a successful write; `pending`
still requires its command's completion result. This does not authorize skipping semantic checks.

## Local workflow measurement

Each case starts with an edited fakegame worktree and the real `git diff` already in the model's
history. It runs the shipped full schema set and captured native Hermes prompt sections with
the baseline or changed SOUL/candidate skill. Terminal commands execute against the isolated
repository; `version.create` runs the real store/build path; the model inspects the built file.
No tools, builds or successful results are stubbed. The model adapter is direct: this does not
include gateway admission, browser rendering, or a deployed workspace.

| Pair | Before seconds | After seconds | Before model calls | After model calls |
|---|---:|---:|---:|---:|
| 1 | 14.98 | 13.99 | 5 | 4 |
| 2 | 18.88 | 13.51 | 5 | 4 |
| 3 | 15.70 | 12.37 | 5 | 4 |

Median completion latency: **15.70 → 13.51 seconds (13.9% lower)**. Every baseline case separated
validation and commit; every changed case combined them. All six built and inspected the expected
policy bytes, recorded a Version and left it unuploaded. Real tool execution occupied 0.02–0.3
seconds per case; model round trips dominated.

A deliberately failing validation command exited **17**. The changed workflow ended without a
commit or Version, and HEAD was unchanged. This checks the consequential failure gate.

These are three pairs, not a production percentile estimate. An earlier SOUL-only experiment,
without native prompt sections or the real diff checkpoint, had mixed timings (before median
21.64 seconds, after 20.39; after mean was worse). Its extra status/diff reads motivated the more
faithful checkpoint above. Do not combine those different experiments or claim universal speedup.
A separate post-build continuation experiment asked for artifact inspection in both arms; it
does not establish that the record-confirmation wording alone reduces readbacks.

## Verification

- 2,553 local tests passed; three existing environment skips. Task/usability fixtures were
  updated to match the new summary surface, not to retain the old full-record shape.
- A real-Hermes release-refresh test passed, covering frozen prompt/schema updates for retained
  sessions through the normal prepare/start path.
- Formatting, lint, prompt budgets, generated SOUL/manifests and prompt checks passed.

No production rollout was performed for these PRs. Deployed latency and broader task-answer
quality remain separate verification; full plans are available when a question needs them.
