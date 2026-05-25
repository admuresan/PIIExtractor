# Read genai/toolkits/architectural_instructions before making changes.

"""JSON file stores under ``<toolkit>/local_cache/`` for extraction results."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TOOLKIT_ROOT = Path(__file__).resolve().parents[1]
_CACHE_DIR = _TOOLKIT_ROOT / "local_cache"
_AGENT_FILE = _CACHE_DIR / "agent_store_v1.json"
_DETERMINISTIC_FILE = _CACHE_DIR / "deterministic_store_v1.json"


def cache_directory() -> Path:
    """Absolute path to the toolkit ``local_cache`` directory."""
    return _CACHE_DIR


def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_cache_dir()
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-pii-store-",
        suffix=".json",
        dir=str(_CACHE_DIR),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_agent_store() -> dict[str, Any]:
    return _read_json_object_file(_AGENT_FILE)


def read_deterministic_store() -> dict[str, Any]:
    return _read_json_object_file(_DETERMINISTIC_FILE)


def _read_json_object_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        logger.warning("disk cache read failed %s: %s", path, exc)
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("disk cache invalid JSON %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("disk cache root is not object at %s", path)
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = v
        else:
            logger.warning("skipped invalid disk cache row key=%r path=%s", k, path)
    return out


def write_agent_store(store: dict[str, Any]) -> None:
    if not isinstance(store, dict):
        raise TypeError("store must be dict")
    _atomic_write_json(_AGENT_FILE, dict(store))


def write_deterministic_store(store: dict[str, Any]) -> None:
    if not isinstance(store, dict):
        raise TypeError("store must be dict")
    _atomic_write_json(_DETERMINISTIC_FILE, dict(store))


__all__ = [
    "cache_directory",
    "read_agent_store",
    "read_deterministic_store",
    "write_agent_store",
    "write_deterministic_store",
]
