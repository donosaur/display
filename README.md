# Echo Show 15 display kiosk

A self-contained photo slideshow kiosk for the Echo Show 15's Silk browser, deployed at `display.polisti.co`. It adapts to any window size, with primary optimization for the Echo Show 15 mounted in portrait orientation (1080×1920).

## Files

- `site/` — deployable kiosk application published to Netlify (`publish = "site"`)
  - `index.html` — single-file kiosk application (responsive pairing layout, color fallback matching, smooth image decoding, stay-awake stack, fullscreen)
  - `sw.js` — service worker for offline runtime caching and postMessage photo pruning
  - `photos/` — slideshow images named by 10-character SHA1 content hash and compact `photos.json` manifest
- `pairs.html` — standalone review and curation UI to confirm or deny pairings (served via `serve.py` at `/tools/pairs.html` or `/pairs.html`)
- `review.html` — standalone desktop review tool to inspect photo tags, captions, dates, and partner rankings (`/tools/review.html`)
- `compare.html` — interactive split slider and side-by-side comparison tool for AI-upscaled photos (`/tools/compare.html`)
- `photos/` — internal/private photo metadata and archives (not published to production)
  - `.tags-cache.json` — cached Claude API visual tags keyed by content hash (tracked in git)
  - `overrides.json` — manual pairing overrides (`always_pair`, `never_pair`) and tag corrections
  - `photos.debug.json` — formatted copy of manifest for human inspection (gitignored)
  - `quality-report.json` — resolution, sharpness, and quality tier audit
  - `originals/` — backups of original low-resolution photos before AI upscaling
  - `comparisons/` — before-and-after image crops for upscale inspection
- `scripts/`
  - `sync-photos.py` — sync script to hash images, extract metadata, run tagging, compute color compatibility, score partners and alts, and write manifests
  - `serve.py` — local development and review server with path lockdown, same-origin enforcement, and atomic write endpoints
  - `upscale-photos.py` — automated Real-ESRGAN upscaling pipeline
  - `inspect-quality.py` — resolution and Laplacian sharpness inspector
- `netlify.toml` — headers and build configuration (`publish = "site"`)
- `README.md` — documentation
- `HANDOFF.md` — project handoff state

## How it works

### Responsive pairing layout & score-based partner matching

To prevent mismatched photo orientations from being heavily cropped, the slideshow pairs images based on calculated crop loss and precomputed compatibility scores:

- Reads viewport size using `window.visualViewport` (falling back to `window.innerWidth/innerHeight`). Viewport aspect ratio `va = w / h`.
- For image aspect `ia` in cell aspect `ca`, crop loss fraction is `1 - min(ia, ca) / max(ia, ca)`.
- Calculates loss for full viewport (`ca = va`) and half cell (`ca = 2 * va` in portrait, `ca = va / 2` in landscape).
- Images with half-cell crop loss at least 0.15 lower than full-viewport loss are marked pairable. In portrait (va < 1), this pairs landscape photos; in landscape, it pairs portrait photos. Near-square and well-fitting images stay single.
- In `buildSlideQueue()`:
  1. The pairable set is shuffled at the start of each round (unless `?shuffle=0`, which enforces deterministic order).
  2. For each unpaired photo, candidates are selected from its precomputed `partners` list that are unused this round, pairable in the current viewport, and have score >= 0.20.
  3. A partner is chosen at random among the top 3 qualifying candidates, weighted by score. This ensures pairs vary naturally across rounds while prioritizing the best matches.
  4. Leftovers pass: remaining unpaired photos attempt to pair with any other unused pairable photo where compatibility score > 0.
  5. Color-compatibility fallback pass: photos that still have no date or semantic partner pair by visual color harmony alone (`alt` partners in manifest, picking weighted among top 3). This reduced random sequential pairings from 56.3% (40/71) down to ~15–18% (11–13/71).
  6. Final fallback pass: any remaining unused pairable photos are paired together sequentially (tagged as `random` in debug overlay, score 0).
  7. If an odd pairable photo remains, it renders uncropped (`contain`) over a blurred, darkened, scaled backdrop of itself (`filter: blur(40px) brightness(0.5); transform: scale(1.2)`).
  8. Event spreading: the queue reorders adjacent slides where possible to avoid showing two consecutive slides from the same calendar date.
- Paired slides split the screen with a 3px black gap (stacked top/bottom in portrait, side-by-side in landscape).
- **Round preservation**: the kiosk tracks photos shown this round (`shownPhotosThisRound`). When the queue is rebuilt mid-round due to an orientation change (`handleResize()`) or a single failed image load (`show()`), it preserves the current slide and rebuilds remaining slides exclusively from photos not yet shown, preventing repeats and skips. A fresh shuffle occurs strictly at the round wrap.

### Pair scoring model & weights

Pair compatibility scores (0..1) are computed offline during sync as a weighted sum. Constants at the top of `scripts/sync-photos.py` allow tuning the balance:

| Factor | Weight / Bonus | Condition |
|---|---|---|
| Same calendar day | `+0.40` | Exact same year, month, and day |
| Within 3 days | `+0.25` | Dates within 3 days (same event or trip) |
| Same month & year | `+0.10` | Same month and year |
| Nearby location | `+0.15` | GPS coordinates within 2 km (Haversine formula) |
| Same event hint | `+0.10` | Matching `event_hint` (excluding `everyday` and `other`) |
| Same scene | `+0.10` | Matching `scene` (excluding `other`) |
| Shared subjects | `× 0.15` | Jaccard similarity of tagged subjects |
| Color compatibility | `× 0.15` | `1 - normalized_blend(\|luma diff\|, \|sat diff\|, \|warmth diff\|, Lab palette dist)` |

**Penalties:**
- **Hard penalties (score = 0, never paired):**
  - Perceptual hash Hamming distance $\le 10$ (suppresses burst shots and near-duplicates).
  - One photo is black & white and the other is color.
  - Either photo is tagged with subject `text-screenshot`.
- **Soft penalty:**
  - Luminance difference $|luma_1 - luma_2| > 0.45$ multiplies final score by `0.5` (prevents glaring dark/bright clashes).

Each photo stores its top 10 partners with score $\ge 0.20$ as `partners: [["<filename>", 0.73], ...]`.

### Visual tagging with Claude API

When `ANTHROPIC_API_KEY` is present, the sync script tags photos visually via Claude Haiku (`claude-3-5-haiku-20241022` by default, or configurable via `ANTHROPIC_MODEL` / `CLAUDE_MODEL`):

- Downscales photos to max 768px JPEG in memory before sending.
- Enforces strict JSON selection from a fixed vocabulary:
  - `scene`: `indoor-home`, `indoor-venue`, `restaurant`, `street-city`, `park-garden`, `beach-water`, `mountains-nature`, `travel-landmark`, `stage-performance`, `event-party`, `vehicle-transit`, `other`
  - `subjects`: `people-group`, `person-solo`, `couple`, `child`, `baby`, `cat`, `dog`, `food-drink`, `architecture`, `landscape`, `art-object`, `text-screenshot`
  - `people_count`: `0`, `1`, `2`, `3-5`, `6+`
  - `mood`: `calm`, `joyful`, `energetic`, `formal`, `moody`
  - `time_of_day`: `day`, `golden-hour`, `night`, `indoor-unknown`
  - `is_bw`: boolean
  - `event_hint`: `birthday`, `wedding`, `holiday`, `concert-performance`, `trip`, `everyday`, `meal`, `other`
  - `caption`: concise summary (max 12 words) for human inspection
- Runs concurrently (concurrency 4) with exponential backoff on HTTP 429 and 5xx.
- Caches responses in `photos/.tags-cache.json` keyed by content hash. Reruns only process untagged photos unless `--retag` is passed.
- **Without an API key:** visual tagging is skipped with a clear warning. Existing cached tags in `.tags-cache.json` are retained, and pairing still functions using capture dates, local color features, and duplicate suppression.

### Capture date and location extraction

Google Photos strips EXIF metadata on web downloads. The sync script reconstructs metadata using Google Takeout and local EXIF:

- `--takeout <dir>`: matches Takeout images to `photos/` by SHA1 content hash first, falling back to 64-bit perceptual hash (DCT pHash) for re-encoded files.
- Reads Takeout JSON sidecars (handling truncated filenames like `.supple.json` and title lookups) to extract `photoTakenTime.timestamp` and `geoData` (ignoring 0,0).
- Fallback: for photos without Takeout matches, reads local EXIF `DateTimeOriginal` (preserving camera local time), then `DateTime`, and EXIF GPS. Photos without any date metadata remain null.

### Local image features (no API)

Computed with Pillow and numpy for every photo:
- `luma`: relative luminance in 0..1 ($0.2126R + 0.7152G + 0.0722B$).
- `sat`: mean HSV saturation in 0..1.
- `warmth`: mean $(R - B) / 255$ in -1..1.
- `palette`: 3 dominant colors as hex codes, computed via k-means clustering on a 64px thumbnail.
- `phash`: 64-bit DCT perceptual hash as 16 hex characters.

### Manual overrides (`photos/overrides.json`)

Optional file `photos/overrides.json` to manually adjust tags and pairings without modifying the script:

```json
{
  "tags": {
    "00378a5e71.jpg": {
      "scene": "beach-water",
      "caption": "Sunny afternoon on the boardwalk"
    }
  },
  "never_pair": [
    ["00378a5e71.jpg", "00647631c8.jpg"]
  ],
  "always_pair": [
    ["029e327450.jpg", "b2ba488013.jpg"]
  ]
}
```

- Keys can be full filenames (`00378a5e71.jpg`) or 10-char hash prefixes (`00378a5e71`).
- `always_pair`: forces the pair score to 1.0 and places each photo at the top of the other's partner list.
- `never_pair`: forces the pair score to 0.0, preventing them from being paired as partners.

### Review tools (`pairs.html` & `review.html`)

- **Pairing review (`pairs.html`)**:
  - Interactive curation UI to inspect algorithm-suggested pairings and apply personal preferences.
  - Three preview modes: **Kiosk Split** (emulates the portrait 1080×1920 Echo Show 15 layout with 9:16 proportions), **Side by Side**, and **Compact Grid**.
  - Review actions: **✓ Confirm** (`always_pair`, score 1.0, top priority), **✕ Deny** (`never_pair`, score 0.0, excluded), and **↺ Reset** (revert to algorithm suggestion).
  - Rapid keyboard workflow: <kbd>C</kbd>/<kbd>Y</kbd> to confirm, <kbd>D</kbd>/<kbd>N</kbd> to deny, <kbd>R</kbd> to reset, <kbd>J</kbd>/<kbd>K</kbd> or arrows to navigate.
  - Filter by status (All, Unreviewed, Confirmed, Denied) or score threshold ($\ge 0.20$, $\ge 0.35$, $\ge 0.45$, $\ge 0.50$).
  - Search by photo filename, caption, tag, or date.
  - Deep-link support: `pairs.html?photo=2e55ef7396.jpg`.
  - Live persistence to `photos/overrides.json` and one-click "Sync Kiosk" to recompute scores.
  - *Pairing philosophy:* suggested pairs do **not** require manual approval to be displayed. Approved items simply reinforce the algorithm by prioritizing your personal preferences.

- **Photo review (`review.html`)**:
  - Desktop inspection page for photo metadata, tags, and top partner rankings.
  - Direct links to review individual photo pairings in `pairs.html`.

- **Local review server (`scripts/serve.py`)**:
  - Run `python3 scripts/serve.py 8088` to serve the kiosk (`site/` at `/`) and review tools (`/tools/` and root `.html` files) locally.
  - Binds strictly to `127.0.0.1` by default (opt into LAN via `--lan`).
  - Security lockdown: refuses path segments starting with `.` (403 for `.env`, `.git`), `scripts/`, and `photos/originals/`. Rejects cross-origin POSTs whose `Origin` does not match the server's own `Host`.
  - Atomic write endpoints for `/api/overrides` (writing to a temporary file before atomic `os.replace`) and a 15-minute timeout on `/api/sync`.

### Stay-awake stack & kiosk resilience

Silk returns to the Echo Show home screen after ~10–15 minutes of idle time, and kiosks run unattended for days over Wi-Fi. The kiosk uses a multi-layer keep-awake and network resilience approach:
1. **Screen Wake Lock API**: requested on start, on visibility changes, every 60s, and re-requested in the `release` event handler when the document is visible.
2. **Audio keepalive**: looping silent audio base64 (`?audio=unmuted` enables 1% volume mode if required).
3. **Canvas video fallback**: hidden 2×2 canvas with an active 1s `setInterval` pixel toggle, connected via `captureStream(1)` only after window `'load'` (or immediate if complete) so page load completes normally without timing out browser readyState.
4. **Tap-to-start overlay**: non-blocking translucent pill in the bottom corner ("tap to keep awake") that leaves the slideshow playing underneath. Any tap anywhere arms wake lock and audio.
5. **Auto-hiding fullscreen button**: hides 10s after load (and immediately on arm); reappears for 10s on any tap while not fullscreen. Tapping it calls `arm()` directly. The `?debug=1` badge is positioned in the top-left to avoid colliding with fullscreen controls.
6. **Soft refresh & maintenance**:
   - Re-fetches `photos/photos.json` every hour with `cache: 'no-cache'`. If changed, updates `photos` and rebuilds the queue seamlessly at the next wrap.
   - Re-fetches `/` every hour with `cache: 'no-store'`, checks `<meta name="build" content="...">` (computed deterministically from a SHA256 hash of `index.html` and `sw.js`), and reloads only if the code changed and the browser is online.
   - Never reloads while offline.
   - Fallback full reload: only if the page has been running continuously for more than 7 days, at 4:00 AM local time, and online.
7. **Transient failure handling**:
   - Skips failed photos for the remainder of the round (and excludes them from partner matching) without dropping them permanently from `photos`.
   - Only removes a photo permanently after 3 confirmed load failures while `navigator.onLine` is true and at least one other photo has loaded in between.
   - Network outage hold: if 3 slides fail in a row, the kiosk stops advancing, holds the current slide on screen (never going blank), and retries every 30s until connectivity returns.
8. **Slide watchdog & load timeouts**:
   - 20-second timeout on image loading/decoding before rejection.
   - Watchdog timer: calls `next()` if no slide transition occurs within $3 \times$ the longest slide duration while the document is visible.
9. **Offline caching via Service Worker (`sw.js`)**:
   - Runtime cache-first strategy for immutable hashed photos (`/photos/*.jpg`, `.jpeg`, `.png`, `.webp`).
   - Network-first strategy (falling back to cache) for `/`, `/index.html`, and `/photos/photos.json` (cached under a single `/photos/photos.json` key regardless of query parameters).
   - On activate, cleans up cached photos no longer referenced in the latest manifest.
   - Proactive runtime pruning: sends a `postMessage` (`PRUNE_PHOTOS`) to the service worker after each successful `photos.json` refresh in the page to evict deleted photos from cache immediately.

## URL parameters

All query parameters are optional:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `interval` | number | `15` | Seconds per single slide |
| `pairhold` | number | `1.5` | Multiplier for paired slide duration |
| `shuffle` | `0` or `1` | `1` | Randomize photo order |
| `layout` | `auto` or `single` | `auto` | Set to `single` to disable pairing |
| `stack` | `3` | `off` | Enable 3-up vertical stacking for wide (e.g. 16:9) photos in portrait (mirror in landscape) |
| `audio` | `muted` or `unmuted` | `muted` | Set to `unmuted` to play keepalive audio at 1% volume |
| `debug` | `0` or `1` | `0` | Set to `1` to overlay filename, dimensions, crop losses, date, caption, tags, and pair scores, and expose `window.__kiosk` |

## Photo sync workflow

1. Setup virtual environment (first time):
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r scripts/requirements.txt
   ```
2. Add or replace image files in `photos/`.
3. (Optional) Provide `ANTHROPIC_API_KEY` via environment variable or in a repo-local `.env` file (gitignored).
4. Run the sync script:
   ```bash
   # Standard run with .venv python
   .venv/bin/python scripts/sync-photos.py

   # With Google Takeout album directory
   .venv/bin/python scripts/sync-photos.py --takeout ~/Downloads/Takeout/Google\ Photos/Display

   # Force re-tagging of all photos
   .venv/bin/python scripts/sync-photos.py --retag
   ```
   The script will:
   - Rename images to their 10-character SHA1 content hash.
   - Match Takeout metadata (or fall back to local EXIF).
   - Tag photos visually via Claude API (or load existing cache / skip gracefully).
   - Compute local color features (luma, sat, warmth, palette, phash).
   - Precompute pair compatibility scores and apply manual overrides.
   - Generate compact `photos/photos.json` and formatted `photos/photos.debug.json`.
   - Update `<meta name="build" content="...">` in `index.html`.
   - Print a summary of date sources, geo sources, and low-resolution photos (<1080px).
5. Open `review.html` in your browser to spot-check tags and pairings.
6. If needed, add adjustments to `photos/overrides.json` and re-run sync.
7. Commit changes and push to deploy via Netlify.
