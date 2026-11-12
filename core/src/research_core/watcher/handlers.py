"""Event vocabulary and model prompt selection. Only Coordinator decides transitions."""

from dataclasses import dataclass

WAKE_KINDS = ("setup", "review", "management", "anti_stall")


@dataclass(frozen=True)
class Handler:
    kind: str
    wake: str | None
    batch_by_kind: bool = False

    @property
    def model(self) -> bool:
        return self.wake is not None


def _wake(kind: str, wake: str, *, batch_by_kind: bool = False) -> Handler:
    return Handler(kind, wake, batch_by_kind)


def _function(kind: str) -> Handler:
    return Handler(kind, None)


HANDLERS: dict[str, Handler] = {
    entry.kind: entry
    for entry in (
        _wake("coworld.setup_needed", "setup"),
        _wake("task.completed", "review"),
        # A rejected review resumes the Researcher with the reason; no model is needed for that.
        _function("task.reviewed"),
        _wake("task.asked", "management"),
        _wake("task.proposed", "management"),
        _wake("task.proposed_submission", "management"),
        _wake("merge.failed", "management"),
        # A merged Version the Researcher marked as a submission candidate: the Orchestrator
        # decides under the submissions settings and the quality bar (now met on the merge).
        _wake("submission.candidate", "management"),
        _function("task.wait_resolved"),
        _function("capability.failed"),
        # After the second failed repair the user hears about it through a notice.
        _function("repair.failed_twice"),
        _wake("goal.needs_ideation", "management"),
        _wake("goal.park", "management"),
        _function("workspace.resumed"),
        # Abandoning a Task stops its run.
        _function("task.abandoned"),
        # A failed Task feeds the repair counter of its capability, or asks for a reader.
        _function("task.failed"),
        _wake("task.failure_review", "management", batch_by_kind=True),
        _wake("champion.changed", "management", batch_by_kind=True),
        _wake("store.unmarked_write", "management"),
        _wake("store.schema_rebuilt", "management"),
        # A store a newer release wrote: the watcher paused the workspace and left it alone.
        _wake("store.schema_newer", "management"),
        _wake("recovery.failed", "management"),
        _wake("wake.failed", "management"),
        _wake("anti_stall", "anti_stall"),
    )
}

# Emitted kinds the watcher ignores. Listed by hand rather than computed from KINDS, so a new
# event kind fails the ownership test until someone decides which set it belongs in.
UNWATCHED_KINDS: frozenset[str] = frozenset(
    {
        "session.dead",
        "task.stuck",
        "transition.requested",
        "transition.applied",
        "transition.refused",
        "transition.retry_waiting",
        "execution.queued",
        "execution.admitted",
        "task.assigned",
        "task.release_requested",
        "goal.created",
        "goal.updated",
        "goal.status",
        "task.created",
        "task.updated",
        "task.approved",
        "task.claimed",
        "task.released",
        "task.waiting",
        "task.answered",
        "task.message",
        "task.reopened",
        "task.merged",
        "claim.created",
        "claim.updated",
        "claim.retired",
        "experiment.created",
        "experiment.updated",
        "experiment.started",
        "experiment.run",
        "experiment.closed",
        "experiment.abandoned",
        "version.created",
        "version.uploaded",
        "version.submitted",
        "directive.added",
        "directive.updated",
        "directive.retired",
        "settings.updated",
        "capability.repaired",
        "intent.pending",
        "intent.done",
        "intent.failed",
        "session.started",
        "session.ended",
        "notice",
        "chat_update.recorded",
        "chat_update.delivered",
        "ui.patch",
        "coworld.registered",
        "coworld.switched",
        "coworld.set_up",
        "platform.request",
        "platform.forum_post",
        "platform.wiki_edit",
        "turn.rendered",
        "watcher.heartbeat",
        "checks.ran",
        "drain.done",
        "workspace.sleep",
        "profile.model_updated",
    }
)

MODEL_KINDS: frozenset[str] = frozenset(k for k, entry in HANDLERS.items() if entry.model)
FUNCTION_KINDS: frozenset[str] = frozenset(k for k, entry in HANDLERS.items() if not entry.model)
