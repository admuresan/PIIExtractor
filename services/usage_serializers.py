# Read genai/toolkits/architectural_instructions before making changes.

"""OpenAI CompletionUsage → plain dict for toolkit JSON."""

from __future__ import annotations

from typing import Any, Optional


def usage_to_plain_dict(usage: Any) -> Optional[dict[str, Any]]:
    if usage is None:
        return None
    out: dict[str, Any] = {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        if hasattr(details, "model_dump"):
            out["completion_tokens_details"] = details.model_dump()
        elif isinstance(details, dict):
            out["completion_tokens_details"] = dict(details)
        else:
            try:
                out["completion_tokens_details"] = dict(details)  # type: ignore[arg-type]
            except Exception:
                pass
    return out
