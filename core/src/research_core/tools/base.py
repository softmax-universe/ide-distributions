"""The shape of a profile tool and the one entry point that runs it (design §4, C.2).

A tool is a :class:`ToolSpec`: a name, the roles that get it, where it executes, either a
flat parameter set or a list of actions with flat optional inputs, and a handler that takes a
:class:`ToolCall` and returns a JSON-ready dict. :func:`run_tool` validates the arguments
against the role's view of the tool, reports every problem at once, runs the handler and
renders the result as JSON text. A failing call never raises: Hermes gets a string. Only a tool
module that cannot load raises, on the first call that loads the catalog.

A call is checked against its selected action: a missing required field, a field of another
action given a value, an unknown field or a wrong type refuses the call before anything runs.
That refusal says what the call sent and carries ``usage`` (the action's required and optional
fields and an example call), so a model can fix the call in one try instead of repeating it.
Empty placeholders for fields the action does not take are dropped, not refused.

A tool with ``inline_chars`` hands a longer successful result to the model as a file in Hermes's
spillover folder: the result names the file, its largest list and an outline (:func:`spill`), so
one platform response does not ride along in every later request of the Session.

An action marked ``own_tool`` is also a flat tool, ``<tool>_<action>``, running the same handler as
the same grouped call (:func:`own_tool`). A flat tool has no ``action`` field and refuses one.

Store-only tools run inside Hermes, with the packaged OpenTelemetry SDK.
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from research_core import layout
from research_core.agent_telemetry import observed
from research_core.store.actors import Actor
from research_core.store.errors import StoreError
from research_core.tools.catalog import TOOLS, Role, register

if TYPE_CHECKING:  # pragma: no cover
    from research_core.layout import Coworld, Workspace
    from research_core.store.db import Store

Executor = Literal["inproc", "workspace"]
JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class ToolError(Exception):
    """A refusal the tool explains: every problem at once, plus context the agent can use."""

    def __init__(self, problems: list[str] | str, **context: Any):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        self.context = context
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    params: dict[str, dict] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    roles: frozenset[Role] = frozenset({"orchestrator", "researcher"})
    # Fields the handler needs in some form beyond ``required`` ("ref or view", "goal unless
    # type is maintenance"): a refusal's example call includes them so copying it can succeed.
    example_fields: tuple[str, ...] = ()
    # Fields a call may still carry but no schema or refusal offers: code fills them in now, and
    # a chat keeps the tool schema it started with, so an open one may send them for a while.
    hidden_params: dict[str, dict] = field(default_factory=dict)
    # Offered as a tool of its own, ``<tool>_<action>`` (:func:`own_tool`), whose schema can mark
    # the required fields as required, which the grouped tool's schema cannot. The grouped tool no
    # longer offers the action but still runs a call that names it.
    own_tool: bool = False
    # Fields only the Orchestrator can use. A Researcher's schema and refusals leave them out; a
    # call that sends one anyway still reaches the handler, which says why it does not apply.
    orchestrator_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[[ToolCall], dict]
    roles: frozenset[Role] = frozenset({"orchestrator", "researcher"})
    executor: Executor = "inproc"
    actions: tuple[ActionSpec, ...] | None = None
    params: dict[str, dict] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    # Wall-clock budget of a workspace-executor call; None means the executor default (400 s,
    # under Hermes's 420 s tool deadline). Long mixin commands run detached instead (A3.2).
    timeout_s: int | None = None
    # Fields the handler needs in some form beyond ``required``; see ActionSpec.example_fields.
    example_fields: tuple[str, ...] = ()
    # For a tool made by :func:`own_tool`: the grouped tool and the action it stands for, and the
    # action's hidden fields, so a retry of an older grouped call can keep its operation_id.
    split_from: tuple[str, str] | None = None
    hidden_params: dict[str, dict] = field(default_factory=dict)
    orchestrator_fields: tuple[str, ...] = ()  # see ActionSpec.orchestrator_fields
    # A result longer than this many characters goes to a file; the model gets its path and
    # outline instead (:func:`spill`). ``spill_hint`` says how to ask for a smaller result.
    inline_chars: int | None = None
    spill_hint: str = ""

    def actions_for(self, role: Role) -> list[ActionSpec]:
        """Every action a call may name for this role, offered here or as a tool of its own."""
        return [action for action in self.actions or () if role in action.roles]

    def grouped_actions(self, role: Role) -> list[ActionSpec]:
        """The actions this tool's schema offers."""
        return [action for action in self.actions_for(role) if not action.own_tool]

    def own_tools(self, role: Role) -> list[str]:
        """The tools of this role's actions that have one of their own."""
        return [f"{self.name}_{a.name}" for a in self.actions_for(role) if a.own_tool]

    def action(self, name: str) -> ActionSpec | None:
        for action in self.actions or ():
            if action.name == name:
                return action
        return None

    def properties(self, role: Role) -> dict[str, dict]:
        """The flat parameter set a role sees: common params plus every action's inputs.

        A field several actions take is one property, shaped as the first of them declares it
        (the actions must agree on the shape; ``test_tool_schemas`` checks it). Its description
        says which actions take it, see :func:`field_description`.
        """
        properties: dict[str, dict] = {}
        if self.actions is not None:
            actions = self.grouped_actions(role)
            properties["action"] = {
                "type": "string",
                "enum": [action.name for action in actions],
                "description": " ".join(action_line(a) for a in actions),
            }
            takers: dict[str, list[ActionSpec]] = {}
            for action in actions:
                for name in self.offered(action, role):
                    if name in action.params:
                        takers.setdefault(name, []).append(action)
            for name, taking in takers.items():
                schema = taking[0].params[name]
                described = field_description(name, taking, len(actions))
                properties[name] = {**schema, "description": described}
        for name, schema in self.offered(None, role).items():
            properties.setdefault(name, schema)
        return properties

    def offered(self, action: ActionSpec | None, role: Role | None = None) -> dict[str, dict]:
        """The fields a model is told about: the action's own inputs plus the tool's common ones;
        with a role, those it can use."""
        fields = {**(action.params if action else {}), **self.params}
        if role == "researcher":
            kept_back = {*self.orchestrator_fields, *(action.orchestrator_fields if action else ())}
            return {name: schema for name, schema in fields.items() if name not in kept_back}
        return fields

    def accepted(self, action: ActionSpec | None) -> dict[str, dict]:
        """The fields one call may carry: the offered ones plus the action's hidden ones."""
        return {**self.offered(action), **(action.hidden_params if action else self.hidden_params)}

    def checked_fields(self, role: Role) -> dict[str, dict]:
        """The shapes arguments are checked against: the offered fields, then the others a call may
        still carry (hidden ones, and the fields of actions that have their own tool)."""
        others = {
            name: schema
            for action in self.actions or ()
            for name, schema in (
                *action.hidden_params.items(),
                *(action.params.items() if action.own_tool else ()),
            )
        }
        return {**self.hidden_params, **others, **self.properties(role)}

    def schema(self, role: Role) -> dict:
        """The OpenAI function body Hermes registers: name, description, parameters."""
        required = list(self.required)
        if self.actions is not None:
            required = ["action", *required]
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": self.properties(role),
            "additionalProperties": False,
        }
        if required:
            parameters["required"] = required
        own = self.own_tools(role)
        note = f" Other changes have their own tools: {', '.join(own)}." if own else ""
        return {
            "name": self.name,
            "description": self.description + note,
            "parameters": parameters,
        }


def first_sentence(text: str) -> str:
    head, dot, _ = text.partition(". ")
    return head + "." if dot else text


def own_tool(grouped: ToolSpec, action: ActionSpec) -> ToolSpec:
    """One action of ``grouped`` as a flat tool: its fields, its required ones really required,
    the same handler."""

    def run_action(call: ToolCall) -> dict:
        # The handler and what it records (a transition's stored arguments) see a grouped call.
        args = {"action": action.name, **call.args}
        return grouped.handler(replace(call, spec=grouped, action=action.name, args=args))

    return ToolSpec(
        name=f"{grouped.name}_{action.name}",
        description=f"{first_sentence(grouped.description)} {action.description}",
        handler=run_action,
        roles=action.roles & grouped.roles,
        executor=grouped.executor,
        params={**action.params, **grouped.params},
        required=(*action.required, *grouped.required),
        timeout_s=grouped.timeout_s,
        example_fields=action.example_fields,
        split_from=(grouped.name, action.name),
        hidden_params=action.hidden_params,
        orchestrator_fields=(*action.orchestrator_fields, *grouped.orchestrator_fields),
        inline_chars=grouped.inline_chars,
        spill_hint=grouped.spill_hint,
    )


def action_line(action: ActionSpec) -> str:
    """One action in the tool's list: its name, the fields it requires, what it does.

    A JSON schema can mark only ``action`` as required when the rest depend on the action, so this
    puts them where the model chooses the action. Measured on Kimi K3.1 Pro on 2026-11-04.
    """
    needs = f" (needs {', '.join(action.required)})" if action.required else ""
    return f"{action.name}{needs}: {action.description}"


def field_description(name: str, taking: list[ActionSpec], action_count: int) -> str:
    """A multi-action tool's field description: what it is for each action, then which actions
    require it and which take it optionally.

    A JSON schema can only mark ``action`` as required when the other requirements depend on
    the action, and a model tends to send only the fields the schema marks required, so the
    description says it. Actions that describe the field differently each get their sentence,
    the ones that require it first (``claim.tags`` is a write field before it is a search
    filter). A field every action takes names only the actions that require it.
    """
    meanings: dict[str, list[ActionSpec]] = {}
    for action in taking:
        meanings.setdefault(str(action.params[name].get("description", "")).strip(), []).append(
            action
        )
    ordered = sorted(
        meanings.items(), key=lambda item: not any(name in a.required for a in item[1])
    )
    if len(ordered) == 1:
        parts = [ordered[0][0]] if ordered[0][0] else []
    else:
        parts = [f"{', '.join(a.name for a in actions)}: {text}" for text, actions in ordered]
    required = [a.name for a in taking if name in a.required]
    optional = [a.name for a in taking if name not in a.required]
    if len(taking) == action_count:
        optional = []  # every action takes it: only the ones that need it are news
    usage = []
    if required:
        usage.append(f"required for {', '.join(required)}")
    if optional:
        usage.append(f"optional for {', '.join(optional)}")
    if usage:
        line = "; ".join(usage)
        parts.append(line[0].upper() + line[1:] + ".")
    return " ".join(parts)


def tool(spec: ToolSpec) -> ToolSpec:
    """Register a tool in the catalog (used as ``SPEC = tool(ToolSpec(...))``), and the tools of
    its actions marked ``own_tool``."""
    for action in spec.actions or ():
        if action.own_tool:
            register(own_tool(spec, action))
    return register(spec)


# Where the watcher's login check keeps the Player id (workspace store meta).
PLAYER_ID_META = "player_id"


@dataclass
class ToolContext:
    role: Role
    actor: Actor
    workspace: Workspace
    env: Mapping[str, str]
    coordinator_instance: str | None = None

    @classmethod
    def from_env(cls, role: Role, env: Mapping[str, str] | None = None) -> ToolContext:
        env = dict(os.environ if env is None else env)
        return cls(role=role, actor=Actor.from_env(env), workspace=layout.from_env(env), env=env)

    def hermes_session_id(self) -> str:
        if self.actor.is_session:
            return self.actor.id
        return self.env.get("IDE_SESSION_ID") or self.env.get("HERMES_SESSION_ID") or ""

    @cached_property
    def wstore(self) -> Store:
        from research_core.store.db import Store

        return Store.open(self.workspace.store_path, kind="workspace")

    def player_id(self) -> str | None:
        """The workspace's Player id: ``SOFTMAX_PLAYER_ID`` when set, else what the last login
        check stored. The hosted image does not set the variable, and code in Hermes's
        interpreter cannot ask the platform."""
        return self.env.get("SOFTMAX_PLAYER_ID") or self.wstore.get_meta(PLAYER_ID_META)

    def coworld(self) -> Coworld:
        from research_core.store import sessions

        session_id = self.hermes_session_id()
        session = sessions.read(self.wstore, session_id) if session_id else None
        coworld_id = (
            session["coworld_id"] if session is not None else self.env.get("IDE_COWORLD_ID")
        )
        if not coworld_id:
            raise ToolError(
                "this session has no Coworld; the client must open a session for one, so tell "
                "the user. A delegated child never has one: return your findings to the parent"
            )
        return self.workspace.coworld(coworld_id)

    def coworld_or_none(self) -> Coworld | None:
        try:
            return self.coworld()
        except ToolError:
            return None

    @cached_property
    def store(self) -> Store:
        from research_core.store.db import Store

        return Store.open(self.coworld().store_path, kind="coworld")

    def platform(self):
        from research_core.platform import from_env

        return from_env(self.env, self.workspace)

    def scrub(self, text: str) -> str:
        """Keep store paths out of what the agent sees (design B.10).

        Only the ``.state`` folders are hidden: agents need the real paths of their worktrees
        and documents.
        """
        hidden = [str(self.workspace.state)]
        active = self.coworld_or_none()
        if active is not None:
            hidden.append(str(active.state))
        for path in sorted(hidden, key=len, reverse=True):
            text = text.replace(path, "<state>")
        return text


@dataclass
class ToolCall:
    spec: ToolSpec
    args: dict
    ctx: ToolContext
    action: str | None = None

    def get(self, name: str, default: Any = None) -> Any:
        return self.args.get(name, default)


def validate_args(
    spec: ToolSpec, role: Role, args: Mapping[str, Any]
) -> tuple[str | None, list[str]]:
    """The selected action (if any) and every problem with the arguments."""
    problems: list[str] = []
    properties = spec.properties(role)
    action_name: str | None = None
    required = list(spec.required)
    if spec.actions is not None:
        allowed = [action.name for action in spec.grouped_actions(role)]
        raw = args.get("action")
        if raw is None:
            problems.append(f"action is required; one of {', '.join(allowed)}")
        elif raw not in [action.name for action in spec.actions_for(role)]:
            known = spec.action(str(raw))
            if known is not None:
                problems.append(
                    f"action {raw!r} is not available to the {role}; allowed: {', '.join(allowed)}"
                )
            else:
                problems.append(f"unknown action {raw!r}; one of {', '.join(allowed)}")
        else:
            action_name = str(raw)
            required.extend(spec.action(action_name).required)  # type: ignore[union-attr]
    known = {*properties, *spec.params, *spec.hidden_params} | {
        name for action in spec.actions or () for name in (*action.params, *action.hidden_params)
    }
    accepted = spec.accepted(spec.action(action_name)) if action_name else None
    for name, value in args.items():
        if name not in known:
            problems.append(f"unknown field {name!r}")
        elif accepted is not None and name not in accepted and name != "action":
            # A value the action would silently ignore is a mistake the model must see; an empty
            # placeholder is padding and is dropped instead (see drop_unused_empty).
            if value not in (None, ""):
                problems.append(f"{name} is not a field of {action_name}")
    for name in required:
        if args.get(name) in (None, ""):
            problems.append(f"{name} is required" + (f" for {action_name}" if action_name else ""))
    checked = spec.checked_fields(role)
    for name, value in args.items():
        schema = checked.get(name)
        unused = accepted is not None and name not in accepted
        if schema is None or value is None or (unused and value == ""):
            continue  # an empty placeholder for another action's field is dropped, not checked
        expected = JSON_TYPES.get(schema.get("type", ""))
        if expected is str and isinstance(value, int) and not isinstance(value, bool):
            continue  # an integer id for a string reference field is coerced by coerce_args
        if expected is not None and not isinstance(value, expected):
            problems.append(f"{name} must be {schema['type']}")
        elif expected is int and isinstance(value, bool):
            problems.append(f"{name} must be integer")
        if "enum" in schema and value not in schema["enum"] and name != "action":
            problems.append(f"{name} must be one of {', '.join(map(str, schema['enum']))}")
    return action_name, problems


def coerce_args(spec: ToolSpec, role: Role, args: Mapping[str, Any]) -> dict:
    """Integers given for string fields (record ids) become strings; everything else is kept."""
    properties = spec.checked_fields(role)
    out = dict(args)
    for name, value in args.items():
        schema = properties.get(name)
        if schema and schema.get("type") == "string" and isinstance(value, int):
            if not isinstance(value, bool):
                out[name] = str(value)
    return out


def drop_unused_empty(spec: ToolSpec, action: str | None, args: Mapping[str, Any]) -> dict:
    """``args`` without empty placeholders for fields the selected action does not take."""
    if action is None:
        return dict(args)
    accepted = spec.accepted(spec.action(action))
    return {
        name: value
        for name, value in args.items()
        if name == "action" or name in accepted or value not in (None, "")
    }


PLACEHOLDERS = {"string": "<text>", "integer": 1, "number": 1.0, "boolean": True}


# The format a description gives, as in "The Task, such as T-3." or 'such as ["C-3"]'.
SUCH_AS = re.compile(r"""such as \[?"?([\w:./-]*\w)""")


def _placeholder(schema: dict) -> Any:
    """A value that shows the format and that no tool would take for a real record if it were sent
    as it is: ``<such as T-3>``, not ``T-3``."""
    if "enum" in schema:
        return schema["enum"][0]
    format_given = SUCH_AS.search(str(schema.get("description", "")))
    if schema.get("type") == "array":
        item = schema.get("items") or {"type": "string"}
        if format_given and item.get("type") == "string":
            return [f"<such as {format_given[1]}>"]
        return [_placeholder(item)]
    if format_given and schema.get("type") == "string":
        return f"<such as {format_given[1]}>"
    return PLACEHOLDERS.get(schema.get("type", ""), "<value>")


def _is_structured(schema: dict) -> bool:
    """An object, or a list of objects: a guessed placeholder would be refused again."""
    return "object" in (schema.get("type"), (schema.get("items") or {}).get("type"))


def _shape(schema: dict) -> str:
    """An object field's keys in short form, ``[{type, ref, stance, note?}]`` (``?``: optional)."""
    if schema.get("type") == "array":
        return f"[{_shape(schema.get('items') or {})}]"
    properties = schema.get("properties") or {}
    if not properties:
        return "{...}"
    required = set(schema.get("required") or ())
    keys = []
    for key, sub in properties.items():
        nested = f": {_shape(sub)}" if _is_structured(sub) else ""
        keys.append(f"{key}{'' if key in required else '?'}{nested}")
    return "{" + ", ".join(keys) + "}"


def _fields(schemas: Mapping[str, dict], names: list[str]) -> dict[str, str]:
    """Each field's description; an object field's also names its keys, so a refusal (which
    carries no schema) still shows the shape."""
    out = {}
    for name in names:
        schema = schemas[name]
        text = str(schema.get("description", schema.get("type", "")))
        if _is_structured(schema):
            text = f"{text} Shape: {_shape(schema)}".strip()
        out[name] = text
    return out


def usage(spec: ToolSpec, role: Role, action: str | None) -> dict:
    """How to call the tool correctly: what a model needs to fix a refused call in one try.

    With a known action: its required and optional fields with their descriptions and an example
    call to copy. Without one: every action the role may use, with the fields each requires.
    """
    if spec.actions is not None and action is None:
        own = spec.own_tools(role)
        return {
            "actions": {
                a.name: {"does": a.description, "requires": list(a.required)}
                for a in spec.grouped_actions(role)
            },
            **({"own_tools": own} if own else {}),
        }
    selected = spec.action(action) if action else None
    schemas = spec.offered(selected, role)
    required = [*(selected.required if selected else ()), *spec.required]
    optional = [name for name in schemas if name not in required]
    names = [*required, *(selected.example_fields if selected else spec.example_fields)]
    out = {
        **({"does": selected.description} if selected else {}),
        "required": _fields(schemas, required),
        "optional": _fields(schemas, optional),
    }
    # A guessed object would be refused again, so an action that needs one gets no example:
    # its required fields' descriptions spell out each shape.
    if not any(_is_structured(schemas[name]) for name in names):
        example = {"action": action} if action else {}
        out["example"] = {**example, **{name: _placeholder(schemas[name]) for name in names}}
    return out


def argument_error(
    spec: ToolSpec, role: Role, action: str | None, args: Mapping[str, Any], problems: list[str]
) -> dict:
    """The refusal for a call whose arguments are wrong: what arrived, what is wrong, how to fix it.

    Models that keep repeating a broken call (seen with Kimi K3 on some OpenRouter hosts) need the
    fix spelled out, so the summary names what this call sent and the payload carries the usage.
    A call naming an action that has its own tool is shown how to call that tool.
    """
    received = sorted(k for k in args if k != "action" or spec.actions is None)
    selected = spec.action(action) if action else None
    if selected is not None and selected.own_tool:
        spec, action = TOOLS[f"{spec.name}_{action}"], None
    label = f"{spec.name} {action}" if action else spec.name
    how_to = usage(spec, role, action)
    if "example" in how_to:
        fix = (
            "Call again with every required field in one call; usage.example shows the shape, "
            "with real values in place of each <...>."
        )
    elif "actions" in how_to:
        fix = "Call again with one of the actions in usage.actions and the fields it requires"
        fix += ", or one of the tools in usage.own_tools." if "own_tools" in how_to else "."
    else:
        fix = "Call again with every required field in one call; usage.required describes each."
    nothing_sent = "no fields besides action" if spec.actions is not None else "no fields"
    summary = (
        f"{label} was not run, nothing changed: {'; '.join(problems)}. "
        f"This call sent: {', '.join(received) or nothing_sent}. {fix}"
    )
    payload = error_payload(problems, {**tool_context(spec.name, action), "received": received})
    payload["error"] = summary
    payload["usage"] = how_to
    return payload


def tool_context(name: str, action: str | None) -> dict:
    return {"tool": name, **({"action": action} if action else {})}


def error_payload(
    problems: list[str], context: dict | None = None, kind: str | None = None
) -> dict:
    """A refusal. ``run_tool`` names its ``kind``, the short class telemetry reports it under."""
    return {
        "ok": False,
        **({"kind": kind} if kind else {}),
        "error": problems[0] if len(problems) == 1 else "; ".join(problems),
        "problems": problems,
        "context": context or {},
    }


# Where platform tools hand a result to a file (``ToolSpec.inline_chars``). Uncapped, one
# Orchestrator Session carried 25 ``softmax_api`` responses averaging 21,500 characters: 62% of its
# 217k-token request.
INLINE_CHARS = 8000
# An outline keeps strings up to this length (ids, cursors, statuses) and objects up to this many
# keys; longer ones are described by their size.
OUTLINE_TEXT_CHARS = 200
OUTLINE_KEYS = 40
# The outline loses a level of depth until it fits in this many characters.
OUTLINE_CHARS = 3000


def _outline(value: Any, depth: int = 3) -> Any:
    """``value``'s shape: short scalars as they are, lists as their length and item keys."""
    if isinstance(value, dict):
        if depth == 0 or len(value) > OUTLINE_KEYS:
            return f"<object with {len(value)} keys: {', '.join(list(value)[:OUTLINE_KEYS])}>"
        return {key: _outline(item, depth - 1) for key, item in value.items()}
    if isinstance(value, list):
        first = value[0] if value else None
        keys = f", each with keys {', '.join(first)}" if isinstance(first, dict) else ""
        return f"<list of {len(value)}{keys}>"
    if isinstance(value, str) and len(value) > OUTLINE_TEXT_CHARS:
        return f"<text of {len(value)} characters>"
    return value


def _largest_list(value: Any, path: str = "") -> tuple[str, int, int]:
    """The jq path, item count and JSON size of the biggest list in ``value``."""
    best = ("", 0, 0)
    if isinstance(value, list):
        best = (path, len(value), len(json.dumps(value, default=str)))
    children = value.items() if isinstance(value, dict) else ()
    for key, item in children:
        step = f".{key}" if re.fullmatch(r"[A-Za-z_]\w*", str(key)) else f"[{json.dumps(key)}]"
        found = _largest_list(item, path + step)
        if found[2] > best[2]:
            best = found
    return best


def spill(spec: ToolSpec, ctx: ToolContext, result: dict, chars: int) -> dict:
    """Write a long result to the profile's Hermes spillover folder; return its path and outline.

    Kept inline, a 20k-65k character platform response stays in every later request of the
    Session. The file is indented JSON, so ``read_file`` pages it by line and ``jq`` filters it.
    It lives where Hermes keeps its own persisted tool results, ``$HERMES_HOME/cache/spillover``,
    and its path is in the transcript, so Hermes's retention removes it with the Session that
    references it (or after ``sessions.retention_days`` once nothing does).
    """
    call_id = ctx.env.get("IDE_TOOL_CALL_ID") or str(time.time_ns())
    folder = Path(ctx.env["HERMES_HOME"]) / "cache" / "spillover"
    path = folder / f"ide-{spec.name}-{re.sub(r'[^A-Za-z0-9_.-]', '_', call_id)}.json"
    folder.mkdir(parents=True, exist_ok=True)
    text = ctx.scrub(json.dumps(result, indent=1, default=str))
    path.write_text(text + "\n")
    where, count, _ = _largest_list(result)
    example = f"jq '{where}[:5]' {path}" if where else f"jq 'keys' {path}"
    largest = f" The largest list is {where} with {count} items." if where else ""
    for depth in (3, 2, 1, 0):
        outline = _outline(result, depth)
        if len(json.dumps(outline, default=str)) <= OUTLINE_CHARS:
            break
    return {
        "ok": True,
        "spilled": {
            "path": str(path),
            "characters": chars,
            "lines": text.count("\n") + 1,
            "read": (
                f"The full result ({chars} characters) is in this file, not here.{largest} "
                f"Read parts of it with read_file (offset and limit, in lines) or filter it with "
                f"jq in terminal, such as {example}. {spec.spill_hint}"
            ).strip(),
        },
        "outline": outline,
    }


def run_tool(name: str, args: Mapping[str, Any], ctx: ToolContext) -> str:
    """Validate, run and render a tool call as JSON text. Raises only if the catalog cannot load."""
    from opentelemetry import trace

    from research_core import telemetry
    from research_core.tools.catalog import _load_tool_modules

    parent = (
        trace.set_span_in_context(trace.get_current_span())
        if trace.get_current_span().get_span_context().is_valid
        else telemetry.process_context(os.environ)
    )
    started = time.monotonic()
    with telemetry.span("execute_tool", context=parent) as span:
        tool_name = "unknown"
        action = None
        own_tool_action = None
        outcome = "internal_error"
        # The failure's short class; ``None`` while the call has not failed.
        error_type = None
        spec = None
        try:
            _load_tool_modules()
            spec = TOOLS.get(name)
            if spec is None or ctx.role not in spec.roles:
                outcome = error_type = "unknown_tool"
                return json.dumps(
                    error_payload([f"unknown tool {name!r} for the {ctx.role}"], kind=error_type)
                )
            # A tool of an action's own reports as the grouped tool and action it stands for.
            tool_name, own_tool_action = spec.split_from or (spec.name, None)
            action, problems = validate_args(spec, ctx.role, args)
            if problems:
                outcome = error_type = "validation_rejected"
                refusal = argument_error(spec, ctx.role, action, args, problems)
                return json.dumps({"kind": error_type, **refusal})
            try:
                args = drop_unused_empty(spec, action, args)
                call = ToolCall(
                    spec=spec, args=coerce_args(spec, ctx.role, args), ctx=ctx, action=action
                )
                result = spec.handler(call)
                if not isinstance(result, dict):
                    result = {"result": result}
                result.setdefault("ok", True)
                outcome = "succeeded" if result["ok"] else "failed"
                if not result["ok"]:
                    # A handler's refusal names its class in ``kind`` (``transition_refused``).
                    kind = result.setdefault("kind", outcome)
                    error_type = kind if telemetry.is_error_type(kind) else outcome
                text = json.dumps(result, default=str)
                # Outside Hermes (the CLI) there is no context to protect and no spillover folder.
                spills = spec.inline_chars is not None and "HERMES_HOME" in ctx.env
                if spills and len(text) > spec.inline_chars:
                    text = json.dumps(spill(spec, ctx, result, len(text)), default=str)
            except ToolError as error:
                outcome = error_type = "tool_rejected"
                text = json.dumps(
                    error_payload(error.problems, {"tool": name, **error.context}, error_type)
                )
            except StoreError as error:
                outcome = error_type = "store_rejected"
                text = json.dumps(
                    error_payload(
                        list(error.problems) or [str(error)],
                        tool_context(name, action),
                        error_type,
                    )
                )
            except Exception as error:  # the agent must see what failed, with context
                outcome = "internal_error"
                error_type = type(error).__name__
                text = json.dumps(
                    error_payload(
                        [f"{type(error).__name__}: {error}"],
                        {**tool_context(name, action), "traceback": traceback.format_exc()[-2000:]},
                        error_type,
                    )
                )
            return ctx.scrub(text)
        finally:
            # No arguments, results, prompts or error text: tool payloads may contain secrets.
            reported_action = own_tool_action or (
                action if spec is not None and spec.action(action) is not None else None
            )
            duration_s = time.monotonic() - started
            telemetry.finish_tool_call(
                span,
                boundary="distribution",
                role=ctx.role,
                tool_name=tool_name,
                tool_call_id=ctx.env.get("IDE_TOOL_CALL_ID"),
                session_id=ctx.hermes_session_id(),
                turn_id=ctx.env.get("IDE_TURN_ID"),
                outcome=outcome,
                error_type=error_type,
                duration_s=duration_s,
                action=reported_action,
            )
            # The completion for agent telemetry, written by the process that ran the tool.
            observed.record(
                "workspace_tool",
                "completed",
                session_id=ctx.hermes_session_id() or None,
                initiator=ctx.role,
                tool_name=tool_name,
                native_event_type=reported_action,
                outcome=outcome,
                reason=error_type,
                duration_ms=round(duration_s * 1000, 3),
                tool_call_id=ctx.env.get("IDE_TOOL_CALL_ID"),
                turn_id=ctx.env.get("IDE_TURN_ID"),
            )
