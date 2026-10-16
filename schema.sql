-- Reel Memory database schema.
-- ingest.py creates the base table automatically (CREATE TABLE IF NOT EXISTS),
-- and later pipeline steps (transcribe.py, ocr_posts.py) ALTER TABLE in the
-- category column; viewed_at/comment are added the same way the first time
-- the web app writes to them. This file documents the full, current schema.

CREATE TABLE IF NOT EXISTS reels (
    shortcode         TEXT PRIMARY KEY,
    url               TEXT,
    fbid              TEXT,
    media_type        TEXT,               -- 'reel' | 'post' | 'tv' | 'other'
    username          TEXT,                -- IG handle of the creator
    caption           TEXT,
    title             TEXT,
    hashtags          TEXT,
    timestamp         INTEGER,             -- unix seconds, from IG export
    video_url         TEXT,
    transcript        TEXT,
    transcribed_at    INTEGER,
    transcribe_status TEXT DEFAULT 'pending',  -- pending | done | error | skip
    category          TEXT,                -- set by transcribe.py / recategorize.py via JEV
    viewed_at         INTEGER,             -- set by /mark_viewed, null until "read"
    comment           TEXT                 -- free-text personal note, set via /comment
);

-- Full-text search index, kept in sync via triggers (see ingest.py).
CREATE VIRTUAL TABLE IF NOT EXISTS reels_fts USING fts5(
    shortcode UNINDEXED, username, caption, title, hashtags, transcript,
    content='reels', content_rowid='rowid'
);
