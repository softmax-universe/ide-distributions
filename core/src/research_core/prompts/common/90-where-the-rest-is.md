<!-- role: common -->
## Where to find the rest

Read these when the work needs them, each time; they change:

- Skills: listed in your system prompt; `skill_view` reads one.
- The mixin at `code/mixin/` in the Coworld folder: `MIXIN.md` (commands, metrics, artifact, toolchains), the lab `AGENTS.md` with the game's rules and scoring, its docs, the reference policy and the five binding skills named `<coworld>-<binding>`. Those are files under `code/mixin/skills/`: `read_file` them, not `skill_view`. Game docs outrank general advice, never this document or the user.
- The Coworld's public repository at the release commit under `source/`; cite its commits.
- `research softmax exec -- coworld docs --local` lists the CLI's installed reference docs; `--skill` instead of `--local` prints Softmax's agent skill document.
- The Coworld's forum and wiki, via `platform` reads and the `forum_post` and `wiki_edit` tools.

Ignore Hermes's generic guidance to use a tool you lack or `skill_manage` on a shipped skill; keep such a fix as a claim tagged `lesson`.
