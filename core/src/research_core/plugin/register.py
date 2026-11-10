"""Build the ``register(ctx)`` function a profile's plugin shim exports.

Hermes calls ``register(ctx)`` once per process with a ``PluginContext``. Registration must be
quick (Hermes abandons a plugin after ten seconds) and must not import anything outside the
packaged dependencies, because it runs inside Hermes's own interpreter.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from research_core.tools.catalog import Role, tools_for

# Two plugin sections share Hermes's 8,000-character budget, headings included (A3.3).
SECTION_MAX_CHARS = 3800


def toolset_name(role: Role) -> str:
    return f"ide-{role}"


def make_register(role: Role) -> Callable[[Any], None]:
    def register(ctx: Any) -> None:
        from opentelemetry import trace

        from research_core.plugin.advisory import make_hooks
        from research_core.plugin.executor import make_handler
        from research_core.plugin.messages import record_native_message
        from research_core.plugin.tracing import make_instrumentation

        hooks, session_turn = make_instrumentation(role)
        ctx.register_hook(
            "pre_api_request",
            partial(
                record_native_message,
                span_for_session=lambda session_id: (
                    trace.INVALID_SPAN if (turn := session_turn(session_id)) is None else turn.span
                ),
            ),
        )
        for hook_name, callback in hooks.items():
            ctx.register_hook(hook_name, callback)

        for spec in tools_for(role):
            ctx.register_tool(
                name=spec.name,
                toolset=toolset_name(role),
                schema=spec.schema(role),
                handler=make_handler(spec, role, session_turn),
                description=spec.description,
            )
        from research_core.plugin.repeats import make_hooks as make_repeat_hooks
        from research_core.plugin.repeats import without_repeated_refusals

        ctx.register_middleware("llm_request", without_repeated_refusals)
        from research_core.plugin.hermes_prompt import without_mandatory_tool_use

        ctx.register_middleware("llm_request", without_mandatory_tool_use)
        # Before the advisory hooks: Hermes keeps the first string a transform_tool_result returns.
        for hook_name, callback in make_repeat_hooks(role).items():
            ctx.register_hook(hook_name, callback)
        for hook_name, callback in make_hooks(role).items():
            ctx.register_hook(hook_name, callback)
        from research_core.plugin.inference_hook import bound_output_request, make_api_request_error

        ctx.register_hook("api_request_error", make_api_request_error())
        ctx.register_middleware("llm_request", bound_output_request)
        from research_core.plugin.briefing import make_section as make_briefing_section

        ctx.register_system_prompt_section(
            "ide-briefing", make_briefing_section(role), max_chars=SECTION_MAX_CHARS
        )
        from research_core.plugin.capture import register_capture
        from research_core.plugin.turn import make_pre_llm_call

        on_context = register_capture(ctx)
        ctx.register_hook("pre_llm_call", make_pre_llm_call(role, on_context))
        from research_core.plugin.session_context import make_approval_audience, make_section

        ctx.register_system_prompt_section(
            "ide-session-context", make_section(role), max_chars=SECTION_MAX_CHARS
        )
        ctx.register_hook("approval_audience", make_approval_audience(role))

    register.__name__ = f"register_{role}"
    return register
