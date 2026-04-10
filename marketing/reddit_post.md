# Reddit post (r/LocalLLaMA, r/LangChain, r/ClaudeAI)

**Title:** I built a framework-agnostic human-in-the-loop approval API for agents — free to try

**Body:**

Been building agent workflows for a while and kept copy-pasting the same "pause and ask a human" logic into every project. Built Greenlight to solve it once.

**The problem:** Your agent is about to send an email, delete records, or deploy something. You want a human to approve it first. But you don't want to build a whole approval UI + notification system just for that.

**How it works:**

1. Agent POSTs an approval request (title, context, optional webhook)
2. You get an email with a clean one-click UI  
3. Agent polls for the decision or gets a webhook callback

```python
import requests

resp = requests.post("http://34.24.181.67/v1/requests",
    headers={"Authorization": "Bearer gl_your_key"},
    json={
        "title": "Deploy v2.3.1 to production?",
        "description": "All tests passing. 847 users affected.",
        "context": {"version": "2.3.1", "env": "prod"},
    }
)
req_id = resp.json()["id"]

# Poll until decided
while True:
    status = requests.get(f"http://34.24.181.67/v1/requests/{req_id}",
        headers={"Authorization": "Bearer gl_your_key"}).json()
    if status["status"] == "decided":
        print(status["decision"])  # "Approve" or "Reject"
        break
    time.sleep(5)
```

**Also ships as an MCP server** so Claude Code can call `request_approval` as a native tool.

**Free tier:** 10 requests/month, no credit card.

Try it: http://34.24.181.67

Happy to answer questions about the implementation or the use case. What notification channels would be most useful beyond email? (Slack? SMS? Telegram?)
