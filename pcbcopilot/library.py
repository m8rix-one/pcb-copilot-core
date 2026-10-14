"""Part library: any LCSC part -> KiCad symbol + footprint + 3D model, cached in the user folder.

* ICs, connectors, modules, discretes: generated from the LCSC/EasyEDA library (the same data JLCPCB assembles
  from), converted with easyeda2kicad and upgraded to the current KiCad format with kicad-cli.
* Chip resistors, capacitors and LEDs: KiCad's own IPC-7351 footprints and symbols.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

from . import catalog, kicad_env as env, paths
from .sexpr import Q, dumps, find, find_all, loads, prop

ROOT = os.path.join(paths.HOME, "parts")
_lock = threading.Lock()

METRIC = {"0201": "0603Metric", "0402": "1005Metric", "0603": "1608Metric", "0805": "2012Metric",
          "1206": "3216Metric", "1210": "3225Metric"}
STD_SYMBOL = {"resistor": "Device:R", "capacitor": "Device:C", "led": "Device:LED"}
STD_FP = {"resistor": "Resistor_SMD:R_{p}_{m}", "capacitor": "Capacitor_SMD:C_{p}_{m}", "led": "LED_SMD:LED_{p}_{m}"}


class PartError(Exception):
    pass


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", s)[:60]


def kind_of(row):
    """resistor / capacitor / led / other, from catalog data."""
    sub = (row.get("subcategory") or "").lower() + " " + (row.get("description") or "").lower()
    pkg = (row.get("package") or "").upper()
    if pkg in METRIC:
        if "chip resistor" in sub or "resistance" in row.get("specs", {}):
            return "resistor"
        if "capacitor" in sub or "capacitance_farads" in row.get("specs", {}):
            return "capacitor"
        if "light emitting" in sub or "led" in sub.split() or "wavelength_nm" in row.get("specs", {}):
            return "led"
    return "other"


def get(lcsc, row=None):
    """Part metadata (pins, symbol, footprint, datasheet), generating the library assets on first use."""
    lcsc = catalog.lcsc_id(lcsc)
    d = os.path.join(ROOT, lcsc)
    meta_fp = os.path.join(d, "meta.json")
    if os.path.exists(meta_fp):
        with open(meta_fp) as f:
            return json.load(f)
    with _lock:
        if os.path.exists(meta_fp):
            with open(meta_fp) as f:
                return json.load(f)
        row = row or catalog.lookup(lcsc)
        if not row:
            raise PartError(f"{lcsc} is not in the JLCPCB/LCSC catalog")
        kind = kind_of(row)
        meta = _std_passive(lcsc, row, kind) if kind != "other" else _easyeda(lcsc, row, d)
        os.makedirs(d, exist_ok=True)
        with open(meta_fp, "w") as f:
            json.dump(meta, f, indent=1)
        return meta


def _std_passive(lcsc, row, kind):
    pkg = row["package"].upper()
    sym = STD_SYMBOL[kind]
    return {"lcsc": lcsc, "mpn": row["mpn"], "package": pkg, "kind": kind, "description": row["description"],
            "datasheet": f"https://www.lcsc.com/datasheet/{lcsc}.pdf", "symbol": sym,
            "footprint": STD_FP[kind].format(p=pkg, m=METRIC[pkg]), "source": "kicad",
            "pins": symbol_pins(env.std_symbol(sym)), "basic": row["basic"], "specs": row.get("specs", {})}


def _easyeda_exe():
    return shutil.which("easyeda2kicad") or os.path.join(os.path.dirname(sys.executable),
                                                         "easyeda2kicad.exe" if sys.platform == "win32" else "easyeda2kicad")


def _easyeda(lcsc, row, d):
    tmp = os.path.join(d, "gen")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    sym_fp = os.path.join(tmp, "lib.kicad_sym")
    r = subprocess.run([_easyeda_exe(), "--full", f"--lcsc_id={lcsc}", "--output", os.path.join(tmp, "lib"),
                        "--overwrite"], capture_output=True, text=True, timeout=180, cwd=tmp)
    if not os.path.exists(sym_fp):
        raise PartError(f"could not generate a library part for {lcsc}: {(r.stdout + r.stderr)[-400:]}")
    env.cli("sym", "upgrade", "--force", sym_fp, check=False)          # bring it up to the current KiCad format
    lib = loads(open(sym_fp, encoding="utf-8").read())
    syms = find_all(lib, "symbol")
    if not syms:
        raise PartError(f"{lcsc}: generated symbol library is empty")
    sym = syms[0]
    _infer_pin_types(sym, row)
    name = safe_name(f"{row['mpn']}_{lcsc}")
    sym[1] = Q(name)
    for u in find_all(sym, "symbol"):                                   # units: NAME_1_1 ...
        m = re.match(r".*_(\d+_\d+)$", str(u[1]))
        if m:
            u[1] = Q(f"{name}_{m.group(1)}")
    pretty = os.path.join(tmp, "lib.pretty")
    fps = os.listdir(pretty) if os.path.isdir(pretty) else []
    if not fps:
        raise PartError(f"{lcsc}: no footprint in the LCSC library")
    fp_name = safe_name(f"{os.path.splitext(fps[0])[0]}_{lcsc}")
    os.makedirs(os.path.join(d, "fp"), exist_ok=True)
    fp_path = os.path.join(d, "fp", fp_name + ".kicad_mod")
    shutil.copy(os.path.join(pretty, fps[0]), fp_path)
    model = None
    shapes = os.path.join(tmp, "lib.3dshapes")
    if os.path.isdir(shapes):
        os.makedirs(os.path.join(d, "3d"), exist_ok=True)
        for f in os.listdir(shapes):
            shutil.copy(os.path.join(shapes, f), os.path.join(d, "3d", f))
        model = next((f for f in os.listdir(shapes) if f.endswith(".wrl")), None) or \
            next((f for f in os.listdir(shapes) if f.endswith(".step")), None)
    _fix_footprint(fp_path, fp_name, model)
    with open(os.path.join(d, "symbol.kicad_sym"), "w", encoding="utf-8") as f:
        f.write(dumps(["kicad_symbol_lib", ["version", 20231120], ["generator", Q("pcb_copilot_core")], sym]) + "\n")
    shutil.rmtree(tmp, ignore_errors=True)
    pins = symbol_pins(sym)
    if not pins:
        raise PartError(f"{lcsc}: symbol has no pins")
    return {"lcsc": lcsc, "mpn": row["mpn"], "package": row["package"], "kind": "other",
            "description": row["description"],
            "datasheet": str(prop(sym, "Datasheet", f"https://www.lcsc.com/datasheet/{lcsc}.pdf")),
            "symbol": f"pcbcopilot:{name}", "footprint": f"pcbcopilot:{fp_name}", "source": "lcsc",
            "pins": pins, "basic": row["basic"], "specs": row.get("specs", {}), "model3d": model,
            "category": row.get("category"), "subcategory": row.get("subcategory")}


PWR_IN = re.compile(r"^(\d+V\d*|\d+\.\d+V|GND|AGND|DGND|PGND|GNDA|VSS|VSSA|EP|EPAD|PAD|THERMAL|TAB|V?CC\w*|V?DD\w*|"
                    r"AVDD\w*|DVDD\w*|IOVDD|VIN|VBAT|VBUS|VSYS|VCCIO|AVCC|DVCC|VREG_?VIN|USB_?VDD|V3|VDDIO\w*)$", re.I)
PWR_OUT = re.compile(r"^(VOUT\w*|VO|OUT|VREG_?VOUT|LDO_?OUT|SW|VREF)$", re.I)
NO_CONN = re.compile(r"^(NC|N/?C|DNC|NC\d+)$", re.I)


def _infer_pin_types(sym, row):
    """LCSC symbols mark most pins 'unspecified'; give KiCad's ERC real electrical types."""
    t = ((row.get("category") or "") + " " + (row.get("subcategory") or "")).lower()
    connector = any(k in t for k in ("connector", "header", "terminal", "socket", "switch"))
    regulator = any(k in t for k in ("regulator", "ldo", "dc-dc", "converter", "power management"))
    seen_out = set()
    for u in find_all(sym, "symbol") or [sym]:
        for p in find_all(u, "pin"):
            name = str((find(p, "name") or ["", ""])[1]).strip()
            if regulator and PWR_OUT.match(name) and name.upper() in seen_out:
                p[1] = "passive"                   # second pin of the same output (e.g. a SOT-223 tab)
                continue
            if regulator and PWR_OUT.match(name):
                seen_out.add(name.upper())
            if connector:
                p[1] = "passive"
            elif NO_CONN.match(name):
                p[1] = "no_connect"
            elif regulator and PWR_OUT.match(name):
                p[1] = "power_out"
            elif PWR_IN.match(name):
                p[1] = "power_in"
            elif p[1] in ("unspecified", "bidirectional"):
                p[1] = "passive"                   # LCSC symbols don't carry reliable I/O direction


def _fix_footprint(path, name, model):
    """Name the footprint after its file and point the 3D model at the project copy."""
    e = loads(open(path, encoding="utf-8").read())
    e[1] = Q(name) if not isinstance(e[1], list) else e[1]
    keep = {}
    for m in find_all(e, "model"):
        for k in ("offset", "scale", "rotate"):
            x = find(m, k)
            if x:
                keep[k] = x
        e.remove(m)
    if model:
        e.append(["model", Q("${PCBCOPILOT_3D}/" + model), keep.get("offset", ["offset", ["xyz", 0, 0, 0]]),
                  keep.get("scale", ["scale", ["xyz", 1, 1, 1]]), keep.get("rotate", ["rotate", ["xyz", 0, 0, 0]])])
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(e) + "\n")


ETYPE = {"input": "input", "output": "output", "bidirectional": "bidir", "tri_state": "tristate",
         "passive": "passive", "free": "free", "unspecified": "unspecified", "power_in": "power_in",
         "power_out": "power_out", "open_collector": "open_collector", "open_emitter": "open_emitter",
         "no_connect": "nc"}


def symbol_pins(sym):
    """[{number, name, type, x, y, rot, length, unit}] from a symbol s-expression (library coordinates, y up)."""
    pins = []
    for u in find_all(sym, "symbol") or [sym]:
        m = re.match(r".*_(\d+)_(\d+)$", str(u[1]))
        unit = int(m.group(1)) if m else 1
        for p in find_all(u, "pin"):
            at, num, nm = find(p, "at"), find(p, "number"), find(p, "name")
            pins.append({"number": str(num[1]) if num else "", "name": str(nm[1]) if nm else "",
                         "type": ETYPE.get(str(p[1]), str(p[1])), "x": at[1], "y": at[2],
                         "rot": at[3] if len(at) > 3 else 0, "length": (find(p, "length") or [0, 2.54])[1],
                         "unit": unit})
    seen, out = set(), []
    for p in pins:
        if p["number"] not in seen:
            seen.add(p["number"])
            out.append(p)
    return out


def symbol_sexpr(meta):
    """The symbol definition to embed in a schematic's lib_symbols."""
    if meta["source"] == "kicad":
        return env.std_symbol(meta["symbol"])
    lib = loads(open(os.path.join(ROOT, meta["lcsc"], "symbol.kicad_sym"), encoding="utf-8").read())
    return find_all(lib, "symbol")[0]


def footprint_file(meta):
    if meta["source"] == "kicad":
        lib, name = meta["footprint"].split(":")
        return os.path.join(env.footprint_dir(), lib + ".pretty", name + ".kicad_mod")
    return os.path.join(ROOT, meta["lcsc"], "fp", meta["footprint"].split(":")[1] + ".kicad_mod")


def model_dir(meta):
    return os.path.join(ROOT, meta["lcsc"], "3d")
