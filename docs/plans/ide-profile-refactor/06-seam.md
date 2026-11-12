# Entry 6 — the client seam: context block, notices stream, `research serve`, scripted client, acceptance scenario

> **Implemented.** Status as of November 12, 2026: built and merged in PR #106 on October 29,
> 2026 (Pacific). The checkboxes were not kept up to date; the plan is historical. Since then
> clients reach the seam only through the AIDE 1.0.0 backend; the pre-AIDE workspace chat
> protocol was removed (#236). The seam's contract is
> `core/src/research_core/contract/CONTRACT.md`; see also [STATUS.md](STATUS.md) and
> `image/README.md`.
> **2026-11-12:** the `ui` tools described here were removed; `ui.tell` became `chat_update` (branch `jb/remove-ui-tools`).

Read `00-overview.md`, then entries 2–5. The seam is the small part of the client contract the
rest of the design needs: each turn carries the user's message and one typed context block; the
bridge (or `research serve` locally) stamps the sending client; notices reach clients as `events`
rows streamed from the store. The full contract layer (`session-open` capabilities, panels, the
agent-context presentation, multi-client sync) ships later in its own stack and is **not** built
here beyond the schema files and the `ui` tool's store writes already in place.

Spec: §10 ("The seam"), A.2 (contract files, `research context-schema`), A.4 (turn stamps),
A.11 (event stream, snapshot + seq), H (notices and continuity, local end-to-end harness,
acceptance scenario), R3 (`research serve` runs the profiles without metta).

**Interfaces produced:**

```python
# research_core/contract/__init__.py
CONTRACT_VERSION = 1
def schema(name: Literal["context","events","ui-state"]) -> dict     # loads the JSON file
def validate_context(block: dict) -> tuple[dict, list[str]]           # (kept fields, problems); used by plugin/turn.py (entry 3) and serve.py
# research_core/serve.py
class SeamServer:   # stdlib http.server + threads; no async
    def __init__(self, *, ws: Workspace, hermes: HermesRuns, profile: str = "orchestrator", host="127.0.0.1", port=0)
    # routes:
    #   POST /session-open {client:{id,kind}, session_context?, capabilities?} → {session_id} (registers sessions row kind chat, stores session_context + capabilities)
    #   POST /message {session_id, message, context?} → validates context, stamps {client, coworld, contract_version}, builds input = message + "\n\nContext v1:\n<json>", calls hermes.start(profile, session_id, input) → {run_id, seq}
    #   POST /ui-action {session_id, action, payload} → for action "answer": ui_state.patch(/asks/<id>/answer), then hermes.start or steer with "[UI action] …" → {accepted}
    #   POST /store/apply {actor, op, args} → store apply (user edits) → {record, seq}
    #   GET  /snapshot?store=workspace|coworld → records + ui_state + seq
    #   GET  /stream?store=…&after=<seq> → SSE; tails events every 0.5 s; id: <seq>; keepalive comments
    #   GET  /runs/<run_id> → hermes.status passthrough
    def serve_forever(self); def shutdown(self)
def main(argv) -> int     # `research serve [--port N] [--profile orchestrator]`
# research_core/client.py — the scripted client for tests and demos
class ScriptedClient:
    def __init__(self, base_url); def open(self, client_id="scripted", kind="test") -> str
    def say(self, text, context: dict | None = None) -> dict; def wait_run(self, run_id, timeout=120) -> RunStatus
    def events(self, store, after=0) -> list[Event]; def notices(self) -> list[dict]; def answer(self, ask_id, value) -> dict
```

---

### Task 6.1: Contract files and `research context-schema`

**Files:** `contract/context.schema.json` (finish from entry 3), `contract/events.schema.json`, `contract/ui-state.schema.json`, `contract/CONTRACT.md` (short human spec: the three channels, the seam fields, limits, versioning rule: `contract_version` increments on any breaking change; clients ignore unknown kinds), `contract/__init__.py`, `cli.py` (`context-schema [--name]` prints the JSON), `core/tests/contract/test_contract.py`.

- [ ] Tests: schema files are valid JSON with `$id` and `contract_version`; `validate_context` drops unknown fields, refuses oversize (`client_data` > 8 KB, total > 16 KB, `mode.instruction` > 1000, `on_screen` > 20) with messages naming the limit; `research context-schema` prints the context schema.
- [ ] Commit: `Publish the seam contract files`.

### Task 6.2: `research serve`

**Files:** `serve.py`, `core/tests/test_serve.py` (against `FakeHermesRuns`).

- [ ] Tests: session-open registers a chat Session with context; message stamps client+coworld and the Hermes input carries the labeled block; `/stream?after=` delivers new events as SSE with ids and resumes from a seq; `/snapshot` seq equals the last streamed id; ui-action answer patches the ask and starts/steers a run; `/store/apply` with a user actor approves a proposed Task; a malformed context block yields 200 with `problems` in the response and the run still starts.
- [ ] Commit: `Add research serve, the local seam server`.

### Task 6.3: Scripted client and the local end-to-end harness (fake Hermes)

**Files:** `client.py`, `core/tests/scenarios/test_seam_end_to_end.py`, `tools/e2e.py` (a runnable demo: starts `research serve` with the fake platform and a *scripted agent* — `FakeHermesRuns` with a scenario script — and prints the event stream).

- [ ] The scripted agent for the acceptance scenario: on input containing "submit the starter as my champion", it calls (through `run_tool` with the chat Session's actor): `workspace.coworld_switch` if no active Coworld (fakegame, with the fixture source), waits for setup (the watcher tick runs inline in the test between runs), `version.create` on `main` (the starter), `upload`, `submit(user_requested=true, cites=<message>)`, `ui.notify("Submitted …")`, and ends. `test_acceptance_submit_starter`: open a session, say the sentence, run ticks, assert the fake platform shows a submission of the starter's policy version, a notice exists, `version.read` shows `competing`, and the chat Session's `events` include `version.submitted` with `cites`.
- [ ] Also: `test_notice_opens_next_turn` (a wake's `ui.notify` appears in the next `/message` input through the plugin's turn hook — verify by inspecting the fake run's input), `test_reaction_never_starts_run` (`/ui-action` with `action=reaction` writes an event and starts no run).
- [ ] Commit: `Add the scripted client and the acceptance scenario`.

### Task 6.4: Real-Hermes end-to-end (optional gate)

**Files:** `core/tests/hermes/test_seam_real_hermes.py` (skipped unless `HERMES_BIN` and `OPENROUTER_API_KEY`).

- [ ] Start a real gateway (as in S1), `research serve` against it with the fake platform, send "submit the starter as my champion" and assert the fake platform received a submission within 10 minutes; record the transcript path in the test output. This is the local stand-in for the release canary.
- [ ] Commit: `Add the real-Hermes acceptance run`.
