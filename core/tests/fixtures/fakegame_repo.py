"""Builds the fakegame fixture repository: the public source repository of the fake Coworld.

``make_fakegame_repo(tmp_path)`` writes a small git repository whose root is an
optimizer-seed-shaped mixin for the fake platform (plan Task 4.7, design D.1/D.2, amendments
A4.3 and A4.11):

- ``MIXIN.md`` with YAML front matter (``coworld``, ``coworld_version``, ``commands``,
  ``metrics``, ``artifact``, ``evidence_types``, ``toolchains``, ``cost_per_episode``);
- ``policy.json``, the starter policy ``{"parameters": {"aggression": 3, "patience": 5}}``;
- ``tools/build.sh`` (the artifact ``build/policy.json``), ``tools/check.py``,
  ``tools/upload.sh`` and ``tools/evaluate.py`` (per-seat metric rows as JSON lines from a
  downloaded ``results.json``);
- a lab ``AGENTS.md`` and ``docs/``;
- the five binding skills ``skills/<name>/SKILL.md`` (survey, eval-design, ab,
  replay-inspection, diagnosis), which a setup wake prefixes with the Coworld name;
- ``seed/`` in the optimizer-seed format: two claims, two lessons and two source citations.

The commands in ``MIXIN.md`` run in the code repository (``code/``) after the mixin was copied
to ``code/mixin/``; the scripts locate the mixin from their own path, so ``sh tools/build.sh``
from the fixture root works as well. Citations in ``seed/evidence/source-citations.json`` point
at ``docs/mechanics.md`` with the placeholder commit :data:`SOURCE_COMMIT`, because a fresh
repository cannot cite its own commit; their ``text`` and hashes are real.

``coworld_id`` lets a test build a mixin that declares another Coworld (A4.3).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import textwrap
from pathlib import Path

COWORLD_ID = "cow_fake"
COWORLD_NAME = "fakegame"
SOURCE_COMMIT = "0" * 40
SKILL_NAMES = ("survey", "eval-design", "ab", "replay-inspection", "diagnosis")
STARTER_POLICY = {"parameters": {"aggression": 3, "patience": 5}}
CLAIM_IDS = ("01HFAKE0000000000000000C01", "01HFAKE0000000000000000C02")
LESSON_IDS = ("01HFAKE0000000000000000X01", "01HFAKE0000000000000000X02")

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
}


def make_fakegame_repo(tmp_path: Path, *, coworld_id: str = COWORLD_ID) -> tuple[Path, str]:
    """Write and commit the fixture under ``tmp_path/fakegame_repo``; return (path, HEAD sha)."""
    repo = Path(tmp_path) / "fakegame_repo"
    files = fixture_files(coworld_id)
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "--", *files)
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fakegame mixin fixture")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    return repo, sha


def _git(repo: Path, *args: str) -> str:
    # No model key: pytest prints a failing frame's arguments, and git has no use for it.
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"} | _GIT_ENV
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )
    return result.stdout


def fixture_files(coworld_id: str) -> dict[str, str]:
    """Every file of the fixture, by path relative to the repository root."""
    mechanics = _mechanics_doc()
    files = {
        "COWORLD.md": _coworld_doc(),
        "MIXIN.md": _mixin_doc(coworld_id),
        "AGENTS.md": _agents_doc(),
        "policy.json": json.dumps(STARTER_POLICY, indent=2) + "\n",
        "docs/mechanics.md": mechanics,
        "docs/platform.md": _platform_doc(),
        "tools/build.sh": _build_sh(),
        "tools/check.py": _check_py(),
        "tools/upload.sh": _upload_sh(),
        "tools/evaluate.py": _evaluate_py(),
    }
    for name in SKILL_NAMES:
        files[f"skills/{name}/SKILL.md"] = _skill_doc(name)
    files.update(_seed_files(mechanics))
    return files


# --- documents -------------------------------------------------------------------------------


def _coworld_doc() -> str:
    return textwrap.dedent(
        f"""\
        # {COWORLD_NAME}

        The public repository of the fake Coworld (`{COWORLD_ID}`) used by the IDE profiles'
        tests. There is no game engine here: episodes are played by the fake platform. The
        research mixin is this repository's root (`MIXIN.md`).
        """
    )


def _mixin_doc(coworld_id: str) -> str:
    front_matter = textwrap.dedent(
        f"""\
        ---
        coworld: {coworld_id}
        coworld_version: "1"
        commands:
          build: sh mixin/tools/build.sh
          check: python3 mixin/tools/check.py
          upload: sh mixin/tools/upload.sh
          evaluate: python3 mixin/tools/evaluate.py
        metrics:
          - name: score
            direction: up
            primary: true
            guardrail: true
            min_episodes: 10
          - name: errors
            direction: down
            min_episodes: 10
        artifact:
          kind: file
          path: build/policy.json
        evidence_types:
          - name: xp-result
            description: A downloaded results.json of one experience request.
          - name: replay
            description: The per-tick summary file of one episode.
        toolchains: []
        cost_per_episode: 1
        ---
        """
    )
    body = textwrap.dedent(
        """\
        # MIXIN — fakegame

        The research mixin of the fake Coworld. The front matter above is what tools read
        (design D.1); the rest is for the agents.

        ## Bindings

        | Command | What it does |
        |---|---|
        | `build` | Checks `policy.json` and copies it to `build/policy.json`, the artifact. |
        | `check` | Validates `policy.json`: an object with numeric `parameters`. |
        | `upload` | Verifies the artifact and prints its sha256; the upload tool transfers it. |
        | `evaluate` | Reads downloaded `results.json` files; prints per-seat metric rows. |

        The commands run in the code repository root, where `policy.json` is the policy under
        study and `mixin/` is this folder. `tools/evaluate.py` takes the paths of
        `results.json` files or folders holding them and prints one JSON line per
        `(episode, seat)`: `{"episode", "seat", "seed", "status", "metrics": {"score", "errors"}}`.

        ## Skills

        `skills/<name>/SKILL.md` holds the five binding skills: survey, eval-design, ab,
        replay-inspection and diagnosis. Setup copies them with the Coworld name as a prefix
        (`fakegame-survey`).

        ## Reference policy

        `policy.json` is the starter policy, `aggression 3` and `patience 5`. The mechanics
        (`docs/mechanics.md`) say what each parameter changes.
        """
    )
    return front_matter + "\n" + body


def _agents_doc() -> str:
    return textwrap.dedent(
        """\
        # fakegame lab

        Read before working on the fake Coworld.

        - **Scoring.** Two metrics per seat and episode: `score` (up; the performance
          guardrail) and `errors` (down). `docs/mechanics.md` is the ground truth for how the
          policy parameters move them.
        - **Structures.** A policy is `policy.json`, an object with `parameters`
          (`aggression`, `patience`). The artifact is `build/policy.json`.
        - **Platform.** One league (`league_fake`) with one division (`div_fake`); episodes
          seat our policy against one house policy. Seeded requests replay the same board for
          every policy, so paired designs over seeds are cheap and sharp.
        - **Evidence.** Downloaded `results.json` files are `xp-result` evidence; the
          `evaluate` command turns them into per-seat metric rows.
        """
    )


def _mechanics_doc() -> str:
    return textwrap.dedent(
        """\
        # Mechanics of the fake game

        The platform plays a policy by the `parameters` of its `policy.json`. The starter has
        `aggression: 3` and `patience: 5`.

        ## score (up)

        About 50, plus a board term set by the seed (within 4 either way), plus 0.5 per
        `aggression` point above 3, plus episode noise within 1 either way.

        ## errors (down)

        An integer 0 to 2 per episode, plus `8 - patience` while patience is below 8. Each
        patience point up to 8 removes about one error per episode; patience never changes
        score.
        """
    )


def _platform_doc() -> str:
    return textwrap.dedent(
        """\
        # The fake platform

        - Coworld `cow_fake` (name `fakegame`, version 1); game `game_fake`.
        - League `league_fake` ("Fake League") with one division `div_fake`.
        - Every experience request costs one credit; the balance starts at 100.
        - A seeded request (`game_config_overrides.seed`) plays the same board for every
          policy; an unseeded one keys its noise on the request id.
        """
    )


def _skill_doc(name: str) -> str:
    descriptions = {
        "survey": "What a batch overview shows for fakegame",
        "eval-design": "Seeds, seats and episode floors for fakegame evals",
        "ab": "Paired A/B over seeds on score and errors",
        "replay-inspection": "Reading fakegame replay summaries",
        "diagnosis": "fakegame failure vocabulary and instruments",
    }
    return textwrap.dedent(
        f"""\
        ---
        name: {name}
        description: {descriptions[name]}
        ---

        # {name} ({COWORLD_NAME})

        The fakegame binding for the core `{name}` skill. Metrics are `score` (up) and
        `errors` (down), one row per seat and episode; seeded requests share the board term,
        so compare policies on the same seeds.
        """
    )


# --- seed (optimizer-seed format) -------------------------------------------------------------


def _seed_files(mechanics: str) -> dict[str, str]:
    citations = {
        _citation_key(start, end): _citation(mechanics, start, end)
        for start, end in ((8, 9), (13, 15))
    }
    keys = list(citations)
    claims = (
        (
            CLAIM_IDS[0],
            "score-follows-aggression",
            keys[0],
            "Score rises by 0.5 per aggression point above 3 on top of a per-seed board term "
            "and episode noise within 1 either way.",
        ),
        (
            CLAIM_IDS[1],
            "errors-follow-patience",
            keys[1],
            "Errors are an integer 0 to 2 per episode plus 8 minus patience while patience is "
            "below 8; patience never changes score.",
        ),
    )
    lessons = (
        (
            LESSON_IDS[0],
            "Compare candidate and baseline on the same seeds; the board term cancels only in "
            "paired differences.",
        ),
        (
            LESSON_IDS[1],
            "Raise patience before touching aggression: it lowers errors without moving the "
            "score guardrail.",
        ),
    )
    files = {
        "seed/evidence/source-citations.json": json.dumps(citations, indent=2, sort_keys=True)
        + "\n"
    }
    for claim_id, slug, ref, sentence in claims:
        files[f"seed/kb/claims/C-{claim_id}--{slug}.md"] = _claim_doc(claim_id, ref, sentence)
    for lesson_id, sentence in lessons:
        files[f"seed/lessons/L-{lesson_id}.md"] = _lesson_doc(lesson_id, keys[0], sentence)
    return files


def _citation_key(start: int, end: int) -> str:
    return f"docs/mechanics.md:{start}-{end}@{SOURCE_COMMIT}"


def _citation(mechanics: str, start: int, end: int) -> dict:
    lines = mechanics.splitlines(keepends=True)
    text = "".join(lines[start - 1 : end])
    return {
        "repo": f"softmax-universe/{COWORLD_NAME}",
        "path": "docs/mechanics.md",
        "commit": SOURCE_COMMIT,
        "start": start,
        "end": end,
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "file_sha256": hashlib.sha256(mechanics.encode()).hexdigest(),
    }


def _claim_doc(claim_id: str, ref: str, sentence: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        confidence: 1
        evidence:
        - direct: true
          ref: {ref}
          source_group: softmax-universe/{COWORLD_NAME}@{SOURCE_COMMIT}
          stance: supports
          tier: A
        evidence_grade: high
        id: C-{claim_id}
        kind: mechanism
        last_checked_version: "1"
        revise_if: The cited mechanics document changes at a new source commit.
        status: verified
        topics:
        - mechanics
        ---

        {sentence}
        """
    )


def _lesson_doc(lesson_id: str, scar: str, sentence: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        id: L-{lesson_id}
        lesson: {json.dumps(sentence)}
        recurrences:
          count: 0
          refs: []
        scar:
        - {scar}
        status: tentative
        ---

        {sentence}

        Provenance: the fakegame mechanics document. A tentative method lesson for the fake
        research cycle, not league evidence.
        """
    )


# --- tools -----------------------------------------------------------------------------------


def _build_sh() -> str:
    return textwrap.dedent(
        """\
        #!/bin/sh
        # Build the fakegame artifact, build/policy.json, from policy.json.
        # Runs in the code repository root (`sh mixin/tools/build.sh`) or in the mixin root
        # (`sh tools/build.sh`): the policy comes from the current directory when it has one,
        # else from the mixin's own starter policy.
        set -eu
        mixin_dir=$(cd "$(dirname "$0")/.." && pwd)
        if [ -f policy.json ]; then source=policy.json; else source="$mixin_dir/policy.json"; fi
        python3 "$mixin_dir/tools/check.py" "$source"
        mkdir -p build
        cp "$source" build/policy.json
        echo "built build/policy.json from $source"
        """
    )


def _check_py() -> str:
    return textwrap.dedent(
        '''\
        #!/usr/bin/env python3
        """Check a fakegame policy: an object with numeric `parameters`. Stdlib only.

        Usage: check.py [policy.json]; the default is ./policy.json, else the mixin's starter.
        Exit 0 and print the parameters when valid; exit 1 with the problems otherwise.
        """

        import json
        import sys
        from pathlib import Path

        REQUIRED = ("aggression", "patience")


        def problems(path: Path) -> list[str]:
            try:
                policy = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                return [f"{path}: not readable JSON: {error}"]
            parameters = policy.get("parameters") if isinstance(policy, dict) else None
            if not isinstance(parameters, dict):
                return [f"{path}: 'parameters' must be an object"]
            found = [f"{path}: parameter {name!r} is missing" for name in REQUIRED
                     if name not in parameters]
            found += [f"{path}: parameter {name!r} is not a number" for name, value
                      in parameters.items() if not isinstance(value, (int, float))]
            return found


        def main() -> int:
            if len(sys.argv) > 1:
                path = Path(sys.argv[1])
            elif Path("policy.json").is_file():
                path = Path("policy.json")
            else:
                path = Path(__file__).resolve().parent.parent / "policy.json"
            found = problems(path)
            for problem in found:
                print(problem, file=sys.stderr)
            if not found:
                print(json.dumps({"ok": True, "policy": str(path),
                                  "parameters": json.loads(path.read_text())["parameters"]}))
            return 1 if found else 0


        if __name__ == "__main__":
            sys.exit(main())
        '''
    )


def _upload_sh() -> str:
    return textwrap.dedent(
        """\
        #!/bin/sh
        # Verify the artifact and print its sha256. The upload tool transfers the bytes
        # through the platform; this command never talks to the network.
        set -eu
        artifact=build/policy.json
        if [ ! -f "$artifact" ]; then
            echo "no $artifact: run the build command first" >&2
            exit 1
        fi
        python3 - "$artifact" <<'EOF'
        import hashlib, json, sys
        path = sys.argv[1]
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        print(json.dumps({"artifact": path, "sha256": digest}))
        EOF
        """
    )


def _evaluate_py() -> str:
    return textwrap.dedent(
        '''\
        #!/usr/bin/env python3
        """Per-seat metric rows from downloaded fakegame results. Stdlib only.

        Usage: evaluate.py <results.json or folder>... — prints one JSON line per
        (episode, seat): {"episode", "seat", "seed", "status", "metrics": {"score", "errors"}}.
        Without arguments it reads the folder named by XP_RESULTS, as the xp tool and the
        watcher's checks run it.
        A folder is searched for results.json files. A results file is the fake platform's
        {"request_id"?, "seed"?, "episodes": [{"episode", "seat", "seed"?, "score", "errors"}]}.
        """

        import json
        import os
        import sys
        from pathlib import Path

        METRICS = ("score", "errors")


        def results_files(arguments: list[str]) -> list[Path]:
            found: list[Path] = []
            for argument in arguments:
                path = Path(argument)
                if path.is_dir():
                    found.extend(sorted(path.rglob("results.json")))
                else:
                    found.append(path)
            return found


        def rows(path: Path) -> list[dict]:
            results = json.loads(path.read_text(encoding="utf-8"))
            out = []
            for episode in results.get("episodes") or []:
                missing = [name for name in ("episode", "seat", *METRICS) if name not in episode]
                if missing:
                    raise ValueError(f"{path}: episode {episode.get('episode')} lacks {missing}")
                out.append({
                    "episode": episode["episode"],
                    "seat": episode["seat"],
                    "seed": episode.get("seed", results.get("seed")),
                    "status": "failed" if episode.get("failed") else "completed",
                    "metrics": {name: episode[name] for name in METRICS},
                })
            return out


        def main() -> int:
            arguments = sys.argv[1:] or [p for p in [os.environ.get("XP_RESULTS")] if p]
            if not arguments:
                print("usage: evaluate.py <results.json or folder>...", file=sys.stderr)
                return 2
            try:
                for path in results_files(arguments):
                    for row in rows(path):
                        print(json.dumps(row, sort_keys=True))
            except (OSError, ValueError) as error:
                print(str(error), file=sys.stderr)
                return 1
            return 0


        if __name__ == "__main__":
            sys.exit(main())
        '''
    )
