# Agent ↔ UI protocol survey

> **Research brief, kept for history (status 2026-11-12).** Its main recommendation was
> followed: no public protocol was adopted, and the rebuild (#106,
> [ide-profile-refactor-2026-10-28-final.html](ide-profile-refactor-2026-10-28-final.html))
> defined its own client contract in `core/src/research_core/contract/` (`CONTRACT.md`: session
> open with declared capabilities, per-turn context, snapshots with `seq`, UI state changed by
> JSON Patch `ui.patch` events). AG-UI's event names were not kept. Since November 5, 2026,
> frontends reach a workspace through AIDE 1.0.0, Softmax's own JSON-RPC protocol whose contract
> lives in metta; `image/aide.py` is the workspace's backend for it and maps each method to
> calls on that contract's seam and the Hermes gateway (`image/README.md`, "AIDE 1.0.0 backend"). AIDE is not one of the protocols
> surveyed below. For current behaviour see `image/README.md` and `core/README.md`.

2026-10-27 (PDT). Research brief for the research-profile redesign (Orchestrator + Researchers on Hermes Agent v0.21.6, behind the Softmax bridge).

**What we need.** One contract that the agent backend owns, with three channels: (1) **context in**: a session-open message (client identity, declared UI capabilities, user, game/league) plus small per-turn context; (2) **UI control out**: `ui.*` tools that change a declarative UI state, rendered by each client as far as it can; (3) **state sync**: a snapshot, then an event stream of record changes, UI state changes, and chat/run events, sent to several clients at once.

**How to read this.** "Verified" means I read it on the primary source linked next to it (spec, repo, or package index) on 2026-10-27. Anything else is marked as inferred or secondary.

---

## AG-UI (Agent–User Interaction Protocol)

**What it is.** An event protocol between an agent backend and a user-facing app. CopilotKit created it and runs the community; the repo is [ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui). The client POSTs a `RunAgentInput` and receives a stream of typed events, over SSE by default.

**Maturity (verified).** Version 1.0 is the current release. The Python package [`ag-ui-protocol` 1.0.0](https://pypi.org/project/ag-ui-protocol/) shipped on 2026-09-17, and 0.1.x releases came out every couple of weeks before that. A [1.1 working draft](https://docs.ag-ui.com/spec/draft) exists. The repo has about 16.8k stars. It lists first-party integrations for Microsoft Agent Framework, Google ADK, AWS Strands, Pydantic AI, LlamaIndex, Mastra, and Agno, plus bridges to A2A, MCP Apps, and A2UI. ([repo README](https://github.com/ag-ui-protocol/ag-ui))

**Mapping to our channels** ([events](https://docs.ag-ui.com/concepts/events), [types](https://docs.ag-ui.com/sdk/python/core/types)):
- **Context in:** `RunAgentInput` carries `thread_id`, `run_id`, `state`, `messages`, `tools` (tools the client offers), `context` (a list of `{description, value}`), and `forwarded_props`. This covers per-turn context well. There is **no session-open or capability handshake**. The 1.1 draft does not cover capability discovery either.
- **UI control out:** `STATE_SNAPSHOT` sends the whole state, and `STATE_DELTA` sends an RFC 6902 JSON Patch. `ACTIVITY_SNAPSHOT/DELTA` covers structured progress items. `CUSTOM {name, value}` handles anything else. A `ui.*` tool that edits a shared UI-state document maps directly onto snapshot + delta.
- **State sync:** It has rich run and chat events (`RUN_*`, `TEXT_MESSAGE_*`, `TOOL_CALL_*`, `REASONING_*`, `SUBAGENT_*`, `MESSAGES_SNAPSHOT`). The weakness: the model is **one HTTP request per run, streamed back**. It does not define a long-lived subscription, several clients watching one session, or replay from a sequence number (the draft is silent on all three).

**Fit for a Python backend behind a thin relay:** good. The events are plain JSON over SSE and a Python SDK exists. The bridge can pass SSE through unchanged. The per-run streaming model is the main mismatch with fan-out.

## A2UI (Agent-to-UI)

**What it is.** Google's declarative format in which the agent describes a UI as a flat list of components chosen from a catalog the client trusts, plus a data model. The client renders the result with its own native widgets. Licensed Apache-2.0; repo at [github.com/google/A2UI](https://github.com/google/A2UI), site at [a2ui.org](https://a2ui.org/).

**Maturity (verified).** v0.9.1 is current, and v1.0 is a release candidate that adds client-to-server RPC and action IDs ([a2ui.org](https://a2ui.org/)). v0.9 shipped on 2026-04-17 ([Google blog](https://developers.googleblog.com/a2ui-v0-9-generative-ui/)). The README still calls the project an "early stage public preview." It has Lit, Angular, and React web renderers, Flutter through GenUI, a Python [`a2ui-agent-sdk`](https://developers.googleblog.com/a2ui-v0-9-generative-ui/), and about 16.8k stars. A secondary source reports AndroidX Compose libraries at alpha in September 2026 ([ecorpit](https://ecorpit.com/google-a2ui-android-jetpack-compose/), not verified).

**Mapping** ([v0.9.1 spec](https://a2ui.org/specification/v0.9.1-a2ui/)):
- **Context in:** the client sends `a2uiClientCapabilities.supportedCatalogIds` (plus optional inline catalogs) in transport metadata. This is the closest existing model for "the agent knows what the client can render." User input returns as `action {name, surfaceId, sourceComponentId, context}`.
- **UI control out:** `createSurface`, `updateComponents`, `updateDataModel` (a JSON Pointer path upsert), and `deleteSurface`. This is *generative* UI: the agent builds the component tree itself. We want the agent to drive views we already designed, so this is more power than we need, and it costs prompt tokens.
- **State sync:** none beyond the per-surface data model. A2UI expects a transport underneath it (A2A, AG-UI, MCP, WebSocket).

**Fit:** It is a payload format, not a transport, so it would run inside whatever stream we pick. It could become a later content type for a "free-form panel," but it should not be our main contract.

## MCP Apps / MCP-UI, and the current MCP spec

**What it is.** An official MCP extension (`io.modelcontextprotocol/ui`). A tool declares a `ui://` HTML resource. The *host* renders that resource in a sandboxed iframe and talks to it over postMessage JSON-RPC. It grew out of community MCP-UI and OpenAI's Apps SDK. ([SEP-1865](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp), [ext-apps](https://github.com/modelcontextprotocol/ext-apps))

**Maturity (verified).** SEP-1865 is Final. The extension spec dated 2026-01-26 is Stable, and the SDKs are at 2.x (TypeScript only; there is no Python SDK). The README lists Claude, ChatGPT, VS Code, Goose, and Postman as hosts. The core MCP spec is now at [2026-07-28](https://modelcontextprotocol.io/specification/latest/changelog). That revision made MCP stateless (client capabilities sent on every request in `_meta`), replaced server-initiated elicitation with "multi round-trip requests" (`input_required` results), and replaced resource subscriptions with `subscriptions/listen`.

**Mapping:** The view↔host bridge ([2026-01-26 spec](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)) has useful shapes: `HostContext` (theme, display mode, locale, platform, device capabilities), `hostCapabilities`, `ui/update-model-context` (the view pushes context for the next turns), and `ui/notifications/host-context-changed`. Still, the model is "a tool result renders an embedded mini-app inside someone else's chat host." In our setup **we are the host**, and our clients already own their views. Elicitation is a narrow "ask the user for a form value" mechanism, not UI control.

**Fit:** poor as the main contract. It is worth copying the shape of `HostContext` and capabilities for our session-open message. Separately, MCP Apps is the right choice if we ever want our tools to render inside Claude or ChatGPT.

## OpenAI Apps SDK

ChatGPT's app platform. Its [reference](https://developers.openai.com/plugins/reference.md) now says to "start with the open standard" (the MCP Apps bridge). `window.openai` and `openai/*` metadata keys are kept as compatibility aliases and ChatGPT-only extras (verified). For us it is a subset of MCP Apps, so it is not relevant beyond that.

## Others

- **Vercel AI SDK UI message stream** ([spec](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)). Chat-oriented parts sent over SSE (`text-*`, `tool-input-*`, `tool-output-available`, `tool-approval-request`, plus custom `data-*` parts), with the header `x-vercel-ai-ui-message-stream: v1`. Pydantic AI implements it from Python ([docs](https://ai.pydantic.dev/ui/vercel-ai)). It covers the chat channel only: no capabilities, no shared state, no multi-client support. Useful only if a client is built on `useChat`.
- **LangGraph `useStream` / assistant-ui** ([LangGraph SDK](https://reference.langchain.com/javascript/langchain-langgraph-sdk/stream), [assistant-ui](https://www.assistant-ui.com/docs/runtimes/langgraph/overview)). Tied to the LangGraph server and its thread/run model. Not a neutral standard.
- **A2A** ([releases](https://github.com/a2aproject/A2A/releases): v1.0.0 on 2026-03-12, v1.0.1 in May 2026). Agent-to-agent, not agent-to-UI. It is mainly relevant as a carrier for A2UI. It might matter later for Orchestrator↔Researcher traffic, but it does nothing for the UI.
- **ACP (Agent Client Protocol, Zed)** ([intro](https://agentclientprotocol.com/overview/introduction)). JSON-RPC between an editor and an agent, over stdio, with remote support "in progress." **Hermes already ships an ACP server** (`hermes acp`), and also has a TUI-gateway JSON-RPC over stdio/WebSocket and an OpenAI-compatible API server with SSE run events ([Hermes integration docs](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration)). ACP is built for a coding agent inside an editor: one client per session, session updates and permission prompts, and no declarative UI state. It is a good reference for session lifecycle, but not our contract.

---

## Comparison

| | Context in | UI control out | State sync / fan-out | Capability negotiation | Python backend | Maturity |
|---|---|---|---|---|---|---|
| **AG-UI** | Per-run `context`, `state`, `forwarded_props`; no session open | `STATE_SNAPSHOT` + JSON Patch `STATE_DELTA`, `ACTIVITY_*`, `CUSTOM` | Rich run/chat events; one stream per run; no multi-client, no replay | None | Yes (`ag-ui-protocol` 1.0.0) | 1.0, wide adoption |
| **A2UI** | `action` events + data model | Agent-built component trees | Not covered (needs a transport) | `supportedCatalogIds` | Yes (`a2ui-agent-sdk`) | 0.9.1; 1.0 RC; "preview" |
| **MCP Apps** | `ui/update-model-context` | Tool result → iframe HTML | Not covered | `HostContext`, `hostCapabilities` | TS SDKs only | Stable (2026-01-26) |
| **Vercel UI stream** | Chat messages | `data-*` parts only | Chat only | None | Via Pydantic AI | Stable, chat-scoped |
| **ACP** | `session/new`, prompts | None | Session updates, single client | Client capability flags | Yes (Hermes ships it) | Active; remote WIP |

## Recommendation: design our own contract, and borrow AG-UI's event shapes for the stream

Do not adopt any one protocol wholesale. None of them covers channel 3 as we need it (a long-lived snapshot + subscription shared by several clients), and none has a real capability handshake. AG-UI is the best place to borrow from. Its run, chat, tool, and reasoning events, and its `STATE_SNAPSHOT` + JSON Patch `STATE_DELTA`, match how our backend already works. It is at 1.0 with a Python SDK, and if we keep the event names and fields as they are, AG-UI tooling (the CopilotKit client, inspectors, the A2UI and MCP Apps bridges) stays within reach.

What to build:
1. **Use AG-UI event types as they are** for chat/run events and UI state (`STATE_SNAPSHOT`/`STATE_DELTA` over one JSON document that the plugin's `ui.*` tools change). Use `CUSTOM` (or a prefix such as `softmax.record.*`) for record changes.
2. **Take the capability declaration from A2UI and MCP Apps**: the session-open message carries `client {id, kind, version}`, `capabilities` (the list of view and panel types the client renders, similar to `supportedCatalogIds`), host-context-style fields, user, and game/league. Return the accepted capability set so the agent can see it as tool context.
3. **Keep A2UI as a possible later panel content type**, not the base contract.

Gaps we still have to fill ourselves:
- **Session open and capability negotiation.** AG-UI has none. We must define the message and decide what `ui.*` does when the client cannot render a view (no-op plus a result the agent can read, I suggest).
- **Multiple clients, one session.** We need per-session sequence numbers on every event, snapshot-then-tail with a "resume from seq N" option, and a rule for whose capabilities count when two clients with different abilities are attached. AG-UI's one-stream-per-run model leaves all of this to us.
- **Record-change schema** (tasks, experiments): the entity types, IDs, and snapshot vs. delta semantics.
- **Where the stream is served from.** Hermes plugins can't register HTTP routes. Something has to turn plugin events into the stream: a sidecar process the plugin talks to, or Hermes's existing TUI-gateway WebSocket / API-server SSE. That affects whether the bridge stays a "thin relay" and how local development works without the bridge. This is the biggest open design question, and it is a Hermes question, not a protocol question. (Inferred from the integration docs; I have not checked whether a plugin can emit custom events onto those gateways.)
- **Auth and identity** come from the bridge. The contract should carry the user and client IDs that the bridge asserts, not values the client claims for itself.
