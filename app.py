#!/usr/bin/env python3
"""Reel-DB web UI. FTS prefilter -> JEV rerank. Tailnet-exposed."""
import os, sqlite3, json, re, urllib.request
from flask import Flask, request, render_template, jsonify

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
TS_KEY = os.environ.get("TYPESAFE_API_KEY", "")
TS_URL = "https://api.typesafe.ai/v1/systemone"
PREFILTER_N = 40
TOP_N = 12
app = Flask(__name__, static_folder="static", static_url_path="/static")
PLAYBOOKS_DIR = os.path.join(HERE, "playbooks")
PLAYBOOK_FILES = {
    "ai_tools": ("AI Tools", "ai_tools_cheatsheet.md"),
    "productivity": ("Productivity", "productivity_cheatsheet.md"),
    "sales": ("Sales", "sales_cheatsheet.md"),
    "marketing": ("Marketing", "marketing_cheatsheet.md"),
    "business": ("Business", "business_cheatsheet.md"),
}
DIGEST_SCRIPT = os.path.join(HERE, "random_picks.py")


@app.route("/sw.js")
def kill_sw():
    # Kill-switch: any previously-registered service worker for this origin
    # fetches this, unregisters itself, nukes its caches, and forces a reload
    # so stale cached API responses stop shadowing the live app.
    js = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map(k => caches.delete(k)));
    const regs = await self.registration.unregister();
    const clientsList = await self.clients.matchAll({type:'window'});
    clientsList.forEach(c => c.navigate(c.url));
  })());
});
"""
    return js, 200, {"Content-Type": "application/javascript"}


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Reel Memory",
        "short_name": "Reel Memory",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#121212",
        "theme_color": "#121212",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

STOP = {"the","a","an","that","this","about","with","for","of","to","in","on","i",
        "me","my","reel","video","find","was","is","it","and","or","how","what"}


def fts_query(q):
    words = re.findall(r"[a-zA-Z0-9]+", q.lower())
    terms = [w for w in words if w not in STOP and len(w) > 2] or words
    return " OR ".join(terms)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def jev_rerank(query, cands):
    if not TS_KEY or not cands:
        return [None] * len(cands)
    state, questions = [], {}
    for i, c in enumerate(cands):
        state.append({"author": c["username"],
                      "caption": (c["caption"] or "")[:600],
                      "transcript": (c["transcript"] or "")[:600]})
        questions[f"q{i}"] = {"type": "noul", "instructions": {
            "search_query": query,
            "question": (f"Is the saved Instagram reel at `state[{i}]` what the user is "
                         f"looking for when they search `search_query`? Judge relevance to "
                         f"their intent, not just keyword overlap.")}}
    payload = json.dumps({"state": state, "model": "jev-latest",
                          "questions": questions}).encode()
    req = urllib.request.Request(TS_URL, data=payload, headers={
        "Authorization": f"Bearer {TS_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ans = json.load(r).get("answers", {})
        return [ans.get(f"q{i}", {}).get("noul") for i in range(len(cands))]
    except Exception:
        return [None] * len(cands)


@app.route("/")
def home():
    con = db()
    stats = dict(con.execute("SELECT transcribe_status, COUNT(*) FROM reels GROUP BY transcribe_status").fetchall())
    con.close()
    return render_template("index.html", stats=stats)


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    use_jev = request.args.get("jev", "1") != "0"
    if not q:
        return jsonify(results=[])
    con = db()
    rows = con.execute("""
        SELECT r.shortcode, r.url, r.username, r.caption, r.transcript,
               r.transcribe_status, r.timestamp, r.viewed_at, r.comment, bm25(reels_fts) AS score
        FROM reels_fts JOIN reels r ON r.shortcode=reels_fts.shortcode
        WHERE reels_fts MATCH ? ORDER BY score LIMIT ?""",
        (fts_query(q), PREFILTER_N)).fetchall()
    con.close()
    scores = jev_rerank(q, rows) if use_jev else [None] * len(rows)
    packed = sorted(zip(rows, scores),
                    key=lambda x: (x[1] if x[1] is not None else -1), reverse=True)
    out = []
    for r, s in packed[:TOP_N]:
        out.append({"id": r["shortcode"], "url": r["url"], "user": r["username"],
                    "caption": r["caption"] or "", "transcript": r["transcript"] or "",
                    "status": r["transcribe_status"],
                    "date": r["timestamp"],
                    "viewed": bool(r["viewed_at"]),
                    "comment": r["comment"] or "",
                    "relevance": round(s * 100) if s is not None else None})
    return jsonify(results=out, jev=use_jev)


CAT_LABELS = {
    "ai_tools": "🤖 AI Tools", "coding_dev": "💻 Coding & Dev",
    "design_creative": "🎨 Design & Creative", "sales": "🤝 Sales",
    "marketing": "📣 Marketing", "business": "💼 Business",
    "productivity": "📈 Productivity", "entertainment": "🎭 Entertainment",
    "other": "📦 Other",
    "health_fitness": "🏋️ Health & Fitness",
}


@app.route("/browse")
def browse():
    cat = request.args.get("cat")
    con = db()
    counts = dict(con.execute(
        "SELECT category, COUNT(*) FROM reels WHERE category IS NOT NULL GROUP BY category").fetchall())
    q = ("SELECT shortcode,url,username,caption,transcript,category,timestamp,viewed_at,comment FROM reels "
         "WHERE category IS NOT NULL")
    params = []
    if cat and cat != "all":
        q += " AND category=?"; params.append(cat)
    q += " ORDER BY transcribed_at DESC"
    rows = con.execute(q, params).fetchall()
    con.close()
    cats = [{"key": k, "label": CAT_LABELS.get(k, k), "count": counts[k]}
            for k in sorted(counts, key=lambda x: -counts[x])]
    out = [{"id": r["shortcode"], "url": r["url"], "user": r["username"], "caption": r["caption"] or "",
            "transcript": r["transcript"] or "", "status": "done",
            "date": r["timestamp"],
            "viewed": bool(r["viewed_at"]),
            "comment": r["comment"] or "",
            "category": CAT_LABELS.get(r["category"], r["category"])} for r in rows]
    return jsonify(cats=cats, total=sum(counts.values()), results=out)


@app.route("/stats")
def stats():
    con = db()
    by = dict(con.execute("SELECT transcribe_status, COUNT(*) FROM reels GROUP BY transcribe_status").fetchall())
    total_reels = con.execute("SELECT COUNT(*) FROM reels WHERE media_type='reel'").fetchone()[0]
    con.close()
    done = by.get("done", 0)
    return jsonify(done=done, pending=by.get("pending", 0), error=by.get("error", 0),
                   total_reels=total_reels,
                   pct=round(done / total_reels * 100, 1) if total_reels else 0)


@app.route("/digest")
def digest():
    import subprocess
    n = request.args.get("n", "10")
    try:
        result = subprocess.run(
            ["python3", DIGEST_SCRIPT, n],
            capture_output=True, text=True, timeout=20, cwd=HERE
        )
        picks = json.loads(result.stdout)
    except Exception as e:
        return jsonify(error=str(e), picks=[]), 500
    return jsonify(picks=picks)


@app.route("/mark_viewed/<shortcode>", methods=["POST"])
def mark_viewed(shortcode):
    import time as _time
    con = db()
    con.execute("UPDATE reels SET viewed_at=? WHERE shortcode=? AND viewed_at IS NULL",
                (int(_time.time()), shortcode))
    con.commit()
    con.close()
    return jsonify(ok=True)


@app.route("/comment/<shortcode>", methods=["POST"])
def set_comment(shortcode):
    body = request.get_json(silent=True) or {}
    text = (body.get("comment") or "").strip()[:2000]
    con = db()
    con.execute("UPDATE reels SET comment=? WHERE shortcode=?", (text or None, shortcode))
    con.commit()
    con.close()
    return jsonify(ok=True, comment=text)


@app.route("/playbooks")
def playbooks_list():
    out = []
    for key, (label, fname) in PLAYBOOK_FILES.items():
        fpath = os.path.join(PLAYBOOKS_DIR, fname)
        exists = os.path.exists(fpath)
        out.append({"key": key, "label": label, "available": exists})
    return jsonify(playbooks=out)


@app.route("/playbook/<key>")
def playbook_detail(key):
    if key not in PLAYBOOK_FILES:
        return jsonify(error="unknown playbook"), 404
    label, fname = PLAYBOOK_FILES[key]
    fpath = os.path.join(PLAYBOOKS_DIR, fname)
    if not os.path.exists(fpath):
        return jsonify(error="not generated yet"), 404
    with open(fpath, "r") as f:
        content = f.read()
    return jsonify(key=key, label=label, markdown=content)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5005)
