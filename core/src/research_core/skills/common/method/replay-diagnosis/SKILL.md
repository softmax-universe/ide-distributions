---
name: replay-diagnosis
description: Use when a cause must be seen in replays before acting.
---

# Replay diagnosis

Before anyone changes the policy because of a bad score, see the cause in a replay. A low number alone names no mechanism. The output is claims with `replay` evidence; the next step is `diagnose`.

## Find episodes

- An Experiment's episodes: `experiment.read {experiment, section: results}` lists the runs with their request ids and seeds; `platform.episodes {policy_version}` lists the newest page of an uploaded Version's episodes (default 20, `limit` up to 100). Continue with `platform.episodes {policy_version, cursor: <next_cursor>}` until the evidence needed is found; do not scan the entire history for the latest episode.
- League play: `platform.episodes {league}`. When you have several episode IDs, read scores, participants and status together with `platform.episode_summaries {episode_ids}` (up to 50).
- Only completed episodes. A local run is for debugging, never for a decision.

## Fetch and decode

1. `platform.replay {episode}` downloads the replay into `documents/replays/` once and pins its path, digest and size. For multiple replays, use one `platform.replays {episode_ids}` call for up to 20 IDs. It downloads missing files concurrently and returns each path or error. Both actions reuse existing pins and restore missing files.
2. Decode it with the game's tool. How to open, expand and read this game's replays and the policy's own logs is game-specific: see `code/mixin/skills/<coworld>-replay-inspection/SKILL.md` or the mixin docs. Policy logs per episode: `softmax_api {method: GET, path: /v2/episode-requests/<ereq>/<pv>/policy-logs/<agent_idx>}`.
3. Pass the gate before any strategy change. All of these must hold:
   - the episode is completed and its game version is known (`platform.coworld` for the current one);
   - a tool decodes it and the output is non-empty and parses (exit code 0 is not enough);
   - actions map to policy versions and Players, not to seat numbers alone;
   - you record a concrete "before" metric, the tick or observation where it goes wrong, and a named cause.

## Write claims

Each inspected replay becomes evidence on a claim (`writing-claims`):

- evidence `{type: replay, ref: "episode:<ereq id>[/seat <n>][/phase <p>] xp:<request id>", stance, note}`; for a league episode outside an XP request write `xp:none`;
- the summary names the behaviour and the number ("V-7 loses 60% of late-phase fights after over-extending; 4 of 5 replays, n=5");
- the body gives the before metric, the tick, the cause and what would change your mind;
- one replay is an anecdote, say so; a pattern across several replays supports a mechanism; tag the policy version, the lever and the Goal.

An existing claim on the same behaviour gets `claim_add_evidence` instead of a twin; a contradicted one is retired with the killing replay (`claim.retire {claim, status: refuted, type: replay, ref, note}`).

## When the replay cannot show the cause

Do not change strategy. Record the gap (what you could not observe) as a claim and propose instrumentation: a maintenance or improvement Task that adds the missing logging or a decoding tool, separate from any competitive change. Then hand over to `diagnose`.

## Failure handling

- The replay does not decode or the file is empty: the gate fails. Report it with the command, exit code and output; a broken decoder is a broken capability (`repair`).
- The episode is still running or failed: it is not evidence. Failed episodes are platform or worker findings, counted separately.
- No completed episode shows the opponent or situation you need: propose an Experiment that produces one (pinned `opponents` in `xp.request`); do not profile from guesses.
