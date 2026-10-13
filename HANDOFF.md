# Display Kiosk — Handoff

- Last save (MBP M3): docs: document new sync flow, scoring weights, overrides, and review tool (antigravity, 2026-10-12 21:15 EDT)
- Completed:
  - Extended `scripts/sync-photos.py` with optional `--takeout <dir>` support to match Google Takeout files by SHA1 and perceptual hash, extracting `photoTakenTime` and `geoData` from sidecars with EXIF fallback.
  - Added visual tagging step via Claude API using `ANTHROPIC_API_KEY` (configurable model, default `claude-3-5-haiku-20241022`) with strict JSON fixed vocabulary (`scene`, `subjects`, `people_count`, `mood`, `time_of_day`, `is_bw`, `event_hint`, `caption`), concurrency 4, retry/backoff on 429/5xx, and `.tags-cache.json` caching.
  - Implemented offline local image feature extraction using Pillow and numpy: relative luminance (`luma`), saturation (`sat`), warmth (`warmth`), 3-color palette via k-means, and 64-bit DCT perceptual hash (`phash`).
  - Precomputed pairwise compatibility scoring (0..1) with configurable weights (same day +0.40, within 3 days +0.25, same month +0.10, geo within 2km +0.15, event +0.10, scene +0.10, subjects Jaccard x0.15, Lab color compatibility x0.15), hard penalties (phash distance <= 10, b&w vs color mismatch, screenshots), soft contrast penalty, and top 10 partners stored in compact `photos.json` and human-readable `photos.debug.json`.
  - Rewrote pairing in `buildSlideQueue()` in `index.html` to pick partners from precomputed partner lists using random top-3 weighted selection, with leftover matching and fallback passes, plus calendar date spreading to prevent consecutive slides from the same event.
  - Enhanced debug overlay (`?debug=1`) to display capture date, caption, tags, and pair score for paired slides.
  - Added standalone `review.html` for desktop inspection of photo metadata, captions, tags, dates, and top 3 partners with interactive tag and no-date filtering.
  - Added manual overrides support via `photos/overrides.json` for custom tags, `always_pair` (pins score to 1.0 at top of both partner lists), and `never_pair` (sets score to 0).
  - Documented complete architecture, scoring weights, overrides, review tool, and workflows in `README.md`.
- In flight: none.
- Inbound: none.
- Next:
  - Run sync with Google Takeout folder once exported to populate remaining 142 dates and GPS coordinates.
  - Set `ANTHROPIC_API_KEY` to populate visual tags across all 321 photos.
  - Test on real Echo Show 15 to verify idle timeout behavior (default muted vs `?audio=unmuted`) and record confirmed mode in README.
  - Optionally swap in higher-resolution originals for the 66 flagged low-res images in `photos/`.
