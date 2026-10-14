"""Circuit IR -> one flat KiCad schematic (.kicad_sch).

Every symbol sits on a grid, and every connected pin gets a short stub ending in a global label with the net's
name. Connectivity is by name, so the drawing is simple to generate and simple to read; KiCad's ERC checks it.
"""
import copy
import math
import re
import uuid

from . import kicad_env as env, library
from .ir import Design, is_ground
from .sexpr import Q, dumps, find, find_all

G = 1.27             # schematic grid, mm
STUB = 5.08          # stub on a left/right pin
VSTUB = 2.54         # stub on a top/bottom pin
LABEL = 14.0         # room for a label after the stub
TEXT = 4.0           # room for the reference and value text
ROW_W = 360.0        # symbols are packed into rows this wide
MARGIN = 15.0
PAPERS = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]


def snap(v):
    return round(round(v / G) * G, 4)


def uid():
    return Q(str(uuid.uuid4()))


def outward(pin):
    """Unit vector (library coordinates, y up) pointing away from the body at the pin's connection point."""
    a = math.radians(pin["rot"])
    return -round(math.cos(a)), -round(math.sin(a))


def flatten(sym):
    """A symbol that `extends` another gets the parent's drawing units, so it can stand alone in lib_symbols."""
    ext = find(sym, "extends")
    if not ext:
        return sym
    lib = str(sym[1]).split(":")[0]
    parent = env.std_symbol(f"{lib}:{ext[1]}")
    out = copy.deepcopy(sym)
    out.remove(find(out, "extends"))
    pname, cname = str(parent[1]).split(":")[-1], str(sym[1]).split(":")[-1]
    for u in find_all(parent, "symbol"):
        u2 = copy.deepcopy(u)
        u2[1] = Q(str(u2[1]).replace(pname, cname, 1))
        out.append(u2)
    return out


def unit_bbox(sym, unit):
    """Body bounding box (library coordinates) of one unit: its own graphics plus the shared unit 0."""
    xs, ys = [], []

    def walk(e):
        if not isinstance(e, list) or not e:
            return
        if e[0] in ("xy", "start", "end", "center") and len(e) >= 3 and isinstance(e[1], (int, float)):
            xs.append(e[1])
            ys.append(e[2])
        if e[0] == "pin":
            at = find(e, "at")
            xs.append(at[1])
            ys.append(at[2])
            return
        if e[0] == "property":
            return
        for x in e[1:]:
            walk(x)

    for u in find_all(sym, "symbol"):
        m = re.match(r".*_(\d+)_(\d+)$", str(u[1]))
        if m and int(m.group(1)) in (0, unit):
            walk(u)
    if not xs:
        walk(sym)
    if not xs:
        return -2.54, -2.54, 2.54, 2.54
    return min(xs), min(ys), max(xs), max(ys)


def _prop(name, value, x, y, hide=False):
    e = ["property", Q(name), Q(value), ["at", snap(x), snap(y), 0]]
    if hide:
        e.append(["hide", "yes"])
    e.append(["effects", ["font", ["size", 1.27, 1.27]], ["justify", "left"]])
    return e


def build(D: Design, project="board"):
    """Returns (schematic text, {"paper", "paths": ref -> symbol uuid (for the footprint links)})."""
    root = str(uuid.uuid4())
    lib_symbols, items, paths = {}, [], {}
    pmap = D.pin_net()
    nc = set(D.d.get("no_connect") or [])
    cx, cy, row_h, max_x, max_y = 0.0, 0.0, 0.0, 0.0, 0.0

    def global_label(name, x, y, angle):
        just = {0: "left", 180: "right", 90: "left", 270: "right"}[angle]
        items.append(["global_label", Q(name), ["shape", "passive"], ["at", snap(x), snap(y), angle],
                      ["fields_autoplaced", "yes"], ["effects", ["font", ["size", 1.27, 1.27]], ["justify", just]],
                      ["uuid", uid()], ["property", Q("Intersheetrefs"), Q("${INTERSHEET_REFS}"),
                                        ["at", snap(x), snap(y), 0], ["hide", "yes"],
                                        ["effects", ["font", ["size", 1.27, 1.27]]]]])

    for c in D.d["components"]:
        meta = D.meta(c["ref"])
        lib_id = meta["symbol"]
        if lib_id not in lib_symbols:
            s = flatten(copy.deepcopy(library.symbol_sexpr(meta)))
            s[1] = Q(lib_id)
            lib_symbols[lib_id] = s
        sym = lib_symbols[lib_id]
        pins = library.symbol_pins(sym)
        for unit in sorted({p["unit"] for p in pins}) or [1]:
            upins = [p for p in pins if p["unit"] == unit]
            x0, y0, x1, y1 = unit_bbox(sym, unit)
            dirs = {outward(p) for p in upins}
            left = STUB + LABEL if (-1, 0) in dirs else 2.0
            right = STUB + LABEL if (1, 0) in dirs else 2.0
            top = (VSTUB + LABEL if (0, 1) in dirs else 2.0) + TEXT
            bottom = (VSTUB + LABEL if (0, -1) in dirs else 2.0) + TEXT
            w, h = (x1 - x0) + left + right, (y1 - y0) + top + bottom
            if cx + w > ROW_W and cx > 0:                     # next row
                cy += row_h
                cx, row_h = 0.0, 0.0
            X = snap(MARGIN + cx + left - x0)                  # symbol origin on the sheet (y down)
            Y = snap(MARGIN + cy + top + y1)
            cx += w
            row_h = max(row_h, h)
            max_x, max_y = max(max_x, X + x1 + right), max(max_y, Y - y0 + bottom)
            su = uid()
            if unit == min(p["unit"] for p in pins):
                paths[c["ref"]] = str(su)
            e = ["symbol", ["lib_id", Q(lib_id)], ["at", X, Y, 0], ["unit", unit], ["exclude_from_sim", "no"],
                 ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"], ["uuid", su],
                 _prop("Reference", c["ref"], X + x0, Y - y1 - 2.0),
                 _prop("Value", c["value"], X + x0, Y - y0 + 2.5),
                 _prop("Footprint", meta["footprint"], X, Y, hide=True),
                 _prop("Datasheet", meta.get("datasheet") or "", X, Y, hide=True),
                 _prop("LCSC", meta["lcsc"], X, Y, hide=True)]
            for p in upins:
                e.append(["pin", Q(p["number"]), ["uuid", uid()]])
            e.append(["instances", ["project", Q(project),
                                    ["path", Q("/" + root), ["reference", Q(c["ref"])], ["unit", unit]]]])
            items.append(e)
            # what hangs off every pin: stub + global label, or a no-connect flag
            for p in upins:
                px, py = X + p["x"], Y - p["y"]
                ox, oy = outward(p)
                sx, sy = ox, -oy
                key = f"{c['ref']}.{p['number']}"
                net = pmap.get(key)
                if net is None:
                    if key in nc or p["type"] != "power_in":
                        items.append(["no_connect", ["at", snap(px), snap(py)], ["uuid", uid()]])
                    continue
                length = STUB if sx else VSTUB
                ex, ey = px + sx * length, py + sy * length
                items.append(["wire", ["pts", ["xy", snap(px), snap(py)], ["xy", snap(ex), snap(ey)]],
                              ["stroke", ["width", 0], ["type", "default"]], ["uuid", uid()]])
                angle = 0 if sx > 0 else 180 if sx < 0 else 90 if sy < 0 else 270
                global_label(net, ex, ey, angle)
    # KiCad wants every supply net driven by a power-output pin: a header or a battery connector is passive, so
    # those nets get a PWR_FLAG (what a KiCad user adds by hand), in a row under the symbols.
    driven = set()
    for c in D.d["components"]:
        for p in D.meta(c["ref"])["pins"]:
            if p["type"] == "power_out":
                driven.add(pmap.get(f"{c['ref']}.{p['number']}"))
    flags = [n["name"] for n in D.d["nets"] if (is_ground(n["name"]) or n.get("voltage") is not None)
             and n["name"] not in driven]
    if flags:
        s = copy.deepcopy(env.std_symbol("power:PWR_FLAG"))
        s[1] = Q("power:PWR_FLAG")
        lib_symbols["power:PWR_FLAG"] = s
        fy = snap(MARGIN + cy + row_h + 8)
        for k, net in enumerate(flags):
            fx = snap(MARGIN + 4 + k * 24)
            items.append(["symbol", ["lib_id", Q("power:PWR_FLAG")], ["at", fx, fy, 0], ["unit", 1],
                          ["exclude_from_sim", "no"], ["in_bom", "no"], ["on_board", "no"], ["dnp", "no"],
                          ["uuid", uid()], _prop("Reference", f"#FLG{k + 1:02d}", fx, fy - 6, hide=True),
                          _prop("Value", "PWR_FLAG", fx + 2, fy - 4), ["pin", Q("1"), ["uuid", uid()]],
                          ["instances", ["project", Q(project), ["path", Q("/" + root),
                                                                 ["reference", Q(f"#FLG{k + 1:02d}")], ["unit", 1]]]]])
            items.append(["wire", ["pts", ["xy", fx, fy], ["xy", fx, snap(fy + VSTUB)]],
                          ["stroke", ["width", 0], ["type", "default"]], ["uuid", uid()]])
            global_label(net, fx, fy + VSTUB, 270)
            max_y = max(max_y, fy + VSTUB + LABEL)
    paper = next((p for p, pw, ph in PAPERS if max_x + MARGIN <= pw and max_y + MARGIN <= ph), PAPERS[-1][0])
    doc = ["kicad_sch", ["version", 20250114], ["generator", Q("eeschema")], ["generator_version", Q("9.0")],
           ["uuid", Q(root)], ["paper", Q(paper)],
           ["title_block", ["title", Q(D.d.get("name") or project)], ["comment", 1, Q("Generated by PCB Copilot Core")]],
           ["lib_symbols"] + list(lib_symbols.values())] + items + \
          [["sheet_instances", ["path", Q("/"), ["page", Q("1")]]]]
    return dumps(doc) + "\n", {"paper": paper, "paths": paths}
