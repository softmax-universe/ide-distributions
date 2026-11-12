"""``research telemetry rebuild``: a Session rebuilt from Datadog alone, for an engineer to run."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys


def run_rebuild(args: argparse.Namespace) -> int:
    from research_core.agent_telemetry import rebuild

    keys = [os.environ.get("DD_API_KEY"), os.environ.get("DD_APP_KEY")]
    if not all(keys):
        print("DD_API_KEY and DD_APP_KEY must be set: see docs/agent-telemetry.md", file=sys.stderr)
        return 2
    search = rebuild.LogsSearch(args.datadog_url, keys[0], keys[1])
    try:
        report = rebuild.rebuild_session(search, args.session, args.workspace)
    except rebuild.AmbiguousSession as error:
        print(f"Several workspaces hold this Session ID, pick one: {error}", file=sys.stderr)
        return 2
    except (OSError, http.client.HTTPException, rebuild.IncompleteSearch) as error:
        print(f"Could not search Datadog: {error}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False)
    # Not 1, which an uncaught Python error also exits with.
    return 3 if report["missing"] else 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("telemetry", help="Rebuild a Session from Datadog")
    commands = parser.add_subparsers(dest="telemetry_command", required=True)
    rebuild = commands.add_parser(
        "rebuild",
        help="Print a Session rebuilt from Datadog alone as JSON (needs DD_API_KEY, DD_APP_KEY)",
    )
    rebuild.add_argument("--session", required=True, help="A Session ID, first or after compaction")
    rebuild.add_argument("--workspace", help="Needed only when several workspaces hold the ID")
    rebuild.add_argument("--datadog-url", default="https://api.datadoghq.com")
    rebuild.set_defaults(run=run_rebuild)
