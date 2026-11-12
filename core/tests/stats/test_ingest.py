"""Episode rows: the evaluate-output contract, cleaning counts and baseline/candidate pairing."""

import dataclasses

import pytest

from research_core.stats.design import EvaluationDesign, MetricSpec, signed
from research_core.stats.ingest import (
    EpisodeRow,
    ingest,
    ingest_rows,
    pair,
    rows_from_evaluate_output,
)

UP = MetricSpec("score", "up", "primary", 0.5, None)
DOWN = MetricSpec("errors", "down", "guardrail", None, 0.1)
DESIGN = EvaluationDesign(
    game_version="65",
    baseline_version="pv-base",
    candidate_version="pv-cand",
    seats=(0, 1),
    opponents=("pv-opp",),
    seeds=(1, 2, 3),
    metrics=(UP, DOWN),
    k=0.5,
    delta={},
    n_min=3,
)


def row(
    episode_id,
    policy="pv-base",
    seed=1,
    version="65",
    seats=(0, 1),
    opponents=("pv-opp",),
    metrics=None,
    status="completed",
):
    return EpisodeRow(
        episode_id,
        version,
        policy,
        seed,
        seats,
        opponents,
        {"score": 1.0, "errors": 2.0} if metrics is None else metrics,
        status,
    )


def test_ingest_rows_drops_and_counts():
    rows = [
        row("a"),
        row("a"),  # duplicate id
        row("b", status="failed"),
        row("c", version="64"),
        row("d", metrics={"score": 1.0}),
        row("e", metrics={}),
    ]
    kept, report = ingest_rows(rows, "65", ["score", "errors"])
    assert [r.episode_id for r in kept] == ["a"]
    assert (report.kept, report.duplicates, report.failed, report.wrong_version) == (1, 1, 1, 1)
    assert report.missing_metrics == ["errors", "score"]


def test_ingest_drops_wrong_seats_and_opponents():
    rows = [row("a"), row("b", seats=(1, 0)), row("c", opponents=("other",))]
    kept, report = ingest(rows, DESIGN)
    assert [r.episode_id for r in kept] == ["a"]
    assert report.wrong_seats == 2


def test_ingest_requests_design_metrics():
    kept, report = ingest([row("a", metrics={"score": 1.0})], DESIGN)
    assert kept == [] and report.missing_metrics == ["errors"]


def test_pair_matches_by_key_and_never_mixes_versions():
    rows = [
        row("b1", "pv-base", seed=1),
        row("c1", "pv-cand", seed=1),
        row("b2", "pv-base", seed=2),
        row("c2", "pv-cand", seed=2, version="64"),
        row("x", "pv-other", seed=3),
        row("b3", "pv-base", seed=3),
    ]
    pairs = pair(rows, DESIGN)
    assert [(b.episode_id, c.episode_id) for b, c in pairs] == [("b1", "c1")]


def test_pair_unique_first_by_episode_id():
    rows = [
        row("b2", "pv-base"),
        row("b1", "pv-base"),
        row("c2", "pv-cand"),
        row("c1", "pv-cand"),
    ]
    pairs = pair(rows, DESIGN)
    assert [(b.episode_id, c.episode_id) for b, c in pairs] == [("b1", "c1")]


def test_pair_requires_same_seats_and_opponents():
    rows = [row("b", "pv-base"), row("c", "pv-cand", seats=(1, 0))]
    assert pair(rows, DESIGN) == []


def test_pair_sorts_by_seed():
    rows = [row("b", seed=3), row("c", "pv-cand", seed=3), row("d", seed=1), row("e", "pv-cand")]
    assert [b.seed for b, _ in pair(rows, DESIGN)] == [1, 3]


def test_signed_negates_down():
    assert signed(2.0, UP) == 2.0
    assert signed(2.0, DOWN) == -2.0


def test_ingest_drops_seed_outside_design():
    kept, report = ingest([row("a", seed=1), row("b", seed=99)], DESIGN)
    assert [r.episode_id for r in kept] == ["a"]
    assert report.wrong_seed == 1


def test_ingest_and_pair_accept_json_lists():
    rows = [row("b", "pv-base", seats=[0, 1], opponents=["pv-opp"]), row("c", "pv-cand")]
    kept, report = ingest(rows, DESIGN)
    assert report.kept == 2
    assert len(pair(kept, DESIGN)) == 1


def test_pair_rejects_candidate_equal_to_baseline():
    same = dataclasses.replace(DESIGN, candidate_version="pv-base")
    with pytest.raises(ValueError):
        pair([row("a")], same)


# --- the mixin evaluate output contract ---

EVALUATE_OUTPUT = "\n".join(
    [
        "",
        '{"episode": 0, "seed": 1, "seat": 0, "opponents": ["pv-opp"],'
        ' "metrics": {"score": 12.5, "errors": 1}}',
        '{"episode": 0, "seed": 1, "seat": 1, "opponents": ["pv-opp"],'
        ' "metrics": {"score": 7, "errors": 0}}',
        "",
        '{"episode": "ep-1", "seed": 2, "seat": 0, "status": "failed", "reason": "timeout"}',
        "",
    ]
)
GOOD_LINE = '{"episode": 0, "seed": 1, "seat": 0, "metrics": {"score": 1}}'


def test_rows_from_evaluate_output_gives_one_row_per_seat():
    rows = rows_from_evaluate_output(
        EVALUATE_OUTPUT, policy_version="pv-cand", game_version="65", request_id="xreq_1"
    )
    assert [r.episode_id for r in rows] == ["xreq_1:0:s0", "xreq_1:0:s1", "xreq_1:ep-1:s0"]
    assert rows[0] == EpisodeRow(
        "xreq_1:0:s0",
        "65",
        "pv-cand",
        1,
        (0,),
        ("pv-opp",),
        {"score": 12.5, "errors": 1.0},
        "completed",
    )
    assert all(isinstance(value, float) for value in rows[0].metrics.values())
    assert rows[1].seats == (1,) and rows[1].metrics == {"score": 7.0, "errors": 0.0}
    failed = rows[2]
    assert failed.status == "failed" and failed.reason == "timeout"
    assert failed.metrics == {} and failed.opponents == ()


def test_rows_from_evaluate_output_without_request_id():
    rows = rows_from_evaluate_output(GOOD_LINE, policy_version="p", game_version="65")
    assert [r.episode_id for r in rows] == ["0:s0"]
    assert rows_from_evaluate_output("\n  \n", policy_version="p", game_version="65") == []


def test_lines_that_are_not_json_objects_are_log_output():
    """stderr shares the log evaluate's output is read from: a uv or warning line is no row."""
    output = "\n".join(
        [
            "Installed 3 packages in 4ms",
            GOOD_LINE,
            "warning: something deprecated",
            "[1, 2]",
        ]
    )
    rows = rows_from_evaluate_output(output, policy_version="p", game_version="65")
    assert [r.episode_id for r in rows] == ["0:s0"]


def test_request_id_keeps_episode_ids_distinct_across_requests():
    """Without the request id, two requests' episode 0 share an id and ingest drops one."""
    base = rows_from_evaluate_output(GOOD_LINE, policy_version="pv-base", game_version="65")
    cand = rows_from_evaluate_output(GOOD_LINE, policy_version="pv-cand", game_version="65")
    assert ingest_rows(base + cand, "65", ["score"])[1].duplicates == 1
    base = rows_from_evaluate_output(
        GOOD_LINE, policy_version="pv-base", game_version="65", request_id="xreq_b"
    )
    cand = rows_from_evaluate_output(
        GOOD_LINE, policy_version="pv-cand", game_version="65", request_id="xreq_c"
    )
    assert ingest_rows(base + cand, "65", ["score"])[1].duplicates == 0


def test_rows_from_evaluate_output_feed_ingest_and_pair():
    base = rows_from_evaluate_output(
        EVALUATE_OUTPUT, policy_version="pv-base", game_version="65", request_id="xreq_b"
    )
    cand = rows_from_evaluate_output(
        EVALUATE_OUTPUT, policy_version="pv-cand", game_version="65", request_id="xreq_c"
    )
    one_seat = dataclasses.replace(DESIGN, seats=(0,))
    kept, report = ingest(base + cand, one_seat)
    assert (report.kept, report.failed, report.wrong_seats) == (2, 2, 2)
    pairs = pair(kept, one_seat)
    assert [(b.episode_id, c.episode_id) for b, c in pairs] == [("xreq_b:0:s0", "xreq_c:0:s0")]


@pytest.mark.parametrize(
    "line, problem",
    [
        ('{"episode": 1, "seed": 1,', "line 2 is not JSON"),
        (GOOD_LINE, "line 2 repeats episode 0 seat 0 of line 1"),
        ('{"seed": 1, "seat": 0, "metrics": {}}', "missing key 'episode'"),
        ('{"episode": 0, "seed": 1, "seat": 0}', "missing key 'metrics'"),
        ('{"episode": 0, "seed": 1, "seat": 0, "metrics": {}, "seats": [0]}', "unknown keys"),
        ('{"episode": true, "seed": 1, "seat": 0, "metrics": {}}', "episode must be"),
        ('{"episode": "", "seed": 1, "seat": 0, "metrics": {}}', "episode must be"),
        ('{"episode": 0, "seed": "1", "seat": 0, "metrics": {}}', "seed and seat must be"),
        ('{"episode": 0, "seed": 1, "seat": 0.5, "metrics": {}}', "seed and seat must be"),
        ('{"episode": 0, "seed": 1, "seat": 0, "opponents": "pv-opp", "metrics": {}}', "opponents"),
        ('{"episode": 0, "seed": 1, "seat": 0, "status": "crashed", "metrics": {}}', "status"),
        ('{"episode": 0, "seed": 1, "seat": 0, "metrics": {"score": "high"}}', "metrics must be"),
        ('{"episode": 0, "seed": 1, "seat": 0, "metrics": {"score": NaN}}', "metrics must be"),
        ('{"episode": 0, "seed": 1, "seat": 0, "metrics": {"score": true}}', "metrics must be"),
        ('{"episode": 0, "seed": 1, "seat": 0, "metrics": [1]}', "metrics must be"),
        ('{"episode": 0, "seed": 1, "seat": 0, "metrics": {}, "reason": 3}', "reason must be"),
    ],
)
def test_rows_from_evaluate_output_rejects_malformed_lines(line, problem):
    with pytest.raises(ValueError, match=problem):
        rows_from_evaluate_output(GOOD_LINE + "\n" + line, policy_version="p", game_version="65")
