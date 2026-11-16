"""Experiments: a comparison of Versions whose design locks at start and ends in a verdict (B.8).

Lifecycle: ``proposed`` (the design may still change) -> ``running`` (``start`` hashes the
hypothesis and methods into ``design_sha256``; from then on neither changes) -> ``confirmed``,
``refuted`` or ``inconclusive`` (``close`` records the measurements read against the decision
rule), or ``abandoned`` with a reason at any point before a verdict. Experiments are never
deleted: a design that has to change is abandoned and the replacement lists it in
``related_experiments``. ``closed`` is when the Experiment ended, by verdict or abandonment.

``create`` and ``update`` check the shape of whatever design is given (types, enumerations,
references that must exist, ``if_true`` differing from ``if_false``, at least one primary
metric), so a proposed Experiment may still be incomplete; ``start`` requires the complete
design and names every missing part. Runs live in ``experiment_runs`` and are written as soon
as the platform accepts them (B.13). ``results`` holds the measurements, the interpretation,
the verdict and the decision rule's reason codes.

Task-scoped writes (``add_run``, ``update_run``, ``close``) need the Task's lease holder when
the Experiment has a Task (A2.3); the check runs inside the write's transaction.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

from research_core.store._common import (
    display_id,
    json_field,
    parse_display_id,
    parse_json,
    require,
    word_count,
)
from research_core.store.errors import NotFound, StoreError
from research_core.store.registry import EXPERIMENT, RECORDS, record_from_row

if TYPE_CHECKING:
    from collections.abc import Sequence

    from research_core.store.actors import Actor
    from research_core.store.db import Store

HYPOTHESIS_KEYS = ("statement", "if_true", "if_false", "motivation")
METHODS_KEYS = ("baseline", "candidate", "procedure", "metrics", "decision_rule")
# ``candidate`` may stay empty until the candidate Version is built.
METHODS_REQUIRED_TO_START = ("baseline", "procedure", "metrics", "decision_rule")
METRIC_KEYS = ("name", "direction", "role", "min_effect", "tolerance")
METRIC_DIRECTIONS = ("up", "down")
METRIC_ROLES = ("primary", "guardrail", "performance")
# ``k`` is no longer offered and nothing reads it; Experiments stored earlier may hold one.
DECISION_RULE_KEYS = ("text", "n_min", "k")
MEASUREMENT_KEYS = ("metric", "baseline", "candidate", "n", "effect")
VERDICTS = ("confirmed", "refuted", "inconclusive")
# A run's platform request states that never change again.
REQUEST_TERMINAL_STATES = ("completed", "failed", "cancelled")
# The validity xp.status gives a run whose episodes are fine but whose evaluate failed.
EVALUATE_FAILED = "invalid: evaluate"
SECTIONS = ("hypothesis", "methods", "results")
SUMMARY_MAX_WORDS = 40

_FTS_WORD = re.compile(r"\w+")
_VALIDITY = re.compile(r"^(valid|invalid: \S.*)$")


# --- writes ------------------------------------------------------------------------------


def create(
    store: Store,
    actor: Actor,
    *,
    summary: str,
    goal_id: int | str | None = None,
    task_id: int | str | None = None,
    tags: Any = (),
    hypothesis: Any,
    methods: Any,
    related_claims: Any = (),
    related_experiments: Any = (),
) -> dict[str, Any]:
    """A proposed Experiment. Event ``experiment.created``."""
    problems: list[str] = []
    _check_summary(summary, problems)
    hypothesis = _check_hypothesis(hypothesis, problems)
    methods = _check_methods(store, methods, problems)
    tags = _check_tags(tags, problems)
    if goal_id is not None:
        goal_id = _require_record(store, goal_id, kind="goal", field="goal_id", problems=problems)
    if task_id is not None:
        task_id = _require_record(store, task_id, kind="task", field="task_id", problems=problems)
        if goal_id is None and task_id is not None:
            # goal.read counts by this column, and its Task says which Goal this serves.
            goal_id = store.one("SELECT goal_id FROM tasks WHERE id = ?", (task_id,))["goal_id"]
    related_claims = _check_related(
        store, related_claims, kind="claim", field="related_claims", problems=problems
    )
    related_experiments = _check_related(
        store,
        related_experiments,
        kind="experiment",
        field="related_experiments",
        problems=problems,
    )
    if problems:
        raise StoreError(problems)
    with store.tx(actor) as tx:
        cursor = tx.execute(
            "INSERT INTO experiments(summary, goal_id, task_id, tags, status, hypothesis, methods,"
            " results, related_claims, related_experiments, created, created_by)"
            " VALUES (?, ?, ?, ?, 'proposed', ?, ?, '{}', ?, ?, ?, ?)",
            (
                summary.strip(),
                goal_id,
                task_id,
                json_field(tags),
                json_field(hypothesis),
                json_field(methods),
                json_field(related_claims),
                json_field(related_experiments),
                tx.at,
                str(actor),
            ),
        )
        experiment_id = cursor.lastrowid
        tx.event(
            "experiment.created",
            display_id("X", experiment_id),
            {"summary": summary.strip(), "goal_id": goal_id, "task_id": task_id},
        )
    return read(store, experiment_id)


def update(
    store: Store,
    actor: Actor,
    experiment_id: int | str,
    *,
    summary: str | None = None,
    hypothesis: Any = None,
    methods: Any = None,
    tags: Any = None,
    related_claims: Any = None,
    related_experiments: Any = None,
) -> dict[str, Any]:
    """Replace the given parts of a proposed Experiment's design. Event ``experiment.updated``.

    A section given here replaces the stored one as a whole; the result carries the record, so
    a caller that wants one more key resends the section with it.
    """
    row = _fetch(store, experiment_id)
    xid = display_id("X", row["id"])
    problems: list[str] = []
    if row["status"] == "running":
        problems.append(
            f"{xid} is running; its design is locked (abandon it and create a new Experiment"
            " that lists it in related_experiments)"
        )
    elif row["status"] != "proposed":
        problems.append(f"{xid} is {row['status']}; only a proposed Experiment can be updated")
    changes: dict[str, Any] = {}
    if summary is not None:
        _check_summary(summary, problems)
        changes["summary"] = summary.strip() if isinstance(summary, str) else summary
    if hypothesis is not None:
        changes["hypothesis"] = json_field(_check_hypothesis(hypothesis, problems))
    if methods is not None:
        changes["methods"] = json_field(_check_methods(store, methods, problems))
    if tags is not None:
        changes["tags"] = json_field(_check_tags(tags, problems))
    if related_claims is not None:
        changes["related_claims"] = json_field(
            _check_related(
                store, related_claims, kind="claim", field="related_claims", problems=problems
            )
        )
    if related_experiments is not None:
        changes["related_experiments"] = json_field(
            _check_related(
                store,
                related_experiments,
                kind="experiment",
                field="related_experiments",
                problems=problems,
            )
        )
    if not changes:
        problems.append("nothing to update")
    if problems:
        raise StoreError(problems)
    assignments = ", ".join(f"{name} = ?" for name in changes)
    with store.tx(actor) as tx:
        tx.execute(
            f"UPDATE experiments SET {assignments} WHERE id = ?", (*changes.values(), row["id"])
        )
        tx.event("experiment.updated", xid, {"fields": sorted(changes)})
    return read(store, row["id"])


def start(store: Store, actor: Actor, experiment_id: int | str) -> dict[str, Any]:
    """Lock the design and run: ``design_sha256`` is set, status ``running``.

    Event ``experiment.started``. Refused unless the Experiment is proposed and its hypothesis
    and methods are complete; every missing part is named.
    """
    row = _fetch(store, experiment_id)
    xid = display_id("X", row["id"])
    problems: list[str] = []
    require(
        problems,
        row["status"] == "proposed",
        f"{xid} is {row['status']}; only a proposed Experiment can start",
    )
    hypothesis = parse_json(row["hypothesis"], {})
    methods = parse_json(row["methods"], {})
    problems.extend(missing_design_parts(hypothesis, methods))
    if problems:
        raise StoreError(problems)
    sha = design_sha256(hypothesis, methods)
    with store.tx(actor) as tx:
        tx.execute(
            "UPDATE experiments SET status = 'running', design_sha256 = ? WHERE id = ?",
            (sha, row["id"]),
        )
        tx.event("experiment.started", xid, {"design_sha256": sha})
    return read(store, row["id"])


def add_run(
    store: Store,
    actor: Actor,
    experiment_id: int | str,
    *,
    kind: str,
    platform_request_id: str,
    arm: str,
    seed: int | None = None,
    episodes_total: int | None = None,
) -> dict[str, Any]:
    """Record an evaluation run the moment the platform accepts it (B.13). Returns the run.

    Only while the Experiment is running. Event ``experiment.run``.
    """
    row = _fetch(store, experiment_id)
    xid = display_id("X", row["id"])
    problems: list[str] = []
    _require_running(row, problems, "runs are added only while it is running")
    require(problems, _is_text(kind), "kind is required")
    require(problems, _is_text(platform_request_id), "platform_request_id is required")
    require(problems, _is_text(arm), "arm is required")
    require(problems, seed is None or _is_int(seed), "seed must be an integer")
    require(
        problems,
        episodes_total is None or (_is_int(episodes_total) and episodes_total >= 1),
        "episodes_total must be a positive integer",
    )
    if problems:
        raise StoreError(problems)
    with store.tx(actor) as tx:
        _require_task_holder(store, row["task_id"], actor)
        cursor = tx.execute(
            "INSERT INTO experiment_runs(experiment_id, kind, platform_request_id, arm, seed,"
            " validity, episodes_total, created, updated)"
            " VALUES (?, ?, ?, ?, ?, 'valid', ?, ?, ?)",
            (
                row["id"],
                kind.strip(),
                platform_request_id.strip(),
                arm.strip(),
                seed,
                episodes_total,
                tx.at,
                tx.at,
            ),
        )
        run_id = cursor.lastrowid
        tx.event(
            "experiment.run",
            xid,
            {
                "run_id": run_id,
                "action": "added",
                "kind": kind.strip(),
                "platform_request_id": platform_request_id.strip(),
                "arm": arm.strip(),
                "seed": seed,
            },
        )
    return _run_record(store, run_id)


def open_requests(store: Store, task_id: int) -> list[str]:
    """The platform request ids of the Task's Experiments' runs that are not yet terminal."""
    rows = store.q(
        "SELECT r.platform_request_id, r.state FROM experiment_runs r"
        " JOIN experiments e ON e.id = r.experiment_id WHERE e.task_id = ? ORDER BY r.id",
        (task_id,),
    )
    return [row[0] for row in rows if row[1] not in REQUEST_TERMINAL_STATES]


def unsettled(run: dict[str, Any]) -> str | None:
    """Why a run's rows may still change, or None once they are final.

    A request still running, a finished request ``xp.status`` has not evaluated, and a failed
    evaluate (waiting for its repair) are unsettled. A run marked invalid for any other reason is
    settled: its rows never count.
    """
    if run["state"] not in REQUEST_TERMINAL_STATES:
        return f"request {run['state'] or 'not started'}"
    if run["validity"].startswith(EVALUATE_FAILED):
        return "evaluate failed; after the repair xp.status evaluates it again"
    if run["validity"] == "valid" and "rows" not in (run.get("results") or {}):
        return "finished but not evaluated; xp.status stores its rows"
    return None


def recorded_requests(store: Store, request_ids: list[str]) -> set[str]:
    """Which of ``request_ids`` some Experiment run records."""
    marks = ", ".join("?" for _ in request_ids)
    rows = store.q(
        f"SELECT platform_request_id FROM experiment_runs WHERE platform_request_id IN ({marks})",
        tuple(request_ids),
    )
    return {row[0] for row in rows}


def require_run_of(store: Store, run_id: int, experiment_id: int) -> None:
    """Refuse a run that does not exist or belongs to another Experiment than the caller named,
    naming that Experiment's run ids."""
    run = store.one("SELECT experiment_id FROM experiment_runs WHERE id = ?", (run_id,))
    if run is None or run["experiment_id"] != experiment_id:
        # Run ids count across all Experiments; models send a run's position (2026-11).
        ids = [
            row["id"]
            for row in store.q(
                "SELECT id FROM experiment_runs WHERE experiment_id = ? ORDER BY id",
                (experiment_id,),
            )
        ]
        where = (
            "does not exist"
            if run is None
            else f"belongs to {display_id('X', run['experiment_id'])}, not X-{experiment_id}"
        )
        raise StoreError(
            [
                f"run {run_id} {where}; X-{experiment_id}'s run ids are"
                f" {', '.join(map(str, ids)) or 'none yet'} (experiment.read lists them)"
            ]
        )


def update_run(
    store: Store,
    actor: Actor,
    run_id: int,
    *,
    state: str | None = None,
    episodes_completed: int | None = None,
    results: dict[str, Any] | None = None,
    validity: str | None = None,
) -> dict[str, Any]:
    """Progress on a run: its platform state, episodes done, numbers, validity. Returns the run.

    ``results`` keys replace the stored ones; keys not given are kept.

    ``validity`` is ``valid`` or ``invalid: <reason>``. Only while the Experiment is running.
    Event ``experiment.run``.
    """
    run = store.one("SELECT * FROM experiment_runs WHERE id = ?", (run_id,))
    if run is None:
        raise NotFound(
            [
                f"run {run_id} does not exist; give a run's id as experiment.read lists it under"
                " runs (ids count across all Experiments, not from 1 in each)"
            ]
        )
    row = _fetch(store, run["experiment_id"])
    xid = display_id("X", row["id"])
    problems: list[str] = []
    _require_running(row, problems, "runs change only while it is running")
    changes: dict[str, Any] = {}
    if state is not None:
        require(problems, _is_text(state), "state must be text")
        changes["state"] = state
    if episodes_completed is not None:
        require(
            problems,
            _is_int(episodes_completed) and episodes_completed >= 0,
            "episodes_completed must be a whole number",
        )
        changes["episodes_completed"] = episodes_completed
    if results is not None:
        if require(problems, isinstance(results, dict), "results must be an object"):
            # Merged by key: xp.request keeps the Version a run plays here, and xp.status needs
            # it after a caller stores rows (a replaced dict crashed xp.status, 2026-11-09).
            changes["results"] = json_field({**parse_json(run["results"], {}), **results})
    if validity is not None:
        require(
            problems,
            isinstance(validity, str) and _VALIDITY.match(validity),
            "validity must be valid or invalid: <reason>",
        )
        changes["validity"] = validity
    if not changes:
        problems.append("nothing to update")
    if problems:
        raise StoreError(problems)
    assignments = ", ".join(f"{name} = ?" for name in changes)
    with store.tx(actor) as tx:
        _require_task_holder(store, row["task_id"], actor)
        tx.execute(
            f"UPDATE experiment_runs SET {assignments}, updated = ? WHERE id = ?",
            (*changes.values(), tx.at, run_id),
        )
        # The holder can evaluate a batch before the feed watcher attaches.
        if run["kind"] == "xp" and actor.is_session:
            for watch in tx.q(
                "SELECT w.id,w.request_ids FROM task_xp_waits w JOIN tasks t ON t.id=w.task_id"
                " WHERE w.experiment=? AND t.held_by=?",
                (xid, actor.id),
            ):
                watched_runs = tx.q(
                    "SELECT r.* FROM experiment_runs r JOIN json_each(?) ids"
                    " ON r.platform_request_id=ids.value WHERE r.experiment_id=?",
                    (watch["request_ids"], row["id"]),
                )
                watched_ids = {r["platform_request_id"] for r in watched_runs}
                if watched_ids == set(parse_json(watch["request_ids"])) and all(
                    not unsettled(_run_from_row(r)) for r in watched_runs
                ):
                    tx.execute("DELETE FROM task_xp_waits WHERE id=?", (watch["id"],))
        tx.event(
            "experiment.run",
            xid,
            {"run_id": run_id, "action": "updated", "fields": sorted(changes)},
        )
    return _run_record(store, run_id)


def close(
    store: Store,
    actor: Actor,
    experiment_id: int | str,
    *,
    verdict: str,
    measurements: Any,
    interpretation: str,
    reason_codes: Any,
) -> dict[str, Any]:
    """Record the verdict: status becomes ``confirmed``, ``refuted`` or ``inconclusive``.

    Needs at least one run, a measurement for every primary metric (each
    ``{metric, baseline, candidate, n, effect}``, more keys allowed), an interpretation and the
    decision rule's reason codes. Event ``experiment.closed``.
    """
    row = _fetch(store, experiment_id)
    xid = display_id("X", row["id"])
    problems: list[str] = []
    _require_running(row, problems, "an Experiment closes only while it is running")
    runs = store.one("SELECT COUNT(*) FROM experiment_runs WHERE experiment_id = ?", (row["id"],))[
        0
    ]
    require(
        problems,
        runs >= 1,
        f"{xid} has no runs; an Experiment closes only after at least one run: record runs with"
        f" xp.request, or experiment.abandon {{experiment: {xid}, reason}} when none will run",
    )
    require(problems, verdict in VERDICTS, "verdict must be confirmed, refuted or inconclusive")
    metrics = parse_json(row["methods"], {}).get("metrics") or []
    measurements = _check_measurements(measurements, metrics, problems)
    require(problems, _is_text(interpretation), "interpretation is required")
    require(
        problems,
        isinstance(reason_codes, list) and all(_is_text(code) for code in reason_codes),
        "reason_codes must be a list of codes",
    )
    if problems:
        raise StoreError(problems)
    results = {
        "verdict": verdict,
        "measurements": measurements,
        "interpretation": interpretation.strip(),
        "reason_codes": list(reason_codes),
    }
    with store.tx(actor) as tx:
        _require_task_holder(store, row["task_id"], actor)
        tx.execute(
            "UPDATE experiments SET status = ?, results = ?, closed = ? WHERE id = ?",
            (verdict, json_field(results), tx.at, row["id"]),
        )
        tx.event("experiment.closed", xid, {"verdict": verdict, "reason_codes": list(reason_codes)})
        if row["goal_id"] is not None:
            from research_core.store.goals import resume_for_evidence

            resume_for_evidence(tx, row["goal_id"], xid)
    return read(store, row["id"])


def abandon(store: Store, actor: Actor, experiment_id: int | str, *, reason: str) -> dict[str, Any]:
    """Stop before a verdict; the record stays. Event ``experiment.abandoned``."""
    row = _fetch(store, experiment_id)
    xid = display_id("X", row["id"])
    problems: list[str] = []
    require(
        problems,
        row["status"] in ("proposed", "running"),
        f"{xid} is {row['status']}; only a proposed or running Experiment can be abandoned",
    )
    require(problems, _is_text(reason), "reason is required")
    if problems:
        raise StoreError(problems)
    with store.tx(actor) as tx:
        tx.execute(
            "UPDATE experiments SET status = 'abandoned', abandon_reason = ?, closed = ?"
            " WHERE id = ?",
            (reason.strip(), tx.at, row["id"]),
        )
        tx.event("experiment.abandoned", xid, {"reason": reason.strip()})
    return read(store, row["id"])


# --- reads -------------------------------------------------------------------------------


def read(store: Store, experiment_id: int | str, section: str | None = None) -> dict[str, Any]:
    """The Experiment with its runs, the claims citing it and the Versions it tests.

    With ``section`` (``hypothesis``, ``methods`` or ``results``) only that part comes back,
    with the id and status; the results section carries the runs.
    """
    row = _fetch(store, experiment_id)
    if section is not None and section not in SECTIONS:
        raise StoreError([f"section must be hypothesis, methods or results (not {section!r})"])
    record = record_from_row(EXPERIMENT, row)
    runs = _runs(store, row["id"])
    if section is not None:
        value = record[section]
        if section == "results":
            value = {**value, "runs": runs}
        return {"id": record["id"], "status": record["status"], section: value}
    record["runs"] = runs
    record["cited_by"] = _cited_by(store, record["id"])
    record["versions_tested"] = _versions_tested(store, row["id"], record["methods"])
    return record


def search(
    store: Store,
    query: str | None = None,
    *,
    tags: Sequence[str] | None = None,
    goal_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Full-text search over summary, hypothesis, methods, results and tags; best match first.

    With no query (``None`` or blank) every Experiment, newest first, so ``tags`` or
    ``goal_id`` alone lists them. ``tags`` keeps only Experiments carrying all of them;
    ``goal_id`` only those created for that Goal. A query with no words matches nothing.
    """
    columns = "e.id, e.summary, e.status, e.tags, e.goal_id, e.created, e.closed"
    if not (query or "").strip():
        rows = store.q(f"SELECT {columns} FROM experiments e ORDER BY e.id DESC")
    else:
        match = fts_match(query or "")
        if match is None:
            return []
        rows = store.q(
            f"SELECT {columns} FROM experiments_fts f JOIN experiments e ON e.id = f.rowid"
            " WHERE experiments_fts MATCH ? ORDER BY rank",
            (match,),
        )
    wanted = set(tags or ())
    results: list[dict[str, Any]] = []
    for row in rows:
        if len(results) >= limit:
            break
        row_tags = parse_json(row["tags"], [])
        if not wanted <= set(row_tags):
            continue
        if goal_id is not None and row["goal_id"] != goal_id:
            continue
        results.append(
            {
                "id": display_id("X", row["id"]),
                "summary": row["summary"],
                "status": row["status"],
                "tags": row_tags,
                "created": row["created"],
                "closed": row["closed"],
            }
        )
    return results


def fts_match(query: str) -> str | None:
    """An FTS5 MATCH expression from free text: every word quoted, all words required.

    Quoting keeps operators and punctuation in the user's text (``OR``, ``*``, ``"``, ``(``)
    from being read as query syntax. ``None`` when the text has no words.
    """
    words = _FTS_WORD.findall(query or "")
    if not words:
        return None
    return " ".join(f'"{word}"' for word in words)


def design_sha256(hypothesis: dict[str, Any], methods: dict[str, Any]) -> str:
    """The lock: sha256 of the canonical JSON (sorted keys, compact) of both sections."""
    canonical = json_field({"hypothesis": hypothesis, "methods": methods})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def missing_design_parts(hypothesis: dict[str, Any], methods: dict[str, Any]) -> list[str]:
    """What ``start`` still needs: each empty hypothesis or methods part, named."""
    missing = [f"hypothesis.{key} is missing" for key in HYPOTHESIS_KEYS if not hypothesis.get(key)]
    missing += [
        f"methods.{key} is missing" for key in METHODS_REQUIRED_TO_START if not methods.get(key)
    ]
    return missing


# --- validation --------------------------------------------------------------------------


def _check_summary(summary: Any, problems: list[str]) -> None:
    if not require(problems, _is_text(summary), "summary is required"):
        return
    words = word_count(summary)
    require(
        problems,
        words <= SUMMARY_MAX_WORDS,
        f"summary must be at most {SUMMARY_MAX_WORDS} words ({words} given)",
    )


def _check_tags(tags: Any, problems: list[str]) -> list[str]:
    if isinstance(tags, str) or not isinstance(tags, list | tuple):
        problems.append("tags must be a list of words")
        return []
    if not all(_is_text(tag) for tag in tags):
        problems.append("tags must be a list of words")
        return []
    return [tag.strip() for tag in tags]


def _check_hypothesis(hypothesis: Any, problems: list[str]) -> dict[str, Any]:
    if not isinstance(hypothesis, dict):
        problems.append(
            "hypothesis must be an object with statement, if_true, if_false and motivation"
        )
        return {}
    _check_unknown_keys(hypothesis, HYPOTHESIS_KEYS, "hypothesis", "a hypothesis field", problems)
    for key in HYPOTHESIS_KEYS:
        if key in hypothesis:
            require(problems, _is_text(hypothesis[key]), f"hypothesis.{key} must be text")
    if_true, if_false = hypothesis.get("if_true"), hypothesis.get("if_false")
    if _is_text(if_true) and _is_text(if_false) and if_true.strip() == if_false.strip():
        problems.append(
            "hypothesis.if_true and if_false must differ, or the Experiment cannot tell us anything"
        )
    return dict(hypothesis)


def _check_methods(store: Store, methods: Any, problems: list[str]) -> dict[str, Any]:
    """Validate the methods given and normalise its Version references to ``V-<n>``."""
    if not isinstance(methods, dict):
        problems.append(
            "methods must be an object with baseline, candidate, procedure, metrics and"
            " decision_rule"
        )
        return {}
    _check_unknown_keys(methods, METHODS_KEYS, "methods", "a methods field", problems)
    checked = dict(methods)
    baseline = candidate = None
    if "baseline" in methods:
        baseline = _require_record(
            store, methods["baseline"], kind="version", field="methods.baseline", problems=problems
        )
        if baseline is not None:
            checked["baseline"] = display_id("V", baseline)
    if methods.get("candidate") not in (None, ""):  # empty: not built yet, as the schema says
        candidate = _require_record(
            store,
            methods["candidate"],
            kind="version",
            field="methods.candidate",
            problems=problems,
        )
        if candidate is not None:
            checked["candidate"] = display_id("V", candidate)
    if baseline is not None and candidate == baseline:
        problems.append("methods.candidate must differ from methods.baseline")
    if "procedure" in methods:
        require(problems, _is_text(methods["procedure"]), "methods.procedure must be text")
    if "metrics" in methods:
        _check_metrics(methods["metrics"], problems)
    if "decision_rule" in methods:
        _check_decision_rule(methods["decision_rule"], problems)
    return checked


def _check_metrics(metrics: Any, problems: list[str]) -> None:
    if not isinstance(metrics, list) or not metrics:
        problems.append(
            "methods.metrics must be a non-empty list of"
            " {name, direction, role, min_effect?, tolerance?}"
        )
        return
    primaries = 0
    names: list[str] = []
    for index, metric in enumerate(metrics):
        label = f"methods.metrics[{index}]"
        if not isinstance(metric, dict):
            problems.append(f"{label} must be an object")
            continue
        _check_unknown_keys(metric, METRIC_KEYS, label, "a metric field", problems)
        if require(problems, _is_text(metric.get("name")), f"{label}.name is required"):
            names.append(metric["name"].strip())
        require(
            problems,
            metric.get("direction") in METRIC_DIRECTIONS,
            f"{label}.direction must be up or down",
        )
        require(
            problems,
            metric.get("role") in METRIC_ROLES,
            f"{label}.role must be primary, guardrail or performance",
        )
        for optional in ("min_effect", "tolerance"):
            value = metric.get(optional)
            require(
                problems,
                value is None or _is_number(value),
                f"{label}.{optional} must be a number",
            )
        if metric.get("role") == "primary":
            primaries += 1
    require(problems, primaries >= 1, "methods.metrics needs at least one primary metric")
    for name in sorted({name for name in names if names.count(name) > 1}):
        problems.append(f"methods.metrics names must be unique ({name} repeats)")


def _check_decision_rule(rule: Any, problems: list[str]) -> None:
    if not isinstance(rule, dict):
        problems.append("methods.decision_rule must be an object with text and n_min")
        return
    _check_unknown_keys(
        rule, DECISION_RULE_KEYS, "methods.decision_rule", "a decision rule field", problems
    )
    require(problems, _is_text(rule.get("text")), "methods.decision_rule.text is required")
    n_min = rule.get("n_min")
    require(
        problems,
        _is_int(n_min) and n_min >= 1,
        "methods.decision_rule.n_min must be a positive integer",
    )


def _check_measurements(
    measurements: Any, metrics: list[dict[str, Any]], problems: list[str]
) -> list[dict[str, Any]]:
    """Each measurement names a declared metric with numbers; every primary metric has one."""
    if not isinstance(measurements, list) or not measurements:
        problems.append(
            "measurements must be a non-empty list of {metric, baseline, candidate, n, effect}"
        )
        return []
    declared = sorted(
        {m["name"] for m in metrics if isinstance(m, dict) and _is_text(m.get("name"))}
    )
    measured: set[str] = set()
    for index, measurement in enumerate(measurements):
        label = f"measurements[{index}]"
        if not isinstance(measurement, dict):
            problems.append(f"{label} must be an object")
            continue
        metric = measurement.get("metric")
        if _is_text(metric) and metric in declared:
            measured.add(metric)
        else:
            problems.append(
                f"{label}.metric {metric!r} is not one of the Experiment's metrics"
                f" ({', '.join(declared)})"
            )
        for key in ("baseline", "candidate", "n", "effect"):
            if key not in measurement:
                problems.append(f"{label}.{key} is missing")
            elif key == "n":
                n = measurement["n"]
                require(problems, _is_int(n) and n >= 1, f"{label}.n must be a positive integer")
            else:
                require(problems, _is_number(measurement[key]), f"{label}.{key} must be a number")
    for metric in metrics:
        if isinstance(metric, dict) and metric.get("role") == "primary":
            name = metric.get("name")
            require(problems, name in measured, f"primary metric {name} has no measurement")
    return [dict(m) for m in measurements if isinstance(m, dict)]


def _check_related(
    store: Store, values: Any, *, kind: str, field: str, problems: list[str]
) -> list[str]:
    """A list of references to records of ``kind`` that exist, normalised to display ids."""
    spec = RECORDS[kind]
    if isinstance(values, str) or not isinstance(values, list | tuple):
        problems.append(f"{field} must be a list of {spec.name.capitalize()} references")
        return []
    normalised: list[str] = []
    for value in values:
        number = _require_record(store, value, kind=kind, field=field, problems=problems)
        if number is not None:
            normalised.append(display_id(spec.prefix, number))
    return normalised


def _check_unknown_keys(
    given: dict[str, Any], allowed: tuple[str, ...], label: str, what: str, problems: list[str]
) -> None:
    for key in sorted(set(given) - set(allowed)):
        problems.append(f"{label}.{key} is not {what} ({', '.join(allowed)})")


def _require_record(
    store: Store, value: Any, *, kind: str, field: str, problems: list[str]
) -> int | None:
    """The number of an existing record of ``kind`` from ``3`` or ``"X-3"``; else a problem."""
    spec = RECORDS[kind]
    number = _ref_number(value, kind)
    if number is None:
        problems.append(
            f"{field}: {value!r} is not a {spec.name.capitalize()} reference ({spec.prefix}-<n>)"
        )
        return None
    if store.one(f"SELECT 1 FROM {spec.table} WHERE id = ?", (number,)) is None:
        problems.append(f"{field}: {display_id(spec.prefix, number)} does not exist")
        return None
    return number


def _ref_number(value: Any, kind: str) -> int | None:
    if _is_int(value):
        return value if value >= 1 else None
    if isinstance(value, str):
        try:
            parsed_kind, number = parse_display_id(value.strip())
        except ValueError:
            return None
        return number if parsed_kind == kind else None
    return None


def _require_running(row: Any, problems: list[str], clause: str) -> None:
    require(
        problems,
        row["status"] == "running",
        f"{display_id('X', row['id'])} is {row['status']}; {clause}",
    )


def _require_task_holder(store: Store, task_id: int | None, actor: Actor) -> None:
    """A2.3: a write scoped to a Task needs the Task's lease holder at the current epoch.

    Called inside the write's transaction, so the lease cannot change between the check and
    the write. ``tasks`` is imported here rather than at module level: it is written alongside
    this module and the lazy import keeps the two modules' import order free.
    """
    if task_id is None:
        return
    from research_core.store import tasks

    tasks.require_holder(store, task_id, actor)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# --- rows --------------------------------------------------------------------------------


def _fetch(store: Store, experiment_id: int | str) -> Any:
    number = _ref_number(experiment_id, "experiment")
    if number is None:
        raise StoreError([f"{experiment_id!r} is not an Experiment reference (X-<n>)"])
    row = store.one("SELECT * FROM experiments WHERE id = ?", (number,))
    if row is None:
        raise NotFound([f"{display_id('X', number)} does not exist"])
    return row


def _runs(store: Store, experiment_id: int) -> list[dict[str, Any]]:
    rows = store.q(
        "SELECT * FROM experiment_runs WHERE experiment_id = ? ORDER BY id", (experiment_id,)
    )
    return [_run_from_row(row) for row in rows]


def _run_record(store: Store, run_id: int) -> dict[str, Any]:
    return _run_from_row(store.one("SELECT * FROM experiment_runs WHERE id = ?", (run_id,)))


def _run_from_row(row: Any) -> dict[str, Any]:
    run = dict(row)
    run["experiment_id"] = display_id("X", run["experiment_id"])
    run["results"] = parse_json(run["results"])
    return run


def _cited_by(store: Store, xid: str) -> list[dict[str, Any]]:
    """Claims whose evidence cites this Experiment, with or without a fragment (``X-5#results``)."""
    rows = store.q(
        "SELECT claim_id, ref, stance, note FROM claim_evidence"
        " WHERE type = 'experiment' AND (ref = ? OR ref LIKE ?) ORDER BY id",
        (xid, f"{xid}#%"),
    )
    return [
        {
            "claim": display_id("C", row["claim_id"]),
            "ref": row["ref"],
            "stance": row["stance"],
            "note": row["note"],
        }
        for row in rows
    ]


def candidate_of(store: Store, experiment_id: int, methods: dict[str, Any]) -> str | None:
    """``methods.candidate``, or when it stayed empty at start (it is built during the Task and the
    methods then lock) the one Version built for the Experiment; None when that is not one."""
    if methods.get("candidate"):
        return methods["candidate"]
    built = [
        v["id"] for v in _versions_tested(store, experiment_id, methods) if v["role"] == "built"
    ]
    return built[0] if len(built) == 1 else None


def _versions_tested(
    store: Store, experiment_id: int, methods: dict[str, Any]
) -> list[dict[str, str]]:
    """The baseline, the candidate and any Version built for this Experiment, in that order."""
    tested: list[dict[str, str]] = []
    for role in ("baseline", "candidate"):
        if methods.get(role):
            tested.append({"id": methods[role], "role": role})
    listed = {entry["id"] for entry in tested}
    rows = store.q("SELECT id FROM versions WHERE experiment_id = ? ORDER BY id", (experiment_id,))
    for row in rows:
        vid = display_id("V", row["id"])
        if vid not in listed:
            tested.append({"id": vid, "role": "built"})
    return tested
