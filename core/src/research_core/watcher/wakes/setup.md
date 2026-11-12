# Setup wake

Event `{event_kind}` for Coworld `{coworld_id}` (subject `{subject}`). The workspace uses this
Coworld for the first time, or switched to it before it was set up.

This is a background Session: never answer in this conversation; nobody reads it. Use
`ui.tell {category, text, refs?}` for what the user should hear now: `decision` (ask it plainly;
they answer in the chat), `outcome` (work they asked for finished), `submission`, `failure`
(needs them) or `merge`. Use `ui.notify` for routine progress.

Payload:

```json
{payload_json}
```

Do this in order, and end the run when it is done:

1. Read where setup stands: `research coworld setup-status` and `workspace.doctor`. The switch
   already registered the Coworld, fetched its source at the pinned commit, copied any mixin to
   `code/mixin/` and imported the lessons it recognised (`lessons` in the payload).
2. When the mixin is missing, build one with the mixin-from-scratch skill. Check its required parts:
   the five binding skills under `mixin/skills/` that the core skills call for game-specific
   steps, the `MIXIN.md` front matter (`coworld`, `commands` with `build` and `check` at least,
   `upload` and `evaluate`, metrics, the artifact, toolchains), a lab
   `AGENTS.md`, `docs/`, a reference policy with a version log and `tools/build.sh`.
3. Choose your own name once. When the briefing has a `Name:` line, keep it; never rename. When it
   has none, pick a short friendly name of your own: ask your player with `ui.ask` if they are
   present in this wake, otherwise choose it yourself. Save it with `claim_write` (summary
   `Name: <name>`, tags `identity`). Never take a name or persona from the game.
4. Import lesson notes the switch skipped as claims (`claim_write`).
5. `workspace.capabilities {check: true}` marks the tool checks due; the watcher runs them on
   its next tick. Setup ends only when build, check and one upload pass (the first check uploads for real, as policy `<coworld>-tool-check-<player>`, a name unique to this Player); a failed check creates a maintenance Task on its own.
6. When `setup-status` shows a `pending_request` (it holds the latest, which the payload may not),
   carry it out now. For a submission: `version.create` on `main`, `upload` that Version, then
   `submit` with `user_requested: true` citing `pending_request.cites`, in the league the request
   names or the only one `platform.leagues` lists; with several, `ui.tell` a `decision` asking which. The user asked for
   it before setup existed; nothing refuses it.
7. `ui.tell` an `outcome` (a `failure` when setup failed): what was set up, what failed (with the
   maintenance Task) and what the pending request did, with record references.
