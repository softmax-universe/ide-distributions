"""Experience request bodies and display titles.

Recovery uses the idempotency key saved with the body in the local intent, never the title.
"""

from __future__ import annotations

from dataclasses import dataclass

TITLE_PREFIX = "ide:"
# The seat our policy sits in. A seat is the game's 0-based player slot, the index its results
# score each player by; only this seat's rows measure the Version a request tests.
OUR_SEAT = 0


def title_for(experiment_n: int, seed: int, arm: str) -> str:
    """``ide:X-<n>:<seed>:<arm>``, the title an Experiment's request carries (A4.4)."""
    return f"{TITLE_PREFIX}X-{experiment_n}:{seed}:{arm}"


@dataclass(frozen=True)
class Seats:
    """What a game accepts: ``exact`` when its variant fixes the count, else any of low..high."""

    exact: int | None
    low: int
    high: int


def seats_for(manifest: dict, variant_id: str | None = None) -> Seats:
    """A Coworld manifest's seat rule, for a variant (default: the first, as the platform does).

    Imports coworld here: the plugin imports this module in Hermes's interpreter, which lacks it.
    """
    from coworld.manifest_validation import infer_token_count_for_game_config, token_count_bounds

    schema = manifest["game"]["config_schema"]
    variants = manifest["variants"]
    variant = next((v for v in variants if v["id"] == variant_id), variants[0])
    low, high = token_count_bounds(schema)
    return Seats(infer_token_count_for_game_config(schema, variant["game_config"]), low, high)


def request_body(
    *,
    coworld_id: str,
    policy_version_id: str,
    seed: int | None,
    episodes: int,
    title: str,
    league_id: str | None = None,
    opponents: list[str] | None = None,
    open_seats: int = 0,
) -> dict:
    """The ``POST /v2/experience-requests`` body (the V2 create shape, reads/1.md §2.10).

    Our policy sits in slot :data:`OUR_SEAT`; each opponent policy version fills the next slot.
    The roster's length is the game's player count: the platform adds no seats, so each of the
    ``open_seats`` is an entry it samples a league champion into, per episode. A league is the
    request's ``target``, which picks the Coworld and variant itself: the platform refuses
    ``coworld_id`` beside it. ``game_config_overrides.seed`` pins the seed when one is given.
    """
    roster = [{"player": {"policy_ref": policy_version_id}, "slot": OUR_SEAT}]
    for slot, opponent in enumerate(opponents or (), start=OUR_SEAT + 1):
        roster.append({"player": {"policy_ref": opponent}, "slot": slot})
    roster.extend({"player": {"random": True}, "slot": -1} for _ in range(open_seats))
    body: dict = {
        "roster": roster,
        "num_episodes": episodes,
        "title": title,
    }
    if league_id is not None:
        body["target"] = {"league_id": league_id}
    else:
        body["coworld_id"] = coworld_id
    if seed is not None:
        body["game_config_overrides"] = {"seed": seed}
    return body


def pinned_seats(body: dict) -> dict[str, str]:
    """The policy version id in each pinned seat of a request body, keyed by the seat as text
    (a run stores it as JSON). A seat the league samples per episode is not in it."""
    return {
        str(entry["slot"]): entry["player"]["policy_ref"]
        for entry in body["roster"]
        if entry["slot"] != -1
    }
