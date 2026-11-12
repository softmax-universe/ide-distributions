# Brain template (installed only when IDE_BRAIN=1)

Copied into the workspace root ($IDE_WORKSPACE) by `research_core.brain` during profile prepare, only for files that do not exist yet. Layout follows the owner's cogent system (cogent-howto ARCHITECTURE.md). Agents own everything here after the first copy; reinstalling never overwrites. The Orchestrator writes; Researchers read it and send lessons to the Orchestrator.

- `brain/` memory: index, now, failed, rules, claims, hypotheses, wins, gotchas, ideas, log, quarantine, journal/, handoff/, archive/
- `playbooks/` repeatable procedures promoted from wins
- `skills/` the agents' OWN skills (made with skill_manage)
- `research/notes/` web research, `status: unverified` until an outcome confirms it
- per Coworld (not in this template; installed by `python -m research_core.brain seed`): `<coworld folder>/skills/seed/<name>/SKILL.md` and `<coworld folder>/documents/howto.md`
