#!/usr/bin/env python3
"""Phase 1: resumable batch transcriber.
Apify (get videoUrl) -> download -> local faster-whisper -> DB.
Idempotent: only touches transcribe_status='pending'. Safe to re-run / kill.
Usage: python3 transcribe.py [LIMIT]   (default: all pending)
"""
import json, sqlite3, os, sys, time, tempfile, subprocess, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN","")
WHISPER = os.environ.get("WHISPER_URL", "http://localhost:8082/v1/audio/transcriptions")
APIFY_ACTOR = "apify~instagram-scraper"
APIFY_BATCH = 25          # urls per apify run
LOG = os.path.join(HERE, "data", "transcribe.log")
TS_KEY = os.environ.get("TYPESAFE_API_KEY", "")
CATS = {
    "ai_tools": "AI tools, LLMs, ChatGPT/Claude/Gemini, prompts, automation, AI agents",
    "coding_dev": "Coding, software development, web dev, programming tutorials",
    "design_creative": "Design, UI/UX, video editing, motion graphics, creative work",
    "sales": "Sales specifically: cold outreach, prospecting, closing deals, sales scripts, objection handling, cold email/DM outreach, lead generation, follow-ups, booking calls, SDR/AE tactics, CRM and pipeline",
    "marketing": "Marketing: ads, content strategy, branding, copywriting, SEO, social media growth, audience building, funnels, email marketing campaigns, positioning",
    "business": "General business/entrepreneurship: strategy, operations, startups, hiring, finance/money, productivity of running a company, business models (that are NOT specifically sales or marketing)",
    "productivity": "Personal productivity, self-improvement, habits, learning, mindset, motivational quotes/mindset content, books/reading recommendations",
    "health_fitness": "Health, fitness, gym/workouts, nutrition, diet, sleep science, biohacking, supplements, bodybuilding, cardio, weight loss/cut, longevity",
    "entertainment": "Entertainment, memes, music, comedy, relatable/lifestyle, travel/trip content, movies/TV, news/politics/current events",
    "other": "None of the above fits well — truly generic engagement-bait with no identifiable topic",
}


def categorize(caption, transcript, author):
    if not TS_KEY:
        return None
    state = {"author": author, "caption": (caption or "")[:500],
             "transcript": (transcript or "")[:500]}
    payload = json.dumps({"state": state, "model": "jev-latest", "questions": {
        "cat": {"type": "choice",
                "instructions": "Classify this saved Instagram reel into the single best category based on its caption and spoken transcript.",
                "criteria": CATS}}}).encode()
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=payload,
        headers={"Authorization": f"Bearer {TS_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["answers"]["cat"].get("choice")
    except Exception:
        return None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def apify_scrape(urls):
    """Return dict shortCode -> videoUrl for a batch of reel urls."""
    payload = json.dumps({
        "directUrls": urls,
        "resultsType": "posts",
        "resultsLimit": len(urls),
        "addParentData": False,
    }).encode()
    api = (f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/"
           f"run-sync-get-dataset-items?token={APIFY_TOKEN}")
    req = urllib.request.Request(api, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        items = json.load(r)
    out = {}
    for it in items:
        if isinstance(it, dict) and it.get("shortCode"):
            out[it["shortCode"]] = it.get("videoUrl") or ""
    return out


GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3")

def transcribe_file(path):
    import requests
    if GROQ_API_KEY:
        with open(path, "rb") as fh:
            r = requests.post(GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": fh},
                data={"model": GROQ_MODEL, "response_format": "json"},
                timeout=120)
        if r.status_code == 429:
            # rate limited — brief backoff then one retry before falling back
            time.sleep(5)
            with open(path, "rb") as fh:
                r = requests.post(GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": fh},
                    data={"model": GROQ_MODEL, "response_format": "json"},
                    timeout=120)
        r.raise_for_status()
        return r.json().get("text", "").strip()
    # fallback: local faster-whisper (only used if no Groq key configured)
    with open(path, "rb") as fh:
        r = requests.post(WHISPER, files={"file": fh},
                          data={"model": "small"}, timeout=300)
    r.raise_for_status()
    return r.json().get("text", "").strip()


def ytdlp_audio(reel_url, out_base):
    """Download bestaudio for a reel via yt-dlp. Returns path or None."""
    import subprocess, glob
    for f in glob.glob(out_base + ".*"):
        try: os.unlink(f)
        except: pass
    cmd = ["yt-dlp", "-q", "--no-warnings", "--no-playlist",
           "-f", "bestaudio/best", "-o", out_base + ".%(ext)s", reel_url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    hits = glob.glob(out_base + ".*")
    if r.returncode != 0 or not hits:
        raise RuntimeError((r.stderr or "yt-dlp failed").strip().splitlines()[-1][:200] if r.stderr else "no audio")
    return hits[0]


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    con = sqlite3.connect(DB)
    cols = [c[1] for c in con.execute("PRAGMA table_info(reels)")]
    if "category" not in cols:
        con.execute("ALTER TABLE reels ADD COLUMN category TEXT")
        con.commit()
    q = "SELECT shortcode, url FROM reels WHERE transcribe_status='pending' ORDER BY timestamp DESC"
    if limit:
        q += f" LIMIT {limit}"
    pending = con.execute(q).fetchall()
    log(f"START: {len(pending)} reels pending (limit={limit})")
    import glob
    done = err = 0
    for n, (sc, url) in enumerate(pending, 1):
        base = os.path.join(tempfile.gettempdir(), f"reel_{sc}")
        audio = None
        try:
            audio = ytdlp_audio(url, base)
            text = transcribe_file(audio)
            cat = categorize(text, None, sc)
            con.execute("UPDATE reels SET transcript=?, category=?, "
                        "transcribed_at=?, transcribe_status='done' WHERE shortcode=?",
                        (text, cat, int(time.time()), sc))
            con.commit()
            done += 1
        except Exception as e:
            con.execute("UPDATE reels SET transcribe_status='error', "
                        "transcript=? WHERE shortcode=?", (f"[error: {e}]"[:250], sc))
            con.commit()
            err += 1
        finally:
            for f in glob.glob(base + ".*"):
                try: os.unlink(f)
                except: pass
        if n % 20 == 0:
            left = con.execute("SELECT COUNT(*) FROM reels WHERE transcribe_status='pending'").fetchone()[0]
            log(f"progress: done={done} err={err} pending_left={left}")
    con.close()
    log(f"FINISHED: done={done} err={err}")


if __name__ == "__main__":
    main()
