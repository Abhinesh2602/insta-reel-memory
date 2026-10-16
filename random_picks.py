#!/usr/bin/env python3
"""Pick N random transcribed reels/posts for the weekly digest cron.
Prints JSON list: [{url, username, caption, transcript, category}, ...]
Avoids repeats across runs via a 'last_picked' log file (skips anything picked in last 60 days).
Filters: English-only (ASCII letter ratio), non-loopy (lexical diversity), minimum real content length.
"""
import sqlite3, os, json, random, sys, time, re

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
LOG = os.path.join(HERE, "data", "digest_picks_log.json")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
COOLDOWN_DAYS = 60
MIN_WORDS = 20
MIN_ASCII_RATIO = 0.85      # filters non-English transcripts (Cyrillic/Tamil/Korean/etc.)
MIN_UNIQUE_RATIO = 0.35     # filters looped/repetitive filler ("chum chum chum...")

def load_log():
    if os.path.exists(LOG):
        try:
            return json.load(open(LOG))
        except Exception:
            return {}
    return {}

def save_log(log):
    json.dump(log, open(LOG, "w"))

def is_quality_transcript(text):
    if not text:
        return False
    words = text.split()
    if len(words) < MIN_WORDS:
        return False
    # ascii letter ratio (catches non-English / mixed-script noise)
    letters = re.findall(r"[^\W\d_]", text, re.UNICODE)
    if not letters:
        return False
    ascii_letters = [c for c in letters if ord(c) < 128]
    if len(ascii_letters) / len(letters) < MIN_ASCII_RATIO:
        return False
    # lexical diversity (catches looped/repetitive filler, e.g. "chum chum chum...")
    lowered = [w.lower().strip(".,!?") for w in words]
    unique_ratio = len(set(lowered)) / len(lowered)
    if unique_ratio < MIN_UNIQUE_RATIO:
        return False
    return True

def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT shortcode, url, username, caption, transcript, category, timestamp, viewed_at, comment "
        "FROM reels WHERE transcribe_status='done' AND transcript IS NOT NULL AND trim(transcript)!=''"
    ).fetchall()
    con.close()
    rows = [r for r in rows if is_quality_transcript(r["transcript"])]
    log = load_log()
    now = time.time()
    cutoff = now - COOLDOWN_DAYS * 86400
    log = {k: v for k, v in log.items() if v > cutoff}
    eligible = [r for r in rows if r["shortcode"] not in log]
    pool = eligible if len(eligible) >= N else rows
    picks = random.sample(pool, min(N, len(pool)))
    for p in picks:
        log[p["shortcode"]] = now
    save_log(log)
    out = [{"id": p["shortcode"], "url": p["url"], "username": p["username"], "caption": (p["caption"] or "")[:300],
            "transcript": (p["transcript"] or "")[:800], "category": p["category"],
            "date": p["timestamp"], "viewed": bool(p["viewed_at"]),
            "comment": p["comment"] or ""} for p in picks]
    print(json.dumps(out, indent=2))

if __name__ == "__main__":
    main()
