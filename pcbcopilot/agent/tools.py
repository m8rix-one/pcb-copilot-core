"""The tools the design agent calls (exposed over MCP and to the Anthropic API).

The model never writes a KiCad file. It adds parts and wires nets through these functions, and every edit is
checked against the real symbol by pcbcopilot.ir.Design: a pin that does not exist is an error the model sees.
"""
import logging

from .. import build, catalog, datasheet, ir, library, projects

logging.getLogger("pypdf").setLevel(logging.ERROR)

TOOLS = {}
S = {"type": "string"}
N = {"type": "number"}
PINS = {"type": "array", "items": S, "description": "Pins as REF.PIN, PIN = number or name, e.g. U1.VIN or U1.3"}


def tool(name, description, props, required=()):
    def deco(fn):
        TOOLS[name] = {"name": name, "description": description, "fn": fn,
                       "inputSchema": {"type": "object", "properties": props, "required": list(required),
                                       "additionalProperties": False}}
        return fn
    return deco


def _row(r):
    sp = ", ".join(f"{k}={v}" for k, v in list((r.get("specs") or {}).items())[:8])
    tag = "Basic" if r.get("basic") else ("Preferred" if r.get("preferred") else "Extended")
    price = f"${float(r['price']):.4f}" if r.get("price") is not None else "?"
    stock = f"{r['stock']:,}" if isinstance(r.get("stock"), int) else "?"
    return f"{r['lcsc']} {r['mpn']} [{r.get('package')}] {tag} stock={stock} {price} | " \
           f"{(r.get('description') or '')[:110]} | {sp}"


@tool("search_parts", "Search the JLCPCB/LCSC catalog. Use `category` for a parametric search sorted by stock "
      "(categories: " + ", ".join(catalog.CATEGORIES) + "). Filters are category fields, e.g. "
      "{\"output_voltage\": 3.3} for ldos. Plain `query` is a weak keyword search; prefer get_part with an exact MPN.",
      {"query": S, "category": S, "package": S, "filters": {"type": "object"}, "limit": N})
def search_parts(pid, query="", category=None, package=None, filters=None, limit=10):
    rows = catalog.search(query or "", category=category, package=package, limit=int(limit or 10), **(filters or {}))
    return "\n".join(_row(r) for r in rows) or "No in-stock matches."


@tool("get_part", "Look up one part by exact manufacturer part number or LCSC number (e.g. 'AP2112K-3.3TRG1' or "
      "'C51118'). Generates its KiCad symbol, footprint and 3D model and returns the pin table you must use for "
      "connections, plus stock, price and datasheet.", {"part": S}, ["part"])
def get_part(pid, part):
    row = catalog.lookup(part)
    if not row:
        return f"'{part}' is not in the JLCPCB/LCSC catalog. Try search_parts or another MPN."
    meta = library.get(row["lcsc"], row=row)
    pins = "; ".join(f"{p['number']}={p['name']}({p['type']})" for p in meta["pins"])
    return (_row(row) + f"\nKind: {meta['kind']}. Footprint: {meta['footprint']}. Datasheet: {meta['datasheet']}"
            f"\nPins: {pins}")


@tool("read_datasheet", "Read the manufacturer datasheet of a part and get the pages relevant to a question "
      "(e.g. 'typical application circuit', 'pin description', 'absolute maximum ratings'), each cited by page.",
      {"part": S, "question": S}, ["part", "question"])
def read_datasheet(pid, part, question):
    row = catalog.lookup(part)
    if not row:
        return f"unknown part {part}"
    return datasheet.lookup(row["lcsc"], question)


@tool("add_part", "Add an IC, connector, module or discrete (by MPN or LCSC number). Returns its ref and pins. "
      "For resistors, capacitors and LEDs use add_passive.",
      {"part": S, "purpose": S, "ref": S, "value": S}, ["part", "purpose"])
def add_part(pid, part, purpose, ref=None, value=None):
    row = catalog.lookup(part)
    if not row:
        return f"'{part}' is not in the JLCPCB/LCSC catalog."
    with projects.editing(pid) as D:
        r, meta = D.add(row["lcsc"], ref=ref, value=value, purpose=purpose, row=row)
    pins = "; ".join(f"{p['number']}={p['name']}" for p in meta["pins"])
    return f"Added {r} = {meta['mpn']} ({row['lcsc']}, {meta['package']}). Pins: {pins}"


@tool("add_passive", "Add a resistor, capacitor or LED: the best in-stock JLCPCB part (Basic library preferred). "
      "value like '10k', '4.7k', '100nF', '10uF'; for LEDs give color.",
      {"kind": {"type": "string", "enum": ["resistor", "capacitor", "led"]}, "value": S,
       "package": {"type": "string", "description": "0402, 0603 (default), 0805, 1206"}, "purpose": S,
       "min_voltage": {"type": "number", "description": "capacitor voltage rating needed"}, "color": S, "ref": S,
       "count": {"type": "number", "description": "add several identical parts (e.g. 4 decoupling caps)"}},
      ["kind", "purpose"])
def add_passive(pid, kind, purpose, value=None, package="0603", min_voltage=None, color=None, ref=None, count=1):
    pkg = package or "0603"
    row, note = catalog.pick_passive(kind, value, pkg, voltage=min_voltage, color=color)
    if not row and pkg == "0402":
        row, note = catalog.pick_passive(kind, value, "0603", voltage=min_voltage, color=color)
    if not row:
        return f"No part found: {note}. Try another package or value."
    refs = []
    with projects.editing(pid) as D:
        for i in range(int(count or 1)):
            r, meta = D.add(row["lcsc"], ref=ref if i == 0 else None,
                            value=value if kind != "led" else (color or "LED"), purpose=purpose, row=row)
            refs.append(r)
    pins = " ".join(f"{p['number']}={p['name'] or p['number']}" for p in meta["pins"])
    return f"Added {', '.join(refs)}: {row['mpn']} {row['lcsc']} ({note}). Pins: {pins}"


@tool("connect", "Create or extend nets. Each entry joins the listed pins to one net. Name ground GND and give "
      "supply nets a voltage. Pin names are resolved against the real symbol (U1.VIN; U1.GND matches every GND pin).",
      {"connections": {"type": "array", "items": {"type": "object", "properties": {
          "net": S, "pins": PINS, "voltage": N}, "required": ["net", "pins"]}}}, ["connections"])
def connect(pid, connections):
    done, errors = [], []
    with projects.editing(pid) as D:
        for c in connections:
            try:
                pins = D.connect(c["net"], c["pins"], voltage=c.get("voltage"))
                done.append(f"{c['net']}: +{' '.join(pins)}")
            except ir.IRError as e:
                errors.append(f"{c['net']}: {e}")
    return "\n".join(done + [f"ERROR {e}" for e in errors]) or "nothing to do"


@tool("disconnect", "Remove pins from whatever net they are on.", {"pins": PINS}, ["pins"])
def disconnect(pid, pins):
    with projects.editing(pid) as D:
        out = D.disconnect(pins)
    return f"Disconnected {' '.join(out)}" if out else "Those pins were not connected."


@tool("mark_no_connect", "Mark pins as intentionally unconnected (no-connect flags).", {"pins": PINS}, ["pins"])
def mark_no_connect(pid, pins):
    with projects.editing(pid) as D:
        D.mark_nc(pins)
    return "ok"


@tool("remove_part", "Remove a part and its connections.", {"ref": S}, ["ref"])
def remove_part(pid, ref):
    with projects.editing(pid) as D:
        D.remove(ref)
    return f"Removed {ref}"


@tool("replace_part", "Swap the part behind a ref (e.g. a different LDO), keeping connections by pin name.",
      {"ref": S, "part": S, "value": S}, ["ref", "part"])
def replace_part(pid, ref, part, value=None):
    row = catalog.lookup(part)
    if not row:
        return f"unknown part {part}"
    library.get(row["lcsc"], row=row)
    with projects.editing(pid) as D:
        moved, dropped = D.replace_part(ref, row["lcsc"], value=value)
    return f"{ref} is now {row['mpn']}. Kept {moved} connections." + (
        f" Could not map: {', '.join(dropped)} (reconnect them)." if dropped else "")


@tool("set_value", "Change the value of a resistor or capacitor (picks a new matching part).",
      {"ref": S, "value": S}, ["ref", "value"])
def set_value(pid, ref, value):
    d = projects.load(pid)
    c = next((c for c in d["components"] if c["ref"] == ref), None)
    if not c:
        return f"no {ref}"
    if c["kind"] not in ("resistor", "capacitor"):
        with projects.editing(pid) as D:
            D.comp(ref)["value"] = value
        return f"{ref} value set to {value}"
    row, note = catalog.pick_passive(c["kind"], value, c["package"])
    if not row:
        return note
    with projects.editing(pid) as D:
        D.replace_part(ref, row["lcsc"], value=value)
    return f"{ref} = {value} ({row['lcsc']} {row['mpn']}, {note})"


@tool("set_board", "Board size in mm (omit for automatic), corner radius, and which board edge each connector "
      "sits on ({\"J1\": \"left\"}).",
      {"width": N, "height": N, "corner_radius": N, "edges": {"type": "object"}})
def set_board(pid, width=None, height=None, corner_radius=None, edges=None):
    with projects.editing(pid) as D:
        b = D.d.setdefault("board", {})
        if width:
            b["width"] = float(width)
        if height:
            b["height"] = float(height)
        if corner_radius is not None:
            b["corner_radius"] = float(corner_radius)
        if edges:
            for ref, side in edges.items():
                D.comp(ref)
                if side not in ("left", "right", "top", "bottom"):
                    raise ir.IRError(f"edge must be left, right, top or bottom, not {side!r}")
            b.setdefault("edges", {}).update(edges)
    return "ok"


@tool("set_info", "Record the design's name, a one-paragraph summary, and engineering notes (calculations, "
      "assumptions, decisions) for the engineer.",
      {"name": S, "summary": S, "notes": {"type": "array", "items": S}})
def set_info(pid, name=None, summary=None, notes=None):
    with projects.editing(pid) as D:
        if name:
            D.d["name"] = name
        if summary:
            D.d["summary"] = summary
        if notes:
            D.d["notes"] = list(notes)
    return "ok"


@tool("get_design", "The current design: every part with its pin-by-pin connections, and every net.", {})
def get_design(pid):
    return ir.Design(projects.load(pid)).summary()


@tool("build_board", "Compile the design to a KiCad project: schematic, KiCad ERC, placement, board, Freerouting "
      "autorouter, ground pours, KiCad DRC. Returns the ERC/DRC results. Call it when the circuit is complete, and "
      "fix what it reports.", {"route": {"type": "boolean", "description": "run the autorouter (default true)"}})
def build_board(pid, route=True):
    d = projects.load(pid)
    rep = build.compile_design(d, projects.path(pid), route=route)
    erc, drc = build.errors(rep, "erc"), build.errors(rep, "drc")
    lines = [f"Built in {rep['seconds']} s: board {rep['placement']['width']:g} × {rep['placement']['height']:g} mm, "
             f"{rep['geometry']['tracks']} tracks, {rep['geometry']['vias']} vias.",
             f"KiCad ERC: {len(erc)} error(s). KiCad DRC: {len(drc)} error(s)."]
    lines += [f"ERC: {v['message']} ({'; '.join(v['items'][:2])})" for v in erc[:15]]
    lines += [f"DRC: {v['message']} ({'; '.join(v['items'][:2])})" for v in drc[:15]]
    return "\n".join(lines)


def call(pid, name, args):
    t = TOOLS.get(name)
    if not t:
        return f"ERROR: unknown tool {name}"
    try:
        return t["fn"](pid, **(args or {}))
    except (ir.IRError, library.PartError, ValueError, build.BuildError) as e:
        return f"ERROR: {e}"
