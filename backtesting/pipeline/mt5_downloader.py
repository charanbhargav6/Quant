"""
MT5 history downloader (real ticks -> M1 bars with REAL spread and tick volume).

SAFETY RULES (read before editing)
  * Run ONLY in its own process (the pipeline runner is one). Never import/call from the bot or
    quant_server process: mt5.initialize()/shutdown() are process-wide, and brokers.mt5_agent.connect()
    itself calls shutdown(), which would sever a live session in the same process.
  * READ-ONLY: never calls login(), order_send(), or anything that mutates the account.
  * Timestamps from MT5 are broker SERVER time labelled as UTC. All requests are converted
    UTC->server and all results server->UTC (see ServerClock). Verify with --probe.
  * Volume: FX/CFD 'volume' is TICK COUNT, not traded volume. Result meta records volume_kind.

CLI
  python -m backtesting.pipeline.mt5_downloader --probe  --symbol XAUUSD=X
  python -m backtesting.pipeline.mt5_downloader --symbol XAUUSD=X --days 180 [--mode m1]
"""
import argparse, json, logging, os, sys, time
from datetime import date, datetime, timedelta, timezone
import numpy as np
import pandas as pd
from . import settings as S
from . import data_store as ds

log = logging.getLogger("crave.bt.mt5dl")

class MT5Unavailable(RuntimeError): ...
class Cancelled(RuntimeError): ...

# ── server clock ─────────────────────────────────────────────────────────────
def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    first = d + timedelta(days=(6 - d.weekday()) % 7)
    if n > 0:
        return first + timedelta(weeks=n - 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)

def _dst_window(year, rule):
    if rule == "us":
        return (datetime(*_nth_sunday(year, 3, 2).timetuple()[:3], 0, tzinfo=timezone.utc),
                datetime(*_nth_sunday(year, 11, 1).timetuple()[:3], 0, tzinfo=timezone.utc))
    if rule == "eu":
        return (datetime(*_nth_sunday(year, 3, -1).timetuple()[:3], 1, tzinfo=timezone.utc),
                datetime(*_nth_sunday(year, 10, -1).timetuple()[:3], 1, tzinfo=timezone.utc))
    return None

class ServerClock:
    """UTC <-> broker-server-time conversion (offset hours = winter + 1h while DST rule active)."""
    def __init__(self, winter=None, rule=None):
        self.winter = S.SERVER_UTC_OFFSET_WINTER if winter is None else winter
        self.rule = rule or S.SERVER_DST_RULE

    def _dst_utc(self, utc_ts: pd.Series) -> np.ndarray:
        if self.rule not in ("us", "eu"):
            return np.zeros(len(utc_ts), dtype=bool)
        out = np.zeros(len(utc_ts), dtype=bool)
        for y in pd.unique(utc_ts.dt.year):
            a, b = _dst_window(int(y), self.rule)
            m = (utc_ts >= pd.Timestamp(a)) & (utc_ts < pd.Timestamp(b))
            out |= m.to_numpy()
        return out

    def offset_hours(self, utc_ts: pd.Series) -> np.ndarray:
        return self.winter + self._dst_utc(utc_ts).astype(float)

    def utc_to_server_dt(self, utc_dt: datetime) -> datetime:
        off = float(self.offset_hours(pd.Series([pd.Timestamp(utc_dt)]))[0])
        return utc_dt + timedelta(hours=off)

    def server_epoch_ms_to_utc(self, ms: np.ndarray) -> pd.Series:
        approx = pd.to_datetime(ms - int(self.winter * 3_600_000), unit="ms", utc=True)
        approx = pd.Series(approx)
        dst = self._dst_utc(approx)
        return pd.Series(approx - pd.to_timedelta(dst.astype(float), unit="h"))

# ── MT5 access ───────────────────────────────────────────────────────────────
def _import_mt5():
    try:
        import MetaTrader5 as mt5
        return mt5
    except Exception as e:
        raise MT5Unavailable(f"MetaTrader5 python package unavailable ({e}). Windows only: pip install MetaTrader5")

TERMINAL_CANDIDATES = [r"C:\Program Files\XM Global MT5\terminal64.exe",
                       r"C:\Program Files\MetaTrader 5\terminal64.exe",
                       r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"]

class MT5History:
    def __init__(self, mt5=None, clock: ServerClock = None):
        self.mt5 = mt5
        self.clock = clock or ServerClock()
        self.connected = False

    def connect(self):
        if self.mt5 is None:
            self.mt5 = _import_mt5()
        path = os.environ.get("MT5_TERMINAL_PATH") or next((p for p in TERMINAL_CANDIDATES if os.path.exists(p)), None)
        ok = self.mt5.initialize(path=path) if path else self.mt5.initialize()   # attach only, NO login
        if not ok:
            raise MT5Unavailable(f"mt5.initialize() failed: {self.mt5.last_error()}")
        self.connected = True

    def close(self):
        if self.connected:
            try: self.mt5.shutdown()
            finally: self.connected = False

    # symbol resolution -------------------------------------------------------
    def resolve_symbol(self, crave_symbol: str) -> str:
        overrides = json.loads(S.SYMBOL_OVERRIDES_JSON) if S.SYMBOL_OVERRIDES_JSON else {}
        cands = []
        if crave_symbol in overrides: cands.append(overrides[crave_symbol])
        try:
            from brokers.mt5_agent import SYMBOL_MAP
            if crave_symbol in SYMBOL_MAP: cands.append(SYMBOL_MAP[crave_symbol])
        except Exception:
            pass
        base = crave_symbol.replace("=X", "").replace("-USD", "USD").replace("USDT", "USD")
        cands += [base, crave_symbol]
        for c in cands:
            if self.mt5.symbol_info(c) is not None:
                self.mt5.symbol_select(c, True)
                return c
        syms = self.mt5.symbols_get() or []                       # discovery fallback
        names = [s.name for s in syms if s.name.upper().startswith(base.upper())]
        if names:
            pick = sorted(names, key=len)[0]
            self.mt5.symbol_select(pick, True)
            return pick
        raise MT5Unavailable(f"No MT5 symbol found for {crave_symbol} (tried {cands}). Set BT_SYMBOL_OVERRIDES.")

    def symbol_meta(self, sym: str) -> dict:
        i = self.mt5.symbol_info(sym)
        return {"mt5_symbol": sym, "digits": i.digits, "point": i.point,
                "contract_size": getattr(i, "trade_contract_size", None),
                "volume_min": getattr(i, "volume_min", None)}

    # one UTC day of M1 --------------------------------------------------------
    def _ticks_window(self, sym, utc_from, utc_to):
        sf = self.clock.utc_to_server_dt(utc_from); st = self.clock.utc_to_server_dt(utc_to)
        for attempt in range(S.DOWNLOAD_RETRIES):
            arr = self.mt5.copy_ticks_range(sym, sf, st, self.mt5.COPY_TICKS_ALL)
            if arr is not None:
                return arr
            time.sleep(0.5 * (attempt + 1))
        return None

    def fetch_day_ticks(self, sym, day: date, point: float):
        d0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        parts, step = [], S.TICK_CHUNK_HOURS
        for h in range(0, 24, step):
            arr = self._ticks_window(sym, d0 + timedelta(hours=h), d0 + timedelta(hours=h + step))
            if arr is None:
                return None, 0
            if len(arr): parts.append(pd.DataFrame(arr))
        if not parts:
            return pd.DataFrame(columns=ds.M1_COLS), 0
        t = pd.concat(parts, ignore_index=True).drop_duplicates(["time_msc", "bid", "ask", "last"])
        n_ticks = len(t)
        utc = self.clock.server_epoch_ms_to_utc(t["time_msc"].to_numpy(dtype="int64"))
        bid, ask, last = t["bid"].to_numpy(float), t["ask"].to_numpy(float), t["last"].to_numpy(float)
        price = np.where(bid > 0, bid, last)
        sp = np.where((bid > 0) & (ask >= bid), ask - bid, np.nan)
        vr = t["volume_real"].to_numpy(float) if "volume_real" in t else np.zeros(len(t))
        x = pd.DataFrame({"t": utc.dt.floor("min").to_numpy(), "p": price, "sp": sp, "vr": vr})
        x = x[(x["p"] > 0) & (x["t"] >= pd.Timestamp(d0)) & (x["t"] < pd.Timestamp(d0 + timedelta(days=1)))]
        if x.empty:
            return pd.DataFrame(columns=ds.M1_COLS), n_ticks
        g = x.groupby("t")
        m1 = pd.DataFrame({"open": g["p"].first(), "high": g["p"].max(), "low": g["p"].min(),
                           "close": g["p"].last(), "tick_count": g.size(),
                           "spread_mean": g["sp"].mean(), "spread_max": g["sp"].max(),
                           "real_volume": g["vr"].sum()}).reset_index().rename(columns={"t": "time"})
        m1["time"] = pd.to_datetime(m1["time"], utc=True)
        m1["source"] = "ticks"
        return m1[ds.M1_COLS], n_ticks

    def fetch_day_m1(self, sym, day: date, point: float):
        d0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        sf, st = self.clock.utc_to_server_dt(d0), self.clock.utc_to_server_dt(d0 + timedelta(days=1))
        arr = None
        for attempt in range(S.DOWNLOAD_RETRIES):
            arr = self.mt5.copy_rates_range(sym, self.mt5.TIMEFRAME_M1, sf, st)
            if arr is not None: break
            time.sleep(0.5 * (attempt + 1))
        if arr is None or len(arr) == 0:
            return pd.DataFrame(columns=ds.M1_COLS)
        r = pd.DataFrame(arr)
        utc = self.clock.server_epoch_ms_to_utc(r["time"].to_numpy(dtype="int64") * 1000)
        sp = r["spread"].to_numpy(float) * point
        m1 = pd.DataFrame({"time": utc, "open": r["open"], "high": r["high"], "low": r["low"],
                           "close": r["close"], "tick_count": r["tick_volume"], "spread_mean": sp,
                           "spread_max": sp, "real_volume": r.get("real_volume", 0), "source": "m1_rates"})
        m1["time"] = pd.to_datetime(m1["time"], utc=True)
        m1 = m1[(m1["time"] >= pd.Timestamp(d0)) & (m1["time"] < pd.Timestamp(d0 + timedelta(days=1)))]
        return m1[ds.M1_COLS]

# ── orchestration ────────────────────────────────────────────────────────────
def days_needed(sym, start: date, end: date, cache_dir=None, mode="ticks"):
    meta = ds.load_meta(sym, cache_dir)
    today = datetime.now(timezone.utc).date()
    need = []
    cur = start
    while cur <= end:
        rec = meta["days"].get(cur.isoformat())
        weekend_fx = (cur.weekday() >= 5) and not ds.is_crypto(sym)
        # 'empty' weekdays (holiday / broker has no history) are not retried once older than 3 days
        done = bool(rec) and cur < today and (rec.get("status") in ("ok", "closed") or
                                              (rec.get("status") == "empty" and cur < today - timedelta(days=3)))
        if rec and mode == "ticks" and rec.get("source") == "m1_rates" and rec.get("status") == "ok":
            done = False   # upgrade previously-fallback days when ticks are requested
        if not done and not weekend_fx:
            need.append(cur)
        cur += timedelta(days=1)
    return need

def download(crave_symbol, days, mode="ticks", progress=None, cancel=None, mt5=None, cache_dir=None, end=None):
    """Download/refresh the cache for the last `days` days. Returns a summary dict."""
    progress = progress or (lambda **k: None)
    cancel = cancel or (lambda: False)
    end = end or datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    need = days_needed(crave_symbol, start, end, cache_dir, mode)
    summary = {"symbol": crave_symbol, "requested_days": days, "to_download": len(need),
               "downloaded": 0, "from_ticks": 0, "from_m1": 0, "empty": 0, "failed": 0,
               "ticks_total": 0, "mt5_used": False}
    if not need:
        progress(done=0, total=0, message="cache already complete")
        return summary
    h = MT5History(mt5=mt5)
    h.connect()
    try:
        sym = h.resolve_symbol(crave_symbol)
        meta = ds.load_meta(crave_symbol, cache_dir)
        sm = h.symbol_meta(sym)
        meta.update(sm); meta.update({"server_utc_offset_winter": h.clock.winter, "server_dst_rule": h.clock.rule,
                                      "tz_validated": False})
        summary["mt5_used"] = True
        for k, d in enumerate(need):
            if cancel(): raise Cancelled()
            m1, n_ticks, src = None, 0, "none"
            if mode == "ticks":
                m1, n_ticks = h.fetch_day_ticks(sym, d, sm["point"])
                src = "ticks"
            if m1 is None or (len(m1) == 0 and mode == "ticks" and n_ticks == 0):
                m1b = h.fetch_day_m1(sym, d, sm["point"])
                if len(m1b): m1, src = m1b, "m1_rates"
            if m1 is None or len(m1) == 0:
                st = "closed" if (d.weekday() >= 5 and not ds.is_crypto(crave_symbol)) else "empty"
                meta["days"][d.isoformat()] = {"status": st, "source": "none", "bars": 0}
                summary["empty"] += 1
            else:
                ds.write_day(crave_symbol, d, m1, cache_dir)
                meta["days"][d.isoformat()] = {"status": "ok", "source": src, "bars": int(len(m1)), "ticks": int(n_ticks)}
                summary["downloaded"] += 1
                summary["from_ticks" if src == "ticks" else "from_m1"] += 1
                summary["ticks_total"] += int(n_ticks)
            if k % 5 == 0 or k == len(need) - 1:
                ds.save_meta(crave_symbol, meta, cache_dir)
            progress(done=k + 1, total=len(need), day=d.isoformat(), source=src, ticks=int(n_ticks),
                     message=f"{d.isoformat()} · {src} · {n_ticks:,} ticks")
        # volume kind + timezone sanity
        m1all = ds.read_days(crave_symbol, start, end, cache_dir)
        meta["volume_kind"] = "real" if len(m1all) and m1all["real_volume"].sum() > 0 else "tick_count"
        ds.save_meta(crave_symbol, meta, cache_dir)
        return summary
    finally:
        h.close()

def probe(crave_symbol, mt5=None):
    """Quick diagnostics: symbol resolution, server-time offset check, how far back ticks go."""
    h = MT5History(mt5=mt5); h.connect()
    try:
        sym = h.resolve_symbol(crave_symbol); meta = h.symbol_meta(sym)
        tick = h.mt5.symbol_info_tick(sym)
        out = {"symbol": crave_symbol, "mt5_symbol": sym, **meta}
        if tick:
            diff = float(tick.time) - time.time()      # server-epoch minus real epoch == offset (market open only)
            det = round(diff / 1800) / 2 if abs(diff) < 6 * 3600 else None
            exp = float(h.clock.offset_hours(pd.Series([pd.Timestamp.now(tz="UTC")]))[0])
            out.update({"detected_offset_hours": det, "configured_offset_hours_now": exp,
                        "offset_ok": (det is not None and abs(det - exp) < 0.01),
                        "note": "detection is only valid while the market is open (fresh tick)"})
        horizon = {}
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for back in (7, 30, 90, 180, 365, 730):
            t0 = now - timedelta(days=back)
            while t0.weekday() >= 5: t0 -= timedelta(days=1)
            arr = h._ticks_window(sym, t0, t0 + timedelta(hours=1))
            horizon[f"{back}d"] = 0 if arr is None else int(len(arr))
        out["ticks_in_1h_window"] = horizon
        return out
    finally:
        h.close()

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD=X"); ap.add_argument("--days", type=int, default=S.DEFAULT_DAYS)
    ap.add_argument("--mode", choices=["ticks", "m1"], default="ticks"); ap.add_argument("--probe", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        if a.probe:
            print(json.dumps(probe(a.symbol), indent=2, default=str)); return 0
        res = download(a.symbol, a.days, a.mode,
                       progress=lambda **k: print(f"[{k.get('done')}/{k.get('total')}] {k.get('message','')}", flush=True))
        print(json.dumps(res, indent=2)); return 0
    except MT5Unavailable as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2

if __name__ == "__main__":
    sys.exit(main())
