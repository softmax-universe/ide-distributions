---
title: Execution authority
description: Keep delegated conversations bound to their authenticated caller and exact product grant through execution and recovery.
---

# Execution authority

The gateway owns product authorization. This runtime preserves that decision through its seam, durable admissions, and native Hermes execution.

## Rules

- Accept external execution authority only from the authenticated AIDE delivery envelope. Treat browser context, client names, instructions, and model arguments as untrusted content.
- Deploy the product gateway's explicit owner-authority envelope before activating this runtime. Missing authority on an external request is refused, including for owner traffic.
- Announce restricted execution support on the authenticated backend handshake. The gateway must bind support to the current backend connection before delivering restricted work.
- Bind each conversation immutably to its issuer, subject, subject kind, OAuth client, player, and grant. Store each admission's expiring authorization separately. Reconnection cannot revive an expired admission.
- Check the original authorization through the fixed product endpoint before accepting or dispatching restricted work and before releasing restricted responses. A missing grant, failed check, or expired authorization denies access.
- Keep restricted conversations in the dedicated model-only Hermes profile. Give it no owner memory, skills, history, native tools, distribution tools, or product credential fallback.
- Enforce restrictions at actual native inference and tool execution boundaries. A hidden tool list or an exception in a middleware callback is insufficient.
- Preserve authority through retries and restarts. Refuse steering, background work, or owner operations that would replace a restricted admission's authority.
- Derive runtime identity tokens from the product's existing workspace binding. Keep token acquisition server-side and unavailable to restricted conversations and model tools.
- Preserve existing local player IDs and stored owner sessions. Runtime versions and workspace generations do not define canonical Identity subjects.

## Checks

- `core/tests/scenarios/test_session_coworld_isolation.py` exercises immutable authority, cross-client denial, queued work, and revocation across restart through the seam and real store.
- `core/tests/test_aide_backend.py` exercises trusted delivery, conversation reads, subscriptions, and reconnection through the production AIDE backend.
- `core/tests/hermes/test_restricted_execution_real.py` exercises the shipped restricted profile against pinned Hermes, including native tools, revoked grants, inherited credentials, and context isolation when its plugin is missing.
- `core/tests/system/test_identity_credentials.py` exercises the composed platform client's scoped token request and denies restricted use before credentials, cached clients, or outbound requests.
- Review new tools, profile hooks, and credential consumers for a path that bypasses the stored execution authority. Run `uv run ruff check .` and the affected pytest suites.
