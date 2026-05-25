# Read genai/toolkits/architectural_instructions before making changes.

"""
Normalize streamed log lines — same rules as ``requirements_matcher`` ``progress_store``:
structured JSON logs → ``level: message``, ``[progress]`` strip, empty drop.
"""

from __future__ import annotations

import json


def normalize_log_line(line: str) -> str:
    """Trim and decode stderr-style log lines into a single human-readable line."""
    s = (line or "").strip()
    if not s:
        return ""
    if s.startswith("[progress]"):
        return s[len("[progress]") :].strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return s
        msg = obj.get("message") if isinstance(obj, dict) else None
        if isinstance(msg, str) and msg.strip():
            level = obj.get("level") if isinstance(obj, dict) else None
            return f"{level}: {msg}" if isinstance(level, str) else msg.strip()
        return s
    return s
