# Read genai/toolkits/architectural_instructions before making changes.

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, render_template

from services.example_catalog import load_email_examples

bp = Blueprint("pages", __name__)

_TOOLKIT_ROOT = Path(__file__).resolve().parents[1]


def _load_ui_strings() -> dict:
    ui_path = _TOOLKIT_ROOT / "content" / "ui_strings.json"
    with ui_path.open(encoding="utf-8") as handle:
        return dict(json.load(handle))


@bp.route("/")
def index():
    ui = _load_ui_strings()
    examples = load_email_examples()
    return render_template(
        "email_pii_lab.html",
        ui=ui,
        boot={
            "examples": examples,
            "uiStrings": ui,
        },
    )
