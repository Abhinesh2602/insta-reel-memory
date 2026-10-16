#!/usr/bin/env python3
"""Phase 0: load saved_posts.json into SQLite with FTS5. Idempotent."""
import json, sqlite3, re, os

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
SRC = os.path.join(HERE, "data", "saved_posts.json")


def fix_mojibake(s):
    if not s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def getlabel(item, label):
    for lv in item.get("label_values", []):
        if lv.get("label") == label:
            return lv.get("value")
    return None


def get_owner(item):
    for lv in item.get("label_values", []):
        if lv.get("title") == "Owner":
            for outer in lv.get("dict", []):
                for inner in outer.get("dict", []):
                    if inner.get("label") == "Username":
                        return inner.get("value")
    return None


def get_hashtags(item):
    for lv in item.get("label_values", []):
        if lv.get("title") == "Hashtags":
            tags = []
            for d in lv.get("dict", []):
                for x in d.get("dict", []):
                    if x.get("value"):
                        tags.append(x["value"])
            return " ".join(tags)
    return ""


SHORTCODE_RE = re.compile(r"/(?:reel|p|tv)/([^/?]+)")


def shortcode(url):
    m = SHORTCODE_RE.search(url or "")
    return m.group(1) if m else None


def media_type(url):
    if "/reel/" in url:
        return "reel"
    if "/p/" in url:
        return "post"
    if "/tv/" in url:
        return "tv"
    return "other"


def main():
    data = json.load(open(SRC))
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS reels(
        shortcode TEXT PRIMARY KEY,
        url TEXT, fbid TEXT, media_type TEXT,
        username TEXT, caption TEXT, title TEXT, hashtags TEXT,
        timestamp INTEGER,
        video_url TEXT, transcript TEXT, transcribed_at INTEGER,
        transcribe_status TEXT DEFAULT 'pending'
    )""")
    con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS reels_fts USING fts5(
        shortcode UNINDEXED, username, caption, title, hashtags, transcript,
        content='reels', content_rowid='rowid'
    )""")
    con.executescript("""
    CREATE TRIGGER IF NOT EXISTS reels_ai AFTER INSERT ON reels BEGIN
      INSERT INTO reels_fts(rowid, shortcode, username, caption, title, hashtags, transcript)
      VALUES (new.rowid, new.shortcode, new.username, new.caption, new.title, new.hashtags, new.transcript);
    END;
    CREATE TRIGGER IF NOT EXISTS reels_ad AFTER DELETE ON reels BEGIN
      INSERT INTO reels_fts(reels_fts, rowid, shortcode, username, caption, title, hashtags, transcript)
      VALUES ('delete', old.rowid, old.shortcode, old.username, old.caption, old.title, old.hashtags, old.transcript);
    END;
    CREATE TRIGGER IF NOT EXISTS reels_au AFTER UPDATE ON reels BEGIN
      INSERT INTO reels_fts(reels_fts, rowid, shortcode, username, caption, title, hashtags, transcript)
      VALUES ('delete', old.rowid, old.shortcode, old.username, old.caption, old.title, old.hashtags, old.transcript);
      INSERT INTO reels_fts(rowid, shortcode, username, caption, title, hashtags, transcript)
      VALUES (new.rowid, new.shortcode, new.username, new.caption, new.title, new.hashtags, new.transcript);
    END;
    """)

    ins = upd = skipped = 0
    for it in data:
        url = getlabel(it, "URL") or ""
        sc = shortcode(url)
        if not sc:
            skipped += 1
            continue
        cap = fix_mojibake(getlabel(it, "Caption") or "")
        title = fix_mojibake(getlabel(it, "Title") or "")
        user = get_owner(it) or ""
        tags = fix_mojibake(get_hashtags(it))
        mt = media_type(url)
        status = "skip" if mt == "post" else "pending"
        cur = con.execute("SELECT shortcode FROM reels WHERE shortcode=?", (sc,))
        if cur.fetchone():
            con.execute("""UPDATE reels SET url=?, fbid=?, media_type=?, username=?,
                caption=?, title=?, hashtags=?, timestamp=? WHERE shortcode=?""",
                (url, it.get("fbid"), mt, user, cap, title, tags, it.get("timestamp"), sc))
            upd += 1
        else:
            con.execute("""INSERT INTO reels(shortcode, url, fbid, media_type, username,
                caption, title, hashtags, timestamp, transcribe_status)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (sc, url, it.get("fbid"), mt, user, cap, title, tags, it.get("timestamp"), status))
            ins += 1
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
    reels = con.execute("SELECT COUNT(*) FROM reels WHERE media_type='reel'").fetchone()[0]
    pend = con.execute("SELECT COUNT(*) FROM reels WHERE transcribe_status='pending'").fetchone()[0]
    con.close()
    print(f"inserted={ins} updated={upd} skipped_no_shortcode={skipped}")
    print(f"total rows={tot} reels={reels} pending_transcription={pend}")
    print(f"DB: {DB}")


if __name__ == "__main__":
    main()
