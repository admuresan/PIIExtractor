#!/usr/bin/env python3
# Read genai/toolkits/architectural_instructions before making changes.
"""
Email PII Lab — Flask UI for scripted OpenAI extraction over email fixture JSON.
Runs on port 5011.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_TOOLKIT_ROOT = Path(__file__).resolve().parent
# Bundled `_shared` lives beside this folder (standalone layout), not `toolkits/_shared`.
_SHARED_ROOT = _TOOLKIT_ROOT / "_shared"
sys.path.insert(0, str(_SHARED_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT))

from flask import Flask
from flask_cors import CORS

from routes.api import bp as api_bp
from routes.pages import bp as pages_bp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

app.register_blueprint(pages_bp)
app.register_blueprint(api_bp)

if __name__ == "__main__":
    logger.info("Starting Email PII lab on http://127.0.0.1:5011/")
    app.run(debug=True, host="0.0.0.0", port=5011)
