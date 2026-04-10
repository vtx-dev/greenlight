"""
Greenlight API integration tests using agenttest.

Each API endpoint is wrapped as a @tracer.tool so agenttest records the full
call sequence, arguments, and responses — same as it would for an LLM agent.

Run: pytest tests/test_api.py -v
     pytest tests/test_api.py -v -s   # to see full trace output
"""
import sys
import os
import json
import sqlite3
import time
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "agenttest"))

import pytest
from agenttest import (
    Tracer,
    assert_tool_called, assert_tool_not_called,
    assert_sequence, assert_tool_arg_contains,
    assert_no_errors, assert_tool_succeeded,
)

BASE    = "http://127.0.0.1:5000"
DB_PATH = Path(__file__).parent.parent / "greenlight.db"

tracer = Tracer(
    traces_dir=str(Path(__file__).parent / "traces"),
    suite="greenlight-api",
    cloud_url=os.environ.get("AGENTTEST_CLOUD_URL", "https://agenttests.dev"),
    cloud_api_key=os.environ.get("AGENTTEST_CLOUD_KEY", "REDACTED"),
)


# ── API wrappers (each becomes a recorded tool call) ──────────────────────────

def _req(method, path, data=None, headers=None, form=False):
    body = None
    hdrs = headers or {}
    if data and form:
        body = urllib.parse.urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif data:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers=hdrs, method=method)

    def _parse(raw_bytes, content_type=""):
        if "application/json" in content_type:
            return json.loads(raw_bytes)
        try:
            return json.loads(raw_bytes)
        except (json.JSONDecodeError, ValueError):
            return raw_bytes.decode(errors="replace")

    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            ct = r.headers.get("Content-Type", "")
            return {"status": r.status, "body": _parse(r.read(), ct)}
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        ct = e.headers.get("Content-Type", "") if e.headers else ""
        return {"status": e.code, "body": _parse(raw, ct)}


@tracer.tool
def register_key(name: str, email: str, _spoof_ip: str = "") -> dict:
    headers = {"X-Forwarded-For": _spoof_ip} if _spoof_ip else {}
    return _req("POST", "/v1/keys", {"name": name, "email": email}, headers=headers)


@tracer.tool
def create_request(api_key: str, title: str, description: str = "",
                   options: list = None, notify_telegram: str = "") -> dict:
    # Always send notify_telegram — empty string suppresses server-default notifications.
    payload = {"title": title, "notify_telegram": notify_telegram}
    if description:
        payload["description"] = description
    if options:
        payload["options"] = options
    return _req("POST", "/v1/requests", payload,
                headers={"Authorization": f"Bearer {api_key}"})


@tracer.tool
def poll_request(api_key: str, req_id: str) -> dict:
    return _req("GET", f"/v1/requests/{req_id}",
                headers={"Authorization": f"Bearer {api_key}"})


@tracer.tool
def list_requests(api_key: str) -> dict:
    return _req("GET", "/v1/requests",
                headers={"Authorization": f"Bearer {api_key}"})


@tracer.tool
def submit_decision(req_id: str, token: str, decision: str, comment: str = "") -> dict:
    return _req("POST", f"/approve/{req_id}/submit",
                {"decision": decision, "token": token, "comment": comment},
                form=True)


def _get_token(req_id: str) -> str:
    """Fetch the human_token directly from DB for test purposes."""
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT human_token FROM approval_requests WHERE id = ?", (req_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _make_key(suffix="") -> str:
    """
    Insert a test API key directly into the DB — bypasses rate limiter.
    Returns the key string.
    """
    import secrets as _s
    key = "gl_test_" + _s.token_urlsafe(16)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO api_keys (key, name, email, plan) VALUES (?, ?, ?, ?)",
        (key, f"Test Agent{suffix}", f"test{suffix}@agenttest.dev", "free")
    )
    conn.commit()
    conn.close()
    return key


def _cleanup_test_keys():
    """Remove all test keys inserted by this suite."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM api_keys WHERE key LIKE 'gl_test_%'")
    conn.execute("DELETE FROM approval_requests WHERE api_key LIKE 'gl_test_%'")
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True, scope="session")
def cleanup(request):
    yield
    _cleanup_test_keys()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestKeyRegistration:
    def test_register_returns_api_key(self):
        with tracer.session("register_key") as trace:
            result = register_key("My Agent", "agent@example.com", _spoof_ip="10.1.0.1")

        assert result["status"] == 201
        assert result["body"]["api_key"].startswith("gl_")
        assert result["body"]["plan"] == "free"
        assert_tool_called(trace, "register_key")
        assert_no_errors(trace)

    def test_register_requires_name_and_email(self):
        with tracer.session("register_missing_fields") as trace:
            result = _req("POST", "/v1/keys", {"name": "No Email"},
                          headers={"X-Forwarded-For": "10.1.0.2"})

        assert result["status"] == 400

    def test_register_rate_limit(self):
        """Registering from same IP many times should eventually 429."""
        import random
        # Use a fresh random IP each run so prior test runs don't exhaust the bucket
        spoof_ip = f"10.{random.randint(100,254)}.{random.randint(0,254)}.{random.randint(1,254)}"
        results = []
        with tracer.session("register_rate_limit") as trace:
            for i in range(5):
                r = register_key(f"Spam Agent {i}", f"spam{i}@test.com",
                                 _spoof_ip=spoof_ip)
                results.append(r["status"])
        # First 3 should succeed (limit is 3/hr), rest 429
        assert 201 in results
        assert 429 in results


class TestApprovalFlow:
    def test_full_approve_flow(self):
        """Register → create request → approve → poll shows decided."""
        with tracer.session("full_approve_flow") as trace:
            api_key = _make_key("_approve")
            resp = create_request(api_key, "Deploy to production?",
                                  description="Agent wants to deploy v1.2")
            req_id = resp["body"]["id"]
            token = _get_token(req_id)
            submit_decision(req_id, token, "Approve", "LGTM")
            status = poll_request(api_key, req_id)

        assert_sequence(trace, "create_request", "submit_decision", "poll_request", strict=True)
        assert_no_errors(trace)
        assert status["body"]["status"] == "decided"
        assert status["body"]["decision"] == "Approve"
        assert status["body"]["decision_comment"] == "LGTM"

    def test_full_reject_flow(self):
        """Register → create → reject → poll shows rejected."""
        with tracer.session("full_reject_flow") as trace:
            api_key = _make_key("_reject")
            resp = create_request(api_key, "Delete all user data?")
            req_id = resp["body"]["id"]
            token = _get_token(req_id)
            submit_decision(req_id, token, "Reject", "Too dangerous")
            status = poll_request(api_key, req_id)

        assert status["body"]["decision"] == "Reject"
        assert_no_errors(trace)

    def test_custom_options(self):
        """Agent can specify custom decision options."""
        with tracer.session("custom_options") as trace:
            api_key = _make_key("_opts")
            resp = create_request(api_key, "Which environment?",
                                  options=["Staging", "Production", "Cancel"])
            req_id = resp["body"]["id"]
            token = _get_token(req_id)
            submit_decision(req_id, token, "Staging")
            status = poll_request(api_key, req_id)

        assert resp["status"] == 201
        assert status["body"]["decision"] == "Staging"

    def test_invalid_decision_rejected(self):
        """Submitting a decision not in options should 400."""
        with tracer.session("invalid_decision") as trace:
            api_key = _make_key("_inv")
            resp = create_request(api_key, "Test?")
            req_id = resp["body"]["id"]
            token = _get_token(req_id)
            result = submit_decision(req_id, token, "Maybe")  # not in ["Approve","Reject"]

        assert result["status"] == 400

    def test_cannot_decide_twice(self):
        """Once decided, submitting again should 400."""
        with tracer.session("double_decide") as trace:
            api_key = _make_key("_2x")
            resp = create_request(api_key, "One-shot?")
            req_id = resp["body"]["id"]
            token = _get_token(req_id)
            submit_decision(req_id, token, "Approve")
            second = submit_decision(req_id, token, "Reject")  # too late

        assert second["status"] == 400

    def test_wrong_token_rejected(self):
        """Approval URL with wrong token should 400."""
        with tracer.session("wrong_token") as trace:
            api_key = _make_key("_tok")
            resp = create_request(api_key, "Secret action?")
            req_id = resp["body"]["id"]
            result = submit_decision(req_id, "wrong_token_abc", "Approve")

        assert result["status"] == 400


class TestAuth:
    def test_missing_api_key_returns_401(self):
        with tracer.session("no_auth") as trace:
            result = _req("POST", "/v1/requests", {"title": "Test"})

        assert result["status"] == 401

    def test_invalid_api_key_returns_401(self):
        with tracer.session("bad_auth") as trace:
            result = _req("POST", "/v1/requests", {"title": "Test"},
                          headers={"Authorization": "Bearer gl_fakefakefake"})

        assert result["status"] == 401

    def test_cannot_see_other_keys_requests(self):
        """A key should only see its own requests."""
        with tracer.session("isolation") as trace:
            key_a = _make_key("_a")
            key_b = _make_key("_b")
            create_request(key_a, "Request from A")
            listing = list_requests(key_b)

        # B should have 0 requests
        assert listing["body"]["requests"] == []


class TestRateLimiting:
    def test_free_plan_has_request_limit(self):
        """Free plan allows 10 requests/month then 429s."""
        with tracer.session("rate_limit_requests") as trace:
            api_key = _make_key("_rl")
            statuses = []
            for i in range(12):
                r = create_request(api_key, f"Request {i}")
                statuses.append(r["status"])

        assert statuses[:10].count(201) == 10
        assert 429 in statuses[10:]


class TestInputValidation:
    def test_empty_title_rejected(self):
        with tracer.session("empty_title") as trace:
            api_key = _make_key("_et")
            result = create_request(api_key, "")

        assert result["status"] == 400

    def test_title_truncated_at_limit(self):
        with tracer.session("long_title") as trace:
            api_key = _make_key("_lt")
            result = create_request(api_key, "A" * 300)

        # Should succeed — server truncates rather than rejects
        assert result["status"] == 201
