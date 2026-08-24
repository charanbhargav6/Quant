import os
import getpass
from core.database_manager import DatabaseManager
from core.secrets_vault import encrypt_secret
from dotenv import load_dotenv

def main():
    load_dotenv()
    db = DatabaseManager()
    
    print("\n--- Force Add XM MT5 Account ---")
    login = input("Enter XM MT5 Login (Account Number): ").strip()
    password = getpass.getpass("Enter XM MT5 Password: ").strip()
    server = input("Enter XM Server exactly as it appears in MT5 (e.g. XMGlobal-MT5 4): ").strip()
    
    if not login or not password or not server:
        print("Error: All fields are required.")
        return
        
    enc_password = encrypt_secret(password)
    
    # 1. Add the new account
    success = db.add_account(
        acc_type="real",
        login=login,
        password=enc_password,
        server=server,
        balance=10000.0, # Placeholder, will auto-update on connect
        status="connected",
        strategies_enabled='["all"]'
    )
    
    if success:
        print(f"✅ Successfully added MT5 account {login} to the database.")
        # Set broker to mt5
        row = db.query_one("SELECT id FROM accounts WHERE login=?", (login,))
        if row:
            db.execute("UPDATE accounts SET broker='mt5', profile='xm' WHERE id=?", (row["id"],))
            
        # 2. Delete the binance placeholder
        db.execute("DELETE FROM accounts WHERE broker='binance' OR login='your_binance_key'")
        print("✅ Removed Binance placeholder.")
        
        print("\n🚀 DONE! Please restart the Crave Quant Engine (Ctrl+C and run python crave_master.py again).")
    else:
        print("❌ Failed to add account to database. It might already exist.")

if __name__ == "__main__":
    main()
