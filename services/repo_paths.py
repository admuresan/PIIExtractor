# Read genai/toolkits/architectural_instructions before making changes.

from __future__ import annotations

import os
from pathlib import Path


def toolkit_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_email_examples_json() -> Path:
    return toolkit_root() / "fixtures" / "email_ex.json"


def resolved_email_examples_path() -> Path:
    override = os.environ.get("EMAIL_PII_LAB_EMAIL_EX_JSON", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return default_email_examples_json()
