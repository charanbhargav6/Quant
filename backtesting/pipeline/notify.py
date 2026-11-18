"""Telegram + AI-Council hand-off. Never raises into the runner; every failure is returned/logged."""
import html, json, logging, time
import requests
from . import settings as S

log = logging.getLogger("crave.bt.notify")

def send_telegram(text: str) -> bool:
    import os
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        log.info("[telegram disabled] %s", text[:200]); return False
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                              json={"chat_id": chat, "text": text[:3900], "parse_mode": "HTML", "disable_web_page_preview": True})
            if r.status_code == 200: return True
            if r.status_code == 429: time.sleep(float(r.json().get("parameters", {}).get("retry_after", 3)))
            else: log.warning("telegram %s %s", r.status_code, r.text[:120]); break
        except Exception as e:
            log.warning("telegram error: %s", e); time.sleep(2)
    return False

def e(x) -> str: return html.escape(str(x))

def fmt_result(name, symbol, m, head):
    return (f"{head}\n<b>{e(name)}</b> · {e(symbol)}\n"
            f"Net <b>{m.get('return_pct', 0):+.2f}%</b> over {m.get('trades', 0)} trades · exp {m.get('expectancy_r', 0):+.3f}R "
            f"(gross {m.get('expectancy_gross_r', 0):+.3f}R, costs {m.get('avg_cost_r', 0):.3f}R)\n"
            f"PF {m.get('profit_factor', 0)} · maxDD {m.get('max_dd_pct', 0)}% · WR {m.get('win_rate', 0)}%")

def council_payload(job, summary, diagnostics):
    review = {"type": "strategy_backtest_review", "strategy": job["strategy_name"], "strategy_ref": job["strategy_ref"],
              "symbol": job["symbol"], "period_days": job["days"], "metrics": summary, "diagnostics": diagnostics,
              "task": ("This is a BACKTEST REVIEW, not a live trade. Net of real MT5 spread the strategy lost money. "
                       "Explain the most likely structural reasons and propose at most 3 concrete, testable changes "
                       "(parameter or logic). Advisory only; do not assume data beyond what is given."),
              "return_format": {"suggested_changes": [{"param": "name", "value": "new value", "rationale": "why"}]}}
    return review

def _hdr():
    h = {"Content-Type": "application/json"}
    if S.COUNCIL_SECRET: h["X-Council-Secret"] = S.COUNCIL_SECRET
    return h

def forward_to_council(job, summary, diagnostics, wait_s=240):
    """Returns {"status": received|unavailable|rate_limited, "response": ..., "endpoint": ...}."""
    review = council_payload(job, summary, diagnostics)
    base = S.COUNCIL_URL.rstrip("/")
    deadline = time.time() + wait_s
    try:
        st = requests.get(base + "/api/council/status", headers=_hdr(), timeout=5)
        if st.status_code == 200:
            cd = float(st.json().get("cooldown_remaining_s", 0) or 0)
            if cd > 0 and cd < wait_s: time.sleep(cd + 1)
    except Exception as ex:
        return {"status": "unavailable", "error": f"council not reachable: {ex}"}
    attempts = [("/api/council/strategy_review", review),               # dedicated mode (council team may add it)
                ("/api/council/assess", {"topic": f"strategy_review:{job['strategy_name']}",
                                         "context": {"signal": {"symbol": job["symbol"], "direction": "research",
                                                                "strategy_review": review},
                                                     "market": {"note": "backtest review - see signal.strategy_review"}}})]
    for path, body in attempts:
        while time.time() < deadline:
            try:
                r = requests.post(base + path, headers=_hdr(), data=json.dumps(body, default=str), timeout=180)
            except Exception as ex:
                return {"status": "unavailable", "error": str(ex), "endpoint": path}
            if r.status_code == 404: break                                   # try the next endpoint
            if r.status_code == 429:
                time.sleep(min(60, float(r.json().get("cooldown_remaining_s", 20)) + 1)); continue
            if r.status_code == 200:
                return {"status": "received", "endpoint": path, "response": r.json()}
            return {"status": "unavailable", "error": f"HTTP {r.status_code}: {r.text[:200]}", "endpoint": path}
    return {"status": "rate_limited", "error": "council cooldown did not clear in time"}

def extract_suggestions(resp) -> list:
    """Pull structured suggestions if the council returned them; otherwise []. Text is stored verbatim elsewhere."""
    out = []
    if not isinstance(resp, dict): return out
    cand = resp.get("suggested_changes") or (resp.get("result") or {}).get("suggested_changes") or []
    for s in cand if isinstance(cand, list) else []:
        if isinstance(s, dict) and s.get("param") is not None and "value" in s:
            out.append({"param": str(s["param"])[:60], "value": s["value"], "rationale": str(s.get("rationale", ""))[:300]})
    return out[:3]

def summarize_council(resp) -> str:
    if not isinstance(resp, dict): return ""
    for k in ("summary", "reasoning", "recommendation", "verdict"):
        v = resp.get(k) or (resp.get("result") or {}).get(k)
        if v: return str(v)[:700]
    return json.dumps(resp, default=str)[:500]
