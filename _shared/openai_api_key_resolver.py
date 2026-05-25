# See architectural_instructions before making changes.
"""
Resolve the OpenAI API key the same way as agent/requirements tooling.

Resolution order after optional project ``.env`` load (standalone default:

``<PIIExtractor>/.env`` next to ``app.py``; override via ``GENAI_DOTENV_PATH``):

1. ``OPENAI_API_KEY`` — plaintext from the environment (local dev).

2. S3 object (UTF-8 text or single-line quoted key):

   - ``OPENAI_API_KEY_S3_URI`` ``s3://bucket/key``

   **or**

   - ``OPENAI_API_KEY_S3_BUCKET`` + ``OPENAI_API_KEY_S3_KEY``

3. ``OPENAI_API_KEY_PARAM_NAME`` — SSM SecureString (with decryption).

``REGION`` is required whenever the key comes from AWS (S3 or SSM).
``OPENAI_API_KEY`` alone suffices for purely local setups without AWS calls.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_cached_key: Optional[str] = None
_dotenv_loaded = False


def ensure_genai_dotenv_loaded() -> None:
    """
    Merge the default project ``.env`` into ``os.environ`` once (never overrides
    existing vars). Default file is ``<PIIExtractor>/.env`` for this standalone copy.

    If ``GENAI_DOTENV_PATH`` is set to an explicit file path, that file loads instead.
    ``python-dotenv`` is optional; without it missing keys must be exported manually.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True

    default_dotenv = Path(__file__).resolve().parents[1] / ".env"
    dotenv_arg = (
        Path(os.environ.get("GENAI_DOTENV_PATH", str(default_dotenv))).expanduser()
    )

    try:
        from dotenv import load_dotenv
    except ImportError:
        if dotenv_arg.is_file():
            logger.warning(
                "python-dotenv is not installed; skipping %s. Install python-dotenv or export env vars.",
                dotenv_arg,
            )
        return

    if dotenv_arg.is_file():
        load_dotenv(dotenv_arg, override=False)
        logger.info("Merged environment defaults from %s", dotenv_arg)


def clear_openai_api_key_cache() -> None:
    """Invalidate in-process cache (tests / key rotation)."""
    global _cached_key
    _cached_key = None


def clear_openai_key_cache() -> None:
    """Alias matching historical ``requirements_matcher`` spelling."""
    clear_openai_api_key_cache()


def _strip_quotes(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {"'", '"'}:
        return s[1:-1].strip()
    return s


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    u = uri.strip()
    if not u.lower().startswith("s3://"):
        raise ValueError("OPENAI_API_KEY_S3_URI must start with s3://")
    rest = u[len("s3://") :].lstrip("/")
    if "/" not in rest:
        raise ValueError(
            'OPENAI_API_KEY_S3_URI must look like "s3://bucket/object-key"'
        )
    bucket, _, key_name = rest.partition("/")
    bucket, key_name = bucket.strip(), key_name.strip()
    if not bucket or not key_name:
        raise ValueError("OPENAI_API_KEY_S3_URI is missing bucket or key")
    return bucket, key_name


def openai_api_key_maybe_configured() -> bool:
    """
    Lightweight check with no AWS I/O — ``ensure_genai_dotenv_loaded()`` runs first.
    """
    ensure_genai_dotenv_loaded()
    if (os.environ.get("OPENAI_API_KEY") or "").strip():
        return True
    if (os.environ.get("OPENAI_API_KEY_S3_URI") or "").strip():
        return True
    bucket = (os.environ.get("OPENAI_API_KEY_S3_BUCKET") or "").strip()
    key_name = (os.environ.get("OPENAI_API_KEY_S3_KEY") or "").strip()
    if bucket and key_name:
        return True
    if (os.environ.get("OPENAI_API_KEY_PARAM_NAME") or "").strip():
        return True
    return False


def resolve_openai_api_key() -> str:
    """Return non-empty OpenAI API key text or raise ``RuntimeError``."""
    global _cached_key

    ensure_genai_dotenv_loaded()

    if _cached_key:
        return _cached_key

    direct = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if direct:
        _cached_key = direct
        return _cached_key

    region = (os.environ.get("REGION") or "").strip()
    if not region:
        raise RuntimeError(
            "Missing env var REGION. Set REGION in genai/.env whenever using "
            "OPENAI_API_KEY_PARAM_NAME (SSM) or OPENAI_API_KEY_S3_* (S3)."
        )

    uri_flag = bool((os.environ.get("OPENAI_API_KEY_S3_URI") or "").strip())
    bucket = (os.environ.get("OPENAI_API_KEY_S3_BUCKET") or "").strip()
    key_name = (os.environ.get("OPENAI_API_KEY_S3_KEY") or "").strip()

    wants_s3 = uri_flag or (bool(bucket) and bool(key_name))
    if uri_flag and (bucket or key_name):
        raise RuntimeError(
            "Set OPENAI_API_KEY_S3_URI **or** OPENAI_API_KEY_S3_BUCKET/OPENAI_API_KEY_S3_KEY, not both."
        )
    if (bucket or key_name) and not (bucket and key_name):
        raise RuntimeError(
            "Both OPENAI_API_KEY_S3_BUCKET and OPENAI_API_KEY_S3_KEY must be set together."
        )

    if wants_s3:
        if uri_flag:
            bucket, key_name = _parse_s3_uri(os.environ["OPENAI_API_KEY_S3_URI"])

        from aws_clients import read_client

        s3_client = read_client("s3", region)
        try:
            blob = s3_client.get_object(Bucket=bucket, Key=key_name)
            body_txt = blob["Body"].read().decode("utf-8", errors="strict")
        except Exception as exc:
            logger.exception("S3 get_object failed s3://%s/%s", bucket, key_name)
            raise RuntimeError(
                f"Could not read OpenAI API key from S3 s3://{bucket}/{key_name}: {exc}"
            ) from exc

        first_line = body_txt.strip().splitlines()[0].strip()
        normalized = _strip_quotes(first_line)
        if not normalized:
            raise RuntimeError(f"S3 secret s3://{bucket}/{key_name} decoded to empty key text")

        _cached_key = normalized
        logger.info("Resolved OpenAI API key from S3 s3://%s/%s", bucket, key_name)
        return _cached_key

    param_name = (os.environ.get("OPENAI_API_KEY_PARAM_NAME") or "").strip()
    if param_name:
        from aws_clients import read_client

        ssm = read_client("ssm", region)
        try:
            resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
        except Exception as exc:
            logger.exception("SSM get_parameter failed for %s", param_name)
            raise RuntimeError(
                "Could not read OpenAI API key from Parameter Store "
                f"({param_name}): {exc}"
            ) from exc

        parm = resp.get("Parameter") or {}
        value = (parm.get("Value") or "").strip()
        if not value:
            raise RuntimeError(
                "OpenAI API key not found or empty in Parameter Store "
                f"for '{param_name}'."
            )

        _cached_key = value
        logger.info("Resolved OpenAI API key from Parameter Store: %s", param_name)
        return _cached_key

    raise RuntimeError(
        "Neither OPENAI_API_KEY nor OPENAI_API_KEY_PARAM_NAME nor S3 OpenAI "
        "key variables are set. Set OPENAI_API_KEY for local dev, configure SSM, "
        "or OPENAI_API_KEY_S3_URI (+ REGION)."
    )
