"""Build fresh hosted profiles at their runtime paths, without shipping mutable stores."""

import os
import shutil
import subprocess
from pathlib import Path

source = Path("/opt/distribution")
home = Path("/workspace/host")
python = "/opt/hermes/.venv/bin/python"
hermes = "/opt/hermes/.venv/bin/hermes"
for role in ("orchestrator", "researcher"):
    subprocess.run(
        [hermes, "profile", "install", str(source / role), "--name", role, "--yes"],
        check=True,
    )
    profile = home / "profiles" / role
    subprocess.run(
        [
            python,
            str(profile / "scripts/prepare.py"),
            "--source",
            str(source / role),
            "--hermes",
            hermes,
        ],
        env={
            **os.environ,
            "HERMES_HOME": str(profile),
            "IDE_LOCAL_STT_MODEL": "/opt/models/whisper-base",
        },
        check=True,
    )
    # Only release files and generated configuration belong in a template. In particular,
    # never copy a database, credential, session identity, or the build's workspace store.
    target = source / "prepared-profiles" / role
    target.mkdir(parents=True)
    for name in (
        ".no-bundled-skills",
        "config.yaml",
        "SOUL.md",
        "distribution.yaml",
        "scripts",
        "plugins",
        "local",
        ".softmax-prepared",
    ):
        path = profile / name
        if path.is_dir():
            shutil.copytree(path, target / name)
        else:
            shutil.copy2(path, target / name)
shutil.rmtree("/workspace")
