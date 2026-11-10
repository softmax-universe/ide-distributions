---
name: coworld-source
description: Use when reading the Coworld's source, docs or release.
---

# The Coworld's source

A Coworld's public code repository holds all its game-specific material: the game, its docs, and the mixin the tools use. Setup cloned it at the release-pinned commit into `source/` in the Coworld folder and copied the mixin to `code/mixin/`. Read `source/` when the rules, the engine or a mechanic matter; read `code/mixin/` for the commands, the lab `AGENTS.md`, the reference policy and the binding skills.

## Which release

`platform.coworld {coworld}` is the ground truth: id, name, version, `player_runtime`, source URL and commit. When `code/mixin/MIXIN.md` and the platform disagree (a version, a runtime), the platform's manifest wins and the mixin needs an update (a maintenance Task). `git -C <Coworld folder>/source rev-parse HEAD` must equal the manifest's commit; if not, `source/` is out of date: fetch and check out that commit there, and open a maintenance Task if the mixin needs to follow.

## Where to start

1. `COWORLD.md` at the root of `source/` is the entry guide when present; follow only the docs it links that the task needs. Without one, the README and a listing of the source folders.
2. The mixin's place differs between Coworlds; `code/mixin/` is the copy the tools read. Its skills are files, `code/mixin/skills/<coworld>-<binding>/SKILL.md` (`ab`, `survey`, `replay-inspection`, `eval-design`, `diagnosis`), read with `read_file`, not `skill_view`; a missing or stubbed binding is a gap to report, and the core method gives a weaker result.
3. `research softmax exec -- coworld docs --local` lists the reference docs installed with the CLI (the cookbook, the ladder, the replay format); `--local <path>` prints one; `coworld docs --skill` prints Softmax's own agent skill document. Read the participation guide the League names (`platform.league` gives its URL).
4. The League's forum and wiki (`platform.forum_search`, `platform.wiki_read`) hold what other Players wrote.

## Citing

- Lines in the Coworld's repository: `doc` evidence `https://github.com/<owner>/<repo>/blob/<full commit>/<path>#L<a>-L<b>`, the commit from the manifest, so the claim is tied to the release and not to a floating branch.
- Lines in our own code repository (`code/` and worktrees): `code` evidence `<path>:<a>-<b>@<sha>`; the inspector reports whether they changed since.
- A documentation page: `doc` evidence `<url> (retrieved <date>): <quote>`.
- Record the League, the Coworld id and version, the source URL and commit in any claim about the game's behaviour; a claim about the engine without its commit rots silently.

## Rules

- Never edit `source/`'s files: it is a reference, moved only by the checkout above. Candidate edits go in your worktree; mixin changes are commits in `code/mixin/` on a branch and go live on merge.
- If the published source URL floats or is absent, that is a provenance gap: say so in the claim. A review of the latest source is still useful but does not establish the released behaviour.
- Never substitute another game's source or bindings, even a similar name or a newer release of the same name; the League's game names its exact Coworld id.
- Other repositories: `github.clone {repository, commit}` (Orchestrator; the user's granted repositories, into `documents/repos/`); public ones with plain `git clone` under `documents/repos/`.
- Greetings, navigation and clarifying an objective need no source scan.
