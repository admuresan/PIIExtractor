# Read genai/toolkits/architectural_instructions before making changes.

"""Tiktoken-based prompt estimates (aligned with assistant preview patterns)."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_ENCODING_MODEL = "gpt-5-mini"
_encoder_last_key: str | None = None
_encoder: Any = None
_tiktoken_unavailable = False


def _resolve_token_model_for_encoder() -> str:
    raw = (
        os.environ.get("EMAIL_PII_LAB_TOKEN_MODEL")
        or os.environ.get("EMAIL_PII_LAB_MODEL")
        or _DEFAULT_ENCODING_MODEL
    )
    return (raw or _DEFAULT_ENCODING_MODEL).strip()


def _get_encoder() -> Any | None:
    global _encoder, _encoder_last_key, _tiktoken_unavailable
    if _tiktoken_unavailable:
        return None
    model_key = _resolve_token_model_for_encoder()
    if _encoder is not None and _encoder_last_key == model_key:
        return _encoder
    try:
        import tiktoken
    except ImportError:
        _tiktoken_unavailable = True
        logger.warning(
            "tiktoken not installed — prompt token breakdown uses chars/4 heuristic"
        )
        _encoder = None
        _encoder_last_key = None
        return None
    try:
        _encoder = tiktoken.encoding_for_model(model_key)
    except KeyError:
        _encoder = tiktoken.get_encoding("o200k_base")
    _encoder_last_key = model_key
    return _encoder


def _count_text_tokens(text: str) -> int:
    enc = _get_encoder()
    if enc is None:
        return max(1, len(text) // 4) if text else 1
    if not text:
        return 1
    return max(1, len(enc.encode(text)))


def _estimate_tokens_for_message_content(content: Any) -> int:
    if isinstance(content, str):
        return _count_text_tokens(content)
    if isinstance(content, list):
        n = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                txt = part.get("text")
                if isinstance(txt, str):
                    n += _count_text_tokens(txt)
        return max(1, n) if n else 1
    return 1


def _gather_labels(messages: List[Dict[str, Any]]) -> List[str]:
    fixed = ("System", "Email body")
    labels: List[str] = []
    for i in range(min(len(messages), len(fixed))):
        labels.append(fixed[i])
    for idx in range(len(fixed), len(messages)):
        role = str(messages[idx].get("role") or "").strip()
        labels.append(f"Message {idx}: {role}")
    while len(labels) < len(messages):
        labels.append(f"Message {len(labels)}")
    return labels


def _preview_rows(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    lbl = _gather_labels(messages)
    rows: List[Dict[str, Any]] = []
    total = 0
    for i, m in enumerate(messages):
        est = _estimate_tokens_for_message_content(m.get("content"))
        total += est
        rows.append(
            {
                "index": i,
                "usageLabel": lbl[i] if i < len(lbl) else f"Message {i}",
                "estimatedTokens": est,
            }
        )
    return rows, total


def tokenizer_estimate_note() -> str:
    enc = _get_encoder()
    m = _resolve_token_model_for_encoder()
    if enc is None:
        return "text ~4 chars/token (install tiktoken for tighter estimates)"
    enc_name = getattr(enc, "name", "?")
    return f"tiktoken ({m!r} → {enc_name}); encoding chosen for EMAIL_PII_LAB_TOKEN_MODEL"


def summarize_extract_prompt_breakdown(
    api_messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    rows, total = _preview_rows(api_messages)
    return {
        "rows": [
            {
                "index": r["index"],
                "usageLabel": r["usageLabel"],
                "estimatedTokens": r["estimatedTokens"],
            }
            for r in rows
        ],
        "estimatedPromptTokens": total,
        "tokenEstimateNote": tokenizer_estimate_note(),
    }
