# Echo Show 15 display kiosk

A self-contained photo slideshow kiosk for the Echo Show 15's Silk browser, deployed at `display.polisti.co`. It adapts to any window size, with primary optimization for the Echo Show 15 mounted in portrait orientation (1080×1920).

## Files

- `index.html` — single-file kiosk application (responsive pairing layout, image decoding, timer, stay-awake stack, fullscreen)
- `photos/` — slideshow images named by 10-character SHA1 content hash, plus `photos.json` manifest with image dimensions
- `scripts/sync-photos.py` — sync utility to hash images, parse dimensions honoring EXIF orientation, write manifest, and flag low-resolution photos
- `netlify.toml` — headers configuration (immutable cache for hashed photos, no-cache for index and manifest)
- `README.md` — documentation
- `HANDOFF.md` — project handoff state

## How it works

### Responsive pairing layout

To prevent mismatched photo orientations from being heavily cropped, the slideshow pairs images based on calculated crop loss:

- Reads viewport size using `window.visualViewport` (falling back to `window.innerWidth/innerHeight`). Viewport aspect ratio `va = w / h`.
- For image aspect `ia` in cell aspect `ca`, crop loss fraction is `1 - min(ia, ca) / max(ia, ca)`.
- Calculates loss for full viewport (`ca = va`) and half cell (`ca = 2 * va` in portrait, `ca = va / 2` in landscape).
- Images with half-cell crop loss at least 0.15 lower than full-viewport loss are marked pairable. In portrait (va < 1), this pairs landscape photos; in landscape, it pairs portrait photos.
- Slide queue is built by shuffling photos and pairing each pairable photo with the next unused pairable one. Near-square and well-fitting images stay single.
- Paired slides split the screen with a 3px black gap (stacked top/bottom in portrait, side-by-side in landscape).
- If an odd pairable image is left over at the end of the queue, it renders uncropped (`contain`) over a blurred, darkened, scaled backdrop of itself (`filter: blur(40px) brightness(0.5); transform: scale(1.2)`).
- Rebuilds the queue on `resize` and `orientationchange` (debounced ~300ms), maintaining the currently viewed photo on screen.

### Smooth crossfading and error resilience

- Before crossfading to a slide, all images in the slide are loaded and decoded using `new Image()` and `img.decode()` (falling back to `onload`).
- Crossfade only begins once all images in the incoming slide have resolved.
- If an image fails to load, it is dropped from the active pool, the slide queue is rebuilt, and the slideshow continues without ever fading to black.
- The next slide's images are preloaded in the background while the current slide displays.

### Interaction and timer

- The first user tap (`pointerup`) arms the stay-awake media and requests fullscreen. Subsequent taps advance to the next slide.
- Slide transitions run on a `setTimeout` chain restarted inside `next()`, so manual advance never gets interrupted by an immediate timer tick.
- Single slides display for 15 seconds by default; paired slides stay 1.5× longer (22.5s) to allow viewing both photos.
- When the queue wraps around, photos are reshuffled and rebuilt, ensuring the first slide of the new round does not repeat photos from the previous slide.
- Re-fetches `photos/photos.json` with exponential backoff (5s, 15s, 60s, then every 5 min) if loading fails, displaying a subtle notice on black.
- Automatically reloads the page nightly at 4:00 AM local time to pick up new deploys and release browser memory.

### Stay-awake stack

Silk returns to the Echo Show home screen after ~10–15 minutes of idle time. The kiosk uses a multi-layer keep-awake approach:

1. **Screen Wake Lock API**: requested once on start and re-requested on visibility change or lock release.
2. **Audio keepalive**: looping 1-second silent WAV base64 audio. Silk treats active media playback as user engagement.
   - Default mode: muted.
   - Test mode: append `?audio=unmuted` to remove `muted` and set `volume = 0.01`.
   - Mode confirmed: [Unconfirmed, test on Echo Show: default muted vs ?audio=unmuted]
3. **Canvas video fallback**: hidden looping 2px canvas video stream via `captureStream()`.
4. **Tap-to-start overlay**: appears if initial autoplay is blocked by browser policy before the first gesture.

### Fullscreen

Requests fullscreen on the first tap, re-enters fullscreen on visibility change, and shows a floating button until fullscreen is active. Degrades gracefully if fullscreen is unavailable or denied.

## URL parameters

All query parameters are optional:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `interval` | number | `15` | Seconds per single slide |
| `pairhold` | number | `1.5` | Multiplier for paired slide duration |
| `shuffle` | `0` or `1` | `1` | Randomize photo order |
| `layout` | `auto` or `single` | `auto` | Set to `single` to disable pairing |
| `audio` | `muted` or `unmuted` | `muted` | Set to `unmuted` to play keepalive audio at 1% volume |
| `debug` | `0` or `1` | `0` | Set to `1` to overlay filename, dimensions, and crop loss metrics on each photo |

Invalid or missing parameter values automatically fall back to their defaults.

## Photo sync workflow

1. Add or replace image files in `photos/`.
2. Run the sync script:
   ```bash
   python3 scripts/sync-photos.py
   ```
   The script will:
   - Rename each image to its 10-character SHA1 content hash.
   - Read image dimensions, honoring EXIF orientation.
   - Generate `photos/photos.json`.
   - Print a list of any images with longest edge under 1080px (for swapping with higher-resolution copies).
3. Commit changes and push to deploy via Netlify.
