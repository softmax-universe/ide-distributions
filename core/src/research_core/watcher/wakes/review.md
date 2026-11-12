# Review wake

Event `{event_kind}` on `{subject}` (Coworld `{coworld_id}`). A Researcher completed a Task, or
the Orchestrator finished a change in its own worktree; decide whether the work is accepted and
merged.

This is a background Session: never answer in this conversation; nobody reads it. Use
`ui.tell {category, text, refs?}` for what the user should hear now: `decision` (ask it plainly;
they answer in the chat), `outcome` (work they asked for finished), `submission`, `failure`
(needs them) or `merge`. Use `ui.notify` for routine progress.

Payload:

```json
{payload_json}
```

Do this, then end the run:

1. `task.read` the Task with its checklist. Read the Experiment (`experiment.read`), the new
   claims (`claim.read`) and the Researcher's answers recorded on the Task. Judge the evidence,
   not the summary: the results against the champion, the decision rule, the replays named as
   evidence, whether the deliverables the checklist names exist.
2. Call `task.review {task, accept, merge, reason}`. Accept and merge when the work is sound and
   `main` stays healthy; accept without merge when the result is a finding, not a change;
   reject with a specific, actionable reason when something must be redone, and the Task
   continues with your reason.
3. The watcher merges accepted work mechanically and re-runs the mixin checks; a merge that
   conflicts or fails its checks comes back to you as a management wake. A submission candidate
   you just accepted waits for that merge, then a `submission.candidate` wake decides it; a user's
   earlier request to submit is carried out, never gated.
4. Report the outcome in one or two sentences with the record references: `ui.tell` it as an
   `outcome` when the user asked for this work (or `merge` when it merged), else `ui.notify`.
