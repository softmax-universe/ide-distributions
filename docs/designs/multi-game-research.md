# Multi-game research and game creation

> **Retired (2026-11-12).** This design was implemented on branch `multi-game-research`
> (2026-10-23) in the previous profiles, and all of that code was removed on October 29, 2026
> by the profile rebuild (#106): `ide_research_core/games/scope.py`, `sources.py`,
> `games/_template/`, the `game-create` skill, `game_install` and the `RESEARCH_GAME` variables
> no longer exist. The rebuild
> ([ide-profile-refactor-2026-10-28-final.html](ide-profile-refactor-2026-10-28-final.html))
> meets the same need differently: one workspace folder per Coworld
> (`core/src/research_core/layout.py`), and the `workspace` tool's `coworld_switch` action
> (`core/src/research_core/tools/workspace.py:39`), which fetches the Coworld's source at the
> pinned commit and imports its mixin with prefixed skills
> (`core/src/research_core/coworld_setup.py`). There is no runtime game-folder scaffold; a
> Coworld without a mixin gets the `mixin-from-scratch` skill instead. Nothing below describes
> the current code; see `core/README.md` and `README.md` for that. Kept for history.

Original status: implemented on branch `multi-game-research` (2026-10-23).

## Problem

The research profiles were built around one game per host. Hosting previously wrote one
`RESEARCH_GAME` (`gota`), one canonical repository (`canonical-gota.git`) and one clone per
profile (`profiles/<name>/workspace`), and every research command reads those three variables.
A user who asks the orchestrator about another coworld gets platform reads (`discover_leagues`,
`game_support`), but nothing can research it:

- there is no game folder (`games/<game>/GAME.md`, bindings, seed) for it, and no way to make
  one at runtime: in hosted sandboxes `games/` is part of the read-only image (metta
  `observatory_hosted_worker/layout.py`, `IMAGE_PATHS`);
- no profile can fetch a game's source or a mixin from GitHub, although the game contract names
  a source repository and claims cite source lines;
- every card runs in the one game's clone, so even a game folder would have nowhere to run.

## Goals

1. A researcher can fetch a public GitHub repository (game source, mixin) into a shared,
   read-only cache, by commit.
2. The orchestrator can have a new game folder made for a game the user asks about, and
   install it host-wide once it passes the contract checks.
3. Making a game folder follows optimizer-seed's mixin method (`tools/add_game.sh` and
   `games/_template/`): install an existing mixin when one is public, otherwise scaffold from
   the template and fill it from the game's source; missing bindings are reported gaps, never
   silent.
4. One host researches several games at once: each card names its game; each game has its own
   canonical repository; each profile has a clone per game.
5. No instruction tells the orchestrator or researchers to work only on GotA.

## Non-goals

- Play has no default league. Its bundled build tools support explicitly selected GotA BASIC programs.
- Private repositories. Sandboxes have no GitHub credential; a private repository fails with a
  plain reason.
- Building container (Docker image) players on every host. Superseded in part: since #77,
  `research game build-container` builds an image with Coworld's BuildKit command and
  `upload_build` publishes it as an archive (`artifact_type: oci-image`), on hosts that
  provide BuildKit (the opted-in Kubernetes workspaces, metta #29703). EC2 workspaces have no
  BuildKit or Docker daemon, so a container build there still fails with a clear reason.

## Design

### Where things live on a host

`<root>` is the Hermes root: `HERMES_HOME/../..` for an installed profile
(`<root>/profiles/<name>`), else `HERMES_HOME`. In hosted sandboxes it is `/opt/data`, one
durable volume.

| Path | What |
|---|---|
| `<profile>/games/<game>/` | Games shipped with the distribution (image, read-only in hosting). Win over installed games of the same name. |
| `<root>/games/<game>/` | Games installed at runtime by `game_install`. Also a Hermes `skills.external_dirs` entry for researchers, so a game's binding skills load as real skills. |
| `<root>/game-history/<game>/<stamp>/` | The previous copy of a replaced installed game. Outside `games/` so its skills never load. |
| `<root>/sources/github.com/<owner>/<repo>.git` | Blobless bare cache of a fetched repository (all fetched commits; `kb renew --game-repo` reads it). |
| `<root>/sources/github.com/<owner>/<repo>@<sha12>/` | Read-only snapshot of one fetched commit, for reading. |
| `dirname(RESEARCH_CANONICAL)/canonical-<game>.git` | One canonical repository per game (the existing naming rule). |
| `<profile>/workspace` (`RESEARCH_WORKSPACE`) | The profile's neutral clone, with no `RESEARCH_GAME` assignment. |
| `<profile>/workspace-<game>` | The profile's clone of an explicitly selected game, made on first use. |

### Selecting a game: one seam

`ide_research_core/games/scope.py` owns every game-to-path rule:

- `env_for_game(env, game)` returns the profile environment with `RESEARCH_GAME`,
  `RESEARCH_CANONICAL` and `RESEARCH_WORKSPACE` pointed at that game. Downstream code keeps
  reading the same three variables, so no research command changes.
- `env_for_worktree(env, worktree)` does the same from a card worktree: the worktree's clone
  (`git rev-parse --git-common-dir`), that clone's `origin` (`canonical-<game>.git`) and the
  game in its name. The CLI entry point applies it whenever `HERMES_KANBAN_WORKSPACE` is set, so
  a researcher working a card of any game runs every command, binding and resolver against
  that game without knowing it.
- `locate_game(name, env)` finds a game folder: shipped first, then installed. `Game.root` is
  the directory bindings and resolvers run from (the one holding `games/`), replacing the
  `_REPO_ROOT` constants.

Profiles omit `RESEARCH_GAME`. Hosting bootstraps a neutral repository without game seeds.
A card or tool that names no game stays neutral; selected tasks set `RESEARCH_GAME` only
within their operation. Frontend hints and the user's conversation select the subject.

### Cards carry their game

`card_create` takes an optional `game`. It checks the game is shipped or installed, prepares the
assignee's clone for it by running the assignee's own CLI (`<profile>/bin/research workspace
bootstrap --game G`, so the assignee's doctrine seeds a new canonical repository), registers
that clone as the assignee's Hermes project for the game, and anchors the card branch there.
Hermes makes the card worktree from that project, so the worker lands in the right game.

The concrete game, the default included, is recorded in the card's immutable
`creation_command` event, so changing the default later never moves a card. A command recorded
before cards named their game keeps its original workspace; it does not assign a game. Every card-scoped orchestrator effect
(`upload_build`, `workspace_integrate`, `submit_requested_version`, `champion_submit`,
`bootstrap_champion`, `game_install`) reads it from there, and so does the minute supervisor for
XP, credit and permit waits. Canonical-scoped tools (`status`, `records_find`, `records_show`,
`plan_write`, `control_write`, `accept_champion`, `xp_status`) take an optional `game`. The
episode budget stays host-wide: one allowance for all games.

### Fetching source

`research source fetch --repo https://github.com/<owner>/<repo> [--ref <ref>]` accepts only
public `https://github.com/` repository URLs and plain ref names. It runs git with no credential
helper and no terminal prompt, fetches the ref into the blobless bare cache under a per-repository
lock, and writes an immutable snapshot of the commit (`git archive`, extracted to a temporary
directory and renamed into place). It prints the commit, the snapshot path and the cache's git
directory. Nothing fetched is executed by the command.

### Making a game folder (adapted from optimizer-seed)

optimizer-seed's mixin contract is `games/_template/` (`MIXIN.md`, a lab `AGENTS.md`, five
required binding skills: `ab`, `survey`, `replay-inspection`, `eval-design`, `diagnosis`) and
`tools/add_game.sh` (clone a mixin, stamp provenance, vendor it, list what to review). This
repository's contract is `GAME.md` (front matter plus guide) with five command bindings and a
seed. The adaptation:

- `researcher/games/_template/` becomes a full scaffold: `GAME.md` (quoted `{{...}}`
  placeholders; body sections from optimizer-seed's lab `AGENTS.md` plus this contract's
  required `## Seats and opponents` and `## Starter policy`), `skills/<binding>/SKILL.md` for the
  five binding skills (named `<game>-<binding>`), stub `tools/` for the five command bindings,
  `seed/` and `PROVENANCE.md`. The field reference that was the template's body moves to
  `games/_template/CONTRACT.md`, which is not copied.
- `research game new --name G --league L [--source URL] [--mixin URL]` copies the scaffold into
  `<worktree>/games/G/`, fills the placeholders, and with `--mixin` vendors the mixin
  (`games/G/mixin/`, minus its skills) and installs its binding and extra skills under
  `games/G/skills/`, renamed `G-<skill>`, with provenance stamped as `add_game.sh` does.
- `research game check --name G [--root DIR]` is the install gate: the contract loads, every
  binding script exists, the guide has its required sections, the seed has `plan.md` and
  `strategy/STRATEGY.md`, skill names are `G-*` and unique, and no `SKILL.md` sits outside
  `skills/`. It reports *gaps* separately: bindings and skills still holding the template stub,
  and host requirements missing on this host. Gaps do not block installation (optimizer-seed:
  "announce it, proceed, warn the human").
- `research softmax league-results --league L --limit N --dest DIR` saves completed episodes’
  raw results without running any game binding, so a new game's `evaluate` binding can be
  written against real files.
- The `game-create` researcher skill is the procedure: resolve the league and Coworld, fetch the
  source, find a mixin, scaffold, fill the contract from verified source facts (never from
  memory), implement what the host can run (always `evaluate`), run `game check`, stage, and
  report gaps.
- `game_install(id, run_id, name, replace=false)` is the orchestrator tool. It admits only the
  latest completed run of the card, requires `games/<name>/` committed in the card's worktree,
  copies the committed tree (`git archive`) into `<root>/games/<name>/` atomically, writes
  `INSTALLED.json` (card, run, commit, time; a reserved name in the folder), and returns the
  check report. A receipt per card run answers a retry, so a late retry never undoes a newer
  installation; a replaced folder moves to `game-history/` and comes back if the swap fails.
  A mixin holding a symbolic link is refused before anything is copied. It refuses a shipped
  name and refuses to overwrite an installed game unless `replace` is true.

## Alternatives considered

- **Per-card environment from Hermes.** Hermes gives a worker its profile `.env`; it has no
  per-task environment. Deriving the game from the worktree needs no Hermes change.
- **Game folders in the canonical repository.** Each canonical repository is one game's research
  record, and a game must exist before its repository can be seeded. A host directory keeps
  the folder beside, not inside, the research it enables.
- **Game skills as plain documents.** Simpler, but loses Hermes skill discovery and card
  preloads (`worker_skills`). `skills.external_dirs` (Hermes 0.21.6) loads them as skills.
- **A Hermes skill per binding in the profile `skills/`.** That directory is Hermes-managed and
  per profile; every researcher would need its own copy and names would collide across games.

## Risks

- **Executing fetched content.** Bindings and mixin tools run on the host. Fetching never
  executes anything; installation is a separate orchestrator decision; the check report shows
  every binding command and file the orchestrator is about to install.
- **Prompt injection from fetched repositories.** A mixin's docs become skills a researcher
  reads. The orchestrator installs only from a reviewed card, and skills cannot change a
  researcher's deny list.
- **Existing hosted sandboxes keep their `config.yaml`** on update, so the new
  `skills.external_dirs` entry reaches them only by `hermes config set` or `--force-config`
  (see [hosting](../hosting.md)).
- **Hosting's doctrine merge** touches only `profiles/<name>/workspace`; other game clones get
  doctrine updates when the CLI bootstraps them, which `card_create` does for every card of
  that game.

## Validation

- `researcher/tests/unit/games/test_scope.py`: the path rules, worker detection, the CLI
  inside a worker, game lookup and `workspace bootstrap --game`.
- `researcher/tests/unit/test_sources.py`: URL and ref validation, and fetches from a local
  repository standing in for GitHub (snapshots, the shared cache, diffs between commits).
- `researcher/tests/unit/games/test_authoring.py`: the scaffold, a mixin import, every check
  problem and gap, and installation (once, replace, refusals, researchers refused).
- `orchestrator/tests/test_research.py` and `test_gate.py`: `in_game`, and the supervisor's
  per-card game and per-game promotion poll.
- `orchestrator/tests/lifecycle_scenarios.py` (`-m hermes`, real Hermes Kanban): a card in
  another game gets its worktree from the assignee's clone of that game, replays are checked,
  and integration runs in that game's workspace.
