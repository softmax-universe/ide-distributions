"""The client contract files the profiles publish (design §10, A.2) and their validator."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Literal

CONTRACT_VERSION = 1
CONTRACT_DIR = Path(__file__).resolve().parent
SchemaName = Literal["context", "events"]


@cache
def schema(name: SchemaName) -> dict:
    return json.loads((CONTRACT_DIR / f"{name}.schema.json").read_text())


def validate_context(block: object) -> tuple[dict, list[str]]:
    from research_core.contract.validate import validate

    return validate(block, schema("context"))
