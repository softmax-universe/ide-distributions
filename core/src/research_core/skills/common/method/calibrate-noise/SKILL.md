---
name: calibrate-noise
description: Use when sizing an Experiment from measured metric noise.
---

# Calibrate noise

A decision rule needs `n_min` pairs that match how much each metric moves on its own between episodes, and a guardrail `tolerance` needs the same number. That noise is per game version; a new version needs a new record. Existing Experiments keep their locked designs; only new ones use the new record.

## Sources

Only hosted episodes count; local runs never do.

- League episodes: `platform.episodes {league}` lists episodes of up to ten active policies, from any league or XP request (20 per policy by default; `limit` selects up to 100; `round_id` marks a round's; `truncated` says policies were left out; `next_cursors` maps each policy to its next page, fetched with `platform.episodes {policy_version, cursor}`); `platform.episode {episode}` gives each one's scores and participants. Per-seat metrics beyond the score need the mixin's `evaluate` command on downloaded results (the results artifact through `softmax_api`; see the `softmax-api-routes` skill).
- Completed Experiments of the same game version: `experiment.read {experiment, section: results}` has the stored rows that `xp.status` downloaded.
- Which metrics matter, and how to run `evaluate` on a folder of results, is game-specific: see `code/mixin/skills/<coworld>-ab/SKILL.md` or the mixin docs.

## Method

1. Find the game version: `platform.coworld {coworld}` (version and commit). Take the metric names and directions from `MIXIN.md`.
2. Collect at least 10 rows per metric; aim for 100 or more. Drop rows of other game versions and failed episodes, and report the counts you dropped.
3. For each metric compute the per-episode standard deviation and, where rows pair by seed, the standard deviation of the paired difference. The workspace environment ships helpers: `research_core.stats.noise.compute_noise` (`EpisodeRow` objects to a record), `noise.sd_for` (the `sd` the power helpers take), `research_core.stats.power.min_episodes` and `power.delta` (the pairs an effect needs, and the slip a `tolerance` should allow: `k` is how many standard errors, default 0.5). Run them with the workspace environment's Python (`environment` skill).
4. Store the record under `documents/measurements/noise-<game version>.json` in the Coworld folder, not in your worktree, with the rows' source ids. `documents/` is not in git, so there is nothing to commit.
5. Write one claim per record (`writing-claims`): summary "metric noise for <game version>: <metric> sd <x> (n=<rows>)…", body with the counts, sources and what would change it (a new game version, a field shift), tags `noise`, the game version and the League, evidence `measurement` (`<command> @<sha> -> documents/measurements/noise-<game version>.json`). Fewer than 10 rows per metric: say the record is weak in the summary.
6. Experiments cite the claim in `decision_rule.text` and take `n_min` from it.

## Failure handling

- League episodes unreadable (a 403 on results): calibrate from Experiment rows alone if they cover the version; otherwise write no record, say what is missing and propose a research Task that collects rows.
- The League has not played the current version yet: the same.
- The mixin's `evaluate` fails on downloaded results: that is a broken capability (`repair` skill), not a measurement.
