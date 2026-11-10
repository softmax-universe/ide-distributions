"""What a read returns in a usability run: the fixture world, in the shape the real tool gives.

A run has no store and no platform, so these are written out, one per ``tool.action``. They agree
with :data:`usability_context.BRIEFING` and with the situation at hand: ``task.read`` of the run's
own Task gives its real title and description, and :attr:`Situation.x5` says how far Experiment
X-5 has got. Keys are checked against the real tools by ``test_usability_reads.py``, which runs
them on a real store and the fake platform. Hermes's own tools follow the result shapes in its
``tools/`` folder at the pinned image; ``read_file`` serves the fake Coworld's mixin
(``tests.fixtures.fakegame_repo``).

Any record a situation can name exists, as in ``write_actions.SYSTEM``: a cautious model is not
sent looking for what the fixture left out. Searches and lists hold only what matches, so they
can come back empty, as they do in a session.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from research_core.stats import decision_rule, ingest
from research_core.store import settings
from research_core.tools import experiment as experiment_tool
from research_core.tools.base import error_payload
from research_core.tools.directive import running_tasks_note
from research_core.tools.settings import describe
from tests.fixtures.fakegame_repo import fixture_files
from tests.model import usability_context
from tests.model.usability_situations import Situation

NOW = "2026-11-04T18:00:00Z"
EARLIER = "2026-11-03T09:00:00Z"
LEAGUE = "league_cogs_main"
DIVISION = "div_main"
PLAYER = "ply_cogs"
WORKTREE = "/opt/data/workspace/cow_cogs/worktrees/task-21"
SESSION = "session:researcher-21"
POLICY_VERSIONS = {"V-7": "pv-7", "V-11": "pv-11", "V-12": "pv-12"}
HIGH_ROSTER = ["pv-1", "pv-2"]
N_MIN = 40

# What the situation says the run has already seen, beyond the fixture's normal state.
TASK_MESSAGES = {
    "r-expert": [
        "First diagnosis of V-12's timeouts failed: the slow ticks do not line up with fights.",
        "Second diagnosis failed: both the path planner and the log writer still fit the logs.",
    ],
}
TOOLCHAIN_DIRECTIVE = {"r-complete-failed", "r-hard-fail-args"}
GOAL_MET = {"o-hard-goal-complete"}
LOW_CREDITS = {"r-wait-credits", "r-hard-wait-credits-args"}
# What a situation's payload says of the mixin, against the fixture's floor of 20 episodes.
MIXIN_FLOOR = {"o-hard-review-reopen": 100}
BROKEN_EVALUATE = {"r-repair"}


def _ref(prefix: str, value: Any) -> str:
    """``X-5`` from ``X-5`` or ``5``: a model may send either, as the real tools accept."""
    text = str(value)
    return f"{prefix}-{text}" if text.isdigit() else text


def _timestamps() -> dict:
    return {"created": EARLIER, "created_by": "session:orchestrator", "updated": NOW}


# --- Goals, directives, settings ------------------------------------------------------------------

GOALS = {
    "G-1": {
        "title": f"Reach the top 3 of {LEAGUE}",
        "end_state": "our policy ranks in the top 3",
        "where_it_stands": "rank 5 after round 12",
        "tasks": {"unclaimed": 2, "proposed": 1, "in-progress": 1, "complete": 6, "abandoned": 0},
        "open": ["T-8", "T-14", "T-15"],
        "claims": ["C-5", "C-2"],
    },
    "G-2": {
        "title": "Understand late-game timeouts",
        "end_state": "the cause of the timeouts is known and recorded",
        "where_it_stands": "",
        "tasks": {"unclaimed": 0, "proposed": 0, "in-progress": 0, "complete": 1, "abandoned": 0},
        "open": [],
        "claims": [],
    },
}


def goal_line(goal_id: str) -> str:
    """The Goal as the Task's start prompt names it."""
    goal = GOALS[goal_id]
    return f"{goal_id} {goal['title']} — end state: {goal['end_state']}"


def _goal_row(goal_id: str) -> dict:
    goal = GOALS.get(goal_id, GOALS["G-2"])
    return {
        "id": goal_id,
        "title": goal["title"],
        "end_state": goal["end_state"],
        "reason": "the user asked for it",
        "status": "active",
        "status_reason": None,
        "judgement": None,
        "tags": [],
        "description": "",
        "where_it_stands": goal["where_it_stands"],
        "updates_for_human": "",
        **_timestamps(),
    }


def goal_read(situation: Situation, args: dict) -> dict:
    goal_id = _ref("G", args["goal"])
    goal = GOALS.get(goal_id, GOALS["G-2"])
    claims = [_claim_summary(_claim(claim_id)) for claim_id in goal["claims"]]
    where = {"where_it_stands": "rank 3 after round 13"} if situation.id in GOAL_MET else {}
    return {
        "record": {
            **_goal_row(goal_id),
            **where,
            "tasks": {**goal["tasks"], "open": goal["open"]},
            "experiments": {
                "proposed": 1,
                "running": 1,
                "confirmed": 1,
                "refuted": 0,
                "inconclusive": 0,
                "abandoned": 0,
            },
            "claims": [
                {key: row[key] for key in ("id", "summary", "status", "freshness")}
                for row in claims
            ],
            "last_progress": NOW,
            "ideation_rounds_without_progress": 0,
            "directives": [row for row in _directives(situation) if row["scope"] == goal_id],
        }
    }


def goal_list(situation: Situation, args: dict) -> dict:
    wanted = args.get("status")
    return {"records": [_goal_row(g) for g in GOALS if wanted in (None, "", "active")]}


def _directives(situation: Situation) -> list[dict]:
    rows = [
        ("W-1", "keep replies short", "workspace"),
        ("D-3", "never touch the opening book", "G-1"),
    ]
    if situation.id in TOOLCHAIN_DIRECTIVE:
        rows.append(("D-4", "no toolchain pin changes under this Goal's Tasks", "G-1"))
    return [
        {
            "id": directive_id,
            "text": text,
            "scope": scope,
            "source": "m-12",
            "status": "active",
            "created": EARLIER,
            "created_by": "session:orchestrator",
            "retired": None,
        }
        for directive_id, text, scope in rows
    ]


def directive_added(situation: Situation, args: dict) -> dict:
    """``directive.add`` with T-8 in progress, as the fixture's briefing has it."""
    record = {
        "id": "W-2",
        "scope": "workspace",
        "text": args.get("text"),
        "status": "active",
        "source": args.get("source"),
        **_timestamps(),
    }
    return {"record": record, "next": running_tasks_note(["T-8"])}


def directive_list(situation: Situation, args: dict) -> dict:
    if args.get("status") not in (None, "", "active"):
        return {"records": []}
    scope = args.get("scope")
    rows = _directives(situation)
    return {"records": [row for row in rows if scope in (None, "", row["scope"])]}


def _setting_value(situation: Situation, key: str) -> Any:
    """A wake's payload may name a setting's value; else the default, as a fresh workspace has."""
    return (situation.payload or {}).get(key, settings.spec(key).default)


def settings_read(situation: Situation, args: dict) -> dict:
    """The real settings, with this situation's values."""
    key = args.get("key")
    if key in settings.SETTINGS:
        return {"record": describe(settings.spec(key), _setting_value(situation, key))}
    if key:
        problem = f"unknown setting {key!r}; known settings: {', '.join(settings.SETTINGS)}"
        return error_payload([problem], {"tool": "settings", "action": "read"})
    return {
        "records": [
            describe(row, _setting_value(situation, row.key)) for row in settings.SETTINGS.values()
        ]
    }


# --- Tasks ----------------------------------------------------------------------------------------

OTHER_TASKS = {
    "T-8": ("Study the opening", "improvement", "in-progress"),
    "T-9": ("Replay survey of early fights", "research", "complete"),
    "T-14": ("Study late fights", "research", "proposed"),
    "T-15": ("Fold in the opening analysis", "research", "unclaimed"),
}


def _task_row(task_id: str, **overrides: Any) -> dict:
    title, kind, status = OTHER_TASKS.get(task_id, ("Earlier work on G-1", "research", "complete"))
    return {
        "id": task_id,
        "goal_id": 1,
        "type": kind,
        "title": title,
        "description": f"{title}, and report what was found.",
        "reasoning": "",
        "for_human": "",
        "end_condition": None,
        "end_detail": None,
        "experiment_id": None,
        "status": status,
        "priority": "normal",
        "proposed_by": None,
        "approved_by": "session:orchestrator",
        "approved_at": EARLIER,
        "outcome": None,
        "result": None,
        "failure": None,
        "last_error_seen": None,
        "waiting_on": None,
        "held_by": None,
        "lease_epoch": 0,
        "lease_renewed": None,
        "run_id": None,
        "worktree": None,
        "branch": None,
        "ideation_round": None,
        "checklist": None,
        "merge": None,
        "completed_commit": None,
        "capability": None,
        **_timestamps(),
        "depends_on": [],
        "sessions": [],
        "messages": [],
        "stuck": False,
        **overrides,
    }


def _message(number: int, text: str) -> dict:
    return {
        "id": number,
        "task_id": 21,
        "direction": "to_orchestrator",
        "kind": "message",
        "text": text,
        "created": EARLIER,
        "created_by": SESSION,
        "delivered_at": None,
    }


REVIEWED_RESULT = "X-5 reached its verdict and the claims record it; see the checklist."


def _reviewed_checklist(situation: Situation) -> dict:
    """What a finished improvement Task holds (``task_types/improvement.md``), by its verdict."""
    confirmed = situation.x5 == "confirmed"
    return {
        "claims": ["C-30"],
        "experiment": "X-5",
        "versions": ["V-12"],
        "submission_candidate": "yes" if confirmed else "no",
        "submission_reason": f"X-5 {X5_STATUS[situation.x5]} against the champion",
        "merge_reason": "the change is the point" if confirmed else "the result is a finding",
    }


def own_task(situation: Situation) -> dict:
    """The run's own Task, as ``task.read`` and ``task.claim`` return it. The ``wait`` a situation
    has already made shows on it."""
    claimed = {
        "status": "in-progress",
        "held_by": "researcher-21",
        "lease_epoch": 1,
        "lease_renewed": NOW,
        "run_id": "run-21",
        "worktree": WORKTREE,
        "branch": "task/21",
        "sessions": [
            {"session_id": "researcher-21", "kind": "task", "epoch": 1, "started": NOW},
        ],
    }
    waiting = next(
        (prior.result for prior in situation.prior if prior.args.get("action") == "wait"), None
    )
    return _task_row(
        usability_context.TASK_ID,
        goal_id=int(situation.goal.split("-")[1]),
        title=situation.title,
        type=situation.task_type,
        description=situation.text,
        experiment_id=5
        if "X-5" in situation.text and situation.task_type == "improvement"
        else None,
        waiting_on=json.loads(waiting)["record"]["waiting_on"] if waiting else None,
        messages=[
            _message(number, text)
            for number, text in enumerate(TASK_MESSAGES.get(situation.id, ()), start=1)
        ],
        **(claimed if situation.claimed else {"status": "unclaimed"}),
    )


def claim_result(situation: Situation, instructions: str) -> dict:
    """What the real ``task.claim`` returns (``tools/task.py``, ``handle_claim``)."""
    return {
        "ok": True,
        "record": own_task(situation),
        "worktree": WORKTREE,
        "branch": "task/21",
        "instructions": instructions,
        "pending_messages": [],
    }


def task_read(situation: Situation, args: dict) -> dict:
    task_id = _ref("T", args["task"])
    if task_id == usability_context.TASK_ID and situation.role == "researcher":
        record = own_task(situation)
    elif task_id == "T-8" and situation.event in ("task.completed", "merge.failed"):
        # These wakes start on a finished Task; its result is what the Orchestrator judges.
        record = _task_row(
            task_id,
            status="complete",
            outcome="succeeded",
            result=REVIEWED_RESULT,
            checklist=_reviewed_checklist(situation),
            merge="yes" if situation.id != "o-hard-review-refuted" else "no",
            completed_commit="abc1234",
            experiment_id=5,
        )
    elif task_id == "T-15":
        record = _task_row(task_id, updated="2026-11-04T10:00:00Z")
    else:
        record = _task_row(task_id)
    return {"record": record, "pending_messages": []}


def task_list(situation: Situation, args: dict) -> dict:
    rows = [_task_row(task_id) for task_id in OTHER_TASKS]
    if situation.role == "researcher":
        rows.append(own_task(situation))
    goal = args.get("goal")
    goal_id = int(_ref("G", goal).split("-")[1]) if goal else None
    return {
        "records": [
            {
                key: row[key]
                for key in (
                    "id",
                    "title",
                    "for_human",
                    "type",
                    "goal_id",
                    "status",
                    "outcome",
                    "priority",
                    "held_by",
                    "waiting_on",
                    "depends_on",
                    "stuck",
                    "updated",
                )
            }
            for row in rows
            if args.get("status") in (None, "", row["status"])
            and args.get("type") in (None, "", row["type"])
            and args.get("held_by") in (None, "", row["held_by"])
            and goal_id in (None, row["goal_id"])
        ]
    }


def task_workers(situation: Situation, args: dict) -> dict:
    return {
        "kind": "available_workers",
        "workers": [{"profile": "researcher", "capabilities": "General research worker"}],
    }


def task_command(situation: Situation, args: dict) -> dict:
    return {
        "transition_state": "pending",
        "kind": "transition_pending",
        "command_id": args["command_id"],
        "owner": "workspace coordinator",
        "reason": "awaiting coordinator application",
        "trigger": "coordinator processes this durable command",
        "retry_at": None,
        "attempts": 0,
    }


# --- claims ---------------------------------------------------------------------------------------

CLAIMS = {
    "C-2": (
        "Earlier scouting does not change the win rate against the champion (n=40)",
        "Over 40 paired seeds of X-3 the win rate moved within noise.",
        ["G-1", "scouting"],
    ),
    "C-5": (
        "V-7 loses 60% of late fights after over-extending; 4 of 5 replays, n=5",
        "In 4 of 5 replays V-7 pushes past its supply line after tick 900.",
        ["G-1", "late-game"],
    ),
    "C-40": (
        "League snapshot: rank 5 after round 11",
        "Standings of round 11.",
        ["G-1", "standings"],
    ),
    "C-41": (
        "League snapshot: rank 4 after round 12, score 81",
        "Standings of round 12.",
        ["G-1", "standings"],
    ),
}


def _claim(claim_id: str) -> dict:
    summary, body, tags = CLAIMS.get(
        claim_id,
        (f"An earlier finding on G-1 ({claim_id})", "Recorded by an earlier Task.", ["G-1"]),
    )
    return {
        "id": claim_id,
        "summary": summary,
        "body": body,
        "tags": tags,
        "status": "active",
        "superseded_by": None,
        "see_also": [],
        **_timestamps(),
        "evidence": [
            {
                "id": 1,
                "type": "replay",
                "ref": "episode:ereq_1 xp:none",
                "stance": "supports",
                "note": "The tick where it goes wrong.",
                "added": EARLIER,
                "by": "session:researcher-5",
            }
        ],
        "freshness": EARLIER,
        "contested": False,
        "counts": {"supports": 1, "contradicts": 0, "qualifies": 0},
        "cited_by": {"claims": [], "experiments": []},
    }


def _claim_summary(claim: dict) -> dict:
    return {
        "id": claim["id"],
        "summary": claim["summary"],
        "tags": claim["tags"],
        "status": claim["status"],
        "retired": False,
        "see_also": [],
        "snippet": claim["summary"],
        "freshness": claim["freshness"],
        "contested": False,
        "counts": claim["counts"],
    }


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def _matches(query: Any, *texts: str) -> bool:
    return bool(_words(str(query)) & _words(" ".join(texts)))


def claim_search(situation: Situation, args: dict) -> dict:
    found = [_claim(claim_id) for claim_id in CLAIMS]
    hits = [c for c in found if _matches(args["query"], c["summary"], c["body"], *c["tags"])]
    return {"records": [_claim_summary(claim) for claim in hits]}


def claim_read(situation: Situation, args: dict) -> dict:
    return {"record": _claim(_ref("C", args["claim"]))}


def claim_inspect(situation: Situation, args: dict) -> dict:
    return {
        "claim": _ref("C", args.get("claim") or "C-5"),
        "inspections": [
            {
                "evidence_id": 1,
                "stance": "supports",
                "note": "The tick where it goes wrong.",
                "type": "replay",
                "ref": "episode:ereq_1 xp:none",
                "summary": (
                    "episode ereq_1 (pinned); the game's replay skill, if it has one, is listed "
                    "in the session context"
                ),
                "episode": "ereq_1",
                "seat": None,
                "phase": None,
                "xp_request": "none",
                "pin": {"path": "documents/replays/ereq_1.json"},
            }
        ],
    }


def claim_written(situation: Situation, args: dict) -> dict:
    return {"record": {**_claim("C-42"), "summary": args["summary"], "new_tags": {}}}


# --- Versions -------------------------------------------------------------------------------------

VERSIONS = {
    "V-7": ("Baseline with resource cap fix", "champion"),
    "V-11": ("Retreat when outnumbered", "uploaded"),
    "V-12": ("Lower retreat threshold", "built"),
}


def _version(version_id: str) -> dict:
    summary, status = VERSIONS.get(version_id, ("An earlier build", "built"))
    # Only what has been uploaded has a policy version; V-12 gets one when ``upload`` is called.
    policy_version = POLICY_VERSIONS.get(version_id) if status != "built" else None
    return {
        "id": version_id,
        "summary": summary,
        "source": "built",
        "commit_sha": "3f2a9c1" + "0" * 33,
        "branch": "main",
        "artifact": {
            "digest": "sha256:" + "0" * 64,
            "kind": "file",
            "path": "build/policy.json",
            "size": 2048,
        },
        "task_id": None,
        "experiment_id": None,
        "uploaded": policy_version
        and {"at": EARLIER, "digest_checked": True, "policy_version_id": policy_version},
        "image": None,
        "policy_name": "cogs-main",
        "created": EARLIER,
        "created_by": SESSION,
        "based_on": [],
        "experiments": [],
        "submission_status": status,
    }


def version_read(situation: Situation, args: dict) -> dict:
    return {
        "record": _version(_ref("V", args["version"])),
        "platform": {"submissions": [], "memberships": []},
    }


def version_list(situation: Situation, args: dict) -> dict:
    return {"records": [_version(version_id) for version_id in ("V-12", "V-11", "V-7")]}


def uploaded(situation: Situation, args: dict) -> dict:
    version_id = _ref("V", args["version"])
    return {
        "record": {
            **_version(version_id),
            "uploaded": {"at": NOW, "digest_checked": True, "policy_version_id": "pv-12"},
            "submission_status": "uploaded",
        },
        "policy_version": _policy_version("pv-12", 12),
        "reconciled": False,
        "attempt": 1,
    }


# --- Experiments ----------------------------------------------------------------------------------

X5_STATUS = {
    "designed": "running",  # started, as a Task's type says to; no candidate is built yet
    "ready": "proposed",  # V-12 fills the candidate
    "started": "running",  # no runs yet
    "pending": "running",  # requests xreq_1 and xreq_2 are running
    "finished": "running",  # those requests ended; xp.status has not stored the rows
    "stored": "running",  # every row is stored and n_min is reached
    "renamed": "running",  # the same, but the metric was renamed halfway, so the rule cannot apply
    "confirmed": "confirmed",
    "refuted": "refuted",
}


# An evaluation has played V-12, so it was uploaded, unless the situation is about uploading it.
X5_EVALUATED = {"pending", "finished", "stored", "renamed", "confirmed", "refuted"}
# How much a stored X-5's candidate beats its baseline by, in win rate.
X5_EFFECT = {"stored": 0.04, "renamed": 0.02, "confirmed": 0.04, "refuted": -0.03}
BASELINE_WIN_RATE = 0.5


def _run(
    run_id: int,
    seed: int,
    arm: str,
    state: str,
    baseline: str,
    effect: float | None = None,
    *,
    metric_renamed: bool = False,
) -> dict:
    version = "V-12" if arm == "candidate" else baseline
    results: dict = {
        "policy_version_id": POLICY_VERSIONS[version],
        "title": f"ide:X-5:{seed}:{arm}",
        "version": version,
    }
    if effect is not None:
        # The two arms differ by the effect on every seed, around a rate that moves by seed.
        win_rate = BASELINE_WIN_RATE + (seed % 5 - 2) / 100 + (effect if arm == "candidate" else 0)
        # The rename: the later seeds of a "renamed" X-5 report the metric under another name.
        metric = "wins_share" if metric_renamed and seed > N_MIN // 2 else "win_rate"
        results["rows"] = [
            {
                "episode_id": f"ereq_{run_id}",
                "game_version": "65",
                "policy_version": version,
                "seed": seed,
                "seats": [0],
                "opponents": HIGH_ROSTER,
                "metrics": {metric: round(win_rate, 4), "timeouts": 1.0},
            }
        ]
    return {
        "id": run_id,
        "experiment_id": "X-5",
        "kind": "xp",
        "platform_request_id": f"xreq_{run_id}",
        "arm": arm,
        "seed": seed,
        "validity": "valid",
        "state": state,
        "episodes_total": 1,
        "episodes_completed": 1 if state == "completed" else 0,
        "results": results,
        "created": EARLIER,
        "updated": NOW,
    }


def _x5_baseline(state: str) -> str:
    """A closed X-5 ran against the champion, as the situations that close it say."""
    return "V-7" if state in ("confirmed", "refuted") else "V-11"


def _x5_runs(state: str) -> list[dict]:
    baseline = _x5_baseline(state)
    if state == "pending":
        return [
            _run(1, 1, "candidate", "pending", baseline),
            _run(2, 1, "baseline", "pending", baseline),
        ]
    if state == "finished":
        return [
            _run(1, 1, "candidate", "completed", baseline),
            _run(2, 1, "baseline", "completed", baseline),
        ]
    if state not in X5_EFFECT:
        return []
    runs = []
    for seed in range(1, N_MIN + 1):
        effect = X5_EFFECT[state]
        renamed = state == "renamed"
        for run_id, arm in ((2 * seed - 1, "candidate"), (2 * seed, "baseline")):
            runs.append(
                _run(run_id, seed, arm, "completed", baseline, effect, metric_renamed=renamed)
            )
    return runs


def _experiment(
    experiment_id: str,
    summary: str,
    status: str,
    baseline: str,
    candidate: str,
    runs: list[dict],
    results: dict,
) -> dict:
    return {
        "id": experiment_id,
        "summary": summary,
        "goal_id": 1,
        "task_id": None,
        "tags": ["G-1", "retreat"],
        "status": status,
        "hypothesis": {
            "statement": f"{summary}: a change at one point, because fights end earlier",
            "if_true": "win_rate rises by at least 3 points",
            "if_false": "win_rate stays within noise of the baseline",
            "motivation": "C-5 shows late losses after over-extending",
        },
        "methods": {
            "baseline": baseline,
            "candidate": candidate,
            "decision_rule": {
                "text": "win_rate up by 3 points with the CI above zero",
                "n_min": N_MIN,
            },
            "metrics": [
                {"direction": "up", "min_effect": 0.03, "name": "win_rate", "role": "primary"},
                {"direction": "down", "name": "timeouts", "role": "guardrail", "tolerance": 1},
            ],
            "procedure": "One paired episode per seed per arm, against pinned opponents",
        },
        "results": results,
        "related_claims": ["C-5"],
        "related_experiments": [],
        "design_sha256": None if status == "proposed" else "f198131f56837de5",
        "created": EARLIER,
        "closed": NOW if status in ("confirmed", "refuted") else None,
        "abandon_reason": None,
        "created_by": SESSION,
        "runs": runs,
        "cited_by": [],
        "versions_tested": [
            {"id": baseline, "role": "baseline"},
            *([{"id": candidate, "role": "candidate"}] if candidate else []),
        ],
    }


def _computed_results(record: dict) -> dict:
    """``results`` as the real close computes them from the record's stored rows."""
    rows = experiment_tool.stored_rows(record["runs"])
    design = experiment_tool.design_from(
        record["methods"], record["methods"]["candidate"], record["runs"], rows
    )
    kept, _ = ingest.ingest_rows(rows, design.game_version, [m.name for m in design.metrics])
    pairs = ingest.pair(experiment_tool.one_row_per_seed(kept), design)
    verdict = decision_rule.evaluate(design, pairs)
    measurements = experiment_tool.measurements_from(design, pairs, verdict)
    return {
        "verdict": verdict.verdict,
        "measurements": measurements,
        "interpretation": experiment_tool.summarise(verdict, measurements, len(pairs)),
        "reason_codes": verdict.reason_codes,
    }


def _closed_results(verdict: str, candidate_rate: float, baseline_rate: float) -> dict:
    return {
        "verdict": verdict,
        "measurements": [
            {
                "metric": "win_rate",
                "baseline": baseline_rate,
                "candidate": candidate_rate,
                "n": N_MIN,
                "effect": round(candidate_rate - baseline_rate, 2),
            }
        ],
        "interpretation": "By the registered rule.",
        "reason_codes": [],
    }


def _x5(situation: Situation) -> dict:
    state = situation.x5
    status = X5_STATUS[state]
    record = _experiment(
        "X-5",
        "Does a lower retreat threshold reduce late losses?",
        status,
        _x5_baseline(state),
        "" if state == "designed" else "V-12",
        _x5_runs(state),
        {},
    )
    if status in ("confirmed", "refuted"):
        record["results"] = _computed_results(record)
    return record


def _x3() -> dict:
    return _experiment(
        "X-3",
        "Does retreating when outnumbered beat the champion?",
        "confirmed",
        "V-7",
        "V-11",
        [],
        _closed_results("confirmed", 0.58, 0.5),
    )


def _x6() -> dict:
    question = "Does a lower retreat threshold raise the win rate?"
    return _experiment("X-6", question, "proposed", "V-11", "V-12", [], {})


def _experiment_for(situation: Situation, experiment_id: str) -> dict | None:
    if experiment_id == "X-5":
        return None if situation.x5 == "absent" else _x5(situation)
    if experiment_id == "X-3":
        return _x3()
    return {**_x6(), "id": experiment_id}


def experiment_read(situation: Situation, args: dict) -> dict:
    experiment_id = _ref("X", args["experiment"])
    record = _experiment_for(situation, experiment_id)
    if record is None:
        problem = f"{experiment_id} does not exist"
        return error_payload([problem], {"tool": "experiment", "action": "read"})
    section = args.get("section")
    if not section:
        return {"record": record}
    if section == "results":
        return {
            "record": {
                "id": experiment_id,
                "status": record["status"],
                "results": {**record["results"], "runs": record["runs"]},
            }
        }
    return {"record": {"id": experiment_id, "status": record["status"], section: record[section]}}


def experiment_search(situation: Situation, args: dict) -> dict:
    records = [_x3()]
    # X-6 is the comparison these situations ask to set up: finding it makes updating it the
    # sensible act, on any tool text, so a search there would measure the fixture.
    if situation.acts != ("experiment.create",):
        records.append(_x6())
    if situation.x5 != "absent":
        records.append(_x5(situation))
    hits = [r for r in records if _matches(args["query"], r["summary"], *r["tags"])]
    return {
        "records": [
            {key: row[key] for key in ("id", "summary", "status", "tags", "created", "closed")}
            for row in hits
        ]
    }


def xp_status(situation: Situation, args: dict) -> dict:
    runs = _x5_runs(situation.x5)
    pending = [run["platform_request_id"] for run in runs if run["state"] != "completed"]
    complete = bool(runs) and not pending and "rows" in runs[0]["results"]
    report = {"experiment": "X-5", "runs": runs, "complete": complete, "pending": pending}
    status = X5_STATUS.get(situation.x5, "absent")
    if status in ("confirmed", "refuted"):
        report["note"] = f"X-5 is {status}; its runs are final"
    elif complete:
        report["next"] = "experiment.close {experiment: X-5} applies the decision rule"
    elif pending:
        report["next"] = f"{len(pending)} requests still run; task_wait {{kind: xp}} or try later"
    return report


# --- the workspace --------------------------------------------------------------------------------


def _capabilities(situation: Situation) -> list[dict]:
    rows = []
    for name in ("build", "check", "evaluate", "upload", "xp", "replay", "submit"):
        broken = name == "evaluate" and situation.id in BROKEN_EVALUATE
        rows.append(
            {
                "name": name,
                "status": "broken" if broken else "ok",
                "last_checked": EARLIER,
                "last_output": "No module named evaluate" if broken else "ok",
                "repair_task": None,
            }
        )
    return rows


def workspace_doctor(situation: Situation, args: dict) -> dict:
    capabilities = _capabilities(situation)
    broken = [row["name"] for row in capabilities if row["status"] == "broken"]
    return {
        "commits": {
            "image": "d27689bc",
            "source": "d27689bc",
            "checkout": "/opt/ide-distributions",
        },
        "watcher": {"heartbeat": NOW, "age_s": 12},
        "stores": {
            "workspace": {"ok": True, "diff": [], "user_version": 9, "expected_version": 9},
            "coworld": {"ok": True, "diff": [], "user_version": 2, "expected_version": 2},
        },
        "capabilities": capabilities,
        "clis": {
            "lock_refreshed": "2026-11-03",
            "clis": [
                {
                    "name": "coworld",
                    "distribution": "coworld",
                    "version": "0.1.67",
                    "path": "/opt/data/workspace/tools/venv/bin/coworld",
                    "lock": "0.1.67",
                    "stale": False,
                }
            ],
        },
        "skill_overrides": [],
        "core_path": {"path": "/opt/ide-distributions", "writable": False},
        "api_keys": {"checked": ["OPENROUTER_API_KEY"], "differing": [], "missing": [], "note": ""},
        "problems": [f"capability {name} is broken" for name in broken],
    }


def workspace_capabilities(situation: Situation, args: dict) -> dict:
    capabilities = _capabilities(situation)
    broken = [row["name"] for row in capabilities if row["status"] == "broken"]
    return {"capabilities": capabilities, "broken": broken}


ENVIRONMENT_GUIDE = f"""# Environment guide

## Where you are

- Workspace root: `/opt/data/workspace`.
- Session Coworld: `cow_cogs`, folder `/opt/data/workspace/cow_cogs`: `code/` (git; `main` holds \
the policy and `mixin/`), `source/`, `worktrees/`, `documents/`.

## Two Python environments

- Hermes's own interpreter runs the plugin and the store-only tools. Nothing else runs there.
- The workspace environment `/opt/data/workspace/tools/venv/bin/python` runs the `research` CLI, \
the platform tools, mixin commands and the watcher. It holds `coworld`, `softmax`, `numpy` and \
`PyYAML`. Use it for your own scripts.

## Writable and read-only paths

- Writable: everything under `/opt/data/workspace/cow_cogs`, such as `{WORKTREE}` and `documents/`.
- Read-only: the profile source checkout `/opt/ide-distributions`.
"""


def workspace_environment_guide(situation: Situation, args: dict) -> dict:
    return {"guide": ENVIRONMENT_GUIDE}


def github_repositories(situation: Situation, args: dict) -> dict:
    return {
        "page": args.get("page") or 1,
        "repositories": [{"full_name": "acme/policy-kit", "private": True}],
        "next": "Pass page + 1 while the list is full; clone with repository owner/name.",
    }


def ui_notify(situation: Situation, args: dict) -> dict:
    return {
        "record": {
            "id": "N-1",
            "text": args["text"],
            "refs": args.get("refs") or [],
            "urgency": args.get("urgency") or "normal",
            "created": NOW,
            "created_by": "session:orchestrator",
            "read_at": None,
            "read_by": None,
            "coworld_id": usability_context.COWORLD,
        }
    }


def ui_show(situation: Situation, args: dict) -> dict:
    target = {key: args[key] for key in ("ref", "view") if args.get(key)}
    return {"conversation": "workspace", "show": {**target, "at": NOW}, "version": 1}


# --- the platform ---------------------------------------------------------------------------------

STANDINGS = [
    ("ply_kraken", "Kraken", 96.5),
    ("ply_harbor", "Harbor", 92.0),
    ("ply_ember", "Ember", 88.75),
    ("ply_quartz", "Quartz", 84.0),
    (PLAYER, "Cogs", 81.0),
    ("ply_lantern", "Lantern", 77.5),
]


def _league_row(league_id: str = LEAGUE) -> dict:
    return {
        "id": league_id,
        "name": "Cogs Main",
        "game_id": "game_cogs",
        "coworld_id": usability_context.COWORLD,
        "coworld_name": "cogs",
        "default_variant_id": None,
        "participation_url": f"https://softmax.com/participate?league_id={league_id}",
        "forum_markdown_url": f"https://softmax.com/v2/forums/{league_id}.md",
        "wiki_markdown_url": f"https://softmax.com/v2/wikis/{league_id}/pages.md",
    }


def _division_row() -> dict:
    return {"id": DIVISION, "name": "Main", "level": 1, "league_id": LEAGUE}


def _standing_rows(situation: Situation) -> list[dict]:
    order = list(STANDINGS)
    if situation.id in GOAL_MET:  # our Player, 5th in the fixture, reached 3rd
        order.insert(2, order.pop(4))
    return [
        {
            "division_id": DIVISION,
            "division_name": "Main",
            "level": 1,
            "rank": rank,
            "player_id": player,
            "player_name": name,
            "score": score,
            "rounds_played": 12,
            "policy_label": f"{name.lower()}-main:v3",
        }
        for rank, (player, name, score) in enumerate(order, start=1)
    ]


def _policy_version(policy_version_id: str, version: int) -> dict:
    return {
        "id": policy_version_id,
        "name": "cogs-main",
        "version": version,
        "label": f"cogs-main:v{version}",
        "content_hash": "0" * 12,
        "player_id": PLAYER,
        "created_at": EARLIER,
    }


def _membership(player: str, policy_version_id: str, *, champion: bool = False) -> dict:
    return {
        "id": f"mem_{policy_version_id}",
        "league_id": LEAGUE,
        "policy_version": {**_policy_version(policy_version_id, 3), "player_id": player},
        "status": "competing",
        "division_id": DIVISION,
        "player_id": player,
        "substatus": None,
        "is_champion": champion,
    }


def _episode(episode_id: str, seed: int, policy_version_id: str) -> dict:
    ours, theirs = (policy_version_id, "pv-1")
    return {
        "id": episode_id,
        "round_id": "rnd_55",
        "coworld_id": usability_context.COWORLD,
        "league_id": LEAGUE,
        "seed": seed,
        "status": "completed",
        "policy_version_ids": [ours, theirs],
        "participants": [
            {"kind": "policy", "position": 0, "policy_version_id": ours},
            {"kind": "policy", "position": 1, "policy_version_id": theirs},
        ],
        "scores": [
            {"policy_version_id": ours, "score": 46.0 + seed % 7},
            {"policy_version_id": theirs, "score": 50.0 - seed % 5},
        ],
        "results": {
            "seed": seed,
            "episodes": [
                {"episode": 0, "seat": 0, "seed": seed, "score": 46.0 + seed % 7, "errors": 1},
                {"episode": 0, "seat": 1, "seed": seed, "score": 50.0 - seed % 5, "errors": 0},
            ],
        },
        "created_at": EARLIER,
    }


def platform_credits(situation: Situation) -> dict:
    low = situation.id in LOW_CREDITS
    return {
        "balance": 0.0 if low else 840.0,
        "max": 1000.0,
        "refill_amount": 1000.0,
        "next_refill_at": "2026-11-05T00:00:00Z",
        "spent_this_period": 160.0,
        "recent_requests": [],
    }


def _post(post_id: str = "p-9") -> dict:
    return {
        "id": post_id,
        "forum": "fakegame",
        "title": "Tips for new players",
        "body": "Scout before the first fight, and watch your supply line.",
        "content_format": "markdown",
        "author": {"player_id": "ply_harbor", "name": "Harbor"},
        "identity": "player",
        "score": 4,
        "vote_count": 4,
        "created_at": EARLIER,
    }


def _wiki_page(wiki: str, page: str) -> dict:
    return {
        "wiki": wiki,
        "page": page,
        "page_id": "wp-31",
        "title": page.replace("-", " ").capitalize(),
        "body": "Most entries open with two scouts and hold the third back.",
        "revision_id": "r-4",
        "created_at": EARLIER,
    }


def _revision(page_id: Any, revision_id: str, parent: str | None, author: str) -> dict:
    return {
        "revision_id": revision_id,
        "page_id": page_id,
        "parent_revision_id": parent,
        "author": {"player_id": f"ply_{author.lower()}", "name": author},
        "identity": "player",
        "note": "",
        "body": "Most entries open with two scouts and hold the third back.",
        "created_at": EARLIER,
    }


def _replay_pin(episode: str) -> dict:
    return {
        "id": 1,
        "kind": "replay",
        "ref": f"episode:{episode}",
        "download": {
            "path": f"/opt/data/workspace/cow_cogs/documents/replays/{episode}.json",
            "digest": "0" * 64,
            "size_bytes": 4096,
            "artifact": "replay",
        },
        "created": NOW,
        "existing": False,
    }


def _standings(situation: Situation, args: dict) -> dict:
    if args.get("division"):
        return {"division": args["division"], "standings": _standing_rows(situation)}
    return {
        "league": args.get("league") or LEAGUE,
        "divisions": [_division_row()],
        "standings": _standing_rows(situation),
    }


def _forum_read(args: dict) -> dict:
    post = _post(str(args["post"]))
    markdown = f"# {post['title']}\n\n{post['body']}\n\n## Comments\n\n- Ember: agreed."
    return {"post": post["id"], "markdown": markdown}


def _episodes(args: dict) -> dict:
    policy_version = args.get("policy_version")
    if policy_version:
        rows = [_episode(f"ereq_{seed}", seed, str(policy_version)) for seed in range(1, 6)]
        return {"policy_version": policy_version, "episodes": rows, "next_cursor": None}
    rows = [_episode(f"ereq_{seed}", seed, "pv-7") for seed in range(1, 6)]
    return {
        "league": args.get("league") or LEAGUE,
        "policy_versions": ["pv-7", "pv-1"],
        "truncated": False,
        "next_cursors": {},
        "episodes": rows,
    }


PLATFORM: dict[str, Callable[[Situation, dict], dict]] = {
    "leagues": lambda s, a: {"leagues": [_league_row()]},
    "league": lambda s, a: {"league": _league_row(str(a["league"]))},
    "coworld_update": lambda s, a: {"status": "current"},
    "divisions": lambda s, a: {"league": a["league"], "divisions": [_division_row()]},
    "standings": _standings,
    "active_policies": lambda s, a: {
        "league": a["league"],
        "excluded": {"player": PLAYER if a.get("exclude_player") else None, "user_players": None},
        "memberships": [
            _membership("ply_kraken", "pv-1"),
            _membership("ply_harbor", "pv-2"),
            *([] if a.get("exclude_player") else [_membership(PLAYER, "pv-7", champion=True)]),
        ],
    },
    "policy_versions": lambda s, a: {
        "identity": a.get("identity") or "player",
        "policy_versions": [_policy_version("pv-7", 1), _policy_version("pv-11", 2)],
    },
    "policy_file": lambda s, a: {
        "policy_version": a["policy_version"],
        "path": f"/opt/data/workspace/cow_cogs/documents/policies/{a['policy_version']}.txt",
        "content_type": "text/plain; charset=utf-8",
        "size_bytes": 2048,
        "sha256": "0" * 64,
        "identity": "player",
    },
    "results": lambda s, a: {
        "player": a.get("player") or PLAYER,
        "leagues": [
            {
                "league": _league_row(),
                "memberships": [_membership(PLAYER, "pv-7", champion=True)],
                "standings": [row for row in _standing_rows(s) if row["player_id"] == PLAYER],
            }
        ],
    },
    "episodes": lambda s, a: _episodes(a),
    "episode": lambda s, a: {"episode": _episode(str(a["episode"]), 12, "pv-7")},
    "replay": lambda s, a: {
        "episode": a["episode"],
        "path": f"/opt/data/workspace/cow_cogs/documents/replays/{a['episode']}.json",
        "pin": _replay_pin(str(a["episode"])),
        "existing": False,
    },
    "credits": lambda s, a: {"credits": platform_credits(s)},
    "champion": lambda s, a: {
        "league": a["league"],
        "champion": _membership(PLAYER, "pv-7", champion=True),
    },
    "forum_list": lambda s, a: {
        "slug": a.get("slug") or LEAGUE,
        "sort": a.get("sort") or "hot",
        "posts": [_post()],
    },
    "forum_read": lambda s, a: _forum_read(a),
    "forum_search": lambda s, a: {
        "query": a["query"],
        "slug": a.get("slug"),
        "hits": [{"post": _post(), "rank": 1.0}] if _matches(a["query"], "scouting scout") else [],
    },
    "wiki_read": lambda s, a: {
        "page": {
            **(page := _wiki_page(str(a["wiki"]), str(a["page"]))),
            "current_revision": {"id": page["revision_id"], "body": page["body"]},
        }
    },
    "wiki_search": lambda s, a: {
        "wiki": a["wiki"],
        "query": a["query"],
        "hits": [_wiki_page(str(a["wiki"]), "opening-book")],
    },
    "wiki_history": lambda s, a: {
        "page_id": a["page_id"],
        "revisions": [
            _revision(a["page_id"], "r-4", "r-3", "Harbor"),
            _revision(a["page_id"], "r-3", None, "Ember"),
        ],
    },
    "coworld": lambda s, a: {
        "coworld": {
            "id": a["coworld"],
            "name": "cogs",
            "version": "65",
            "player_runtime": "game-hosted",
            "source_url": "https://github.com/softmax-universe/cogs",
            "commit": "9c1d2e3" + "0" * 33,
        }
    },
}


def softmax_get(situation: Situation, args: dict) -> dict:
    path = str(args.get("path"))
    response: Any = {"entries": []}
    if "policy-logs" in path:
        response = "tick 880: path planner 41 ms\ntick 881: path planner 44 ms\n"
    return {
        "method": args.get("method"),
        "path": path,
        "identity": args.get("identity") or "player",
        "response": response,
    }


# --- Hermes's own tools ---------------------------------------------------------------------------


@functools.cache
def _code_files() -> dict[str, str]:
    """The code repository: the fake Coworld's files, under ``mixin/`` and at the root as a
    session's setup copies them, in this fixture's metrics and at its floor of 20 episodes."""
    files = {}
    for relative, text in fixture_files(usability_context.COWORLD).items():
        text = text.replace("fakegame", "cogs").replace("min_episodes: 10", "min_episodes: 20")
        text = re.sub(r"\berrors\b", "timeouts", re.sub(r"\bscore\b", "win_rate", text))
        parts = relative.split("/")
        if parts[0] == "skills":
            files[f"mixin/skills/cogs-{parts[1]}/SKILL.md"] = text
        else:
            files[relative] = files[f"mixin/{relative}"] = text
    return files


def read_file(situation: Situation, args: dict) -> dict:
    path = str(args.get("path"))
    if "documents/logs/" in path:
        text = "$ sh mixin/tools/build.sh\nbuilding...\n"
    else:
        relative = path.split("/code/", 1)[-1].split(f"{WORKTREE}/", 1)[-1]
        text = _code_files().get(relative.lstrip("./"))
    if text is None:
        return {"error": f"File not found: {path}", "not_found": True}
    floor = MIXIN_FLOOR.get(situation.id)
    if floor:
        text = text.replace("min_episodes: 20", f"min_episodes: {floor}")
    lines = text.splitlines()
    numbered = "\n".join(f"{n:6}|{line}" for n, line in enumerate(lines, start=1))
    return {
        "content": numbered,
        "total_lines": len(lines),
        "file_size": len(text.encode()),
        "truncated": False,
    }


def terminal(situation: Situation, args: dict) -> dict:
    command = str(args.get("command"))
    output = ""
    if command.startswith("git log"):
        output = "3f2a9c1 Lower the retreat threshold\nabc1234 Fix the resource cap\n"
    elif command.startswith("git status"):
        output = "On branch task/21\nnothing to commit, working tree clean\n"
    elif command.startswith("pwd"):
        output = f"{WORKTREE}\n"
    elif command.startswith("ls"):
        output = "AGENTS.md\nmixin\npolicy.json\ntools\n"
    return {"output": output, "exit_code": 0, "error": None}


BUILTINS: dict[str, Callable[[Situation, dict], dict]] = {
    "read_file": read_file,
    "terminal": terminal,
    "search_files": lambda s, a: {"total_count": 0},
    "write_file": lambda s, a: {
        "bytes_written": len(str(a.get("content"))),
        "dirs_created": False,
    },
    "patch": lambda s, a: {"success": True, "files_modified": [a.get("path")]},
    "execute_code": lambda s, a: {
        "status": "success",
        "output": "",
        "tool_calls_made": 0,
        "duration_seconds": 0.2,
    },
    "process_manage": lambda s, a: {"processes": []},
    "session_search": lambda s, a: {"success": True, "results": [], "count": 0},
    "web_search": lambda s, a: {"success": True, "data": {"web": []}},
    "web_extract": lambda s, a: {"results": []},
    "vision_analyze": lambda s, a: {"success": True, "analysis": "A chart of win rate by round."},
}

# --- the table ------------------------------------------------------------------------------------

ACTIONS: dict[str, Callable[[Situation, dict], dict]] = {
    "goal.read": goal_read,
    "goal.list": goal_list,
    "directive.list": directive_list,
    "directive.add": directive_added,
    "settings.read": settings_read,
    "task.read": task_read,
    "task.list": task_list,
    "task.workers": task_workers,
    "task.command": task_command,
    "claim.search": claim_search,
    "claim.read": claim_read,
    "claim.inspect_evidence": claim_inspect,
    "claim.write": claim_written,
    "version.read": version_read,
    "version.list": version_list,
    "experiment.read": experiment_read,
    "experiment.search": experiment_search,
    "xp.status": xp_status,
    "workspace.doctor": workspace_doctor,
    "workspace.capabilities": workspace_capabilities,
    "workspace.environment_guide": workspace_environment_guide,
    "github.repositories": github_repositories,
    "ui.notify": ui_notify,
    "ui.show": ui_show,
    "upload": uploaded,
    "softmax_api": softmax_get,
    **{f"platform.{action}": handler for action, handler in PLATFORM.items()},
    **BUILTINS,
}


# Which read results a run used, kept in a baseline's ``meta.reads``. ``placeholder`` there means
# every read got one fixed line. ``usability_results.compare`` says when two runs differ in this.
READS_HASH = hashlib.sha256(
    Path(__file__).read_bytes() + json.dumps(_code_files(), sort_keys=True).encode()
).hexdigest()[:10]


def version_ref(args: dict | None) -> str:
    return _ref("V", (args or {}).get("version"))


def _show_uploads(answer: dict, uploaded: Sequence[str]) -> None:
    """A read after this run's own ``upload`` finds it uploaded."""
    records = answer["records"] if "records" in answer else [answer["record"]]
    for record in records:
        if record["id"] in uploaded:
            record["uploaded"] = {
                "at": NOW,
                "digest_checked": True,
                "policy_version_id": POLICY_VERSIONS.get(record["id"], "pv-new"),
            }
            record["submission_status"] = "uploaded"


def result(
    situation: Situation, label: str, args: dict | None, uploaded: Sequence[str] = ()
) -> str:
    """The answer to a call the tool's checks accepted. ``label`` is ``tool.action`` or a flat
    tool's name. An IDE tool's answer carries ``ok``, as ``run_tool`` adds it. ``uploaded`` names
    the Versions this run has uploaded."""
    answer = ACTIONS[label](situation, args or {})
    if label in ("version.read", "version.list"):
        evaluated = situation.x5 in X5_EVALUATED and "upload" not in situation.acts
        # xp.request checks uploads before it asks the platform, so a credit refusal means uploaded.
        evaluated = evaluated or situation.id in LOW_CREDITS
        _show_uploads(answer, [*uploaded, "V-12"] if evaluated else uploaded)
    if label not in BUILTINS:
        answer.setdefault("ok", True)
    return json.dumps(answer)
