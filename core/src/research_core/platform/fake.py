"""A persistent fake of the Softmax platform for tests and local runs (plan Task 4.2).

State is one JSON file (``<workspace>/.state/fake_platform.json``) read, changed and written
under a file lock on every operation, so the watcher process, tool subprocesses and a test
share one platform. Ids are deterministic (``pv_<hash>``, ``sub_<hash>``, ``xreq_<hash>``), so
the same sequence of operations gives the same ids in any run.

Shapes follow :mod:`research_core.platform.protocol`. A submission is ``pending`` until the
next ``submissions``/``memberships`` read after ``polls_to_complete`` polls, then ``placed``;
its membership goes ``pending`` -> ``competing`` and, with ``auto_champion="always"``,
becomes the Player's champion in that league. An XP request is ``pending`` before its first
``xp_get``, ``running`` until ``polls_to_complete`` polls, then ``completed``. The game seats
:data:`FAKE_GAME_SEATS`, and ``xp_create`` refuses the rosters the real route refuses.

Seeded metrics (the research lever the fake research cycle measures). A ``policy.json`` upload
with ``{"parameters": {"aggression": a, "patience": p}}`` plays by those parameters; any other
policy plays :data:`DEFAULT_PARAMETERS` with noise keyed on its id. For a request whose body
has ``game_config_overrides.seed``: ``score`` = 50 + a per-seed board term (+-4) + 0.5 per
``aggression`` point above 3 + episode noise (+-1) keyed on (seed, episode, seat, aggression);
``errors`` = an integer 0..2 keyed on (seed, episode, seat, patience) + ``max(0, 8 - patience)``.
So a policy that differs only in ``patience`` plays the same score on every seed and fewer
errors. Unseeded requests key their metrics on the request id.

League reads are generated, never stored: :meth:`FakePlatform.league_episodes` plays three
fixed policies against one house policy per seed (paired noise is measurable);
``episodes_for_policy`` plays that policy against the house policy on ten seeds.

Faults come from ``IDE_FAKE_FAULTS`` (semicolon-separated ``name=value``):

- ``credits=N``: the balance reads ``N``.
- ``identity=user``: the Player credential answers as a User (writes are refused);
  ``identity=rejected``: ``whoami`` raises a 401.
- ``expires_in=<seconds>``: the Player credential expires that long after now.
- ``fail_after=<op>``: ``RuntimeError`` after that operation persisted its change.
- ``xp_limit=N``: a new XP request raises a retryable 429 once ``N`` exist.
- ``upload=503:N``: the first ``N`` uploads raise a retryable 503 (``upload=503`` means one).
- ``lookup=503``: ``lookup_policy_upload`` raises a retryable 503; ``lookup=403``: it is
  refused; ``lookup=unavailable``: it raises ``NotAvailable`` (a client without the route);
  ``lookup=not_found``: it answers no match whatever the platform holds.
- ``submit=409``: ``submit`` raises a 409.
- ``champion=<policy version id>``: on the next ``memberships`` read the champion flips to
  that policy version, as if the user changed it on the website.
- ``platform=down``: every call raises a retryable 503.
- ``credits_refuse=1``: ``xp_create`` raises :class:`CreditsRefused`.

``counts`` in the state (``upload``, ``submit``, ``xp_create``) records how many of each write
reached the platform, so tests can assert that a retried tool did not upload twice.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from research_core.locks import file_lock
from research_core.platform.errors import (
    CreditsRefused,
    IdentityUnavailable,
    NotAvailable,
    PlatformError,
)
from research_core.platform.protocol import (
    AutoChampion,
    Credits,
    EpisodePage,
    Identity,
    League,
    Membership,
    PolicyVersion,
    Submission,
    WhoAmI,
    XpRequest,
)

FAULTS_ENV = "IDE_FAKE_FAULTS"
SOURCE_URL_ENV = "IDE_FAKE_SOURCE_URL"
PLAYER_ID_ENV = "SOFTMAX_PLAYER_ID"
PLAYER_CREDENTIAL_ENV = "SOFTMAX_PLAYER_CREDENTIAL"
USER_CREDENTIAL_ENV = "SOFTMAX_USER_CREDENTIAL"

DEFAULT_PLAYER_ID = "ply_fake"
USER_ID = "usr_fake"
USER_EMAIL = "fake-user@example.com"
COWORLD_ID = "cow_fake"
COWORLD_NAME = "fakegame"
COWORLD_VERSION = "1"
# Other Coworld versions the manifest answers for: an older fakegame version no league runs and
# a Coworld no league runs. The one league runs COWORLD_ID.
OLD_COWORLD_ID = "cow_fake_old"
LEAGUELESS_COWORLD_ID = "cow_leagueless"
COWORLD_VERSIONS = {
    COWORLD_ID: (COWORLD_NAME, COWORLD_VERSION),
    OLD_COWORLD_ID: (COWORLD_NAME, "0"),
    LEAGUELESS_COWORLD_ID: ("leaguelessgame", "1"),
}
GAME_ID = "game_fake"
LEAGUE_ID = "league_fake"
FAKE_GAME_SEATS = 2
LEAGUE_NAME = "Fake League"
DIVISION_ID = "div_fake"
DIVISION_NAME = "Fake Division"
DEFAULT_CREDITS = 100
DEFAULT_EPISODES = 2
CREDITS_PER_REQUEST = 1
LEAGUE_POLICIES = ("fake-alpha:v1", "fake-beta:v1", "fake-gamma:v1")
LEAGUE_HOUSE = "fake-house:v1"
LEAGUE_FIRST_SEED = 1000
POLICY_EPISODE_SEEDS = 10
DEFAULT_PARAMETERS = {"aggression": 3, "patience": 5}
HOUSE_STANDINGS = (("ply_house_a", "House A", 61.5, 12), ("ply_house_b", "House B", 48.25, 12))


def parse_faults(env: Mapping[str, str]) -> dict[str, str]:
    faults: dict[str, str] = {}
    for item in env.get(FAULTS_ENV, "").split(";"):
        if item.strip():
            name, _, value = item.partition("=")
            faults[name.strip()] = value.strip()
    return faults


def _short_hash(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _unit(*parts: object) -> float:
    """A deterministic float in [0, 1) from the parts."""
    return int(_short_hash(*parts), 16) / 16**12


def _policy_parameters(content: bytes) -> dict | None:
    """The ``parameters`` of a fakegame ``policy.json`` upload, or None for any other file."""
    try:
        policy = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    parameters = policy.get("parameters") if isinstance(policy, dict) else None
    return parameters if isinstance(parameters, dict) else None


def seeded_metrics(
    seed: int, index: int, slot: int, policy: str | None, parameters: dict | None
) -> dict:
    """One seat's metrics in a seeded episode (module docstring, "Seeded metrics")."""
    played = {**DEFAULT_PARAMETERS, **(parameters or {})}
    aggression, patience = played["aggression"], played["patience"]
    score_key = aggression if parameters is not None else policy
    errors_key = patience if parameters is not None else policy
    score = 50 + (_unit(seed, "board") - 0.5) * 8 + 0.5 * (aggression - 3)
    score += (_unit(seed, index, slot, "score", score_key) - 0.5) * 2
    errors = int(_unit(seed, index, slot, "errors", errors_key) * 3) + max(0, 8 - patience)
    return {"score": round(score, 4), "errors": errors}


def _unseeded_metrics(request_id: str, index: int, slot: int) -> dict:
    return {
        "score": int(_short_hash(request_id, index, slot, "score"), 16) % 100,
        "errors": int(_short_hash(request_id, index, slot, "errors"), 16) % 5,
    }


def _league_metrics(policy: str, seed: int) -> dict:
    """A league seat's metrics: a policy shift, the shared board term and per-policy noise."""
    score = 50 + (_unit(policy, "shift") - 0.5) * 4
    score += (_unit(seed, "board") - 0.5) * 8 + (_unit(policy, seed, "own") - 0.5) * 2
    return {"score": round(score, 4), "errors": int(_unit(policy, seed, "errors") * 5)}


def _roster(body: dict) -> tuple[str | None, list[int]]:
    """Our policy (the first roster entry) and the seats it sits in, in order."""
    roster = body["roster"]
    policy = roster[0]["player"].get("policy_ref")
    slots = [entry["slot"] for entry in roster if entry["player"].get("policy_ref") == policy]
    return policy, sorted(slots)


def _refuse_create(body: dict) -> None:
    """Refuse what ``POST /v2/experience-requests`` refuses (metta's experience_requests service).

    The roster's length is the player count and nothing adds seats; a league target picks the
    Coworld itself; only sampled seats need a league.
    """
    route = "POST /v2/experience-requests"
    league_id = (body.get("target") or {}).get("league_id")

    def refuse(detail: str, status: int = 400) -> PlatformError:
        return PlatformError(detail, route=route, status=status)

    if league_id and body.get("coworld_id"):
        raise refuse("coworld_id cannot be supplied with league or division target selectors")
    if not league_id and not body.get("coworld_id"):
        raise refuse("coworld_id or target is required")
    roster = body.get("roster") or []
    for entry in roster:
        kinds = [key for key, value in entry.get("player", {}).items() if value]
        if len(kinds) != 1 or kinds[0] not in ("policy_ref", "top_n", "random"):
            raise refuse("player must set exactly one of policy_ref, top_n or random", 422)
        if kinds[0] != "policy_ref" and not league_id:
            raise refuse("top_n and random roster participants require a league target")
    if len(roster) != FAKE_GAME_SEATS:
        raise refuse(f"game_config is invalid at tokens: this game seats {FAKE_GAME_SEATS}")
    pinned = [entry.get("slot", -1) for entry in roster if entry.get("slot", -1) != -1]
    if len(set(pinned)) != len(pinned) or any(slot >= len(roster) for slot in pinned):
        raise refuse("roster slots must be unique and within the player count")


class FakePlatform:
    """See the module docstring. ``state_path`` is the JSON file; ``env`` supplies identities
    and faults; ``clock`` is overridable for expiry tests."""

    def __init__(
        self,
        state_path: Path,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        env = os.environ if env is None else env
        self.state_path = Path(state_path)
        self._lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self.player_id = env.get(PLAYER_ID_ENV) or DEFAULT_PLAYER_ID
        self._agent_token = (env.get(PLAYER_CREDENTIAL_ENV) or "").startswith("cga_")
        self._user_credential = env.get(USER_CREDENTIAL_ENV) or None
        self._source_url_env = env.get(SOURCE_URL_ENV) or None
        self._faults = parse_faults(env)
        self._clock = clock

    def close(self) -> None:
        """The fake opens files only for individual operations."""

    # --- state plumbing ---------------------------------------------------------------------

    @staticmethod
    def _initial_state() -> dict:
        return {
            "credits": DEFAULT_CREDITS,
            "polls_to_complete": 1,
            "source_url": None,
            "source_commit": None,
            "counts": {"upload": 0, "submit": 0, "xp_create": 0},
            "fault_counters": {},
            "policies": {},
            "submissions": [],
            "memberships": {},
            "xp": {},
            "forum": {"posts": {}, "comments": {}, "votes": {}},
            "wiki": {},
            "idempotency": {},
        }

    def _load(self) -> dict:
        if not self.state_path.exists():
            return self._initial_state()
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self, state: dict) -> None:
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _operate(self, op: str, action: Callable[[dict], Any], *, persist: bool = True) -> Any:
        """Run ``action`` on the state under the lock, save it, then apply ``fail_after``."""
        if self._faults.get("platform") == "down":
            raise PlatformError("the platform is down (fake fault)", route=op, status=503)
        with file_lock(self._lock_path):
            state = self._load()
            result = action(state)
            if persist:
                self._save(state)
        if self._faults.get("fail_after") == op:
            raise RuntimeError(f"injected fault after {op}")
        return result

    def configure(
        self,
        *,
        source_url: str | None = None,
        source_commit: str | None = None,
        polls_to_complete: int | None = None,
        credits: int | None = None,
    ) -> dict:
        """Write test-controlled state (faults do not apply). Returns the four values."""
        values = {
            "source_url": source_url,
            "source_commit": source_commit,
            "polls_to_complete": polls_to_complete,
            "credits": credits,
        }
        with file_lock(self._lock_path):
            state = self._load()
            state.update({k: v for k, v in values.items() if v is not None})
            self._save(state)
        return {k: state[k] for k in values}

    def snapshot(self) -> dict:
        """A copy of the whole state for assertions (faults do not apply)."""
        with file_lock(self._lock_path):
            return self._load()

    def _now(self) -> datetime:
        return self._clock()

    def _iso(self, delta_s: float = 0) -> str:
        return (self._now() + timedelta(seconds=delta_s)).isoformat()

    # --- identities -------------------------------------------------------------------------

    def whoami(self, identity: Identity = "player") -> WhoAmI:
        if identity == "user":
            if not self._user_credential:
                raise IdentityUnavailable("user", route="GET /whoami")
            answer = WhoAmI(kind="user", subject_id=USER_ID, email=USER_EMAIL)
        elif self._faults.get("identity") == "rejected":
            raise PlatformError(
                "the platform did not accept the Player credential (fake fault)",
                route="GET /whoami",
                status=401,
            )
        elif self._faults.get("identity") == "user":
            answer = WhoAmI(kind="user", subject_id=USER_ID, email=USER_EMAIL)
        else:
            lifetime = float(self._faults.get("expires_in", 24 * 3600))
            answer = WhoAmI(
                kind="player",
                subject_id=self.player_id,
                owner_user_id=USER_ID,
                expires_at=None if self._agent_token else self._iso(lifetime),
            )
        return self._operate("whoami", lambda _state: answer, persist=False)

    def _require_identity(self, identity: Identity, route: str) -> None:
        """Writes re-check the identity first, as the real client does, so an identity fault
        stops a running tool at the platform boundary."""
        who = self.whoami(identity)
        if identity == "player" and who.kind != "player":
            raise PlatformError(
                "the Player credential belongs to a User, not a Player", route=route, status=403
            )
        if who.expires_at is not None and datetime.fromisoformat(who.expires_at) <= self._now():
            raise PlatformError(
                f"the Player credential expired at {who.expires_at}", route=route, status=401
            )

    # --- credits ----------------------------------------------------------------------------

    def _balance(self, state: dict) -> int:
        return int(self._faults["credits"]) if "credits" in self._faults else state["credits"]

    def _credits(self, state: dict) -> Credits:
        recent = sorted(state["xp"].values(), key=lambda r: r["created_at"])[-5:]
        return Credits(
            balance=float(self._balance(state)),
            max=float(DEFAULT_CREDITS),
            refill_amount=float(DEFAULT_CREDITS),
            next_refill_at=self._iso(24 * 3600),
            spent_this_period=float(state["counts"]["xp_create"] * CREDITS_PER_REQUEST),
            recent_requests=[
                {
                    "experience_request_id": row["id"],
                    "coworld": COWORLD_NAME,
                    "episodes": row["episodes"],
                    "compute_cost_credits": float(CREDITS_PER_REQUEST),
                    "last_activity_at": row["created_at"],
                }
                for row in recent
            ],
        )

    def credits(self) -> Credits:
        return self._operate("credits", self._credits, persist=False)

    # --- policies ---------------------------------------------------------------------------

    @staticmethod
    def _policy_version(row: dict) -> PolicyVersion:
        return PolicyVersion(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            content_hash=row["content_hash"],
            player_id=row["player_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _public(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "content_b64"}

    def upload_file_policy(
        self, *, name: str, content: bytes, tags: dict[str, str] | None = None
    ) -> PolicyVersion:
        route = "POST /stats/policies/files/upload"
        self._require_identity("player", route)
        self._upload_fault(route)
        content_hash = hashlib.sha256(content).hexdigest()
        parameters = _policy_parameters(content)

        def action(state: dict) -> PolicyVersion:
            version = 1 + sum(1 for row in state["policies"].values() if row["name"] == name)
            row = {
                "id": f"pv_{_short_hash(name, version)}",
                "name": name,
                "version": version,
                "content_hash": content_hash,
                "player_id": self.player_id,
                "created_at": self._iso(),
                "tags": dict(tags or {}),
                "parameters": parameters,
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
            state["policies"][row["id"]] = row
            state["counts"]["upload"] += 1
            return self._policy_version(row)

        return self._operate("upload", action)

    def upload_image_policy(
        self, *, name: str, archive_path: Path, tags: dict[str, str] | None = None
    ) -> PolicyVersion:
        self._require_identity("player", "container image upload")
        self._upload_fault("container image upload")
        inputs = self.upload_inputs(name=name, artifact=archive_path, image_archive=True)

        def action(state: dict) -> PolicyVersion:
            for row in state["policies"].values():
                if row.get("image_inputs") == inputs:
                    return self._policy_version(row)
            version = 1 + sum(row["name"] == name for row in state["policies"].values())
            row = {
                "id": f"pv_{_short_hash(name, version)}",
                "name": name,
                "version": version,
                "content_hash": None,
                "player_id": self.player_id,
                "created_at": self._iso(),
                "tags": dict(tags or {}),
                "parameters": None,
                "content_b64": base64.b64encode(archive_path.read_bytes()).decode("ascii"),
                "image_inputs": inputs,
            }
            state["policies"][row["id"]] = row
            state["counts"]["upload"] += 1
            return self._policy_version(row)

        return self._operate("upload", action)

    def _upload_fault(self, route: str) -> None:
        """``upload=503:N``: the first N uploads fail with a retryable 503 (counted in state)."""
        fault = self._faults.get("upload")
        if not fault or not fault.startswith("503"):
            return
        _, _, count = fault.partition(":")
        limit = int(count or 1)

        def action(state: dict) -> bool:
            fired = state["fault_counters"].get("upload_503", 0)
            if fired >= limit:
                return False
            state["fault_counters"]["upload_503"] = fired + 1
            return True

        if self._operate("upload_fault", action):
            raise PlatformError("upload failed (fake fault)", route=route, status=503)

    def upload_inputs(
        self, *, name: str, artifact: Path, image_archive: bool = False
    ) -> dict[str, Any]:
        """The shape coworld 0.1.67's ``policy_upload_inputs`` gives a single file."""
        if image_archive:
            from coworld.upload import policy_upload_inputs

            return policy_upload_inputs(
                artifact=artifact, name=name, player_id=self.player_id, image_archive=True
            )
        content = Path(artifact).read_bytes()
        return {
            "name": name,
            "player_id": self.player_id,
            "file_hash": hashlib.sha256(content).hexdigest(),
            "file_size": len(content),
            "attributes": {},
        }

    def lookup_policy_upload(self, inputs: dict[str, Any]) -> PolicyVersion | None:
        """The stored version with these exact inputs (name, player, hash, size), or None."""
        route = "POST /stats/policies/lookup"
        fault = self._faults.get("lookup")
        if fault == "503":
            raise PlatformError("lookup failed (fake fault)", route=route, status=503)
        if fault == "403":
            raise PlatformError("not this Player's upload (fake fault)", route=route, status=403)
        if fault == "unavailable":
            raise NotAvailable("the client has no lookup route (fake fault)", route=route)

        def action(state: dict) -> PolicyVersion | None:
            if fault == "not_found":
                return None
            for row in state["policies"].values():
                if inputs["attributes"] == {"kind": "docker-img"}:
                    if row.get("image_inputs") == inputs:
                        return self._policy_version(row)
                    continue
                size = len(base64.b64decode(row["content_b64"]))
                if (
                    row["name"] == inputs.get("name")
                    and row["player_id"] == inputs.get("player_id")
                    and row["content_hash"] == inputs.get("file_hash")
                    and size == inputs.get("file_size")
                ):
                    return self._policy_version(row)
            return None

        return self._operate("lookup_policy_upload", action, persist=False)

    def policy_versions(
        self,
        *,
        name: str | None = None,
        player_id: str | None = None,
        mine: bool = True,
        identity: Identity = "player",
    ) -> list[PolicyVersion]:
        if identity == "user":
            self.whoami("user")
        if mine and player_id is None:
            player_id = self.player_id

        def action(state: dict) -> list[PolicyVersion]:
            rows = [
                row
                for row in state["policies"].values()
                if (name is None or row["name"] == name)
                and (player_id is None or row["player_id"] == player_id)
            ]
            rows.sort(key=lambda row: (row["name"], row["version"]))
            return [self._policy_version(row) for row in rows]

        return self._operate("policy_versions", action, persist=False)

    def policy_file(self, policy_version_id: str, identity: Identity = "player") -> bytes:
        if identity == "user":
            self.whoami("user")

        def action(state: dict) -> bytes:
            route = f"GET /v2/policy-files/{policy_version_id}/download"
            row = state["policies"].get(policy_version_id)
            if row is None:
                raise self._not_found(route)
            if "image_inputs" in row:
                raise PlatformError("Policy version has no player file", route=route, status=409)
            return base64.b64decode(row["content_b64"])

        return self._operate("policy_file", action, persist=False)

    # --- leagues ----------------------------------------------------------------------------

    @staticmethod
    def _league() -> League:
        return League(
            id=LEAGUE_ID,
            name=LEAGUE_NAME,
            game_id=GAME_ID,
            coworld_id=COWORLD_ID,
            coworld_name=COWORLD_NAME,
            participation_url=f"https://fake.invalid/participate?league_id={LEAGUE_ID}",
            forum_markdown_url=f"https://fake.invalid/v2/forums/{LEAGUE_ID}.md",
            wiki_markdown_url=f"https://fake.invalid/v2/wikis/{LEAGUE_ID}/pages.md",
        )

    @staticmethod
    def _not_found(route: str) -> PlatformError:
        return PlatformError("not found", route=route, status=404)

    def leagues(self, *, game_id: str | None = None) -> list[League]:
        found = [self._league()] if game_id in (None, GAME_ID) else []
        return self._operate("leagues", lambda _state: found, persist=False)

    def league(self, league_id: str) -> League:
        if league_id != LEAGUE_ID:
            raise self._not_found(f"GET /v2/leagues/{league_id}")
        return self._operate("league", lambda _state: self._league(), persist=False)

    @staticmethod
    def _division() -> dict:
        return {"id": DIVISION_ID, "name": DIVISION_NAME, "level": 1, "league_id": LEAGUE_ID}

    def divisions(self, league_id: str) -> list[dict]:
        if league_id != LEAGUE_ID:
            raise self._not_found(f"GET /v2/divisions?league_id={league_id}")
        return self._operate("divisions", lambda _state: [self._division()], persist=False)

    def standings(self, division_id: str) -> list[dict]:
        if division_id != DIVISION_ID:
            raise self._not_found(f"GET /v2/divisions/{division_id}/leaderboard")

        def action(state: dict) -> list[dict]:
            self._settle(state)
            board = [(*row, None) for row in HOUSE_STANDINGS]
            ours = [
                m
                for m in state["memberships"].values()
                if m["player_id"] == self.player_id and m["status"] == "competing"
            ]
            if ours:
                champion = next((m for m in ours if m["is_champion"]), ours[-1])
                policy = state["policies"].get(champion["policy_version_id"])
                label = (
                    self._policy_version(policy).label if policy else champion["policy_version_id"]
                )
                board.append((self.player_id, "Autoresearch", 55.0, 1, label))
            board.sort(key=lambda row: -row[2])
            return [
                {
                    "division_id": DIVISION_ID,
                    "division_name": DIVISION_NAME,
                    "level": 1,
                    "rank": rank,
                    "player_id": player_id,
                    "player_name": name,
                    "score": score,
                    "rounds_played": rounds,
                    "policy_label": label,
                }
                for rank, (player_id, name, score, rounds, label) in enumerate(board, start=1)
            ]

        return self._operate("standings", action)

    # --- submissions and memberships ----------------------------------------------------------

    def _settle(self, state: dict) -> None:
        """Advance pending submissions by one poll; place them after ``polls_to_complete``
        polls; then apply the ``champion`` fault once."""
        for submission in state["submissions"]:
            if submission["status"] != "pending":
                continue
            submission["polls"] += 1
            if submission["polls"] < state["polls_to_complete"]:
                continue
            submission["status"] = "placed"
            membership = state["memberships"][submission["membership_id"]]
            membership["status"] = "competing"
            membership["substatus"] = None
            if self._becomes_champion(state, submission, membership):
                self._crown(state, membership)
        external = self._faults.get("champion")
        if external and state["fault_counters"].get("champion") != external:
            state["fault_counters"]["champion"] = external
            self._crown(state, self._external_membership(state, external))

    def _becomes_champion(self, state: dict, submission: dict, membership: dict) -> bool:
        mode = submission["auto_champion"]
        if mode == "always":
            return True
        if mode == "lineage":
            policy = state["policies"][membership["policy_version_id"]]
            for m in state["memberships"].values():
                champion_policy = state["policies"].get(m["policy_version_id"], {})
                if (
                    m["is_champion"]
                    and m["league_id"] == membership["league_id"]
                    and m["player_id"] == membership["player_id"]
                    and champion_policy.get("name") == policy["name"]
                    and champion_policy["version"] < policy["version"]
                ):
                    return True
            return False
        return False

    @staticmethod
    def _crown(state: dict, membership: dict) -> None:
        for other in state["memberships"].values():
            if (
                other["league_id"] == membership["league_id"]
                and other["player_id"] == membership["player_id"]
            ):
                other["is_champion"] = other["id"] == membership["id"]

    def _external_membership(self, state: dict, policy_version_id: str) -> dict:
        """The membership the ``champion`` fault crowns: an existing one for that policy
        version, else a new competing one, as if the user had submitted it on the website."""
        for membership in state["memberships"].values():
            if membership["policy_version_id"] == policy_version_id:
                return membership
        return self._membership_row(state, LEAGUE_ID, policy_version_id, status="competing")

    def _membership_row(
        self, state: dict, league_id: str, policy_version_id: str, *, status: str
    ) -> dict:
        membership_id = f"mem_{_short_hash(league_id, policy_version_id)}"
        row = state["memberships"].get(membership_id)
        if row is None:
            row = {
                "id": membership_id,
                "league_id": league_id,
                "division_id": DIVISION_ID,
                "policy_version_id": policy_version_id,
                "player_id": self.player_id,
                "status": status,
                "substatus": "qualifying" if status == "pending" else None,
                "is_champion": False,
                "created_at": self._iso(),
            }
            state["memberships"][membership_id] = row
        return row

    def _membership(self, state: dict, row: dict) -> Membership:
        policy = state["policies"].get(row["policy_version_id"]) or {
            "id": row["policy_version_id"],
            "name": row["policy_version_id"],
            "version": 1,
            "content_hash": None,
            "player_id": row["player_id"],
            "created_at": row["created_at"],
        }
        return Membership(
            id=row["id"],
            league_id=row["league_id"],
            policy_version=self._policy_version(policy),
            status=row["status"],
            division_id=row["division_id"],
            player_id=row["player_id"],
            substatus=row["substatus"],
            is_champion=row["is_champion"],
        )

    @staticmethod
    def _submission(row: dict) -> Submission:
        return Submission(
            id=row["id"],
            league_id=row["league_id"],
            policy_version_id=row["policy_version_id"],
            status=row["status"],
            membership_id=row["membership_id"],
            created_at=row["created_at"],
        )

    def memberships(
        self,
        *,
        league_id: str | None = None,
        player_id: str | None = None,
        policy_version_id: str | None = None,
        active_only: bool = False,
        mine: bool = False,
        champions_only: bool = False,
    ) -> list[Membership]:
        if mine:
            player_id = self.player_id

        def action(state: dict) -> list[Membership]:
            self._settle(state)
            return [
                self._membership(state, row)
                for row in state["memberships"].values()
                if (league_id is None or row["league_id"] == league_id)
                and (player_id is None or row["player_id"] == player_id)
                and (policy_version_id is None or row["policy_version_id"] == policy_version_id)
                and (not active_only or row["status"] in ("competing", "pending"))
                and (not champions_only or row["is_champion"])
            ]

        return self._operate("memberships", action)

    def submissions(
        self,
        *,
        league_id: str | None = None,
        player_id: str | None = None,
        policy_version_id: str | None = None,
        mine: bool = False,
    ) -> list[Submission]:
        if mine:
            player_id = self.player_id

        def action(state: dict) -> list[Submission]:
            self._settle(state)
            return [
                self._submission(row)
                for row in state["submissions"]
                if (league_id is None or row["league_id"] == league_id)
                and (player_id is None or row["player_id"] == player_id)
                and (policy_version_id is None or row["policy_version_id"] == policy_version_id)
            ]

        return self._operate("submissions", action)

    def submit(
        self,
        *,
        league_id: str,
        policy_version_id: str,
        auto_champion: AutoChampion = "always",
        player_id: str | None = None,
    ) -> Submission:
        route = "POST /v2/league-submissions"
        self._require_identity("player", route)
        if self._faults.get("submit") == "409":
            raise PlatformError("submission refused (fake fault)", route=route, status=409)
        if league_id != LEAGUE_ID:
            raise self._not_found(f"{route} league {league_id}")

        def action(state: dict) -> Submission:
            if policy_version_id not in state["policies"]:
                raise self._not_found(f"{route} policy version {policy_version_id}")
            membership = self._membership_row(state, league_id, policy_version_id, status="pending")
            ordinal = len(state["submissions"])
            row = {
                "id": f"sub_{_short_hash(policy_version_id, league_id, ordinal)}",
                "league_id": league_id,
                "policy_version_id": policy_version_id,
                "status": "pending",
                "membership_id": membership["id"],
                "player_id": player_id or self.player_id,
                "auto_champion": auto_champion,
                "polls": 0,
                "created_at": self._iso(),
            }
            state["submissions"].append(row)
            state["counts"]["submit"] += 1
            return self._submission(row)

        return self._operate("submit", action)

    def set_champion(self, membership_id: str) -> dict:
        return self.request("POST", f"/v2/league-policy-memberships/{membership_id}/champion")

    # --- XP requests ------------------------------------------------------------------------

    def _xp_status(self, state: dict, row: dict) -> str:
        if row.get("failed"):
            return "failed"
        if row["polls"] == 0:
            return "pending"
        return "running" if row["polls"] < state["polls_to_complete"] else "completed"

    def _xp(self, state: dict, row: dict) -> XpRequest:
        status = self._xp_status(state, row)
        return XpRequest(
            id=row["id"],
            status=status,
            episode_count=row["episodes"],
            completed_count=row["episodes"] if status == "completed" else 0,
            failed_count=row["episodes"] if status == "failed" else 0,
            coworld_id=row["coworld_id"],
            created_at=row["created_at"],
            error="injected failure" if status == "failed" else None,
            title=row["title"],
        )

    def xp_create(self, body: dict) -> XpRequest:
        route = "POST /v2/experience-requests"
        self._require_identity("player", route)
        _refuse_create(body)
        policy, slots = _roster(body)
        title = body.get("title")

        def action(state: dict) -> XpRequest:
            idempotency_key = body.get("idempotency_key")
            if idempotency_key is not None:
                for existing in state["xp"].values():
                    if existing["body"].get("idempotency_key") == idempotency_key:
                        return self._xp(state, existing)
            balance = self._balance(state)
            if self._faults.get("credits_refuse") == "1" or balance <= 0:
                raise CreditsRefused(
                    "not enough credits for this request",
                    balance=float(balance),
                    held=0.0,
                    next_refill_at=self._iso(24 * 3600),
                    route=route,
                )
            limit = self._faults.get("xp_limit")
            if limit is not None and len(state["xp"]) >= int(limit):
                raise PlatformError(
                    "pending experience request limit reached (fake fault)",
                    route=route,
                    status=429,
                )
            key = title or json.dumps(body, sort_keys=True)
            row = {
                "id": f"xreq_{_short_hash(key, len(state['xp']))}",
                "title": title,
                "coworld_id": body.get("coworld_id") or COWORLD_ID,
                "episodes": int(
                    body.get("num_episodes") or body.get("episode_count") or DEFAULT_EPISODES
                ),
                "seed": (body.get("game_config_overrides") or {}).get("seed"),
                "policy": policy,
                "slots": slots,
                "polls": 0,
                "created_at": self._iso(),
                "body": body,
            }
            state["xp"][row["id"]] = row
            state["credits"] = max(0, state["credits"] - CREDITS_PER_REQUEST)
            state["counts"]["xp_create"] += 1
            return self._xp(state, row)

        return self._operate("xp_create", action)

    def _xp_row(self, state: dict, xp_id: str, route: str) -> dict:
        row = state["xp"].get(xp_id)
        if row is None:
            raise self._not_found(route)
        return row

    def xp_progress(self, ids: tuple[str, ...]) -> list[dict[str, str]]:
        """Read simulated progress without advancing the fake platform's poll counters."""

        def progress(state: dict) -> list[dict[str, str]]:
            batches = []
            for xp_id in ids:
                row = self._xp_row(state, xp_id, "GET /v2/episode-requests/changes")
                status = self._xp_status(state, {**row, "polls": row["polls"] + 1})
                batches.append({"id": xp_id, "status": status})
            return batches

        return self._operate("xp_progress", progress, persist=False)

    def xp_get(self, xp_id: str) -> XpRequest:
        def action(state: dict) -> XpRequest:
            row = self._xp_row(state, xp_id, f"GET /v2/experience-requests/{xp_id}")
            row["polls"] += 1
            return self._xp(state, row)

        return self._operate("xp_get", action)

    def xp_list(self, *, mine: bool = True, limit: int = 50) -> list[XpRequest]:
        def action(state: dict) -> list[XpRequest]:
            rows = sorted(state["xp"].values(), key=lambda r: r["created_at"], reverse=True)
            return [self._xp(state, row) for row in rows[:limit]]

        return self._operate("xp_list", action, persist=False)

    def xp_episodes(self, xp_id: str) -> list[dict]:
        def action(state: dict) -> list[dict]:
            row = self._xp_row(state, xp_id, f"GET /v2/experience-requests/{xp_id}/episodes")
            status = self._xp_status(state, row)
            return [
                {
                    "id": f"{xp_id}-ep{index:03d}",
                    "index": index,
                    "experience_request_id": xp_id,
                    "coworld_id": row["coworld_id"],
                    "seed": row["seed"],
                    "status": status if status in ("completed", "failed") else "pending",
                    "policy_version_ids": [row["policy"]] if row["policy"] else [],
                }
                for index in range(row["episodes"])
            ]

        return self._operate("xp_episodes", action, persist=False)

    def _xp_results(self, state: dict, row: dict) -> dict:
        upload = state["policies"].get(row["policy"] or "") or {}
        parameters = upload.get("parameters")
        seed = row["seed"]
        episodes = []
        for index in range(row["episodes"]):
            for slot in row["slots"]:
                if seed is None:
                    metrics = _unseeded_metrics(row["id"], index, slot)
                else:
                    metrics = seeded_metrics(seed, index, slot, row["policy"], parameters)
                episode = {"episode": index, "seat": slot, **metrics}
                if seed is not None:
                    episode["seed"] = seed
                episodes.append(episode)
        results = {"request_id": row["id"], "episodes": episodes}
        if seed is not None:
            results["seed"] = seed
        return results

    def xp_download(self, xp_id: str, *, include: list[str], dest: Path) -> list[Path]:
        route = f"GET /v2/experience-requests/{xp_id}/download"

        def action(state: dict) -> dict:
            row = self._xp_row(state, xp_id, route)
            if self._xp_status(state, row) != "completed":
                raise PlatformError(
                    f"experience request {xp_id} is not completed", route=route, status=409
                )
            return self._xp_results(state, row)

        results = self._operate("xp_download", action, persist=False)
        wanted = set(include) or {"results"}
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        if "results" in wanted:
            path = dest / "results.json"
            path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
            written.append(path)
        if "replay" in wanted:
            path = dest / "replay.json"
            path.write_text(json.dumps(_replay(xp_id), sort_keys=True) + "\n")
            written.append(path)
        return written

    # --- episodes ---------------------------------------------------------------------------

    def _episode(self, state: dict, policies: tuple[str, ...], seed: int, index: int) -> dict:
        seats = []
        for slot, policy in enumerate(policies):
            upload = state["policies"].get(policy)
            if upload is not None:
                metrics = seeded_metrics(seed, 0, slot, policy, upload["parameters"])
            else:
                metrics = _league_metrics(policy, seed)
            seats.append({"episode": 0, "seat": slot, "seed": seed, **metrics})
        return {
            "id": f"ereq_fake_{_short_hash(*policies, seed)}",
            "round_id": f"round_fake_{index // 30:03d}",
            "coworld_id": COWORLD_ID,
            "league_id": LEAGUE_ID,
            "seed": seed,
            "status": "completed",
            "policy_version_ids": list(policies),
            "participants": [
                {"kind": "policy", "position": slot, "policy_version_id": policy}
                for slot, policy in enumerate(policies)
            ],
            "scores": [
                {"policy_version_id": policy, "score": seat["score"]}
                for policy, seat in zip(policies, seats, strict=True)
            ],
            "results": {"seed": seed, "episodes": seats},
            "created_at": self._iso(),
        }

    def _league_episodes(self, state: dict, limit: int) -> list[dict]:
        episodes = []
        for index in range(limit):
            seed = LEAGUE_FIRST_SEED + index // len(LEAGUE_POLICIES)
            policies = (LEAGUE_POLICIES[index % len(LEAGUE_POLICIES)], LEAGUE_HOUSE)
            episodes.append(self._episode(state, policies, seed, index))
        return episodes

    def _policy_episodes(self, state: dict, policy_version_id: str) -> list[dict]:
        return [
            self._episode(state, (policy_version_id, LEAGUE_HOUSE), LEAGUE_FIRST_SEED + i, i)
            for i in range(POLICY_EPISODE_SEEDS)
        ]

    def league_episodes(self, limit: int = 30) -> list[dict]:
        """The league's recent episodes (not in the protocol): three policies against the
        house policy per seed, for noise calibration."""
        return self._operate(
            "league_episodes", lambda state: self._league_episodes(state, limit), persist=False
        )

    def episodes_for_policy(
        self, policy_version_id: str, *, limit: int = 20, cursor: str | None = None
    ) -> EpisodePage:
        rows = self._operate(
            "episodes_for_policy",
            lambda state: self._policy_episodes(state, policy_version_id),
            persist=False,
        )
        offset = int(cursor) if cursor else 0
        end = offset + limit
        return EpisodePage(rows[offset:end], str(end) if end < len(rows) else None)

    def _find_episode(self, state: dict, episode_request_id: str) -> dict:
        candidates = self._league_episodes(state, 30)
        for policy_version_id in state["policies"]:
            candidates.extend(self._policy_episodes(state, policy_version_id))
        for episode in candidates:
            if episode["id"] == episode_request_id:
                return episode
        raise self._not_found(f"GET /v2/episode-requests/{episode_request_id}")

    def episode(self, episode_request_id: str) -> dict:
        return self._operate(
            "episode", lambda state: self._find_episode(state, episode_request_id), persist=False
        )

    def episode_artifact(self, episode_request_id: str, artifact_type: str) -> bytes:
        episode = self.episode(episode_request_id)
        if artifact_type == "results":
            return json.dumps(episode["results"], sort_keys=True).encode()
        if artifact_type == "replay":
            return json.dumps(_replay(episode_request_id), sort_keys=True).encode()
        raise self._not_found(
            f"GET /v2/episode-requests/{episode_request_id}/artifacts/{artifact_type}"
        )

    # --- Coworld ----------------------------------------------------------------------------

    def _manifest(self, state: dict, coworld_id: str = COWORLD_ID) -> dict:
        name, version = COWORLD_VERSIONS[coworld_id]
        return {
            "id": coworld_id,
            "name": name,
            "version": version,
            "manifest": {
                "variants": [{"id": "default", "name": "Default", "game_config": {}}],
                "game": {
                    "config_schema": {
                        "type": "object",
                        "required": ["tokens"],
                        "properties": {
                            "tokens": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": FAKE_GAME_SEATS,
                                "maxItems": FAKE_GAME_SEATS,
                            }
                        },
                    },
                    "player_runtime": "game-hosted",
                    "runnable": {
                        "source_url": self._source_url_env or state["source_url"],
                        "commit": state["source_commit"],
                    },
                },
            },
        }

    def coworld_manifest(self, coworld_id: str) -> dict:
        if coworld_id not in COWORLD_VERSIONS:
            raise self._not_found(f"GET /v2/coworlds/{coworld_id}")
        return self._operate(
            "coworld_manifest", lambda state: self._manifest(state, coworld_id), persist=False
        )

    # --- forum --------------------------------------------------------------------------------

    def _author(self, identity: Identity) -> dict:
        if identity == "user":
            return {"user_id": USER_ID, "name": "Fake User"}
        return {"player_id": self.player_id, "name": "Autoresearch"}

    def _idempotent(self, state: dict, key: str, kind: str) -> dict | None:
        """The object an idempotency key already produced, or None."""
        stored = state["idempotency"].get(key)
        if stored is None:
            return None
        if kind == "wiki_edit":
            return stored
        return self._public(state["forum"][kind][stored])

    def forum_list(self, slug: str, *, sort: str = "hot", limit: int = 20) -> list[dict]:
        def action(state: dict) -> list[dict]:
            posts = [p for p in state["forum"]["posts"].values() if p.get("forum") == slug]
            if sort == "new":
                posts.sort(key=lambda p: p["created_at"], reverse=True)
            else:
                posts.sort(key=lambda p: (p["score"], p["created_at"]), reverse=True)
            return [self._public(p) for p in posts[:limit]]

        return self._operate("forum_list", action, persist=False)

    def forum_read(self, post_id: str) -> str:
        def action(state: dict) -> str:
            post = state["forum"]["posts"].get(post_id)
            if post is None:
                raise self._not_found(f"GET /v2/posts/{post_id}.md")
            lines = [f"# {post['title']}", "", post["body"], ""]
            comments = [c for c in state["forum"]["comments"].values() if c["post_id"] == post_id]
            if comments:
                lines.append("## Comments")
                lines.append("")
                for comment in sorted(comments, key=lambda c: c["created_at"]):
                    lines.append(f"- ({comment['author']['name']}) {comment['body']}")
            return "\n".join(lines) + "\n"

        return self._operate("forum_read", action, persist=False)

    def forum_search(self, query: str, *, slug: str | None = None) -> list[dict]:
        needle = query.lower()

        def action(state: dict) -> list[dict]:
            return [
                {"post": self._public(p), "rank": 1.0}
                for p in state["forum"]["posts"].values()
                if needle in p["title"].lower() or needle in p["body"].lower()
            ]

        return self._operate("forum_search", action, persist=False)

    def forum_post(
        self,
        *,
        title: str,
        body: str,
        idempotency_key: str,
        slug: str | None = None,
        identity: Identity = "player",
    ) -> dict:
        self._require_identity(identity, "POST /v2/posts")

        def action(state: dict) -> dict:
            existing = self._idempotent(state, idempotency_key, "posts")
            if existing is not None:
                return existing
            post = {
                "id": f"post_{_short_hash(idempotency_key)}",
                "forum": slug,
                "title": title,
                "body": body,
                "content_format": "markdown",
                "author": self._author(identity),
                "identity": identity,
                "score": 0,
                "vote_count": 0,
                "created_at": self._iso(),
            }
            state["forum"]["posts"][post["id"]] = post
            state["idempotency"][idempotency_key] = post["id"]
            return dict(post)

        return self._operate("forum_post", action)

    def forum_comment(
        self,
        post_id: str,
        *,
        body: str,
        idempotency_key: str,
        parent_id: str | None = None,
        identity: Identity = "player",
    ) -> dict:
        route = f"POST /v2/posts/{post_id}/comments"
        self._require_identity(identity, route)

        def action(state: dict) -> dict:
            existing = self._idempotent(state, idempotency_key, "comments")
            if existing is not None:
                return existing
            if post_id not in state["forum"]["posts"]:
                raise self._not_found(route)
            comment = {
                "id": f"cmt_{_short_hash(idempotency_key)}",
                "post_id": post_id,
                "parent_id": parent_id,
                "body": body,
                "author": self._author(identity),
                "identity": identity,
                "score": 0,
                "created_at": self._iso(),
            }
            state["forum"]["comments"][comment["id"]] = comment
            state["idempotency"][idempotency_key] = comment["id"]
            return dict(comment)

        return self._operate("forum_comment", action)

    def forum_vote(self, post_id: str, value: int, comment_id: str | None = None) -> dict:
        route = f"PUT /v2/posts/{post_id}/vote"
        self._require_identity("player", route)

        def action(state: dict) -> dict:
            table = state["forum"]["comments" if comment_id else "posts"]
            target = table.get(comment_id or post_id)
            if target is None:
                raise self._not_found(route)
            votes = state["forum"]["votes"].setdefault(target["id"], {})
            votes[self.player_id] = value
            target["score"] = sum(votes.values())
            target["vote_count"] = sum(1 for v in votes.values() if v)
            return {
                "post_id": post_id,
                "comment_id": comment_id,
                "value": value,
                "score": target["score"],
                "vote_count": target["vote_count"],
            }

        return self._operate("forum_vote", action)

    # --- wiki -------------------------------------------------------------------------------

    @staticmethod
    def _page_view(wiki: str, page: str, row: dict) -> dict:
        return {
            "wiki": wiki,
            "page": page,
            "page_id": row["page_id"],
            "title": row["title"],
            "body": row["body"],
            "revision_id": row["revision_id"],
            "created_at": row["created_at"],
        }

    def wiki_read(self, wiki: str, page: str) -> dict:
        def action(state: dict) -> dict:
            row = state["wiki"].get(wiki, {}).get(page)
            if row is None:
                raise self._not_found(f"GET /v2/wikis/{wiki}/pages/{page}")
            # The platform nests the current text under its revision.
            current = {"id": row["revision_id"], "body": row["body"]}
            return {**self._page_view(wiki, page, row), "current_revision": current}

        return self._operate("wiki_read", action, persist=False)

    def wiki_search(self, wiki: str, query: str) -> list[dict]:
        needle = query.lower()

        def action(state: dict) -> list[dict]:
            return [
                self._page_view(wiki, page, row)
                for page, row in state["wiki"].get(wiki, {}).items()
                if needle in row["title"].lower() or needle in row["body"].lower()
            ]

        return self._operate("wiki_search", action, persist=False)

    def wiki_history(self, page_id: str) -> list[dict]:
        def action(state: dict) -> list[dict]:
            for pages in state["wiki"].values():
                for row in pages.values():
                    if row["page_id"] == page_id:
                        return list(reversed(row["history"]))
            raise self._not_found(f"GET /v2/wiki-pages/{page_id}/revisions")

        return self._operate("wiki_history", action, persist=False)

    def wiki_edit(
        self,
        wiki: str,
        page: str,
        *,
        title: str,
        body: str,
        base_revision_id: str | None,
        idempotency_key: str,
        note: str | None = None,
        identity: Identity = "player",
    ) -> dict:
        """Compare-and-swap: a stale or missing ``base_revision_id`` on a page that exists returns
        ``{"conflict": True, "current_revision_id", "current_body"}`` instead of writing. A missing
        page is created by an edit with no base; one with a base is a 404."""
        self._require_identity(identity, f"PUT /v2/wikis/{wiki}/pages/{page}")

        def action(state: dict) -> dict:
            existing = self._idempotent(state, idempotency_key, "wiki_edit")
            if existing is not None:
                return existing
            pages = state["wiki"].setdefault(wiki, {})
            row = pages.get(page)
            if row is not None and row["revision_id"] != base_revision_id:
                return {
                    "conflict": True,
                    "page_id": row["page_id"],
                    "current_revision_id": row["revision_id"],
                    "current_body": row["body"],
                }
            if row is None:
                if base_revision_id is not None:
                    raise self._not_found(f"PUT /v2/wikis/{wiki}/pages/{page}")
                row = pages[page] = {
                    "page_id": f"wpage_{_short_hash(wiki, page)}",
                    "title": title,
                    "body": "",
                    "revision_id": None,
                    "created_at": self._iso(),
                    "history": [],
                }
            revision = {
                "revision_id": f"wrev_{_short_hash(row['page_id'], len(row['history']))}",
                "page_id": row["page_id"],
                "parent_revision_id": row["revision_id"],
                "author": self._author(identity),
                "identity": identity,
                "note": note,
                "body": body,
                "created_at": self._iso(),
            }
            row.update(title=title, body=body, revision_id=revision["revision_id"])
            row["history"].append(revision)
            result = {
                "conflict": False,
                "page_id": row["page_id"],
                "revision_id": revision["revision_id"],
                "identity": identity,
            }
            state["idempotency"][idempotency_key] = result
            return dict(result)

        return self._operate("wiki_edit", action)

    # --- the generic request ----------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        identity: Identity = "player",
    ) -> dict:
        """The ``softmax_api`` escape hatch over a small route table: the champion route and
        GETs of stored objects. Anything else is a 404."""
        method = method.upper()
        route = f"{method} {path}"
        # Identity checks happen before the lock: file_lock is not re-entrant.
        who = self.whoami(identity) if method == "GET" and identity == "user" else None
        if method != "GET":
            self._require_identity(identity, route)
        parts = path.removeprefix("/observatory").strip("/").split("/")
        if parts == ["whoami"] and who is None:
            who = self.whoami(identity)

        def action(state: dict) -> dict:
            match (method, parts):
                case ("GET", ["whoami"]):
                    return {"subject_type": who.kind, "subject_id": who.subject_id}
                case ("GET", ["usage", "me", "credits"]):
                    return asdict(self._credits(state))
                case ("GET", ["v2", "leagues"]):
                    return {"entries": [asdict(self._league())]}
                case ("GET", ["v2", "leagues", league_id]) if league_id == LEAGUE_ID:
                    return asdict(self._league())
                case ("GET", ["v2", "divisions", division_id]) if division_id == DIVISION_ID:
                    return self._division()
                case ("GET", ["v2", "coworlds", coworld_id]) if coworld_id == COWORLD_ID:
                    return self._manifest(state)
                case ("GET", ["v2", "policy-versions", pv]) if pv in state["policies"]:
                    return self._public(state["policies"][pv])
                case ("GET", ["v2", "league-submissions", sid]):
                    for row in state["submissions"]:
                        if row["id"] == sid:
                            return dict(row)
                case ("GET", ["v2", "league-policy-memberships", mid]):
                    if mid in state["memberships"]:
                        return dict(state["memberships"][mid])
                case ("POST", ["v2", "league-policy-memberships", mid, "champion"]):
                    membership = state["memberships"].get(mid)
                    if membership is not None:
                        self._crown(state, membership)
                        return {**membership, "identity": identity}
                case ("GET", ["v2", "experience-requests", xid]) if xid in state["xp"]:
                    row = state["xp"][xid]
                    return {**asdict(self._xp(state, row)), "body": row["body"]}
            raise self._not_found(route)

        return self._operate("request", action, persist=method != "GET")


def _replay(episode_or_request_id: str, ticks: int = 5) -> dict:
    """A tiny deterministic per-tick summary standing in for a replay file."""
    return {
        "id": episode_or_request_id,
        "ticks": [
            {"tick": tick, "score": round(_unit(episode_or_request_id, "tick", tick) * 10, 4)}
            for tick in range(ticks)
        ],
    }
