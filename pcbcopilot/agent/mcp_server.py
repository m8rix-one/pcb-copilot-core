"""Model Context Protocol server (stdio, JSON-RPC 2.0, newline-delimited): the design tools bound to one project.

    python -m pcbcopilot.agent.mcp_server --project <id>

Works with Claude Code, Claude Desktop, Cursor and any other MCP client.
"""
from .. import __version__
import argparse
import json
import sys
import traceback

from . import tools

PROTOCOL = "2025-06-18"


def handle(msg, pid):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pcb-copilot-core", "version": __version__},
                "instructions": "PCB Copilot Core: design a circuit board on KiCad 10 with real JLCPCB/LCSC parts."}
    if method == "tools/list":
        return {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                          for t in tools.TOOLS.values()]}
    if method == "tools/call":
        p = msg.get("params", {})
        try:
            out = tools.call(pid, p.get("name"), p.get("arguments") or {})
        except Exception as e:                                  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": str(out)}], "isError": str(out).startswith("ERROR")}
    if method == "ping":
        return {}
    if mid is not None:
        raise KeyError(method)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        try:
            res = handle(msg, a.project)
            if mid is None:
                continue
            out = {"jsonrpc": "2.0", "id": mid, "result": res}
        except KeyError as e:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {e}"}}
        except Exception as e:                                  # noqa: BLE001
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
