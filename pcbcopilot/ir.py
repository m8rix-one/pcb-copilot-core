"""Circuit IR: the design the agent edits, compiled to KiCad afterwards.

    {
      "name", "summary", "requirements",
      "board": {"width", "height", "corner_radius", "edges": {"J1": "left"}},
      "components": [{"ref", "lcsc", "mpn", "value", "kind", "package", "purpose"}],
      "nets": [{"name", "voltage"?, "pins": ["U1.3"]}],
      "no_connect": ["U1.7"],
      "notes": []
    }

Pins are always stored as REF.NUMBER; the tools accept pin names and resolve them against the real symbol.
The model never writes this file by hand: every change goes through Design, which refuses what does not exist.
"""
import copy
import re

from . import library

REF_PREFIX = {"resistor": "R", "capacitor": "C", "led": "D"}
GROUND = re.compile(r"^(GND|AGND|DGND|PGND|GNDA|GNDD|VSS|0V|SGND)$", re.I)


def is_ground(name):
    return bool(GROUND.match(name or ""))


def new(name="Untitled board", requirements=""):
    return {"name": name, "summary": "", "requirements": requirements,
            "board": {"width": None, "height": None, "corner_radius": 1.0, "edges": {}},
            "components": [], "nets": [], "no_connect": [], "notes": []}


class IRError(Exception):
    pass


class Design:
    """Mutating operations with validation; every tool call goes through here."""

    def __init__(self, data):
        self.d = data
        self._meta = {}

    # ------------------------------------------------------------ lookup
    def comp(self, ref):
        for c in self.d["components"]:
            if c["ref"] == ref:
                return c
        raise IRError(f"no component {ref}")

    def meta(self, ref):
        c = self.comp(ref)
        if c["lcsc"] not in self._meta:
            self._meta[c["lcsc"]] = library.get(c["lcsc"])
        return self._meta[c["lcsc"]]

    def net(self, name):
        for n in self.d["nets"]:
            if n["name"] == name:
                return n
        return None

    def pin_net(self):
        return {p: n["name"] for n in self.d["nets"] for p in n["pins"]}

    def resolve_pins(self, spec):
        """'U1.VIN' / 'U1.3' / 'U1.GND' (every pin named GND) -> ['U1.3', ...]."""
        ref, _, pin = str(spec).strip().partition(".")
        if not pin:
            raise IRError(f"'{spec}' must look like REF.PIN (e.g. U1.VIN or U1.3)")
        meta = self.meta(ref)
        if pin in [p["number"] for p in meta["pins"]]:
            return [f"{ref}.{pin}"]
        want = pin.rstrip("*").upper()
        hits = [p["number"] for p in meta["pins"] if p["name"].upper() == want]
        if not hits:
            clean = lambda s: re.sub(r"[~{}/#!_\- ]", "", s.upper())        # noqa: E731  ~{RESET}, /RESET, RESET#
            hits = [p["number"] for p in meta["pins"] if clean(p["name"]) == clean(want)]
        if not hits:
            names = ", ".join(f"{p['number']}={p['name']}" for p in meta["pins"][:80])
            raise IRError(f"{ref} ({meta['mpn']}) has no pin '{pin}'. Pins: {names}")
        return [f"{ref}.{h}" for h in hits]

    # ------------------------------------------------------------ edits
    def next_ref(self, prefix):
        used = {int(m.group(1)) for c in self.d["components"]
                for m in [re.match(rf"^{prefix}(\d+)$", c["ref"])] if m}
        k = 1
        while k in used:
            k += 1
        return f"{prefix}{k}"

    def add(self, lcsc, ref=None, value=None, purpose="", row=None):
        meta = library.get(lcsc, row=row)
        self._meta[meta["lcsc"]] = meta
        ref = ref or self.next_ref(REF_PREFIX.get(meta["kind"]) or _prefix_for(meta))
        if any(c["ref"] == ref for c in self.d["components"]):
            raise IRError(f"ref {ref} already used")
        self.d["components"].append({"ref": ref, "lcsc": meta["lcsc"], "mpn": meta["mpn"],
                                     "value": value or meta["mpn"], "kind": meta["kind"],
                                     "package": meta["package"], "purpose": purpose})
        return ref, meta

    def remove(self, ref):
        self.comp(ref)
        self.d["components"] = [c for c in self.d["components"] if c["ref"] != ref]
        for n in self.d["nets"]:
            n["pins"] = [p for p in n["pins"] if p.split(".")[0] != ref]
        self.d["nets"] = [n for n in self.d["nets"] if n["pins"]]
        self.d["no_connect"] = [p for p in self.d["no_connect"] if p.split(".")[0] != ref]

    def replace_part(self, ref, lcsc, value=None):
        """Swap the part behind a ref, keeping connections by pin name (then pin number) where possible."""
        c = self.comp(ref)
        old = self.meta(ref)
        new_meta = library.get(lcsc)
        self._meta[new_meta["lcsc"]] = new_meta
        old_names = {p["number"]: p["name"] for p in old["pins"]}
        moved, dropped = 0, []
        for n in self.d["nets"]:
            out = []
            for p in n["pins"]:
                r, num = p.split(".")
                if r != ref:
                    out.append(p)
                    continue
                name = old_names.get(num, "")
                cands = [q["number"] for q in new_meta["pins"] if q["name"].upper() == name.upper()] or \
                        [q["number"] for q in new_meta["pins"] if q["number"] == num]
                if cands:
                    out.append(f"{ref}.{cands[0]}")
                    moved += 1
                else:
                    dropped.append(f"{num}({name})")
            n["pins"] = out
        c.update({"lcsc": new_meta["lcsc"], "mpn": new_meta["mpn"], "kind": new_meta["kind"],
                  "package": new_meta["package"], "value": value or new_meta["mpn"]})
        return moved, dropped

    def connect(self, net, pins, voltage=None):
        resolved = []
        for spec in pins:
            resolved += self.resolve_pins(spec)
        pmap = self.pin_net()
        n = self.net(net)
        if n is None:
            n = {"name": net, "pins": []}
            self.d["nets"].append(n)
        for p in resolved:
            other = pmap.get(p)
            if other and other != net:
                raise IRError(f"{p} is already on net {other}. Disconnect it first.")
            if p not in n["pins"]:
                n["pins"].append(p)
            if p in self.d["no_connect"]:
                self.d["no_connect"].remove(p)
        if voltage is not None:
            n["voltage"] = voltage
        return resolved

    def disconnect(self, pins):
        out = []
        for spec in pins:
            for p in self.resolve_pins(spec):
                for n in self.d["nets"]:
                    if p in n["pins"]:
                        n["pins"].remove(p)
                        out.append(p)
        self.d["nets"] = [n for n in self.d["nets"] if n["pins"]]
        return out

    def mark_nc(self, pins):
        for spec in pins:
            for p in self.resolve_pins(spec):
                if p in self.pin_net():
                    raise IRError(f"{p} is connected to {self.pin_net()[p]}; disconnect it first")
                if p not in self.d["no_connect"]:
                    self.d["no_connect"].append(p)

    # ------------------------------------------------------------ views
    def summary(self):
        lines = [f"# {self.d['name']}", self.d.get("summary", "")]
        pmap = self.pin_net()
        for c in self.d["components"]:
            m = self.meta(c["ref"])
            line = f"{c['ref']} {c['value']} [{c['lcsc']} {m['mpn']} {m['package']}] {c.get('purpose', '')}"
            pins = []
            for p in m["pins"]:
                key = f"{c['ref']}.{p['number']}"
                tag = pmap.get(key) or ("NC" if key in self.d["no_connect"] else "-")
                pins.append(f"{p['number']}:{p['name']}={tag}")
            lines.append(line + "\n    " + " ".join(pins))
        lines.append("Nets:")
        for n in self.d["nets"]:
            v = f" {n['voltage']}V" if n.get("voltage") is not None else ""
            lines.append(f"  {n['name']}{v}: {' '.join(n['pins'])}")
        return "\n".join(lines)

    def copy(self):
        return copy.deepcopy(self.d)


def _prefix_for(meta):
    t = ((meta.get("category") or "") + " " + (meta.get("subcategory") or "") + " " +
         (meta.get("description") or "")).lower()
    for keys, p in ((("connector", "header", "usb", "terminal", "socket", "jack"), "J"),
                    (("crystal", "oscillator", "resonator"), "Y"), (("inductor", "ferrite"), "L"),
                    (("mosfet", "transistor", "bjt"), "Q"), (("diode", "tvs", "esd", "zener", "led"), "D"),
                    (("switch", "button"), "SW"), (("fuse",), "F"), (("buzzer",), "BZ"),
                    (("resistor",), "R"), (("capacitor",), "C")):
        if any(k in t for k in keys):
            return p
    return "U"


def validate(d):
    """The only IR-level check: refs unique, every pin on one net at most, every pin's part exists."""
    errs, refs, seen = [], set(), {}
    for c in d["components"]:
        if c["ref"] in refs:
            errs.append(f"duplicate ref {c['ref']}")
        refs.add(c["ref"])
    for n in d["nets"]:
        for p in n["pins"]:
            if p.split(".")[0] not in refs:
                errs.append(f"net {n['name']}: unknown component in {p}")
            if p in seen:
                errs.append(f"{p} is on nets {seen[p]} and {n['name']}")
            seen[p] = n["name"]
    return errs
