# Echo Show 15 display page

A self-contained photo-slideshow kiosk page for the Echo Show 15's Silk
browser, deployed at `polisti.co/display`.

## Files

- `index.html` — the whole page (slideshow + keep-awake stack + fullscreen)
- `photos/` — the 372 slideshow images + `photos.json` manifest
- `README.md` — this file

## What it does

- Full-bleed photo slideshow, crossfading every 15 seconds (configurable
  via `SLIDE_MS` at the top of the script). Order shuffles on load.
- Photos show uncropped (`background-size: contain`) on black.
- Subtle clock/date overlay at the bottom (`SHOW_CLOCK` toggles it).
- Tapping the screen advances to the next photo (after the first arming tap).

## Stay-awake stack (Silk returns to the home screen after ~10-15 min idle)

1. Screen Wake Lock API, re-requested every 60s and on visibility change.
2. Looping 1s silent audio (base64 WAV in the page) — the keep-silk-open
   trick; Silk treats active media as engagement and skips its timeout.
   Default is muted. Append `?audio=unmuted` to test with `muted` removed
   and `volume = 0.01` if muted media fails to prevent timeout on Echo Show.
   - Mode confirmed: [Unconfirmed — test on Echo Show: default muted vs ?audio=unmuted]
3. Fallback: 2px canvas captureStream in a hidden looping muted video.
4. "Tap anywhere to start" overlay if autoplay is blocked pre-gesture.

The green dot (bottom right) means the wake lock is active.

## Fullscreen

Requests fullscreen on first tap (browsers require a gesture), re-enters on
visibility change, and shows a small "fullscreen" button bottom-left until
engaged. Degrades gracefully if Silk refuses fullscreen.

## Updating the photos

1. Add/remove images in the Google Photos "Display" album
   (https://photos.app.goo.gl/aApkvGk8CEChkoCF7).
2. Download full-size copies into `photos/` as `photo-NNN.jpg`.
3. Regenerate `photos/photos.json` (a JSON array of filenames), or edit it
   by hand. The page also carries an inline fallback list.
4. Redeploy `index.html` + `photos/`.
