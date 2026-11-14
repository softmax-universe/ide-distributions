"""Execution identity comes from the authenticated bridge, never from model context."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

if TYPE_CHECKING:
    from research_core.store.db import Store


class OwnerAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["owner"] = "owner"


class RestrictedIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["restricted"] = "restricted"
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    subject_type: Literal["person", "cogent"]
    client_id: str = Field(min_length=1)
    player_id: str = Field(
        pattern=r"^ply_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    grant_id: UUID


class RestrictedAuthority(RestrictedIdentity):
    expires_at: int = Field(gt=0)
    authorization_id: UUID


Authority = Annotated[OwnerAuthority | RestrictedAuthority, Field(discriminator="mode")]
SessionIdentity = Annotated[OwnerAuthority | RestrictedIdentity, Field(discriminator="mode")]
AUTHORITY = TypeAdapter(Authority)
SESSION_IDENTITY = TypeAdapter(SessionIdentity)
OWNER = OwnerAuthority()


class AuthorityDenied(PermissionError):
    """The original execution grant no longer permits this operation."""


class AuthorityUnavailable(AuthorityDenied):
    """The grant could not be checked; no new effect is authorized."""


class AuthorityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    active: bool


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_authority(value: object) -> Authority:
    return AUTHORITY.validate_python(value)


def identity(authority: Authority) -> SessionIdentity:
    if isinstance(authority, OwnerAuthority):
        return authority
    return RestrictedIdentity.model_validate(
        authority.model_dump(exclude={"expires_at", "authorization_id"})
    )


def require_same(expected: SessionIdentity | dict, authority: Authority) -> None:
    if SESSION_IDENTITY.validate_python(expected) != identity(authority):
        raise AuthorityDenied("Conversation belongs to a different execution grant")


def require_active(authority: Authority, env: Mapping[str, str] | None = None) -> None:
    if isinstance(authority, OwnerAuthority):
        return
    if authority.expires_at <= time.time():
        raise AuthorityDenied("Execution authorization expired")
    settings = os.environ if env is None else env
    url = settings.get("IDE_AUTHORITY_CHECK_URL")
    credential = settings.get("SOFTMAX_PLAYER_CREDENTIAL")
    if not url or not credential:
        raise AuthorityUnavailable("Execution authorization service is unavailable")
    try:
        request = urllib.request.Request(
            url,
            data=authority.model_dump_json().encode(),
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
            result = AuthorityCheck.model_validate_json(response.read())
    except (OSError, ValueError) as error:
        if authority.expires_at <= time.time():
            raise AuthorityDenied("Execution authorization expired") from error
        raise AuthorityUnavailable("Execution authorization could not be verified") from error
    if not result.active or authority.expires_at <= time.time():
        raise AuthorityDenied("Execution authorization is no longer active")


def execution_authority(store: Store, session_id: str) -> Authority:
    session = store.one("SELECT authority FROM sessions WHERE id=?", (session_id,))
    if session is None:
        raise AuthorityDenied("Execution has no registered conversation")
    expected = SESSION_IDENTITY.validate_json(session["authority"])
    if isinstance(expected, OwnerAuthority):
        return OWNER
    admissions = store.q(
        "SELECT authority FROM client_admissions WHERE session_id=?"
        " AND kind='message' AND dispatch_started=1"
        " AND (status IN ('queued','running') OR execution_uncertain=1)",
        (session_id,),
    )
    if len(admissions) != 1:
        raise AuthorityDenied("Execution has no unique active admission")
    authority = parse_authority(json.loads(admissions[0]["authority"]))
    require_same(expected, authority)
    return authority


def bound_authority(env: Mapping[str, str], session_id: str) -> Authority:
    if not session_id:
        raise AuthorityDenied("Execution has no registered conversation")
    from research_core import layout
    from research_core.store.db import Store

    with Store.open(layout.from_env(env).store_path, kind="workspace") as store:
        return execution_authority(store, session_id)
