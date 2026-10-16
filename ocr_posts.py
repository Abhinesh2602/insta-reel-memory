#!/usr/bin/env python3
"""Phase 3: process 'post' media_type items that were blanket-skipped.
Each post is either a video, a single image, or a carousel (mix of images/videos).
Videos -> yt-dlp + whisper (existing pipeline). Images -> instaloader + tesseract OCR.
OCR'd/transcribed text goes into the same `transcript` column so search/categorize/digest
all pick it up for free. Idempotent: only touches transcribe_status='skip' AND media_type='post'.
"""
import json, sqlite3, os, sys, time, tempfile, subprocess, urllib.request, glob

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "reels.db")
LOG = os.path.join(HERE, "data", "ocr_posts.log")
TS_KEY = os.environ.get("TYPESAFE_API_KEY", "")
WHISPER = os.environ.get("WHISPER_URL", "http://localhost:8082/v1/audio/transcriptions")
MAX_IMAGES_PER_POST = 3  # cap OCR work for carousels; first few slides carry most signal

sys.path.insert(0, HERE)
from transcribe import CATS, categorize  # reuse same categorizer/criteria

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def ytdlp_audio(reel_url, out_base):
    for f in glob.glob(out_base + ".*"):
        try: os.unlink(f)
        except: pass
    cmd = ["yt-dlp", "-q", "--no-warnings", "--no-playlist",
           "-f", "bestaudio/best", "-o", out_base + ".%(ext)s", reel_url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    hits = glob.glob(out_base + ".*")
    if r.returncode != 0 or not hits:
        raise RuntimeError("yt-dlp failed")
    return hits[0]

def has_audio_stream(path):
    """ffprobe check: some Instagram carousel 'video' slides are silent (no audio track at all),
    which crashes faster-whisper with an obscure 'tuple index out of range' 500 instead of
    gracefully returning empty text. Skip the whisper call entirely for those."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=20)
    return bool(r.stdout.strip())

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3")

def transcribe_file(path):
    if not has_audio_stream(path):
        return ""  # silent slide, nothing to transcribe
    import requests
    if GROQ_API_KEY:
        with open(path, "rb") as fh:
            r = requests.post(GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": fh},
                data={"model": GROQ_MODEL, "response_format": "json"},
                timeout=120)
        if r.status_code == 429:
            time.sleep(5)
            with open(path, "rb") as fh:
                r = requests.post(GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": fh},
                    data={"model": GROQ_MODEL, "response_format": "json"},
                    timeout=120)
        r.raise_for_status()
        return r.json().get("text", "").strip()
    with open(path, "rb") as fh:
        r = requests.post(WHISPER, files={"file": fh}, data={"model": "small"}, timeout=300)
    r.raise_for_status()
    return r.json().get("text", "").strip()

def download_image(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        f.write(r.read())

def ocr_batch_dir(img_dir):
    """Run tesseract on every .jpg in img_dir via one docker invocation. Returns {filename: text}."""
    script = (
        "for f in /data/*.jpg; do "
        "tesseract \"$f\" \"${f%.jpg}\" 2>/dev/null; "
        "done"
    )
    subprocess.run(["docker", "run", "--rm", "-v", f"{img_dir}:/data",
                    "tesseractshadow/tesseract4re", "sh", "-c", script],
                   capture_output=True, timeout=300)
    out = {}
    for txt_path in glob.glob(os.path.join(img_dir, "*.txt")):
        base = os.path.basename(txt_path)[:-4] + ".jpg"
        try:
            out[base] = open(txt_path, encoding="utf-8", errors="ignore").read().strip()
        except Exception:
            out[base] = ""
    return out

def get_post_media(shortcode):
    """Use instaloader to classify a post and return (kind, items).
    kind: 'video' | 'images' | 'mixed' | 'error'
    items: list of (is_video, url) for carousels, or single url for non-carousel."""
    import instaloader
    L = instaloader.Instaloader(download_videos=False, download_video_thumbnails=False,
                                 save_metadata=False, download_comments=False,
                                 post_metadata_txt_pattern='', quiet=True)
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    if post.typename == "GraphSidecar":
        items = [(n.is_video, n.video_url if n.is_video else n.display_url)
                 for n in post.get_sidecar_nodes()]
        return ("mixed", items)
    elif post.is_video:
        return ("video", [(True, post.video_url)])
    else:
        return ("images", [(False, post.url)])

def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    con = sqlite3.connect(DB)
    cols = [c[1] for c in con.execute("PRAGMA table_info(reels)")]
    if "category" not in cols:
        con.execute("ALTER TABLE reels ADD COLUMN category TEXT")
        con.commit()
    q = "SELECT shortcode, url FROM reels WHERE media_type='post' AND transcribe_status='skip' ORDER BY timestamp DESC"
    if limit:
        q += f" LIMIT {limit}"
    pending = con.execute(q).fetchall()
    log(f"START: {len(pending)} posts pending (limit={limit})")
    done = err = 0
    for n, (sc, url) in enumerate(pending, 1):
        work_dir = tempfile.mkdtemp(prefix=f"post_{sc}_")
        try:
            kind, items = get_post_media(sc)
            texts = []
            video_done = False
            img_urls = []
            for is_video, media_url in items:
                if is_video and not video_done:
                    try:
                        vid_path = os.path.join(work_dir, "vid.mp4")
                        download_image(media_url, vid_path)  # same direct-CDN download fn works for video bytes
                        t = transcribe_file(vid_path)
                        if t: texts.append(t)
                        video_done = True
                    except Exception as ve:
                        log(f"  {sc}: video transcribe failed: {ve}")
                elif not is_video and len(img_urls) < MAX_IMAGES_PER_POST:
                    img_urls.append(media_url)
            if img_urls:
                for i, iu in enumerate(img_urls):
                    try:
                        download_image(iu, os.path.join(work_dir, f"img{i}.jpg"))
                    except Exception as ie:
                        log(f"  {sc}: image {i} download failed: {ie}")
                ocr_results = ocr_batch_dir(work_dir)
                for i in range(len(img_urls)):
                    t = ocr_results.get(f"img{i}.jpg", "").strip()
                    if t and len(t) > 3:
                        texts.append(t)
            combined = "\n".join(texts).strip()
            cap = con.execute("SELECT caption FROM reels WHERE shortcode=?", (sc,)).fetchone()
            cap = cap[0] if cap else ""
            cat = categorize(cap, combined, sc) if TS_KEY else None
            new_status = "done" if combined else "skip"  # still mark truly-empty ones as skip, not error
            con.execute("UPDATE reels SET transcript=?, category=?, transcribed_at=?, "
                        "transcribe_status=? WHERE shortcode=?",
                        (combined or None, cat, int(time.time()), new_status, sc))
            con.commit()
            done += 1
        except Exception as e:
            con.execute("UPDATE reels SET transcribe_status='error', transcript=? WHERE shortcode=?",
                        (f"[ocr error: {e}]"[:250], sc))
            con.commit()
            err += 1
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)
        if n % 20 == 0:
            log(f"progress: {n}/{len(pending)} done={done} err={err}")
    con.close()
    log(f"FINISHED: done={done} err={err}")

if __name__ == "__main__":
    main()
