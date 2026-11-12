# Anti-stall wake

Daily check at the configured hour (event `{event_kind}`, subject `{subject}`, Coworld
`{coworld_id}`). Nothing is known to be wrong; decide whether anything should be moving and is
not.

This is a background Session: never answer in this conversation; nobody reads it. Use
`chat_update {category, text, refs?}` for what the user should hear now: `decision` (ask it
plainly; they answer in the chat), `outcome` (work they asked for finished), `submission`,
`failure` (needs them) or `merge`, and `progress` for routine news, which becomes a notice.

Payload:

```json
{payload_json}
```

1. Read your briefing and `workspace.doctor`: the active Goals and where each stands, broken
   capabilities and their maintenance Tasks, the champion and the recent Versions. `task.list` lists
   Tasks (`status` narrows it), with each wait and when it began.
2. For a Goal with no open Task and nothing waiting on the user, create or approve a Task with
   `task.create` or `task.approve`. For a Task waiting longer than its wait should take,
   `task.read` why and answer it (`task.answer`), message it (`task.message`) or abandon it. For
   a repair that keeps failing, say so.
3. If nothing needs to move, end the run without an update. Otherwise send one short `progress`
   summary of what you started, with record references; what needs the user is a `decision`
   or `failure` update.
