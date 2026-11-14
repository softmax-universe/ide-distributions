"""The restricted profile sends only its own text conversation and runs no tools."""

from __future__ import annotations

import logging
from typing import Any

from research_core.authority import RestrictedAuthority, bound_authority, require_active
from research_core.plugin.authority import TOOL_DENIED
from research_core.plugin.executor import profile_env
from research_core.restricted_profile import INFERENCE_KEY

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a Softmax cogent in a private conversation with an application. "
    "Use only this conversation. You have no tools or access to the owner's profile, "
    "other conversations, files, credentials, or workspace resources."
)
REQUEST_OPTIONS = frozenset(
    {
        "model",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "stop",
        "timeout",
    }
)


def conversation_request(request: dict[str, Any]) -> dict[str, Any]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for message in request["messages"]:
        if message["role"] in {"system", "developer"}:
            continue
        if message["role"] not in {"user", "assistant"}:
            raise PermissionError("Restricted conversations contain text messages only")
        messages.append({"role": message["role"], "content": conversation_text(message["content"])})
    if len(messages) == 1:
        raise PermissionError("Restricted conversation has no messages")
    return {key: value for key, value in request.items() if key in REQUEST_OPTIONS} | {
        "messages": messages
    }


def conversation_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        for part in content
    ):
        return "".join(part["text"] for part in content)
    raise PermissionError("Restricted conversations contain text messages only")


def authorize_inference(
    *, request: dict[str, Any], next_call: Any, session_id: str = "", task_id: str = "", **_: Any
) -> Any:
    try:
        env = profile_env()
        authority = bound_authority(env, task_id or session_id)
        if not isinstance(authority, RestrictedAuthority):
            return None
        require_active(authority, env)
        clean = conversation_request(request)
        key = env[INFERENCE_KEY]
        if not key:
            return None
        clean["extra_headers"] = {"Authorization": f"Bearer {key}"}
    except Exception as error:
        # Returning without next_call denies the provider call; raising would make Hermes skip us.
        log.warning("Restricted inference denied (%s)", type(error).__name__)
        return None
    return next_call(clean)


def deny_tools(**_: Any) -> str:
    return TOOL_DENIED


def register(ctx: Any) -> None:
    ctx.register_middleware("llm_execution", authorize_inference)
    ctx.register_middleware("tool_execution", deny_tools)
    from research_core.plugin.inference_hook import bound_output_request, make_api_request_error
    from research_core.plugin.reasoning_relay import register_relay

    ctx.register_middleware("llm_request", bound_output_request)
    ctx.register_hook("api_request_error", make_api_request_error())
    register_relay(ctx)
