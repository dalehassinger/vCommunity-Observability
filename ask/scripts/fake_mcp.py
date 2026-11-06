"""A stand-in stdio MCP server, for testing the Ask host agent's MCP bridge and the
MCP tools in Ask without PowerShell or a lab.

    python3 ask/scripts/fake_mcp.py

Speaks newline-delimited JSON-RPC on stdin/stdout like an MCP server (initialize,
notifications/initialized, tools/list, tools/call) and offers three tools: two that only
read (Get-HostUptime, Get-Lab-Hosts) and one that would change something (Send-Email)."""

from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {"name": "Get-HostUptime", "description": "Returns host uptime in seconds and the boot time.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "Get-Lab-Hosts", "description": "Lists the ESXi hosts of the lab with CPU and memory use.",
     "inputSchema": {"type": "object", "properties": {"Cluster": {"type": "string", "description": "Only this cluster"}},
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "Send-Email", "description": "Sends an email.",
     "inputSchema": {"type": "object", "properties": {"To": {"type": "string"}, "Subject": {"type": "string"},
                                                      "Body": {"type": "string"}},
                     "required": ["To", "Subject", "Body"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
]
HOSTS = [{"Name": f"esx9-0{i}.lab.local", "Cluster": "cl01" if i < 4 else "cl02", "CpuPct": 20 + i * 7,
          "MemPct": 41 + i * 5, "VMs": 10 + i} for i in range(1, 6)]
START = time.time()


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def text_result(data):
    s = json.dumps({"data": data})
    return {"content": [{"type": "text", "text": s}], "structuredContent": {"data": data}, "isError": False}


print("fake MCP server starting", file=sys.stderr, flush=True)
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        reply(None, error={"code": -32700, "message": "Parse error"})
        continue
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        continue
    if method == "initialize":
        reply(mid, {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "0.1"}})
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "Get-HostUptime":
            up = int(time.time() - START) + 86400 * 3
            reply(mid, text_result({"UptimeSeconds": up, "BootTime": "2026-11-03T08:00:00Z"}))
        elif name == "Get-Lab-Hosts":
            rows = [h for h in HOSTS if not args.get("Cluster") or h["Cluster"] == args["Cluster"]]
            reply(mid, text_result(rows))
        elif name == "Send-Email":
            if not all(args.get(k) for k in ("To", "Subject", "Body")):
                reply(mid, error={"code": -32602, "message": "Arguments do not match the tool input schema"})
            else:
                reply(mid, text_result({"Status": "Sent (not really: fake MCP)", "To": args["To"]}))
        else:
            reply(mid, error={"code": -32602, "message": "Unknown or missing tool name"})
    elif method == "ping":
        reply(mid, {})
    else:
        reply(mid, error={"code": -32601, "message": "Method not found"})
