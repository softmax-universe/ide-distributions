import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = (
    "README.md",
    "core/README.md",
    "docs/hosting.md",
    "docs/local-end-to-end.md",
    "image/README.md",
    "orchestrator/README.md",
    "researcher/README.md",
)
REPO_PATH = re.compile(
    r"`((?:core|contract|docs|image|orchestrator|researcher|tools)/[\w./-]+\.\w+)`"
)


def test_version():
    import research_core

    assert research_core.__version__ == "0.1.0"


@pytest.mark.parametrize("doc", DOCS)
def test_paths_a_doc_names_exist(doc):
    """A file path written from the root exists there; `core/README.md` also writes `tools/...` and
    `contract/...` from inside the package."""
    roots = [REPO]
    if doc == "core/README.md":
        roots.append(REPO / "core" / "src" / "research_core")
    missing = {
        path
        for path in REPO_PATH.findall((REPO / doc).read_text())
        if not any((root / path).exists() for root in roots)
    }
    assert not missing, f"{doc} names paths that do not exist: {sorted(missing)}"


def test_the_plugin_imports_without_the_workspace_environment():
    """Hermes's interpreter runs the plugin and has no Softmax client, so a top-level import of it
    from anything the plugin reaches breaks every Session, and only the real-Hermes tests see it."""
    code = (
        "import sys\n"
        "sys.modules['softmax_client'] = None\n"
        "import research_core.plugin, research_core.tools.catalog\n"
        "import research_core.environment_guide\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
