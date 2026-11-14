import sys
from pathlib import Path

from hermes_constants import get_hermes_home

core = (Path(get_hermes_home()) / "local" / "core-path.txt").read_text().strip()
sys.path.insert(0, core)

from research_core.plugin.restricted import register  # noqa: E402

__all__ = ["register"]
