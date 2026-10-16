#!/usr/bin/env python3
"""Retry transcribe_status='error' rows.
 - Downloads via yt-dlp, checks locally with ffprobe for an audio stream
   BEFORE calling Groq (free, no quota cost) -> no-audio rows go to 'skip'.
 - Rows with audio get sent to Groq whisper-large-v3.
 - Stops cleanly when Groq's daily request quota is nearly exhausted
   (checked via x-ratelimit-remaining-requests response header).
Safe to re-run; idempotent per row (only touches status='error').
"""
import json, sqlite3, os, sys, time, tempfile, glob, subprocess, requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3")
LOG = os.path.join(HERE, "data", "retry_errors.log")
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
    "other": "None of the above fits well -- truly generic engagement-bait with no identifiable topic",
}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def categorize(caption, transcript, author):
    if not TS_KEY:
        return None
    state = {"author": author, "caption": (caption or "")[:500], "transcript": (transcript or "")[:500]}
    payload = json.dumps({"state": state, "model": "jev-latest", "questions": {
        "cat": {"type": "choice",
                "instructions": "Classify this saved Instagram reel into the single best category based on its caption and spoken transcript.",
                "criteria": CATS}}}).encode()
    try:
        import urllib.request
        r = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=payload,
            headers={"Authorization": f"Bearer {TS_KEY}", "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.load(resp)["answers"]["cat"].get("choice")
    except Exception:
        return None


def ytdlp_audio(reel_url, out_base):
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


def has_audio_stream(path):
    """Check locally via ffprobe -- no API cost."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30)
    return bool(r.stdout.strip())


class QuotaExhausted(Exception):
    pass


def transcribe_file(path):
    with open(path, "rb") as fh:
        r = requests.post(GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": fh},
            data={"model": GROQ_MODEL, "response_format": "json"},
            timeout=120)
    remaining = r.headers.get("x-ratelimit-remaining-requests")
    reset = r.headers.get("x-ratelimit-reset-requests")
    if r.status_code == 429:
        raise QuotaExhausted(f"429 reset={reset}")
    r.raise_for_status()
    text = r.json().get("text", "").strip()
    if remaining is not None:
        try:
            if int(remaining) <= 1:
                # record this transcription first (already done above), but stop after.
                raise QuotaExhausted(f"remaining={remaining} reset={reset}")
        except ValueError:
            pass
    return text


def main():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT shortcode, url FROM reels WHERE transcribe_status='error'"
    ).fetchall()
    log(f"START: {len(rows)} error rows to retry")

    done = err = skip = 0
    quota_hit = False
    for n, (sc, url) in enumerate(rows, 1):
        base = os.path.join(tempfile.gettempdir(), f"retry_{sc}")
        try:
            audio = ytdlp_audio(url, base)
            if not has_audio_stream(audio):
                con.execute("UPDATE reels SET transcribe_status='skip', transcript=? WHERE shortcode=?",
                            ("[skip: no audio track]", sc))
                con.commit()
                skip += 1
                continue
            try:
                text = transcribe_file(audio)
            except QuotaExhausted as e:
                # transcription itself succeeded even though quota is now low;
                # only stop BEFORE a request if remaining was already <=1 at entry.
                # Here exception means genuine 429 (no transcript obtained).
                log(f"QUOTA EXHAUSTED at row {n}/{len(rows)}: {e}. Stopping cleanly.")
                quota_hit = True
                break
            cat = categorize(text, None, sc)
            con.execute("UPDATE reels SET transcript=?, category=?, transcribed_at=?, "
                        "transcribe_status='done' WHERE shortcode=?",
                        (text, cat, int(time.time()), sc))
            con.commit()
            done += 1
        except Exception as e:
            msg = str(e)
            if "QuotaExhausted" in type(e).__name__:
                log(f"QUOTA EXHAUSTED at row {n}/{len(rows)}: {e}. Stopping cleanly.")
                quota_hit = True
                break
            con.execute("UPDATE reels SET transcribe_status='error', transcript=? WHERE shortcode=?",
                        (f"[error: {msg}]"[:250], sc))
            con.commit()
            err += 1
        finally:
            for f in glob.glob(base + ".*"):
                try: os.unlink(f)
                except: pass
        if n % 10 == 0:
            log(f"progress: done={done} err={err} skip={skip} ({n}/{len(rows)})")
        time.sleep(0.3)

    left = con.execute("SELECT COUNT(*) FROM reels WHERE transcribe_status='error'").fetchone()[0]
    log(f"FINISHED: done={done} err={err} skip={skip} quota_hit={quota_hit} remaining_error_rows={left}")
    con.close()


if __name__ == "__main__":
    main()
