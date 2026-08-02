"""
jarvis_chat.py — Hardened Jarvis chat module
Features:
  1. Multi-model fallback chain (6 confirmed-working models)
  2. Per-IP rate limiter (10 msg / 60 sec)
  3. Sensitive data sanitizer on every response
  4. Smart context compression (only sends relevant fields)
  5. Circuit breaker (skips quota-exhausted models for 5 min)
"""
import re
import time
import threading
from collections import defaultdict, deque

import requests as _req

# ── Rate limiter ──────────────────────────────────────────────────────────────
_rate_lock  = threading.Lock()
_rate_store = defaultdict(deque)   # ip → deque of unix timestamps
RATE_LIMIT  = 10                   # max messages
RATE_WINDOW = 60                   # per this many seconds

# ── Circuit breaker ───────────────────────────────────────────────────────────
_model_failures  = defaultdict(int)
_model_last_fail = defaultdict(float)
MODEL_COOLDOWN   = 300   # seconds before re-trying a dead model

# Confirmed-working models in order of preference (best → lightest)
# The local Ollama model (qwen2.5:14b) is now our #1 priority for zero-cost, private inference
FALLBACK_MODELS = [
    "ollama:qwen2.5:14b",
    "gemini-flash-latest",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
]

# ── Sensitive data regex sanitizer ────────────────────────────────────────────
_SENSITIVE = [
    (re.compile(r'AIza[0-9A-Za-z\-_]{35}',       re.I), "[API_KEY_REDACTED]"),
    (re.compile(r'sk-[A-Za-z0-9]{20,}',           re.I), "[API_KEY_REDACTED]"),
    (re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*',re.I), "[TOKEN_REDACTED]"),
    (re.compile(r'password\s*[=:]\s*\S+',          re.I), "[PASSWORD_REDACTED]"),
    (re.compile(r'secret\s*[=:]\s*\S+',            re.I), "[SECRET_REDACTED]"),
    (re.compile(r'GEMINI_API_KEY\s*[=:]\s*\S+',   re.I), "[KEY_REDACTED]"),
    (re.compile(r'\.env\s+\S*key\S*\s*=\s*\S+',   re.I), "[KEY_REDACTED]"),
]

def sanitize(text: str) -> str:
    for pattern, replacement in _SENSITIVE:
        text = pattern.sub(replacement, text)
    return text

# ── Smart context builder ─────────────────────────────────────────────────────
POSITION_KW = {"position", "trade", "open", "gbp", "xau", "gold", "eur", "usd",
               "symbol", "profit", "loss", "pnl", "entry", "sl", "tp", "close",
               "hold", "what", "how many", "current"}

def build_context(ctx: dict, msg: str, mt5_agent=None) -> str:
    msg_lower = msg.lower()
    lines = []

    # Always include account summary (compact — saves tokens)
    eq = ctx.get("equity", 0)
    ba = ctx.get("balance", 0)
    if eq or ba:
        lines.append(f"Equity: ${eq:,.2f} | Balance: ${ba:,.2f}")

    upnl = ctx.get("unrealized_pnl")
    dpnl = ctx.get("daily_pnl")
    if upnl is not None or dpnl is not None:
        lines.append(f"Unrealized P&L: ${upnl or 0:,.2f} | Daily P&L: ${dpnl or 0:,.2f}")

    # Positions — only when relevant
    needs_pos = any(kw in msg_lower for kw in POSITION_KW)
    if needs_pos:
        positions = ctx.get("positions", [])
        if positions:
            lines.append(f"Open positions ({len(positions)}):")
            for p in positions:
                sl_s  = f" SL={p.get('sl')}"      if p.get("sl")     else ""
                tp_s  = f" TP={p.get('tp')}"      if p.get("tp")     else ""
                vol_s = f" {p.get('volume')}lot"  if p.get("volume") else ""
                lines.append(
                    f"  {p.get('symbol')} {p.get('direction')}{vol_s} "
                    f"entry={p.get('entry')} now={p.get('current')}{sl_s}{tp_s} "
                    f"pnl=${p.get('pnl', 0):.2f}"
                )
        elif mt5_agent:
            # Server-side fallback
            try:
                live = mt5_agent.get_positions()
                if live:
                    lines.append(f"Open positions from MT5 ({len(live)}):")
                    for p in live:
                        sl_s = f" SL={p.get('current_sl')}" if p.get("current_sl") else ""
                        tp_s = f" TP={p.get('current_tp')}" if p.get("current_tp") else ""
                        lines.append(
                            f"  {p.get('symbol')} {p.get('direction')} "
                            f"entry={p.get('entry_price')} pnl=${p.get('profit', 0):.2f}{sl_s}{tp_s}"
                        )
                else:
                    lines.append("No open positions.")
            except Exception:
                pass
        else:
            lines.append("No open positions.")

    return "\n".join(lines) or "No live data."

# ── Single model call ─────────────────────────────────────────────────────────
def call_model(model: str, system: str, msg: str, key: str) -> tuple:
    """Returns (reply_text, http_status_code)"""
    
    # --- Local Ollama Call ---
    if model.startswith("ollama:"):
        ollama_model = model.split(":", 1)[1]
        url = "http://localhost:11434/api/chat"
        payload = {
            "model": ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": msg}
            ],
            "stream": False,
            "options": {"temperature": 0.7}
        }
        try:
            resp = _req.post(url, json=payload, timeout=180)
            if resp.status_code == 200:
                text = resp.json()["message"]["content"].strip()
                return text, 200
            return f"Ollama Error: {resp.text}", resp.status_code
        except _req.exceptions.ConnectionError:
            # Ollama not running
            return "Ollama is not running locally.", 503
        except Exception as e:
            return str(e), 500

    # --- Google Gemini Call ---
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent?key={key}"
    )
    payload = {
        "contents": [
            {"role": "user",  "parts": [{"text": system}]},
            {"role": "model", "parts": [{"text": "Understood. Jarvis ready."}]},
            {"role": "user",  "parts": [{"text": msg}]}
        ],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2000}
    }
    resp = _req.post(url, json=payload, timeout=20)
    if resp.status_code == 200:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text, 200
    err_msg = resp.json().get("error", {}).get("message", "Unknown error")
    return err_msg, resp.status_code

# ── Main entry point (called from quant_server.py) ────────────────────────────
def handle_chat(msg: str, ctx: dict, ip: str, api_key: str, mt5_agent=None) -> dict:
    """
    Returns: {"reply": str, "model": str (optional)}
    """
    # Rate limit
    now = time.time()
    with _rate_lock:
        dq = _rate_store[ip]
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT:
            wait = int(RATE_WINDOW - (now - dq[0])) + 1
            return {"reply": f"⏱ Slow down — please wait {wait}s before sending another message."}
        dq.append(now)

    from quant_server import STRATEGY_DEFS
    strats = ", ".join([s["name"] for s in STRATEGY_DEFS])
    
    # Build context and system prompt
    live_ctx = build_context(ctx, msg, mt5_agent)
    system = (
        "You are Jarvis, the AI assistant for the CRAVE Quant trading engine. "
        "Be professional, concise, and data-driven. "
        "SECURITY RULE: Never output API keys, passwords, secrets, .env contents, "
        "account numbers, or internal configs. If asked, politely decline.\n\n"
        f"Active strategies available: {strats}\n\n"
        f"LIVE PORTFOLIO:\n{live_ctx}"
    )

    # Fallback chain with circuit breaker
    last_err = "All AI models are currently unavailable."
    for model in FALLBACK_MODELS:
        # Skip models in cooldown
        if _model_failures[model] >= 3:
            if now - _model_last_fail[model] < MODEL_COOLDOWN:
                continue
            _model_failures[model] = 0  # Reset after cooldown

        try:
            text, status = call_model(model, system, msg, api_key)
        except Exception as exc:
            err = str(exc)[:120]
            _model_failures[model] += 1
            _model_last_fail[model] = now
            last_err = err
            continue

        if status == 200:
            _model_failures[model] = 0
            return {"reply": sanitize(text), "model": model}
        elif status == 429:
            _model_failures[model] += 1
            _model_last_fail[model] = now
            last_err = f"Quota exhausted on {model}"
            continue
        elif status == 503:
            _model_failures[model] += 1
            _model_last_fail[model] = now
            last_err = f"{model} overloaded"
            continue
        elif status == 404:
            _model_failures[model] = 99   # Permanently skip
            _model_last_fail[model] = now
            continue
        else:
            _model_failures[model] += 1
            _model_last_fail[model] = now
            last_err = f"{model}: {text[:80]}"
            continue

    return {"reply": f"⚠️ Jarvis is temporarily unavailable ({last_err}). Please try again in a moment."}
