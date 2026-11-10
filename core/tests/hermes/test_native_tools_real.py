"""``plugin/native_tools.py`` against the installed Hermes: the middleware reaches Hermes's own
``skill_view`` and ``read_file`` through Hermes's middleware chain, with no patch to Hermes."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.hermes.test_spike_s1 import hermes_python

SCRIPT = """
import json, sys
from hermes_cli.middleware import apply_tool_request_middleware, run_tool_execution_middleware
from hermes_cli.plugins import _delivery_manager
from research_core.plugin.native_tools import read_file_nearest_directory, skill_by_category
from tools.file_tools import read_file_tool
from tools.skills_tool import skill_view

middleware = _delivery_manager()._middleware
middleware.setdefault("tool_request", []).append(skill_by_category)
middleware.setdefault("tool_execution", []).append(read_file_nearest_directory)
args = apply_tool_request_middleware("skill_view", {"name": "method.experiment"}).payload
skill = json.loads(skill_view(**args))
read = json.loads(run_tool_execution_middleware(
    "read_file", {"path": sys.argv[1]}, lambda a: read_file_tool(**a)))
print(json.dumps({"args": args, "skill": skill.get("success"), "read": read}))
"""


def test_skill_view_takes_a_category_name_and_a_missing_directory_names_its_parent(
    hermes_bin, tmp_path
):
    skill = tmp_path / "home" / "skills" / "method" / "experiment"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: experiment\ndescription: Run one experiment.\n---\n\n# Experiment\n"
    )
    (tmp_path / "work" / "code" / "mixin").mkdir(parents=True)
    missing = tmp_path / "work" / "code" / "src" / "policy.py"
    source = Path(__file__).resolve().parents[2] / "src"
    done = subprocess.run(
        [str(hermes_python(hermes_bin)), "-c", SCRIPT, str(missing)],
        env={**os.environ, "HERMES_HOME": str(tmp_path / "home"), "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(done.stdout.splitlines()[-1])
    assert result["args"] == {"name": "method/experiment"}
    assert result["skill"] is True
    assert result["read"]["error"] == f"File not found: {missing}"
    assert result["read"]["hint"] == (
        f"No directory {missing.parent}. The nearest existing one is "
        f"{tmp_path / 'work' / 'code'}, holding: mixin/"
    )
