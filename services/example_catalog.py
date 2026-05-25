# Read genai/toolkits/architectural_instructions before making changes.

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from services.repo_paths import resolved_email_examples_path

logger = logging.getLogger(__name__)


def load_email_examples(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or resolved_email_examples_path()
    if not target.is_file():
        logger.warning("Email examples file missing at %s", target)
        return []
    with target.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON array in {target}")
    out: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, dict):
            out.append(row)
    return out


def find_example_by_id(
    examples: list[dict[str, Any]],
    example_id: str,
) -> dict[str, Any] | None:
    for row in examples:
        if str(row.get("id", "")) == example_id:
            return row
    return None
