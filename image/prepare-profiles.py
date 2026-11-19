"""Build fresh hosted profiles at their runtime paths, without shipping mutable stores."""

import compileall
import os
import py_compile
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
    # A profile update may give identical modules new mtimes. Checked hashes both
    # survive that update and make CPython reject cached code for an owner edit.
    for folder in ("scripts", "plugins"):
        if not compileall.compile_dir(
            profile / folder, quiet=1, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH
        ):
            raise RuntimeError(f"Could not compile {profile / folder}")
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
# Use Hermes's own mtime/size-validated discovery cache, generated against this image's tools.
from tools.registry import discover_builtin_tools  # noqa: E402

discover_builtin_tools()
shutil.copyfile(home / "cache/tool_discovery_cache.json", source / "tool_discovery_cache.json")
shutil.rmtree("/workspace")
