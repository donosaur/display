# Echo Show 15 display kiosk

A self-contained photo slideshow kiosk for the Echo Show 15's Silk browser, deployed at `display.polisti.co`. It adapts to any window size, with primary optimization for the Echo Show 15 mounted in portrait orientation (1080×1920).

## Files

- `index.html` — single-file kiosk application (responsive pairing layout, score-weighted partner matching, smooth image decoding, stay-awake stack, fullscreen)
- `review.html` — standalone desktop review tool to inspect photo tags, captions, dates, and top 3 partners with scores (not linked from kiosk)
- `photos/` — slideshow images named by 10-character SHA1 content hash
  - `photos.json` — compact manifest with dimensions, dates, geo, visual tags, color features, and precomputed top partner scores
  - `photos.debug.json` — formatted copy of manifest for human inspection (gitignored)
  - `.tags-cache.json` — cached Claude API visual tags keyed by content hash
  - `overrides.json` — optional manual overrides for tags and pairings
- `scripts/sync-photos.py` — sync script to hash images, extract dates/geo, run Claude visual tagging, compute local color features, score pairs, and write manifests
- `netlify.toml` — headers configuration (immutable cache for hashed photos, no-cache for index and manifest)
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
  1. The pairable set is shuffled at the start of each round.
  2. For each unpaired photo, candidates are selected from its precomputed `partners` list that are unused this round, pairable in the current viewport, and have score >= 0.20.
  3. A partner is chosen at random among the top 3 qualifying candidates, weighted by score. This ensures pairs vary naturally across rounds while prioritizing the best matches.
  4. Leftovers pass: remaining unpaired photos attempt to pair with any other unused pairable photo where compatibility score > 0.
  5. Fallback pass: any remaining unused pairable photos are paired together sequentially (score 0).
  6. If an odd pairable photo remains, it renders uncropped (`contain`) over a blurred, darkened, scaled backdrop of itself (`filter: blur(40px) brightness(0.5); transform: scale(1.2)`).
  7. Event spreading: the queue reorders adjacent slides where possible to avoid showing two consecutive slides from the same calendar date.
- Paired slides split the screen with a 3px black gap (stacked top/bottom in portrait, side-by-side in landscape).
- Rebuilds the queue on `resize` and `orientationchange` (debounced ~300ms), maintaining the currently viewed photo on screen.

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

### Review tool (`review.html`)

A local desktop review page to verify tagging accuracy and pairing quality:
- Displays each photo with its thumbnail, dimensions, capture date, coordinates, caption, and tag badges.
- Shows the top 3 partner thumbnails with calculated compatibility scores and dates.
- Filter by tag (scene, subject, event, mood, black & white).
- Filter by "No date" to identify photos needing Takeout metadata.
- Filter by "Has partners" and search by caption or filename.
- Sort by date, partner count, or top score.

### Stay-awake stack & kiosk resilience

Silk returns to the Echo Show home screen after ~10–15 minutes of idle time. The kiosk uses a multi-layer keep-awake approach:
1. **Screen Wake Lock API**: requested once on start and re-requested periodically and on visibility changes.
2. **Audio keepalive**: looping silent audio base64 (`?audio=unmuted` enables 1% volume mode if required).
3. **Canvas video fallback**: hidden looping 2px canvas video stream via `captureStream()`.
4. **Tap-to-start overlay**: appears if autoplay is initially blocked.
5. **Nightly reload**: automatically reloads at 4:00 AM local time to refresh browser memory.

## URL parameters

All query parameters are optional:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `interval` | number | `15` | Seconds per single slide |
| `pairhold` | number | `1.5` | Multiplier for paired slide duration |
| `shuffle` | `0` or `1` | `1` | Randomize photo order |
| `layout` | `auto` or `single` | `auto` | Set to `single` to disable pairing |
| `audio` | `muted` or `unmuted` | `muted` | Set to `unmuted` to play keepalive audio at 1% volume |
| `debug` | `0` or `1` | `0` | Set to `1` to overlay filename, dimensions, crop loss, date, caption, tags, and pair scores |

## Photo sync workflow

1. Add or replace image files in `photos/`.
2. Run the sync script:
   ```bash
   # Standard run with Claude API key
   export ANTHROPIC_API_KEY="your-key-here"
   python3 scripts/sync-photos.py

   # With Google Takeout album directory
   python3 scripts/sync-photos.py --takeout ~/Downloads/Takeout/Google\ Photos/Display

   # Force re-tagging of all photos
   python3 scripts/sync-photos.py --retag
   ```
   The script will:
   - Rename images to their 10-character SHA1 content hash.
   - Match Takeout metadata (or fall back to local EXIF).
   - Tag photos visually via Claude API (or load existing cache / skip gracefully).
   - Compute local color features (luma, sat, warmth, palette, phash).
   - Precompute pair compatibility scores and apply manual overrides.
   - Generate compact `photos/photos.json` and formatted `photos/photos.debug.json`.
   - Print a summary of date sources, geo sources, and low-resolution photos (<1080px).
3. Open `review.html` in your browser to spot-check tags and pairings.
4. If needed, add adjustments to `photos/overrides.json` and re-run sync.
5. Commit changes and push to deploy via Netlify.
