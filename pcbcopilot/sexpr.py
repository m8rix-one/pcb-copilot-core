"""Minimal S-expression reader/writer for KiCad files.

Atoms keep their type: str for bare symbols, Q for quoted strings, int/float for numbers,
so a file can be read, edited and written back without changing its meaning.
"""
import re


class Q(str):
    """A quoted string atom."""


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def loads(text):
    stack, cur = [], []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"bad s-expression near {text[pos:pos + 40]!r}")
        pos = m.end()
        if m.group(1):
            stack.append(cur)
            cur = []
        elif m.group(2):
            done = cur
            cur = stack.pop()
            cur.append(done)
        elif m.group(3) is not None:
            cur.append(Q(m.group(3).replace('\\"', '"').replace("\\\\", "\\")))
        else:
            cur.append(_atom(m.group(4)))
    if stack:
        raise ValueError("unbalanced parentheses")
    return cur[0] if len(cur) == 1 else cur


def _atom(s):
    try:
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?(\d+\.\d*|\.\d+|\d+)([eE]-?\d+)?", s):
            return float(s)
    except ValueError:
        pass
    return s


def _fmt(a):
    if isinstance(a, Q):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, float):
        s = f"{a:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(a)


INLINE = {"at", "xy", "pts", "start", "end", "mid", "center", "size", "effects", "font", "stroke", "fill",
          "uuid", "layer", "layers", "width", "type", "color", "justify", "offset", "length", "drill", "net",
          "property_hide", "exclude_from_sim", "in_bom", "on_board", "dnp", "pin_numbers", "pin_names",
          "number", "name", "rotate", "scale", "hide", "unit", "lib_id", "reference", "value", "thickness",
          "radius", "angle", "roundrect_rratio", "page", "path", "generator", "generator_version", "version"}


def dumps(e, indent=0):
    if not isinstance(e, list):
        return _fmt(e)
    if not e:
        return "()"
    head = e[0]
    simple = all(not isinstance(x, list) for x in e)
    if simple or (isinstance(head, str) and head in INLINE and sum(isinstance(x, list) for x in e) <= 2
                  and len(str(e)) < 140):
        return "(" + " ".join(dumps(x) for x in e) + ")"
    pad = "  " * (indent + 1)
    parts = ["(" + _fmt(head)]
    tail_atoms = []
    for x in e[1:]:
        if isinstance(x, list):
            if tail_atoms:
                parts[-1] += " " + " ".join(_fmt(a) for a in tail_atoms)
                tail_atoms = []
            parts.append(pad + dumps(x, indent + 1))
        else:
            if len(parts) == 1:
                parts[0] += " " + _fmt(x)
            else:
                parts.append(pad + _fmt(x))
    return "\n".join(parts) + "\n" + "  " * indent + ")"


def find(e, key):
    for x in e:
        if isinstance(x, list) and x and x[0] == key:
            return x
    return None


def find_all(e, key):
    return [x for x in e if isinstance(x, list) and x and x[0] == key]


def prop(e, name, default=None):
    for x in find_all(e, "property"):
        if len(x) > 2 and x[1] == name:
            return x[2]
    return default
