"""
CRAVE v10.3 — NSE Bhavcopy & India Data Layer (Session 8)
===========================================================
Provides accurate Indian market historical data from official NSE sources.

WHY NOT JUST USE YFINANCE FOR INDIA:
  yfinance.NS prices are adjusted for splits/dividends and have occasional
  gaps and incorrect timestamps. For backtesting Indian stocks you want
  NSE's official end-of-day bhavcopy data which is:
    - Exact closing auction prices (not last traded price)
    - No split-adjustment errors
    - Official settlement data used for F&O expiry calculations
    - Free to download from NSE directly

BHAVCOPY DOWNLOAD:
  NSE publishes daily bhavcopy at:
    https://archives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv
  File size: ~2MB/day. Last 3 years = ~1.5GB total.
  We download only what we need and store in database.

ADDITIONAL INDIA DATA:
  FII/DII: Daily institutional flow (FII net buy/sell in crores)
  Max Pain: Strike where option sellers lose least (strong price magnet)
  OI Data: Open interest by strike (shows where smart money is positioned)

USAGE:
  from data.nse_bhavcopy import get_bhavcopy

  bc = get_bhavcopy()
  df = bc.get_stock_history("RELIANCE", days=60)
  mp = bc.calculate_max_pain("NIFTY", expiry="2024-03-28")
  fii = bc.get_fii_history(days=30)
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd

logger = logging.getLogger("crave.nse_bhavcopy")

# Local storage for downloaded bhavcopy files
BHAVCOPY_DIR = Path(__file__).parent.parent.parent.parent / "Database" / "bhavcopy"


class NSEBhavcopy:

    BASE_URL = "https://archives.nseindia.com/products/content"
    FII_URL  = "https://www.nseindia.com/api/fiidiiTradeReact"

    def __init__(self):
        BHAVCOPY_DIR.mkdir(parents=True, exist_ok=True)
        self._session = None
        self._session_refreshed = None

    def _get_session(self):
        """
        NSE requires a session cookie to download data.
        First hit the main page to get the cookie, then download.
        Session refreshed every 30 minutes.
        """
        import requests
        now = datetime.now(timezone.utc)

        if (self._session is not None and
                self._session_refreshed is not None and
                (now - self._session_refreshed).seconds < 1800):
            return self._session

        session = requests.Session()
        session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            # Get session cookie from NSE homepage
            session.get("https://www.nseindia.com", timeout=10)
            time.sleep(1)
            self._session          = session
            self._session_refreshed = now
        except Exception as e:
            logger.debug(f"[Bhavcopy] Session init warning: {e}")
            self._session = session

        return session

    # ─────────────────────────────────────────────────────────────────────────
    # BHAVCOPY DOWNLOAD
    # ─────────────────────────────────────────────────────────────────────────

    def download_bhavcopy(self, date: datetime) -> Optional[pd.DataFrame]:
        """
        Download NSE equity bhavcopy for a specific date.
        Returns DataFrame with OHLCV for all NSE-listed equities.
        Caches locally to avoid re-downloading.
        """
        date_str   = date.strftime("%d%m%Y")
        cache_file = BHAVCOPY_DIR / f"bhav_{date_str}.csv"

        # Return cached version if exists
        if cache_file.exists():
            try:
                return pd.read_csv(cache_file)
            except Exception:
                cache_file.unlink(missing_ok=True)

        # Skip weekends
        if date.weekday() >= 5:
            return None

        url = (f"{self.BASE_URL}/sec_bhavdata_full_{date_str}.csv")

        try:
            session = self._get_session()
            resp    = session.get(url, timeout=15)

            if resp.status_code == 200:
                lines = resp.text.strip()
                if len(lines) < 100:
                    return None  # Empty file

                # Save locally
                cache_file.write_text(lines)

                df = pd.read_csv(cache_file)
                logger.debug(
                    f"[Bhavcopy] Downloaded {date_str}: {len(df)} instruments"
                )
                return df
            else:
                logger.debug(
                    f"[Bhavcopy] {date_str}: HTTP {resp.status_code}"
                )
                return None

        except Exception as e:
            logger.debug(f"[Bhavcopy] Download failed {date_str}: {e}")
            return None

    def get_stock_history(self, symbol: str,
                           days: int = 60) -> Optional[pd.DataFrame]:
        """
        Build OHLCV history for an NSE stock from bhavcopy files.
        More accurate than yfinance for backtesting Indian stocks.

        Returns DataFrame with columns: date, open, high, low, close, volume
        """
        records = []
        end     = datetime.now(timezone.utc)

        for i in range(days + 10):   # +10 for weekends/holidays
            date = end - timedelta(days=i)
            if date.weekday() >= 5:
                continue

            df_day = self.download_bhavcopy(date)
            if df_day is None:
                continue

            # Find this symbol
            # Bhavcopy uses SYMBOL column
            sym_col = None
            for col in df_day.columns:
                if col.strip().upper() in ("SYMBOL", "SCRIP_CD", "SC_CODE"):
                    sym_col = col
                    break

            if sym_col is None:
                continue

            df_sym = df_day[
                df_day[sym_col].astype(str).str.strip().str.upper() == symbol.upper()
            ]
            if df_sym.empty:
                continue

            row = df_sym.iloc[0]

            # Map columns (NSE bhavcopy column names vary by series)
            try:
                records.append({
                    "date":   date.strftime("%Y-%m-%d"),
                    "open":   float(str(row.get("OPEN_PRICE",
                                               row.get("OPEN", 0))).replace(",","")),
                    "high":   float(str(row.get("HIGH_PRICE",
                                               row.get("HIGH", 0))).replace(",","")),
                    "low":    float(str(row.get("LOW_PRICE",
                                               row.get("LOW", 0))).replace(",","")),
                    "close":  float(str(row.get("CLOSE_PRICE",
                                               row.get("CLOSE", 0))).replace(",","")),
                    "volume": float(str(row.get("TTL_TRD_QNTY",
                                               row.get("TOT_QTY", 0))).replace(",","")),
                })
            except Exception:
                continue

        if not records:
            logger.info(
                f"[Bhavcopy] No bhavcopy data for {symbol}. "
                f"Falling back to yfinance."
            )
            return self._yfinance_fallback(f"{symbol}.NS", days)

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        df.rename(columns={"date": "time"}, inplace=True)
        return df.tail(days)

    def _yfinance_fallback(self, symbol: str,
                            days: int) -> Optional[pd.DataFrame]:
        """yfinance as fallback when bhavcopy unavailable."""
        try:
            import yfinance as yf
            end   = datetime.now()
            start = end - timedelta(days=days + 10)
            df    = yf.download(symbol, start=start, end=end,
                                 interval="1d", progress=False)
            if df is None or df.empty:
                return None
            df = df.reset_index()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            col_map = {}
            for c in df.columns:
                cl = c.lower()
                if "date" in cl: col_map[c] = "time"
                elif cl == "open":   col_map[c] = "open"
                elif cl == "high":   col_map[c] = "high"
                elif cl == "low":    col_map[c] = "low"
                elif cl == "close":  col_map[c] = "close"
                elif cl == "volume": col_map[c] = "volume"
            df = df.rename(columns=col_map)
            df["time"] = pd.to_datetime(df["time"])
            df = df[["time","open","high","low","close","volume"]].dropna()
            return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.debug(f"[Bhavcopy] yfinance fallback failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # FII/DII HISTORICAL DATA
    # ─────────────────────────────────────────────────────────────────────────

    def get_fii_history(self, days: int = 30) -> pd.DataFrame:
        """
        Get FII/DII net buy/sell history for last N days.
        Published by NSE daily. Excellent leading indicator for Nifty.

        Returns DataFrame: date | fii_net | dii_net | combined | bias
        """
        try:
            import requests
            session = self._get_session()
            resp    = session.get(self.FII_URL, timeout=10)

            if resp.status_code != 200:
                return pd.DataFrame()

            data = resp.json()
            if not data:
                return pd.DataFrame()

            records = []
            for row in data[:days]:
                try:
                    fii_net = float(str(row.get("fiiNetDii", 0)).replace(",",""))
                    dii_net = float(str(row.get("diiNetDii", 0)).replace(",",""))
                    combined = fii_net + dii_net
                    bias = (
                        "BULLISH" if fii_net > 500  else
                        "BEARISH" if fii_net < -500 else
                        "NEUTRAL"
                    )
                    records.append({
                        "date":     row.get("date", ""),
                        "fii_net":  round(fii_net,  2),
                        "dii_net":  round(dii_net,  2),
                        "combined": round(combined, 2),
                        "bias":     bias,
                    })
                except Exception:
                    continue

            if not records:
                return pd.DataFrame()

            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)

        except Exception as e:
            logger.debug(f"[Bhavcopy] FII history failed: {e}")
            return pd.DataFrame()

    # ─────────────────────────────────────────────────────────────────────────
    # MAX PAIN CALCULATOR
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_max_pain(self, symbol: str = "NIFTY",
                            expiry: Optional[str] = None) -> Optional[dict]:
        """
        Max Pain = strike price where total option seller loss is minimised.
        Price has strong tendency to gravitate toward max pain at expiry.

        Algorithm:
          For each strike K:
            loss_to_sellers = sum(max(0, K - K_i) × put_OI_i  for K_i < K)
                            + sum(max(0, K_i - K) × call_OI_i for K_i > K)
          Max Pain = strike K that minimises total loss

        Returns:
          max_pain_strike: the strike price
          current_spot:    current Nifty level
          distance_pct:    % gap between spot and max pain
          interpretation:  "spot above max pain → bearish pull" etc.
        """
        try:
            import requests
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            session = self._get_session()
            resp    = session.get(url, timeout=10)

            if resp.status_code != 200:
                return None

            records_data = resp.json().get("records", {})
            spot         = records_data.get("underlyingValue", 0)
            chain        = records_data.get("data", [])

            if not chain or not spot:
                return None

            # Build OI table per strike
            strikes = {}
            for row in chain:
                k       = row.get("strikePrice", 0)
                ce_oi   = row.get("CE", {}).get("openInterest", 0) or 0
                pe_oi   = row.get("PE", {}).get("openInterest", 0) or 0
                strikes[k] = {"call_oi": ce_oi, "put_oi": pe_oi}

            if not strikes:
                return None

            sorted_strikes = sorted(strikes.keys())

            # Calculate pain at each strike
            min_pain   = float("inf")
            max_pain_k = sorted_strikes[0]

            for k in sorted_strikes:
                call_pain = sum(
                    max(0, ki - k) * strikes[ki]["call_oi"]
                    for ki in sorted_strikes
                )
                put_pain = sum(
                    max(0, k - ki) * strikes[ki]["put_oi"]
                    for ki in sorted_strikes
                )
                total_pain = call_pain + put_pain
                if total_pain < min_pain:
                    min_pain   = total_pain
                    max_pain_k = k

            distance_pct = round((spot - max_pain_k) / max_pain_k * 100, 2)

            if abs(distance_pct) < 0.5:
                interp = "Spot near max pain — sideways likely"
            elif spot > max_pain_k:
                interp = f"Spot {distance_pct:+.1f}% above max pain — bearish pull toward {max_pain_k}"
            else:
                interp = f"Spot {distance_pct:+.1f}% below max pain — bullish pull toward {max_pain_k}"

            return {
                "symbol":          symbol,
                "max_pain_strike": max_pain_k,
                "current_spot":    spot,
                "distance_pct":    distance_pct,
                "interpretation":  interp,
                "strikes_analyzed": len(sorted_strikes),
            }

        except Exception as e:
            logger.debug(f"[Bhavcopy] Max pain failed {symbol}: {e}")
            return None


# ── Singleton ─────────────────────────────────────────────────────────────────
_bhavcopy: Optional[NSEBhavcopy] = None

def get_bhavcopy() -> NSEBhavcopy:
    global _bhavcopy
    if _bhavcopy is None:
        _bhavcopy = NSEBhavcopy()
    return _bhavcopy
