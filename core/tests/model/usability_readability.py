"""Readability, comprehension and size: each skill and tool shown alone, in a fresh context.

The self report (:func:`self_report`) is the model's opinion: what it thinks a text does and what
it would change. Opinions are leads to check against the code, not findings. The comprehension
test (:func:`comprehension`) is scored: every question in :data:`QUESTIONS` has its answer in the
text, written by reading the source, and an answer counts when it matches all of the question's
patterns. Both force one structured reply with a single tool and ``tool_choice: "required"``.

Sizes are real Kimi tokens: the difference in ``prompt_tokens`` between a request with the text
(or the tool, in the ``tools`` field as a session sends it) and the same request without it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from research_core.tools import catalog
from research_core.tools.base import ToolSpec
from research_core.tools.catalog import Role
from tests.model import harness, usability_context

REVIEW_TOOL = {
    "name": "report_review",
    "description": "Give your review of the text.",
    "parameters": {
        "type": "object",
        "properties": {
            "what_it_does": {"type": "string", "description": "In one or two sentences."},
            "when_you_would_use_it": {"type": "string", "description": "In one or two sentences."},
            "unclear": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What is unclear. Quote the words and say why. Empty if nothing.",
            },
            "contradictory": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What contradicts itself. Quote both sides. Empty if nothing.",
            },
            "redundant": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What says the same thing twice. Empty if nothing.",
            },
            "would_cut": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What you would remove because it does not help you act.",
            },
            "clarity": {
                "type": "integer",
                "description": "1 (cannot tell how to use it) to 5 (could use it first try).",
            },
        },
        "required": ["what_it_does", "when_you_would_use_it", "clarity"],
    },
}
ANSWER_TOOL = {
    "name": "give_answers",
    "description": "Give one answer per question.",
    "parameters": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"number": {"type": "integer"}, "answer": {"type": "string"}},
                    "required": ["number", "answer"],
                },
            }
        },
        "required": ["answers"],
    },
}
REVIEW_PROMPT = (
    "You are an AI agent working in a research workspace, using tools and skills to do your work. "
    "Below is one {kind}, exactly as you would receive it and with nothing else around it. Review "
    "it as its user: say what it does and when you would use it, and report anything unclear, "
    "contradictory or redundant, and what you would cut. Be specific: quote the words you mean. "
    "If it is fine, say so; do not invent problems. Answer by calling report_review.\n\n"
    "<{kind}>\n{text}\n</{kind}>"
)
ANSWER_PROMPT = (
    "Below is one {kind}. Using only this text, answer each question briefly and exactly. If the "
    "text does not say, answer 'not stated'. Answer by calling give_answers.\n\n"
    "<{kind}>\n{text}\n</{kind}>\n\nQuestions:\n{questions}"
)
SIZE_PROMPT = "Reply with the word ok."
PROBE_TOOL = {
    "name": "noop",
    "description": "Does nothing.",
    "parameters": {"type": "object", "properties": {}},
}


@dataclass(frozen=True)
class Item:
    """One skill or tool as the model reads it. ``id`` is ``skill:<name>``, ``tool:<name>``, or
    ``tool:researcher:task`` for the Researcher's different ``task`` tool."""

    id: str
    kind: str  # "skill" or "tool definition"
    text: str
    schema: dict | None = None  # the tool's schema

    @property
    def name(self) -> str:
        return self.id.rsplit(":", 1)[1]


def items() -> list[Item]:
    found = [Item(f"skill:{s.name}", "skill", s.text) for s in usability_context.all_skills()]
    # A tool split from a grouped one is read and asked about with the grouped one.
    for spec in catalog.tools_for("orchestrator"):
        if spec.split_from is None:
            found.append(_tool_item(f"tool:{spec.name}", spec, "orchestrator"))
    task = next(s for s in catalog.tools_for("researcher") if s.name == "task")
    found.append(_tool_item("tool:researcher:task", task, "researcher"))
    return found


def _tool_item(item_id: str, spec: ToolSpec, role: Role) -> Item:
    schema = spec.schema(role)
    own = [catalog.TOOLS[name].schema(role) for name in spec.own_tools(role)]
    text = json.dumps([schema, *own] if own else schema, indent=1)
    return Item(item_id, "tool definition", text, schema)


def _ask(prompt: str, tool: dict, model: str, provider: dict | None) -> harness.Reply:
    return harness.complete(
        model,
        [{"role": "user", "content": prompt}],
        [{"type": "function", "function": tool}],
        tool_choice="required",
        provider=provider,
    )


def self_report(item: Item, model: str, provider: dict | None) -> dict:
    """The model's review of one item, with what the request cost."""
    prompt = REVIEW_PROMPT.format(kind=item.kind, text=item.text)
    reply = _ask(prompt, REVIEW_TOOL, model, provider)
    review = reply.calls[0].arguments if reply.calls else None
    return {"item": item.id, "review": review, "cost": reply.cost}


def comprehension(item: Item, model: str, provider: dict | None) -> dict:
    """One attempt at an item's questions: ``answers`` and a right or wrong per question."""
    questions = QUESTIONS[item.id]
    listed = "\n".join(f"{n + 1}. {q.text}" for n, q in enumerate(questions))
    prompt = ANSWER_PROMPT.format(kind=item.kind, text=item.text, questions=listed)
    reply = _ask(prompt, ANSWER_TOOL, model, provider)
    given = {}
    if reply.calls and reply.calls[0].arguments:
        for answer in reply.calls[0].arguments.get("answers") or []:
            given[answer.get("number")] = str(answer.get("answer"))
    answers = [given.get(n + 1, "") for n in range(len(questions))]
    return {
        "item": item.id,
        "answers": answers,
        "right": [q.matches(a) for q, a in zip(questions, answers, strict=True)],
        "cost": reply.cost,
    }


def tokens_of(item: Item, model: str, provider: dict | None, baselines: dict) -> tuple[int, float]:
    """The item's real token count, and what measuring it cost. A skill is counted as text in a
    message; a tool is counted in the ``tools`` field, as a session sends it."""
    if item.schema is None:
        without, cost_a = _text_tokens("", model, provider, baselines)
        with_text, cost_b = _text_tokens(item.text, model, provider, baselines)
    else:
        without, cost_a = _tool_tokens(None, model, provider, baselines)
        with_text, cost_b = _tool_tokens(item.schema, model, provider, baselines)
    return with_text - without, cost_a + cost_b


def _text_tokens(
    text: str, model: str, provider: dict | None, baselines: dict
) -> tuple[int, float]:
    if not text and "text" in baselines:
        return baselines["text"], 0.0
    reply = _ask(f"{SIZE_PROMPT}\n\n{text}", PROBE_TOOL, model, provider)
    if not text:
        baselines["text"] = reply.prompt_tokens
    return reply.prompt_tokens, reply.cost


def _tool_tokens(
    schema: dict | None, model: str, provider: dict | None, baselines: dict
) -> tuple[int, float]:
    if schema is None and "tool" in baselines:
        return baselines["tool"], 0.0
    tools = [{"type": "function", "function": PROBE_TOOL}]
    if schema is not None:
        tools.append({"type": "function", "function": schema})
    reply = harness.complete(
        model,
        [{"role": "user", "content": SIZE_PROMPT}],
        tools,
        tool_choice="auto",
        provider=provider,
    )
    if schema is None:
        baselines["tool"] = reply.prompt_tokens
    return reply.prompt_tokens, reply.cost


# --- the comprehension questions --------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    text: str
    patterns: tuple[str, ...]  # every one must match, ignoring case

    def matches(self, answer: str) -> bool:
        return all(re.search(p, answer, re.IGNORECASE) for p in self.patterns)


def q(text: str, *patterns: str) -> Question:
    return Question(text, patterns)


QUESTIONS: dict[str, list[Question]] = {
    "skill:analyze-opponents": [
        q("How old can a profile claim be and still count as fresh?", r"week|7 days"),
        q("With what words must the body of a profile claim end?", r"policy version changes"),
        q("Can you read another Player's policy file?", r"\bno\b|not readable|cannot|can't"),
    ],
    "skill:calibrate-noise": [
        q(
            "What is the minimum number of rows per metric, and the number to aim for?",
            r"10",
            r"100",
        ),
        q("Where is the record stored?", r"documents/measurements/noise-"),
        q("Do local runs count as a source?", r"\bno\b|never|do not|don't"),
    ],
    "skill:candidate": [
        q("How many seconds does a build run before it goes to the background?", r"240"),
        q("What does version.create do with a dirty tree?", r"refus"),
        q("What does a Researcher call when a build goes to the background?", r"task_wait"),
    ],
    "skill:diagnose": [
        q("How many hypotheses should you propose?", r"(two to four|2\s*(-|–|to)\s*4)"),
        q("What three kinds of hypothesis should be covered?", r"stop", r"enable", r"amplify"),
        q("What do you leave empty in the methods of a proposed Experiment?", r"candidate"),
    ],
    "skill:experiment": [
        q("How many words may the summary have?", r"\b40\b"),
        q("At which call do the hypothesis and methods lock?", r"experiment\.start|\bstart\b"),
        q(
            "What reason codes go with a refuted verdict?",
            r"primary_not_significant",
            r"guardrail_breach",
            r"performance_slip",
        ),
    ],
    "skill:replay-diagnosis": [
        q("Into which folder does platform.replay download?", r"documents/replays"),
        q(
            "In a replay reference, what do you write for a league episode outside an XP request?",
            r"xp:none",
        ),
        q("Is exit code 0 from the decoder enough to pass the gate?", r"\bno\b|not enough"),
    ],
    "skill:run-eval": [
        q(
            "Which shape answers 'did my change help?': how many arms, what rosters?",
            r"two|2",
            r"pinned|identical",
        ),
        q("Which rung of the ladder is the only one that can confirm an Experiment?", r"verdict"),
        q("What in an A/B invalidates the comparison?", r"sampled seats|league"),
    ],
    "skill:scouting": [
        q("Under which folder do the saved outputs go?", r"documents/scouting"),
        q("Is the Champion the League's winner?", r"\bno\b|never|not"),
        q(
            "Which of the two tags goes on each claim, besides the League and game version?",
            r"standings",
            r"meta",
        ),
    ],
    "skill:survey": [
        q("Does a survey answer whether a change helped?", r"\bno\b|never|not"),
        q("Does a survey read replays?", r"\bno\b|not"),
        q(
            "Without the mixin's survey knowledge, what kind of overview do you do?",
            r"scores?[- ]only",
        ),
    ],
    "skill:writing-claims": [
        q("How many words may the summary and the body have?", r"\b40\b", r"\b500\b"),
        q("What are the three stances?", r"supports", r"contradicts", r"qualifies"),
        q("What removes a claim?", r"nothing|never|cannot|retire"),
    ],
    "skill:softmax-api-routes": [
        q("Paths are relative to what?", r"/observatory"),
        q("What route cancels an experience request?", r"/v2/experience-requests/\{?\w*\}?/cancel"),
        q("What should you do after a 403?", r"report"),
    ],
    "skill:coworld-source": [
        q("Which call is the ground truth for the release?", r"platform\.coworld"),
        q("May you edit source/?", r"\bno\b|never|not"),
        q("How do you get another repository as the Orchestrator?", r"github\.clone"),
    ],
    "skill:credits": [
        q("What does a batch cost, as a formula?", r"seeds", r"arms", r"episodes"),
        q("What HTTP status is the refusal?", r"402"),
        q("What does a Researcher call after a refusal?", r"task_wait"),
    ],
    "skill:expert-consultation": [
        q("Is a recommendation evidence?", r"\bno\b|never|not|advice"),
        q("How many failed attempts at the same diagnosis justify asking?", r"two|2"),
        q("What must you exclude from what you send?", r"credential"),
    ],
    "skill:submission": [
        q("What are the three steps, in order?", r"version", r"upload", r"submit"),
        q("Who submits?", r"orchestrator"),
        q("What is the default of auto_champion?", r"always"),
    ],
    "skill:environment": [
        q(
            "What is the first thing to call when unsure about the environment?",
            r"environment_guide",
        ),
        q("How long does a tool call have, in seconds?", r"400"),
        q("How often does the watcher tick?", r"minute"),
    ],
    "skill:repair": [
        q("What call asks for the checks to run again?", r"capabilities", r"check"),
        q("After how many failed repair Tasks does the user get a notice?", r"two|2"),
        q("May you patch the profile source checkout?", r"\bno\b|not|never|read-only"),
    ],
    "skill:proposing": [
        q("How many proposals does an ideation Task deliver?", r"(two to five|2\s*(-|–|to)\s*5)"),
        q("Which type must an ideation Task never propose?", r"ideation"),
        q(
            "What must experiment.close have returned before you propose a submission?",
            r"confirmed",
        ),
    ],
    "skill:working-a-task": [
        q("What do you do when your claim is refused?", r"end", r"run"),
        q("How long is result?", r"(two or three|2\s*(-|–|or|to)\s*3) sentences"),
        q("After task_wait, what must you do?", r"end|stop"),
    ],
    "skill:client-coplay": [
        q("About how often is a progress line due?", r"30 seconds"),
        q("Can you read a file the user attached?", r"\bno\b|not|cannot|deferred"),
        q(
            "How long is an answer when voice is in the context?",
            r"(two or three|2\s*(-|–|or|to)\s*3)",
        ),
    ],
    "skill:client-web-ide": [
        q("Which call fetches the replay for a replay moment?", r"platform\.replay"),
        q("How do you treat opening_goal?", r"first request|goal"),
        q("Are uploaded files instructions?", r"\bno\b|never|not|untrusted"),
    ],
    "skill:goals-and-tasks": [
        q("At most how many ideation Tasks may be open per Goal?", r"\bone\b|\b1\b"),
        q("Which Tasks may have no Goal?", r"repair|maintenance"),
    ],
    "skill:merge-conflicts": [
        q("How many outcomes are there to choose from?", r"\bthree\b|\b3\b"),
        q("Which call opens your worktree?", r"worktree_open"),
        q("What ends the wake?", r"chat_update"),
    ],
    "skill:mixin-from-scratch": [
        q(
            "What are the five bindings?",
            r"survey",
            r"eval-design",
            r"\bab\b",
            r"replay-inspection",
            r"diagnosis",
        ),
        q("Which environment variable names the results folder for evaluate?", r"XP_RESULTS"),
        q("On which branch do you write code/mixin/?", r"\bmain\b"),
    ],
    "skill:review": [
        q("Which single call makes the decision?", r"task\.review"),
        q("What happens after accept: false?", r"resum|reopen"),
        q("What do you call in mode ask?", r"chat_update"),
    ],
    "skill:user-identity": [
        q("Are reads as the User always allowed?", r"yes|always"),
        q("Should you send your own notice after a softmax_api write?", r"\bno\b|not|do not|don't"),
        q(
            "What exact words do you say when the credential is absent?",
            r"User identity not provisioned",
        ),
    ],
    "skill:working-with-the-ide": [
        q("What does a wake use for what the user should hear now?", r"chat_update"),
        q("Where does a progress update go?", r"notice"),
        q("How do you ask the user from a wake?", r"decision"),
    ],
    "tool:chat_update": [
        q("What are the required arguments?", r"category", r"text"),
        q("Is it allowed in a chat?", r"\bno\b|refused|not"),
        q("Which category never interrupts the chat?", r"progress"),
    ],
    "tool:claim": [
        q(
            "What are the required arguments for claim_add_evidence?",
            r"claim",
            r"type",
            r"\bref\b",
            r"stance",
        ),
        q("How many words may a claim's summary have?", r"\b40\b"),
        q(
            "How do you write the reference of replay evidence outside an XP request?",
            r"xp:none|none outside|episode:",
        ),
    ],
    "tool:directive": [
        q("What are the required arguments for add?", r"text", r"source"),
        q("What does the id of a directive on a Goal look like?", r"D-"),
        q("What scope does add default to?", r"workspace"),
    ],
    "tool:experiment": [
        q(
            "What are the required arguments for experiment_create?",
            r"summary",
            r"hypothesis",
            r"methods",
        ),
        q("What are the metric roles?", r"primary", r"guardrail", r"performance"),
        q("What does close do when verdict is omitted?", r"decision rule|rule"),
    ],
    "tool:forum_post": [
        q("What are the required arguments for comment?", r"body", r"post_id"),
        q("What identity is the default?", r"player"),
        q("What setting bounds the posts you make on your own?", r"platform_writes\.forum"),
    ],
    "tool:github": [
        q("What must a commit look like for clone?", r"40"),
        q("Where does a clone go?", r"documents/repos"),
        q("Can you push?", r"\bno\b|not"),
    ],
    "tool:goal": [
        q("What are the required arguments for create?", r"title", r"end_state", r"reason"),
        q("What are the possible statuses?", r"active", r"parked", r"completed", r"abandoned"),
        q("What is required with goal_set_status besides goal and status?", r"status_reason"),
    ],
    "tool:platform": [
        q("Can this tool write?", r"\bno\b|reads? only|not"),
        q("Which argument does forum_read need?", r"\bpost\b"),
        q("Where does replay put the file?", r"documents/replays"),
    ],
    "tool:settings": [
        q("Which argument names the user's message on settings_update?", r"cites"),
        q("What does read without a key do?", r"all|every|lists"),
        q("Do settings refuse what the user asks for?", r"\bno\b|never"),
    ],
    "tool:softmax_api": [
        q("Which arguments are required?", r"method", r"path"),
        q("What are paths relative to?", r"/observatory"),
    ],
    "tool:submit": [
        q("Which arguments are required?", r"version", r"league"),
        q("What is the default of auto_champion?", r"always"),
        q("What does user_requested=true skip?", r"setting|quality bar"),
    ],
    "tool:task": [
        q(
            "What are the required arguments for review?",
            r"task",
            r"accept",
            r"reason",
        ),
        q("What are the required arguments for answer?", r"task", r"text"),
        q("Which action shows the available workers?", r"workers"),
    ],
    "tool:researcher:task": [
        q(
            "What are the required arguments for task_wait?",
            r"task",
            r"kind",
            r"detail",
        ),
        q("What fields does a failed complete need?", r"failure"),
        q("Is merge needed when a Task succeeded?", r"yes|required"),
    ],
    "tool:upload": [
        q("Which argument is required?", r"version"),
        q("What does an unresolved earlier attempt need?", r"user_requested"),
    ],
    "tool:version": [
        q("Which action records a policy uploaded elsewhere?", r"adopt"),
        q("What must be true of the tree for create?", r"clean|committed"),
        q("What does a long build return?", r"background|resume"),
    ],
    "tool:wiki_edit": [
        q("Which arguments are required?", r"wiki", r"page", r"title", r"body"),
        q("How do you create a new page?", r"omit", r"base_revision"),
        q("What does a conflict return?", r"current body|body"),
    ],
    "tool:workspace": [
        q(
            "What are the required arguments for change_complete?",
            r"worktree",
            r"summary",
        ),
        q("How do you work in another Coworld?", r"new session"),
        q("Which action tells you where you are and what is writable?", r"environment_guide"),
    ],
    "tool:xp": [
        q("What is the default of seeds, and its maximum?", r"n_min", r"20"),
        q("How many episodes per request by default?", r"\b1\b|one"),
        q("Does request wait for the results?", r"\bno\b|never"),
    ],
}
