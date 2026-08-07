from core.terminal_ui import dashboard
try:
    dashboard.update_account(10000.0, 0, 0.0)
    dashboard.update_market("BTCUSDT", 60000.0, "BUY", "TRENDING", "buy")
    dashboard.add_council_msg("SYS", "Test council message")
    dashboard.add_signal("BTCUSDT", "buy", "A", "EXECUTED")
    dashboard.render()
    print("UI render test successful!")
except Exception as e:
    print(f"UI render test failed: {e}")
