# Read genai/toolkits/architectural_instructions before making changes.

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any, Optional

from openai import OpenAI

from services.prompt_token_estimate import summarize_extract_prompt_breakdown
from services.usage_serializers import usage_to_plain_dict

MODEL_NAME = (os.environ.get("EMAIL_PII_LAB_MODEL") or "gpt-5-mini").strip()
# Passed to chat.completions as `reasoning_effort` when supported (`low`|`medium`|`high`).
# Default `low`; set `EMAIL_PII_LAB_REASONING_EFFORT` to empty to omit entirely.
_REASONING_RAW = os.environ.get("EMAIL_PII_LAB_REASONING_EFFORT")
REASONING_EFFORT: Optional[str]
if _REASONING_RAW is None:
    REASONING_EFFORT = "low"
else:
    REASONING_EFFORT = _REASONING_RAW.strip() or None

SYSTEM_MESSAGE = """\
You identify personal information spans inside the user's email body.

For each genuine instance, return character offsets into the PROVIDED body \
string only: `start` is inclusive, `end` is exclusive, using Python slicing \
rules (0-based indexing by Unicode scalar values, identical to slicing the \
provided string with `body[start:end]`).

Classify each span using exactly one of these labels:
  - phone number
  - address
  - date of birth
  - name

Do NOT include invoice totals, gate codes, employee IDs, changelog tokens, joke \
references, placeholders explicitly marked as non-information, fictional \
addresses the text says to ignore, or other strings the author disqualifies—even \
if they resemble PII."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "extractions": {
            "type": "array",
            "description": (
                "Ordered list of detected personal-information spans with offsets "
                "into the input body."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "info_type": {
                        "type": "string",
                        "enum": [
                            "phone number",
                            "address",
                            "date of birth",
                            "name",
                        ],
                    },
                    "info_value": {"type": "string"},
                },
                "required": ["start", "end", "info_type", "info_value"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["extractions"],
    "additionalProperties": False,
}


def load_examples_any(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return list(raw)


def _build_messages(body: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM_MESSAGE},
        {"role": "user", "content": body},
    ]


def extract_with_usage(
    client: OpenAI,
    body: str,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Run extraction once; returns (extractions, usage_meta suitable for toolkit JSON).

    ``usage_meta`` matches requirement-assistant naming: usage, durationMs,
    promptTokenBreakdown (tiktoken estimates), model.

    ``on_log`` receives human-readable milestones (shown in toolkit progress UI).
    """

    def lg(msg: str) -> None:
        if on_log is not None:
            on_log(msg)

    messages = _build_messages(body)
    lg("Computing tiktoken heuristic for the prompt.")
    prompt_breakdown = summarize_extract_prompt_breakdown(messages)
    approx = prompt_breakdown.get("estimatedPromptTokens")
    if isinstance(approx, (int, float)):
        lg(f"Estimated prompt (~{int(round(approx))} tokens, heuristic pre-request).")

    started = time.perf_counter()

    eff = REASONING_EFFORT
    eff_hint = f", reasoning_effort={eff}" if eff else ""
    lg(
        f"Calling OpenAI chat.completions (`{MODEL_NAME}`, structured outputs{eff_hint})…"
    )
    kwargs: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "email_pii_extractions",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
    }
    if eff:
        kwargs["reasoning_effort"] = eff
        try:
            completion = client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("reasoning_effort", None)
            lg("SDK rejected reasoning_effort; retrying completion without it.")
            completion = client.chat.completions.create(**kwargs)
    else:
        completion = client.chat.completions.create(**kwargs)

    duration_ms = int(max(0.0, (time.perf_counter() - started) * 1000.0))
    lg(f"Completion finished in ~{duration_ms} ms.")

    payload = completion.choices[0].message.content or ""
    data = json.loads(payload)
    extractions = list(data["extractions"])
    lg(f"Parsed structured JSON ({len(extractions)} extraction row(s)).")

    meta: dict[str, Any] = {
        "usage": usage_to_plain_dict(getattr(completion, "usage", None)),
        "durationMs": duration_ms,
        "promptTokenBreakdown": prompt_breakdown,
        "model": MODEL_NAME,
    }
    return extractions, meta


def extract_from_body(client: OpenAI, body: str) -> list[dict[str, Any]]:
    extractions, _meta = extract_with_usage(client, body)
    return extractions
