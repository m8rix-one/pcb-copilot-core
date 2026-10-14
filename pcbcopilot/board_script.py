"""Runs under KiCad's bundled Python (3.9) where `pcbnew` is importable.

    python board_script.py probe  in.json out.json   footprint geometry for the placer
    python board_script.py build  in.json            create the .kicad_pcb (+ Specctra .dsn for Freerouting)
    python board_script.py finish in.json            import the routed .ses, pour and fill zones, save
    python board_script.py dump   in.json out.json   footprint positions and pads (for the pick-and-place file)

Keep this file Python 3.9 compatible and free of non-stdlib imports.
"""
import json
import math
import sys

import pcbnew

MM = 1e6


def mm(v):
    return int(round(v * MM))


def V(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def to_mm(v):
    return round(v / MM, 4)


def load_fp(d, name):
    fp = pcbnew.FootprintLoad(d, name)
    if fp is None:
        raise SystemExit("footprint not found: %s/%s" % (d, name))
    return fp


def bbox_of(box):
    return [to_mm(box.GetLeft()), to_mm(box.GetTop()), to_mm(box.GetRight()), to_mm(box.GetBottom())]


def pad_info(p, absolute=True):
    pos = p.GetPosition() if absolute else p.GetFPRelativePosition()
    size = p.GetSize(pcbnew.F_Cu)
    drill = p.GetDrillSize()
    return {"num": p.GetNumber(), "x": to_mm(pos.x), "y": to_mm(pos.y), "w": to_mm(size.x), "h": to_mm(size.y),
            "bbox": bbox_of(p.GetBoundingBox()),
            "drill": to_mm(min(drill.x, drill.y)) if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH else 0}


# ------------------------------------------------------------------ probe

def probe(spec, out):
    res = {}
    for f in spec["footprints"]:
        fp = load_fp(f["dir"], f["name"])
        fp.SetPosition(V(0, 0))
        pads = [pad_info(p, absolute=False) for p in fp.Pads()]
        cy = fp.GetCourtyard(pcbnew.F_CrtYd)
        court = bbox_of(cy.BBox()) if cy.OutlineCount() else bbox_of(fp.GetBoundingBox(False))
        for p in pads:                       # never let pads stick out of the keep-out box
            b = p["bbox"]
            court = [min(court[0], b[0] - 0.25), min(court[1], b[1] - 0.25),
                     max(court[2], b[2] + 0.25), max(court[3], b[3] + 0.25)]
        res[f["key"]] = {"pads": pads, "courtyard": court}
    with open(out, "w") as fh:
        json.dump(res, fh)


# ------------------------------------------------------------------ build

def add_outline(board, w, h, r):
    def seg(x1, y1, x2, y2):
        s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(x1, y1))
        s.SetEnd(V(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1))
        board.Add(s)

    def arc(cx, cy, a0):
        s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_ARC)
        p = lambda a: V(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))  # noqa: E731
        s.SetArcGeometry(p(a0), p(a0 + 45), p(a0 + 90))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1))
        board.Add(s)
    if r <= 0:
        seg(0, 0, w, 0); seg(w, 0, w, h); seg(w, h, 0, h); seg(0, h, 0, 0)
        return
    seg(r, 0, w - r, 0); seg(w, r, w, h - r); seg(w - r, h, r, h); seg(0, h - r, 0, r)
    arc(w - r, r, 270); arc(w - r, h - r, 0); arc(r, h - r, 90); arc(r, r, 180)


def add_zone(board, net, layer, w, h, inset=0.3, clearance=0.3, min_width=0.25):
    """A copper pour over the whole board."""
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNetCode(net.GetNetCode())
    z.SetAssignedPriority(0)
    z.SetLocalClearance(mm(clearance))
    z.SetMinThickness(mm(min_width))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_THT_THERMAL)
    z.SetThermalReliefGap(mm(0.4))
    z.SetThermalReliefSpokeWidth(mm(0.45))
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    o = z.Outline()
    o.NewOutline()
    for x, y in ((inset, inset), (w - inset, inset), (w - inset, h - inset), (inset, h - inset)):
        o.Append(mm(x), mm(y))
    board.Add(z)
    return z


def add_edge_band(board, rect, W, H):
    """A keep-out band along the outline so Freerouting honours the copper-to-edge clearance."""
    x0, y0, x1, y1 = max(0, rect[0]), max(0, rect[1]), min(W, rect[2]), min(H, rect[3])
    z = pcbnew.ZONE(board)
    z.SetIsRuleArea(True)
    ls = pcbnew.LSET()
    for layer in board.GetEnabledLayers().CuStack():
        ls.AddLayer(layer)
    z.SetLayerSet(ls)
    for fn in ("SetDoNotAllowZoneFills", "SetDoNotAllowCopperPour"):
        if hasattr(z, fn):
            getattr(z, fn)(False)
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowVias(True)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowFootprints(False)
    z.SetZoneName("Edge clearance")
    o = z.Outline()
    o.NewOutline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        o.Append(mm(x), mm(y))
    board.Add(z)


def build(spec):
    board = pcbnew.LoadBoard(spec["pcb"])
    ds = board.GetDesignSettings()
    r = spec["rules"]
    ds.m_MinClearance = mm(r["clearance"])
    ds.m_TrackMinWidth = mm(r.get("min_track", 0.1))
    ds.m_CopperEdgeClearance = mm(r.get("edge", 0.3))
    ds.m_HoleClearance = mm(0.25)
    ds.SetCopperLayerCount(2)
    nets = {}
    for name in spec["nets"]:
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        nets[name] = ni
    for f in spec["footprints"]:
        fp = load_fp(f["dir"], f["name"])
        fp.SetFPID(pcbnew.LIB_ID(f["lib"], f["name"]))
        fp.SetReference(f["ref"])
        fp.SetValue(f["value"])
        if f.get("path"):
            fp.SetPath(pcbnew.KIID_PATH(f["path"]))
        board.Add(fp)
        fp.SetPosition(V(f["x"], f["y"]))
        fp.SetOrientationDegrees(f["rot"])
        for p in fp.Pads():
            n = f["pad_nets"].get(p.GetNumber())
            if n:
                p.SetNet(nets[n])
        fp.Value().SetLayer(pcbnew.F_Fab)          # the reference stays readable on the silkscreen
        fp.Reference().SetTextSize(pcbnew.VECTOR2I(mm(0.8), mm(0.8)))
        fp.Reference().SetTextThickness(mm(0.12))
    W, H = spec["board"]["width"], spec["board"]["height"]
    add_outline(board, W, H, spec["board"].get("corner_radius", 1.0))
    ds.SetAuxOrigin(V(0, H))                      # fab convention: origin at the bottom-left corner
    ds.SetGridOrigin(V(0, H))
    band = max(0.0, r.get("edge", 0.3) - r["clearance"]) + 0.05
    if band > 0.05:
        for rect in ((0, 0, W, band), (0, H - band, W, H), (0, 0, band, H), (W - band, 0, W, H)):
            add_edge_band(board, rect, W, H)
    pcbnew.SaveBoard(spec["pcb"], board)
    if spec.get("dsn"):
        board = pcbnew.LoadBoard(spec["pcb"])
        if not pcbnew.ExportSpecctraDSN(board, spec["dsn"]):
            raise SystemExit("DSN export failed")


LAYER = {"F.Cu": pcbnew.F_Cu, "B.Cu": pcbnew.B_Cu}


def finish(spec):
    board = pcbnew.LoadBoard(spec["pcb"])
    if spec.get("ses"):
        if not pcbnew.ImportSpecctraSES(board, spec["ses"]):
            raise SystemExit("SES import failed")
    W, H = spec["board"]["width"], spec["board"]["height"]
    for z in spec.get("zones", []):
        net = board.FindNet(z["net"])
        if net is None:
            sys.stderr.write("pour skipped: no net %s on the board\n" % z["net"])
            continue
        add_zone(board, net, LAYER[z["layer"]], W, H)
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    if spec.get("stitch_net"):
        if stitch(board, spec["stitch_net"], 5.0):
            filler.Fill(board.Zones())
    pcbnew.SaveBoard(spec["pcb"], board)


def stitch(board, netname, pitch):
    """GND stitching vias wherever both pours are solid, so top and bottom ground act as one plane."""
    net = board.FindNet(netname)
    if net is None:
        return 0
    fills = {}
    for z in board.Zones():
        if z.GetNetname() != netname:
            continue
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            if z.IsOnLayer(layer) and z.HasFilledPolysForLayer(layer):
                fills[layer] = z.GetFilledPolysList(layer)
    if len(fills) < 2:
        return 0
    bb = board.GetBoardEdgesBoundingBox()
    r = 0.3 + 0.35
    existing = [(v.GetPosition().x, v.GetPosition().y) for v in board.GetTracks() if v.GetClass() == "PCB_VIA"]
    added = 0
    y = bb.GetTop() + mm(pitch / 2)
    while y < bb.GetBottom():
        x = bb.GetLeft() + mm(pitch / 2)
        while x < bb.GetRight():
            ok = True
            for poly in fills.values():
                for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
                    if not poly.Contains(pcbnew.VECTOR2I(x + mm(dx), y + mm(dy))):
                        ok = False
                        break
                if not ok:
                    break
            if ok and all(abs(x - ex) > mm(1.0) or abs(y - ey) > mm(1.0) for ex, ey in existing):
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(pcbnew.VECTOR2I(x, y))
                v.SetWidth(mm(0.6))
                v.SetDrill(mm(0.3))
                v.SetNet(net)
                board.Add(v)
                added += 1
            x += mm(pitch)
        y += mm(pitch)
    return added


# ------------------------------------------------------------------ dump

def dump(spec, out):
    board = pcbnew.LoadBoard(spec["pcb"])
    res = {"footprints": [], "tracks": 0, "vias": 0}
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        res["footprints"].append({"ref": fp.GetReference(), "value": fp.GetValue(), "x": to_mm(pos.x),
                                  "y": to_mm(pos.y), "rot": fp.GetOrientation().AsDegrees(),
                                  "bottom": fp.IsFlipped()})
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            res["vias"] += 1
        else:
            res["tracks"] += 1
    with open(out, "w") as fh:
        json.dump(res, fh)


if __name__ == "__main__":
    mode, inp = sys.argv[1], sys.argv[2]
    with open(inp) as fh:
        spec = json.load(fh)
    if mode == "probe":
        probe(spec, sys.argv[3])
    elif mode == "build":
        build(spec)
    elif mode == "finish":
        finish(spec)
    elif mode == "dump":
        dump(spec, sys.argv[3])
    else:
        raise SystemExit("unknown mode " + mode)
