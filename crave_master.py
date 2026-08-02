import os
import sys
import time
import signal
import subprocess
from pathlib import Path

ENGINE_ROOT = Path(__file__).parent

def main():
    print("==========================================================")
    print(" CRAVE QUANT - MASTER LAUNCHER ")
    print("==========================================================")
    
    # 1. Discover all profiles by looking for .env.<profile> files
    profiles = []
    for p in ENGINE_ROOT.glob(".env.*"):
        if p.name == ".env.example":
            continue
        profile_name = p.name.split(".", 2)[-1]
        profiles.append(profile_name)

    if not profiles:
        # Fallback to default
        profiles = [""]
        print("[!] No .env.<profile> files found. Starting single default instance.")
    else:
        print(f"[+] Found {len(profiles)} accounts: {', '.join(profiles)}")

    processes = []
    
    def cleanup(signum, frame):
        print("\n[!] Shutting down all Crave Quant processes...")
        for proc in processes:
            try:
                proc.terminate()
            except:
                pass
        sys.exit(0)
        
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("\n[+] Starting engines and web dashboards...")
    for prof in profiles:
        print(f"  -> Starting Profile: {prof if prof else 'DEFAULT'}")
        
        # Determine args
        prof_args = ["--profile", prof] if prof else []
        
        # 1. Start the bot engine (stdout prints to this console)
        bot_proc = subprocess.Popen(
            [sys.executable, "run_bot.py"] + prof_args,
            cwd=str(ENGINE_ROOT)
        )
        processes.append(bot_proc)
        
        # 2. Start the web server (stdout silenced to prevent messy overlapping flask logs, errors printed)
        srv_proc = subprocess.Popen(
            [sys.executable, "quant_server.py"] + prof_args,
            cwd=str(ENGINE_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT
        )
        processes.append(srv_proc)

    print("\n==========================================================")
    print(" ALL SYSTEMS OPERATIONAL ")
    print(" Open your browser to: http://127.0.0.1:8765")
    print(" Press Ctrl+C to safely shut down all accounts.")
    print("==========================================================\n")

    # Automatically open the dashboard
    import webbrowser
    time.sleep(3) # Wait for Flask to boot
    webbrowser.open("http://127.0.0.1:8765/?v=2")

    try:
        # Keep master process alive and wait for children
        for p in processes:
            p.wait()
    except KeyboardInterrupt:
        cleanup(None, None)

if __name__ == "__main__":
    main()
