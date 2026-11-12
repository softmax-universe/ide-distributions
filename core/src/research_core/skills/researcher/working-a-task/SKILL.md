---
name: working-a-task
description: Use at the start of every Task Session, before any work.
---

# Working a Task

One Task per Session, in the Task's worktree, speaking only to the Orchestrator through the
Task.

## 1. Claim

The assignment prompt names the Task and your first call: `task.claim {task: T-n}`. The
workspace reserved ownership before starting you. This call renews that lease, creates
`worktrees/task-<n>/` on branch `task/<n>` from `main`, and returns the Task, its Goal's id, the
type's instructions and the Orchestrator's messages. A refused claim names its reason (ownership,
an unset repository, an unfinished dependency): end your run without writing anything.

## 2. Read before working

- The briefing: your Player's id, the champion, the directives, the broken capabilities, your
  Task.
- `task.read {task}` for the full record, `goal.read {goal}` for the end state the work is judged
  by, `directive.list` for standing instructions on the workspace and the Goal.
- The type's instructions (returned by the claim) and the skills they name; the mixin's
  `code/mixin/skills/<coworld>-<binding>/SKILL.md` files (`read_file`) for every game-specific step.
- `claim.search` and `experiment.search` for what is known or was tried: a refuted claim or an
  abandoned Experiment is a lead, not a gap.

## 3. Work in your worktree

Edit code only inside your worktree and commit on your branch with messages that say what and why;
never touch `main` or another worktree. `version.create {summary, task}` builds from your
committed tree and refuses a dirty one, listing the files. Record findings as you go:
`claim_write` with evidence, `experiment_create`; `xp.request` records its runs. The record is the
report; there is no second write-up. Keep the Task small enough to finish; a bigger idea becomes
a proposal (see `proposing`).

## 4. Ask or decide

`task_ask {task, question}` when the decision is not yours: the Goal is ambiguous, two readings
of a directive conflict, the Experiment would cost far more credits than the Task implied, or
the result changes what the Goal should be. The question sets a wait: end your run, and the
answer resumes you. Decide yourself everything inside the Task: the method, the seeds, which
claim to write, how to fix a broken build. A question you can answer with a tool is not a
`task_ask`.

## 5. Wait, then END YOUR RUN

Anything outside your control is a wait: `task_wait {task, kind, detail, ref}` with kind `xp`
(no `ref`: your Task's open requests), `credits` (after a 402), `process` (`ref`: the pid of
a background build or evaluate), `permit` (`ref`: the route to read), `submission` (`ref`: the
policy version id), `platform` (set for you when a route keeps failing), `user` (`ref`: the setting
or T-n their answer changes) or `dependency` (`ref`: the T-n). Then stop: end the run with no further tool call. That
is not quitting: the watcher continues the Task when the wait resolves, usually in a new Session.
Never poll in a loop and never sleep. On resume, call the tool you were waiting for
(`xp.status {experiment}` for XP) and `directive.list`, since directives may have changed while you waited.

## 6. Complete

`task_complete {task, outcome: "succeeded", commit, merge, merge_reason, submission_candidate,
submission_reason, result, ...}`, plus the fields the type's checklist names (`claims`, `experiment`,
`versions`, `proposed_tasks`, `commit`, `summary`, `capability`); the tool refuses with every missing item at once, so fix them all and call
again. `commit` is the commit on your branch to merge; `merge: no` needs `merge_reason`. `result`
is two or three sentences the Orchestrator reads first.

`task_complete {task, outcome: "failed", failure: {what_failed, next_step, error, exit_code,
tried}}` when the Task cannot succeed. Fill `tried` so
nobody repeats it.

`task_release {task, reason}` when you must not continue, as when a directive forbids the only
path; tried and it did not work is `outcome: "failed"`. End your run afterward. The workspace stops your recorded run, then returns the Task to scheduling.

## 7. What happens next

A review wake reads your Task, Experiment and claims. It accepts, and if the merge is on the watcher merges your
commit after the mixin's build and check pass again, or it reopens the Task with a reason: the
Task resumes with that reason, so read it and act on it rather than defending the first
attempt. A proposed submission is decided by the Orchestrator under the user's settings; you
never submit.
