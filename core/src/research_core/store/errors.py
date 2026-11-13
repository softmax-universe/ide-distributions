"""Store errors. Every refusal carries all of its problems at once (design B.2).

Messages never name the store path: tools show them to agents and the path stays out of every
prompt, skill and tool output (B.10). Say "the store", never where it is.
"""

from __future__ import annotations

from collections.abc import Sequence


class StoreError(Exception):
    """A refused request. ``problems`` lists everything wrong with it."""

    def __init__(self, problems: Sequence[str] | str) -> None:
        if isinstance(problems, str):
            problems = [problems]
        self.problems: list[str] = list(problems)
        super().__init__("; ".join(self.problems))


class SchemaMismatch(StoreError):
    """The store's shape differs from the release's migrations; nothing works through it.

    The watcher repairs this in place, keeping the rows (``migrate.rebuild_keeping_rows``).
    """


class SchemaNewer(StoreError):
    """A newer release wrote the store: its ``user_version`` is above this release's migrations.

    Never repaired: the newer shape is legitimate and this release cannot know it. The file is
    left untouched until the matching or a newer runtime opens it.
    """

    def __init__(self, user_version: int, expected_version: int) -> None:
        self.user_version = user_version
        self.expected_version = expected_version
        super().__init__(
            f"the store's schema version is {user_version}; this release knows version"
            f" {expected_version}: a newer release wrote it, so it is left untouched"
        )


class NotFound(StoreError):
    """A referenced record does not exist."""


class IdReused(StoreError):
    """An op_id was reused with different input."""


class LeaseError(StoreError):
    """A Task-scoped write from someone other than the holder at the current epoch."""
