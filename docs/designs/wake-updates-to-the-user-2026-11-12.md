# Background updates that reach the user (2026-11-12)

> **Implemented (2026-11-12, branch `jb/wake-chat-updates`).** This is the
> `ui.tell` design (Part 1 of the first draft). Part 2 made notices an AIDE resource. James
> cancelled it, so this copy leaves it out (see "Not in scope and known limits"). Two changes came
> in during implementation: the `category` field and its `chat_updates.categories` setting (see
> "Which updates reach the chat"). Where this document and the code differ, `docs/hosting.md`
> ("Coordinator and pause") and `core/src/research_core/store/chat_updates.py` are the current
> truth.

## Problem

A background Orchestrator wake (review, management, anti-stall, setup) cannot reach the user in the
hosted image:

- **No path into a chat.** A wake runs in its own Hermes Session. No tool writes into a chat, and
  the wake instructions forbade it (`core/src/research_core/coordinator.py` `WAKE_INSTRUCTIONS`,
  every `watcher/wakes/*.md`).
- **`ui.notify` is invisible.** It writes a `notices` row (`store/notices.py`). The only reader is
  the Orchestrator's own turn hook at its next chat turn (`plugin/turn.py`). The AIDE
  backend (`image/aide.py`), the only chat path since #236, serves sessions, tasks, goals and
  requests, but no notices.
- **`ui.ask` from a wake is invisible too.** It writes the shared conversation's UI state, which
  AIDE does not serve.

So a finished review, a merged Version, or a decision only the user can make waits until the user
types. Even then, the user hears about it only if the Orchestrator chooses to mention it.

## Design: `ui.tell`, a wake's way to say something in the user's chat

A new `ui` action, Orchestrator only: `tell {category, text, refs?}`.

- **Wake Sessions only.** In a chat Session it is refused with "you are in the chat; reply here".
  It is also refused in any Session that is not a wake.
- **Validated like `notify`.** `text` must be non-empty and at most 4,000 characters. `refs` is a
  list of non-empty strings, with at most 20 entries. `category` must be one of the fixed values
  below.
- **Recorded, not sent at once.** It writes a row to a new workspace-store table `chat_updates`
  (migration `0013_chat_updates.sql`). The row has `id`, `coworld_id`, `category`, `text`, `refs`,
  `created`, `created_by`, `admission_id` and `notice_id`. `admission_id` is NULL until a chat
  admission carries the row. `notice_id` is set when the row becomes a notice.
- **The coordinator delivers.** `Coordinator.advance` has a new step,
  `deliver_chat_updates`, beside `deliver_worker_messages`. Each pass, it reads the pending rows
  for the pass's Coworld. They are due when the newest is at least `DEBOUNCE` (120 s) old, or the
  oldest is at least `MAX_WAIT` (10 minutes) old. A full batch of 20 is also due at once. The
  target chat is the Coworld's most recently active top-level chat Session. "Most recently active"
  uses the order AIDE's `session.page` uses to offer a chat, without its league filter: the later of the Session's start and
  its latest user message, then the start, then the id. If the Coworld has no chat, the rows
  become notices (`notices.add`), and each row records its notice. Otherwise the coordinator
  enqueues **one** admission into the chat with `admissions.enqueue_in_transaction(...,
  origin="followup", key="chat-updates:<first id>-<last id>")`, the same path #263 uses. Every row
  it included gets `admission_id` set. If that chat already has a `chat-updates:` admission that
  is still queued, the coordinator waits for that one first.
- **Idempotent.** The admission and the rows' receipts commit in one transaction. The admission key
  is built from the row ids, and its id is derived from that key. If a coordinator restarts after
  the commit, it sees no pending rows. If it restarts before the commit, nothing was written.
  Replaying the same batch returns the admission that already exists.
- **The durable inbox protects the conversation.** A queued admission starts only after the chat's
  live run ends, so an update never cuts into a reply.
- **The chat turn decides how to say it.** The admission text starts with this instruction:
  "Background updates arrived from your own background work. Tell the user about them in this
  chat, briefly. If the conversation is on another topic, give each update a sentence or two and
  go back to that topic; do not drop what you and the user were doing. Lead with anything the
  user must decide. Read the records before you report numbers." Then it lists each update with
  its category, time and refs.
- **Paused workspace.** Nothing is delivered while `settings.paused` is set, the same as #263.
  The rows wait.
- **Events.** `chat_update.recorded` and `chat_update.delivered` are store events, unwatched by
  the watcher. They carry metadata only, and telemetry takes the Coworld, category, Session,
  admission and count from them (`agent_telemetry/observed.py`).

### Which updates reach the chat

`category` says why the user should hear the update now:

| Category | Meaning |
|---|---|
| `decision` | Only the user can decide; ask it plainly, and they answer in the chat. |
| `outcome` | Work the user asked for finished: a review accepted or rejected, an Experiment they asked for closed. |
| `submission` | A Version was submitted, placed or rejected. |
| `failure` | Something broke that needs the user. |
| `merge` | A Version merged to main. |

The workspace setting `chat_updates.categories` lists the categories that reach the chat. Its
default is every category except `merge`, because merges are routine. A `tell` whose category the
setting leaves out is written as a notice at once. The tool result says so, so the wake knows the
update will not interrupt the chat. An empty list sends every update to notices, so no separate
on/off switch is needed. The debounce windows stay constants.

The field is named `category` rather than `kind` because `ui_panel_set` already has a `kind` field
with a different enum. The grouped `ui` tool validates fields by name across its actions, so a
second `kind` field would collide with it.

### Wake instructions

The wake instructions (`WAKE_INSTRUCTIONS` and the four Orchestrator wake files) used to say
"never write into a user conversation; `ui.notify` for news, `ui.ask` for a question". They now
say: never answer in this conversation, because nobody reads it. Use `ui.tell`, with the category
that says why, for what the user should hear now. Use `ui.notify` for routine progress. Wakes ask
their decisions as a `decision` tell instead of `ui.ask`. The Orchestrator SOUL changed by the same
number of words, so it stays at 2,492 of its 2,500-word budget.

## Not in scope and known limits

- **Notices still reach the user only through the next chat turn.** The AIDE backend serves no
  notices. A notice (a `ui.notify` call, a category the setting leaves out, or an update for a
  Coworld with no chat) appears only when the turn hook lists unread notices at the user's next
  chat turn.
- **`ui.ask`, `ui.show` and `ui.panel_set` are invisible in the hosted image.** AIDE serves no UI
  state. Wakes no longer rely on `ui.ask`. The chat side needs its own fix.
- **A sleeping workspace is not woken** when platform work finishes. Updates are recorded and
  delivered at the next wake.
