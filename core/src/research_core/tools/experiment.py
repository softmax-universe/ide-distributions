"""The ``experiment`` tool (design B.8, C.2): a comparison of Versions that ends in a verdict.

Every action is available to both roles. The store module does the record work (validation,
the design lock at ``start``, the lease check when the Experiment has a Task); this module maps
arguments to it and adds the one computation a tool needs numeric libraries for: the computed
verdict at ``close``. That is why the tool runs in the workspace environment (A3.1) even though
most of its actions are store-only.

``close`` has two paths:

- **Explicit**: ``verdict`` is given with ``measurements`` and ``interpretation``; the reason
  codes are ``reason_codes`` or ``["explicit:<reason>"]``. This records a judgement (usually the
  Orchestrator's) when the decision rule cannot be applied as pre-registered.
- **Computed** (no ``verdict``): the Experiment's methods become an ``EvaluationDesign``
  (the candidate is ``methods.candidate`` or, when that stayed empty at start, the one Version
  built for the Experiment), the rows stored on its runs are paired baseline against candidate
  by seed, and ``decision_rule.evaluate`` gives the verdict, the reason codes and one
  measurement per metric. ``interpretation`` is optional here; a one-line summary is generated
  when it is absent.

**The results-row contract.** A tool that fetches evaluation results (``xp.status``) stores them
on the run with ``update_run {results: {"rows": [...]}}``. Each row is an object with:

- ``episode_id`` (string, unique within the Experiment),
- ``game_version`` (string),
- ``policy_version`` (string): the Version the run played, written as its record id
  (``methods.baseline`` for a baseline arm, ``methods.candidate`` for a candidate arm, e.g.
  ``"V-3"``); rows naming any other policy never pair,
- ``seed`` (integer), ``seats`` (list of integers), ``opponents`` (list of strings),
- ``metrics`` (object of metric name to number; every metric named in ``methods.metrics``),
- ``status`` (``"completed"`` by default, or ``"failed"``) and ``reason`` (string, optional).

The computed close is refused while any run's rows may still change (``experiments.unsettled``:
a request still running, a finished one ``xp.status`` has not evaluated, an evaluate awaiting
repair), because a verdict is final and read early it would miss them. Rows of a run marked
invalid do not count; the result names those runs in ``evaluation.invalid_runs``. Only rows of
the run's own seat count (``results.seat``, which ``xp.request`` records; 0 for older runs): a
row of another seat is an opponent's and is counted in ``evaluation.rows.dropped.other_seats``.
Rows with ``status: failed``, a repeated ``episode_id``, another game version or a missing
metric are dropped and counted in ``evaluation.rows.dropped`` too. Several rows for one
``(policy_version, game_version, seed, opponents)`` - several episodes on one seed - are averaged
into one row, so each seed makes exactly one baseline/candidate pair. The ``evaluation`` design's
seats, opponents and game version are read from the first row; its seeds are the runs' seeds
and the rows' seeds together.

numpy is imported only inside the bootstrap that ``evaluate`` calls, so importing this module
stays light.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from research_core.platform.xp import OUR_SEAT
from research_core.stats import decision_rule, ingest
from research_core.stats.design import EvaluationDesign, MetricSpec
from research_core.stats.ingest import EpisodeRow
from research_core.store import experiments
from research_core.tools import _args as a
from research_core.tools.base import ActionSpec, ToolCall, ToolError, ToolSpec, tool

EXPERIMENT = a.string("The Experiment, such as X-5.")

HYPOTHESIS = a.obj(
    "",
    {
        "statement": a.string("The hypothesis, in one sentence."),
        "if_true": a.string("What we do if it holds."),
        "if_false": a.string("What we do if it does not; must differ from if_true."),
        "motivation": a.string("Why we think it might hold."),
    },
    closed=True,
)
METRIC = a.obj(
    "",
    {
        "name": a.string("The metric, such as win_rate."),
        "direction": a.string("Which way is better.", enum=list(experiments.METRIC_DIRECTIONS)),
        "role": a.string("At least one metric is primary.", enum=list(experiments.METRIC_ROLES)),
        "min_effect": {"type": "number", "description": "Primary: the smallest effect worth it."},
        "tolerance": {
            "type": "number",
            "description": "Guardrail or performance: how far it may drop; none: any drop refutes.",
        },
    },
    required=("name", "direction", "role"),
    closed=True,
)
METHODS = a.obj(
    "",
    {
        "baseline": a.string("The baseline Version, such as V-11."),
        "candidate": a.string("The candidate Version; may stay empty until it is built."),
        "procedure": a.string("How the runs are done."),
        "metrics": a.objects("At least one, one of them primary.", METRIC, min_items=1),
        "decision_rule": a.obj(
            "How close decides the verdict.",
            {
                "text": a.string("The rule, in words."),
                "n_min": a.integer("Paired seeds before deciding."),
            },
            required=("text", "n_min"),
            closed=True,
        ),
    },
    closed=True,
)
MEASUREMENT = a.obj(
    "",
    {
        "metric": a.string("A metric named in the methods."),
        "baseline": {"type": "number", "description": "The baseline arm's value."},
        "candidate": {"type": "number", "description": "The candidate arm's value."},
        "n": a.integer("How many pairs."),
        "effect": {
            "type": "number",
            "description": "Candidate minus baseline, signed so that positive is better.",
        },
    },
    required=experiments.MEASUREMENT_KEYS,
)
# xp.status writes rows, so a call rarely does; a bad row is named at close. The keys are spelled
# out in words, not as a schema: a schema with them was the largest single part of this tool.
RESULTS = a.free_object(
    "The run's results; each key given replaces that key, others are kept. xp.status writes "
    "evaluation rows under rows: [{episode_id, game_version, policy_version, seed, seats, "
    "opponents, metrics, status?, reason?}]."
)
# The design fields create takes and update replaces (a section or a list as a whole).
DESIGN = {
    "summary": a.string("What is being tested, in at most 40 words."),
    "tags": a.strings("Keywords from the shared vocabulary; the lever is a tag."),
    "hypothesis": {**HYPOTHESIS, "description": "experiment.start needs every part."},
    "methods": {**METHODS, "description": "experiment.start needs all but candidate."},
    "related_claims": a.strings("Claims it builds on, such as C-12."),
    "related_experiments": a.strings("Experiments it follows, such as X-4."),
}
SAME_KEY = "As in experiment_create."
# What update takes: the same sections, whose keys experiment_create describes in the same list.
DESIGN_UPDATE = {
    **DESIGN,
    "hypothesis": a.obj(
        "Replaces the hypothesis whole.",
        {key: a.string(SAME_KEY) for key in experiments.HYPOTHESIS_KEYS},
        closed=True,
    ),
    "methods": a.obj(
        "Replaces the methods whole.",
        {
            "baseline": a.string(SAME_KEY),
            "candidate": a.string(SAME_KEY),
            "procedure": a.string(SAME_KEY),
            "metrics": a.objects(
                SAME_KEY,
                a.obj(
                    "",
                    {
                        "name": a.string(SAME_KEY),
                        "direction": a.string(SAME_KEY, enum=list(experiments.METRIC_DIRECTIONS)),
                        "role": a.string(SAME_KEY, enum=list(experiments.METRIC_ROLES)),
                        "min_effect": {"type": "number", "description": SAME_KEY},
                        "tolerance": {"type": "number", "description": SAME_KEY},
                    },
                    required=("name", "direction", "role"),
                    closed=True,
                ),
                min_items=1,
            ),
            "decision_rule": a.obj(
                SAME_KEY,
                {"text": a.string(SAME_KEY), "n_min": a.integer(SAME_KEY)},
                required=("text", "n_min"),
                closed=True,
            ),
        },
        closed=True,
    ),
}
UPDATABLE = ("summary", "hypothesis", "methods", "tags", "related_claims", "related_experiments")
NO_ROWS = "no evaluation rows recorded yet; run xp.status first or close with an explicit verdict"
ROW_LIST_KEYS = ("seats", "opponents")


def handle(call: ToolCall) -> dict:
    store, actor = call.ctx.store, call.ctx.actor
    if call.action == "create":
        goal, task = call.get("goal"), call.get("task")
        record = experiments.create(
            store,
            actor,
            summary=call.get("summary"),
            goal_id=a.record_number(goal, "goal", "goal") if goal is not None else None,
            task_id=a.record_number(task, "task", "task") if task is not None else None,
            tags=call.get("tags") or [],
            hypothesis=call.get("hypothesis"),
            methods=call.get("methods"),
            related_claims=call.get("related_claims") or [],
            related_experiments=call.get("related_experiments") or [],
        )
        return {"record": record}
    if call.action == "search":
        goal = call.get("goal")
        records = experiments.search(
            store,
            call.get("query"),
            tags=call.get("tags"),
            goal_id=a.record_number(goal, "goal", "goal") if goal is not None else None,
            limit=call.get("limit") or 20,
        )
        return {"records": records}
    if call.action == "update_run":
        if call.get("experiment") is not None:
            experiments.require_run_of(
                store,
                call.get("run"),
                a.record_number(call.get("experiment"), "experiment", "experiment"),
            )
        record = experiments.update_run(
            store,
            actor,
            call.get("run"),
            state=call.get("state"),
            episodes_completed=call.get("episodes_completed"),
            results=call.get("results"),
            validity=call.get("validity"),
        )
        return {"record": record}
    experiment_id = a.record_number(call.get("experiment"), "experiment", "experiment")
    if call.action == "read":
        return {"record": experiments.read(store, experiment_id, section=call.get("section"))}
    if call.action == "update":
        fields = {name: call.get(name) for name in UPDATABLE if call.get(name) is not None}
        return {"record": experiments.update(store, actor, experiment_id, **fields)}
    if call.action == "start":
        return {"record": experiments.start(store, actor, experiment_id)}
    if call.action == "close":
        if call.get("verdict") is not None:
            return explicit_close(call, experiment_id)
        return computed_close(call, experiment_id)
    if call.action == "abandon":
        return {
            "record": experiments.abandon(store, actor, experiment_id, reason=call.get("reason"))
        }
    raise AssertionError(call.action)


# --- close: the explicit path -----------------------------------------------------------------


def explicit_close(call: ToolCall, experiment_id: int) -> dict:
    problems = []
    if not call.get("measurements"):
        problems.append("measurements are required with an explicit verdict")
    if not call.get("interpretation"):
        problems.append("interpretation is required with an explicit verdict")
    reason_codes, reason = call.get("reason_codes"), call.get("reason")
    if not reason_codes and not reason:
        problems.append("reason or reason_codes is required with an explicit verdict")
    if problems:
        raise ToolError(problems)
    record = experiments.close(
        call.ctx.store,
        call.ctx.actor,
        experiment_id,
        verdict=call.get("verdict"),
        measurements=call.get("measurements"),
        interpretation=call.get("interpretation"),
        reason_codes=reason_codes or [f"explicit:{reason}"],
    )
    return {"record": record}


# --- close: the computed path -----------------------------------------------------------------


def computed_close(call: ToolCall, experiment_id: int) -> dict:
    store, actor = call.ctx.store, call.ctx.actor
    record = experiments.read(store, experiment_id)
    if record["status"] != "running":
        raise ToolError(
            f"{record['id']} is {record['status']}; an Experiment closes only while it is running"
        )
    unsettled = {
        run["id"]: reason for run in record["runs"] if (reason := experiments.unsettled(run))
    }
    if unsettled:
        # A close is final: computed now, it would leave out rows still on their way (often
        # n_short, so inconclusive for good) or the runs of an evaluate awaiting repair.
        raise ToolError(
            f"{record['id']} has {len(unsettled)} runs whose rows are not final, so the rule"
            f" cannot be applied yet: xp.status {{experiment: {record['id']}}} refreshes and"
            " evaluates them. A run that will never count: experiment.update_run {run,"
            " validity: 'invalid: <reason>'}",
            unsettled_runs=unsettled,
        )
    candidate = candidate_version(record)
    counted = [run for run in record["runs"] if run["validity"] == "valid"]
    excluded = [run["id"] for run in record["runs"] if run["validity"] != "valid"]
    rows, other_seats = rows_by_seat(counted)
    if not rows:
        raise ToolError(NO_ROWS, invalid_runs=excluded)
    design = design_from(record["methods"], candidate, counted, rows)
    kept, report = ingest.ingest_rows(rows, design.game_version, [m.name for m in design.metrics])
    pairs = ingest.pair(one_row_per_seed(kept), design)
    if not pairs:
        raise ToolError(
            f"{record['id']} has {len(kept)} usable rows but no baseline/candidate pair: rows pair"
            f" when {design.baseline_version} and {design.candidate_version} rows share a seed,"
            " seats, opponents and game version; close with an explicit verdict if the rows"
            " cannot be repaired",
            rows_by_policy=dict(Counter(row.policy_version for row in kept)),
            dropped=dropped(report, other_seats),
        )
    verdict = decision_rule.evaluate(design, pairs)
    measurements = measurements_from(design, pairs, verdict)
    interpretation = call.get("interpretation") or summarise(verdict, measurements, len(pairs))
    closed = experiments.close(
        store,
        actor,
        experiment_id,
        verdict=verdict.verdict,
        measurements=measurements,
        interpretation=interpretation,
        reason_codes=verdict.reason_codes,
    )
    return {
        "record": closed,
        "evaluation": {
            "pairs": len(pairs),
            "rows": {"kept": report.kept, "dropped": dropped(report, other_seats)},
            "invalid_runs": excluded,
            "reason_codes": verdict.reason_codes,
        },
    }


def stored_rows(runs: list[dict]) -> list[EpisodeRow]:
    """Every row of our seat under ``results.rows`` of the runs given (``rows_by_seat``)."""
    return rows_by_seat(runs)[0]


def rows_by_seat(runs: list[dict]) -> tuple[list[EpisodeRow], int]:
    """The rows under ``results.rows`` that cover the run's own seat, and how many did not.

    A run's seat is ``results.seat`` (``xp.request`` records it; runs from before default to
    seat 0, where every request has put our policy). A row whose seats leave it out is an
    opponent's:
    ``xp.status`` once stored every seat a mixin scored under our Version, so those rows are left
    out here and counted. Every malformed row is named at once.
    """
    rows: list[EpisodeRow] = []
    other_seats = 0
    problems: list[str] = []
    for run in runs:
        results = run.get("results") or {}
        stored = results.get("rows")
        if stored is None:
            continue
        if not isinstance(stored, list):
            problems.append(f"run {run['id']} results.rows must be a list")
            continue
        seat = results.get("seat", OUR_SEAT)
        for index, data in enumerate(stored):
            try:
                row = row_from_dict(data)
            except ValueError as error:
                problems.append(f"run {run['id']} results.rows[{index}] {error}")
                continue
            if seat in row.seats:
                rows.append(row)
            else:
                other_seats += 1
    if problems:
        raise ToolError(problems)
    return rows, other_seats


def row_from_dict(data: Any) -> EpisodeRow:
    """An ``EpisodeRow`` from a stored row dict (the contract in the module docstring)."""
    if not isinstance(data, dict):
        raise ValueError("must be an object")
    problems = []
    for key in ("episode_id", "game_version", "policy_version"):
        if not isinstance(data.get(key), str) or not data.get(key):
            problems.append(f"{key} must be a non-empty string")
    if not _is_int(data.get("seed")):
        problems.append("seed must be an integer")
    seats = data.get("seats", [])
    if not isinstance(seats, list) or not all(_is_int(seat) for seat in seats):
        problems.append("seats must be a list of integers")
    opponents = data.get("opponents", [])
    if not isinstance(opponents, list) or not all(isinstance(o, str) for o in opponents):
        problems.append("opponents must be a list of strings")
    metrics = data.get("metrics", {})
    if not isinstance(metrics, dict) or not all(
        isinstance(name, str) and _is_number(value) for name, value in metrics.items()
    ):
        problems.append("metrics must be an object of metric name to number")
    status = data.get("status", "completed")
    if status not in ("completed", "failed"):
        problems.append("status must be completed or failed")
    if problems:
        raise ValueError("; ".join(problems))
    return EpisodeRow(
        episode_id=data["episode_id"],
        game_version=data["game_version"],
        policy_version=data["policy_version"],
        seed=data["seed"],
        seats=tuple(seats),
        opponents=tuple(opponents),
        metrics={name: float(value) for name, value in metrics.items()},
        status=status,
        reason=data.get("reason"),
    )


def candidate_version(record: dict) -> str:
    """``methods.candidate``, or the one Version built for the Experiment when it is empty.

    The store lets the candidate stay empty at ``start`` (it is built during the Task) and
    locks the methods from then on, so the Version built for the Experiment
    (``version.create`` with this ``experiment_id``; ``versions_tested`` role ``built``) is the
    candidate.
    """
    if record["methods"].get("candidate"):
        return record["methods"]["candidate"]
    built = [v["id"] for v in record["versions_tested"] if v["role"] == "built"]
    if len(built) == 1:
        return built[0]
    raise ToolError(
        f"{record['id']} has no candidate: methods.candidate is empty and {len(built)} Versions"
        f" were built for it ({', '.join(built) or 'none'}); the computed verdict needs one"
        " candidate, so close with an explicit verdict"
    )


def design_from(
    methods: dict, candidate: str, runs: list[dict], rows: list[EpisodeRow]
) -> EvaluationDesign:
    """The locked methods as an ``EvaluationDesign``.

    Built directly rather than through ``from_dict`` because ``validate`` refuses fewer seeds
    than ``n_min``, and at close time that case is exactly what the decision rule must report
    as ``n_short`` rather than refuse. The design requires a ``k``, which the decision rule does
    not read, so a placeholder stands in.
    """
    first = rows[0]
    seeds = {run["seed"] for run in runs if run.get("seed") is not None}
    seeds |= {row.seed for row in rows}
    rule = methods.get("decision_rule") or {}
    return EvaluationDesign(
        game_version=first.game_version,
        baseline_version=methods["baseline"],
        candidate_version=candidate,
        seats=first.seats,
        opponents=first.opponents,
        seeds=tuple(sorted(seeds)),
        metrics=tuple(
            MetricSpec(
                name=m["name"],
                direction=m["direction"],
                role=m["role"],
                min_effect=m.get("min_effect"),
                tolerance=m.get("tolerance"),
            )
            for m in methods["metrics"]
        ),
        k=1.0,
        delta={},
        n_min=rule["n_min"],
    )


def one_row_per_seed(rows: list[EpisodeRow]) -> list[EpisodeRow]:
    """Average several rows of one policy, game version, seed and opponents into one row.

    Repeated episodes on one seed would otherwise pair as several pairs for one seed. The rows
    are our seat's only (``rows_by_seat``): averaging in other seats would mix opponents in.
    """
    groups: dict[tuple, list[EpisodeRow]] = {}
    for row in sorted(rows, key=lambda r: r.episode_id):
        key = (row.policy_version, row.game_version, row.seed, row.opponents)
        groups.setdefault(key, []).append(row)
    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        first = group[0]
        names = set(first.metrics)
        for row in group[1:]:
            names &= set(row.metrics)
        merged.append(
            EpisodeRow(
                episode_id=first.episode_id,
                game_version=first.game_version,
                policy_version=first.policy_version,
                seed=first.seed,
                seats=tuple(sorted({seat for row in group for seat in row.seats})),
                opponents=first.opponents,
                metrics={
                    name: statistics.fmean(row.metrics[name] for row in group)
                    for name in sorted(names)
                },
                status="completed",
            )
        )
    return merged


def measurements_from(
    design: EvaluationDesign,
    pairs: list[tuple[EpisodeRow, EpisodeRow]],
    verdict: decision_rule.Verdict,
) -> list[dict]:
    """One measurement per metric: the store's ``{metric, baseline, candidate, n, effect}`` plus
    the decision rule's ``mean_signed_diff``, ``ci``, ``threshold`` and ``passed``."""
    measurements = []
    for spec in design.metrics:
        entry = verdict.per_metric[spec.name][spec.role]
        measurements.append(
            {
                "metric": spec.name,
                "baseline": statistics.fmean(b.metrics[spec.name] for b, _ in pairs),
                "candidate": statistics.fmean(c.metrics[spec.name] for _, c in pairs),
                "n": len(pairs),
                "effect": entry["mean_signed_diff"],
                **entry,
            }
        )
    return measurements


def summarise(verdict: decision_rule.Verdict, measurements: list[dict], pairs: int) -> str:
    """A one-line interpretation when the caller gives none."""
    effects = ", ".join(f"{m['metric']} {m['effect']:+.4g}" for m in measurements)
    line = f"{verdict.verdict} by the decision rule over {pairs} pairs: {effects}"
    if verdict.reason_codes:
        line += f" ({', '.join(verdict.reason_codes)})"
    return line


def dropped(report: ingest.IngestReport, other_seats: int = 0) -> dict:
    return {
        "duplicates": report.duplicates,
        "failed": report.failed,
        "wrong_version": report.wrong_version,
        "missing_metrics": report.missing_metrics,
        "other_seats": other_seats,
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# --- the spec ---------------------------------------------------------------------------------

SPEC = tool(
    ToolSpec(
        name="experiment",
        description=(
            "Experiments: a comparison of Versions whose hypothesis and methods lock at start "
            "and end in a verdict or are abandoned. xp.request records the runs; close applies "
            "the pre-registered decision rule to the stored rows, or records an explicit verdict "
            "with its reason."
        ),
        handler=handle,
        executor="workspace",
        actions=(
            ActionSpec(
                "create",
                "Propose an Experiment; the design may still change until start.",
                {
                    **DESIGN,
                    "goal": a.string("The Goal it serves, such as G-3."),
                    "task": a.string("The Task that runs it, such as T-12."),
                },
                ("summary", "hypothesis", "methods"),
                own_tool=True,
            ),
            ActionSpec(
                "read",
                "Read an Experiment with its runs, or one section of it.",
                {
                    "experiment": EXPERIMENT,
                    "section": a.string(
                        "Only this section; results carries the runs.",
                        enum=list(experiments.SECTIONS),
                    ),
                },
                ("experiment",),
            ),
            ActionSpec(
                "search",
                "Full-text search over summaries, designs, results and tags, or omit query to "
                "list Experiments, newest first; tags and goal narrow either.",
                {
                    "query": a.string("Words to look for; all must match. Omit to list."),
                    "tags": a.strings("Keep only Experiments carrying all of these tags."),
                    "goal": a.string("Keep only Experiments created for this Goal, such as G-2."),
                    "limit": a.integer("At most this many results (default 20)."),
                },
            ),
            ActionSpec(
                "update",
                "Change a proposed Experiment's design; each section or list given replaces the "
                "stored one whole.",
                {"experiment": EXPERIMENT, **DESIGN_UPDATE},
                ("experiment",),
                example_fields=("summary",),
            ),
            ActionSpec(
                "start",
                "Lock the design and run; refused while any part is missing, naming each.",
                {"experiment": EXPERIMENT},
                ("experiment",),
            ),
            ActionSpec(
                "update_run",
                "Progress on a run: platform state, episodes done, result rows, validity. "
                "An invalid run's rows do not count at close.",
                {
                    "run": a.integer(
                        "The run's id as experiment.read lists it under runs; ids count across "
                        "all Experiments, so it is not the run's position."
                    ),
                    "state": a.string("The platform's state of the request."),
                    "episodes_completed": a.integer("Episodes finished so far."),
                    "results": RESULTS,
                    "validity": a.string("valid, or 'invalid: <reason>'."),
                },
                ("run",),
                example_fields=("validity",),
                # A chat keeps the schema it started with until the next release's refresh.
                hidden_params={"experiment": EXPERIMENT},
            ),
            ActionSpec(
                "close",
                "Record the verdict: computed from the stored rows by the decision rule, or "
                "explicit when verdict is given. Either needs at least one run from xp.request.",
                {
                    "experiment": EXPERIMENT,
                    "verdict": a.string(
                        "An explicit verdict; omit to apply the decision rule to the rows.",
                        enum=list(experiments.VERDICTS),
                    ),
                    "measurements": a.objects(
                        "Required with an explicit verdict: one for every primary metric.",
                        MEASUREMENT,
                        min_items=1,
                    ),
                    "interpretation": a.string(
                        "What the result means; required with an explicit verdict."
                    ),
                    "reason_codes": a.strings(
                        "The decision rule's codes; with an explicit verdict, this or reason."
                    ),
                    "reason": a.string(
                        "Why the verdict is given by hand; with an explicit verdict, this or "
                        "reason_codes."
                    ),
                },
                ("experiment",),
            ),
            ActionSpec(
                "abandon",
                "Stop before a verdict; the record stays and a replacement can cite it.",
                {"experiment": EXPERIMENT, "reason": a.string("Why it is abandoned.")},
                ("experiment", "reason"),
            ),
        ),
    )
)
