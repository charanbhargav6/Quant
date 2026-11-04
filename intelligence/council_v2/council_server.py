"""
CRAVE Council v2 — Standalone Advisory Server
===============================================
Runs as a separate process (port 8766) alongside quant_server.py (:8765).
Provides a REST API for the dashboard to query council debates and
market assessments.

START:  python intelligence/council_v2/council_server.py
        (started automatically by crave_master.py)

Endpoints:
  GET  /health                      liveness probe
  GET  /api/council/status          provider availability + last debate summary
  POST /api/council/debate          trigger a full 3-round debate
  POST /api/council/assess          quick market assessment (no signal needed)
  GET  /api/council/last            last debate result (cached)
  GET  /api/council/providers       which providers are configured and working

Authentication: shared secret via COUNCIL_API_SECRET env var.
Quant server calls this via council_client.py.
"""

import os
import sys
import io
import logging
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

# ── stdout encoding fix for Windows ──────────────────────────────────────────
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Path setup ────────────────────────────────────────────────────────────────
ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ENGINE_ROOT / ".env")
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Council] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("crave.council_v2.server")

# ── Flask ─────────────────────────────────────────────────────────────────────
from flask import Flask, jsonify, request, abort
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://127.0.0.1:8765",
                                              "http://localhost:8765",
                                              "http://127.0.0.1:*"]}})

COUNCIL_PORT   = int(os.environ.get("COUNCIL_PORT", "8766"))
COUNCIL_SECRET = os.environ.get("COUNCIL_API_SECRET", "").strip()

# ── Lazy engine import (don't load LLM clients at import time) ────────────────
_engine = None

def get_engine():
    global _engine
    if _engine is None:
        from intelligence.council_v2.debate_engine import get_debate_engine
        _engine = get_debate_engine()
    return _engine

# ── Auth helper ───────────────────────────────────────────────────────────────
def require_local_or_secret(f):
    """Allow requests from localhost, OR require X-Council-Secret header."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        remote = request.remote_addr
        secret_header = request.headers.get("X-Council-Secret", "")
        is_local = remote in ("127.0.0.1", "::1", "localhost")
        if not is_local and COUNCIL_SECRET and secret_header != COUNCIL_SECRET:
            abort(403, "Forbidden: missing or invalid Council secret")
        return f(*args, **kwargs)
    return wrapper

# ── Rate limiting (simple in-memory, per-endpoint) ───────────────────────────
_last_debate_time: float = 0
DEBATE_COOLDOWN_S = 30   # prevent hammering LLM APIs

# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "council_v2",
                    "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/api/council/providers")
@require_local_or_secret
def api_providers():
    """Return which providers are configured."""
    try:
        from intelligence.council_v2.provider_registry import get_registry
        reg = get_registry()
        available = reg.available_providers()
        return jsonify({
            "available": available,
            "configured": {
                "groq":        bool(os.environ.get("COUNCIL_GROQ_API_KEY", "").strip()),
                "openrouter":  bool(os.environ.get("COUNCIL_GEMINI_API_KEY", "").strip()
                                    and os.environ.get("COUNCIL_GEMINI_BASE_URL", "").strip()),
                "openai":      bool(os.environ.get("COUNCIL_OPENAI_API_KEY", "").strip()),
            },
            "models": {
                "groq":        os.environ.get("COUNCIL_GROQ_MODEL", ""),
                "openrouter":  os.environ.get("COUNCIL_GEMINI_MODEL", ""),
                "openai":      os.environ.get("COUNCIL_OPENAI_MODEL", ""),
            },
            "advisory_only": os.environ.get("COUNCIL_ADVISORY_ONLY", "true").lower() == "true",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/council/status")
@require_local_or_secret
def api_status():
    """Combined status: providers + last debate summary."""
    last = get_engine().get_last_result()
    return jsonify({
        "last_debate": {
            "symbol":         last["symbol"]         if last else None,
            "direction":      last["direction"]       if last else None,
            "recommendation": last["recommendation"] if last else None,
            "confidence":     last["confidence"]     if last else None,
            "timestamp":      last["timestamp"]       if last else None,
        } if last else None,
        "ready": True,
        "advisory_only": os.environ.get("COUNCIL_ADVISORY_ONLY", "true").lower() == "true",
        "cooldown_remaining_s": max(0, DEBATE_COOLDOWN_S - (time.time() - _last_debate_time)),
    })


@app.route("/api/council/last")
@require_local_or_secret
def api_last():
    """Return full last debate result."""
    last = get_engine().get_last_result()
    if not last:
        return jsonify({"status": "no_debate_yet",
                        "message": "No council debate has run this session."}), 200
    return jsonify(last)


@app.route("/api/council/debate", methods=["POST"])
@require_local_or_secret
def api_debate():
    """
    Trigger a full 3-round adversarial debate.
    Body: { "signal": {...}, "session": "...", "daily_bias": "...", ... }
    """
    global _last_debate_time

    # Rate limit
    since_last = time.time() - _last_debate_time
    if since_last < DEBATE_COOLDOWN_S:
        remaining = round(DEBATE_COOLDOWN_S - since_last)
        return jsonify({
            "error": f"Debate cooldown active. Try again in {remaining}s.",
            "cooldown_remaining_s": remaining,
        }), 429

    body = request.get_json(silent=True) or {}
    if not body.get("signal"):
        return jsonify({"error": "Request body must contain 'signal' dict"}), 400

    _last_debate_time = time.time()

    try:
        result = get_engine().run(body)
        return jsonify(result)
    except Exception as e:
        log.error(f"[CouncilServer] Debate failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/council/assess", methods=["POST"])
@require_local_or_secret
def api_assess():
    """
    Quick market assessment (no signal required).
    Body: { "topic": "EURUSD", "context": {...} }
    """
    body    = request.get_json(silent=True) or {}
    topic   = body.get("topic", "market")
    context = body.get("context", {})

    try:
        result = get_engine().quick_market_assessment(topic, context)
        return jsonify(result)
    except Exception as e:
        log.error(f"[CouncilServer] Assessment failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info(f"CRAVE Council v2 Server starting on port {COUNCIL_PORT}")
    log.info(f"Advisory-only mode: {os.environ.get('COUNCIL_ADVISORY_ONLY','true')}")

    from intelligence.council_v2.provider_registry import get_registry
    reg = get_registry()
    available = reg.available_providers()
    log.info(f"Active providers: {available or ['none — check .env keys']}")

    if not available:
        log.warning("No providers configured! Set COUNCIL_GROQ_API_KEY in .env")

    app.run(
        host="127.0.0.1",
        port=COUNCIL_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )

