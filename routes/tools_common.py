"""routes/tools_common.py: pieces shared by the three tools (Answer Finder, Source Tracer, Topic Finder).
Only put something here when two or more tools need it. Everything specific to one tool stays in that tool's own file."""
import re as _re
import traceback
import requests
import xml.etree.ElementTree as ET
from flask import Blueprint, jsonify
from core import login_required, format_paper
from engine import SOURCE_LABELS

# ONE blueprint for all three tools, so URLs and endpoint names stay exactly as they were (tools.answer_finder, ...).
tools_bp = Blueprint("tools", __name__)


def _fail(what):
    traceback.print_exc()
    return jsonify({"error": f"{what} failed. Please try again."}), 500

def _pick(p, abstract_chars=0):
    """The fields a tool page needs from a paper (keeps responses small)."""
    out = {k: p.get(k) for k in ("title", "authors", "year", "date", "journal", "doi", "oa_url",
                                 "is_oa", "citations", "data_source", "apa_reference", "openalex_id")}
    out["authors"] = (out.get("authors") or [])[:6]
    out["citations"] = out.get("citations") or 0
    if abstract_chars:
        a = p.get("abstract") or ""
        out["abstract"] = a[:abstract_chars] + ("…" if len(a) > abstract_chars else "")
    return out


# ── used by more than one tool ──
def _clean_abstract(text):
    """Remove HTML/JATS tags and LaTeX leftovers such as \\latin{et al.}."""
    t = _re.sub(r"<[^>]+>", " ", text or "")
    t = _re.sub(r"\\[A-Za-z]+\s*\{([^{}]*)\}", r"\1", t)
    t = t.replace("\\", " ")
    return _re.sub(r"\s+", " ", t).strip()


def _pubmed_abstracts(papers):
    """PubMed is not used any more; kept so the tools keep working unchanged."""
    return


_DOI_OK = _re.compile(r"^10\.\d{4,9}/[^\s\"<>]+$")


def _client_paper(p):
    """The page sends back the papers it was given; keep only plain, expected fields."""
    out = {}
    for k in ("title", "journal", "doi", "oa_url", "data_source", "apa_reference", "openalex_id", "date"):
        v = p.get(k)
        out[k] = v[:400] if isinstance(v, str) else None
    y = p.get("year")
    out["year"] = y if isinstance(y, (int, str)) and not isinstance(y, bool) else None
    c = p.get("citations")
    out["citations"] = c if isinstance(c, int) and not isinstance(c, bool) else 0
    out["authors"] = [a[:80] for a in (p.get("authors") or []) if isinstance(a, str)][:6]
    out["is_oa"] = True
    return out
