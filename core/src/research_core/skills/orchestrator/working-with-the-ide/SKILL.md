---
name: working-with-the-ide
description: Use when choosing between chat, the ui tool and a notice.
---

# Working with the IDE and other clients

Three channels reach the user. Which one you use depends on whether someone is reading.

## In a chat Session

The user sent a message; answer in the conversation. The turn context (`[client context]`)
says which client it is, the mode and the command, what is on screen and the references, and
names any client guide (`guide skill: client-web-ide`): load it. The session context says which
panels the client can show. Mode and command outrank your judgement on how to handle a request.

Each turn opens with unread notices, a changed-since note and the session's Coworld; mention what
matters, do not recite the list. The first turn of a new chat after an hour or more away carries
`[while you were away …]` with counts from the events log: open with two or three sentences
(what finished, what was submitted, what needs a decision, with record ids), then answer.

## In a wake

Nobody is reading. A wake's UI state goes to its Coworld's shared conversation, which every client
sees; its chat text reaches no one. Finish with `ui.tell` or `ui.notify`, or neither when
nothing changed.

## The `ui` tool

It writes UI state and never waits on a client.

- `ui.show {ref}` or `{view}`: bring a record (G-3, X-5, V-7) or a client view into view when
  the user would otherwise have to find it.
- `ui_panel_set {panel, kind, content}`: a named panel with a declared kind (`markdown`,
  `record`, `table`, `checklist`, `file`, `link`, `replay`, `html`, `agent-context`). Never assume Markdown. A
  `file` panel names a file under the Coworld's `documents/` folder; a `replay` panel a replay
  or a moment in it; `html` only when the client can show it.
- `ui.notify {text, refs, urgency}`: plain language, informs and never asks, one or two
  sentences with ids, for routine progress. Urgency `high` only for what the user should act on
  today.
- `ui.tell {category, text, refs?}`, wakes only: a message in the user's latest chat.
  `category`: `decision` (ask plainly; they answer in the chat), `outcome` (work they asked
  for), `submission`, `failure` (needs them) or `merge`. Categories the
  `chat_updates.categories` setting omits become notices.
- `ui.ask {question, options, refs}`: only for what the user alone can answer (a preference, a
  credential, a choice between Goals, a submission in mode `ask`). The first answer from any
  client wins and arrives as a UI action. Decide everything else yourself; a question a tool
  could answer is not a `ui.ask`.

## The agent-context panel and its needs section

The watcher computes the `agent-context` panel: Coworld, Player, Goals, champion, Versions,
running Tasks, capabilities. From any Session, you write only its "I need from you" items:
`ui_panel_set {panel: "agent-context", kind: "agent-context", content: {needs: [...]}}`, each
item one sentence saying what and why, with a record id when there is one. List only what the
user alone can supply (a User credential, a decision on a parked Goal, a repository grant).
Clear an item as soon as it arrives; a stale need is worse than none.

## Answering during setup

Nothing else in a Session starts setup. When the briefing says it has not started, call
`workspace.coworld_switch {coworld, cites}` first, which brings the setup wake. A user who asks
for something meanwhile gets an answer at once: say that setup is running and that their request
runs when it is ready, adding `pending_request: {text, cites}` to the call. The setup wake ends
by carrying the request out (for a submission: a Version on `main`, upload, submit citing their
message) and notifying. Never make the user wait for setup, and never refuse because it is
running. Questions that need only platform reads (`platform.standings`, `platform.leagues`) are
answered now.

The setup call takes the source repository from the platform manifest. When the platform does not
list the Coworld, or the user names a fork, pass `source` (a git URL) and `commit`; a
repository without `MIXIN.md` gets a mixin built from scratch.

## Skill overrides

The code repository may override a core skill under `.agents/skills/<skill>/`. `workspace.doctor`
flags an override whose upstream changed. Suggest the update to the user with
what changed; the user decides. Mixin skill changes go live on merge.
