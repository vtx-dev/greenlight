# Tweet thread

**Tweet 1 (hook):**
AI agents are getting powerful enough to cause real damage.

The missing primitive: a way to pause and ask a human before acting.

I built Greenlight — human-in-the-loop as a one-call API. 🧵

**Tweet 2 (problem):**
Every agent framework has its own approval pattern.

LangGraph has interrupt(). CrewAI has human_input. Temporal has signals.

If you're not using those frameworks? You're writing email/Slack logic from scratch. Every. Single. Project.

**Tweet 3 (solution):**
Greenlight is framework-agnostic.

POST a request → human gets notified → agent waits for decision.

Works from Python, JS, Go, curl — anything that can make an HTTP call.

**Tweet 4 (MCP angle):**
Also ships as an MCP server.

Claude Code and other MCP-compatible agents can call request_approval as a native tool — no HTTP code needed.

Agent: "I want to send this email"
Human: [sees clean UI, clicks Approve]
Agent: continues

**Tweet 5 (CTA):**
Free tier: 10 requests/month, no card required.

Try it → http://34.24.181.67
Docs + MCP setup in the README.

What notification channels do you need beyond email? Building the roadmap now.
