# Greenlight — Human-in-the-Loop API for AI Agents

Let your agents ask before acting.

## What it does

Greenlight is a REST API + MCP server that lets AI agents pause and request human approval before taking high-stakes actions (sending emails, deleting data, making purchases, deploying code).

## Quick deploy

```bash
pip install flask
python3 app.py
```

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
  "title": "Send email to 5,000 users?",
  "description": "Agent has drafted the Q1 report email.",
  "context": {"recipient_count": 5000},
  "webhook_url": "https://yourapp.com/webhook"  // optional
}
→ {"id": "abc123", "approval_url": "https://greenlight.dev/approve/abc123?token=..."}
```

### Poll for decision
```
GET /v1/requests/{id}
Authorization: Bearer gl_...
→ {"status": "decided", "decision": "Approve", "decision_comment": "Looks good"}
```

## MCP Setup (Claude Code)

Add to `.claude/settings.json`:
```json
{
  "mcpServers": {
    "greenlight": {
      "command": "python3",
      "args": ["/path/to/mcp_server.py"],
      "env": {
        "GREENLIGHT_API_KEY": "gl_your_key_here",
        "GREENLIGHT_BASE_URL": "https://greenlight.dev"
      }
    }
  }
}
```

Then Claude can use `request_approval` as a native tool.

## Pricing

| Plan | Price | Requests/mo |
|------|-------|-------------|
| Free | $0 | 10 |
| Starter | $9/mo | 500 |
| Pro | $29/mo | Unlimited |

## Environment variables (optional)

| Variable | Description |
|----------|-------------|
| `SMTP_HOST` | SMTP server for email notifications |
| `SMTP_PORT` | SMTP port (default: 587) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASS` | SMTP password |
| `FROM_EMAIL` | Sender email address |
