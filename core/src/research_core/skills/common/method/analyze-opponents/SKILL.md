---
name: analyze-opponents
description: Use when studying opponent policies to find an edge.
---

# Analyze opponents

A deep profile of chosen opponents: what each policy does, as mechanisms with a measured effect, and the counters that follow. `scouting` chooses whom to profile; this skill produces the claims and the Experiments that test a counter.

## Choose

1. `platform.active_policies {league, exclude_player: true}` lists the field by policy version and Player. The Orchestrator adds `exclude_user: true` to drop all the user's Players (it needs the User identity); a Researcher drops them by id when the Task names them.
2. Skip an opponent whose policy version has not changed and whose profile claim is fresh (under a week old; `claim.search {query: <player id>, tags: [opponent]}`). Re-profile on a version change.

## Evidence

- Episodes where the opponent played: `platform.episodes {policy_version}` for its policy version, or `platform.episodes {league}` filtered by `policy_version_ids`; our Experiments' runs name their `opponents`.
- `platform.replays {episode_ids}` fetches and pins up to 20 replays together; decode with the game's tool: see `code/mixin/skills/<coworld>-replay-inspection/SKILL.md` or the mixin docs. Pass the gate in `replay-diagnosis` (completed episode, a decoder that parses, actions mapped to policy versions, a named behaviour).
- What the author wrote: `platform.forum_search {query}` and `platform.wiki_search {wiki, query}`. A stated strategy is a hypothesis until replays show it.
- Another Player's policy file is not readable; `platform.policy_file` covers your Player, and for the Orchestrator the user's own.

## Decode

Mechanism statements, not adjectives: its rule, its apparent parameters, the measured effect ("opens with X in 9 of 10 episodes; wins 70% when unanswered, 35% when met with Y"). "They are good at X" is not a finding. One replay is an anecdote; a pattern across several replays supports a claim. Note counters: a predictable strong move is a target; a weakness that appears under a condition we can create is a lever.

## Record

Claims (`writing-claims`) tagged `opponent`, the policy version id, the Player id, the League and the game version:

- one claim per strength and per weakness, `replay` evidence per inspected episode (`episode:<ereq>[/seat <n>] xp:<request id or none>`), the body ending with "what would change our mind: the opponent's policy version changes", and any `doc` evidence from the forum (`<url> (retrieved <date>): <quote>`);
- a transplant tag per mechanism: `copyable`, `prerequisite-first` (name the capability) or `not-copyable`;
- when the opponent's version changes, retire the old claims as superseded by the new ones; never delete.

## Counters

For each exploitable weakness, write the counter as a hypothesis: `experiment_create` with `methods.metrics` naming the metric and `methods.procedure` saying the evaluation pins this opponent (`xp.request {experiment, opponents: [<policy version id>]}` on both arms), then `task_propose {goal, type: improvement, title, description, experiment: X-n}`. A counter that only works against one opponent needs a guardrail against the field (`run-eval`, the guardrail rung).

## Failure handling

- No completed episode with this opponent: do not profile from guesses. Propose an Experiment that pins it in `opponents`, or record the gap.
- The replay shows a seat, not a Player: map seats to policy versions through `platform.episode` participants before you attribute behaviour.
- The field changed while you worked: finish the profile at the versions you saw and tag them; `scouting` picks up the shift.
