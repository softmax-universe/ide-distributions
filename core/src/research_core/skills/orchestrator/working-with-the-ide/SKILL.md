---
name: working-with-the-ide
description: Use when choosing how to reach the user.
---

# Working with the IDE and other clients

Three channels reach the user. Which one you use depends on whether someone is reading.

## In a chat Session

The user sent a message; answer in the conversation. The turn context (`[client context]`)
says which client it is, the mode and the command, what is on screen and the references, and
names any client guide (`guide skill: client-web-ide`): load it. Mode and command outrank your judgement on how to handle a request.

Each turn opens with unread notices, a changed-since note and the session's Coworld; mention what
matters, do not recite the list. The first turn of a new chat after an hour or more away carries
`[while you were away …]` with counts from the events log: open with two or three sentences
(what finished, what was submitted, what needs a decision, with record ids), then answer.

## In a wake

Nobody is reading; a wake's chat text reaches no one. Finish with `chat_update`, or nothing when
nothing changed.

## `chat_update`, wakes only

`chat_update {category, text, refs?}` puts a message in the user's latest chat; it is refused in
a chat, where you reply instead. Plain language, one or two sentences with ids.

- `decision`: only the user can decide (a preference, a credential, a choice between Goals, a
  submission in mode `ask`); ask it plainly, and they answer in the chat. Decide everything else
  yourself; a question a tool could answer is not a decision.
- `outcome` (work they asked for finished), `submission`, `failure` (needs them) or `merge`.
- `progress`: routine news. It becomes a notice at once, shown at the user's next chat turn, and
  never interrupts the chat.

Categories the `chat_updates.categories` setting omits also become notices; the result says so.

## Asking the user

In a chat, ask in your reply. In a wake, send a `decision` `chat_update`.

## Answering during setup

Nothing else in a Session starts setup. When the briefing says it has not started, call
`workspace.coworld_switch {coworld, cites}` first, which brings the setup wake. A user who asks
for something meanwhile gets an answer at once: say that setup is running and that their request
runs when it is ready, adding `pending_request: {text, cites}` to the call. The setup wake ends
by carrying the request out (for a submission: a Version on `main`, upload, submit citing their
message) and telling the user. Never make the user wait for setup, and never refuse because it is
running. Questions that need only platform reads (`platform.standings`, `platform.leagues`) are
answered now.

The setup call takes the source repository from the platform manifest. When the platform does not
list the Coworld, or the user names a fork, pass `source` (a git URL) and `commit`; a
repository without `MIXIN.md` gets a mixin built from scratch.

## Skill overrides

The code repository may override a core skill under `.agents/skills/<skill>/`. `workspace.doctor`
flags an override whose upstream changed. Suggest the update to the user with
what changed; the user decides. Mixin skill changes go live on merge.
