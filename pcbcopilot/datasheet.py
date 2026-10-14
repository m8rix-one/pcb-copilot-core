"""Datasheets: fetch the manufacturer PDF via LCSC, extract text per page, answer a focused question."""
import json
import os
import re
import urllib.request

from . import catalog, paths

ROOT = os.path.join(paths.HOME, "datasheets")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) pcb-copilot-core/0.1"}

TOPICS = {
    "pinout": ["pin", "description", "function", "configuration", "assignment"],
    "application": ["typical application", "application circuit", "reference", "schematic", "recommended"],
    "ratings": ["absolute maximum", "maximum ratings", "recommended operating", "electrical characteristics"],
    "layout": ["layout", "pcb", "guideline", "placement", "routing"],
    "package": ["package", "dimension", "land pattern", "footprint", "outline"],
}


def _get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def pdf_url(lcsc):
    html = _get(f"https://www.lcsc.com/datasheet/{lcsc}.pdf")
    m = re.search(r"https://datasheet\.lcsc\.com/datasheet/pdf/[0-9a-f]+\.pdf\?productCode=" + lcsc, html)
    if not m:
        m = re.search(r"https://[^\"' ]+\.pdf", html.replace("\\u002F", "/"))
    return m.group(0) if m else None


def pages(lcsc):
    """List of page texts (cached)."""
    lcsc = catalog.lcsc_id(lcsc)
    d = os.path.join(ROOT, lcsc)
    cache = os.path.join(d, "pages.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    url = pdf_url(lcsc)
    if not url:
        raise RuntimeError(f"no datasheet PDF found for {lcsc}")
    os.makedirs(d, exist_ok=True)
    pdf = os.path.join(d, "datasheet.pdf")
    with open(pdf, "wb") as f:
        f.write(_get(url, binary=True))
    from pypdf import PdfReader
    out = []
    for p in PdfReader(pdf).pages[:80]:
        try:
            t = p.extract_text() or ""
        except Exception:                                   # noqa: BLE001
            t = ""
        out.append(re.sub(r"[ \t]+", " ", t).strip())
    with open(cache, "w") as f:
        json.dump(out, f)
    return out


def lookup(lcsc, query="", max_chars=7000):
    """The pages most relevant to `query` (or a topic name), always including page 1, each cited by number."""
    ps = pages(lcsc)
    if not ps:
        return "datasheet has no extractable text (scanned PDF)"
    words = []
    for k, v in TOPICS.items():
        if k in query.lower():
            words += v
    words += re.findall(r"[a-z0-9]{3,}", query.lower())
    scores = []
    for i, t in enumerate(ps):
        low = t.lower()
        scores.append((sum(low.count(w) for w in words) if words else 0, -i, i))
    chosen = [0] + [i for s, _, i in sorted(scores, reverse=True) if i != 0 and s > 0][:6]
    out, used = [], 0
    for i in chosen:
        chunk = ps[i][: max(800, max_chars // max(1, len(chosen)))]
        if used + len(chunk) > max_chars:
            break
        out.append(f"--- page {i + 1} ---\n{chunk}")
        used += len(chunk)
    return f"Datasheet {lcsc} ({len(ps)} pages). Relevant pages for '{query}':\n" + "\n".join(out)
