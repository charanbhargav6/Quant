import yfinance as yf
print("1h max:")
df_1h = yf.download("^NSEI", period="max", interval="1h", auto_adjust=True, progress=False)
print(len(df_1h) if df_1h is not None else 0)

print("1d max:")
df_1d = yf.download("^NSEI", period="max", interval="1d", auto_adjust=True, progress=False)
print(len(df_1d) if df_1d is not None else 0)
