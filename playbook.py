#!/usr/bin/env python3
"""Generate a category playbook: dedupe transcripts of a category into a cheatsheet doc.
Usage: python3 playbook.py <category>
Writes playbooks/<category>.md with raw grouped transcript excerpts for an LLM pass,
or if TYPESAFE_API_KEY set, uses JEV to synthesize a cheatsheet directly.
"""
import sqlite3, os, sys, json, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
TS_KEY = os.environ.get("TYPESAFE_API_KEY", "")

def main():
    if len(sys.argv) < 2:
        print("usage: playbook.py <category>"); return
    cat = sys.argv[1]
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT username, caption, transcript, url FROM reels WHERE category=? "
        "AND transcript IS NOT NULL AND trim(transcript)!=''", (cat,)).fetchall()
    con.close()
    rows = [r for r in rows if len((r["transcript"] or "").split()) >= 15]
    print(f"{len(rows)} usable transcripts in category '{cat}'")
    out_path = os.path.join(HERE, "playbooks", f"{cat}_raw.md")
    with open(out_path, "w") as f:
        f.write(f"# Raw transcripts: {cat} ({len(rows)} items)\n\n")
        for r in rows:
            f.write(f"## @{r['username']} — {r['url']}\n")
            if r["caption"]:
                f.write(f"Caption: {r['caption'][:300]}\n\n")
            f.write(f"{r['transcript'][:2000]}\n\n---\n\n")
    print(f"wrote {out_path}")

if __name__ == "__main__":
    main()
