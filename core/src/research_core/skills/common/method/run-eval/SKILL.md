---
name: run-eval
description: Use when shaping an XP request to the question asked.
---

# Run-eval

Hosted evaluation (an Experience request) is the only oracle; local runs debug, they never decide. The expensive mistake is running the wrong shape and reading it anyway. Every request belongs to a running Experiment: `xp.request {experiment, seeds, episodes, arms, league | opponents}`.

## The question decides the shape

| Question | Shape |
|---|---|
| Did my change help? | Two matched arms (`arms: [baseline, candidate]`), identical pinned rosters (`opponents: [<policy version ids>]`), the same seeds; never sampled seats in an A/B |
| How does V-7 do against the field? | One arm (`arms: [candidate]`), the League fills the other seats (`league: <id>`), more seeds; with no pairs, `experiment.close` needs an explicit `verdict` with `measurements`, `interpretation` and `reason` |
| How do we do in role X? | A role-pinned probe with a pinned roster; what can be pinned is game-specific: see `code/mixin/skills/<coworld>-eval-design/SKILL.md` or the mixin docs |
| Does the build even run? | The mixin's `check` command from `MIXIN.md` in your worktree first; then the smallest request the game allows, one seed, one episode |

Rosters, episode floors by question type, seat rules and pacing are game-specific: see `code/mixin/skills/<coworld>-eval-design/SKILL.md` or the mixin docs. Without it, say so and treat every result as directional.

## The ladder

Size follows purpose; the episode count is a calculation, not a habit.

1. **Smoke**: the minimum the game allows. Answers "does it run".
2. **Directional**: enough to see a large effect; deltas are directional only.
3. **Verdict**: at or above `n_min` pairs from the decision rule (`calibrate-noise`). The only rung that can confirm an Experiment.
4. **Guardrail**: a broad field eval after a candidate confirms, to catch overfitting to the comparison roster.

Never conclude from a rung below the question's weight.

## Method

1. Name the question, pick the shape and the rung. Write the expected effect into the Experiment before anything runs (`experiment`).
2. Pin seats by explicit policy version id: `platform.active_policies {league, exclude_player: true}` lists the field, `platform.policy_versions` ours. "Latest" in a roster is a reproducibility hole.
3. Cost: `platform.credits`; the request result carries an `estimate` (`credits` skill). Shrink seeds or split into batches before you start, keeping both arms of a seed together.
4. `xp.request`. Each accepted request is recorded as a run at once; the result lists `created`, `reconciled`, `requests` and `runs`. Repeating the call creates only the missing requests.
5. `task_wait {task, kind: xp, detail: "X-n's requests", ref: [<request ids>]}` and end your run. Reading starts when the watcher resumes you: `xp.status {experiment}`.
6. Both arms of an A/B go out in the same window; when the binding gives a batch size, respect it.

## Footguns

- Sampled seats (`league`) in an A/B invalidate the comparison: the arms saw different fields.
- A created or running request counts for nothing until its results are inspected.
- A platform 4xx: read the error's detail and, if the route or body may have changed, the `softmax-api-routes` skill; do not retry blind.
- Above the `xp.ask_above_requests_per_experiment` setting a Researcher's request is refused (`credits` skill).
