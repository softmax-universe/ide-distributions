"""The ``research`` command line.

Subcommands live in their own modules and register through ``COMMANDS``; each module exposes
``add_parser(subparsers)`` and sets ``parser.set_defaults(run=<callable>)``. The handler is called
with the parsed namespace and returns an exit code. Keep this module import-light: it is also
the entry point of tool subprocesses started from inside Hermes.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys

from research_core import __version__

# Load only the requested command; root help still lists every command.
COMMAND_MODULES: dict[str, str] = {
    "context-schema": "research_core.contract.cli",
    "tool": "research_core.tools.cli",
    "store": "research_core.store.cli",
    "platform": "research_core.platform.cli",
    "softmax": "research_core.platform.cli",
    "coworld": "research_core.coworld_cli",
    "watcher": "research_core.watcher.cli",
    "workspace": "research_core.watcher.cli",
    "serve": "research_core.serve",
    "telemetry": "research_core.agent_telemetry.cli",
}


def build_parser(command: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research", description="Softmax IDE research profiles")
    parser.add_argument("--version", action="version", version=f"research {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    modules = (
        (COMMAND_MODULES[command],) if command in COMMAND_MODULES else COMMAND_MODULES.values()
    )
    for module_name in dict.fromkeys(modules):
        importlib.import_module(module_name).add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    from research_core.telemetry import configure_logging

    configure_logging()
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser(argv[0] if argv else None)
    args = parser.parse_args(argv)
    run = getattr(args, "run", None)
    if run is None:
        parser.print_usage(sys.stderr)
        return 2
    # A long-running command replaces this writer with its own. Imported here to stay import-light.
    from research_core.agent_telemetry import observed

    writer = observed.bind_command(os.environ)
    try:
        return int(run(args) or 0)
    finally:
        observed.unbind(writer)


if __name__ == "__main__":  # pragma: no cover - exercised through subprocesses
    sys.exit(main())
