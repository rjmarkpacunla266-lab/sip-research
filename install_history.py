#!/usr/bin/env python3
"""install_history.py - run from the project folder (~/W):  python3 install_history.py
Search Papers: a History list under the filters (like the chat list in ChatGPT). It shows your recent searches, newest first.
Tap one to search it again with the filters it was used with, tap x to remove one, Clear to remove all. It updates after each search.
It is part of the Advanced layout (next to the filters). Safe to run twice."""
import os, sys
CSS = (".hist{margin-top:12px;background:var(--surf);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}\n"
 ".hist-head{display:flex;align-items:center;gap:8px;padding:0 6px 0 13px;min-height:46px}\n"
 ".hist-title{flex:1;display:flex;align-items:center;gap:8px;min-height:46px;border:0;background:none;color:var(--text);font:600 .86rem var(--sans);cursor:pointer;text-align:left;padding:0}\n"
 ".hist-caret{margin-left:auto;color:var(--muted);transition:transform .15s}\n.hist.open .hist-caret{transform:rotate(180deg)}\n"
 ".hist-clear{min-height:40px;border:0;background:none;color:var(--accent);font:600 .8rem var(--sans);padding:0 8px;cursor:pointer}\n"
 ".hist-body{display:none;border-top:1px solid var(--border);padding:4px 6px 8px}\n.hist.open .hist-body{display:block}\n"
 ".hist-row{display:flex;align-items:center;gap:4px;border-radius:9px}\n.hist-row:hover{background:var(--surf2)}\n"
 ".hist-q{flex:1;min-width:0;display:block;padding:0 8px;min-height:44px;line-height:44px;color:var(--text);font-size:.88rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-decoration:none}\n"
 ".hist-t{flex:none;font-size:.7rem;color:var(--muted)}\n"
 ".hist-x{flex:none;width:40px;height:40px;border:0;background:none;color:var(--muted);font-size:.95rem;border-radius:50%;cursor:pointer}\n.hist-x:hover{color:var(--red,#c93a3a);background:var(--surf3,var(--surf2))}\n"
 ".hist-empty{color:var(--muted);font-size:.84rem;padding:10px 8px}\n"
 ".hist-all{display:inline-flex;align-items:center;min-height:40px;padding:0 8px;color:var(--accent);font-weight:600;font-size:.84rem;text-decoration:none}\n")
HTML = ('    <div class="hist" id="histBox">\n'
 '      <div class="hist-head">\n'
 '        <button type="button" class="hist-title" id="histHead" onclick="histToggle()" aria-expanded="false">🕐 History <span class="hist-caret">▾</span></button>\n'
 '        <button type="button" class="hist-clear" id="histClear" onclick="histClearAll()" hidden>Clear</button>\n'
 '      </div>\n'
 '      <div class="hist-body"><div id="histList"><p class="hist-empty">Loading…</p></div><a class="hist-all" href="/history">View all →</a></div>\n'
 '    </div>\n')
JS = r"""
/* ── History list under the filters ── */
var _histRows = [], _histShown = [];
function histAgo(iso) {
  if (!iso) return ''; let t = Date.parse(iso); if (isNaN(t)) { t = Date.parse(iso + 'Z'); if (isNaN(t)) return ''; }
  const s = Math.max(0, (Date.now() - t) / 1000); if (s < 60) return 'now'; const m = s / 60; if (m < 60) return Math.floor(m) + 'm';
  const h = m / 60; if (h < 24) return Math.floor(h) + 'h'; const d = h / 24; if (d < 30) return Math.floor(d) + 'd';
  const mo = d / 30; return mo < 12 ? Math.floor(mo) + 'mo' : Math.floor(d / 365) + 'y';
}
function histHref(r) {
  const f = (r && r.filters) || {}; let u = '/search?tag=' + encodeURIComponent((r && r.query) || '');
  if (f.year_from) u += '&yf=' + encodeURIComponent(f.year_from);
  if (f.year_to) u += '&yt=' + encodeURIComponent(f.year_to);
  if (f.oa) u += '&oa=1';
  if (['match', 'cited', 'recent'].includes(f.sort)) u += '&sort=' + f.sort;
  if (['both', 'title', 'abstract', 'fulltext'].includes(f.field)) u += '&field=' + f.field;
  if (Array.isArray(f.sources) && f.sources.length) u += '&src=' + encodeURIComponent(f.sources.join(','));
  return u;
}
function histRender() {
  const box = document.getElementById('histList'); if (!box) return;
  const seen = {}, out = [];
  _histRows.forEach(r => {
    const q = String(r.query || '').trim(); if (!q) return; const k = q.toLowerCase();
    if (seen[k]) { seen[k].ids.push(r.id); return; }
    seen[k] = {q: q, r: r, ids: [r.id]}; out.push(seen[k]);
  });
  _histShown = out.slice(0, 12);
  document.getElementById('histClear').hidden = !_histShown.length;
  box.innerHTML = _histShown.length ? _histShown.map((o, i) =>
    '<div class="hist-row"><a class="hist-q" href="' + esc(histHref(o.r)) + '" title="' + esc(o.q) + '">' + esc(o.q) + '</a>' +
    '<span class="hist-t">' + esc(histAgo(o.r.searched_at)) + '</span>' +
    '<button type="button" class="hist-x" data-i="' + i + '" aria-label="Remove from history">✕</button></div>').join('')
    : '<p class="hist-empty">Your searches will show up here.</p>';
}
function loadHistory() {
  fetch('/api/history').then(r => r.ok ? r.json() : Promise.reject()).then(rows => { _histRows = Array.isArray(rows) ? rows : []; histRender(); })
    .catch(() => { const b = document.getElementById('histList'); if (b && !_histRows.length) b.innerHTML = '<p class="hist-empty">Could not load your history.</p>'; });
}
function histToggle() {
  const h = document.getElementById('histBox'), o = h.classList.toggle('open');
  document.getElementById('histHead').setAttribute('aria-expanded', String(o));
}
function histClearAll() {
  if (!confirm('Clear your whole search history?')) return;
  fetch('/api/history', {method: 'DELETE'}).then(r => { if (r.ok) { _histRows = []; histRender(); } }).catch(() => {});
}
document.getElementById('histList').addEventListener('click', e => {
  const b = e.target.closest('.hist-x'); if (!b) return;
  const o = _histShown[parseInt(b.dataset.i, 10)]; if (!o) return;
  const ids = o.ids.filter(x => x != null).map(String); if (!ids.length) return;
  fetch('/api/history/delete', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: ids})})
    .then(r => { if (r.ok) { _histRows = _histRows.filter(x => !ids.includes(String(x.id))); histRender(); } }).catch(() => {});
});
if (window.innerWidth >= 900) { document.getElementById('histBox').classList.add('open'); document.getElementById('histHead').setAttribute('aria-expanded', 'true'); }
if (typeof doSearch === 'function') {
  const _histDs = doSearch;
  doSearch = async function (p) { const r = await _histDs.apply(this, arguments); if (!p || p === 1) setTimeout(loadHistory, 400); return r; };
}
loadHistory();
"""
ESC = "function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');}\n"
PAIRS = [
 (".sbtn-wide:hover{background:var(--accent-h)}\n", ".sbtn-wide:hover{background:var(--accent-h)}\n" + CSS),
 ("  body.adv .side{position:sticky;top:76px;margin-top:6px}\n", "  body.adv .side{position:sticky;top:76px;margin-top:6px;max-height:calc(100vh - 90px);overflow-y:auto}\n"),
 ("    </div>\n  </aside>\n", "    </div>\n" + HTML + "  </aside>\n"),
 (ESC, ESC + JS),
]
def find(name):
    for c in (name, os.path.join("templates", name)):
        if os.path.isfile(c): return c
p = find("index.html")
if not p: print("index.html not found. Run this from the project folder (~/W)."); sys.exit(1)
t = open(p, encoding="utf-8").read(); n, bad = 0, []
for old, new in PAIRS:
    if new in t or (new.endswith(old) and new[:-len(old)] in t): continue
    if t.count(old) == 1: t = t.replace(old, new, 1); n += 1
    else: bad.append(old[:60].replace("\n", " "))
if bad: print("index.html left alone, did not match:", " | ".join(bad)); sys.exit(1)
if n: open(p + ".new", "w", encoding="utf-8", newline="").write(t); os.replace(p + ".new", p)
print("Updated:", p if n else "nothing (already done?)")
