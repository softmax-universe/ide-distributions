---
name: environment
description: Use when unsure about paths, environments, CLIs or limits.
---

# Environment

`workspace.environment_guide` is the first thing to call when unsure about the environment; it is generated from the live configuration and the last capability check.

## What the guide covers

Where you are, the image and source commits, the two Python environments, writable and read-only paths, identities, the CLIs, how to reach the user, sleep and wake, limits, and the last capability check. `workspace.doctor` adds the watcher heartbeat, store checks, CLI staleness and stale skill overrides.

## Two Python environments

- **Hermes's interpreter** runs the plugin and the store-only tools (`goal`, `task`, `claim`, `directive`, `settings`, `ui`). Nothing else runs there and nothing is installed into it.
- **The workspace environment** (`tools/venv` under the workspace root; the guide prints its Python) runs the `research` CLI, the platform tools, the mixin commands, `experiment.close` and the watcher. It holds `coworld`, `softmax`, `numpy` and `PyYAML`. Run your own scripts that need those with its Python.
- **Mixin toolchains** live under `tools/<kind>-<version>`, declared in `MIXIN.md` `toolchains`. Setup, merges and full checks install `python` ones (to test first: `uv venv --python <version> <that path>`); install other kinds (Nim) by hand. A command that needs one names it in its `PATH`; a missing one is a repair (`repair` skill).

## Writable and read-only

- Writable: everything in the session's Coworld's folder except its bookkeeping: `code/` (after setup, commit on a branch in a worktree, never on `main` directly), `documents/` (your files; command logs under `documents/logs/`, artifacts, replays and measurements; in no git repository, so never committed; kept through sleep and restarts), and `worktrees/`. `source/` is a clone to read, not to edit.
- Read-only: the profile source checkout (plugin, store library and schema, tools, prompts). Report a bug there; do not patch it. `config.yaml` is writable, but `prepare.py` re-applies the protected keys.
- Shipped skills are in that checkout: do not `skill_manage` one (it fails, or on a local install changes every profile). Keep a fix as a claim tagged `lesson` and report the fault. `create` with a new name makes a skill only this profile sees, kept through sleep, restarts and releases.
- Records live in the store and change through the record tools and the user's edits; direct edits to the store are detected and reported. A record tool returns the updated record; a transition may answer `pending` instead.

## CLIs and identities

- `research softmax exec -- coworld <args>` and `-- softmax <args>` run the official CLIs from the workspace environment as the workspace's Player. The wrapper refuses commands that would change the identity (`login`, `player use`). `-- coworld docs --local` lists the installed reference docs.
- The Player credential is the workspace's; the User credential is the Orchestrator's and may be absent ("User identity not provisioned"). A permission error means the Player may not: report it, never find another credential.
- The `platform` tool's reads, `upload`, `xp` and `version` act as the Player and record what they did; `softmax_api` covers the rest (`softmax-api-routes`).

## Sleep, wake and limits

- Neither profile has a turn or time limit, but a guard ends the turn at five failures of one tool (not `terminal`, `execute_code` and the like), five identical failing calls or ten identical calls with identical results. A workspace tool has about 400 seconds; a mixin command 240 before it is backgrounded with a log and a resume token (`candidate` skill); its result has the log path and tail.
- The watcher ticks every minute: it resumes Sessions whose wait resolved, runs due checks, merges reviewed Tasks and syncs the champion every ten minutes. A Task on `task_wait` is asleep, not finished.
- Reaching the user: the Orchestrator through the conversation or `ui.notify`. A Researcher reaches only the Orchestrator, through its Task (`task_ask`, the result). Wakes never write into a chat.
- `paused` stops background starts and holds new user messages; a turn already running finishes.

## Native terminal store commands

Hermes injects the current session ID into each native terminal command; research resolves that immutable session.
To name the same Coworld explicitly, use `research store snapshot --store coworld --coworld <session-coworld-id>` and the same
`--coworld` selector for store events or user edits. Without a session scope or named selector,
Coworld reads fail.
