---
name: survey
description: Use to read what happened, not to rank Versions.
---

# Survey

A survey turns a pile of finished episodes into a finding: what happened, decomposed honestly, with the episodes worth attention narrated. It describes one batch. It never answers "did the change help" (that is an Experiment, `experiment`) and never explains why (`diagnose`). It reads results and episode metadata, not replays; it tells you which episodes deserve `replay-diagnosis`.

## Inputs

- An Experiment's downloaded results: `xp.status {experiment}` stores the mixin's `evaluate` rows on the runs; `experiment.read {experiment, section: results}` lists them with request ids; the files are under `documents/xp/<X-n>/<request id>/`.
- League play: `platform.episodes {league}` or `{policy_version}`, `platform.episode_summaries {episode_ids}` for scores and participants across up to 50 episodes, `platform.results` for our standing rows.
- Per-seat metrics come from the mixin's `evaluate` command on downloaded results; what it produces is game-specific: see `code/mixin/skills/<coworld>-survey/SKILL.md` or the mixin docs.

For raw per-episode game metrics absent from summaries, use `platform.episode_results {episode_ids}` in groups of up to 20. Read the returned JSON paths together.

## Method

1. **Aggregate** per policy version: episode count, win rate, score, the game's headline metrics. Separate infrastructure failures (crashes, timeouts, `status: failed`) from behaviour from the first table on: a crash is a repair finding, not a strategy finding.
2. **Decompose** every stat by the game's meaningful groups (roles, seats, phases, opponents) before concluding anything; the aggregate has hidden a large role-specific gap before. Surface who beats whom when the game has that structure. The groups are game-specific: see `code/mixin/skills/<coworld>-survey/SKILL.md` or the mixin docs.
3. **Flag** the episodes that stand out: blowouts, upsets, near-wins, clusters of invalid episodes (crashes, timeouts, anything the mixin marks as tainted), degenerate behaviour. Keep the list short and de-duplicated, rarer flags first.
4. **Narrate.** Every flagged episode gets one specific sentence a reader can act on: which policy, which group, what happened, the number that makes it stand out. "A close game" ten times is noise; if you cannot write a real reason, unflag it.
5. **Present finding-first.** The conclusion in the first sentence, evidence after; every number carries its N and its comparison.

## Record

Write the finding as claims (`writing-claims`): a `measurement` evidence link to the stored results (`<command> @<sha> -> <path under documents/>`), a `replay` link for each narrated episode (`platform.replays {episode_ids}` pins them together) and the Experiment (`X-n`) when the batch came from one. Tag the Goal, the policy versions and the group the finding is about. A directional finding says so in the summary.

## Footguns

- **Taint before means.** Drop invalid (tainted) episodes at the episode level before computing anything and report the share dropped, the taint rate; counting a crash as a score fabricates a regression.
- **A survey is not a verdict.** Comparing this batch with an older one is confounded by field drift; when the question becomes "is V-7 better than V-5", stop: `experiment.search` for one that answers it, else run an Experiment.
- **Small batches are directional only.** Say so. Noise is a first-class finding.
- **Generic reasons are worse than none**; they teach the reader to ignore the list.
- **Missing binding.** Without the mixin's survey knowledge, do a scores-only overview (win rate, score, taint rate), say the result is weaker and propose a maintenance Task to write the binding.
