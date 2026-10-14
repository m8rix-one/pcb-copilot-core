"""Placement: connectors on the edge the design asks for, every other part packed into rows. Nothing clever;
the designer moves parts in KiCad afterwards, or the autorouter works with what it gets."""
import math

SPACING = 0.6       # between courtyards, mm
EDGE = 1.0          # courtyard to board edge, mm


def place(D, geom, board):
    """geom: ref -> {"courtyard": [x0, y0, x1, y1]} from the board script's probe.
    Returns {"width", "height", "parts": {ref: {x, y, rot}}}."""
    comps = D.d["components"]
    edges = board.get("edges") or {}
    court = {c["ref"]: geom[c["ref"]]["courtyard"] for c in comps}

    def size(ref):
        x0, y0, x1, y1 = court[ref]
        return x1 - x0, y1 - y0

    side = {s: [c["ref"] for c in comps if edges.get(c["ref"]) == s] for s in ("left", "right", "top", "bottom")}
    inner = [c["ref"] for c in comps if edges.get(c["ref"]) not in side]
    area = sum(w * h for w, h in (size(c["ref"]) for c in comps))
    left_w = max((size(r)[0] for r in side["left"]), default=0) + (SPACING if side["left"] else 0)
    right_w = max((size(r)[0] for r in side["right"]), default=0) + (SPACING if side["right"] else 0)
    top_h = max((size(r)[1] for r in side["top"]), default=0) + (SPACING if side["top"] else 0)
    bottom_h = max((size(r)[1] for r in side["bottom"]), default=0) + (SPACING if side["bottom"] else 0)
    W = board.get("width") or max(12.0, math.ceil(math.sqrt(area * 2.6)) + 2 * EDGE + left_w + right_w)
    W = max(W, max((size(r)[0] for r in inner), default=0) + 2 * EDGE + left_w + right_w + 2 * SPACING)
    parts = {}
    x_start, cx, cy, row_h = EDGE + left_w, EDGE + left_w, EDGE + top_h, 0.0
    for ref in sorted(inner, key=lambda r: (-size(r)[1], r)):
        w, h = size(ref)
        if cx + w > W - EDGE - right_w and cx > x_start:
            cy += row_h + SPACING
            cx, row_h = x_start, 0.0
        parts[ref] = {"x": round(cx - court[ref][0], 2), "y": round(cy - court[ref][1], 2), "rot": 0}
        cx += w + SPACING
        row_h = max(row_h, h)
    body_bottom = cy + row_h
    H = board.get("height") or math.ceil(body_bottom + bottom_h + EDGE)
    # edge connectors, stacked along their edge
    for s, refs in side.items():
        pos = EDGE
        for ref in sorted(refs):
            x0, y0, x1, y1 = court[ref]
            w, h = size(ref)
            if s == "left":
                parts[ref] = {"x": round(EDGE - x0, 2), "y": round(pos - y0, 2), "rot": 0}
                pos += h + SPACING
            elif s == "right":
                parts[ref] = {"x": round(W - EDGE - x1, 2), "y": round(pos - y0, 2), "rot": 0}
                pos += h + SPACING
            elif s == "top":
                parts[ref] = {"x": round(pos - x0, 2), "y": round(EDGE - y0, 2), "rot": 0}
                pos += w + SPACING
            else:
                parts[ref] = {"x": round(pos - x0, 2), "y": round(H - EDGE - y1, 2), "rot": 0}
                pos += w + SPACING
        H = max(H, math.ceil(pos + EDGE)) if s in ("left", "right") else H
        W = max(W, math.ceil(pos + EDGE)) if s in ("top", "bottom") else W
    return {"width": float(W), "height": float(H), "parts": parts}
