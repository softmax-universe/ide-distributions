---
name: candidate
description: Use when building a Version with one conceptual change.
---

# Candidate

A Version is a policy candidate built on purpose: a commit in the code repository plus the artifact the mixin's `build` command produced from it. One conceptual change per Version, the Experiment's lever and nothing else, so the verdict says something about the mechanism.

## Where to work

- A Researcher works in its Task's worktree (branch `task/<n>`, from `task.claim`).
- The Orchestrator works in `worktrees/orch-<n>` from `workspace.worktree_open`, or builds what is on `main` from `code/` when the user asks for exactly that (a starter, the current policy).
- Never commit candidate changes on `main` directly; merges happen after review.

## Method

1. Read the Experiment (`experiment.read`), the reference policy and the game's build contract in `MIXIN.md` and the lab `AGENTS.md`. Which files carry a lever, how to build and what passes `check` are game-specific: see the mixin's `<coworld>-diagnosis` skill or docs.
2. Edit only the components the change names. Diff against the baseline's commit (`version.read {version}` gives it): `git diff <baseline commit> -- <paths>` must show that change and nothing else.
3. Run the mixin's `check` command from `MIXIN.md` in your worktree. After reviewing the diff, combine the check and commit in one terminal call with `&&`: a failed check must prevent the commit. A failed check is a result: fix the candidate within the one change, or record the failure. An errored tool is a broken capability (`repair`).
4. A Version is a commit, not a working copy: `version.create` refuses a dirty tree or inputs git does not track, and lists the files. Do not spend another model round solely to commit after a successful check; the conditional command above does both.
5. `version.create {summary, based_on: [V-base], task, experiment}` (Orchestrator: `worktree: orch-<n>`). It runs `build` detached, hashes the artifact, records the Version at the exact commit and tags it `v/<n>`; the exact bytes are kept so `upload` sends what was built.
6. Check the artifact implements the change before anyone evaluates it: passed gates alone do not prove the intended value moved. Open the built file or run the inspection the binding names.

## Long builds

`build` runs with a 240-second budget. Past it, `version.create` answers `{"ok": true, "background": {"pid", "log", "resume"}}` and the build keeps running.

- A Researcher calls `task_wait {task, kind: process, ref: <pid>, detail: "build of <commit>"}` and ends its run, even if the build looks close to done; on resume it calls `version.create {summary, resume: <token>}` with the first call's other arguments too, to record the Version.
- The Orchestrator reads the log, finishes other work and calls `version.create {summary, resume}` with the first call's other arguments once the process has ended.
- Never launch the build again because it has not finished; the log under `documents/logs/` shows progress.

## Failure handling

- A failed build: the refusal carries the log path and its tail, and the `build` capability is reported broken with a repair Task. Read the whole log, fix the environment or the command (`repair`), rebuild.
- Image policies use `artifact.kind: image` and an OCI archive at `artifact.path`. Use `coworld build-policy` with the configured BuildKit endpoint, then `version.create` and `upload`. Keep the archive outside the Docker build context.
- The baseline changed (a new champion) while you built: the Version is still valid; the Experiment's baseline is locked, so note it and let the review decide whether a new Experiment is needed.
- The same commit builds to a different artifact digest: the build is not reproducible, a maintenance finding to record.
