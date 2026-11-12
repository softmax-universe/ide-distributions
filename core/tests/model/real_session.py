"""Model tool calls through a real Session: a real Hermes gateway serving both profiles.

The direct tests (``write_actions.py``) ask OpenRouter themselves. Here the model is called by
Hermes, so the request is the real one: the stored system prompt, the frozen tool list, Hermes's
own request shaping, the plugin's hooks, the real tools answering on the fake platform. The
judging is the same (``write_actions.is_judged`` and ``judge``), read from the Session's stored
messages. A run is stopped as soon as the judged call is seen, except that a forced turn or an
ordinary case goes on after an invalid one (``MAX_LATER_ATTEMPTS``). What is judged is the call a
model composes, not what the real tool then answered: that also depends on the seeded records (a
Researcher's Session holds its Task but has no worker execution, so the real ``task_ask`` refuses
it). Only an ordinary case also reads the answer, as below.
``test_real_session.py`` runs the cases.

A recovery case needs refused calls already in the history, and the runs API takes only text
history, so those rows are written into the profile's ``state.db`` before the first run, in the
shape Hermes stores them.

That history also differs from a refusal inside one turn: the run's input adds a user message
after the refused calls. To measure one turn, ``test_mid_turn_recovery.py`` gives the gateway a
``ForcingProxy``, which answers a conversation's first requests with the broken calls, so the
refusals, hooks and loop guard are the real ones. Both are worth knowing; they do not measure the
same thing, and the mid-turn rate is much lower without ``repeats.without_repeated_refusals``.

A forced refusal invents a call the model did not make: it is fault injection, not a production
rate. ``ORDINARY_CASES`` (``test_ordinary_creation.py``) force nothing. A plain request asks for a
Task, every refusal is the real tool's answer to a call the model chose, and the run goes on after
a bad call as a forced turn does. Its instructions name only seeded records, so here the real tool's
answer counts as well: a call the judging passes and the tool refuses is invalid. These runs have
a host label of their own and are in no ``CASES`` list, so no ``test_the_agreed_rate`` counts them.

No ``research serve`` runs: its coordinator would start Researchers for the Tasks these cases
create, and a chat turn reaches the gateway the same way. Each case gets its own Coworld with
freshly seeded records (``seed_records``). Sharing one lets runs answer each other: with a shared
store a repeat of the claim case rightly finds the earlier run's claim and writes nothing, which
measured 0/5 on correct behaviour. Workspace records and settings are still shared, so a case that
writes one (``<run>`` in its instruction) is made different for each run.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from research_core import layout
from research_core.platform.fake import FakePlatform
from research_core.plugin.repeats import refusal_of
from research_core.store import claims, coworlds, goals, sessions, tasks
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.tools import catalog
from research_core.watcher.hermes_api import HermesRuns, RunsUnavailable, mint_session_id
from tests.fixtures.fakegame_repo import COWORLD_ID, make_fakegame_repo
from tests.hermes.test_spike_s1 import REPO, Host, SecretEnv, free_port, hermes_python, run
from tests.model import harness, write_actions
from tests.model.forcing_proxy import ForcedCall, ForcingProxy
from tests.model.write_actions import Case

HOST_LABEL = "real session"
MID_TURN_LABEL = "real session, one turn"
ORDINARY_LABEL = "real session, unforced"
RUN_TIMEOUT_S = 600
STOP_TIMEOUT_S = 60
POLL_S = 2
RUN_NUMBERS = itertools.count(100)
# Calls before the judged one, as a Session makes them: reading a skill or the record first is
# normal here, so this is wider than the direct tests' limit.
MAX_DETOURS = 12
# How many more judged calls (``write_actions.is_judged``: any call to the tool that is not a read)
# a forced turn may make after a bad first one. Its other calls do not count, and Hermes's loop
# guard can end the turn first.
MAX_LATER_ATTEMPTS = 3
FAKE_PLAYER_ID = "ply_model_tests"
# How a run's error text gives a host's refusal: the SDK's "Error code: 400", or "HTTP 400".
REFUSED_REQUEST = re.compile(r"(?:Error code:?|HTTP)\s*(4\d\d)\b")
GOAL = "G-1"
TASK = "T-1"  # open, for the Orchestrator's cases
HELD_TASK = "T-2"  # held by the Researcher's Session
CLAIM = "C-1"
EARLIER_DOC = "https://softmax.com/leagues/L-7 (retrieved 2026-10-20): champion pv-39"

TASK_CREATE = write_actions.TASK_CREATE
ORCHESTRATOR_CASES = [
    Case("orchestrator", "claim", "write", write_actions.CHAMPION_CLAIM),
    Case(
        "orchestrator",
        "claim",
        "add_evidence",
        f"Add league page evidence (type doc, ref '{write_actions.CHAMPION_DOC}') to claim "
        f"{CLAIM}, supporting it.",
    ),
    Case(
        "orchestrator",
        "goal",
        "create",
        "Create a Goal 'Reach the top 3 of league L-7': done when our policy ranks in the top 3; "
        "I am asking for it now.",
    ),
    Case("orchestrator", "task", "create", TASK_CREATE),
    Case(
        "orchestrator",
        "task",
        "assign",
        f"Assign Task {TASK} to the researcher profile: start from league L-7's standings, "
        "because it is the only worker.",
    ),
    Case(
        "orchestrator",
        "task",
        "message",
        f"Tell the Researcher on Task {TASK}: use league L-7's practice division.",
    ),
    Case(
        "orchestrator",
        "directive",
        "add",
        "From now on, never submit to league L-<run> without asking me first.",
    ),
    # A number of its own per run: asking for the value already set leaves nothing to change.
    Case("orchestrator", "settings", "update", "Set max_researchers to <run>, please."),
]
RESEARCHER_CASES = [
    Case("researcher", "claim", "write", write_actions.CHAMPION_CLAIM),
    Case(
        "researcher",
        "task",
        "ask",
        f"Ask the Orchestrator on Task {HELD_TASK} whether to use the practice or ranked division.",
    ),
    Case(
        "researcher",
        "task",
        "wait",
        f"Mark Task {HELD_TASK} as waiting for xp request req-1c0 to finish.",
    ),
]
RECOVERY_CASES = [
    Case(role, tool, action, instruction, refused=refused, refusals=refusals)
    for role, tool, action, instruction, refused in (
        (
            "orchestrator",
            "claim",
            "write",
            write_actions.CHAMPION_CLAIM,
            write_actions.INCIDENT_CLAIM_ARGS,
        ),
        (
            "researcher",
            "claim",
            "write",
            write_actions.CHAMPION_CLAIM,
            write_actions.INCIDENT_CLAIM_ARGS,
        ),
        (
            "orchestrator",
            "task",
            "create",
            TASK_CREATE,
            {"action": "create", "title": "Why do we lose early fights?", "goal": GOAL},
        ),
        (
            "orchestrator",
            "task",
            "message",
            f"Tell the Researcher on Task {TASK}: use league L-7's practice division.",
            {"action": "message", "task": TASK},
        ),
    )
    for refusals in write_actions.RECOVERY_DEPTHS
]
CASES = [*ORCHESTRATOR_CASES, *RESEARCHER_CASES, *RECOVERY_CASES]
# Requests as a user words them: no tool, action or field named, and the type and the shape of the
# call left to the model. The last invites end_condition, which a call must send with end_detail.
ORDINARY_CASES = [
    Case("orchestrator", "task", "create", instruction, ordinary=kind)
    for kind, instruction in (
        (
            "research",
            f"We keep losing early fights, which is what Goal {GOAL} is about. Get someone to "
            "study 20 replays of lost games and tell us the three most common causes.",
        ),
        (
            "maintenance",
            "The build on main has been failing since yesterday's toolchain bump. Get someone on "
            "it soon: this one is urgent.",
        ),
        (
            "done-when",
            f"Under Goal {GOAL}, have a Researcher compare our opening with the top three "
            "policies of league L-7. It is finished once there is a written comparison of their "
            "first 50 steps.",
        ),
    )
]


@dataclass
class RealGateway:
    """Both profiles installed and prepared in ``root``, as hosting sets them up, on one gateway
    with the fake platform and the seeded records."""

    root: Path
    hermes_bin: Path
    # Not in the repr: pytest prints a failing test's fixtures with it.
    openrouter_key: str = field(repr=False)
    model: str
    host: Host | None = field(default=None, repr=False)
    runs: HermesRuns | None = None
    # When set, recovery cases are forced in one turn through this proxy (``forcing_proxy.py``),
    # rather than seeded before a new user message.
    forcing: ForcingProxy | None = field(default=None, repr=False)

    def start(self) -> None:
        self.host = Host(
            hermes_bin=self.hermes_bin,
            home=self.root / "host",
            workspace=self.root / "workspace",
            port=free_port(),
            key=secrets.token_hex(16),
            openrouter_key=self.openrouter_key,
            log=self.root / "gateway.log",
            dotenv_extra={
                "IDE_PLATFORM": "fake",
                "SOFTMAX_PLAYER_ID": FAKE_PLAYER_ID,
                "IDE_RESEARCHER_PROFILES": "researcher",
                **({"OPENROUTER_BASE_URL": self.forcing.start()} if self.forcing else {}),
            },
        )
        self.host.home.mkdir(parents=True)
        self.host.write_host_dotenv()
        for role in catalog.ROLES:
            self._install(role)
        self._seed()
        self.host.start_gateway()
        self.runs = HermesRuns.from_profile(
            self.host.profile("orchestrator"), researcher_profiles=("researcher",)
        )

    def stop(self) -> None:
        """Stop the gateway and remove the ``.env`` files: pytest keeps its temporary folders,
        and these hold the OpenRouter key."""
        self.host.stop_gateway()
        for dotenv in self.host.home.rglob(".env"):
            dotenv.unlink()

    def env(self, role: str = "orchestrator") -> dict[str, str]:
        return SecretEnv({**self.host.profile_env(role), **self.host.dotenv_extra})

    def state_db(self, role: str) -> Path:
        return self.host.profile(role) / "state.db"

    def _install(self, role: str) -> None:
        hermes = self.hermes_bin
        install_env = SecretEnv({**os.environ, "HERMES_HOME": str(self.host.home)})
        result = run(
            [hermes, "profile", "install", REPO / role, "--name", role, "--yes"], install_env
        )
        assert result.returncode == 0, result.stdout + result.stderr
        self.host.write_dotenv(role)
        script = self.host.profile(role) / "scripts" / "prepare.py"
        argv = [hermes_python(hermes), script, "--source", REPO / role, "--hermes", hermes]
        result = run(argv, self.env(role), timeout=900)
        assert result.returncode == 0, f"prepare {role}:\n{result.stdout}\n{result.stderr}"
        # One model only: a fallback's valid call would count as this model's.
        for key, value in (("model.default", self.model), ("fallback_providers", "[]")):
            result = run([hermes, "config", "set", key, value], self.env(role))
            assert result.returncode == 0, result.stdout + result.stderr

    def _seed(self) -> None:
        env = self.env()
        ws = layout.from_env(env)
        ws.ensure_dirs()
        source, commit = make_fakegame_repo(self.root / "upstream")
        FakePlatform(ws.state / "fake_platform.json", env).configure(
            source_url=source.as_uri(), source_commit=commit
        )
        with Store.open(ws.store_path, kind="workspace") as wstore:
            # A running watcher's mark, so the briefing does not say background work is down.
            wstore.set_meta("heartbeat", datetime.now(UTC).isoformat())

    def open_session(self, role: str) -> str:
        """A native session and its Session row, bound to a new seeded Coworld, as the seam or a
        wake makes them. A Researcher's Session holds the seeded Task."""
        session_id = mint_session_id()
        ws = layout.from_env(self.env(role))
        actor = Actor.harness()
        coworld_id = f"{COWORLD_ID}_{secrets.token_hex(4)}"
        task_id = int(HELD_TASK.split("-")[1]) if role == "researcher" else None
        with Store.open(ws.store_path, kind="workspace") as wstore:
            coworlds.register(wstore, actor, coworld_id, folder=layout.folder_name_for(coworld_id))
            sessions.register(
                wstore,
                actor,
                session_id=session_id,
                profile=role,
                kind="chat" if role == "orchestrator" else "task",
                coworld_id=coworld_id,
                task_id=task_id,
                session_context={"contract_version": 1, "scope": "coworld", "coworld": coworld_id},
            )
        coworld = ws.coworld(coworld_id)
        coworld.ensure_dirs()
        with Store.open(coworld.store_path, kind="coworld") as store:
            holder = session_id if role == "researcher" else "another-session"
            seed_records(store, actor, holder)
        self.runs.create_session(session_id, profile=role)
        return session_id

    def seed_refusals(self, session_id: str, case: Case, instruction: str) -> None:
        """Write the case's first user turn and refused calls into ``state.db``. The instruction
        is stored here, so the run's input only asks the model to continue."""
        schemas = harness.ShippedSchemas()
        rows = [{"role": "user", "content": instruction}]
        rows += write_actions.refused_history(case, schemas)
        tool_name = schemas.outgoing(case.role, case.tool, case.refused)[0]
        connection = sqlite3.connect(self.state_db(case.role), timeout=30)
        try:
            for index, row in enumerate(rows):
                tool_calls = row.get("tool_calls")
                connection.execute(
                    "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, "
                    "tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        row["role"],
                        row.get("content"),
                        row.get("tool_call_id"),
                        json.dumps(tool_calls) if tool_calls else None,
                        tool_name if row["role"] == "tool" else None,
                        time.time() + index * 0.001,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def stored_prompt_and_tools(self, role: str, session_id: str) -> harness.SessionContext:
        """The system prompt Hermes stored for a Session, and its frozen Hermes tools."""
        connection = sqlite3.connect(f"file:{self.state_db(role)}?mode=ro", uri=True)
        try:
            prompt, pin = connection.execute(
                "SELECT sp.prompt, tp.prompt FROM sessions s "
                "JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
                "JOIN system_prompts tp ON tp.hash = s.tool_names WHERE s.id = ?",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
        ide_tools = {spec.name for spec in catalog.tools_for(role)}
        hermes_tools = [
            tool for tool in json.loads(pin)["tools"] if tool["function"]["name"] not in ide_tools
        ]
        return harness.SessionContext(system=prompt, hermes_tools=hermes_tools)

    def run_to_completion(self, role: str, session_id: str, text: str) -> None:
        run_id = self.runs.start(
            profile=role, session_id=session_id, text=text, idempotency_key=f"capture:{session_id}"
        )
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while not self.runs.status(run_id, profile=role).terminal:
            assert time.monotonic() < deadline, f"run {run_id} did not end"
            time.sleep(POLL_S)

    def force_refusals(self, case: Case, text: str) -> str:
        """Script the case's refused calls; returns the message whose conversation gets them."""
        marked = f"{text} (request {next(RUN_NUMBERS)})"
        forced = harness.ShippedSchemas().outgoing(case.role, case.tool, case.refused)
        self.forcing.scripts[marked] = [ForcedCall(*forced)] * case.refusals
        return marked

    def run_case(self, model: str, case: Case) -> harness.Result:
        """Start a run for the case and read the Session until the judged call shows up."""
        label = ORDINARY_LABEL if case.ordinary else MID_TURN_LABEL if self.forcing else HOST_LABEL
        result = harness.Result(model, label, case.label, "wrong_tool")
        session_id = self.open_session(case.role)
        text = case.instruction.replace("<run>", str(next(RUN_NUMBERS)))
        seeded = 0
        if case.refused is not None:
            seeded = 1 + 2 * case.refusals
            if self.forcing:
                text = self.force_refusals(case, text)
            else:
                self.seed_refusals(session_id, case, case.instruction)
                text = "Please go on with that request."
        started = time.monotonic()
        run_id = self.runs.start(
            profile=case.role,
            session_id=session_id,
            text=text,
            idempotency_key=f"model-test:{session_id}",
        )
        try:
            while time.monotonic() - started < RUN_TIMEOUT_S:
                status = self.runs.status(run_id, profile=case.role)
                stored = self.runs.session_messages(session_id, profile=case.role)
                if self._judge_new_calls(case, stored[seeded:], result):
                    break
                if status.terminal:
                    self._judge_end(status, stored[seeded:], result)
                    break
                time.sleep(POLL_S)
            else:
                self._host_error(result, f"no judged call in {RUN_TIMEOUT_S} s")
        except RunsUnavailable as error:
            self._host_error(result, f"the gateway stopped answering: {error}")
        finally:
            self._stop_and_wait(case.role, run_id)  # a run left going keeps spending
        result.replies = self._billed_replies(case.role, session_id, time.monotonic() - started)
        return result

    @staticmethod
    def _host_error(result: harness.Result, detail: str) -> None:
        """A run that failed before its verdict counts for nothing. An invalid verdict stands: a
        turn that goes on after a bad call and then hangs still made that call."""
        if result.outcome != "invalid":
            result.outcome, result.detail = "host_error", detail

    def _stop_and_wait(self, role: str, run_id: str) -> None:
        """Stop the run and let it end, so its usage is stored before it is read. A gateway that
        no longer answers has nothing left to stop."""
        try:
            if not self.runs.status(run_id, profile=role).terminal:
                self.runs.stop(run_id, profile=role)
            deadline = time.monotonic() + STOP_TIMEOUT_S
            while not self.runs.status(run_id, profile=role).terminal:
                if time.monotonic() > deadline:
                    return
                time.sleep(POLL_S)
        except RunsUnavailable:
            return

    def _judge_new_calls(self, case: Case, stored: list[dict], result: harness.Result) -> bool:
        """True when the run can stop: the judged call settled it, or there were too many other
        calls first. After an invalid call, a forced turn or an ordinary case stops at a later
        valid one or at ``MAX_LATER_ATTEMPTS``."""
        detours: list[str] = []
        judged = False
        later: list[str] = []
        for message in stored:
            for raw in message.get("tool_calls") or []:
                call = harness._call(raw)
                name, args = harness.ShippedSchemas().canonical(
                    case.role, call.name, call.arguments or {}
                )
                if write_actions.is_judged(case, name, args):
                    outcome, detail, settled = self._verdict(case, call, args, stored)
                    if judged:
                        later.append(outcome)
                        if settled and (outcome == "valid" or len(later) == MAX_LATER_ATTEMPTS):
                            return True
                        continue
                    judged = True
                    result.outcome, result.detail = outcome, detail
                    result.reasoning_gaps = write_actions.gaps_in_reasoning(
                        case, args, harness.reasoning_of(message)
                    )
                    result.detours = detours
                    if not settled:
                        return False
                    # Only the first call sets the rate. A forced turn or an ordinary case runs on
                    # after a bad one to show whether the model gets there; other runs have no
                    # reason to spend more.
                    if outcome == "valid" or not (self.forcing or case.ordinary):
                        return True
                    result.later_attempts = later
                    continue
                if judged:
                    continue
                detours.append(f"{call.name} {call.raw[:120]}")
                if len(detours) > MAX_DETOURS:
                    result.detours = detours
                    result.detail = f"never called {case.tool}.{case.action}; called {detours}"
                    return True
        if not judged:
            result.detours = detours
        return False

    @staticmethod
    def _verdict(
        case: Case, call: harness.Call, args: dict, stored: list[dict]
    ) -> tuple[str, str, bool]:
        """The judged call's outcome and detail, and whether that is final. A valid call of an
        ordinary case is not final until the real tool's answer is stored, and a refusal in that
        answer makes it invalid."""
        outcome, detail = write_actions.judge(case, call, args)
        if outcome != "valid" or not case.ordinary:
            return outcome, detail, True
        answer = next(
            (m for m in stored if m.get("role") == "tool" and m.get("tool_call_id") == call.id),
            None,
        )
        if answer is None:
            return outcome, detail, False
        refusal = refusal_of(answer.get("content"))
        if refusal is not None:
            return "invalid", f"the tool refused it: {refusal['error']}"[:300], True
        return outcome, detail, True

    def _judge_end(self, status, stored: list[dict], result: harness.Result) -> None:
        """A run that ended without the judged call: text, or a host problem. A run that ended
        after it, giving up on a bad call or with no answer stored to a good one, keeps that
        call's verdict."""
        if result.outcome in ("invalid", "valid"):
            return
        if status.status != "completed":
            http_status = REFUSED_REQUEST.search(status.error or "")
            refused = http_status and harness._is_rejection(int(http_status[1]))
            result.outcome = "rejected" if refused else "host_error"
            result.detail = f"run ended {status.status}: {status.error or ''}"[:300]
            return
        last = next((m for m in reversed(stored) if m.get("role") == "assistant"), {})
        result.outcome = "stopped" if result.detours else "no_call"
        result.detail = f"text: {str(last.get('content'))[:150]!r}"

    def _billed_replies(self, role: str, session_id: str, seconds: float) -> list[harness.Reply]:
        """One reply per billed model call, from the usage Hermes stored for the Session. It
        stores totals, so each reply gets an even share: requests and cost are exact, tokens
        are the mean per request."""
        connection = sqlite3.connect(f"file:{self.state_db(role)}?mode=ro", uri=True)
        try:
            calls, uncached, cached, output, cost = connection.execute(
                "SELECT api_call_count, input_tokens, cache_read_tokens, output_tokens, "
                "COALESCE(actual_cost_usd, estimated_cost_usd, 0) FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
        calls = max(calls, 1)
        return [
            harness.Reply(
                message={},
                calls=[],
                text="",
                provider=f"{self.model} through Hermes",
                prompt_tokens=(uncached + cached) // calls,
                cached_tokens=cached // calls,
                completion_tokens=output // calls,
                cost=cost / calls,
                seconds=seconds / calls,
            )
            for _ in range(calls)
        ]


def seed_records(store: Store, actor: Actor, holder: str) -> None:
    """The records the instructions name: Goal G-1, Tasks T-1 and T-2 (held by the Session
    ``holder``), claim C-1."""
    goals.create(
        store,
        actor,
        title="Understand early fights",
        end_state="We know why we lose early fights.",
        reason="The user asked for it.",
    )
    tasks.create(
        store,
        actor,
        type="research",
        title="Survey league L-7",
        description="Survey the field of league L-7 and report the leading strategies.",
        goal_id=GOAL,
    )
    tasks.create(
        store,
        actor,
        type="research",
        title="Compare the two divisions",
        description="Compare the practice and ranked divisions of league L-7.",
        goal_id=GOAL,
    )
    # Held, so a Researcher's cases find their Task in progress instead of first claiming it.
    tasks.claim(store, Actor.session(holder), HELD_TASK, run_id=None, worktree="w", branch="b")
    claims.write(
        store,
        actor,
        summary="pv-39 led league L-7 until this week.",
        body="The league page showed pv-39 at rank 1.",
        tags=["champion"],
        # Not CHAMPION_DOC: the evidence case adds that one, and a model rightly declines to add
        # a link that is already there.
        evidence=[{"type": "doc", "ref": EARLIER_DOC, "stance": "supports"}],
    )


def hermes_bin_from_env() -> Path | None:
    value = os.environ.get("HERMES_BIN")
    return Path(value) if value else None
