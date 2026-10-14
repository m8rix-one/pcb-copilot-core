"""Where KiCad 10 lives and how to call it (kicad-cli, and KiCad's bundled Python, which can import pcbnew)."""
import copy
import functools
import glob
import os
import re
import subprocess
import sys

from .sexpr import Q, find_all, loads

WINDOWS = sys.platform == "win32"
if WINDOWS:
    CANDIDATES = sorted(glob.glob(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "KiCad", "10.*")),
                        reverse=True)
else:
    CANDIDATES = [os.path.expanduser("~/Applications/KiCad/KiCad.app"), "/Applications/KiCad/KiCad.app"]


class KiCadError(Exception):
    pass


def _cli_in(app_dir):
    if WINDOWS:
        return os.path.join(app_dir, "bin", "kicad-cli.exe")
    return os.path.join(app_dir, "Contents", "MacOS", "kicad-cli")


@functools.lru_cache()
def app():
    env_app = os.environ.get("KICAD_APP")
    for c in ([env_app] if env_app else []) + CANDIDATES:
        if c and os.path.exists(_cli_in(c)):
            return c
    raise KiCadError("KiCad 10 not found. Install it (macOS: brew install --cask kicad) or set KICAD_APP.")


def cli_path():
    return _cli_in(app())


@functools.lru_cache()
def python():
    """KiCad's bundled Python: the only interpreter that can import pcbnew."""
    if WINDOWS:
        exe = os.path.join(app(), "bin", "python.exe")
        if not os.path.exists(exe):
            raise KiCadError("KiCad's bundled Python was not found")
        return exe
    hits = sorted(glob.glob(os.path.join(app(), "Contents", "Frameworks", "Python.framework", "Versions", "3.*",
                                         "bin", "python3.[0-9]*")))
    hits = [h for h in hits if h[-1].isdigit() and "-" not in os.path.basename(h)]
    if not hits:
        raise KiCadError("KiCad's bundled Python was not found")
    return hits[-1]


def shared():
    if WINDOWS:
        return os.path.join(app(), "share", "kicad")
    return os.path.join(app(), "Contents", "SharedSupport")


def footprint_dir():
    return os.path.join(shared(), "footprints")


def symbol_dir():
    return os.path.join(shared(), "symbols")


def model_dir():
    return os.path.join(shared(), "3dmodels")


def kienv():
    e = dict(os.environ)
    e.update({"KICAD10_3DMODEL_DIR": model_dir(), "KICAD10_FOOTPRINT_DIR": footprint_dir(),
              "KICAD10_SYMBOL_DIR": symbol_dir(), "KICAD_3DMODEL_DIR": model_dir()})
    return e


def cli(*args, check=True, timeout=300):
    """Run kicad-cli. Return code 5 means ERC/DRC found violations, which is not a failure."""
    args = [str(a) for a in args]
    r = subprocess.run([cli_path(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=kienv(), timeout=timeout)
    if check and r.returncode not in (0, 5):
        raise KiCadError(f"kicad-cli {' '.join(args[:3])} failed ({r.returncode}): "
                         f"{(r.stderr or r.stdout).strip()[-600:]}")
    return r


def run_python(script, *args, timeout=600):
    """Run a script under KiCad's Python (pcbnew available)."""
    r = subprocess.run([python(), script, *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=kienv(), timeout=timeout)
    if r.returncode != 0:
        raise KiCadError(f"{os.path.basename(script)} failed: {(r.stderr or r.stdout).strip()[-1500:]}")
    return r.stdout


@functools.lru_cache(maxsize=64)
def _symlib(lib):
    with open(os.path.join(symbol_dir(), lib + ".kicad_sym"), encoding="utf-8") as f:
        return loads(f.read())


def std_symbol(lib_id):
    """A symbol from KiCad's standard library, as an s-expression named 'Lib:Name'."""
    lib, name = lib_id.split(":")
    for s in find_all(_symlib(lib), "symbol"):
        if s[1] == name:
            s = copy.deepcopy(s)
            s[1] = Q(lib_id)
            return s
    raise KiCadError(f"symbol {lib_id} not in the KiCad library")


def search_library(query, kind="footprint", limit=25):
    """KiCad standard library footprints ('Lib:Name') or symbols whose name contains every query word."""
    words = [w.lower() for w in query.split()]
    out = []
    if kind == "footprint":
        for lib in sorted(os.listdir(footprint_dir())):
            if not lib.endswith(".pretty"):
                continue
            for f in sorted(os.listdir(os.path.join(footprint_dir(), lib))):
                name = f"{lib[:-7]}:{f[:-10]}"
                if all(w in name.lower() for w in words):
                    out.append(name)
    else:
        for f in sorted(os.listdir(symbol_dir())):
            if not f.endswith(".kicad_sym"):
                continue
            txt = open(os.path.join(symbol_dir(), f), encoding="utf-8").read()
            for name in re.findall(r'^\t\(symbol "([^"]+)"', txt, re.M):
                full = f"{f[:-10]}:{name}"
                if all(w in full.lower() for w in words):
                    out.append(full)
    return out[:limit]
