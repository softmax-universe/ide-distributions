```json
{
  "required": [],
  "checklist": [
    {
      "key": "commit",
      "text": "the committed change on the Task branch (a git sha)",
      "pattern": "^[0-9a-f]{7,40}$"
    },
    {
      "key": "summary",
      "text": "a summary of the change",
      "pattern": "\\S"
    },
    {
      "key": "capability",
      "text": "the capability this repair restores",
      "pattern": "^\\S+$",
      "required_when": "capability"
    }
  ]
}
```

# Maintenance Task

Deliver a committed change on your Task branch and a summary; never a Version. A maintenance
Task fixes the workspace: a broken capability, a mixin command, a toolchain pin, a document.
Policy behaviour belongs to an improvement Task.

Start: read the Task, its Goal when it has one (a repair may have none), the directives, the
briefing and these instructions; load `repair` and `environment`. `workspace.capabilities`
shows what the last check found; the Task's `capability` field names the one to restore, and
the failure result carries the log path, output and exit code.

Work: reproduce the failure first, from the log and the mixin's command in `MIXIN.md`. Fix the
cause, not the symptom: a missing toolchain is pinned in the `MIXIN.md` toolchains; a changed
API is followed in the mixin's tool; a check that fails for a real defect is fixed in the code
it checks. Rerun the command yourself: its passing output is your evidence (`repair`). Commit a
small change with a message saying what broke and why this fixes it. Game-specific build or
evaluate details: see the mixin's docs.

Ask (`task_ask`) when two fixes differ in what the user would want (upgrade or pin a runtime);
decide everything else.

End: `task_complete {task, commit, summary, capability, merge: "yes", result}`. `commit` is on
your branch; `capability` is required for a repair. The change merges after review once build
and check pass again; the watcher then rechecks the capabilities.

When the Task cannot succeed, `task_complete {task, outcome: "failed", failure: {what_failed,
next_step}}`.
