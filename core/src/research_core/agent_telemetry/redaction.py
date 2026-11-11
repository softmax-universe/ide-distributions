"""Take secrets out of captured text before it is hashed or written.

The gateway writer calls Hermes's own scanner and adds patterns for Softmax credential tokens
(User, Player, cogent agent, and the platform's service tokens).
Those patterns stay here and are not registered with Hermes, because a registered pattern would also
change what the agent sees in its tool output. Tested on 2026-11-04: Hermes alone leaves a bare
``usr_...`` token untouched.

Only the gateway has this scanner and only the gateway sends free text. Every other writer has only
``mask_softmax_tokens``, for the metadata strings it sends. Digests and byte offsets in records
refer to the redacted text.

An image's base64 text is left alone. Hermes's JWT pattern matches ``eyJ`` wherever it occurs, so a
scan damaged each of three 1 MB test images (2026-11-04), and no pattern can read a secret out of
base64.

A token split by a control character passes the Softmax patterns. Hermes joins such splits only for
its own list of formats.

Hermes's scanner has known gaps, each seen on the real one (Hermes 0.22.6, 2026-11-05):
``docs/agent-telemetry.md``, "Known holes". Nothing here closes them.

Hermes keeps exact browser vault values per profile home and looks them up from the calling
thread's scope. The writer thread has no profile scope, so it does not apply them. Hermes already
removes those values from browser tool results before the model sees them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from research_core.agent_telemetry.records import clean_text

# Every prefix in metta's ``CredentialTokenPrefix`` except ``code_`` (a short-lived auth code, and a
# common word) names a token built the same way: the prefix and the URL-safe base64 of 32 random
# bytes (``observatory_core/credential_tokens.py``), so exactly 43 characters and one "=". The
# workspace's own Player credential is a ``cga_`` (cogent agent) token. A Player ID is ``ply_`` and
# a 36-character UUID, which must survive. No letter or digit comes before a token, except the end
# of an escape in escaped text: ``\nusr_...``, ``\u000ausr_...``, ``%20usr_...``.
SOFTMAX_TOKEN_PREFIXES = ("usr_", "ply_", "cga_", "rrt_", "cmr_", "mch_", "xpw_")
_SOFTMAX_TOKEN = re.compile(
    r"(?:(?<![A-Za-z0-9])|(?<=\\[nrtbf])|(?<=\\u[0-9a-fA-F]{4})|(?<=%[0-9a-fA-F]{2}))"
    rf"(?:{'|'.join(prefix[:-1] for prefix in SOFTMAX_TOKEN_PREFIXES)})_[A-Za-z0-9_-]{{43}}"
    r"(?![A-Za-z0-9_-])=?"
)
SOFTMAX_TOKEN_MASK = "[redacted-softmax-token]"
_SECRET_FIELD_MASK = "[redacted-secret-field]"
_BASE64_IMAGE = re.compile(r"data:image/[a-zA-Z0-9.+-]{1,64};base64,[A-Za-z0-9+/]*={0,2}")
_SOFTMAX_RULES = "softmax-1"


def mask_softmax_tokens(text: str) -> str:
    """The patterns above alone: what a writer outside the gateway, with no scanner, applies."""
    return _SOFTMAX_TOKEN.sub(SOFTMAX_TOKEN_MASK, clean_text(text))


class Redactor:
    """One scanner and its version. ``is_secret_field`` names keys whose text is a secret."""

    def __init__(
        self,
        scrub: Callable[[str], str],
        version: str,
        is_secret_field: Callable[[str], bool] = lambda _key: False,
    ) -> None:
        self._scrub = scrub
        self._is_secret_field = is_secret_field
        self.version = version

    def text(self, text: str) -> str:
        if _BASE64_IMAGE.fullmatch(text):
            return text
        return self._scrub(mask_softmax_tokens(text))

    def value(self, value: Any) -> Any:
        """A JSON value with every string redacted, walked so that a mask cannot break the JSON.

        A key's name can show that its value is secret where the string alone cannot.
        """
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return self._mapping(value)
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value

    def _mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            name = self.text(key)
            # Two secrets used as keys become one mask, and both values must survive.
            while name in redacted:
                name += "#"
            if item and isinstance(item, str) and self._is_secret_field(key):
                redacted[name] = _SECRET_FIELD_MASK
            else:
                redacted[name] = self.value(item)
        return redacted


def hermes_redactor() -> Redactor:
    """The gateway writer's redactor. It imports Hermes, so it runs only in Hermes's interpreter.

    ``redact_for_egress`` is Hermes's scrub for text leaving the process. It ignores the agent's
    ``security.redact_secrets`` setting and returns a marker rather than raw text if it fails.
    """
    from agent.redact import is_secret_field_name, redact_for_egress
    from hermes_cli import __version__ as hermes_version

    return Redactor(
        redact_for_egress,
        f"hermes-{hermes_version}+{_SOFTMAX_RULES}",
        is_secret_field_name,
    )
