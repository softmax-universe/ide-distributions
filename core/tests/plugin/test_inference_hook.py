"""The ``api_request_error`` hook records failed model calls in the inference state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from research_core import inference
from research_core.plugin import register
from research_core.plugin.inference_hook import make_api_request_error, record_api_error

NOW = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
REFILL = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
def lease_file(tmp_path):
    return tmp_path / ".inference-lease"


def record(lease_file, reason, *, at=NOW, status_code=None, message="", session_id="api_1"):
    return record_api_error(
        lease_file,
        session_id=session_id,
        reason=reason,
        status_code=status_code,
        message=message,
        now=at,
    )


def test_billing_blocks_until_the_recorded_refill(lease_file):
    inference.lease_installed(
        lease_file, REFILL + inference.RENEWAL_GRACE, "sk-one", now=NOW - timedelta(hours=1)
    )
    state = record(lease_file, "billing", status_code=402)
    assert state.blocked == "budget_exhausted" and state.until == REFILL and state.since == NOW
    assert state.blocked_key_id == inference.key_id("sk-one")
    assert state.last_error == inference.ApiError("api_1", "billing", 402, NOW)
    assert inference.read_state(lease_file) == state


def test_billing_without_a_known_refill_blocks_until_midnight_utc(lease_file):
    assert record(lease_file, "billing").until == REFILL


def test_billing_after_the_lease_expired_blocks_until_the_refused_lease_refill(lease_file):
    inference.lease_installed(lease_file, NOW, "sk-one", now=NOW - timedelta(hours=1))
    inference.lease_refused(lease_file, REFILL - timedelta(hours=1), budget_exhausted=False)
    assert record(lease_file, "billing").until == REFILL - timedelta(hours=1)


def test_billing_with_a_bare_iso_lease_blocks_with_no_key_known(lease_file):
    lease_file.write_text((REFILL + inference.RENEWAL_GRACE).isoformat())
    state = record(lease_file, "billing", status_code=402)
    assert state.blocked == "budget_exhausted" and state.until == REFILL
    assert state.blocked_key_id is None
    assert not inference.available(lease_file, now=NOW)


def test_billing_soon_after_a_key_change_records_but_does_not_block(lease_file):
    # Runs that started on the spent key can still fail just after the new key arrives.
    inference.lease_installed(lease_file, REFILL + inference.RENEWAL_GRACE, "sk-new", now=NOW)
    late = record(lease_file, "billing", at=NOW + timedelta(seconds=30), status_code=402)
    assert late.blocked is None
    assert late.last_error == inference.ApiError(
        "api_1", "billing", 402, NOW + timedelta(seconds=30)
    )

    # After the grace period, billing on the new key blocks again.
    spent = record(lease_file, "billing", at=NOW + timedelta(minutes=3), status_code=402)
    assert spent.blocked == "budget_exhausted" and spent.until == REFILL
    assert spent.blocked_key_id == inference.key_id("sk-new")


def test_key_limit_403_counts_as_billing(lease_file):
    state = record(lease_file, "auth", status_code=403, message="Key limit exceeded (total limit)")
    assert state.blocked == "budget_exhausted"
    assert state.last_error.reason == "billing"


def test_outages_block_only_after_three_within_the_window(lease_file):
    first = record(lease_file, "overloaded", status_code=503)
    second = record(lease_file, "server_error", at=NOW + timedelta(seconds=30), status_code=500)
    assert first.blocked is None and second.blocked is None
    third = record(lease_file, "timeout", at=NOW + timedelta(seconds=60))
    assert third.blocked == "provider_unavailable"
    assert third.until == NOW + timedelta(seconds=60) + timedelta(minutes=5)


def test_spread_out_outages_do_not_block(lease_file):
    for minutes in (0, 4, 8, 12):
        state = record(lease_file, "overloaded", at=NOW + timedelta(minutes=minutes))
    assert state.blocked is None and len(state.outages) == 1


def test_other_classes_only_record_last_error(lease_file):
    for index, reason in enumerate(("rate_limit", "context_overflow", "rate_limit", "unknown")):
        state = record(lease_file, reason, at=NOW + timedelta(seconds=index), session_id=reason)
    assert state.blocked is None and state.outages == ()
    last = inference.ApiError("unknown", "unknown", None, NOW + timedelta(seconds=3))
    assert state.last_error == last


def test_an_outage_does_not_override_a_budget_block(lease_file):
    record(lease_file, "billing")
    for seconds in (1, 2, 3):
        state = record(lease_file, "overloaded", at=NOW + timedelta(seconds=seconds))
    assert state.blocked == "budget_exhausted" and state.until == REFILL
    assert state.last_error.reason == "overloaded"


def test_hook_reads_the_lease_path_from_the_environment(lease_file, monkeypatch):
    monkeypatch.setenv("IDE_INFERENCE_LEASE_FILE", str(lease_file))
    hook = make_api_request_error()
    assert (
        hook(
            session_id="api_9",
            reason="billing",
            status_code=402,
            error={"type": "APIStatusError", "message": "Insufficient credits"},
            task_id="t",
            telemetry_schema_version=1,
        )
        is None
    )
    state = inference.read_state(lease_file)
    assert state.blocked == "budget_exhausted" and state.last_error.session_id == "api_9"
    # task_id (the run's own session id) survives a round trip; compression rotates session_id.
    assert state.last_error.task_id == "t"


def test_hook_does_nothing_without_a_lease_file_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("IDE_INFERENCE_LEASE_FILE", raising=False)
    hook = make_api_request_error()
    hook(session_id="api_1", reason="billing")
    assert list(tmp_path.iterdir()) == []

    (tmp_path / "a-file").write_text("")
    monkeypatch.setenv("IDE_INFERENCE_LEASE_FILE", str(tmp_path / "a-file" / ".inference-lease"))
    hook(session_id="api_1", reason="billing", status_code="402", error="not a dict")


class HookContext:
    def __init__(self):
        self.hooks = []

    def register_middleware(self, name, callback):
        self.hooks.append(name)

    def register_tool(self, **kwargs):
        pass

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def register_system_prompt_section(self, *args, **kwargs):
        pass


def test_both_profiles_register_the_hook():
    for role in ("orchestrator", "researcher"):
        ctx = HookContext()
        register.make_register(role)(ctx)
        assert "api_request_error" in ctx.hooks


@pytest.mark.parametrize("affordable", ["105913", "97,700", "1"])
def test_affordable_smaller_reply_does_not_block_workspace(lease_file, affordable):
    state = record(
        lease_file,
        "billing",
        status_code=402,
        message=f"You requested up to 131072 tokens, but can only afford {affordable}.",
    )
    assert state.blocked is None
    assert state.last_error.reason == "output_limit"


@pytest.mark.parametrize(
    "message",
    [
        "You requested up to 16384 tokens, but can only afford 0.",
        "Prompt tokens limit exceeded: 35795 > 0.",
    ],
)
def test_no_affordable_tokens_still_blocks(lease_file, message):
    state = record(lease_file, "billing", status_code=402, message=message)
    assert state.blocked == "budget_exhausted"


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
def test_output_middleware_caps_reservations_and_preserves_smaller_requests(key):
    from research_core.plugin.inference_hook import bound_output_request

    for requested, expected in [(131072, 16384), (1024, 1024)]:
        request = {key: requested, "model": "moonshotai/kimi-k3.1-pro"}
        result = bound_output_request(request=request, provider="openrouter")
        assert result["request"][key] == expected
        assert request[key] == requested


@pytest.mark.parametrize(
    "body, key", [({"messages": []}, "max_tokens"), ({"input": []}, "max_output_tokens")]
)
def test_output_middleware_sets_limit_when_native_request_omits_it(body, key):
    from research_core.plugin.inference_hook import bound_output_request

    request = {"model": "moonshotai/kimi-k3.1-pro", **body}
    result = bound_output_request(request=request, provider="openrouter")
    assert result["request"] == {**request, key: 16384}
    assert key not in request
