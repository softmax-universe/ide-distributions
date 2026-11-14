"""The two platform identities of a workspace and where their credentials come from (B.16).

Player: ``SOFTMAX_PLAYER_CREDENTIAL`` (hosting writes it), either a bare token or the JSON
``{"token": ..., "expires_at": ...}`` of a minted player session. Every profile has it.

User: ``SOFTMAX_USER_CREDENTIAL`` when hosting provisions it (Orchestrator only, later), else
the token the ``softmax`` CLI saved with ``softmax login`` in ``credentials.yaml``. Absent means
"User identity not provisioned" (:class:`IdentityUnavailable`), never a silent fallback to the
Player.

``credentials.yaml`` (schema from ``softmax_client.credentials``) lives in ``$SOFTMAX_CONFIG_DIR``
(default ``~/.softmax``) and is keyed by server URL without a trailing slash::

    tokens:                      # server -> the USER token
      https://softmax.com/api: <token>
    player_sessions:             # server -> active player session (`softmax player use`)
      https://softmax.com/api:
        active: ply_...
        cache:
          ply_...: {token: ..., expires_at: 2026-10-29T12:00:00Z}

``softmax_client.credentials.load_current_token`` prefers an unexpired active player session over
``tokens[server]``. :func:`write_credentials_dir` puts the Player token into ``tokens[server]`` so
a ``coworld``/``softmax`` CLI run with that config dir acts as the Player without a session row.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from softmax_client import credentials as softmax_credentials

from research_core.platform.errors import IdentityUnavailable, PlatformError
from research_core.platform.protocol import Identity

DEFAULT_SERVER = "https://softmax.com/api"

PLAYER_CREDENTIAL_VAR = "SOFTMAX_PLAYER_CREDENTIAL"
PLAYER_ID_VAR = "SOFTMAX_PLAYER_ID"
USER_CREDENTIAL_VAR = "SOFTMAX_USER_CREDENTIAL"
SERVER_VAR = "COGAMES_API_URL"


@dataclass(frozen=True)
class PlayerCredential:
    token: str
    expires_at: str | None = None  # ISO 8601, when the credential is a minted player session


class IdentityAccessToken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    access_token: str = Field(min_length=1, repr=False)
    token_type: Literal["Bearer"]
    expires_in: int = Field(gt=0, le=300)


def server(env: Mapping[str, str]) -> str:
    """The Softmax API server (``COGAMES_API_URL``), without a trailing slash."""
    return (env.get(SERVER_VAR) or DEFAULT_SERVER).rstrip("/")


def player_credential(env: Mapping[str, str]) -> PlayerCredential:
    """Parse ``SOFTMAX_PLAYER_CREDENTIAL``; an expired player session is refused outright."""
    require_owner_credentials(env)
    raw = (env.get(PLAYER_CREDENTIAL_VAR) or "").strip()
    if not raw:
        raise IdentityUnavailable("player")
    if not raw.startswith("{"):
        return PlayerCredential(token=raw)
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise PlatformError(
            f"{PLAYER_CREDENTIAL_VAR} is not valid JSON: {error}", retryable=False
        ) from error
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise PlatformError(
            f'{PLAYER_CREDENTIAL_VAR} JSON must carry a "token" string', retryable=False
        )
    expires_at = data.get("expires_at")
    if expires_at is not None:
        expiry = _parse_timestamp(expires_at)
        if expiry <= datetime.now(UTC):
            raise PlatformError(
                f"Player session expired at {expiry.isoformat()}; {PLAYER_CREDENTIAL_VAR} must be "
                "refreshed by the host (a player session lasts 24 hours)",
                retryable=False,
            )
    return PlayerCredential(token=token, expires_at=str(expires_at) if expires_at else None)


def player_token(env: Mapping[str, str]) -> str:
    return player_credential(env).token


def player_id(env: Mapping[str, str]) -> str | None:
    """The workspace's Player id (``SOFTMAX_PLAYER_ID``), when hosting set it."""
    return env.get(PLAYER_ID_VAR) or None


def user_token(env: Mapping[str, str]) -> str | None:
    """The User token: ``SOFTMAX_USER_CREDENTIAL``, else the ``softmax login`` token on disk.

    The saved token is read from ``$SOFTMAX_CONFIG_DIR/credentials.yaml`` with the directory
    taken from ``env`` (the library's ``load_user_token`` only reads ``os.environ``). A saved
    token equal to the Player credential is not a User identity: hosts write the Player token
    into that slot so the CLIs act as the Player (see :func:`write_credentials_dir`).
    """
    require_owner_credentials(env)
    token = (env.get(USER_CREDENTIAL_VAR) or "").strip()
    if token:
        return token
    saved = _saved_user_token(env)
    if saved is None:
        return None
    player = (env.get(PLAYER_CREDENTIAL_VAR) or "").strip()
    if player and saved == player_credential(env).token:
        return None
    return saved


def token_for(env: Mapping[str, str], identity: Identity) -> str:
    """The bearer token for ``identity``; ``IdentityUnavailable`` when the User is absent."""
    if identity == "player":
        return player_token(env)
    token = user_token(env)
    if token is None:
        raise IdentityUnavailable("user")
    return token


def require_owner_credentials(env: Mapping[str, str]) -> None:
    session_id = env.get("IDE_SESSION_ID") or env.get("HERMES_SESSION_ID")
    if not session_id:
        return
    from research_core.authority import OwnerAuthority, bound_authority

    if not isinstance(bound_authority(env, session_id), OwnerAuthority):
        raise PlatformError("This conversation has no workspace credentials", retryable=False)


def write_credentials_dir(env: Mapping[str, str], dest: Path) -> Path:
    """Write ``dest/credentials.yaml`` holding the Player token as the server's token.

    For ``research softmax exec``: a child ``coworld``/``softmax`` process started with
    ``SOFTMAX_CONFIG_DIR=dest`` then resolves ``load_current_token`` to the Player. The file is
    written through the library's own model so it stays valid for the installed CLI. Returns the
    file path.
    """
    data = softmax_credentials.Credentials(tokens={server(env): player_token(env)})
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = dest / softmax_credentials.CREDENTIALS_FILE
    path.write_text(yaml.safe_dump(data.model_dump(mode="json"), default_flow_style=False))
    os.chmod(path, 0o600)
    return path


def credentials_file(env: Mapping[str, str]) -> Path:
    """Where the ``softmax`` CLI keeps its credentials for this environment."""
    config_dir = env.get(softmax_credentials.CONFIG_DIR_ENV)
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".softmax"
    return base / softmax_credentials.CREDENTIALS_FILE


def _saved_user_token(env: Mapping[str, str]) -> str | None:
    path = credentials_file(env)
    if not path.is_file():
        return None
    data = softmax_credentials.Credentials.model_validate(yaml.safe_load(path.read_text()) or {})
    return data.tokens.get(server(env))


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise PlatformError(
            f"{PLAYER_CREDENTIAL_VAR} expires_at is not a timestamp: {value!r}", retryable=False
        ) from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
