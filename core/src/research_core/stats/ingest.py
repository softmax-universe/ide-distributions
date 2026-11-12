"""Episode rows: parse the mixin ``evaluate`` output, clean rows, pair baseline with candidate."""

import json
import math
from dataclasses import dataclass, field
from typing import Literal

from research_core.stats.design import EvaluationDesign


@dataclass
class EpisodeRow:
    episode_id: str
    game_version: str
    policy_version: str
    seed: int
    seats: tuple[int, ...]
    opponents: tuple[str, ...]
    metrics: dict[str, float]
    status: Literal["completed", "failed"]
    reason: str | None = None  # why a failed row failed, when the producer knows


@dataclass
class IngestReport:
    kept: int = 0
    duplicates: int = 0
    failed: int = 0
    wrong_version: int = 0
    wrong_seats: int = 0
    wrong_seed: int = 0
    missing_metrics: list[str] = field(default_factory=list)


_STATUSES = ("completed", "failed")
_ENTRY_KEYS = frozenset({"episode", "seed", "seat", "opponents", "metrics", "status", "reason"})


def rows_from_evaluate_output(
    text: str, *, policy_version: str, game_version: str, request_id: str | None = None
) -> list[EpisodeRow]:
    """Parse the output of a mixin's ``evaluate`` command into one row per (episode, seat).

    This is the contract a mixin's ``evaluate`` command meets: it prints JSON lines, one object
    per seat it scored in each episode, for example::

        {"episode": 0, "seed": 1853398679, "seat": 0, "opponents": ["pv-opp"],
         "metrics": {"score": 12.5, "errors": 1}}

    - ``episode`` (integer or non-empty string, required): the episode's index or id within
      the request.
    - ``seed`` (integer, required): the game seed the episode played.
    - ``seat`` (integer, required): the roster slot whose metrics this line reports: the game's
      0-based player slot, as its results index players. Our policy sits in the seat
      ``XP_SEAT`` names (0); ``xp.status`` keeps that seat's rows as the Version's and files the
      other seats' rows as context, so a mixin may print every seat or only ``XP_SEAT``.
    - ``opponents`` (list of strings, optional, default ``[]``): the other seats' policy labels.
    - ``metrics`` (object of metric name to finite number): required unless ``status`` is
      ``"failed"``.
    - ``status`` (``"completed"`` or ``"failed"``, optional, default ``"completed"``).
    - ``reason`` (string, optional): why a failed episode failed.

    Lines that do not start with ``{`` are log output and skipped: stderr shares the log the
    output is read from, so a warning or a ``uv`` progress line must not break evaluate. In an
    object line, any other key, or a wrong type, raises ``ValueError`` naming the line, and so
    does a second line for the same episode and seat (``ingest_rows`` would silently keep only
    the first, losing every later episode a mixin numbered alike). ``episode_id`` is
    ``<request_id>:<episode>:s<seat>`` (``<episode>:s<seat>`` without a ``request_id``); pass
    the platform request id so rows from different requests never share an id, because
    ``ingest_rows`` drops a repeated ``episode_id`` as a duplicate. The caller names the
    ``policy_version`` and ``game_version`` the request played; every row carries that
    ``policy_version``, so the caller relabels the rows of seats our policy did not sit in.
    """
    prefix = f"{request_id}:" if request_id else ""
    rows = []
    seen: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("{"):
            continue  # a log line: the command's stderr shares the output (mixin.run_command)
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"evaluate output line {number} is not JSON: {error.msg}") from error
        try:
            row = _row_from_entry(entry, prefix, policy_version, game_version)
        except ValueError as error:
            raise ValueError(f"evaluate output line {number}: {error}") from error
        if row.episode_id in seen:
            raise ValueError(
                f"evaluate output line {number} repeats episode {entry['episode']!r} seat"
                f" {entry['seat']} of line {seen[row.episode_id]}; episode must tell the"
                " request's episodes apart (its episode request id, or its index)"
            )
        seen[row.episode_id] = number
        rows.append(row)
    return rows


def _row_from_entry(entry: dict, prefix: str, policy_version: str, game_version: str) -> EpisodeRow:
    for key in ("episode", "seed", "seat"):
        if key not in entry:
            raise ValueError(f"missing key {key!r}")
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise ValueError(f"unknown keys {unknown}")
    episode, seed, seat = entry["episode"], entry["seed"], entry["seat"]
    if not (_is_int(episode) or (isinstance(episode, str) and episode)):
        raise ValueError("episode must be an integer or a non-empty string")
    if not _is_int(seed) or not _is_int(seat):
        raise ValueError("seed and seat must be integers")
    opponents = entry.get("opponents", [])
    if not isinstance(opponents, list) or not all(isinstance(o, str) for o in opponents):
        raise ValueError("opponents must be a list of strings")
    status = entry.get("status", "completed")
    if status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}, not {status!r}")
    reason = entry.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")
    if "metrics" not in entry and status != "failed":
        raise ValueError("missing key 'metrics'")
    metrics = entry.get("metrics", {})
    if not isinstance(metrics, dict) or not all(
        isinstance(name, str) and _is_finite_number(value) for name, value in metrics.items()
    ):
        raise ValueError("metrics must be an object of metric name to finite number")
    return EpisodeRow(
        episode_id=f"{prefix}{episode}:s{seat}",
        game_version=game_version,
        policy_version=policy_version,
        seed=seed,
        seats=(seat,),
        opponents=tuple(opponents),
        metrics={name: float(value) for name, value in metrics.items()},
        status=status,
        reason=reason,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def ingest_rows(
    rows: list[EpisodeRow], game_version: str, metrics: list[str]
) -> tuple[list[EpisodeRow], IngestReport]:
    """Drop duplicate (same episode_id), failed, wrong-game-version and metric-incomplete rows."""
    report = IngestReport()
    seen: set[str] = set()
    missing: set[str] = set()
    kept: list[EpisodeRow] = []
    for row in rows:
        if row.episode_id in seen:
            report.duplicates += 1
            continue
        seen.add(row.episode_id)
        if row.status == "failed":
            report.failed += 1
        elif row.game_version != game_version:
            report.wrong_version += 1
        elif lacking := [name for name in metrics if name not in row.metrics]:
            missing.update(lacking)
        else:
            kept.append(row)
    report.kept = len(kept)
    report.missing_metrics = sorted(missing)
    return kept, report


def ingest(
    rows: list[EpisodeRow], design: EvaluationDesign
) -> tuple[list[EpisodeRow], IngestReport]:
    """``ingest_rows`` plus the design's seat, seed and opponent checks (a seat or opponent
    mismatch counts as wrong_seats). A row's opponents must be its seed's
    (``design.opponents_for``: the sampled line-up or the fixed set)."""
    kept, report = ingest_rows(rows, design.game_version, [m.name for m in design.metrics])
    seats, seeds = tuple(design.seats), set(design.seeds)
    in_design = []
    for row in kept:
        if tuple(row.seats) != seats:
            report.wrong_seats += 1
        elif row.seed not in seeds:
            report.wrong_seed += 1
        elif tuple(row.opponents) != tuple(design.opponents_for(row.seed)):
            report.wrong_seats += 1
        else:
            in_design.append(row)
    report.kept = len(in_design)
    return in_design, report


def pair(rows: list[EpisodeRow], design: EvaluationDesign) -> list[tuple[EpisodeRow, EpisodeRow]]:
    """Match the design's baseline and candidate rows on (game_version, seed, seats, opponents).

    Returns ``(baseline_row, candidate_row)`` tuples sorted by key. Only the first row per side
    and key, in episode_id order, is used; extras are ignored. Rows of other policies are
    ignored too.
    """
    if design.candidate_version == design.baseline_version:
        raise ValueError("candidate_version equals baseline_version; nothing to pair")
    sides: dict[str, dict[tuple, EpisodeRow]] = {
        design.baseline_version: {},
        design.candidate_version: {},
    }
    for row in sorted(rows, key=lambda r: r.episode_id):
        side = sides.get(row.policy_version)
        if side is not None:
            key = (row.game_version, row.seed, tuple(row.seats), tuple(row.opponents))
            side.setdefault(key, row)
    baseline, candidate = sides[design.baseline_version], sides[design.candidate_version]
    return [(baseline[key], candidate[key]) for key in sorted(baseline.keys() & candidate.keys())]
