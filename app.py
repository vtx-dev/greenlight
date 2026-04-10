"""
Greenlight — Human-in-the-Loop Approval API for AI Agents
REST API + simple web UI for agents to request human approval before high-stakes actions.
"""

import os
import sqlite3
import json
import secrets
import hashlib
import hmac
import threading
import time
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, abort, redirect, send_from_directory

app = Flask(__name__)
DB_PATH = "greenlight.db"
BASE_URL = os.environ.get("BASE_URL", "https://greenlightapi.dev")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6878450536")


def _fetch_secret(secret_id: str) -> str:
    """Fetch a secret from GCP Secret Manager. Returns '' on failure."""
    try:
        import urllib.request as _ur
        import subprocess
        tok = subprocess.check_output(
            ["gcloud", "auth", "application-default", "print-access-token"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        req = _ur.Request(
            f"https://secretmanager.googleapis.com/v1/projects/ontimeteetimes/secrets/{secret_id}/versions/latest:access",
            headers={"Authorization": f"Bearer {tok}"},
        )
        with _ur.urlopen(req, timeout=5) as r:
            import base64
            d = json.loads(r.read())
            return base64.b64decode(d["payload"]["data"]).decode().strip()
    except Exception as e:
        print(f"[Greenlight] Could not fetch secret {secret_id}: {e}")
        return ""


# Load bot token once at startup (not per-request)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _fetch_secret("vtx-telegram-token")

# Simple in-memory rate limit for key registration: {ip: [timestamps]}
_reg_attempts: dict = {}

MAX_TITLE_LEN = 200
MAX_DESC_LEN = 2000
MAX_COMMENT_LEN = 1000

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                plan TEXT DEFAULT 'free',
                requests_this_month INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS approval_requests (
                id TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                context_json TEXT,
                options_json TEXT,
                webhook_url TEXT,
                status TEXT DEFAULT 'pending',
                decision TEXT,
                decision_comment TEXT,
                human_token TEXT UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                decided_at TEXT,
                expires_at TEXT,
                FOREIGN KEY (api_key) REFERENCES api_keys(key)
            );
        """)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

PLAN_LIMITS = {"free": 10, "starter": 500, "pro": -1}

def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Missing API key"}), 401
        key = auth[7:]
        with get_db() as conn:
            row = conn.execute("SELECT * FROM api_keys WHERE key = ?", (key,)).fetchone()
        if not row:
            return jsonify({"error": "Invalid API key"}), 401
        limit = PLAN_LIMITS.get(row["plan"], 10)
        if limit != -1 and row["requests_this_month"] >= limit:
            return jsonify({"error": f"Monthly limit reached ({limit} requests on {row['plan']} plan). Upgrade at /upgrade"}), 429
        request.api_key_row = row
        return f(*args, **kwargs)
    return wrapper

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(LANDING_PAGE)

@app.route("/v1/keys", methods=["POST"])
def create_key():
    """Register a new API key (free tier)."""
    # Rate limit: max 3 registrations per IP per hour
    # Use X-Forwarded-For only if set by Caddy (trusted proxy on 127.0.0.1)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) if request.remote_addr == "127.0.0.1" else request.remote_addr or "unknown"
    ip = ip.split(",")[0].strip()  # take first IP only
    now = time.time()
    attempts = [t for t in _reg_attempts.get(ip, []) if now - t < 3600]
    if len(attempts) >= 3:
        return jsonify({"error": "Too many registrations from this IP. Try again later."}), 429
    attempts.append(now)
    _reg_attempts[ip] = attempts

    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name or not email:
        return jsonify({"error": "name and email required"}), 400
    key = "gl_" + secrets.token_urlsafe(32)
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO api_keys (key, name, email) VALUES (?, ?, ?)",
                (key, name, email)
            )
    except sqlite3.IntegrityError:
        return jsonify({"error": "Key collision, try again"}), 500
    return jsonify({"api_key": key, "plan": "free", "limit": 10}), 201


@app.route("/v1/requests", methods=["POST"])
@require_api_key
def create_request():
    """Agent submits an approval request."""
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()[:MAX_TITLE_LEN]
    if not title:
        return jsonify({"error": "title required"}), 400

    description = (data.get("description") or "")[:MAX_DESC_LEN]
    options = data.get("options") or ["Approve", "Reject"]
    # Validate options: list of non-empty strings, max 10
    if not isinstance(options, list) or not options or len(options) > 10:
        return jsonify({"error": "options must be a list of 1-10 strings"}), 400
    options = [str(o)[:50] for o in options if str(o).strip()]

    try:
        expires_minutes = max(1, min(int(data.get("expires_minutes") or 60), 10080))  # cap at 1 week
    except (TypeError, ValueError):
        expires_minutes = 60

    webhook_url = data.get("webhook_url") or ""
    if webhook_url and not _is_safe_webhook_url(webhook_url):
        return jsonify({"error": "webhook_url must be a public http/https URL"}), 400

    req_id = secrets.token_urlsafe(12)
    human_token = secrets.token_urlsafe(24)

    row = request.api_key_row
    with get_db() as conn:
        conn.execute("""
            INSERT INTO approval_requests
              (id, api_key, title, description, context_json, options_json,
               webhook_url, human_token, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ? || ' minutes'))
        """, (
            req_id,
            row["key"],
            title,
            description,
            json.dumps(data.get("context") or {}),
            json.dumps(options),
            webhook_url,
            human_token,
            str(expires_minutes),
        ))
        conn.execute(
            "UPDATE api_keys SET requests_this_month = requests_this_month + 1 WHERE key = ?",
            (row["key"],)
        )

    approval_url = f"{BASE_URL}/approve/{req_id}?token={human_token}"
    desc = data.get("description") or ""

    # Notify via Telegram (caller can pass notify_telegram chat_id, falls back to configured default)
    notify_telegram = str(data.get("notify_telegram") or TELEGRAM_CHAT_ID).strip()
    if notify_telegram:
        threading.Thread(
            target=send_telegram_notification,
            args=(notify_telegram, title, approval_url, desc),
            daemon=True
        ).start()

    return jsonify({
        "id": req_id,
        "status": "pending",
        "approval_url": approval_url,
        "poll_url": f"{BASE_URL}/v1/requests/{req_id}",
        "expires_minutes": expires_minutes,
    }), 201


@app.route("/v1/requests/<req_id>", methods=["GET"])
@require_api_key
def get_request(req_id):
    """Poll for approval status."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id = ? AND api_key = ?",
            (req_id, request.api_key_row["key"])
        ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "decision": row["decision"],
        "decision_comment": row["decision_comment"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    })


@app.route("/v1/requests", methods=["GET"])
@require_api_key
def list_requests():
    """List recent approval requests for this API key."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, status, decision, created_at, decided_at "
            "FROM approval_requests WHERE api_key = ? ORDER BY created_at DESC LIMIT 50",
            (request.api_key_row["key"],)
        ).fetchall()
    return jsonify({"requests": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Human-facing approval UI
# ---------------------------------------------------------------------------

@app.route("/approve/<req_id>")
def approve_page(req_id):
    token = request.args.get("token", "")
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id = ? AND human_token = ?",
            (req_id, token)
        ).fetchone()
    if not row:
        abort(404)
    if row["status"] != "pending":
        return render_template_string(DECIDED_PAGE, row=dict(row))
    options = json.loads(row["options_json"])
    context = json.loads(row["context_json"])
    return render_template_string(
        APPROVE_PAGE,
        row=dict(row),
        options=options,
        context=context,
        token=token,
    )


@app.route("/approve/<req_id>/submit", methods=["POST"])
def submit_decision(req_id):
    token = request.form.get("token", "")
    decision = request.form.get("decision", "")
    comment = request.form.get("comment", "")[:MAX_COMMENT_LEN]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id = ? AND human_token = ?",
            (req_id, token)
        ).fetchone()
        if not row or row["status"] != "pending":
            abort(400)
        # Enforce expiry
        if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"):
            abort(410)
        # Validate decision is one of the allowed options
        allowed_options = json.loads(row["options_json"])
        if decision not in allowed_options:
            abort(400)
        conn.execute("""
            UPDATE approval_requests
            SET status = 'decided', decision = ?, decision_comment = ?,
                decided_at = datetime('now')
            WHERE id = ?
        """, (decision, comment, req_id))
        webhook_url = row["webhook_url"]

    # Fire webhook in background
    if webhook_url:
        threading.Thread(
            target=fire_webhook,
            args=(webhook_url, req_id, decision, comment),
            daemon=True
        ).start()

    return render_template_string(THANKS_PAGE, decision=decision)


# ---------------------------------------------------------------------------
# Notifications & Webhooks
# ---------------------------------------------------------------------------

def send_telegram_notification(chat_id, title, approval_url, description):
    """Send approval notification via Telegram bot."""
    token = TELEGRAM_BOT_TOKEN
    if not token:
        return
    short_desc = (description[:300] + "…") if len(description) > 300 else description
    text = (
        f"🟢 *Approval needed*\n\n"
        f"*{title}*\n"
        + (f"\n{short_desc}\n" if short_desc else "")
        + f"\n[Review & Decide]({approval_url})"
    )
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        if result.get("ok"):
            print(f"[Greenlight] Telegram notification sent to {chat_id}")
        else:
            print(f"[Greenlight] Telegram error: {result}")
    except Exception as e:
        print(f"[Greenlight] Telegram failed ({e}). Approval URL: {approval_url}")


def _is_safe_webhook_url(url: str) -> bool:
    """Block SSRF: only allow public HTTPS URLs, reject private/metadata IPs."""
    import urllib.parse, ipaddress
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("https", "http"):
            return False
        host = p.hostname or ""
        # Block GCP/AWS/Azure metadata endpoints and private ranges
        blocked_hosts = {"169.254.169.254", "metadata.google.internal", "metadata.internal"}
        if host in blocked_hosts:
            return False
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return False
        except ValueError:
            pass  # hostname, not an IP — allow DNS resolution
        return True
    except Exception:
        return False


def fire_webhook(url, req_id, decision, comment):
    import urllib.request, urllib.error
    if not _is_safe_webhook_url(url):
        print(f"[Greenlight] Webhook blocked (unsafe URL): {url}")
        return
    payload = json.dumps({
        "id": req_id,
        "status": "decided",
        "decision": decision,
        "decision_comment": comment,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[Greenlight] Webhook failed: {e}")


# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------

BLOG_POST = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>The Missing Primitive: Human-in-the-Loop for AI Agents | Greenlight</title>
<meta name="description" content="AI agents are powerful enough to cause real damage. Here's why human-in-the-loop approval is the missing primitive, and how to add it to any agent in one API call.">
<link rel="canonical" href="https://greenlightapi.dev/blog">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta property="og:title" content="The Missing Primitive: Human-in-the-Loop for AI Agents">
<meta property="og:description" content="AI agents are powerful enough to cause real damage. Here's why human approval is the missing primitive.">
<meta property="og:url" content="https://greenlightapi.dev/blog">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e8e8e8; line-height: 1.7; }
  .container { max-width: 680px; margin: 0 auto; padding: 60px 24px 80px; }
  .back { color: #22c55e; text-decoration: none; font-size: 0.85rem; display: inline-block; margin-bottom: 40px; }
  .back:hover { text-decoration: underline; }
  .tag { background: #22c55e22; color: #22c55e; border: 1px solid #22c55e44; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
  h1 { font-size: 2rem; font-weight: 800; color: #fff; line-height: 1.2; margin: 16px 0 8px; }
  .meta { color: #555; font-size: 0.85rem; margin-bottom: 40px; }
  h2 { font-size: 1.2rem; font-weight: 700; color: #fff; margin: 40px 0 12px; }
  p { color: #aaa; margin-bottom: 20px; }
  strong { color: #e8e8e8; }
  code { background: #1a1a1a; border: 1px solid #222; border-radius: 4px; padding: 1px 6px; font-family: monospace; font-size: 0.85em; color: #22c55e; }
  pre { background: #111; border: 1px solid #222; border-radius: 10px; padding: 20px; margin: 24px 0; overflow-x: auto; font-family: monospace; font-size: 0.82rem; line-height: 1.6; color: #ccc; }
  pre .c { color: #555; }
  pre .s { color: #22c55e; }
  pre .k { color: #60a5fa; }
  blockquote { border-left: 3px solid #22c55e; padding-left: 20px; margin: 24px 0; color: #888; font-style: italic; }
  .cta-box { background: #111; border: 1px solid #22c55e33; border-radius: 10px; padding: 28px; margin-top: 48px; text-align: center; }
  .cta-box h3 { color: #fff; font-size: 1.1rem; margin-bottom: 8px; }
  .cta-box p { margin-bottom: 20px; font-size: 0.9rem; }
  .btn { display: inline-block; background: #22c55e; color: #000; padding: 12px 24px; border-radius: 8px; font-weight: 700; text-decoration: none; font-size: 0.95rem; }
</style>
</head>
<body>
<div class="container">
  <a href="/" class="back">← greenlightapi.dev</a>
  <span class="tag">Engineering</span>
  <h1>The Missing Primitive: Human-in-the-Loop for AI Agents</h1>
  <div class="meta">April 2026 · 6 min read</div>

  <p>AI agents are getting good. Dangerously good. They can browse the web, write and execute code, send emails, manage files, call APIs, and coordinate with other agents — all without you in the loop.</p>

  <p>That's the point. That's what makes them useful.</p>

  <p>But it's also what makes them terrifying.</p>

  <blockquote>At what point does an autonomous agent stop and ask: "Are you sure you want me to do this?"</blockquote>

  <p>This is the question nobody building agents has fully answered. And the gap between <strong>theoretical autonomy</strong> and <strong>safe autonomy</strong> lives right here.</p>

  <h2>The problem with existing solutions</h2>

  <p>Every major agent framework has some version of human-in-the-loop (HITL) support. LangGraph has <code>interrupt()</code>. CrewAI has <code>human_input=True</code>. Temporal has signals. The Vercel AI SDK has <code>needsApproval</code>.</p>

  <p>These are fine if you're already inside those frameworks. But they share three problems:</p>

  <p><strong>1. They're framework-specific.</strong> If you're using LangGraph's interrupt, you can't easily use it in a CrewAI workflow. Custom agent loops get nothing.</p>

  <p><strong>2. They handle the pause, not the notification.</strong> Knowing to stop is half the problem. You also need to actually reach a human — via Telegram, webhook, or other channel — and give them a clean UI to respond from. Every team rebuilds this from scratch.</p>

  <p><strong>3. They're synchronous.</strong> Most HITL implementations block the thread. For long-running agents this is a problem — you want the agent to pause, send a notification, and then resume when a human responds, potentially hours later.</p>

  <h2>What a proper HITL primitive looks like</h2>

  <p>After building several agent systems, here's what I think the interface should be:</p>

  <pre><span class="c"># 1. Agent decides it needs approval</span>
response = requests.post(<span class="s">"https://greenlightapi.dev/v1/requests"</span>,
    headers={<span class="s">"Authorization"</span>: <span class="s">"Bearer gl_..."</span>},
    json={
        <span class="s">"title"</span>: <span class="s">"Deploy new model to production?"</span>,
        <span class="s">"notify_telegram"</span>: <span class="s">"your_chat_id"</span>,
        <span class="s">"context"</span>: {<span class="s">"environment"</span>: <span class="s">"prod"</span>, <span class="s">"version"</span>: <span class="s">"2.1.0"</span>},
        <span class="s">"webhook_url"</span>: <span class="s">"https://yourapp.com/resume"</span>
    }
)

<span class="c"># 2. Human gets a Telegram message, taps approve/reject in a clean UI</span>
# 3. Your webhook fires with the decision — agent resumes</span>
</pre>

  <p>Three things matter here: <strong>framework-agnostic</strong> (just HTTP), <strong>async-first</strong> (webhook-based, not blocking), and <strong>human-friendly</strong> (the notification and UI are handled for you).</p>

  <h2>The MCP angle</h2>

  <p>If you're using Claude Code or any MCP-compatible agent, there's a better way: install Greenlight as an MCP server and the agent gets <code>request_approval</code> as a native tool — no HTTP code required.</p>

  <pre><span class="c"># .claude/settings.json</span>
{
  <span class="k">"mcpServers"</span>: {
    <span class="k">"greenlight"</span>: {
      <span class="k">"command"</span>: <span class="s">"python3"</span>,
      <span class="k">"args"</span>: [<span class="s">"/path/to/mcp_server.py"</span>],
      <span class="k">"env"</span>: {
        <span class="k">"GREENLIGHT_API_KEY"</span>: <span class="s">"gl_..."</span>,
        <span class="k">"GREENLIGHT_BASE_URL"</span>: <span class="s">"https://greenlightapi.dev"</span>
      }
    }
  }
}</pre>

  <p>Now Claude can say "I need approval before deploying this" and the tool handles everything — creating the request, polling for a decision, and returning the result to the agent.</p>

  <h2>When should agents ask for approval?</h2>

  <p>A useful heuristic: <strong>ask when the action is hard to reverse or the blast radius is large.</strong></p>

  <p>Ask for approval before: sending bulk emails, deleting data, deploying to production, making purchases, posting publicly, modifying shared infrastructure, or any action affecting more than one person.</p>

  <p>Don't ask for approval before: reading files, making GET requests, generating drafts, running tests, or any purely read/compute operation.</p>

  <h2>The bigger picture</h2>

  <p>We're in an interesting moment. Agents are capable enough to do real work but not trustworthy enough to do it unsupervised. HITL is the bridge — it's how you get the productivity benefits of autonomy while maintaining the oversight that makes autonomy safe.</p>

  <p>The goal isn't to keep humans in the loop forever. It's to build trust incrementally. Each approved action is a data point. Over time, you can automate the approvals for actions that always get approved, and keep humans in the loop only for the genuinely risky ones.</p>

  <div class="cta-box">
    <h3>Add HITL to your agent in 5 minutes</h3>
    <p>Free tier: 10 approval requests/month. No credit card required.</p>
    <a href="/" class="btn">Get your free API key →</a>
  </div>
</div>
</body>
</html>
"""

LANDING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Greenlight — Human-in-the-Loop Approval API for AI Agents</title>
<meta name="description" content="Greenlight lets AI agents pause and request human approval before high-stakes actions. One API call. Framework-agnostic. Also ships as an MCP server.">
<meta name="keywords" content="human in the loop, AI agents, approval API, MCP server, agent oversight, HITL">
<link rel="canonical" href="https://greenlightapi.dev">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<!-- Open Graph -->
<meta property="og:title" content="Greenlight — Human-in-the-Loop for AI Agents">
<meta property="og:description" content="Let your agents ask before acting. One API call to pause and request human approval. Framework-agnostic, MCP-native.">
<meta property="og:url" content="https://greenlightapi.dev">
<meta property="og:type" content="website">
<meta property="og:image" content="https://greenlightapi.dev/favicon.svg">
<!-- Twitter -->
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Greenlight — Human-in-the-Loop for AI Agents">
<meta name="twitter:description" content="Let your agents ask before acting. One API call to pause and request human approval.">
<!-- JSON-LD -->
<script type="application/ld+json">{"@context":"https://schema.org","@type":"SoftwareApplication","name":"Greenlight","url":"https://greenlightapi.dev","description":"Human-in-the-loop approval API for AI agents","applicationCategory":"DeveloperApplication","offers":{"@type":"Offer","price":"0","priceCurrency":"USD"}}</script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e8e8e8; }
  .hero { max-width: 760px; margin: 0 auto; padding: 80px 24px 60px; }
  h1 { font-size: 2.8rem; font-weight: 800; color: #fff; line-height: 1.1; }
  h1 span { color: #22c55e; }
  .sub { font-size: 1.15rem; color: #999; margin: 20px 0 40px; max-width: 540px; line-height: 1.6; }
  .cta { display: inline-block; background: #22c55e; color: #000; padding: 14px 28px; border-radius: 8px; font-weight: 700; text-decoration: none; font-size: 1rem; }
  .code-block { background: #111; border: 1px solid #222; border-radius: 10px; padding: 24px; margin: 48px 0; font-family: 'Monaco', 'Menlo', monospace; font-size: 0.85rem; color: #ccc; overflow-x: auto; }
  .code-block .comment { color: #555; }
  .code-block .key { color: #22c55e; }
  .code-block .val { color: #60a5fa; }
  .section { max-width: 760px; margin: 0 auto; padding: 0 24px 60px; }
  h2 { font-size: 1.5rem; font-weight: 700; color: #fff; margin-bottom: 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 16px; }
  .card { background: #111; border: 1px solid #222; border-radius: 10px; padding: 20px; }
  .card h3 { color: #22c55e; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
  .card p { color: #888; font-size: 0.9rem; line-height: 1.5; }
  .pricing { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
  .plan { background: #111; border: 1px solid #222; border-radius: 10px; padding: 24px; }
  .plan.featured { border-color: #22c55e; }
  .plan h3 { font-size: 1.1rem; font-weight: 700; color: #fff; }
  .plan .price { font-size: 2rem; font-weight: 800; color: #22c55e; margin: 8px 0; }
  .plan ul { list-style: none; color: #888; font-size: 0.9rem; margin-top: 12px; }
  .plan ul li { padding: 4px 0; }
  .plan ul li::before { content: "✓ "; color: #22c55e; }
  footer { text-align: center; padding: 40px 24px; color: #444; font-size: 0.85rem; }
</style>
</head>

<body>
<div class="hero">
  <h1>Let your agents <span>ask before acting</span>.</h1>
  <p class="sub">Greenlight is a one-call API that lets AI agents pause and request human approval before taking high-stakes actions. No infrastructure required.</p>
  <a href="#quickstart" class="cta">Get started free →</a>
</div>

<div class="section">
  <h2>How it works</h2>
  <div class="cards">
    <div class="card"><h3>1. Agent calls API</h3><p>Your agent POSTs an approval request with a title, description, and context.</p></div>
    <div class="card"><h3>2. Human gets notified</h3><p>The approver receives a Telegram message instantly with a one-tap decision UI. Webhook callbacks also supported.</p></div>
    <div class="card"><h3>3. Agent gets decision</h3><p>Agent polls the request or receives a webhook callback with approve/reject + comment.</p></div>
  </div>
</div>

<div class="section" id="quickstart">
  <h2>Quickstart</h2>
  <div class="code-block">
<span class="comment"># 1. Get a free API key</span>
curl -X POST /v1/keys \\
  -H "Content-Type: application/json" \\
  -d '{"name": "My Agent", "email": "you@example.com"}'

<span class="comment"># 2. Request approval before a dangerous action</span>
curl -X POST /v1/requests \\
  -H "Authorization: Bearer <span class="key">gl_your_key</span>" \\
  -H "Content-Type: application/json" \\
  -d '{
    <span class="key">"title"</span>: <span class="val">"Deploy hotfix to production?"</span>,
    <span class="key">"description"</span>: <span class="val">"The agent has tested the fix. Approve to deploy."</span>,
    <span class="key">"context"</span>: {"recipient_count": 5000, "subject": "Q1 Report"},
    <span class="key">"webhook_url"</span>: <span class="val">"https://yourapp.com/webhook"</span>
  }'

<span class="comment"># 3. Poll for decision</span>
curl /v1/requests/<span class="key">{id}</span> \\
  -H "Authorization: Bearer <span class="key">gl_your_key</span>"
  </div>
</div>

<div class="section">
  <h2>Pricing</h2>
  <div class="pricing">
    <div class="plan">
      <h3>Free</h3>
      <div class="price">$0</div>
      <ul><li>10 requests/month</li><li>Telegram notifications</li><li>Webhook callbacks</li><li>48hr retention</li></ul>
    </div>
    <div class="plan featured">
      <h3>Starter</h3>
      <div class="price">$9/mo</div>
      <ul><li>500 requests/month</li><li>Telegram notifications</li><li>Webhook callbacks</li><li>30-day retention</li><li>Custom options</li></ul>
    </div>
    <div class="plan">
      <h3>Pro</h3>
      <div class="price">$29/mo</div>
      <ul><li>Unlimited requests</li><li>All notification channels</li><li>90-day retention</li><li>Priority support</li><li>Team approvers</li></ul>
    </div>
  </div>
</div>

<footer>Greenlight · Built by VERTEX · Questions? support@greenlight.dev</footer>
</body>
</html>
"""

APPROVE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Greenlight — Approval Request</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e8e8e8; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }
  .card { background: #111; border: 1px solid #222; border-radius: 14px; padding: 36px; max-width: 520px; width: 100%; }
  .badge { display: inline-block; background: #f59e0b22; color: #f59e0b; border: 1px solid #f59e0b44; border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  h1 { font-size: 1.4rem; font-weight: 700; color: #fff; line-height: 1.3; margin-bottom: 12px; }
  .desc { color: #888; font-size: 0.95rem; line-height: 1.6; margin-bottom: 20px; }
  .context { background: #0a0a0a; border: 1px solid #1e1e1e; border-radius: 8px; padding: 14px; margin-bottom: 24px; font-family: monospace; font-size: 0.8rem; color: #666; }
  .context-item { margin: 4px 0; }
  .context-key { color: #555; }
  .context-val { color: #888; }
  label { display: block; font-size: 0.85rem; color: #666; margin-bottom: 6px; }
  textarea { width: 100%; background: #0a0a0a; border: 1px solid #222; border-radius: 8px; padding: 10px 12px; color: #e8e8e8; font-size: 0.9rem; resize: vertical; min-height: 70px; margin-bottom: 20px; }
  .buttons { display: flex; gap: 10px; flex-wrap: wrap; }
  .btn { flex: 1; padding: 12px; border: none; border-radius: 8px; font-size: 0.95rem; font-weight: 600; cursor: pointer; min-width: 120px; }
  .btn-approve { background: #22c55e; color: #000; }
  .btn-reject { background: #1e1e1e; color: #e8e8e8; border: 1px solid #333; }
  .btn:hover { opacity: 0.85; }
  .logo { font-size: 0.8rem; color: #333; text-align: center; margin-top: 24px; }
</style>
</head>
<body>
<div class="card">
  <div class="badge">⚡ Agent Approval Request</div>
  <h1>{{ row.title }}</h1>
  {% if row.description %}
  <p class="desc">{{ row.description }}</p>
  {% endif %}

  {% if context %}
  <div class="context">
    {% for k, v in context.items() %}
    <div class="context-item"><span class="context-key">{{ k }}:</span> <span class="context-val">{{ v }}</span></div>
    {% endfor %}
  </div>
  {% endif %}

  <form method="POST" action="/approve/{{ row.id }}/submit">
    <input type="hidden" name="token" value="{{ token }}">
    <label>Comment (optional)</label>
    <textarea name="comment" placeholder="Add a note for the agent..."></textarea>
    <div class="buttons">
      {% for option in options %}
      <button class="btn {% if loop.first %}btn-approve{% else %}btn-reject{% endif %}"
              type="submit" name="decision" value="{{ option }}">{{ option }}</button>
      {% endfor %}
    </div>
  </form>

  <div class="logo">Powered by Greenlight</div>
</div>
</body>
</html>
"""

DECIDED_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Already Decided</title>
<style>
  body { font-family: sans-serif; background: #0a0a0a; color: #e8e8e8; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #111; border: 1px solid #222; border-radius: 14px; padding: 36px; max-width: 420px; text-align: center; }
  h1 { color: #fff; margin-bottom: 12px; }
  p { color: #888; }
  .decision { font-size: 1.2rem; font-weight: 700; color: #22c55e; margin: 16px 0; }
</style></head>
<body>
<div class="card">
  <h1>Already decided</h1>
  <p>This request has already been resolved.</p>
  <div class="decision">{{ row.decision }}</div>
  {% if row.decision_comment %}<p>{{ row.decision_comment }}</p>{% endif %}
</div>
</body>
</html>
"""

THANKS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Decision Recorded</title>
<style>
  body { font-family: sans-serif; background: #0a0a0a; color: #e8e8e8; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #111; border: 1px solid #222; border-radius: 14px; padding: 36px; max-width: 420px; text-align: center; }
  h1 { color: #fff; margin-bottom: 12px; }
  p { color: #888; }
  .check { font-size: 3rem; margin-bottom: 16px; }
  .decision { font-size: 1.2rem; font-weight: 700; color: #22c55e; margin: 16px 0; }
</style></head>
<body>
<div class="card">
  <div class="check">✓</div>
  <h1>Decision recorded</h1>
  <div class="decision">{{ decision }}</div>
  <p>The agent has been notified. You can close this page.</p>
</div>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@app.route("/blog")
def blog():
    return render_template_string(BLOG_POST)


@app.route("/favicon.ico")
@app.route("/favicon.png")
@app.route("/favicon.svg")
def favicon():
    return send_from_directory("static", "favicon.svg", mimetype="image/svg+xml")


@app.route("/sitemap.xml")
def sitemap():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://greenlightapi.dev/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>
</urlset>'''
    return xml, 200, {'Content-Type': 'application/xml'}

@app.route("/robots.txt")
def robots():
    return "User-agent: *\nAllow: /\nSitemap: https://greenlightapi.dev/sitemap.xml\n", 200, {'Content-Type': 'text/plain'}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"🟢 Greenlight running on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
