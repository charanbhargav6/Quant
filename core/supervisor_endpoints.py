"""
CRAVE — Process Supervisor API routes (Phase 1)
==================================================
WIRING INTO quant_server.py:
  from core.supervisor_endpoints import register_supervisor_routes
  register_supervisor_routes(app, get_db_agent, log)
  (add this call next to register_account_routes(app, get_db_agent, log))
"""

from flask import jsonify

from core.process_supervisor import get_supervisor


def register_supervisor_routes(app, get_db_agent, log):

    @app.route("/api/ping")
    def api_ping():
        """Lightweight liveness endpoint. Every quant_server.py instance
        (base process AND every --profile child) serves this — it's what
        the supervisor's health-check loop polls per account's port."""
        return jsonify({"status": "ok"})

    @app.route("/api/accounts/<int:account_id>/launch", methods=["POST"])
    def api_account_launch(account_id):
        db = get_db_agent()
        if not db:
            return jsonify({"status": "error", "reason": "Database not available"})
        sup = get_supervisor(db)
        result = sup.launch(account_id)
        return jsonify({"status": "success" if result["ok"] else "error", **result})

    @app.route("/api/accounts/<int:account_id>/stop", methods=["POST"])
    def api_account_stop(account_id):
        db = get_db_agent()
        if not db:
            return jsonify({"status": "error", "reason": "Database not available"})
        sup = get_supervisor(db)
        result = sup.stop(account_id)
        return jsonify({"status": "success" if result["ok"] else "error", **result})

    @app.route("/api/accounts/<int:account_id>/process_status")
    def api_account_process_status(account_id):
        db = get_db_agent()
        if not db:
            return jsonify({"status": "error", "reason": "Database not available"})
        acc = db.get_account(account_id)
        if not acc:
            return jsonify({"status": "error", "reason": "Account not found"})
        return jsonify({
            "status": "success",
            "account_id": account_id,
            "process_status": acc.get("process_status"),
            "pid": acc.get("pid"),
            "port": acc.get("port"),
            "last_health_check": acc.get("last_health_check"),
            "restart_count": acc.get("restart_count"),
            "last_error": acc.get("last_error"),
        })
