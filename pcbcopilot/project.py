"""Write a self-contained KiCad project: project-local symbol/footprint libraries, 3D models and the .kicad_pro."""
import json
import os
import shutil

from . import kicad_env as env, library
from .ir import Design
from .sexpr import Q, dumps

LIB = "pcbcopilot"

# One set of design rules for every board (JLCPCB's standard 2-layer process, with margin).
RULES = {"clearance": 0.15, "track": 0.2, "power_track": 0.4, "via": 0.6, "drill": 0.3, "edge": 0.3,
         "min_track": 0.1}


def write_libraries(design: Design, kdir):
    """Copy every LCSC-generated symbol, footprint and 3D model the design uses into the project."""
    os.makedirs(os.path.join(kdir, "lib", f"{LIB}.pretty"), exist_ok=True)
    os.makedirs(os.path.join(kdir, "lib", "3dshapes"), exist_ok=True)
    syms, seen = [], set()
    for c in design.d["components"]:
        meta = design.meta(c["ref"])
        if meta["source"] != "lcsc" or meta["lcsc"] in seen:
            continue
        seen.add(meta["lcsc"])
        syms.append(library.symbol_sexpr(meta))
        src = library.footprint_file(meta)
        dst = os.path.join(kdir, "lib", f"{LIB}.pretty", os.path.basename(src))
        text = open(src, encoding="utf-8").read().replace("${PCBCOPILOT_3D}", "${KIPRJMOD}/lib/3dshapes")
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)
        mdir = library.model_dir(meta)
        if os.path.isdir(mdir):
            for m in os.listdir(mdir):
                if m.endswith((".step", ".wrl")):
                    shutil.copy(os.path.join(mdir, m), os.path.join(kdir, "lib", "3dshapes", m))
    with open(os.path.join(kdir, "lib", f"{LIB}.kicad_sym"), "w", encoding="utf-8") as f:
        f.write(dumps(["kicad_symbol_lib", ["version", 20241209], ["generator", Q("pcb_copilot_core")]] + syms) + "\n")
    std_syms = {"Device"} | {design.meta(c["ref"])["symbol"].split(":")[0] for c in design.d["components"]}
    std_fps = {design.meta(c["ref"])["footprint"].split(":")[0] for c in design.d["components"]}
    std_syms.discard(LIB)
    std_fps.discard(LIB)
    with open(os.path.join(kdir, "sym-lib-table"), "w") as f:
        f.write("(sym_lib_table\n  (version 7)\n")
        f.write(f'  (lib (name "{LIB}")(type "KiCad")(uri "${{KIPRJMOD}}/lib/{LIB}.kicad_sym")(options "")(descr ""))\n')
        for lib in sorted(std_syms):
            f.write(f'  (lib (name "{lib}")(type "KiCad")(uri "{env.symbol_dir()}/{lib}.kicad_sym")(options "")(descr ""))\n')
        f.write(")\n")
    with open(os.path.join(kdir, "fp-lib-table"), "w") as f:
        f.write("(fp_lib_table\n  (version 7)\n")
        f.write(f'  (lib (name "{LIB}")(type "KiCad")(uri "${{KIPRJMOD}}/lib/{LIB}.pretty")(options "")(descr ""))\n')
        for lib in sorted(std_fps):
            f.write(f'  (lib (name "{lib}")(type "KiCad")(uri "{env.footprint_dir()}/{lib}.pretty")(options "")(descr ""))\n')
        f.write(")\n")


def _netclass(name, clearance, track, via, drill, priority):
    return {"name": name, "clearance": clearance, "track_width": track, "via_diameter": via, "via_drill": drill,
            "wire_width": 6, "bus_width": 12, "line_style": 0, "pcb_color": "rgba(0, 0, 0, 0.000)",
            "schematic_color": "rgba(0, 0, 0, 0.000)", "diff_pair_gap": 0.25, "diff_pair_width": 0.2,
            "diff_pair_via_gap": 0.25, "microvia_diameter": 0.3, "microvia_drill": 0.1, "priority": priority}


def write_project_file(kdir, name, power_nets=()):
    """.kicad_pro with the design rules and two net classes (Default, Power); KiCad fills in the rest."""
    r = RULES
    pro = {
        "board": {"design_settings": {"rules": {
            "min_clearance": r["clearance"], "min_track_width": r["min_track"], "min_via_diameter": 0.45,
            "min_via_annular_width": 0.1, "min_through_hole_diameter": 0.2, "min_hole_clearance": 0.25,
            "min_hole_to_hole": 0.25, "min_copper_edge_clearance": r["edge"], "min_silk_clearance": 0.0,
            "min_text_height": 0.8, "min_text_thickness": 0.08}}},
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "net_settings": {"classes": [_netclass("Default", r["clearance"], r["track"], r["via"], r["drill"], 2147483647),
                                     _netclass("Power", r["clearance"], r["power_track"], r["via"], r["drill"], 0)],
                         "meta": {"version": 4}, "netclass_assignments": {},
                         "netclass_patterns": [{"netclass": "Power", "pattern": n} for n in power_nets]},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        "text_variables": {},
    }
    with open(os.path.join(kdir, f"{name}.kicad_pro"), "w") as f:
        json.dump(pro, f, indent=2)
