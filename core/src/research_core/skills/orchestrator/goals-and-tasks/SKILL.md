---
name: goals-and-tasks
description: Use when turning a request into Goals, Tasks or settings.
---

# Goals, Tasks, directives and settings

Goals and Tasks are the plan. Write them so a Researcher can start without you and the user can
judge the result.

## Goals

`goal.create {title, end_state, reason, description, judgement, tags}`. The end state is
something that can be judged: "a Version beats the champion by 2 mean score over 200
episodes with the mixin's guardrails met", not "improve the policy". `reason` is the user's
words or your reading of them; `judgement` says how the end state will be checked (which metric,
which Experiment design). Keep `where_it_stands` current with `goal.update` after each review.
`goal_set_status {goal, status, status_reason}` to park (waiting on the user or on credits), complete (the end state was met; cite the Experiment) or abandon.

A read-only question needs no Goal. A request that takes more than a turn does.

## Task types

- `research`: learn something; delivers claims or `none: <reason>`; no policy code.
- `improvement`: one change, one Experiment from hypothesis to verdict; delivers claims, the
  Experiment, the Versions and a submission answer.
- `ideation`: one to five viable proposed Tasks grounded in the Goal's claims, or an explicit empty result; at most one open per Goal.
- `maintenance`: a committed change (a repair, a mixin fix, a toolchain pin) and a summary; never
  a Version. Repairs are `priority: high`. A maintenance Task may have no Goal; every other type
  needs one.

`task.create {goal, type, title, description, reasoning, for_human, end_condition, end_detail,
priority, depends_on, experiment}`: `description` what to do and deliver; `reasoning` why, with ids. Order Tasks with
`depends_on` or `task.link`. The user can edit a Task before it is claimed; you cannot.

Inspect profiles with `task.workers`. Assign with `task.assign`: profile, context, rationale. The
watcher starts a wake to do this for accepted Tasks while a Researcher slot is free. When you do not know whether a call landed, send it again unchanged: it will not act twice. A failed Task
cannot reopen: read its `failure` and create a new Task.

## Proposals

Researchers propose with `task_propose`; a management wake brings each one to you. Read it, then
`task.approve {task}` or `task.reject {task, next_step}` with what to do instead. When
`approval_required.task_types` names the type, only the user can accept: ask them, and pass
their message as `cites` in `task.approve` or `task.create`; without `cites` the Task stays
proposed. A Researcher's question arrives the same way: `task.answer
{task, text}`; a note to a running Researcher is `task.message`.

## Ideation and parking

A Goal without open work gets an ideation wake. An ongoing Goal parks on an explicit empty
result. New evidence on a Goal-tagged claim or a linked Experiment verdict can resume it.
Outcome Goals use the `ideation.park_after_rounds` limit. When asked to park, record why and
tell the user what would help.

## Directives and settings

A standing instruction or preference ("never touch the opening book", "keep replies short") is a
directive: `directive.add {text, scope, source}` word for word, `scope` the workspace or a Goal
id, `source` the message. A Task started later gets it
in its briefing; a Task in progress gets it only through `task.message`, so send one before you
tell the user running work will follow it. `directive.update` when the user changes it,
`directive.retire` when they withdraw it, and `claim.retire` any older `preference` claim that contradicts.

When a setting covers it, use the setting instead: `settings_update {key, value, cites}`, and
`cites` is the user's message, always. `settings.read` lists the keys with their scopes,
such as `paused`, `max_researchers`, `approval_required.task_types` and the `submissions` keys.
Settings bound only the agents' own initiative; nothing asked in a running turn is refused by one.

`paused: true` stops everything in the background: no Researcher starts, no merge, no recovery,
no autonomous submission; running Researcher Sessions are drained and resume on `paused: false`.
A new user message waits too until the workspace resumes; a turn already running finishes.

## Abandoning

`task_abandon {task, failure: {what_failed, next_step}}` for a Task that should not continue; a
held run is stopped by the watcher. Both fields are read by people: "rejected by the user: the
Goal changed", not "dropped".
