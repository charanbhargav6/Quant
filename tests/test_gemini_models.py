"""
Test all available Gemini models via raw REST and pick the first one that returns a real response.
Uses requests directly (no SDK) to avoid the deprecated google.generativeai package.
"""
import os, sys, time, requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("GEMINI_API_KEY", "")
if not api_key:
    print("ERROR: GEMINI_API_KEY not set in .env")
    sys.exit(1)

# Fetch all models via REST
list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
r = requests.get(list_url)
models_data = r.json().get("models", [])

# Filter to text generation capable models only
models = [
    m["name"] for m in models_data
    if "generateContent" in m.get("supportedGenerationMethods", [])
]

print(f"Found {len(models)} models. Testing each...\n")

test_payload = {
    "contents": [{"role": "user", "parts": [{"text": "Reply with exactly: JARVIS_OK"}]}]
}

working_model = None

for model_name in models:
    short = model_name.replace("models/", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={api_key}"
    try:
        resp = requests.post(url, json=test_payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "NO_TEXT")
            print(f"  PASS {short}: '{text[:60].strip()}'")
            if working_model is None:
                working_model = short
        elif resp.status_code == 429:
            print(f"  QUOTA {short}: Free tier exhausted")
        elif resp.status_code == 404:
            print(f"  MISS  {short}: Not found / not available to new users")
        else:
            err = resp.json().get("error", {}).get("message", "")[:80]
            print(f"  ERR   {short}: HTTP {resp.status_code} - {err}")
    except Exception as e:
        print(f"  EXC   {short}: {str(e)[:80]}")
    time.sleep(0.5)

print()
if working_model:
    print(f"BEST_MODEL={working_model}")
else:
    print("BEST_MODEL=NONE - All models quota-exhausted or unavailable for this API key.")
    print("ACTION_REQUIRED: Your API key has exhausted its free tier.")
    print("Solution: Enable billing at https://aistudio.google.com or use a different GEMINI_API_KEY")
