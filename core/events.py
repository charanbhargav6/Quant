import threading

# Global kill switch triggered by UI or Admin
emergency_stop_event = threading.Event()
