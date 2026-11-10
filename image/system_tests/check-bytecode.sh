#!/usr/bin/env bash
# Read the importer's verbose records. A cache path alone cannot prove that Python loaded it.
set -euo pipefail
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

PYTHONVERBOSE=1 /opt/hermes/.venv/bin/hermes --version >"$scratch/launcher" 2>&1
PYTHONVERBOSE=1 /opt/hermes/.venv/bin/python -c 'from hermes_cli import config' \
    >"$scratch/native" 2>&1
PYTHONVERBOSE=1 /opt/workspace-env/bin/research --version >"$scratch/workspace" 2>&1

grep -E "^# code object from '$PYTHONPYCACHEPREFIX/.*/hermes_cli/main\.cpython-[0-9]+\.pyc'$" "$scratch/launcher"
grep -E "^# code object from '$PYTHONPYCACHEPREFIX/.*/hermes_cli/config\.cpython-[0-9]+\.pyc'$" "$scratch/native"
grep -E "^# code object from '$PYTHONPYCACHEPREFIX/.*/ruamel/yaml/__init__\.cpython-[0-9]+\.pyc'$" "$scratch/native"
grep -E "^# code object from '$PYTHONPYCACHEPREFIX/.*/research_core/cli\.cpython-[0-9]+\.pyc'$" "$scratch/workspace"
grep -E "^# code object from '$PYTHONPYCACHEPREFIX/.*/opentelemetry/sdk/trace/__init__\.cpython-[0-9]+\.pyc'$" "$scratch/workspace"
echo "PASS packaged bytecode reads: Hermes launcher, native Python and workspace CLI"
