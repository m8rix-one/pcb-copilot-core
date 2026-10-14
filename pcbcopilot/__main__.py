"""Command line.

    python -m pcbcopilot design "USB-C powered ESP32-C3 sensor board with a BME280 and a status LED"
    python -m pcbcopilot chat <project> "add a reset button"
    python -m pcbcopilot build <project> [--no-route]
    python -m pcbcopilot release <project>
    python -m pcbcopilot list
    python -m pcbcopilot mcp <project>        (what Claude Code / Claude Desktop run to get the tools)
"""
import argparse
import json
import sys

from . import build, projects


def _show(ev):
    if ev["type"] == "text":
        print(ev["text"])
    elif ev["type"] == "tool":
        print(f"  > {ev['name']} {json.dumps(ev['input'])[:140]}")
    elif ev["type"] == "tool_result":
        first = ev["output"].splitlines()[0] if ev["output"] else ""
        print(f"    {first[:140]} ({ev['seconds']} s)")
    elif ev["type"] == "usage":
        cost = f", ${ev['cost_usd']:.2f}" if ev.get("cost_usd") is not None else ""
        print(f"  [{ev.get('turns', '?')} turns, {ev['seconds']} s{cost}]")


def _build(pid, route=True):
    rep = build.compile_design(projects.load(pid), projects.path(pid), emit=lambda s, m: print(f"[{s}] {m}"),
                               route=route)
    erc, drc = build.errors(rep, "erc"), build.errors(rep, "drc")
    print(f"Done in {rep['seconds']} s. ERC errors: {len(erc)}. DRC errors: {len(drc)}.")
    for v in erc + drc:
        print(f"  - {v['message']}: {'; '.join(v['items'][:2])}")
    print(f"KiCad project: {projects.path(pid, 'kicad', 'board.kicad_pro')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pcbcopilot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("design", help="create a project and let the agent design it from a brief")
    p.add_argument("brief")
    p.add_argument("--name", default=None)
    p = sub.add_parser("chat", help="ask the agent to change an existing project")
    p.add_argument("project")
    p.add_argument("message")
    p = sub.add_parser("build", help="compile design.json to a KiCad project (no AI)")
    p.add_argument("project")
    p.add_argument("--no-route", action="store_true")
    p = sub.add_parser("release", help="Gerbers, drill, BOM, pick-and-place, STEP")
    p.add_argument("project")
    sub.add_parser("list", help="list projects")
    p = sub.add_parser("mcp", help="serve the design tools over MCP (stdio) for one project")
    p.add_argument("project")
    a = ap.parse_args(argv)

    if a.cmd == "design":
        from .agent import runner
        pid = projects.create(a.name or a.brief[:40], a.brief)
        print(f"Project {pid} ({projects.path(pid)})")
        print(runner.run(pid, a.brief, _show))
        if projects.load(pid)["components"]:
            _build(pid)
    elif a.cmd == "chat":
        from .agent import runner
        print(runner.run(a.project, a.message, _show))
        _build(a.project)
    elif a.cmd == "build":
        _build(a.project, route=not a.no_route)
    elif a.cmd == "release":
        out = build.release(projects.load(a.project), projects.path(a.project), emit=lambda s, m: print(f"[{s}] {m}"))
        print(f"Release files: {out}")
    elif a.cmd == "list":
        for p in projects.list_all():
            print(f"{p['id']:40s} {p['parts']:3d} parts  {p['modified']}  {p['name']}")
    elif a.cmd == "mcp":
        from .agent import mcp_server
        sys.argv = ["mcp_server", "--project", a.project]
        mcp_server.main()


if __name__ == "__main__":
    main()
