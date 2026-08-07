"""
Drop-in replacement for the account-related routes in quant_server.py.

WHAT CHANGES FROM THE ORIGINAL:
  1. /api/accounts/add no longer trusts the client. It calls
     account_connector.verify_and_connect() and only writes to the DB /
     .env file if the broker actually confirmed the login.
  2. Credentials are encrypted at rest (Fernet) instead of stored as
     plaintext columns — see encrypt_secret()/decrypt_secret().
  3. New /api/prop_firms endpoint exposes PropFirmGuard.FIRM_RULES so the
     UI can render real metrics the moment the user picks an account type,
     before they've typed any credentials.
  4. New /api/accounts/zerodha/complete endpoint finishes the Zerodha OAuth
     handshake (Zerodha can never be verified in the single /add call).

WIRING INTO quant_server.py:
  - Replace the existing @app.route("/api/accounts/add") handler with
    api_accounts_add() below.
  - Add api_prop_firms() and api_zerodha_complete() as new routes.
  - Add: from core.account_connector import verify_and_connect, complete_zerodha_login
  - Add: from core.secrets_vault import encrypt_secret, decrypt_secret
  - Requires: pip install cryptography (for Fernet)
  - Requires: an ACCOUNT_ENCRYPTION_KEY in .env — generate once with
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from pathlib import Path
from flask import request, jsonify

from core.account_connector import verify_and_connect, complete_zerodha_login
from core.secrets_vault import encrypt_secret
from core.prop_firm_guard import FIRM_RULES


def _write_profile_env(profile: str, values: dict):
    """
    Write .env.<profile> next to .env, copying every non-credential line
    from the base .env and overwriting only the keys in `values`. Any value
    that is None/empty is skipped (left out of the file entirely) rather
    than written as the literal string "None".
    """
    engine_root = Path(__file__).parent.parent
    base_env = engine_root / ".env"
    new_env = engine_root / f".env.{profile}"

    keys_to_replace = set(values.keys())
    new_lines = []
    if base_env.exists():
        with open(base_env, "r", encoding="utf-8") as f:
            for line in f:
                ls = line.strip()
                if not ls or ls.startswith("#"):
                    new_lines.append(line)
                    continue
                k = ls.split("=")[0].strip()
                if k not in keys_to_replace:
                    new_lines.append(line)

    new_lines.append(f"\n# --- Multi-Instance Config: {profile.upper()} ---\n")
    for k, v in values.items():
        if v is None or v == "":
            continue
        new_lines.append(f"{k}={v}\n")

    with open(new_env, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def register_account_routes(app, get_db_agent, log):

    @app.route("/api/prop_firms")
    def api_prop_firms():
        """
        Real metrics per account type, for Step 1 of the Add Account flow.
        Frontend calls this on account-type dropdown change and renders the
        result BEFORE the user enters any credentials.
        """
        out = []
        for key, rules in FIRM_RULES.items():
            out.append({
                "key": key,
                "name": rules["name"],
                "max_loss_pct": rules["max_loss_pct"],
                "daily_loss_pct": rules["daily_loss_pct"],
                "profit_target": rules["profit_target"],
                "min_trading_days": rules["min_trading_days"],
                "drawdown_type": rules["drawdown_type"],
                "news_restriction": rules["news_restriction"],
                "note": rules["note"],
            })
        return jsonify({"firms": out})

    @app.route("/api/accounts/add", methods=["POST"])
    def api_accounts_add():
        try:
            data = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"status": "error", "reason": "Invalid JSON body"}), 400

        acc_type = data.get("type", "demo")          # prop_firm | real | demo | binance
        broker   = data.get("broker", "mt5")          # mt5 | binance | zerodha
        prop_firm_key = data.get("prop_firm")          # e.g. "ftmo", only if acc_type == prop_firm
        profile  = data.get("profile")
        nickname = data.get("nickname") or profile

        if not profile:
            return jsonify({"status": "error", "reason": "Profile name is required"})

        if acc_type == "prop_firm" and prop_firm_key not in FIRM_RULES:
            return jsonify({"status": "error",
                             "reason": f"Unknown prop firm '{prop_firm_key}'"})

        # ── Broker-specific credential shape (see account_connector.py) ────
        if broker == "mt5":
            creds = {
                "login": data.get("login"),
                "password": data.get("password"),
                "server": data.get("server"),
            }
        elif broker == "binance":
            creds = {
                "api_key": data.get("api_key"),
                "api_secret": data.get("api_secret"),
            }
        elif broker == "zerodha":
            creds = {
                "api_key": data.get("api_key"),
                "api_secret": data.get("api_secret"),
            }
        else:
            return jsonify({"status": "error", "reason": f"Unknown broker '{broker}'"})

        # ── ACTUALLY VERIFY — this is the step the old code skipped ────────
        result = verify_and_connect(acc_type, broker, creds)

        if result["requires_oauth"]:
            # Zerodha: can't finish here. Hand the frontend the login_url and
            # a pending token; DB row is NOT created yet.
            return jsonify({
                "status": "pending_oauth",
                "login_url": result["login_url"],
                "reason": result["reason"],
            })

        if not result["ok"]:
            # NEVER write to DB on failure. NEVER return status:success.
            return jsonify({"status": "error", "reason": result["reason"]})

        # ── Verified. Now it's safe to persist. ─────────────────────────────
        db = get_db_agent()
        if not db:
            return jsonify({"status": "error", "reason": "Database not available"})

        login_identifier = str(creds.get("login") or creds.get("api_key"))
        existing = db.query_one("SELECT id FROM accounts WHERE login=?", (login_identifier,))
        if existing:
            return jsonify({"status": "error", "reason": "Account already exists"})

        enc_password = encrypt_secret(creds.get("password") or creds.get("api_secret") or "")

        # ── Write .env.<profile> so `run_bot.py --profile <name>` /
        # `quant_server.py --profile <name>` can start this account as its
        # own process. Value written is the ENCRYPTED ciphertext — never
        # plaintext — matching what mt5_agent.py now expects to decrypt on
        # read. This mirrors the old handler's file-rewrite, minus the
        # plaintext leak.
        if broker == "mt5":
            _write_profile_env(profile, {
                "MT5_LOGIN": creds.get("login"),
                "MT5_PASSWORD": enc_password,
                "MT5_SERVER": result["server"],
                "SERVER_PORT": data.get("port"),
                "TELEGRAM_BOT_TOKEN": data.get("telegram"),
                "MT5_TERMINAL_PATH": data.get("path"),
            })
        elif broker == "binance":
            _write_profile_env(profile, {
                "BROKER_TYPE": "BINANCE",
                "BINANCE_API_KEY": creds.get("api_key"),
                "BINANCE_API_SECRET": enc_password,
                "SERVER_PORT": data.get("port"),
                "TELEGRAM_BOT_TOKEN": data.get("telegram"),
            })
        # Zerodha's token is written by api_zerodha_complete() below, since
        # verification only finishes after the OAuth redirect.

        success = db.add_account(
            acc_type, login_identifier, enc_password,
            result["server"] or broker, result["balance"], "connected", '["all"]'
        )
        if not success:
            return jsonify({"status": "error", "reason": "Verified but failed to save to DB"})

        # Record broker + profile now that the row exists (add_account's
        # signature predates these columns — see migrate_accounts_supervisor.py).
        row = db.query_one("SELECT id FROM accounts WHERE login=?", (login_identifier,))
        account_id = row["id"] if row else None
        if account_id:
            db.execute("UPDATE accounts SET broker=?, profile=? WHERE id=?", (broker, profile, account_id))

        # ── Launch (Phase 1 supervisor) — MT5 only. Zerodha/Binance don't
        # need process isolation (Phase 2 multi-tenant manager handles those
        # in-process). A verified MT5 account that never gets launched would
        # just sit in the DB looking "connected" without ever actually
        # trading — this is the step that closes that gap.
        launch_result = None
        if account_id and broker == "mt5":
            from core.process_supervisor import get_supervisor
            launch_result = get_supervisor(db).launch(account_id)
            if not launch_result["ok"]:
                log.error(f"[Accounts] Verified but failed to launch account {account_id}: {launch_result['reason']}")

        return jsonify({
            "status": "success",
            "account": {
                "id": account_id,
                "login": login_identifier,
                "server": result["server"],
                "balance": result["balance"],      # REAL balance, not user input
                "equity": result["equity"],
                "currency": result["currency"],
                "type": acc_type,
                "broker": broker,
                "prop_firm": prop_firm_key,
                "rules": FIRM_RULES.get(prop_firm_key) if prop_firm_key else None,
                "process": launch_result,   # None for zerodha/binance; {"ok","reason","port"} for mt5
            },
        })

    @app.route("/api/accounts/zerodha/complete", methods=["POST"])
    def api_zerodha_complete():
        """Second half of Zerodha onboarding — call after the OAuth redirect."""
        data = request.get_json(force=True) or {}
        api_key = data.get("api_key")
        api_secret = data.get("api_secret")
        request_token = data.get("request_token")
        profile = data.get("profile")

        if not (api_key and api_secret and request_token and profile):
            return jsonify({"status": "error", "reason": "Missing api_key/api_secret/request_token/profile"})

        result = complete_zerodha_login(api_key, api_secret, request_token)
        if not result["ok"]:
            return jsonify({"status": "error", "reason": result["reason"]})

        db = get_db_agent()
        if not db:
            return jsonify({"status": "error", "reason": "Database not available"})

        enc_token = encrypt_secret(result["access_token"])

        _write_profile_env(profile, {
            "ZERODHA_API_KEY": api_key,
            "ZERODHA_API_SECRET": encrypt_secret(api_secret),
            "ZERODHA_ACCESS_TOKEN": enc_token,
            "SERVER_PORT": data.get("port"),
            "TELEGRAM_BOT_TOKEN": data.get("telegram"),
        })

        success = db.add_account(
            "real", api_key, enc_token, "Zerodha/Kite",
            result["balance"], "connected", '["all"]'
        )
        if not success:
            return jsonify({"status": "error", "reason": "Verified but failed to save to DB"})

        return jsonify({
            "status": "success",
            "account": {
                "login": api_key, "server": "Zerodha/Kite",
                "balance": result["balance"], "currency": "INR", "broker": "zerodha",
            },
        })
