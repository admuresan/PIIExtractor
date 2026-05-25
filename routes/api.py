# Read genai/toolkits/architectural_instructions before making changes.

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request
from openai_api_key_resolver import resolve_openai_api_key

from services import extraction_job_store, pii_lab_disk_cache
from services.example_catalog import find_example_by_id, load_email_examples
from services.extraction_runner import spawn_extract_job

bp = Blueprint("api", __name__, url_prefix="/api")

logger = logging.getLogger(__name__)


@bp.route("/examples", methods=["GET"])
def get_examples():
    rows = load_email_examples()
    return jsonify({"ok": True, "examples": rows})


@bp.route("/extract", methods=["POST"])
def extract_start():
    """
    Validates the fixture then spawns an async extraction worker.

    Poll ``GET /api/extract/progress?job_id=…`` until ``status`` is
    ``complete`` or ``error`` (same pacing model as requirements_matcher snapshot
    progress polling).
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}

    example_id = str(payload.get("example_id") or "").strip()
    if not example_id:
        return jsonify({"ok": False, "error": "missing_example_id"}), 400

    examples = load_email_examples()
    row = find_example_by_id(examples, example_id)
    if row is None:
        return jsonify({"ok": False, "error": "example_not_found"}), 404

    body_field = row.get("body")
    if not isinstance(body_field, str):
        logger.error("Example %s has non-string body", example_id)
        return jsonify({"ok": False, "error": "invalid_example_body"}), 500

    try:
        resolve_openai_api_key()
    except RuntimeError as exc:
        logger.warning("OpenAI key resolution failed: %s", exc)
        return jsonify(
            {"ok": False, "error": "missing_openai_api_key", "detail": str(exc)},
        ), 503

    job_id = spawn_extract_job(example_id)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/cache/pii-stores", methods=["GET"])
def pii_disk_cache_read():
    """
    Persisted sidebar caches under ``local_cache/*.json`` (disk under this package).
    Responses may mirror fixture-derived PII; do not expose beyond local dev without care.
    """
    return jsonify(
        {
            "ok": True,
            "agent_store": pii_lab_disk_cache.read_agent_store(),
            "deterministic_store": pii_lab_disk_cache.read_deterministic_store(),
            "cache_dir": str(pii_lab_disk_cache.cache_directory()),
        }
    )


@bp.route("/cache/pii-stores", methods=["PUT"])
def pii_disk_cache_write():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid_body"}), 400

    saved: list[str] = []
    if "agent_store" in payload:
        agent = payload["agent_store"]
        if not isinstance(agent, dict):
            return jsonify({"ok": False, "error": "agent_store_not_object"}), 400
        try:
            pii_lab_disk_cache.write_agent_store(agent)
            saved.append("agent_store")
        except OSError:
            logger.exception("Writing agent_store failed")
            return jsonify({"ok": False, "error": "write_failed", "field": "agent_store"}), 500

    if "deterministic_store" in payload:
        det = payload["deterministic_store"]
        if not isinstance(det, dict):
            return jsonify({"ok": False, "error": "deterministic_store_not_object"}), 400
        try:
            pii_lab_disk_cache.write_deterministic_store(det)
            saved.append("deterministic_store")
        except OSError:
            logger.exception("Writing deterministic_store failed")
            return jsonify(
                {"ok": False, "error": "write_failed", "field": "deterministic_store"}
            ), 500

    if not saved:
        return jsonify({"ok": False, "error": "nothing_to_save"}), 400

    return jsonify({"ok": True, "saved": saved})


@bp.route("/extract/progress", methods=["GET"])
def extract_progress():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "missing_job_id"}), 400

    snap = extraction_job_store.snapshot_progress(job_id)
    if snap is None:
        return jsonify({"ok": False, "error": "unknown_job"}), 404

    return jsonify(snap)
