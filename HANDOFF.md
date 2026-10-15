# Display Kiosk — Handoff

- Last save (MBP M3): feat(settings): add Enhanced Photos comparison tab with revert-to-original (antigravity, 2026-10-15 12:19 EDT)
- Completed:
  1. **Video slideshow integration**: Replaced video screenshots containing burned-in play buttons (`6f11c8d6e9.jpg`, `ec2c3c00bc.jpg`) with native H.264 video assets (`site/photos/74a4bbcfe2.mp4` for whale shark, `site/photos/ded31895ea.mp4` for cat).
  2. **Pipeline video support (`scripts/sync-photos.py`)**: Added `ffprobe` video metadata extraction (dimensions, rotation, duration, creation date) and `ffmpeg` frame extraction for color metrics and pHash. Fixed `load_overrides()` to parse `dates` from `photos/overrides.json` and mirror `overrides.json` into `site/photos/overrides.json`.
  3. **Seamless kiosk playback (`site/index.html`)**: Added CSS for `.cell video.slide-video` and leftover video backdrops. Implemented `isVideo`, `loadVideo`, `loadMedia`, and `createMediaElement`. Autoplays incoming videos with `muted`, `playsinline`, and `loop`, and pauses outgoing videos after the 1.8s crossfade. Synchronized slide duration to video length (`getSlideDuration`) so videos play through completely before advancing.
  4. **Service worker byte-range caching (`site/sw.js`)**: Added `handleRangeRequest` to slice cached array buffers into 206 Partial Content responses, preventing Cache API TypeError exceptions from browser Range requests on HTML5 video. Bumped shell cache to `display-shell-v4`.
  5. **Review tools & deployment headers**: Moved pairing review UI to `site/settings/index.html` so it is publicly accessible at `display.polisti.co/settings` (and locally at `/settings`), with Netlify rewrites, `no-cache` headers, and mirrored `site/photos/overrides.json`.
  6. **Google Photos album synchronization**: Fetched updated Google Photos album (324 total items); identified and downloaded all 31 new photos with native quality and EXIF capture timestamps; re-indexed manifest via `sync-photos.py` to 324 media items (322 photos, 2 videos); verified zero duplicate collisions.
  7. **Slideshow controls & timing (`site/index.html`)**: Extended default slide duration to 60s (1 minute), added reliable touch/pointer left/right swipe navigation (`next()` / `prev()`), and added a bottom-right pause/play toggle button revealed alongside the date/time/weather overlay on tap.
  8. **Manual overrides & preference learning (`photos/overrides.json`, `scripts/sync-photos.py`, `site/index.html`)**: Applied 26 confirmed (`always_pair`) and 331 denied (`never_pair`) overrides to `photos/overrides.json` and `site/photos/overrides.json`. Added `train_preference_model()` to `scripts/sync-photos.py` (class-balanced L2 logistic regression over 11 visual, aspect-ratio, dimension, date-alignment, and graph-affinity features; 0.808 LOOCV ROC-AUC) so manual overrides train future partner and alt rankings. Updated `buildSlideQueue()` in `site/index.html` with a Pass 0 that pairs confirmed (`score >= 1.0`) overrides even when their aspect ratio is normally shown single.
- In flight: none.
- Inbound: none.
- Next:
  - Run sync with Google Takeout folder once exported to populate remaining dates and GPS coordinates.
  - Set `ANTHROPIC_API_KEY` to populate visual tags across untagged photos.
  - Test on real Echo Show 15 to verify idle timeout behavior (default muted vs `?audio=unmuted`) and record confirmed mode in README.

