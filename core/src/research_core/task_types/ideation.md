```json
{
  "required": [],
  "checklist": [
    {
      "key": "proposed_tasks",
      "text": "two to five proposed Tasks for the Goal (T- ids)",
      "pattern": "^T-[1-9][0-9]*$",
      "min_items": 2,
      "max_items": 5
    }
  ]
}
```

# Ideation Task

Deliver two to five proposed Tasks for the Goal, each with reasoning grounded in the Goal's
claims and in what was tried. No code: an ideation Task reads and proposes.

Start: read the Task, its Goal (`goal.read`: the end state, "where it stands", the claims tagged
`G-n` and how many Experiments it has in each status), the directives, the briefing and these
instructions; load `proposing`. Read what is known: `claim.search {query: <lever>,
include_retired: true}`, and `experiment.search {query: "G-n"}` and by lever for what was tried,
whatever its verdict. A refuted claim or a failed Experiment is a lead: what changed since, what
was not controlled. Read the league
now: `platform.standings`, `platform.active_policies {league, exclude_player: true}`, the champion.
Game-specific levers and failure vocabulary: see the mixin's `<coworld>-diagnosis` and
`<coworld>-ab` skills or docs.

Work: for each idea, `task_propose {goal, type, title, description, reasoning, for_human,
end_condition, end_detail, priority}`.
`reasoning` cites claims (C-n) and Experiments (X-n) by id; `description` is concrete enough
that a Researcher starts cold. Types are `improvement`, `research` or `maintenance`, never
another `ideation`. Rank them: the first is what you would run. Two strong proposals beat five
weak ones; five is the ceiling, not the target.

Ask (`task_ask`) only when the Goal's end state cannot be read; the choice of ideas is yours.

End: `task_complete {task, proposed_tasks: ["T-7", "T-8"], result, merge: "no", merge_reason:
"ideation changes no code"}`. `result` says in two sentences what the Goal should try first and
why.
