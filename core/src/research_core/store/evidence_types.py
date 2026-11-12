"""The evidence type registry (design B.7): reference formats, validators, inspectors.

Every evidence link on a claim has a type from this registry. A type names how its reference
is written (``ref_format``, quoted back in refusals), checks one (``validate`` returns the
problems) and shows what the evidence says (``inspect``). The record types (``claim``,
``experiment``, ``version``) also ``resolve`` against the store: the record must exist.

Inspectors take ``(ref, env)``: an :class:`InspectEnv` carries the store and whatever else the
inspectors read (the Coworld code repository for ``code``, the Hermes ``state.db`` for
``transcript``, the documents folder for ``measurement``). Callers go through
:func:`inspect_evidence`, which never raises: every answer is a dict with at least ``type``,
``ref`` and ``summary``, and anything that could not be read is a ``problem``.

The types are all registered below. Nothing registers a mixin's ``evidence_types``: a mixin may
list them, and a claim cannot yet use them. Stdlib only.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from research_core import brain, refs
from research_core.store._common import display_id, parse_json

if TYPE_CHECKING:
    from research_core.store.db import Store

Validator = Callable[[str], list[str]]
Resolver = Callable[["Store", str], list[str]]
Inspector = Callable[[str, "InspectEnv"], dict[str, Any]]

EXCERPT_CHARS = 4000


@dataclass(frozen=True)
class InspectEnv:
    """What the inspectors read besides the reference.

    ``code_repo`` is the active Coworld's code repository (``<coworld>/code``),
    ``hermes_state_db`` this profile's ``<HERMES_HOME>/state.db`` (sibling profiles' databases
    are searched too, since a transcript reference carries no profile) and ``documents`` the
    Coworld's documents folder.
    """

    store: Store
    code_repo: Path | None = None
    hermes_state_db: Path | None = None
    documents: Path | None = None


@dataclass(frozen=True)
class EvidenceType:
    name: str
    ref_format: str
    validate: Validator
    inspect: Inspector | None = None
    resolve: Resolver | None = None


_SHA = r"[0-9a-fA-F]{7,40}"
_CODE = re.compile(rf"^(?P<path>\S+?):(?P<start>[0-9]+)-(?P<end>[0-9]+)@(?P<sha>{_SHA})$")
_DOC_RETRIEVED = re.compile(r"^\S+ \(retrieved [0-9]{4}-[0-9]{2}-[0-9]{2}\): \S", re.DOTALL)
_TRANSCRIPT = re.compile(r"^session:(?P<session>[^/\s]+)/(?P<message>[^#\s]+)#[0-9a-fA-F]{64}$")
_MEASUREMENT = re.compile(rf"^(?P<command>\S.*?) @(?P<sha>{_SHA}) -> (?P<output>\S.*)$")
_REPLAY = re.compile(
    r"^episode:(?P<episode>[^/\s]+)(?:/seat (?P<seat>[0-9]+))?(?:/phase (?P<phase>[^/\s]+))?"
    r" xp:(?P<request>\S+)$"
)
_ANNOTATION = re.compile(r"^annotation:\S+$")


def _format_problem(ref: str, ref_format: str) -> list[str]:
    return [f"{ref!r} does not match {ref_format}"]


def _blank(ref: object) -> bool:
    return not isinstance(ref, str) or not ref.strip()


def _pattern_validator(pattern: re.Pattern[str], ref_format: str) -> Validator:
    def validate(ref: str) -> list[str]:
        if _blank(ref) or pattern.match(ref) is None:
            return _format_problem(ref, ref_format)
        return []

    return validate


def _validate_code(ref: str) -> list[str]:
    match = None if _blank(ref) else _CODE.match(ref)
    if match is None:
        return _format_problem(ref, CODE_FORMAT)
    problems: list[str] = []
    start, end = int(match["start"]), int(match["end"])
    if start < 1:
        problems.append(f"{ref!r}: line numbers start at 1")
    if start > end:
        problems.append(f"{ref!r}: the line range {start}-{end} is backwards")
    return problems


def _validate_doc(ref: str) -> list[str]:
    if _blank(ref):
        return _format_problem(ref, DOC_FORMAT)
    if _DOC_RETRIEVED.match(ref):
        return []
    document, sep, section = ref.partition("#")
    if sep and document.strip() and section.strip():
        return []
    return _format_problem(ref, DOC_FORMAT)


def _record_validator(record_kind: str, ref_format: str) -> Validator:
    def validate(ref: str) -> list[str]:
        if _blank(ref):
            return _format_problem(ref, ref_format)
        parsed = refs.parse(ref)
        if not parsed.is_record or parsed.record_kind != record_kind:
            return _format_problem(ref, ref_format)
        return []

    return validate


def _record_resolver(table: str, prefix: str) -> Resolver:
    def resolve(store: Store, ref: str) -> list[str]:
        number = refs.parse(ref).number
        if store.one(f"SELECT id FROM {table} WHERE id = ?", (number,)) is None:
            return [f"{display_id(prefix, number)} does not exist"]
        return []

    return resolve


def _result(type_name: str, ref: str, summary: str, **detail: Any) -> dict[str, Any]:
    return {"type": type_name, "ref": ref, "summary": summary, **detail}


def _record_inspector(name: str, table: str, prefix: str) -> Inspector:
    """The record's summary and status, read by SQL so the record modules need not exist."""

    def inspect(ref: str, env: InspectEnv) -> dict[str, Any]:
        number = refs.parse(ref).number
        display = display_id(prefix, number)
        row = env.store.one(f"SELECT * FROM {table} WHERE id = ?", (number,))
        if row is None:
            return _result(name, ref, display, id=display, problem=f"{display} does not exist")
        detail: dict[str, Any] = {"id": display}
        if "status" in row.keys():
            detail["status"] = row["status"]
        return _result(name, ref, row["summary"], **detail)

    return inspect


def _record_type(name: str, table: str, prefix: str) -> EvidenceType:
    ref_format = f"{prefix}-<n>"
    return EvidenceType(
        name=name,
        ref_format=ref_format,
        validate=_record_validator(name, ref_format),
        inspect=_record_inspector(name, table, prefix),
        resolve=_record_resolver(table, prefix),
    )


def _inspect_code(ref: str, env: InspectEnv) -> dict[str, Any]:
    """The cited lines at their commit and whether they still read the same at HEAD."""
    from research_core import gitops  # subprocess-backed; kept out of the store's import graph

    match = _CODE.match(ref)
    path, sha = match["path"], match["sha"]
    start, end = int(match["start"]), int(match["end"])
    where = f"{path}:{start}-{end}"
    if env.code_repo is None or not (env.code_repo / ".git").exists():
        problem = "the Coworld code repository is not set up, so the citation cannot be checked"
        return _result("code", ref, where, path=path, start=start, end=end, problem=problem)
    try:
        lines = gitops.show_lines(env.code_repo, path, start, end, sha)
        current = not gitops.lines_changed_since(env.code_repo, path, start, end, sha)
        head = gitops.rev_parse(env.code_repo, "HEAD")
    except gitops.GitError as error:
        detail = error.stderr.strip() or str(error)
        problem = f"git could not read {where} at {sha}: {detail}"
        return _result("code", ref, where, path=path, start=start, end=end, problem=problem)
    state = "unchanged" if current else "changed"
    summary = f"{where} {state} since {sha[:7]} (HEAD {head[:7]})"
    return _result(
        "code",
        ref,
        summary,
        path=path,
        start=start,
        end=end,
        sha=sha,
        head=head,
        lines=lines,
        current=current,
    )


def _inspect_transcript(ref: str, env: InspectEnv) -> dict[str, Any]:
    """The quoted Hermes message, read from ``state.db`` read-only, and its hash check."""
    match = _TRANSCRIPT.match(ref)
    session, message = match["session"], match["message"]
    digest = ref.rsplit("#", 1)[1].lower()
    where = f"message {message} of session {session}"
    if env.hermes_state_db is None:
        problem = "HERMES_HOME is not set, so the transcript store cannot be found"
        return _result("transcript", ref, where, problem=problem)
    found = None
    for state_db in _state_dbs(env.hermes_state_db):
        try:
            found = _transcript_message(state_db, session, message)
        except sqlite3.Error as error:
            problem = f"the Hermes transcript store could not be read: {error}"
            return _result("transcript", ref, where, problem=problem)
        if found is not None:
            break
    if found is None:
        return _result("transcript", ref, where, problem=f"{where} was not found in any profile")
    role, content, timestamp = found
    content = content or ""
    matches = hashlib.sha256(content.encode()).hexdigest() == digest
    state = "matches the cited hash" if matches else "differs from the cited hash"
    return _result(
        "transcript",
        ref,
        f"{role} {where}: the content {state}",
        role=role,
        content=content[:EXCERPT_CHARS],
        content_truncated=len(content) > EXCERPT_CHARS,
        timestamp=timestamp,
        hash_matches=matches,
    )


def _state_dbs(own: Path) -> list[Path]:
    """This profile's ``state.db`` first, then the sibling profiles' (``<profiles>/*/state.db``)."""
    candidates = [own] + sorted(own.parent.parent.glob("*/state.db"))
    seen: list[Path] = []
    for candidate in candidates:
        if candidate.is_file() and candidate not in seen:
            seen.append(candidate)
    return seen


def _transcript_message(state_db: Path, session: str, message: str) -> tuple | None:
    conn = sqlite3.connect(f"{state_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE session_id = ? AND id = ?",
            (session, int(message) if message.isdigit() else message),
        ).fetchone()
    finally:
        conn.close()


def _inspect_doc(ref: str, env: InspectEnv) -> dict[str, Any]:
    """The pinned excerpt when one was stored; otherwise the parts of the reference itself."""
    pin = _pin(env.store, "doc", ref)
    if _DOC_RETRIEVED.match(ref):
        head, _, quote = ref.partition("): ")
        source, _, retrieved = head.partition(" (retrieved ")
        return _result("doc", ref, quote, source=source, retrieved=retrieved, quote=quote, pin=pin)
    document, _, section = ref.partition("#")
    return _result("doc", ref, ref, document=document, section=section, pin=pin)


def _inspect_measurement(ref: str, env: InspectEnv) -> dict[str, Any]:
    """The stored output: a pin by its id or the output token, else the file's first lines."""
    match = _MEASUREMENT.match(ref)
    command, sha, output = match["command"], match["sha"], match["output"]
    detail: dict[str, Any] = {"command": command, "sha": sha, "output": output}
    pin = _pin(env.store, "measurement", output)
    if pin is None and output.startswith("pin:") and output[4:].isdigit():
        row = env.store.one("SELECT * FROM pins WHERE id = ?", (int(output[4:]),))
        pin = None if row is None else parse_json(row["download"], default={})
    if pin is not None:
        return _result(
            "measurement", ref, f"{command} @{sha[:7]}: pinned output", pin=pin, **detail
        )
    path = _output_path(output, env.documents)
    if path is None:
        problem = f"stored output {output} was not found as a pin or a file"
        return _result("measurement", ref, ref, excerpt=None, problem=problem, **detail)
    text = path.read_text(errors="replace")
    return _result(
        "measurement",
        ref,
        f"{command} @{sha[:7]}: {output}",
        excerpt=text[:EXCERPT_CHARS],
        excerpt_truncated=len(text) > EXCERPT_CHARS,
        size=path.stat().st_size,
        **detail,
    )


def _output_path(output: str, documents: Path | None) -> Path | None:
    """A readable file for a measurement's output token: as given, under the documents folder,
    or under the Coworld folder (so ``documents/x.txt`` works from the Coworld root)."""
    given = Path(output).expanduser()
    candidates = [given]
    if documents is not None and not given.is_absolute():
        candidates += [documents / given, documents.parent / given]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _inspect_replay(ref: str, env: InspectEnv) -> dict[str, Any]:
    """The replay's pin (its download data), kept under ``episode:<id>`` by ``platform.replay``."""
    match = _REPLAY.match(ref)
    episode = match["episode"]
    pin = _pin(env.store, "replay", f"episode:{episode}")
    state = "pinned" if pin is not None else "not downloaded yet"
    return _result(
        "replay",
        ref,
        f"episode {episode} ({state}); the game's replay skill, if it has one, is listed in the"
        " session context",
        episode=episode,
        seat=match["seat"],
        phase=match["phase"],
        xp_request=match["request"],
        pin=pin,
    )


def _inspect_annotation(ref: str, env: InspectEnv) -> dict[str, Any]:
    return _result("annotation", ref, ref, annotation_id=ref.partition(":")[2])


def _pin(store: Store, kind: str, ref: str) -> dict[str, Any] | None:
    row = store.one("SELECT download FROM pins WHERE kind = ? AND ref = ?", (kind, ref))
    return None if row is None else parse_json(row["download"], default={})


CODE_FORMAT = "<path>:<a>-<b>@<sha>"
DOC_FORMAT = "<repo-or-url>#<section> or <url> (retrieved <date>): <quote>"
TRANSCRIPT_FORMAT = "session:<id>/<message id>#<sha256 of the whole message>"
MEASUREMENT_FORMAT = "<command> @<code commit sha> -> <stored output path or pin id>"
REPLAY_FORMAT = "episode:<id>[/seat <n>][/phase <p>] xp:<request id, or none outside an XP request>"
ANNOTATION_FORMAT = "annotation:<id>"

EVIDENCE_TYPES: dict[str, EvidenceType] = {
    kind.name: kind
    for kind in (
        _record_type("claim", "claims", "C"),
        EvidenceType(
            name="code",
            ref_format=CODE_FORMAT,
            validate=_validate_code,
            inspect=_inspect_code,
        ),
        EvidenceType(
            name="doc",
            ref_format=DOC_FORMAT,
            validate=_validate_doc,
            inspect=_inspect_doc,
        ),
        EvidenceType(
            name="transcript",
            ref_format=TRANSCRIPT_FORMAT,
            validate=_pattern_validator(_TRANSCRIPT, TRANSCRIPT_FORMAT),
            inspect=_inspect_transcript,
        ),
        EvidenceType(
            name="measurement",
            ref_format=MEASUREMENT_FORMAT,
            validate=_pattern_validator(_MEASUREMENT, MEASUREMENT_FORMAT),
            inspect=_inspect_measurement,
        ),
        _record_type("experiment", "experiments", "X"),
        _record_type("version", "versions", "V"),
        EvidenceType(
            name="replay",
            ref_format=REPLAY_FORMAT,
            validate=_pattern_validator(_REPLAY, REPLAY_FORMAT),
            inspect=_inspect_replay,
        ),
        EvidenceType(
            name="annotation",
            ref_format=ANNOTATION_FORMAT,
            validate=_pattern_validator(_ANNOTATION, ANNOTATION_FORMAT),
            inspect=_inspect_annotation,
        ),
    )
}


WEB_FORMAT = "<http(s) url> (fetched <YYYY-MM-DD>)"
_WEB = re.compile(r"^https?://\S+ \(fetched (?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\)$")


def _validate_web(ref: str) -> list[str]:
    match = _WEB.match(ref) if isinstance(ref, str) else None
    if match is None:
        return _format_problem(ref, WEB_FORMAT)
    try:
        datetime.date.fromisoformat(match.group("date"))
    except ValueError:
        return [f"{ref!r} has an impossible fetched date; use {WEB_FORMAT}"]
    return []


def _inspect_web(ref: str, env: InspectEnv) -> dict[str, Any]:
    url, _, rest = ref.partition(" (fetched ")
    return _result("web", ref, url, url=url, fetched_at=rest.rstrip(")"))


WEB_TYPE = EvidenceType(
    name="web", ref_format=WEB_FORMAT, validate=_validate_web, inspect=_inspect_web
)


def _types(env: Mapping[str, str] | None = None) -> dict[str, EvidenceType]:
    """The registry; ``web`` (an unverified research note's source) exists only with IDE_BRAIN=1."""
    return {**EVIDENCE_TYPES, "web": WEB_TYPE} if brain.enabled(env) else EVIDENCE_TYPES


def check_ref(
    store: Store, type_name: object, ref: object, env: Mapping[str, str] | None = None
) -> list[str]:
    """Every problem with one evidence link: an unknown type, a malformed reference, or a
    referenced record that does not exist. Empty when the link is good."""
    kind = _types(env).get(type_name) if isinstance(type_name, str) else None
    if kind is None:
        return [_unknown_type(type_name, env)]
    problems = kind.validate(ref)  # type: ignore[arg-type]
    if problems and isinstance(ref, str) and ref.startswith(f"{type_name} "):
        # The most common miss (2026-11): the type name copied into the ref, "code path:1-2@sha".
        bare = ref[len(type_name) + 1 :].strip()
        problems = [
            f"{ref!r} repeats the type name: the type goes in type, the ref starts at {bare!r}",
            *kind.validate(bare),
        ]
    if problems or kind.resolve is None:
        return problems
    return kind.resolve(store, ref)  # type: ignore[arg-type]


def inspect_evidence(
    type_name: str, ref: str, env: InspectEnv, flags: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """What one evidence link says, by its type's inspector. Never raises.

    The answer always has ``type``, ``ref`` and ``summary``; an unknown type, a malformed
    reference, a type without an inspector or an inspector failure is a ``problem``.
    """
    kind = _types(flags).get(type_name)
    if kind is None:
        return _result(type_name, ref, ref, problem=_unknown_type(type_name, flags))
    problems = kind.validate(ref)
    if problems:
        return _result(type_name, ref, ref, problem="; ".join(problems))
    if kind.inspect is None:
        return _result(type_name, ref, ref, problem=f"{type_name} evidence has no inspector")
    try:
        return kind.inspect(ref, env)
    except Exception as error:  # an inspector reads git, sqlite and files; report, never raise
        return _result(type_name, ref, ref, problem=f"{type(error).__name__}: {error}")


def _unknown_type(type_name: object, env: Mapping[str, str] | None = None) -> str:
    known = ", ".join(sorted(_types(env)))
    return f"evidence type {type_name!r} is not registered (known: {known})"
