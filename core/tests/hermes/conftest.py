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
SHARDS_BY_FILE = {
    "test_prepare_real.py": "prepare",
    "test_rebuild_real.py": "rebuild",
    "test_frozen_sessions_real.py": "lifecycle",
    "test_message_reaches_researcher_real.py": "lifecycle",
}


def pytest_addoption(parser):
    parser.addoption(
        "--hermes-shard",
        choices=("prepare", "rebuild", "lifecycle", "gateway"),
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

        if SHARDS_BY_FILE.get(item.path.name, "gateway") != shard:
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
