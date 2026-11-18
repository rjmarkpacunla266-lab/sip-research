"""citeauthors.py - the "Authors" option for citations: Automatic / Show all authors / Shorten with et al.

  auto  - exactly what each style's own rules do (nothing changes; this is the default)
  all   - every author is listed, in every style
  etal  - first author + "et al." when the paper has 3 or more authors (2 authors are still both shown)

It works on the finished citation text: it finds the author part at the start of what the normal builder
produced and swaps only that part. If the start of a citation does not look as expected, the citation is
returned untouched, so a citation can never come out broken. Rule-based, no AI."""

STYLES = ("apa", "mla", "chicago", "harvard")
MODES = ("auto", "all", "etal")

def _c():
    import core
    return core

def _list(style, ppl, mode):
    """The author text a style prints for these people, for 'all' or 'etal'. None = nothing to change."""
    c = _c()
    n = len(ppl)
    if n == 0:
        return None
    if style in ("apa", "harvard"):
        names = [c._inverted(p) for p in ppl]
        if n == 1:
            return names[0]
        if mode == "etal":
            return None if n == 2 else names[0] + (", et al." if style == "apa" else " et al.")
        if style == "apa":
            return ", ".join(names[:-1]) + ", & " + names[-1]
        return ", ".join(names[:-1]) + " and " + names[-1]
    first = c._inverted(ppl[0], full=True)
    if n == 1:
        return first
    if mode == "etal":
        return None if n == 2 else first + ", et al."
    names = [first] + [c._natural(p) for p in ppl[1:]]
    return f"{names[0]}, and {names[1]}" if n == 2 else ", ".join(names[:-1]) + ", and " + names[-1]

def _auto(style, ppl):
    """The author text the normal builders print (same rules as core.py)."""
    c = _c()
    n = len(ppl)
    if n == 0:
        return None
    if style == "apa":
        names = [c._inverted(p) for p in ppl]
        if n == 1: return names[0]
        if n <= 20: return ", ".join(names[:-1]) + ", & " + names[-1]
        return ", ".join(names[:19]) + ", … " + names[-1]
    if style == "harvard":
        names = [c._inverted(p) for p in ppl]
        if n == 1: return names[0]
        if n <= 3: return ", ".join(names[:-1]) + " and " + names[-1]
        return names[0] + " et al."
    if style == "mla":
        if n == 1: return c._inverted(ppl[0], full=True)
        if n == 2: return f"{c._inverted(ppl[0], full=True)}, and {c._natural(ppl[1])}"
        return f"{c._inverted(ppl[0], full=True)}, et al"
    shown = ppl if n <= 10 else ppl[:7]
    names = [c._inverted(shown[0], full=True)] + [c._natural(p) for p in shown[1:]]
    if n > 10: return ", ".join(names) + ", et al."
    if len(names) == 1: return names[0]
    if len(names) == 2: return f"{names[0]}, and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]

def apply(cites, authors, mode):
    """cites = {'apa':..., 'mla':..., 'chicago':..., 'harvard':...}. Returns a (possibly) changed copy."""
    if mode not in ("all", "etal") or not isinstance(cites, dict):
        return cites
    try:
        c = _c()
        ppl = c._people(authors if isinstance(authors, list) else [])
        out = dict(cites)
        for style in STYLES:
            if style in (cites.get("ref_done") or ()):
                continue                                  # built from the school guide with the Authors option already applied
            base = cites.get(style)
            old, new = _auto(style, ppl), _list(style, ppl, mode)
            if not base or old is None or new is None:
                continue
            if style == "harvard":
                a, b = old, new
            else:
                a, b = c._dot(old), c._dot(new)
            if a != b and base.startswith(a):
                out[style] = b + base[len(a):]
                if style == "apa" and isinstance(out.get("apa_html"), str):
                    import html as _hh
                    ha, hb = _hh.escape(a, quote=False), _hh.escape(b, quote=False)
                    if out["apa_html"].startswith(ha):
                        out["apa_html"] = hb + out["apa_html"][len(ha):]
        return out
    except Exception:
        return cites
