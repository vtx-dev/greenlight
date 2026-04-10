# Show HN: Greenlight – Human-in-the-loop approval API for AI agents

**URL:** http://34.24.181.67

I built this over the weekend because I kept running into the same problem: agents doing things they shouldn't without asking first.

Existing solutions (LangGraph's interrupt(), HumanLayer, Temporal signals) all require you to be inside a specific framework. If you're rolling your own agent loop, or using a different framework, you're copy-pasting email/Slack logic into every project.

Greenlight is framework-agnostic. Any agent, any language, one HTTP call:

```bash
curl -X POST http://34.24.181.67/v1/requests \
  -H "Authorization: Bearer gl_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Send campaign email to 12,000 users?",
    "context": {"list": "q1-leads", "subject": "Spring sale"},
    "webhook_url": "https://yourapp.com/webhook"
  }'
```

Human gets an email with a one-click approve/reject UI. Agent polls or gets a webhook callback.

Also ships as an MCP server — Claude Code and other MCP-compatible agents can call `request_approval` as a native tool without writing any HTTP code.

**Free tier:** 10 requests/month. No card required.

Would love feedback on the pricing model and what notification channels matter most to you (currently: email + webhook. Slack is next).
