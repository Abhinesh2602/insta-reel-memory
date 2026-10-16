#!/usr/bin/env python3
"""Phase 2: search the reel DB. FTS5 prefilter -> JEV rerank -> top hits.
Usage: python3 search.py "that reel about the opus prompt library"
       python3 search.py --no-jev "keyword only"
"""
import sqlite3, os, sys, json, re, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
TS_KEY = os.environ.get("TYPESAFE_API_KEY", "")
TS_URL = "https://api.typesafe.ai/v1/systemone"
PREFILTER_N = 40
TOP_N = 8


def fts_query(q):
    """Turn a natural phrase into a forgiving FTS5 OR query."""
    words = re.findall(r"[a-zA-Z0-9]+", q.lower())
    stop = {"the","a","an","that","this","about","with","for","of","to","in",
            "on","i","me","my","reel","video","find","was","is","it","and","or"}
    terms = [w for w in words if w not in stop and len(w) > 2]
    if not terms:
        terms = words
    return " OR ".join(terms)


def search_fts(con, q, limit=PREFILTER_N):
    ftsq = fts_query(q)
    rows = con.execute("""
        SELECT r.shortcode, r.url, r.username, r.caption, r.transcript,
               bm25(reels_fts) AS score
        FROM reels_fts JOIN reels r ON r.shortcode = reels_fts.shortcode
        WHERE reels_fts MATCH ?
        ORDER BY score LIMIT ?""", (ftsq, limit)).fetchall()
    return rows


def jev_rerank(query, candidates):
    """Score each candidate's relevance to the query with JEV Noul. Parallel questions."""
    if not TS_KEY or not candidates:
        return {i: None for i in range(len(candidates))}
    state = []
    questions = {}
    for i, c in enumerate(candidates):
        state.append({
            "author": c[2],
            "caption": (c[3] or "")[:600],
            "transcript": (c[4] or "")[:600],
        })
        questions[f"q{i}"] = {
            "type": "noul",
            "instructions": {
                "search_query": query,
                "question": (f"Is the saved Instagram reel at `state[{i}]` what the user is "
                             f"looking for when they search `search_query`? Judge relevance to "
                             f"their intent, not just keyword overlap."),
            },
        }
    payload = json.dumps({"state": state, "model": "jev-latest",
                          "questions": questions}).encode()
    req = urllib.request.Request(TS_URL, data=payload, headers={
        "Authorization": f"Bearer {TS_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ans = json.load(r).get("answers", {})
        return {i: ans.get(f"q{i}", {}).get("noul") for i in range(len(candidates))}
    except Exception as e:
        sys.stderr.write(f"[jev rerank failed: {e}]\n")
        return {i: None for i in range(len(candidates))}


def main():
    args = sys.argv[1:]
    use_jev = True
    if "--no-jev" in args:
        use_jev = False
        args.remove("--no-jev")
    query = " ".join(args).strip()
    if not query:
        print("usage: search.py [--no-jev] <query>"); return
    con = sqlite3.connect(DB)
    cands = search_fts(con, query)
    if not cands:
        print("No matches. (transcription still in progress; try broader words)"); return
    scores = jev_rerank(query, cands) if use_jev else {i: None for i in range(len(cands))}
    ranked = sorted(enumerate(cands),
                    key=lambda x: (scores[x[0]] if scores[x[0]] is not None else -1),
                    reverse=True)
    print(f"\nTop results for: {query!r}\n" + "=" * 60)
    for rank, (i, c) in enumerate(ranked[:TOP_N], 1):
        sc = scores[i]
        relev = f"{sc:.0%}" if sc is not None else "n/a"
        cap = (c[3] or "").replace("\n", " ")[:110]
        tr = (c[4] or "").replace("\n", " ")[:110]
        print(f"\n{rank}. [{relev}] @{c[2]}  {c[1]}")
        if cap: print(f"    cap: {cap}")
        if tr and tr != cap: print(f"    say: {tr}")
    con.close()


if __name__ == "__main__":
    main()
