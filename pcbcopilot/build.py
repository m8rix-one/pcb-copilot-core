"""Compile a design: libraries -> schematic -> KiCad ERC -> placement -> board -> Freerouting -> pours -> KiCad DRC.

No AI runs here. The same design.json always gives the same KiCad project.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from . import kicad_env as env, paths, placer, project, schematic
from .ir import Design, is_ground

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD_SCRIPT = os.path.join(HERE, "board_script.py")
FREEROUTING_URL = "https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar"
FREEROUTING = os.path.join(paths.HOME, "freerouting-2.5.0.jar")
JAVA_CANDIDATES = ["/opt/homebrew/opt/openjdk/bin/java", "/usr/local/opt/openjdk/bin/java"]

LAYERS = """  (layers
    (0 "F.Cu" signal) (2 "B.Cu" signal) (9 "F.Adhes" user "F.Adhesive") (11 "B.Adhes" user "B.Adhesive")
    (13 "F.Paste" user) (15 "B.Paste" user) (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen")
    (1 "F.Mask" user) (3 "B.Mask" user) (17 "Dwgs.User" user "User.Drawings") (19 "Cmts.User" user "User.Comments")
    (21 "Eco1.User" user "User.Eco1") (23 "Eco2.User" user "User.Eco2") (25 "Edge.Cuts" user) (27 "Margin" user)
    (31 "F.CrtYd" user "F.Courtyard") (29 "B.CrtYd" user "B.Courtyard") (35 "F.Fab" user) (33 "B.Fab" user)
  )"""


class BuildError(Exception):
    pass


def _kpy(mode, spec, kdir, out=None):
    fp = os.path.join(kdir, f".{mode}.json")
    with open(fp, "w") as f:
        json.dump(spec, f)
    env.run_python(BOARD_SCRIPT, mode, fp, *([out] if out else []))
    if out:
        with open(out) as f:
            return json.load(f)


def footprint_ref(meta):
    """(dir, name, lib) for pcbnew.FootprintLoad."""
    if meta["source"] == "kicad":
        lib, name = meta["footprint"].split(":")
        return os.path.join(env.footprint_dir(), lib + ".pretty"), name, lib
    return None, meta["footprint"].split(":")[1], project.LIB


def compile_design(data, pdir, emit=lambda stage, msg: None, route=True):
    t0 = time.time()
    D = Design(data)
    name = "board"
    kdir = os.path.join(pdir, "kicad")
    os.makedirs(kdir, exist_ok=True)
    report = {}

    # ------------------------------------------------------------ libraries + schematic + ERC
    emit("library", f"Collecting {len(D.d['components'])} parts (symbols, footprints, 3D models)")
    for c in D.d["components"]:
        D.meta(c["ref"])
    project.write_libraries(D, kdir)
    power = [n["name"] for n in D.d["nets"] if is_ground(n["name"]) or n.get("voltage") is not None]
    project.write_project_file(kdir, name, power)
    emit("schematic", "Drawing the schematic")
    text, info = schematic.build(D, name)
    sch = os.path.join(kdir, f"{name}.kicad_sch")
    with open(sch, "w", encoding="utf-8") as f:
        f.write(text)
    env.cli("sch", "upgrade", "--force", sch, check=False)
    emit("erc", "Running KiCad ERC")
    report["erc"] = run_erc(sch, kdir)
    report["schematic"] = {"paper": info["paper"]}

    # ------------------------------------------------------------ footprints + placement
    emit("placement", "Measuring footprints")
    fps, keys = [], {}
    pretty = os.path.join(kdir, "lib", f"{project.LIB}.pretty")
    for c in D.d["components"]:
        d, fpname, lib = footprint_ref(D.meta(c["ref"]))
        d = d or pretty
        key = f"{lib}:{fpname}"
        keys[c["ref"]] = (key, d, fpname, lib)
        if key not in {f["key"] for f in fps}:
            fps.append({"key": key, "dir": d, "name": fpname})
    probe = _kpy("probe", {"footprints": fps}, kdir, os.path.join(kdir, ".probe_out.json"))
    geom = {c["ref"]: probe[keys[c["ref"]][0]] for c in D.d["components"]}
    b = D.d.get("board") or {}
    emit("placement", "Placing parts")
    pl = placer.place(D, geom, b)
    report["placement"] = {"width": pl["width"], "height": pl["height"]}
    emit("placement", f"Board {pl['width']:g} × {pl['height']:g} mm")

    # ------------------------------------------------------------ board
    pcb = os.path.join(kdir, f"{name}.kicad_pcb")
    with open(pcb, "w", encoding="utf-8") as f:
        f.write(f'(kicad_pcb (version 20241229) (generator "pcb_copilot_core") (generator_version "10.0")\n'
                f'  (general (thickness 1.6) (legacy_teardrops no))\n  (paper "A4")\n{LAYERS}\n'
                f'  (setup (pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints yes))\n'
                f'  (net 0 "")\n)\n')
    pmap = D.pin_net()
    footprints = []
    for c in D.d["components"]:
        key, d, fpname, lib = keys[c["ref"]]
        p = pl["parts"][c["ref"]]
        footprints.append({"ref": c["ref"], "value": c["value"], "dir": d, "name": fpname, "lib": lib,
                           "x": p["x"], "y": p["y"], "rot": p["rot"],
                           "path": "/" + info["paths"][c["ref"]] if c["ref"] in info["paths"] else None,
                           "pad_nets": {pin.split(".")[1]: net for pin, net in pmap.items()
                                        if pin.split(".")[0] == c["ref"]}})
    gnd = next((n["name"] for n in D.d["nets"] if is_ground(n["name"])), None)
    zones = [{"net": gnd, "layer": layer} for layer in ("F.Cu", "B.Cu")] if gnd else []
    dsn = os.path.join(kdir, f"{name}.dsn")
    spec = {"pcb": pcb, "dsn": dsn, "rules": project.RULES, "nets": sorted(n["name"] for n in D.d["nets"]),
            "footprints": footprints,
            "board": {"width": pl["width"], "height": pl["height"], "corner_radius": b.get("corner_radius", 1.0)}}
    emit("board", "Creating the KiCad board (outline, nets, rules)")
    _kpy("build", spec, kdir)

    # ------------------------------------------------------------ routing + pours + DRC
    ses = os.path.join(kdir, f"{name}.ses")
    if route:
        emit("routing", "Autorouting with Freerouting")
        report["routing"] = autoroute(dsn, ses, emit)
    _kpy("finish", {"pcb": pcb, "ses": ses if route and os.path.exists(ses) else None, "zones": zones,
                    "stitch_net": gnd if len(zones) == 2 else None, "board": spec["board"]}, kdir)
    emit("drc", "Running KiCad DRC")
    report["drc"] = run_drc(pcb, kdir)
    report["geometry"] = _kpy("dump", {"pcb": pcb}, kdir, os.path.join(kdir, ".dump.json"))
    report["seconds"] = round(time.time() - t0, 1)
    report["built_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(pdir, "build.json"), "w") as f:
        json.dump(report, f, indent=1)
    return report


def errors(report, key):
    return [v for v in report.get(key) or [] if v.get("severity") == "error"]


# ------------------------------------------------------------------ Freerouting

def java():
    home = os.environ.get("JAVA_HOME")
    for c in JAVA_CANDIDATES + ([os.path.join(home, "bin", "java")] if home else []):
        if os.path.exists(c):
            return c
    return shutil.which("java")


def freerouting_jar(emit):
    if not os.path.exists(FREEROUTING):
        emit("routing", "Downloading Freerouting 2.5.0 (about 65 MB, once)")
        os.makedirs(paths.HOME, exist_ok=True)
        tmp = FREEROUTING + ".part"
        urllib.request.urlretrieve(FREEROUTING_URL, tmp)
        os.replace(tmp, FREEROUTING)
    return FREEROUTING


def autoroute(dsn, ses, emit, passes=60, timeout=420):
    """Freerouting, headless. Needs Java 21 or newer on the machine."""
    if os.path.exists(ses):
        os.remove(ses)
    exe = java()
    if not exe:
        return {"ok": False, "log_tail": "Java not found: install OpenJDK 21+ (brew install openjdk) to autoroute"}
    t = time.time()
    cmd = [exe, "-Djava.awt.headless=true", "-jar", freerouting_jar(emit), "-de", dsn, "-do", ses, "-mp", str(passes),
           "--gui.enabled=false", "-l", "en", "--usage_and_diagnostic_data.disable_analytics=true"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        log = (r.stdout + r.stderr)[-800:]
    except subprocess.TimeoutExpired:
        log = "timeout"
    return {"ok": os.path.exists(ses), "seconds": round(time.time() - t, 1), "log_tail": log}


# ------------------------------------------------------------------ KiCad's own checks

def run_erc(sch, kdir):
    out = os.path.join(kdir, "erc.json")
    env.cli("sch", "erc", "--format", "json", "--severity-all", "-o", out, sch, check=False)
    with open(out) as f:
        d = json.load(f)
    return [{"severity": v.get("severity"), "type": v.get("type"), "message": v.get("description"),
             "items": [i.get("description") for i in v.get("items", [])]}
            for sh in d.get("sheets", []) for v in sh.get("violations", [])]


def run_drc(pcb, kdir):
    out = os.path.join(kdir, "drc.json")
    env.cli("pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "-o", out, pcb, check=False)
    with open(out) as f:
        d = json.load(f)
    return [{"severity": v.get("severity"), "type": v.get("type"), "group": key, "message": v.get("description"),
             "items": [i.get("description") for i in v.get("items", [])]}
            for key in ("violations", "unconnected_items") for v in d.get(key, [])]


# ------------------------------------------------------------------ release files

def release(data, pdir, emit=lambda stage, msg: None):
    """Gerbers, drill files, BOM and pick-and-place CSVs for JLCPCB, STEP model. Returns the output folder."""
    kdir = os.path.join(pdir, "kicad")
    pcb = os.path.join(kdir, "board.kicad_pcb")
    if not os.path.exists(pcb):
        raise BuildError("build the board first")
    out = os.path.join(pdir, "release")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(out, "gerber"))
    emit("release", "Exporting Gerbers and drill files")
    env.cli("pcb", "export", "gerbers", "--layers", "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts",
            "--subtract-soldermask", "--use-drill-file-origin", "-o", os.path.join(out, "gerber"), pcb)
    env.cli("pcb", "export", "drill", "--format", "excellon", "--drill-origin", "plot", "--excellon-separate-th",
            "-o", os.path.join(out, "gerber") + "/", pcb)
    emit("release", "Writing BOM and pick-and-place files")
    D = Design(data)
    rows = {}
    for c in D.d["components"]:
        rows.setdefault(c["lcsc"], {"value": c["value"], "mpn": c["mpn"], "package": c["package"], "refs": []})
        rows[c["lcsc"]]["refs"].append(c["ref"])
    with open(os.path.join(out, "bom.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #", "MPN"])
        for lcsc, r in rows.items():
            w.writerow([r["value"], ",".join(r["refs"]), r["package"], lcsc, r["mpn"]])
    geom = _kpy("dump", {"pcb": pcb}, kdir, os.path.join(kdir, ".dump.json"))
    H = max((fp["y"] for fp in geom["footprints"]), default=0)
    with open(os.path.join(out, "cpl.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for fp in geom["footprints"]:
            w.writerow([fp["ref"], f"{fp['x']:.3f}mm", f"{H - fp['y']:.3f}mm", "Bottom" if fp["bottom"] else "Top",
                        f"{fp['rot']:.1f}"])
    emit("release", "Exporting the STEP model")
    env.cli("pcb", "export", "step", "--subst-models", "-o", os.path.join(out, "board.step"), pcb, check=False)
    for f in os.listdir(kdir):
        if f.endswith((".kicad_pro", ".kicad_sch", ".kicad_pcb", "sym-lib-table", "fp-lib-table")):
            shutil.copy(os.path.join(kdir, f), os.path.join(out, f))
    shutil.copytree(os.path.join(kdir, "lib"), os.path.join(out, "lib"))
    return out


if __name__ == "__main__":                         # python -m pcbcopilot.build <project folder>
    pdir = sys.argv[1]
    with open(os.path.join(pdir, "design.json")) as f:
        rep = compile_design(json.load(f), pdir, emit=lambda s, m: print(f"[{s}] {m}"))
    print(f"ERC errors: {len(errors(rep, 'erc'))}, DRC errors: {len(errors(rep, 'drc'))}, {rep['seconds']} s")
