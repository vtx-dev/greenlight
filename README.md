# Greenlight — Human-in-the-Loop API for AI Agents

Let your agents ask before acting. Get notified on Telegram. One tap to approve or reject.

**Live at [greenlightapi.dev](https://greenlightapi.dev)**

---

## What it does

Greenlight is a REST API + MCP server that lets AI agents pause and request human approval before taking high-stakes actions (deleting data, making purchases, deploying code, pushing to production).

When an agent submits a request, you get a **Telegram message** with a one-tap approve/reject link. The agent blocks until you decide.

---

## Telegram setup

1. Message **@BotFather** on Telegram → `/newbot` → get your bot token
2. Start a chat with your new bot → send `/start`
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars when running the server

```bash
TELEGRAM_BOT_TOKEN=your_token TELEGRAM_CHAT_ID=your_chat_id python3 app.py
```

Agents pass `notify_telegram` in the request body to override the default chat ID per-request.

---

## API

### Register (free)
```
POST /v1/keys
{"name": "My Agent", "email": "you@example.com"}
→ {"api_key": "gl_..."}
```

### Request approval
```
POST /v1/requests
Authorization: Bearer gl_...
{
  "title": "Deploy hotfix to production?",
  "description": "Agent has tested the fix on staging. Approve to deploy.",
  "notify_telegram": "123456789",   // your Telegram chat ID (optional, uses server default)
  "context": {"environment": "prod", "version": "1.4.2"},
  "webhook_url": "https://yourapp.com/webhook"  // optional, fires on decision
}
→ {
    "id": "abc123",
    "approval_url": "https://greenlightapi.dev/approve/abc123?token=...",
    "poll_url": "https://greenlightapi.dev/v1/requests/abc123"
  }
```

### Poll for decision
```
GET /v1/requests/{id}
Authorization: Bearer gl_...
→ {"status": "decided", "decision": "Approve", "decision_comment": "Looks good"}
```

---

## Python quickstart

```python
import requests, time

API_KEY = "gl_your_key"
BASE    = "https://greenlightapi.dev"

# 1. Submit request
r = requests.post(f"{BASE}/v1/requests",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "title": "Delete 10,000 stale user records?",
        "description": "Identified users inactive >2 years with no purchases.",
        "notify_telegram": "your_chat_id",
    }
)
req_id = r.json()["id"]

# 2. Poll until decided (you can also use webhooks)
while True:
    status = requests.get(f"{BASE}/v1/requests/{req_id}",
        headers={"Authorization": f"Bearer {API_KEY}"}).json()
    if status["status"] == "decided":
        if status["decision"] == "Approve":
            do_the_thing()
        break
    time.sleep(3)
```

---

## MCP setup (Claude Code)

Add to `.claude/settings.json`:
```json
{
  "mcpServers": {
    "greenlight": {
      "command": "python3",
      "args": ["/path/to/mcp_server.py"],
      "env": {
        "GREENLIGHT_API_KEY": "gl_your_key_here",
        "GREENLIGHT_BASE_URL": "https://greenlightapi.dev"
      }
    }
  }
}
```

Then Claude can call `request_approval` as a native tool — it blocks until you approve on Telegram.

---

## Claude Code hook (dogfooding example)

Wire Greenlight into Claude Code's `PreToolUse` hook so it intercepts high-stakes Bash commands automatically:

```json
// ~/.claude/settings.json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/greenlight_hook.py"}]
    }]
  }
}
```

The hook fires on `git push`, `gh pr create`, `rm -rf`, and other destructive commands — pausing until you approve on Telegram.

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (required for notifications) |
| `TELEGRAM_CHAT_ID` | Default Telegram chat ID to notify |
| `BASE_URL` | Public URL of your Greenlight instance |

---

## Pricing

| Plan | Price | Requests/mo |
|------|-------|-------------|
| Free | $0 | 10 |
| Starter | $9/mo | 500 |
| Pro | $29/mo | Unlimited |

---

## Self-host

```bash
git clone https://github.com/vtx-dev/greenlight
cd greenlight
pip install flask
TELEGRAM_BOT_TOKEN=your_token TELEGRAM_CHAT_ID=your_chat_id python3 app.py
```

Runs on SQLite. Put Caddy in front for TLS.
