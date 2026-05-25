# Read genai/toolkits/architectural_instructions before making changes.

"""Background extraction worker feeding ``extraction_job_store``."""

from __future__ import annotations

import logging
import threading
from typing import Any

from openai import OpenAI
from openai_api_key_resolver import resolve_openai_api_key

from services import extraction_job_store
from services.email_extract import extract_with_usage
from services.example_catalog import load_email_examples, find_example_by_id

logger = logging.getLogger(__name__)


def _normalize_extractions(entries: object) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    return [row for row in entries if isinstance(row, dict)]


def run_extract_job(job_id: str, example_id: str) -> None:
    """Execute inside a daemon thread."""

    def log(msg: str) -> None:
        extraction_job_store.append_line(job_id, msg)

    try:
        extraction_job_store.mark_running(job_id)
        log("Extraction started")

        examples = load_email_examples()
        row = find_example_by_id(examples, example_id)
        if row is None:
            log(f"FAILED: Example not found ({example_id!r}).")
            extraction_job_store.fail_job(job_id, "example_not_found")
            return

        body_field = row.get("body")
        if not isinstance(body_field, str):
            log("FAILED: Fixture body missing or invalid type.")
            extraction_job_store.fail_job(job_id, "invalid_example_body")
            return

        log(f'Loaded fixture body ({len(body_field)} characters).')

        try:
            api_key = resolve_openai_api_key()
        except RuntimeError as exc:
            warn = str(exc)
            logger.warning("OpenAI key resolution failed: %s", warn)
            log(f"FAILED: Credential resolution ({warn}).")
            extraction_job_store.fail_job(job_id, warn)
            return

        log("Resolved OpenAI API credential.")

        client = OpenAI(api_key=api_key)
        extractions, usage_bundle = extract_with_usage(
            client,
            body_field,
            on_log=log,
        )

        payload = {
            "extractions": _normalize_extractions(extractions),
            "usage": usage_bundle.get("usage"),
            "durationMs": usage_bundle.get("durationMs"),
            "promptTokenBreakdown": usage_bundle.get("promptTokenBreakdown"),
            "model": usage_bundle.get("model"),
        }

        spans = payload["extractions"]
        log(f"Done — extracted {len(spans)} PI span(s).")
        extraction_job_store.complete_job(job_id, payload)
    except Exception as exc:
        logger.exception("Unexpected extraction crash job=%s", job_id)
        log(f"FAILED: {exc}")
        extraction_job_store.fail_job(job_id, str(exc))


def spawn_extract_job(example_id: str) -> str:
    job_id = extraction_job_store.create_job(example_id=example_id)
    thread = threading.Thread(
        target=run_extract_job,
        kwargs={"job_id": job_id, "example_id": example_id},
        name=f"pii_extract_{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return job_id
