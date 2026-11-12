"""Prepare this profile after an install or an update (run by the host with Hermes's Python).

Usage: prepare.py --source <distribution folder> --hermes <hermes exe>

It loads research_core from <source>/../core/src, the release checkout, so an agent's edits can
never stop preparation. Scheduling is owned by research serve.
"""

import argparse
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument(
    "--source", required=True, type=Path, help="This distribution's folder in the release checkout"
)
parser.add_argument("--hermes", required=True, help="The Hermes executable")
parser.add_argument("--check", action="store_true", help="Report what would change; write nothing")
args = parser.parse_args()

source = args.source.resolve()
sys.path.insert(0, str(source.parent / "core" / "src"))
from research_core.brain.soul import apply as apply_brain  # noqa: E402
from research_core.prepare import HermesConfig, load_profile_env, prepare  # noqa: E402

load_profile_env(Path(__file__), source)
if not args.check:
    apply_brain(Path(os.environ["HERMES_HOME"]), source.name)  # no-op unless IDE_BRAIN=1
prepare(
    source=source,
    cli=HermesConfig(args.hermes),
    check=args.check,
)
