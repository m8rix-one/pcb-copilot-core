"""Runs the design agent: Claude plus the design tools, in a tool-use loop until the reply is final.

Two ways to reach Claude, same tools either way:
* the `claude` CLI (Claude Code) with the tools served over MCP: no API key needed, uses your Claude login;
* the Anthropic Messages API directly, when ANTHROPIC_API_KEY is set (no SDK needed, plain HTTPS).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from .. import paths, projects
from . import tools

MODEL = os.environ.get("PCB_COPILOT_MODEL", "claude-opus-5-5")
MAX_TURNS = 60
AGENT_DIR = os.path.join(paths.HOME, "agent")

SYSTEM = """You are the PCB Copilot design agent: a senior electronics engineer who designs real, manufacturable
circuit boards with parts in stock at JLCPCB/LCSC, using only the tools you are given. You never write KiCad files
yourself; the tools draw the schematic and the board from what you add and connect.

How you work (make sensible engineering choices and state your assumptions; do not ask questions):
1. Read the request and decide the architecture: functional blocks such as power, MCU, USB, sensor, I/O.
2. Choose proven, widely stocked ICs (prefer JLC Basic/Preferred parts). Verify every IC with get_part using its
   exact MPN before adding it; if it is missing or out of stock, search_parts for an alternative.
3. For every IC, read_datasheet for the typical application circuit and the pin description, then follow it:
   decoupling values, enable/reset pull-ups, feedback dividers, crystal load capacitors, boot pins, thermal pad.
4. Add parts with add_part and add_passive (decoupling <= 1 uF in 0402, general passives 0603, bulk >= 10 uF 0805).
   Give every part a purpose.
5. Connect with `connect`, many nets per call. Ground is GND. Give supply nets their voltage. Name signals
   meaningfully (USB_D+, I2C_SDA, LED_STATUS). Mark deliberately unused IC pins with mark_no_connect.
6. Professional basics: ESD protection on external USB/data connectors, 5.1 kOhm CC pull-downs for USB-C sinks,
   a power LED, a programming/debug header and reset/boot buttons for MCUs. Add nothing else the user did not ask
   for.
7. set_board: put connectors on edges ({"J1": "left"}). Give a size only if the user gave one.
8. set_info with the name, a summary and your notes (calculations with numbers, assumptions, decisions).
9. build_board, read KiCad's ERC and DRC results, fix every error you can with the tools, and build again.
Never invent pin names or numbers: use the pin tables the tools return. When editing an existing design, call
get_design first, change only what was asked, and keep the existing refs.

Finish with a short reply written as bullet points: what you built, the key parts and why, the calculations, and
anything the engineer should double-check in KiCad."""


# ------------------------------------------------------------------ Claude Code CLI + MCP

def claude_bin():
    exe = shutil.which("claude") or next((c for c in (os.path.expanduser("~/.local/bin/claude"),
                                                      os.path.expanduser("~/.claude/local/claude"))
                                          if os.path.exists(c)), None)
    if not exe:
        raise RuntimeError("the `claude` CLI (Claude Code) was not found, and ANTHROPIC_API_KEY is not set")
    return exe


def _file(name, text):
    os.makedirs(AGENT_DIR, exist_ok=True)
    fp = os.path.join(AGENT_DIR, name)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text)
    return fp


def run_cli(pid, message, on_event):
    mcp = {"mcpServers": {"pcb": {"command": sys.executable,
                                  "args": ["-m", "pcbcopilot.agent.mcp_server", "--project", pid],
                                  "cwd": paths.ROOT, "env": {"PYTHONPATH": paths.ROOT, "PYTHONUTF8": "1"}}}}
    cmd = [claude_bin(), "-p", "--output-format", "stream-json", "--verbose", "--model", MODEL,
           "--system-prompt-file", _file("system.md", SYSTEM),
           "--mcp-config", _file(f"mcp-{pid}.json", json.dumps(mcp)), "--strict-mcp-config",
           "--tools", "", "--allowedTools", "mcp__pcb", "--no-session-persistence", "--setting-sources", "user"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", cwd=AGENT_DIR, bufsize=1)
    proc.stdin.write(message)
    proc.stdin.close()
    err = []
    threading.Thread(target=lambda: err.extend(proc.stderr.readlines()), daemon=True).start()
    final, pending = "", {}
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            pcb = next((s for s in ev.get("mcp_servers", []) if s.get("name") == "pcb"), {})
            if pcb.get("status") not in ("connected", "pending"):
                proc.kill()
                raise RuntimeError(f"the pcb tool server did not start (status: {pcb.get('status', 'missing')})")
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block["name"].replace("mcp__pcb__", "")
                    pending[block["id"]] = (name, time.time())
                    on_event({"type": "tool", "name": name, "input": block.get("input", {})})
                elif block.get("type") == "text" and block.get("text", "").strip():
                    on_event({"type": "text", "text": block["text"]})
        elif t == "user":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    name, t0 = pending.pop(block.get("tool_use_id"), ("?", time.time()))
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                    on_event({"type": "tool_result", "name": name, "output": str(content)[:2000],
                              "seconds": round(time.time() - t0, 1)})
        elif t == "result":
            final = ev.get("result") or ""
            on_event({"type": "usage", "cost_usd": ev.get("total_cost_usd"), "turns": ev.get("num_turns"),
                      "seconds": round((ev.get("duration_ms") or 0) / 1000, 1)})
    proc.wait()
    if proc.returncode != 0 and not final:
        raise RuntimeError("design agent failed: " + "".join(err)[-800:])
    return final


# ------------------------------------------------------------------ Anthropic Messages API

def run_api(pid, message, on_event):
    key = os.environ["ANTHROPIC_API_KEY"]
    tool_defs = [{"name": t["name"], "description": t["description"], "input_schema": t["inputSchema"]}
                 for t in tools.TOOLS.values()]
    messages = [{"role": "user", "content": message}]
    t_start, final = time.time(), ""
    for _ in range(MAX_TURNS):
        body = {"model": MODEL, "max_tokens": 8000, "system": SYSTEM, "tools": tool_defs, "messages": messages}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read())
        content = resp.get("content", [])
        messages.append({"role": "assistant", "content": content})
        results = []
        for block in content:
            if block["type"] == "text" and block["text"].strip():
                final = block["text"]
                on_event({"type": "text", "text": block["text"]})
            elif block["type"] == "tool_use":
                on_event({"type": "tool", "name": block["name"], "input": block["input"]})
                t0 = time.time()
                out = str(tools.call(pid, block["name"], block["input"]))
                on_event({"type": "tool_result", "name": block["name"], "output": out[:2000],
                          "seconds": round(time.time() - t0, 1)})
                results.append({"type": "tool_result", "tool_use_id": block["id"], "content": out,
                                "is_error": out.startswith("ERROR")})
        if resp.get("stop_reason") != "tool_use" or not results:
            break
        messages.append({"role": "user", "content": results})
    on_event({"type": "usage", "seconds": round(time.time() - t_start, 1)})
    return final


def run(pid, message, on_event=lambda ev: None):
    """Blocking. on_event(dict) for every step ("text", "tool", "tool_result", "usage"). Returns the final reply."""
    d = projects.load(pid)
    prompt = message if not d["components"] else \
        f"The project already has a design (call get_design first).\n\nRequest: {message}"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return run_api(pid, prompt, on_event)
    return run_cli(pid, prompt, on_event)
