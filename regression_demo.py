"""
Regression demo — shows what a real behavioral regression looks like in agenttests.dev.

Simulates two versions of an agent that interacts with Greenlight:
  v1 (baseline): create_request → poll_request  (polls before anyone decides)
  v2 (regressed): create_request → submit_decision → poll_request
                  but also introduces an error by submitting a bad token

Run: python3 regression_demo.py
"""
import sys
import os
import json
import sqlite3
import secrets as _secrets
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "agenttest"))

from agenttest import Tracer

CLOUD_URL = os.environ.get("AGENTTEST_CLOUD_URL", "https://agenttests.dev")
CLOUD_KEY = os.environ.get("AGENTTEST_CLOUD_KEY", "")
DB_PATH   = Path(__file__).parent / "greenlight.db"

tracer = Tracer(
    suite="regression-demo",
    cloud_url=CLOUD_URL,
    cloud_api_key=CLOUD_KEY,
)

import urllib.request, urllib.parse

BASE = "http://127.0.0.1:5000"

def _req(method, path, data=None, headers=None, form=False):
    body, hdrs = None, headers or {}
    if data and form:
        body = urllib.parse.urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif data:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            try: return {"status": r.status, "body": json.loads(r.read())}
            except: return {"status": r.status, "body": {}}
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try: return {"status": e.code, "body": json.loads(raw)}
        except: return {"status": e.code, "body": {}}

def _make_key(suffix=""):
    key = "gl_demo_" + _secrets.token_urlsafe(12)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT INTO api_keys (key, name, email, plan) VALUES (?, ?, ?, ?)",
                 (key, f"Demo{suffix}", f"demo{suffix}@demo.dev", "free"))
    conn.commit(); conn.close()
    return key

def _cleanup():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM api_keys WHERE key LIKE 'gl_demo_%'")
    conn.execute("DELETE FROM approval_requests WHERE api_key LIKE 'gl_demo_%'")
    conn.commit(); conn.close()

# ── Tool definitions ───────────────────────────────────────────────────────────

@tracer.tool
def create_request(api_key: str, title: str) -> dict:
    return _req("POST", "/v1/requests",
                {"title": title, "notify_telegram": ""},
                headers={"Authorization": f"Bearer {api_key}"})

@tracer.tool
def poll_request(api_key: str, req_id: str) -> dict:
    return _req("GET", f"/v1/requests/{req_id}",
                headers={"Authorization": f"Bearer {api_key}"})

@tracer.tool
def submit_decision(req_id: str, token: str, decision: str) -> dict:
    return _req("POST", f"/approve/{req_id}/submit",
                {"decision": decision, "token": token, "comment": ""},
                form=True)

@tracer.tool
def validate_response(resp: dict) -> bool:
    """Check the response looks sane before proceeding."""
    return resp.get("status") == 201

# ── v1: healthy baseline ───────────────────────────────────────────────────────

def run_v1(api_key: str):
    """Normal flow: create → validate → poll."""
    with tracer.session("deploy-agent") as trace:
        resp = create_request(api_key, "Deploy hotfix to production?")
        ok   = validate_response(resp)
        req_id = resp["body"].get("id", "")
        status = poll_request(api_key, req_id)
    return trace

# ── v2: regressed ─────────────────────────────────────────────────────────────

def run_v2(api_key: str):
    """
    Regression: validate_response was removed (refactor dropped it),
    and submit_decision was added mid-flow with a bad token causing an error.
    poll_request is now called with a hardcoded fallback id (bug).
    """
    with tracer.session("deploy-agent") as trace:
        resp   = create_request(api_key, "Deploy hotfix to production?")
        req_id = resp["body"].get("id", "")
        # Bug: submits with wrong token
        submit_decision(req_id, "wrong_token_oops", "Approve")
        # Bug: polls a hardcoded id instead of the real one
        status = poll_request(api_key, "hardcoded-id-bug")
    return trace

# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    key1 = _make_key("_v1")
    key2 = _make_key("_v2")

    print("Running v1 (baseline)...")
    t1 = run_v1(key1)
    print(f"  calls: {[c.tool for c in t1.tool_calls]}")

    print("Running v2 (regressed)...")
    t2 = run_v2(key2)
    print(f"  calls: {[c.tool for c in t2.tool_calls]}")
    print(f"  errors: {[c.tool for c in t2.tool_calls if c.error]}")

    _cleanup()

    print(f"\nDiff URL:")
    print(f"  https://agenttests.dev/dashboard?key={CLOUD_KEY}&suite=regression-demo")
