"""``research telemetry rebuild``: a Session rebuilt from Datadog alone, for an engineer to run."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import uuid
from pathlib import Path

# As metta's ``scripts/token_broker_client.py`` finds the broker and this machine's client token.
BROKER_URL = "https://cdsrezebj75evx33ppyorn43ia0ayckl.lambda-url.us-east-1.on.aws"
BROKER_TOKEN_FILE = Path.home() / ".config" / "token-broker" / "token"


def _broker_token() -> str | None:
    if token := os.environ.get("TOKEN_BROKER_TOKEN"):
        return token
    if BROKER_TOKEN_FILE.exists():
        return BROKER_TOKEN_FILE.read_text().strip() or None
    return None


def run_rebuild(args: argparse.Namespace) -> int:
    from research_core.agent_telemetry import rebuild

    token = _broker_token()
    if token is None:
        print("Needs a token broker token: see docs/agent-telemetry.md", file=sys.stderr)
        return 2
    search = rebuild.BrokerLogsSearch(
        os.environ.get("TOKEN_BROKER_URL") or BROKER_URL,
        token,
        args.reason or f"telemetry rebuild of Session {args.session}",
        os.environ.get("TOKEN_BROKER_SESSION_ID") or uuid.uuid4().hex,
    )
    try:
        report = rebuild.rebuild_session(search, args.session, args.workspace)
    except rebuild.AmbiguousSession as error:
        print(f"Several workspaces hold this Session ID, pick one: {error}", file=sys.stderr)
        return 2
    except (OSError, http.client.HTTPException, rebuild.IncompleteSearch) as error:
        # An error's body says what was wrong with the query, which holds only IDs and digests.
        detail = ""
        if isinstance(error, urllib.error.HTTPError):
            detail = " ".join(error.read(300).decode("utf-8", "replace").split())
        print(f"Could not search Datadog: {error} {detail}".rstrip(), file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False)
    # Not 1, which an uncaught Python error also exits with.
    return 3 if report["missing"] else 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("telemetry", help="Rebuild a Session from Datadog")
    commands = parser.add_subparsers(dest="telemetry_command", required=True)
    rebuild = commands.add_parser(
        "rebuild",
        help="Print a Session rebuilt from Datadog alone as JSON (through the token broker)",
    )
    rebuild.add_argument("--session", required=True, help="A Session ID, first or after compaction")
    rebuild.add_argument("--workspace", help="Needed only when several workspaces hold the ID")
    rebuild.add_argument(
        "--reason",
        help="Why, for the token broker's audit log (default: telemetry rebuild of Session <id>)",
    )
    rebuild.set_defaults(run=run_rebuild)
