---
name: scouting
description: Use when reading the league standings, field and opponents.
---

# Scouting

Scouting keeps the picture of the League current: where the Player stands, who moved, what wins now, and which opponents deserve a close look (`analyze-opponents`). Its output is claims; a Goal's "where it stands" and the Orchestrator's reports read them.

## Snapshot

1. `platform.results {league}`: our standing rows and memberships. `platform.champion {league}`: the policy version that represents the Player (the Champion is never the winner).
2. `platform.standings {league}`: every Division with rank, score and rounds played; `platform.divisions` lists the Divisions.
3. `platform.active_policies {league, exclude_player: true}`: the field by policy version and Player. Standings list Players; this is how you learn which policy version each one runs, never by guessing.
4. Save the outputs under `documents/scouting/<date>/` in the Coworld folder, not in your worktree. `documents/` is not in git, so there is nothing to commit.

## Compare

Read the last snapshot: `claim.search {query: standings, tags: [<league id>]}`. Name what changed: a new league leader, our rank, new entrants near the top, a Player whose score moved by more than its usual round-to-round swing, a policy version change. A score without its rank and rounds played is not a finding. A swing of an unchanged opponent's mean is noise, not a meta shift. When our score is flat but our rank drops, the field improved.

## The meta

What wins now, and why, in game terms. Read a few episodes of the top policies (`platform.episodes {league}`, `platform.replay`; decode with `code/mixin/skills/<coworld>-replay-inspection/SKILL.md` or the mixin docs) and the League's forum and wiki (`platform.forum_search`, `platform.wiki_search`). Old conclusions rot: check a claim's game version against `platform.coworld` before leaning on it.

## Record

Claims (`writing-claims`), each tagged `standings` or `meta`, the League id and the game version:

- the snapshot claim: our rank and score, the champion, the top of each Division, with `measurement` evidence (`platform.standings <league> @<sha> -> documents/scouting/<date>/standings.json`) and what would change it (the next Round);
- one claim per shift, with the numbers and their n;
- one claim per decoded winning mechanism, with `replay` evidence and a transplant tag: `copyable` (works for us as is), `prerequisite-first` (name the missing capability: that is a lever) or `not-copyable` (depends on the opponent's other strengths).

Retire the previous snapshot claim with `claim.retire {claim, status: superseded, superseded_by}`.

## Propose

For each usable weakness in the field or gap in ours: `task_propose` a research Task (a question with the evidence) or an improvement Task with a proposed Experiment (`diagnose`). Name the opponents worth a deep profile for `analyze-opponents`: those above us, the league leader, and any whose policy version changed since the last profile.

## Failure handling

- A platform error after retries puts the Task on a `platform` wait; end your run and report the route when resumed.
- A reading needs the User identity (`exclude_user`): a Researcher cannot use it; drop the user's other Players by id when the Task names them, or `task_ask`.
- Standings and memberships disagree (a champion the ladder does not show): record both and report it; do not resolve it by assumption.
