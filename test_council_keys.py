"""
CRAVE Council v2 — API Key Verification (supports OpenRouter for Gemini slot)
"""
import os, sys, io
# Force UTF-8 output on Windows to avoid cp1252 errors with emoji/box chars
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    print("[!] pip install python-dotenv"); sys.exit(1)

try:
    import requests
except ImportError:
    print("[!] pip install requests"); sys.exit(1)

RESULTS = []
RECOMMENDED = {}

def report(provider, status, detail=""):
    icon = {"PASS": "✅", "SKIP": "⬛", "FAIL": "❌", "WARN": "⚠️"}[status]
    print(f"{icon}  {provider:<35} {status}  {detail}")
    RESULTS.append((provider, status))

# ─── Gemini (native OR OpenRouter) ───────────────────────────────────────────
def test_gemini():
    key      = os.environ.get("COUNCIL_GEMINI_API_KEY", "").strip()
    model    = os.environ.get("COUNCIL_GEMINI_MODEL", "gemini-2.0-flash").strip()
    base_url = os.environ.get("COUNCIL_GEMINI_BASE_URL", "").strip()
    if not key:
        report("Council Gemini / OpenRouter", "SKIP", "(COUNCIL_GEMINI_API_KEY is blank)"); return

    # ── Path A: OpenRouter (OpenAI-compat endpoint) ──
    if base_url:
        # OpenRouter or any OpenAI-compat base
        openrouter_free = [
            "google/gemini-2.0-flash-exp:free",
            "google/gemini-2.5-flash-preview:thinking",
            "meta-llama/llama-3.3-70b-instruct:free",
            "meta-llama/llama-3.1-8b-instruct:free",
            "deepseek/deepseek-r1:free",
            "mistralai/mistral-7b-instruct:free",
        ]
        # Use env model if set, else try free models in order
        candidates = [model] if model else openrouter_free
        if model not in openrouter_free:
            candidates = [model] + openrouter_free

        for candidate in candidates:
            try:
                hdrs = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://crave-quant.local",
                    "X-Title": "CRAVE Council v2",
                }
                body = {
                    "model": candidate,
                    "messages": [{"role": "user", "content": "Reply with one word: ONLINE"}],
                    "max_tokens": 10,
                }
                resp = requests.post(f"{base_url.rstrip('/')}/chat/completions",
                                     json=body, headers=hdrs, timeout=20)
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    report("Council Gemini / OpenRouter", "PASS",
                           f"model={candidate}  reply={text!r}")
                    RECOMMENDED["COUNCIL_GEMINI_MODEL"] = candidate
                    return
                elif resp.status_code == 429:
                    continue   # try next free model
            except Exception as e:
                report("Council Gemini / OpenRouter", "FAIL", str(e)); return

        report("Council Gemini / OpenRouter", "FAIL",
               "All free OpenRouter models rate-limited. Try again in a few minutes.")
        return

    # ── Path B: Native Gemini API ──
    exclude_kw = ["image", "embed", "vision", "aqa", "retriev", "lyria", "clip",
                  "audio", "music", "tts", "speech"]
    prefer_text = ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-2.0-flash-exp",
                   "gemini-1.5-flash-latest", "gemini-1.5-flash-002",
                   "gemini-1.5-pro-latest", "gemini-pro"]
    available_text = set()

    try:
        r = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            timeout=15)
        if r.status_code != 200:
            report("Council Gemini (native)", "FAIL",
                   f"Cannot list models — HTTP {r.status_code}: {r.text[:100]}")
            return
        for m in r.json().get("models", []):
            name = m["name"].replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            is_excluded = any(x in name.lower() for x in exclude_kw)
            if "generateContent" in methods and not is_excluded:
                available_text.add(name)
    except Exception as e:
        report("Council Gemini (native)", "FAIL", f"Network error: {e}"); return

    chosen = model if model in available_text else \
             next((m for m in prefer_text if m in available_text), None) or \
             next(iter(available_text), None)

    if not chosen:
        report("Council Gemini (native)", "FAIL", "No text-capable models found"); return

    RECOMMENDED["COUNCIL_GEMINI_MODEL"] = chosen

    try:
        url  = f"https://generativelanguage.googleapis.com/v1beta/models/{chosen}:generateContent?key={key}"
        body = {"contents": [{"parts": [{"text": "Reply with one word: ONLINE"}]}]}
        resp = requests.post(url, json=body, timeout=20)
        if resp.status_code == 200:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            report("Council Gemini (native)", "PASS", f"model={chosen}  reply={text!r}")
        elif resp.status_code == 429:
            report("Council Gemini (native)", "WARN",
                   f"Key VALID, free-tier quota exhausted. model={chosen}\n"
                   f"{'':37}→ Enable billing at aistudio.google.com OR use OpenRouter (see below)")
        else:
            report("Council Gemini (native)", "FAIL",
                   f"model={chosen}  HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        report("Council Gemini (native)", "FAIL", str(e))

# ─── Groq ─────────────────────────────────────────────────────────────────────
def test_groq():
    key   = os.environ.get("COUNCIL_GROQ_API_KEY", "").strip()
    model = os.environ.get("COUNCIL_GROQ_MODEL", "").strip()
    if not key:
        report("Council Groq", "SKIP", "(COUNCIL_GROQ_API_KEY is blank)"); return

    prefer = ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile",
              "qwen/qwen3.8-27b", "llama3-70b-8192", "mixtral-8x7b-32768",
              "llama-3.1-8b-instant", "llama3-8b-8192", "gemma2-9b-it"]
    available = set()

    try:
        hdrs = {"Authorization": f"Bearer {key}"}
        r = requests.get("https://api.groq.com/openai/v1/models", headers=hdrs, timeout=15)
        if r.status_code == 200:
            for m in r.json().get("data", []):
                available.add(m["id"])
        else:
            report("Council Groq", "FAIL",
                   f"Cannot list models — HTTP {r.status_code}: {r.text[:100]}"); return
    except Exception as e:
        report("Council Groq", "FAIL", f"Network error: {e}"); return

    chosen = model if model in available else \
             next((m for m in prefer if m in available), None) or \
             next(iter(available), None)

    if not chosen:
        report("Council Groq", "FAIL", "No models available"); return

    RECOMMENDED["COUNCIL_GROQ_MODEL"] = chosen

    try:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"model": chosen,
                "messages": [{"role": "user", "content": "Reply with one word: ONLINE"}],
                "max_tokens": 10}
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                             json=body, headers=hdrs, timeout=20)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            report("Council Groq", "PASS", f"model={chosen}  reply={text!r}")
        elif resp.status_code == 429:
            report("Council Groq", "WARN", f"Rate limited. model={chosen}")
        else:
            report("Council Groq", "FAIL",
                   f"model={chosen}  HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        report("Council Groq", "FAIL", str(e))

# ─── OpenAI-compat ────────────────────────────────────────────────────────────
def test_openai():
    key   = os.environ.get("COUNCIL_OPENAI_API_KEY", "").strip()
    model = os.environ.get("COUNCIL_OPENAI_MODEL", "gpt-4o-mini")
    base  = os.environ.get("COUNCIL_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not key:
        report("Council OpenAI", "SKIP", "(COUNCIL_OPENAI_API_KEY is blank)"); return

    try:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"model": model,
                "messages": [{"role": "user", "content": "Reply with one word: ONLINE"}],
                "max_tokens": 10}
        resp = requests.post(f"{base}/chat/completions", json=body, headers=hdrs, timeout=20)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            report("Council OpenAI", "PASS", f"model={model}  reply={text!r}")
        elif resp.status_code == 429:
            report("Council OpenAI", "WARN",
                   "Key VALID — no credits. Top up at platform.openai.com")
        elif resp.status_code in (401, 403):
            report("Council OpenAI", "FAIL", f"Invalid/revoked key — HTTP {resp.status_code}")
        else:
            report("Council OpenAI", "FAIL", f"HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        report("Council OpenAI", "FAIL", str(e))

# ─── Main ─────────────────────────────────────────────────────────────────────
print("\n" + "═"*68)
print("  CRAVE Council v2 — API Key Test")
print("═"*68)
test_gemini()
test_groq()
test_openai()
print("═"*68)

passed  = sum(1 for _, s in RESULTS if s == "PASS")
warned  = sum(1 for _, s in RESULTS if s == "WARN")
failed  = sum(1 for _, s in RESULTS if s == "FAIL")
skipped = sum(1 for _, s in RESULTS if s == "SKIP")
print(f"\nSummary: {passed} PASS  |  {warned} WARN  |  {failed} FAIL  |  {skipped} SKIP")

if RECOMMENDED:
    print("\n📝  Recommended .env updates:")
    for k, v in RECOMMENDED.items():
        current = os.environ.get(k, "")
        marker = "✓ already set" if current == v else f"← update from {current!r}"
        print(f"    {k}={v}   {marker}")

gemini_warn = any(p == "Council Gemini (native)" and s == "WARN" for p, s in RESULTS)
if gemini_warn:
    print("\n💡  To use Gemini for free via OpenRouter (you already have a key):")
    print("    Add to .env:")
    print("      COUNCIL_GEMINI_BASE_URL=https://openrouter.ai/api/v1")
    print("      COUNCIL_GEMINI_API_KEY=<your OPENROUTER_API_KEY value>")
    print("      COUNCIL_GEMINI_MODEL=google/gemini-2.0-flash-exp:free")
    print("    Then re-run this script.")

if failed == 0 and warned == 0:
    print("\n✅  All configured providers working. Ready for Stage 1A.")
elif failed == 0:
    print(f"\n⚠️   {passed}/3 providers active. Council will use working providers only.")
    if passed >= 1:
        print("    ✅  Enough to proceed to Stage 1A.")
else:
    print("\n❌  Some keys failed. Check above.")
print()
