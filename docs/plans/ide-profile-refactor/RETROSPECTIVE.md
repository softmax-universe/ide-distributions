# How the rebuild went

> **2026-11-12:** the `ui` tools described here were removed; `ui.tell` became `chat_update` (branch `jb/remove-ui-tools`).

Written October 29, 2026 (Pacific), after the implementation of
`docs/designs/ide-profile-refactor-2026-10-28-final.html` landed on `jb/idd-profile-refactor`.
`STATUS.md` says what exists and what is open; this page says how it went: what was decided,
what was hard, what was smooth, and what to watch.

## Shape of the work

One agent read the design in full, wrote the plan (`00-overview.md` to `07-skills-soul-setup.md`)
and ran a three-lens adversarial review of it before building; the 72 findings became the
binding amendments in `08-amendments.md`. The build then ran as parallel subagents on disjoint
files inside one checkout, in roughly the plan's order: package and shells, store, tools and
plugin, platform, watcher (three waves), seam, skills and prompts, preparation, checks and docs.
About 1,450 tests were written alongside the code. The last phase was validation on a real
Hermes gateway with a real model, which found four defects that no unit test could have.

## Decisions worth knowing

- **Two Python environments.** Hermes's own interpreter runs the plugin, so the base of
  `research_core` is stdlib only; the official `coworld` and `softmax-cli` clients live in one
  workspace environment (`$IDE_WORKSPACE/tools/venv`) that `prepare.py` builds from the root
  lock. Tools that need the clients run there as subprocesses. This avoids installing anything
  into Hermes's interpreter and keeps the lock the single source of client versions.
- **Records in SQLite, one store per Coworld plus a workspace store.** Integer ids with
  prefixes, an append-only events table as the bridge's feed, token-marked transactions so an
  unmarked write is detectable, and intents written before every platform call that money or
  identity depends on.
- **The watcher has no model.** Twelve steps, an event-ownership map that a test forces to be
  total (every event kind is either handled, a wake, or explicitly unwatched), idempotency keys
  that carry the attempt number, and wakes that are Sessions of the Orchestrator profile
  rather than chat turns.
- **Few gates, and only on the agents' own initiative.** Approvals off, no deny list, the
  advisory hook never blocks; the user's explicit request is carried out even before setup
  exists (`pending_request` on the Coworld registry row). Protection is teaching plus
  visibility: notices, events, the quality bar for autonomous submissions, unmarked-write
  detection.
- **Preparation is idempotent and registry-aware.** `prepare.py` re-applies protected keys at
  every run (including, after the real runs, turning off Hermes's post-turn skill-patching
  review), keeps the owner's values elsewhere, points every profile at the active Coworld, and
  reports in `--check` mode without writing.
- **The seam is a local server first.** `research serve` is the reference bridge: one live run
  per Session, steer when live, start otherwise, events as Server-Sent Events, the user-edit
  allow-list. The scripted client and a scripted agent make the acceptance scenario a plain
  test on the fake platform.
- **Freshness over tolerance.** No compatibility shims for the old profiles; retired config
  keys are removed at preparation; old docs carry banners instead of being rewritten.

## What was smooth

- The store and tool layers, once the base was in, were cheap to extend: a new record was a
  migration, a module, a registry entry and a tool action, and the fixtures made each tool's
  tests a few lines.
- Parallel subagents on disjoint files worked, with two tricks: pre-registering every planned
  module in the CLI and the tool catalog with tolerant imports (so a half-written module could
  not break another agent's tests), and a status file with the ownership map.
- The fake platform with fault injection carried the whole scenario layer, including the
  acceptance run on a real gateway, without a network.
- The generated SOULs with a word budget and a leak test caught drift early: every tool named
  in a prompt must exist for that role.

## What was hard

- **Hermes facts that are not in the obvious places.** The gateway enables its API server only
  from the home's `.env`, not from the process environment; the multiplexing gateway keeps each
  profile's `.env` out of `os.environ` and exposes it through `agent.secret_scope`; the post-turn
  "self-improvement" review patches skills in `skills.external_dirs`; the API server needs the
  `messaging` extra. Each of these cost a real-model run to learn, and each is now either
  handled in code or written down in `docs/hosting.md`, `core/README.md` and
  `docs/local-end-to-end.md`.
- **The model's tool-call habits.** The chosen model called `ui.notify` with every field but
  `text` eleven times in one wake. Validation errors now echo the fields received and the
  descriptions name the field, which cut the retries to four. A model-gated tool-call format
  test exists for the next time the model changes.
- **The API rate limit.** A five-hour usage ceiling stopped seven subagents mid-flight once; the
  checkpoint file and tolerant module registration made the resumption clean, but it cost about
  an hour.
- **Concurrency inside one checkout.** A syntax error in an in-progress tool module briefly broke
  the catalog for everyone; the fix (load errors recorded, loading continues) is the right
  behaviour for production too.
- **Design seams that only showed under load.** `require_holder` gives the watcher no sanctioned
  way to write Task-scoped Experiment runs; `experiments.methods.candidate` cannot be set after
  start; a merged submission candidate had no wake until `submission.candidate` was added.
  These are listed in `STATUS.md`.

## What to watch after merge

- The real hosting differences: gVisor and WAL reads from a second process (S3), per-Session
  turn serialisation in the gateway, `sleep_through_jobs`, the read-only core checkout.
- The first real Coworld's mixin: the fixture is small; a real repository's `MIXIN.md`,
  toolchains and lesson notes will stretch `coworld_setup` and the setup wake.
- Prompt and skill quality against the production model: the structural checks pass, the
  model-run contradiction check runs only with a key in CI.

## Codebase friction: 2/5

Self-inflicted rather than inherited, since the repository was rebuilt from scratch. The store,
fake platform and runs-client seams made every later test cheap. The friction was outside the
codebase: undocumented Hermes behaviours that only a live gateway reveals.
