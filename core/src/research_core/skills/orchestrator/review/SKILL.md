---
name: review
description: Use in a review wake to judge a completed Task.
---

# Review a completed Task

A review wake starts you when a Task's completion passed its type checklist, or when a change
of your own finished in its worktree. Code checked that the deliverables exist; you check that
they mean what they say. Nobody is reading a chat: tell the user with `chat_update`.

## Read before you judge

1. `task.read {task: T-n}`: the description and reasoning, the checklist values, the recorded
   commit, `merge` and `merge_reason`, `submission_candidate` and `submission_reason`, `result`
   and the messages.
2. `goal.read` on its Goal: does the result move the end state, or only the Task?
3. `experiment.read {experiment: X-n}`: the hypothesis and methods as locked at start, the runs,
   the verdict and its reason codes. The decision rule was pre-registered: check that the verdict
   follows from the stored rows, that both arms met the mixin's minimum episodes, and that
   nothing differed between arms except the candidate. Game-specific metrics and floors: see
   `code/mixin/skills/<coworld>-ab/SKILL.md` and `code/mixin/skills/<coworld>-eval-design/SKILL.md`
   or the mixin docs.
4. `claim.read` on each new claim: a summary of at most 40 words, evidence whose stance supports
   it, no duplicate of what `claim.search` already finds. `claim.inspect_evidence` when a claim
   rests on one measurement or one replay.
5. A maintenance Task: the committed change and, for a repair, that the failing command now
   passes on the branch.

## Decide with one call

`task.review {task, accept, merge, reason}`. With `accept: false`, send `merge: no`.

- `accept: true` when the deliverables are real and the reasoning holds. A refuted hypothesis
  with sound evidence is a completed improvement Task, not a failed one.
- `accept: false` reopens the Task, which continues with your reason. Write what to do next,
  not what is wrong. Good: "C-12's evidence does not show its cause; add the replay." Bad:
  "insufficient evidence." A Task keeps its Experiment: for a new one, accept with `merge: no`
  and `task.create` a follow-up.
- `merge: yes` when the change is the point: an improvement whose Experiment confirmed, a
  maintenance fix, a mixin edit. The watcher merges the recorded commit with no model after the
  mixin's build and check pass again; a conflict or a failed check comes back to you as a
  management wake (see `merge-conflicts`).
- `merge: no` when the result is a finding, the Experiment refuted or was inconclusive, or the
  code would regress `main`. The branch is kept in every case; say in `reason` why nothing
  merges.
- `reason` is what the user reads in the events log: one or two plain sentences with record ids.

Do not reopen for polish. Reopen when a deliverable is wrong, unsupported or unsafe to merge.

## Carry a submission proposal forward

When `submission_candidate` is yes, or the Researcher proposed a Version for submission, decide
now rather than leaving it to the next wake.

1. `version.read {version: V-n}` for its submission status; `settings.read` for
   `submissions.mode`, `until`, `max_count`, `quality_bar` and `min_margin_over_champion`.
2. The quality bar for a submission on your own initiative: the Version is merged into `main`
   and its Experiment against the champion confirmed its decision rule with an effect at or above
   the margin; with no champion, a merged Version that passes the mixin's checks. `submit`
   applies this itself and names each shortfall.
3. Mode `autonomous` and the Version already merged: `submit {version, league, reason}`, which
   writes its own notice.
4. Mode `autonomous` but the merge you just approved has not landed: you cannot submit in this
   wake. Say in `reason` that V-n is a candidate pending merge. The watcher merges it within
   minutes and wakes a management Session (event `submission.candidate`), which submits it
   under the same rule.
5. Mode `ask`: `chat_update {category: decision, text, refs: ["V-n", "X-n"]}`: whether to submit,
   with what the Experiment showed.
6. A setting or the bar stops it: a `progress` update saying why, with the ids, so the user can ask for it in
   chat. A user's request overrides every setting.

A submission the user already asked for in chat is never gated: carry it out as the
`submission` skill says.

## End

Report the outcome in one or two sentences with the record ids (T-n, X-n, V-n), then end the
run: an `outcome` `chat_update` if the user asked for the work, else `progress`.
