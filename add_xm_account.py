"""Safely add and verify an XM MT5 account.

This helper is deliberately read/verify-only with respect to trading: it calls
MT5 account initialization and account-info methods, but never sends an order.
The account is persisted only after login and server identity match.
"""

import argparse
import getpass
import sys

from dotenv import load_dotenv

from core.account_connector import verify_and_connect
from core.database_manager import DatabaseManager
from core.secrets_vault import encrypt_secret


def parse_args():
    parser = argparse.ArgumentParser(description="Verify and add an XM MT5 account")
    parser.add_argument("--login", help="XM MT5 login/account number")
    parser.add_argument("--server", help="XM server exactly as shown in MT5")
    parser.add_argument("--profile", help="Profile name; default xm_<login>")
    parser.add_argument("--strategies", default='["all"]', help="JSON strategy allocation")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    login = (args.login or input("Enter XM MT5 Login (Account Number): ")).strip()
    password = getpass.getpass("Enter XM MT5 Password: ").strip()
    server = (args.server or input("Enter XM Server exactly as it appears in MT5: ")).strip()
    if not login or not password or not server:
        print("Error: login, password, and server are required.")
        return 2
    try:
        login_int = int(login)
    except ValueError:
        print("Error: MT5 login must be numeric.")
        return 2

    result = verify_and_connect("real", "mt5", {
        "login": login_int,
        "password": password,
        "server": server,
    })
    if not result.get("ok"):
        print(f"Verification failed: {result.get('reason')}")
        return 1

    profile = args.profile or f"xm_{login_int}"
    db = DatabaseManager()
    if db.query_one("SELECT id FROM accounts WHERE login=?", (str(login_int),)):
        print(f"Account {login_int} already exists; no duplicate was inserted.")
        return 0

    try:
        import json
        strategies = json.loads(args.strategies)
        if not isinstance(strategies, list):
            raise ValueError("strategies must be a JSON list")
        strategies_json = json.dumps(strategies)
    except Exception as exc:
        print(f"Error: invalid --strategies JSON: {exc}")
        return 2

    encrypted_password = encrypt_secret(password)
    if not db.add_account(
        acc_type="real",
        login=str(login_int),
        password=encrypted_password,
        server=result["server"],
        capital=float(result["balance"]),
        status="verified",
        strategies=strategies_json,
    ):
        print("Failed to persist the verified account.")
        return 1

    row = db.query_one("SELECT id FROM accounts WHERE login=?", (str(login_int),))
    if row:
        db.execute(
            "UPDATE accounts SET broker='mt5', profile=?, process_status='stopped' WHERE id=?",
            (profile, row["id"]),
        )
    print(
        f"Verified and added MT5 account {login_int} on {result['server']} "
        f"with broker balance {result['balance']:.2f}. Profile: {profile}"
    )
    print("No order was sent. Run tools/verify_runtime.py --probe on the terminal host before paper/live startup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
