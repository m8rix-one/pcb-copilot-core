"""JLCPCB / LCSC catalog: keyword and parametric search, part lookup, best in-stock passive.

Backed by the public jlcsearch API (tscircuit), which mirrors JLCPCB's assembly catalog. Responses are
cached on disk for a few hours.
"""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import paths

BASE = "https://jlcsearch.tscircuit.com"
CACHE = os.path.join(paths.HOME, "cache", "http")
TTL = 6 * 3600
UA = {"User-Agent": "pcb-copilot-core/0.1"}

CATEGORIES = [
    "accelerometers", "adcs", "analog_multiplexers", "analog_switches", "arm_processors", "barrel_jacks",
    "battery_holders", "bjt_transistors", "ble_chips", "ble_modules", "boost_converters", "buck_boost_converters",
    "capacitors", "dacs", "diodes", "ethernet_controllers", "fpc_connectors", "fuses", "gas_sensors", "gyroscopes",
    "headers", "io_expanders", "jst_connectors", "ldos", "led_drivers", "led_with_ic", "leds", "microcontrollers",
    "microphones", "mosfets", "oled_display", "optical_sensors", "potentiometers", "relays", "resistor_arrays",
    "resistors", "risc_v_processors", "stepper_motor_drivers", "switches", "usb_c_connectors",
    "voltage_regulators", "wifi_modules", "wire_to_board_connectors", "micro_usb_connectors", "lcd_display",
    "spring_clamp_terminal_blocks",
]


def _get(path, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    key = hashlib.sha1(url.encode()).hexdigest()
    fp = os.path.join(CACHE, key + ".json")
    if os.path.exists(fp) and time.time() - os.path.getmtime(fp) < TTL:
        with open(fp) as f:
            return json.load(f)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read())
    os.makedirs(CACHE, exist_ok=True)
    with open(fp, "w") as f:
        json.dump(data, f)
    return data


def _rows(data):
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


def lcsc_id(x):
    """'C6186' / 6186 / '6186' -> 'C6186'."""
    s = str(x).strip().upper()
    return s if s.startswith("C") else "C" + s


def price_at(price, qty=1):
    """jlcsearch price is either a float or a '1-49:0.2,50-149:0.15' ladder."""
    if price is None:
        return None
    if isinstance(price, (int, float)):
        return float(price)
    best = None
    for part in str(price).split(","):
        rng, _, p = part.partition(":")
        try:
            lo, val = int(rng.split("-")[0] or 1), float(p)
        except ValueError:
            continue
        if qty >= lo:
            best = val
    return best


def normalize(r):
    """One shape for every endpoint."""
    return {
        "lcsc": lcsc_id(r["lcsc"]),
        "mpn": r.get("mfr"),
        "package": r.get("package"),
        "description": r.get("description") or "",
        "stock": r.get("stock"),
        "price": price_at(r.get("price1", r.get("price"))),
        "basic": bool(r.get("is_basic")),
        "preferred": bool(r.get("is_preferred")),
        "category": r.get("category"),
        "subcategory": r.get("subcategory"),
        "specs": {k: v for k, v in r.items() if k not in (
            "lcsc", "mfr", "package", "description", "stock", "price", "price1", "is_basic", "is_preferred",
            "attributes", "category", "subcategory", "in_stock") and v not in (None, "", "-")},
    }


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def lookup(part):
    """Exact LCSC number or manufacturer part number -> catalog row (or None)."""
    q = str(part).strip()
    if re.fullmatch(r"[Cc]?\d+", q):
        q = lcsc_id(q)
        rows = [normalize(r) for r in _rows(_get("/components/list.json", {"search": q}))]
        return next((r for r in rows if r["lcsc"] == q), None) or _easyeda_row(q)
    want = _norm(q)
    # jlcsearch is fuzzy: try the full MPN, then shorter prefixes, and match on the normalised MPN
    tried = set()
    parts = re.split(r"(?=[-_/])", q)
    for k in range(len(parts), 0, -1):
        term = "".join(parts[:k]).strip("-_/ ")
        if len(term) < 3 or term in tried:
            continue
        tried.add(term)
        try:
            rows = [normalize(r) for r in _rows(_get("/components/list.json", {"search": term}))]
        except OSError:
            rows = []
        exact = [r for r in rows if _norm(r["mpn"]) == want]
        if exact:
            return max(exact, key=lambda r: (r["stock"] or 0))
    return None


def _easyeda_row(lcsc):
    """Fallback for any LCSC number: the LCSC/EasyEDA product record."""
    try:
        req = urllib.request.Request(f"https://easyeda.com/api/products/{lcsc}/components?version=6.4.19.5",
                                     headers=UA)
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read()).get("result") or {}
    except Exception:                                           # noqa: BLE001
        return None
    head = (res.get("dataStr") or {}).get("head", {}).get("c_para", {})
    info = res.get("lcsc") or {}
    if not head:
        return None
    pkg = head.get("package", "")
    m = re.match(r"^[RCL](0201|0402|0603|0805|1206|1210)$", pkg)
    tags = " ".join(res.get("tags") or [])
    sub = {"Resistors": "Chip Resistor - Surface Mount", "Capacitors": "Multilayer Ceramic Capacitors MLCC"}.get(
        tags, tags)
    return {"lcsc": lcsc, "mpn": head.get("Manufacturer Part") or res.get("title"), "package": m.group(1) if m else pkg,
            "description": f"{head.get('Value', '')} {tags} {pkg}".strip(), "stock": info.get("stock"),
            "price": info.get("price"), "basic": "Basic" in head.get("JLCPCB Part Class", ""),
            "preferred": "Preferred" in head.get("JLCPCB Part Class", ""), "category": tags, "subcategory": sub,
            "specs": {"value": head.get("Value")} if head.get("Value") else {}}


def search(query="", category=None, package=None, limit=12, in_stock=True, **filters):
    """Keyword search, or parametric search inside a category (sorted by stock)."""
    if category:
        category = category.strip().lower().replace(" ", "_").replace("-", "_")
        if category not in CATEGORIES:
            raise ValueError(f"unknown category '{category}'. Known: {', '.join(CATEGORIES)}")
        params = {k: v for k, v in filters.items() if v is not None}
        if package:
            params["package"] = package
        if query:
            params["search"] = query
        rows = _rows(_get(f"/{category}/list.json", params))
    else:
        rows = _rows(_get("/components/list.json", {"search": query, "limit": 60}))
        q = query.upper().split()
        rows.sort(key=lambda r: -sum(t in ((r.get("mfr") or "") + " " + (r.get("description") or "")).upper()
                                     for t in q))
        if package:
            rows = [r for r in rows if package.upper() in (r.get("package") or "").upper()]
    out = [normalize(r) for r in rows]
    if in_stock:
        out = [r for r in out if (r["stock"] or 0) > 0]
    return out[:limit]


# ------------------------------------------------------------- passives

SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "R": 1, "r": 1}


def parse_value(s):
    """'4.7k' / '4k7' / '100nF' / '10uF' / '330R' -> float (None if unparseable)."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace("Ω", "").replace("ohm", "").replace(" ", "")
    m = re.match(r"^(\d+)([pnuµmkKMRr])(\d+)", s)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * SI[m.group(2)]
    m = re.match(r"^(\d+(?:\.\d+)?(?:[eE]-?\d+)?)([pnuµmkKMRr]?)", s)
    if not m:
        return None
    return float(m.group(1)) * (SI.get(m.group(2), 1) if m.group(2) else 1)


def pick_passive(kind, value, package="0603", voltage=None, color=None):
    """Best in-stock JLC part for a resistor / capacitor / LED, preferring Basic parts (no loading fee).

    Returns (row, note)."""
    kind = kind.lower()
    if kind in ("r", "resistor"):
        v = parse_value(value)
        rows = _rows(_get("/resistors/list.json", {"package": package, "resistance": f"{v:.12g}"}))
        rows = [r for r in rows if r.get("resistance") and abs(r["resistance"] - v) / v < 1e-3]
    elif kind in ("c", "capacitor"):
        v = parse_value(value)
        rows = _rows(_get("/capacitors/list.json", {"package": package, "capacitance": f"{v:.12g}"}))
        rows = [r for r in rows if r.get("capacitance_farads") and abs(r["capacitance_farads"] - v) / v < 1e-3]
        if voltage:
            rows = [r for r in rows if (r.get("voltage_rating") or 0) >= voltage]
    elif kind == "led":
        rows = _rows(_get("/leds/list.json", {"package": package}))
        if color:
            nm = {"red": (610, 640), "orange": (595, 612), "yellow": (580, 595), "green": (515, 575),
                  "blue": (455, 480), "white": (0, 0)}.get(color.lower())
            if nm:
                rows = [r for r in rows if (nm == (0, 0) and "white" in (r.get("description") or "").lower())
                        or (r.get("wavelength_nm") and nm[0] <= r["wavelength_nm"] <= nm[1])
                        or color.lower() in (r.get("color") or "").lower()]
    else:
        raise ValueError("kind must be resistor, capacitor or led")
    rows = [r for r in rows if (r.get("stock") or 0) > 0]
    if not rows:
        return None, f"no in-stock {kind} {value} in {package} on JLCPCB"
    rows.sort(key=lambda r: (not r.get("is_basic"), not r.get("is_preferred"), -(r.get("stock") or 0)))
    best = normalize(rows[0])
    note = ("JLC Basic part" if best["basic"] else "JLC Extended part (assembly loading fee)") + \
           f", {best['stock']:,} in stock"
    return best, note
