---
name: minecraft-controller
description: Use when authoring or debugging Minecraft controllers.
---

# Minecraft controllers

Use this guide for `softmax-universe/coworld-minecraft` gameplay controllers.
First read `coworld-source` and identify the exact source revision and runtime.
The Practice Yard is a deterministic simulation, not the live Paper server.
Its score cannot establish that movement, crafting, or multiplayer works.

## Discover the body's contract

Read the selected revision's `bot/sdk/README.md`, `game-pack/howto.md`, and
`game-pack/seeds/README.md`. Inspect `bot/sdk/schema.js` for accepted actions.
Use `node scripts/run-controller.mjs --list` when that revision supports it;
otherwise inspect `brain/seeds.mjs`. Retrieve only the selected controller's
source and parameter description. Optional seeds are examples, not orders.

Shared runtime integration belongs in `softmax-universe/sdk`; read its README,
project status and source ownership before editing. Tracked runtime files have
another owning repository. IDE profiles do not own Minecraft's movement loop.

## Write one bounded controller

The sandbox calls synchronous `function policy(obs, mem)`. It has no imports,
network, timers, or raw Mineflayer bot. Do not paste upstream async skills into
it. Goals and skill memory belong to the cogent's workspace.

- Require a connected, living observation. Retain `sessionId`; fail on a new
  session so old action tags cannot move a respawned player.
- Issue one explicit action through `h.act`. Return its controls while running.
  Wait for that exact receipt before planning from changed inventory.
- Call `h.next` only after settling the operation. A duplicate tag deduplicates
  recent delivery; a new tag starts a new effect. Do not blindly retry a timed
  out craft, placement, or inventory transfer.
- Count progress from fresh `sequence` values and observed position/inventory
  changes. Give the whole skill a finite budget. A timer renewed at every retry
  is not a bound. Stop with `fail` when stuck, then inspect the evidence.
- Completion of a mutating action needs `observed_effect`; command acceptance
  is insufficient. A read such as recipe inspection may instead return a
  documented settled result. Return `done` only after checking the skill's goal.
- Stay within player-visible targets and ownership rules. No administrator
  commands, teleporting, hidden-world searches, or opening others' containers.

## Prove the workflow

Use a candidate worktree, never the release-pinned reference source. Against an
explicitly selected development server, run:

```sh
node scripts/run-controller.mjs path/to/controller.js \
  --name RegisteredTestPlayer --host 127.0.0.1 --port 25665 \
  --params '{"count":1}' --timeout 90 --journal /tmp/minecraft-run-UNIQUE.jsonl
```

Use a unique artifact path. Journal output refuses overwrite. Check the final
JSON and exit status; correlate requests and receipts by tag and session, then
verify inventory or position. A chat reply claiming movement is not evidence.

The repository's `tests/e2e/sdk.mjs` and `sdk-skills.mjs` create terrain with
server commands. Run them only on a disposable loopback Paper world, with its
RCON settings. Never aim fixture setup at the player's shared server.

Cover success, blocked progress, a session change, and an uncertain or duplicate
action. Report the tested commit, selected skill/parameters, server version,
commands, observed effects, and remaining gaps. Do not claim a deployed fix
from a passing local test or a merged PR alone.
