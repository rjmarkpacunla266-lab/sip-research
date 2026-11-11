#!/usr/bin/env python3
"""install_seo4.py - run from the project folder (~/W):  python3 install_seo4.py
Adds the keyword study to the site, using only things Sturch really does:
  * titles, descriptions and keywords for Search, Ask a Question, Quote Finder, Topic Ideas, Guide, Landing
  * brand-name variants (student research, study research, studies research, stu rch) in meta keywords,
    the structured data and one plain sentence on the landing page
  * a "Free research tools for students and teachers" section + 15 tags on the landing page
  * 8 more FAQ answers + matching FAQPage structured data
Safe to run twice (it skips what is already there). Your own edits elsewhere are kept."""
import os, re, json, html

def read(p): return open(p, encoding="utf-8").read()
def write(p, t): open(p, "w", encoding="utf-8", newline="").write(t)
esc = lambda s: html.escape(s, quote=True)

def set_meta(t, attr, key, value):
    pat = r'(<meta\s+%s="%s"\s+content=")[^"]*(")' % (attr, re.escape(key))
    return re.sub(pat, lambda m: m.group(1) + esc(value) + m.group(2), t, count=1)

def set_title(t, title):
    t = re.sub(r"<title>.*?</title>", lambda m: "<title>%s</title>" % esc(title), t, count=1, flags=re.S)
    t = set_meta(t, "property", "og:title", title)
    t = set_meta(t, "name", "twitter:title", title)
    return t

BRAND = "sturch, stu rch, student research, study research, studies research, student research tool"

PAGES = {
 "index.html": dict(
    title="Search Academic Papers: Free Research Paper Finder | Sturch",
    desc="Free academic paper search for students. Find papers from OpenAlex, arXiv and Crossref, open the original article, and cite in APA, MLA, Chicago or Harvard.",
    kw="free academic paper search, research paper finder, scholarly article finder, find academic papers by title, search research papers by author, search open access research articles, RRL finder, find related literature, free scholarly search engine for students, google scholar alternative, " + BRAND),
 "answer.html": dict(
    title="Ask a Question: Answers From Real Research Papers | Sturch",
    desc="Ask a research question and see sentences from real papers that answer it, with the source for each. Free, no AI, no made-up answers.",
    kw="ask a research question, find answers in research papers, research question tool, evidence from real papers, no AI research tool, find academic sources for a statement, " + BRAND),
 "source_tracer.html": dict(
    title="Quote Finder: Find the Source of a Quote | Sturch",
    desc="Find the source of a quote. Paste a quote or sentence and Quote Finder looks for the research paper it most likely came from. Free, real papers, no AI.",
    kw="quote finder, find the source of a quote, research quote finder, find academic papers from a quotation, find research paper by sentence, find a citation for a quote, identify the original source of a quote, " + BRAND),
 "topic_finder.html": dict(
    title="Topic Ideas: Research Topic Ideas for Students | Sturch",
    desc="Research topic ideas for students, SIP and thesis. See recent and highly cited real papers in your field to help you choose a topic. Free, no AI.",
    kw="research topic ideas, research topic generator for students, research topic ideas for senior high school, SIP research ideas, science investigatory project topic ideas, quantitative research topics, practical research topic ideas, research ideas based on existing studies, " + BRAND),
 "guide.html": dict(
    kw="how to use sturch, how to find research papers for free, how to find related literature, how to find credible sources, how to cite a journal article in APA 7, RRL finder, SIP research help, " + BRAND),
}

LANDING_KW = ("free research article multi-tool, free research tools for students, research tools for senior high school students, "
 "research tools for Filipino students, free research website Philippines, google scholar alternative, free google scholar alternative for students, "
 "free academic paper search, research paper finder, free APA 7 citation generator, MLA citation generator, Chicago citation generator, Harvard reference generator, "
 "citation tool without email, find the source of a quote, research topic ideas for students, RRL finder, SIP research help, thesis research tools, "
 "no AI research tool, no email needed, " + BRAND)

TAGS = ["Academic Research", "Paper Search", "APA 7", "MLA Citations", "Chicago Style", "Harvard Referencing",
        "RRL Finder", "SIP Research", "Thesis Help", "Quote Finder", "Research Topics", "Find Papers",
        "Free Research Tools", "Student Resources", "Saved Papers"]

FAQ = [
 ("What does Sturch mean?",
  "Sturch is short for “student research”. People also search for it as study research, studies research or stu rch. It is a free research article multi-tool made by a student in the Philippines."),
 ("Is Sturch good for senior high school and college research?",
  "Yes. Students use it to find papers for a thesis, a practical research subject or a science investigatory project (SIP), then open the original article, copy the citation and save the paper to a collection. It does not write your research for you, and your teacher or adviser decides what sources are accepted."),
 ("How do I find related literature (RRL) for my research?",
  "Search your topic in Search Papers, use the filters to narrow by year or open access, open the original article to read it, and save useful papers to a collection. Sturch finds real papers. You still need to read them and decide which are relevant."),
 ("How do I cite a journal article in APA, MLA, Chicago or Harvard?",
  "Search for the paper, tap its citation button, choose APA, MLA, Chicago or Harvard, and copy the reference. APA follows the 7th edition journal-article format. References are made from paper data and can miss details, so check them against the original article before you submit."),
 ("How do I find the source of a quote?",
  "Paste the quote or sentence into Quote Finder. It looks for the paper it most likely came from and shows the match. If the quote is paraphrased or the paper is not indexed, it may not be found. Check the original article for the full context."),
 ("Can I get research topic ideas?",
  "Yes. Topic Ideas shows recent and highly cited papers for your subject and level so you can see what has already been studied. These are ideas, not approved topics. Ask your teacher or adviser before you commit."),
 ("Do I need an email to use Sturch?",
  "No email is needed. You can continue as a guest, or sign up with just a username and password. A guest account keeps saved papers on that browser for a limited time, so create a free account if you want to keep them longer."),
 ("Does Sturch use AI?",
  "No. Sturch uses fixed rules to search papers, match sentences and build citations. Real papers, real sources, no AI."),
]

SECTION_MARK = 'id="for-students"'
LD_MARK = 'id="sturch-faq-ld"'
CSS_MARK = "/* seo4 */"

def section_html():
    tags = "".join("<span>%s</span>" % esc(x) for x in TAGS)
    return f'''<section class="s" {SECTION_MARK}>
  <h2>Free research tools for students and teachers</h2>
  <p class="sub">Sturch (short for student research, also searched as study research, studies research or stu rch) is a free research article multi-tool made by a student in the Philippines. It is built for senior high school and college students, and for teachers who guide them.</p>
  <ul class="stu-list">
    <li><a href="/guide#search"><b>Search papers</b></a> from OpenAlex, arXiv and Crossref, then open the original article.</li>
    <li><a href="/guide#answer"><b>Ask a Question</b></a> and see sentences from real papers that answer it.</li>
    <li><a href="/guide#tracer"><b>Quote Finder</b></a> looks for the research paper a quote most likely came from.</li>
    <li><a href="/guide#topic"><b>Topic Ideas</b></a> for thesis, practical research and SIP, based on real papers.</li>
    <li><b>Citations</b> in APA 7, MLA, Chicago and Harvard, plus saved papers and collections.</li>
  </ul>
  <p class="sub" style="margin-top:12px">No email needed: continue as a guest. Real papers, real sources, no AI.</p>
  <div class="tagrow" aria-label="Topics">{tags}</div>
</section>

'''

def faq_html():
    return "".join(
        '  <details>\n    <summary>%s</summary>\n    <p>%s</p>\n  </details>\n' % (esc(q), esc(a)) for q, a in FAQ)

def faq_ld():
    data = {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": q,
                            "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ]}
    body = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    return '<script type="application/ld+json" %s>\n%s\n</script>\n' % (LD_MARK, body)

CSS = CSS_MARK + """
.stu-list{margin:12px 0 0 18px;display:grid;gap:6px;font-size:.92rem}
.stu-list li{color:var(--muted)} .stu-list a{color:var(--accent)}
.tagrow{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.tagrow span{padding:6px 12px;border:1px solid var(--border);background:var(--surf);border-radius:999px;font-size:.8rem;color:var(--muted)}
"""

done, warn = [], []

# ---- the tool pages and guide -------------------------------------------------
for fn, cfg in PAGES.items():
    p = os.path.join("templates", fn)
    if not os.path.isfile(p):
        print("skipped (not found):", p); continue
    t = t0 = read(p)
    if "title" in cfg:
        t = set_title(t, cfg["title"])
    if "desc" in cfg:
        for attr, key in (("name", "description"), ("property", "og:description"), ("name", "twitter:description")):
            t = set_meta(t, attr, key, cfg["desc"])
    t2 = set_meta(t, "name", "keywords", cfg["kw"])
    if t2 == t and 'name="keywords"' not in t:
        warn.append(fn + ": no keywords meta tag to update")
    t = t2
    if t != t0:
        write(p, t); done.append(fn)
    if "desc" in cfg and len(cfg["desc"]) > 160:
        warn.append("%s: description is %d characters (Google may cut it)" % (fn, len(cfg["desc"])))

# ---- landing ------------------------------------------------------------------
p = os.path.join("templates", "landing.html")
if os.path.isfile(p):
    t = t0 = read(p)
    t = set_meta(t, "name", "keywords", LANDING_KW)
    # brand-name variants in structured data
    t = t.replace('"alternateName": "Sturch Free Research Article Multi-Tool",',
                  '"alternateName": ["Sturch Free Research Article Multi-Tool", "Student Research", "Study Research", "Studies Research", "Stu Rch"],', 1)
    # section + tags (before the FAQ)
    if SECTION_MARK not in t:
        if '<section class="s faq" id="faq">' in t:
            t = t.replace('<section class="s faq" id="faq">', section_html() + '<section class="s faq" id="faq">', 1)
        else:
            warn.append("landing.html: FAQ section not found, student section not added")
    # css
    if CSS_MARK not in t and "</style>" in t:
        t = t.replace("</style>", CSS + "</style>", 1)
    # FAQ answers (visible) + FAQPage data
    if LD_MARK not in t:
        marker = "</details>\n</section>\n\n<div class=\"cta\">"
        if marker in t:
            t = t.replace(marker, "</details>\n" + faq_html() + "</section>\n\n<div class=\"cta\">", 1)
            t = t.replace("<script>\n(function(){try{var t=localStorage.getItem('sturch_theme');",
                          faq_ld() + "<script>\n(function(){try{var t=localStorage.getItem('sturch_theme');", 1)
        else:
            warn.append("landing.html: FAQ end not found, FAQ answers not added (FAQ data skipped so it matches the page)")
    if t != t0:
        write(p, t); done.append("landing.html")
else:
    print("skipped (not found):", p)

print("Updated:", ", ".join(done) or "nothing (already done?)")
for w in warn: print("  warning:", w)
