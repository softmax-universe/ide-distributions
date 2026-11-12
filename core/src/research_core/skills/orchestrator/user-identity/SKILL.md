---
name: user-identity
description: Use before acting as the user's own User identity.
---

# Acting as the user's User identity

You hold two identities. Everything runs as the workspace's Player unless you say otherwise.
The User identity is the user's own account; the briefing says whether its credential is
provisioned. When it is absent, say "User identity not provisioned", do what the Player can,
and tell the user once that it is missing.

## The rule

Reads are always allowed. Writes are allowed whenever they serve the user's request or an active
Goal. Every write is recorded in the events log as the User identity and leaves a notice that informs
and never asks. Before a write the user would be surprised by, ask first.

## What needs the User identity

- Other Players of the user: `platform.policy_versions {player, identity: "user"}`.
- Policy files the Player may not read: `platform.policy_file` tries the Player first and falls
  back to the User on a refusal; nothing to do.
- Opponent analysis that excludes every Player of the user, not only yours:
  `platform.active_policies {league, exclude_user: true}`.
- Account-level routes: the account's grants, anything the platform gates to Users:
  `softmax_api {method, path, identity: "user"}`. The `softmax-api-routes` skill lists the
  routes.
- Forum posts and wiki edits in the user's own name when they asked for that:
  `forum_post.post {title, body, identity: "user", cites}`, `wiki_edit` with `identity: "user"`.

## How to decide a write

1. Does it serve what the user asked for in this conversation, or an active Goal? If yes, do it.
   `cites` is only ever the user's message: it lifts a `platform_writes` setting, and a Goal does not.
2. Would the user be surprised to find it in the events log? If yes, ask first (in a chat, in your
   reply; from a wake, a `decision` `chat_update`); this is one of the things only they can answer.
3. The write leaves a notice: "Acted as your User identity: <what> (<ids>)". `softmax_api` (for
   any write), `forum_post` and `wiki_edit` write it themselves.

## Never

- Never log in, switch Player or look for another credential: a permission error means that
  identity may not do it; report it.
- Never act as the User to get round a setting that bounds your own initiative: settings bound
  the agents, not identities, and a user request overrides them anyway.
- Never assume the Player in a conversation is you: name Players by id.
