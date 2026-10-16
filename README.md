# Reel Memory

Turn your Instagram "Saved" reels into a personal, searchable knowledge base
instead of a graveyard you never revisit.

If you're like most people, "Saved" on Instagram is where good content goes
to die. This app ingests your IG saved-posts export, transcribes every reel
with Whisper (speech-to-text), indexes everything with SQLite FTS5, and gives
you a tiny self-hosted web app to search, browse, and re-discover what you
saved - plus a daily "digest" of random forgotten gems and auto-generated
topic cheatsheets synthesized from groups of related reels.

## What it actually does

1. **Ingest** (`ingest.py`) - loads your Instagram "saved posts" JSON export
   into a local SQLite DB with full-text search (FTS5).
2. **Transcribe** (`transcribe.py`, `retry_errors.py`) - downloads each saved
   reel's video and runs it through Whisper (via Groq's free API or your own
   local Whisper server) to get a text transcript.
3. **OCR fallback** (`ocr_posts.py`) - for saved image posts (not video),
   OCRs the image text instead of transcribing audio.
4. **Categorize** (`recategorize.py`) - optionally uses an LLM to tag each
   reel with a topic category.
5. **Search & browse** (`app.py`, `templates/index.html`) - a small Flask app:
   full-text search over captions + transcripts, browse by category, mark
   reels as "viewed", leave yourself notes, see stats.
6. **Digest** (`/digest` route, `random_picks.py`) - surfaces random
   not-yet-viewed reels so saved content actually resurfaces instead of
   disappearing forever.
7. **Playbooks** (`playbook.py`) - synthesizes a deduped "cheatsheet" markdown
   doc from all the reels in one category, with every bullet point citing its
   source reel. See `playbooks/examples/` for a sample output.

## Why this exists

I kept saving reels on Instagram with genuinely useful info (AI tools,
business tactics, marketing ideas) and never looking at them again. This
turns that dead pile into something you can actually search and that
resurfaces old saves instead of burying them.

## Quick start

```bash
git clone <this-repo>
cd reel-memory
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env - at minimum set GROQ_API_KEY for transcription (free tier at
# https://console.groq.com); everything else is optional

mkdir -p data
# Export your Instagram saved posts (Settings > Accounts Center > Your
# information and permissions > Download your information > request a JSON
# export, "Saved" category). Drop the resulting JSON as data/saved_posts.json

python3 ingest.py          # load saved_posts.json into data/reels.db
python3 transcribe.py      # transcribe videos (takes a while, resumable)
python3 ocr_posts.py       # OCR any saved image posts
python3 recategorize.py    # optional: tag categories via LLM

python3 app.py             # starts the web app on :5000 (see app.py for port)
```

Then open `http://localhost:5000` and search your saved reels.

## Requirements

- Python 3.9+
- A Groq API key (free tier) for Whisper transcription, or your own local
  Whisper-compatible server
- Your Instagram data export (saved posts JSON) - this is NOT included in
  this repo and never will be; it's personal data

## What's NOT in this repo

This is the open-source template, stripped of all personal data:
- No database (`data/reels.db` is gitignored)
- No Instagram export (`data/saved_posts.json` is gitignored)
- No logs, no personal transcripts, no API keys
- `playbooks/examples/` has exactly one sample output so you can see what the
  categorization + cheatsheet pipeline produces, on public reel content

## Optional: LLM re-ranking and categorization

Search and categorization can optionally use an LLM via `TYPESAFE_API_KEY`
(see `.env.example`). Without it, search falls back to plain SQLite FTS5
ranking and reels simply won't get auto-categorized. Everything else works
fine without it.

## Tech stack

Flask, SQLite + FTS5, Whisper (via Groq), yt-dlp for video download, plain
HTML/JS frontend (PWA-installable, see `static/` icons + `manifest.json`
route in `app.py`).

## License

MIT - see LICENSE.
