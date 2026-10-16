#!/usr/bin/env python3
"""Re-run JEV categorization on all transcribed reels with current CATS schema.
Batches multiple reels per JEV call (shared state array). Idempotent-safe.
Scope with --only-other to just reclassify the current 'other' bucket (cheap, targeted).
"""
import sqlite3, json, os, sys, urllib.request, time, re
from transcribe import CATS  # single source of truth

DB=os.path.join(os.path.dirname(os.path.abspath(__file__)),"data","reels.db")
TS=os.environ["TYPESAFE_API_KEY"]
BATCH=20

def is_garbage_transcript(t):
    """Detect looped/gibberish ASR output (e.g. 'yeah yeah yeah...' or single repeated
    non-latin glyph strings) so it doesn't drown out a perfectly good caption in JEV's input."""
    if not t:
        return True
    words = t.split()
    if len(words) < 3:
        return True
    uniq_ratio = len(set(words)) / len(words)
    if uniq_ratio < 0.25:
        return True
    # a huge run of a single repeated character/short token (e.g. "నినిని నిని...")
    if re.search(r'(.{1,6})\1{8,}', t):
        return True
    return False

def jev_batch(rows):
    state=[]; questions={}
    for i,r in enumerate(rows):
        cap = (r["caption"] or "")[:500]
        tr_raw = r["transcript"] or ""
        tr = "" if is_garbage_transcript(tr_raw) else tr_raw[:500]
        state.append({"author":r["username"],"caption":cap,"transcript":tr})
        questions[f"q{i}"]={"type":"choice",
            "instructions":f"Classify the saved Instagram reel at `state[{i}]` into the single best category based on its caption and spoken transcript. If transcript is empty/noisy, rely on caption. Only use 'other' if truly nothing identifies the topic.",
            "criteria":CATS}
    payload=json.dumps({"state":state,"model":"jev-latest","questions":questions}).encode()
    req=urllib.request.Request("https://api.typesafe.ai/v1/systemone",data=payload,
        headers={"Authorization":f"Bearer {TS}","Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=90) as r:
        ans=json.load(r)["answers"]
    return [ans.get(f"q{i}",{}).get("choice") for i in range(len(rows))]

def main():
    only_other = "--only-other" in sys.argv
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
    q = "SELECT shortcode,username,caption,transcript FROM reels WHERE transcribe_status='done'"
    if only_other:
        q += " AND category='other'"
    rows=con.execute(q).fetchall()
    print(f"recategorizing {len(rows)} reels... (only_other={only_other})")
    n=0
    for i in range(0,len(rows),BATCH):
        b=rows[i:i+BATCH]
        try:
            cats=jev_batch(b)
        except Exception as e:
            print(f"batch {i} fail: {e}"); continue
        for r,c in zip(b,cats):
            con.execute("UPDATE reels SET category=? WHERE shortcode=?",(c,r["shortcode"]))
        con.commit(); n+=len(b)
        print(f"  {n}/{len(rows)}")
    from collections import Counter
    dist=Counter(r[0] for r in con.execute("SELECT category FROM reels WHERE category IS NOT NULL"))
    con.close()
    print("distribution:",dict(dist))

if __name__=="__main__": main()
