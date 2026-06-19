"""
CRAVE v10.4 — Windows Sleep Watcher
===================================
Listens for the Windows 'Sleep' event (PBT_APMSUSPEND).
When the user closes their laptop lid, this script intercepts
the sleep signal, instantly fires an API call to start the AWS node,
updates the state file, and pushes to Supabase/GitHub before
the laptop finishes going to sleep!
"""

import logging
import threading
import win32api
import win32con
import win32gui
from infra.node_orchestrator import orchestrator
from infra.aws_manager import aws

logger = logging.getLogger("crave.sleep_watcher")

def _on_sleep():
    """Triggered right before laptop goes to sleep."""
    logger.warning("🚨 [SleepWatcher] Windows Sleep Event Detected! Laptop lid closed.")
    
    # Fire AWS start API call immediately
    logger.info("[SleepWatcher] Firing AWS start command...")
    aws.start_instance(wait=False)
    
    # Tell orchestrator to immediately hand over power
    orchestrator.trigger_failover("laptop", "Laptop Lid Closed / Sleep Mode")
    
    # Try one last emergency push to sync state before Wi-Fi cuts
    try:
        from infra.state_sync import sync
        sync.push()
        from dashboard.supabase_pusher import get_pusher
        get_pusher()._push_all()
    except Exception as e:
        logger.debug(f"[SleepWatcher] Final state sync failed: {e}")
        
    logger.info("✅ [SleepWatcher] Handoff complete. Laptop is now sleeping.")

def wndproc(hwnd, msg, wparam, lparam):
    if msg == win32con.WM_POWERBROADCAST:
        if wparam == win32con.PBT_APMSUSPEND:
            # Run in separate thread so we don't block Windows Message Pump
            t = threading.Thread(target=_on_sleep)
            t.start()
            t.join(timeout=3.0)  # Windows only gives us ~2 seconds anyway
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

def start_watcher():
    """Run the Windows message loop in a background thread."""
    def _loop():
        try:
            hinst = win32api.GetModuleHandle(None)
            wndclass = win32gui.WNDCLASS()
            wndclass.hInstance = hinst
            wndclass.lpszClassName = "CraveSleepWatcherClass"
            wndclass.lpfnWndProc = wndproc
            win32gui.RegisterClass(wndclass)
            hwnd = win32gui.CreateWindowEx(
                0, wndclass.lpszClassName, "CraveSleepWatcher",
                0, 0, 0, 0, 0, 0, 0, hinst, None
            )
            logger.info("[SleepWatcher] Listening for Windows lid close/sleep events.")
            win32gui.PumpMessages()
        except Exception as e:
            logger.warning(f"[SleepWatcher] Failed to start: {e}")

    t = threading.Thread(target=_loop, daemon=True, name="CraveSleepWatcher")
    t.start()
