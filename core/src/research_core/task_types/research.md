```json
{
  "required": [],
  "checklist": [
    {
      "key": "claims",
      "text": "claims recorded (C- ids), or none: <reason>",
      "pattern": "^C-[1-9][0-9]*$",
      "min_items": 1,
      "none_ok": true
    }
  ]
}
```

# Research Task

Deliver claims: short findings with typed evidence that the Goal can use. No policy code
changes: a research Task reads source, replays, the forum and wiki, standings and episode
results, and records what it learns; the improvement Task that follows changes the policy.

Start: read the Task and its Goal (`task.read`, `goal.read`), the directives (`directive.list`),
the briefing and these instructions; load the method skill the Task calls for (`survey`,
`scouting`, `analyze-opponents`, `diagnose`, `replay-diagnosis`, `coworld-source`), and
`writing-claims` before the first claim. Search first: `claim.search` and `experiment.search` for what is known; a
refuted claim is a lead.

Work: every claim rests on at least one evidence link (`code` at a commit, `doc`, `replay`,
`measurement`, `transcript`, `experiment`, `version`, `claim`, `annotation`). Summary at most 40 words, body
at most 500; tag it with the Goal's id (`G-n`). Retire what new evidence supersedes or refutes
(`claim.retire`). Game-specific reading: see `code/mixin/skills/<coworld>-<binding>/SKILL.md` or the mixin docs.

Ask (`task_ask`) when the question itself is unclear or the Goal would change; decide method,
scope and which claims to write yourself. After `task_wait`, end your run yourself; the
watcher resumes you.

End: `task_complete {task, claims: ["C-12", "C-13"], result, merge: "no", merge_reason}`; a
research Task merges nothing unless it changed documents in the mixin. When nothing held up,
`claims: "none: <reason>"` with a reason the Goal can act on ("the lever is already at the
mixin's floor; see C-4").
