"""
Greenlight MCP Server
Exposes Greenlight as an MCP tool so AI agents (Claude Code, etc.)
can request human approval natively via the Model Context Protocol.

Usage:
  python3 mcp_server.py --api-key gl_xxx --base-url https://greenlight.dev

Configure in .claude/settings.json:
  {
    "mcpServers": {
      "greenlight": {
        "command": "python3",
        "args": ["/path/to/mcp_server.py"],
        "env": {
          "GREENLIGHT_API_KEY": "gl_xxx",
          "GREENLIGHT_BASE_URL": "https://greenlight.dev"
        }
      }
    }
  }
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Any

# ---------------------------------------------------------------------------
# Minimal MCP server over stdio (JSON-RPC 2.0)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("GREENLIGHT_BASE_URL", "http://localhost:5000")
API_KEY = os.environ.get("GREENLIGHT_API_KEY", "")


def api_call(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    params = msg.get("params") or {}
    req_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "greenlight", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "request_approval",
                        "description": (
                            "Pause execution and request a human to approve or reject "
                            "a proposed action. Use this before any high-stakes, "
                            "irreversible, or potentially harmful action. "
                            "Returns the human's decision ('Approve'/'Reject') and an optional comment."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Short description of the action needing approval (e.g. 'Send email to 5,000 users?')"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Longer explanation of what will happen and why"
                                },
                                "context": {
                                    "type": "object",
                                    "description": "Key-value pairs of relevant data to show the human (e.g. {recipient_count: 5000})"
                                },
                                "options": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Decision options to present (default: ['Approve', 'Reject'])"
                                },
                                "timeout_seconds": {
                                    "type": "integer",
                                    "description": "How long to wait for a decision before returning 'timeout' (default: 300)"
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    {
                        "name": "check_approval",
                        "description": "Check the status of a previously submitted approval request by its ID.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "request_id": {
                                    "type": "string",
                                    "description": "The approval request ID returned by request_approval"
                                }
                            },
                            "required": ["request_id"],
                        },
                    },
                ]
            },
        }

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}

        if tool_name == "request_approval":
            result = api_call("POST", "/v1/requests", {
                "title": args.get("title"),
                "description": args.get("description", ""),
                "context": args.get("context", {}),
                "options": args.get("options", ["Approve", "Reject"]),
                "expires_minutes": max(1, int(args.get("timeout_seconds", 300)) // 60),
            })

            if "error" in result:
                return {
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": f"Error: {result['error']}"}], "isError": True}
                }

            req_id_greenlight = result["id"]
            approval_url = result["approval_url"]
            timeout = int(args.get("timeout_seconds", 300))
            poll_interval = 5
            elapsed = 0

            # Poll until decided or timeout
            while elapsed < timeout:
                time.sleep(poll_interval)
                elapsed += poll_interval
                status = api_call("GET", f"/v1/requests/{req_id_greenlight}")
                if status.get("status") == "decided":
                    decision = status["decision"]
                    comment = status.get("decision_comment") or ""
                    text = f"Decision: {decision}"
                    if comment:
                        text += f"\nHuman comment: {comment}"
                    return {
                        "jsonrpc": "2.0", "id": req_id,
                        "result": {"content": [{"type": "text", "text": text}]}
                    }

            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": f"Timeout: No decision received within {timeout}s.\nRequest ID: {req_id_greenlight}\nApproval URL: {approval_url}\nUse check_approval to poll later."
                    }]
                }
            }

        if tool_name == "check_approval":
            status = api_call("GET", f"/v1/requests/{args['request_id']}")
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(status, indent=2)}]}
            }

    # Unknown method — return null result
    if req_id is not None:
        return {"jsonrpc": "2.0", "id": req_id, "result": None}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(msg)
        if response is not None:
            send(response)


if __name__ == "__main__":
    main()
