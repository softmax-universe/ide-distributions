---
name: merge-conflicts
description: Use when a merge conflicted with main or failed its checks.
---

# Resolve a failed merge

The watcher merges accepted work with no model: `main` clean, the recorded commit, `--no-ff`,
then the mixin's build and check again. When the branch conflicts with `main` or a check fails,
`main` is unchanged and a management wake brings it to you with the Task and the reason in the
payload, plus the files on a conflict or the command, log path and output tail on a failed check. Choose one of three outcomes; never force a merge and
never commit on `main` directly.

## Read first

1. `task.read {task: T-n}`: the recorded commit, the branch, the merge reason and what the Task
   was for.
2. The log the payload names, if any: full output, exit code, the failing command. A result tail is
   2,000 characters; the log is complete.
3. In the Coworld's `code/` folder, read-only: `git log main..task/<n>` and `git diff main...task/<n>`
   for what the branch changes, and `git log task/<n>..main` for what moved on `main` since it branched.

## Outcome 1: resolve it yourself

For a conflict you can judge (both sides touched the same file; a dependency pin; a renamed
function):

1. `workspace.worktree_open` gives `worktrees/orch-<n>/` on branch `orch/<n>` from
   `main`. Work there.
2. Merge the Task's recorded commit into your worktree branch, resolve each conflict keeping the intent of
   both changes, and run the mixin's `check` command from `MIXIN.md` (game-specific checks: see
   the mixin's docs). A change that only silences the check is not a resolution.
3. Commit on your branch with a message naming the Task.
4. `workspace.change_complete {worktree, commit, summary}`: it records a complete maintenance
   Task held by you, and the review wake and the mechanical merge take it from there. The
   original Task branch stays as it was.

## Outcome 2: a maintenance Task

When the fix needs real work (a toolchain pin, a mixin command that broke on `main`, a build
that fails on both branches): `task.create {type: "maintenance", title, description, priority:
"high", goal, depends_on}` with the branch, the commit, the failing command and the log path in
`description`. Write the description so a Researcher can start without you. T-n is complete, so it
takes no dependency: when the fix has merged, `task.review {task: T-n, accept: true, merge: "yes",
reason}` queues its merge again.

## Outcome 3: the user

When the conflict is a decision (two Researchers changed the same strategy in opposite ways;
the merge would undo something the user asked for): `chat_update {category: decision, text,
refs: ["T-n", "T-m"]}` asking plainly between the two outcomes; the merge waits for their answer in the
chat. Ask only when the user alone can decide; most conflicts are outcome 1.

## Always

- The branch is kept whether or not it merges. Record what you did on the Task: `task.message`
  to a Researcher still holding it, or the maintenance Task's description.
- A failed check that is not the branch's fault (the platform is down, a toolchain vanished) is
  a capability failure: `workspace.capabilities {check: true}` reruns the checks next tick and
  the `repair` skill applies.
- End the wake naming the Task and the outcome: a `failure` `chat_update` when it needs the
  user, otherwise `progress`.
