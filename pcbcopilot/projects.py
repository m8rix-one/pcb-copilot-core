"""Projects: one folder per board under ~/.pcb-copilot-core/projects/<id>/ with design.json and a kicad/ folder."""
import contextlib
import json
import os
import re
import time

from . import ir, paths
from .ir import Design

ROOT = paths.PROJECTS


def path(pid, *parts):
    return os.path.join(ROOT, pid, *parts)


def create(name, requirements=""):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "board"
    pid = slug
    k = 2
    while os.path.exists(path(pid)):
        pid = f"{slug}-{k}"
        k += 1
    os.makedirs(path(pid))
    save(pid, ir.new(name, requirements))
    return pid


def list_all():
    if not os.path.isdir(ROOT):
        return []
    out = []
    for pid in sorted(os.listdir(ROOT)):
        fp = path(pid, "design.json")
        if os.path.exists(fp):
            d = load(pid)
            out.append({"id": pid, "name": d.get("name"), "parts": len(d.get("components", [])),
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(fp)))})
    return out


def load(pid):
    fp = path(pid, "design.json")
    if not os.path.exists(fp):
        raise FileNotFoundError(f"no project '{pid}' in {ROOT}")
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def save(pid, d):
    tmp = path(pid, "design.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path(pid, "design.json"))


@contextlib.contextmanager
def editing(pid):
    """with editing(pid) as D: ... -> the design is saved when the block ends without an error."""
    D = Design(load(pid))
    yield D
    errs = ir.validate(D.d)
    if errs:
        raise ir.IRError("; ".join(errs))
    save(pid, D.d)
