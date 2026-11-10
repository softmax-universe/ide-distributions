"""Tests that need a real Hermes install.

Every test in this folder carries the ``hermes`` marker (deselected by default through
``addopts``) and skips unless ``HERMES_BIN`` names the Hermes executable to test against.
Every Hermes invocation must set ``HERMES_HOME`` to a temporary directory so the developer's
own ``~/.hermes`` is never touched.
"""

import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def pytest_addoption(parser):
    parser.addoption(
        "--hermes-shard",
        choices=("prepare", "rebuild-orchestrator", "rebuild-researcher", "gateway"),
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    shard = config.getoption("--hermes-shard")
    deselected = []
    for item in items:
        if HERE not in item.path.parents:
            continue
        item.add_marker(pytest.mark.hermes)
        if shard is None:
            continue

        item_shard = "gateway"
        if item.path.name == "test_prepare_real.py":
            item_shard = "prepare"
        elif item.path.name == "test_rebuild_real.py" and hasattr(item, "callspec"):
            # Keep every assertion on a shared proof in the same job.
            role = item.callspec.params.get("proof", item.callspec.params.get("profile"))
            if role in ("orchestrator", "researcher"):
                item_shard = f"rebuild-{role}"
        if item_shard != shard:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]


@pytest.fixture(scope="session")
def hermes_bin() -> Path:
    value = os.environ.get("HERMES_BIN")
    if not value:
        pytest.skip("HERMES_BIN is not set; these tests need a real Hermes install")
    path = Path(value).expanduser()
    if not path.is_file():
        pytest.skip(f"HERMES_BIN does not name a file: {path}")
    return path
