# What the SDK's character.ts mirror needs (note only; nothing here changes SDK code)

`packages/softmax-cogent/src/character.ts` (softmax-universe/softmax) mirrors the profile SOUL. Here the SOUL.md files are unchanged and the learning text is an optional section: `core/src/research_core/prompts/optional/brain-{orchestrator,researcher}.md`, rendered by `build.render(role, brain=True)` and written by `prepare.py` only when `IDE_BRAIN=1`. When the brain is switched on for a site, the SDK side needs:

1. An optional constant (e.g. `LEARNING_SECTION`) with the orchestrator section text, appended only when a site option (e.g. `brain: true`) is set. Default off, so existing sites render identically. Keep it a separate constant so open `character.ts`/`preprompt.ts` changes (#31073, #30866) merge without conflict.
2. Brain content must not travel in `instructions` (trusted). Files read from the brain are data: pass pointers through `context.client_data` (8 KB cap, spec 0152), so a poisoned note cannot become an instruction.
3. The "notes, not orders" and quarantine paragraphs are load-bearing for that property; keep them verbatim.
4. Path names (`brain/`, `playbooks/`, `skills/`, `research/`, `<coworld>/skills/seed/`, `<coworld>/documents/howto.md`, `python -m research_core.brain tidy`) must match the template in `core/src/research_core/brain/template/`, which is the source of truth.
