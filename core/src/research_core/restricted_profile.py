"""Install the model-only profile without importing an owner's mutable profile."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path

PROFILE = "restricted"
INFERENCE_KEY = "IDE_RESTRICTED_INFERENCE_KEY"
INFERENCE_SENTINEL = "restricted-authority-check-required"
ENVIRONMENT = frozenset(
    {
        "API_SERVER_KEY",
        "API_SERVER_HOST",
        "API_SERVER_PORT",
        INFERENCE_KEY,
        "IDE_WORKSPACE",
        "IDE_INFERENCE_LEASE_FILE",
        "IDE_AUTHORITY_CHECK_URL",
        "SOFTMAX_PLAYER_CREDENTIAL",
        "IDE_REASONING_RELAY",
    }
)


def prepare_restricted_profile(home: Path, source: Path, env: Mapping[str, str]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / PROFILE, home, dirs_exist_ok=True)
    (home / ".no-bundled-skills").touch()
    local = home / "local"
    local.mkdir(exist_ok=True)
    (local / "core-path.txt").write_text(str(source / "core" / "src") + "\n")
    context = home / "conversation"
    context.mkdir(exist_ok=True)
    values = {key: value for key, value in env.items() if key in ENVIRONMENT}
    values["OPENROUTER_API_KEY"] = INFERENCE_SENTINEL
    if "OPENROUTER_API_KEY" in env:
        values[INFERENCE_KEY] = env["OPENROUTER_API_KEY"]
    values["TERMINAL_CWD"] = str(context)
    dotenv = home / ".env"
    dotenv.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()))
    dotenv.chmod(0o600)
    # A nonempty profile pool prevents Hermes from borrowing root auth.json credentials.
    auth = home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openrouter": [
                        {
                            "id": "restricted-sentinel",
                            "source": "manual",
                            "access_token": INFERENCE_SENTINEL,
                        }
                    ]
                }
            }
        )
        + "\n"
    )
    auth.chmod(0o600)
