import sys
import time
import subprocess
import webbrowser
import atexit

def cleanup(server_process, bot_process):
    print("\nShutting down engine...")
    server_process.terminate()
    bot_process.terminate()

def run():
    print("="*60)
    print("  CRAVE QUANT TRADING ENGINE (v12.2 Live Mode)  ")
    print("="*60)
    
    # 1. Start quant_server.py (UI backend)
    print("\n[1/3] Starting Web Dashboard Server (Port 8765)...")
    server_process = subprocess.Popen([sys.executable, "quant_server.py"])
    
    # 2. Start run_bot.py (Trading Engine)
    print("[2/3] Starting Trading Bot Engine (Live)...")
    bot_process = subprocess.Popen([sys.executable, "run_bot.py", "--live"])
    
    atexit.register(cleanup, server_process, bot_process)

    # 3. Wait for initialization
    time.sleep(4)
    
    # 4. Open Browser
    url = "http://127.0.0.1:8765/?v=2"
    print(f"\n[3/3] Opening dashboard at {url}...")
    webbrowser.open(url)
    
    print("\n" + "-"*60)
    print("CRAVE Trading Engine is RUNNING.")
    print("Press Ctrl+C to stop all services.")
    print("-"*60 + "\n")
    
    try:
        server_process.wait()
        bot_process.wait()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    run()
