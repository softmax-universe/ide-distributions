"""Capture agent operations as lines on the container's output stream, for Datadog.

``CONTRACT.md`` beside this file says what a line and a body hold, and how a request is rebuilt from
them. ``docs/agent-telemetry.md`` says what is captured and how to read it.

Every module here is stdlib only, because the gateway plugin imports them in Hermes's interpreter.
"""

from research_core.agent_telemetry.encoder import ContextPart, PromptFile, PromptSection
from research_core.agent_telemetry.records import Producer
from research_core.agent_telemetry.redaction import hermes_redactor
from research_core.agent_telemetry.stream import stream_path
from research_core.agent_telemetry.writer import InlineWriter, TelemetryWriter, Writer

__all__ = [
    "ContextPart",
    "InlineWriter",
    "Producer",
    "PromptFile",
    "PromptSection",
    "TelemetryWriter",
    "Writer",
    "hermes_redactor",
    "stream_path",
]
