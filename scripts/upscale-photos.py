#!/usr/bin/env python3
"""
upscale-photos.py — Automated AI upscaling and enhancement pipeline for Display Kiosk.

Features:
- Identifies candidates using photos/quality-report.json or direct dimension inspection
- Non-destructive: backs up originals to photos/originals/
- Multiple enhancement backends:
  1. 'realesrgan-ncnn' (Local Apple Silicon/Mac GPU via Vulkan - recommended)
  2. 'replicate' (Cloud API: Real-ESRGAN or CodeFormer with face restoration)
  3. 'preview' (Generates side-by-side comparison files and HTML slider)
- Updates manifest by calling sync-photos if desired
"""

import os
import sys
import json
import shutil
import argparse
import subprocess
from datetime import datetime
from PIL import Image

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
SITE_DIR = os.path.join(REPO_DIR, "site")
SITE_PHOTOS_DIR = os.path.join(SITE_DIR, "photos")
ROOT_PHOTOS_DIR = os.path.join(REPO_DIR, "photos")

PHOTOS_DIR = SITE_PHOTOS_DIR if os.path.isdir(SITE_PHOTOS_DIR) else ROOT_PHOTOS_DIR
ORIGINALS_DIR = os.path.join(ROOT_PHOTOS_DIR, "originals")
COMPARISONS_DIR = os.path.join(ROOT_PHOTOS_DIR, "comparisons")
MANIFEST_PATH = os.path.join(PHOTOS_DIR, "photos.json")
REPORT_PATH = os.path.join(ROOT_PHOTOS_DIR, "quality-report.json")


def load_candidates(tier=None, limit=None):
    """Load upscale candidates from quality report or manifest."""
    candidates = []
    if os.path.isfile(REPORT_PATH):
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        photos = data.get("photos", [])
        for p in photos:
            if tier and p.get("tier") != tier:
                continue
            if not tier and p.get("tier") not in ("CRITICAL", "NEEDS_UPSCALE"):
                continue
            candidates.append(p)
    elif os.path.isfile(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            photos = json.load(f)
        for p in photos:
            max_dim = max(p.get("w", 0), p.get("h", 0))
            if max_dim < 1080:
                candidates.append({
                    "filename": p["f"],
                    "width": p.get("w"),
                    "height": p.get("h"),
                    "longest_edge": max_dim,
                    "tier": "CRITICAL" if max_dim < 650 else "NEEDS_UPSCALE"
                })

    if limit and limit > 0:
        candidates = candidates[:limit]

    return candidates


def backup_original(filename):
    """Ensure original file is backed up in photos/originals/ before modification."""
    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    src = os.path.join(PHOTOS_DIR, filename)
    dst = os.path.join(ORIGINALS_DIR, filename)
    if os.path.isfile(src) and not os.path.isfile(dst):
        shutil.copy2(src, dst)
        return True
    return False


def find_realesrgan_binary():
    """Look for realesrgan-ncnn-vulkan binary in PATH or local repo bin."""
    search_paths = [
        shutil.which("realesrgan-ncnn-vulkan"),
        os.path.join(REPO_DIR, "bin", "realesrgan-ncnn-vulkan"),
        os.path.expanduser("~/bin/realesrgan-ncnn-vulkan"),
        "/opt/homebrew/bin/realesrgan-ncnn-vulkan",
    ]
    for p in search_paths:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def run_local_realesrgan(input_path, output_path, scale=4, model="realesrgan-x4plus", target_max_edge=2400):
    """Run local Real-ESRGAN binary with optical blending, natural grain, and EXIF preservation."""
    import tempfile
    import numpy as np
    from PIL import ImageFilter, ImageOps

    binary = find_realesrgan_binary()
    if not binary:
        raise FileNotFoundError(
            "realesrgan-ncnn-vulkan binary not found. Download or install it to PATH or ./bin/"
        )

    with Image.open(input_path) as im:
        orig_img = ImageOps.exif_transpose(im)
        exif_bytes = orig_img.info.get("exif")
        if orig_img.mode != "RGB":
            orig_img = orig_img.convert("RGB")
        orig_w, orig_h = orig_img.size

    longest = max(orig_w, orig_h)
    if longest <= 650:
        target_longest = longest * 4
    elif longest <= 1200:
        target_longest = 2160
    else:
        target_longest = target_max_edge

    scale_factor = target_longest / float(longest)
    target_w = int(round(orig_w * scale_factor))
    target_h = int(round(orig_h * scale_factor))

    # Pre-scale input if needed so 4x Real-ESRGAN matches target ceiling
    in_longest = min(longest, int(round(target_longest / 4.0)))
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_in = os.path.join(tmpdir, "in.jpg")
        tmp_out = os.path.join(tmpdir, "out.jpg")
        if in_longest < longest:
            in_scale = in_longest / float(longest)
            in_w = max(1, int(round(orig_w * in_scale)))
            in_h = max(1, int(round(orig_h * in_scale)))
            orig_img.resize((in_w, in_h), Image.Resampling.LANCZOS).save(tmp_in, "JPEG", quality=98)
        else:
            orig_img.save(tmp_in, "JPEG", quality=98)

        cmd = [
            binary,
            "-i", tmp_in,
            "-o", tmp_out,
            "-s", str(scale),
            "-n", model,
            "-t", "400",
        ]
        models_dir = os.path.join( os.path.dirname(binary), "models")
        if os.path.isdir(models_dir):
            cmd.extend(["-m", models_dir])

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0 or not os.path.isfile(tmp_out):
            raise RuntimeError(f"Real-ESRGAN failed: {res.stderr}")

        with Image.open(tmp_out) as sr_im:
            sr_img = sr_im.convert("RGB")
            if sr_img.size != (target_w, target_h):
                sr_img = sr_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        # Blend 88% neural reconstruction with 12% de-blocked optical reference + subtle film grain
        base_smooth = orig_img.filter(ImageFilter.GaussianBlur(radius=0.6)).resize(
            (target_w, target_h), Image.Resampling.BICUBIC
        )
        sr_arr = np.asarray(sr_img, dtype=np.float32)
        base_arr = np.asarray(base_smooth, dtype=np.float32)
        blended = 0.88 * sr_arr + 0.12 * base_arr

        rng = np.random.default_rng(42)
        grain = rng.normal(0.0, 1.3, size=(target_h, target_w, 1)).astype(np.float32)
        final_arr = np.clip(blended + grain, 0, 255).astype(np.uint8)
        final_img = Image.fromarray(final_arr, mode="RGB")

        save_kwargs = {"quality": 95, "optimize": True}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        final_img.save(output_path, "JPEG", **save_kwargs)



def run_replicate_upscale(input_path, output_path, scale=4, face_enhance=True, token=None):
    """Run upscaling via Replicate API."""
    import urllib.request
    import time

    api_token = token or os.environ.get("REPLICATE_API_TOKEN")
    if not api_token:
        raise ValueError("REPLICATE_API_TOKEN environment variable required for Replicate backend")

    import base64
    with open(input_path, "rb") as f:
        img_b64 = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("utf-8")

    # Use sczhou/codeformer if face_enhance, else nightmareai/real-esrgan
    model_version = "sczhou/codeformer" if face_enhance else "nightmareai/real-esrgan"

    headers = {
        "Authorization": f"Token {api_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "version": "7de2ea26c616d5bfde245ad0d11cf0f1b81f14d0f49f7b3fced83d54e4d36b81" if face_enhance else "f121370def2e7cd9e9e31d7f417163e25b68d5b70b001c3e9d5b136fc9e88049",
        "input": {
            "image": img_b64,
            "upscale": scale,
            "face_upsample": face_enhance,
            "background_enhance": True,
            "codeformer_fidelity": 0.75
        }
    }

    req = urllib.request.Request(
        "https://api.replicate.com/v1/predictions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers
    )
    with urllib.request.urlopen(req) as resp:
        prediction = json.loads(resp.read().decode("utf-8"))

    poll_url = prediction["urls"]["get"]
    while prediction["status"] not in ("succeeded", "failed", "canceled"):
        time.sleep(1.5)
        poll_req = urllib.request.Request(poll_url, headers=headers)
        with urllib.request.urlopen(poll_req) as resp:
            prediction = json.loads(resp.read().decode("utf-8"))

    if prediction["status"] != "succeeded":
        raise RuntimeError(f"Prediction failed with status {prediction['status']}: {prediction.get('error')}")

    output_url = prediction["output"]
    urllib.request.urlretrieve(output_url, output_path)


def generate_comparison_html(comparisons):
    """Generate an interactive before/after visual review page."""
    html_path = os.path.join(REPO_DIR, "compare.html")
    items_json = json.dumps(comparisons, indent=2)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Upscale Comparisons — Display Kiosk</title>
<style>
  :root {{
    --bg: #f7f6f3;
    --surface: #ffffff;
    --border: #e7e5e4;
    --text: #1c1917;
    --text-muted: #78716c;
    --accent: #c2410c;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px;
    max-width: 1400px;
    margin: 0 auto;
  }}
  header {{
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }}
  h1 {{ font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }}
  .subhead {{ font-size: 13px; color: var(--text-muted); }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(600px, 1fr));
    gap: 24px;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}
  .card-header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 13px;
    font-family: ui-monospace, Menlo, monospace;
  }}
  .filename {{ font-weight: 600; }}
  .meta {{ color: var(--text-muted); font-size: 12px; }}
  .compare-view {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    background: #000;
    padding: 8px;
    border-radius: 4px;
  }}
  .pane {{
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
  }}
  .pane img {{
    max-width: 100%;
    max-height: 440px;
    object-fit: contain;
    display: block;
    background: #111;
  }}
  .pane-label {{
    color: #a8a29e;
    font-size: 11px;
    font-family: ui-monospace, Menlo, monospace;
  }}
</style>
</head>
<body>
<header>
  <div>
    <h1>AI Upscaling & Detail Comparison</h1>
    <div class="subhead">Side-by-side inspection for before/after enhancement validation</div>
  </div>
  <div class="subhead"><a href="review.html" style="color: var(--accent); text-decoration: none;">&larr; Back to Photo Review</a></div>
</header>
<main class="grid" id="grid"></main>
<script>
  var items = {items_json};
  var grid = document.getElementById('grid');
  items.forEach(function(item) {{
    var card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="card-header">
        <span class="filename">${{item.filename}}</span>
        <span class="meta">${{item.orig_w}}x${{item.orig_h}} &rarr; ${{item.new_w}}x${{item.new_h}}</span>
      </div>
      <div class="compare-view">
        <div class="pane">
          <img src="${{item.before_src}}" alt="Original" loading="lazy">
          <span class="pane-label">Original (${{item.orig_w}}x${{item.orig_h}})</span>
        </div>
        <div class="pane">
          <img src="${{item.after_src}}" alt="Upscaled" loading="lazy">
          <span class="pane-label">Enhanced (${{item.new_w}}x${{item.new_h}})</span>
        </div>
      </div>
    `;
    grid.appendChild(card);
  }});
</script>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Generated comparison UI at: {html_path}")


def main():
    parser = argparse.ArgumentParser(description="Process display photos for AI upscaling and enhancement.")
    parser.add_argument("--tier", choices=["CRITICAL", "NEEDS_UPSCALE"], default=None, help="Process specific tier")
    parser.add_argument("--file", type=str, default=None, help="Process a single photo by filename")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of photos to process")
    parser.add_argument("--backend", choices=["realesrgan-ncnn", "replicate", "dry-run"], default="dry-run", help="Upscaling backend")
    parser.add_argument("--scale", type=int, choices=[2, 4], default=4, help="Upscale factor (2x or 4x)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without modifying files")
    args = parser.parse_args()

    # Determine files to process
    if args.file:
        filepath = os.path.join(PHOTOS_DIR, args.file)
        if not os.path.isfile(filepath):
            print(f"Error: Photo {args.file} not found in {PHOTOS_DIR}", file=sys.stderr)
            sys.exit(1)
        with Image.open(filepath) as img:
            w, h = img.size
        candidates = [{
            "filename": args.file,
            "width": w,
            "height": h,
            "longest_edge": max(w, h),
            "tier": "CRITICAL" if max(w, h) < 650 else "NEEDS_UPSCALE"
        }]
    else:
        candidates = load_candidates(tier=args.tier, limit=args.limit)

    print(f"\nFound {len(candidates)} photo(s) selected for processing.")
    print(f"Backend: {args.backend} | Scale: {args.scale}x\n")

    if not candidates:
        print("No candidates found matching criteria.")
        return

    for c in candidates:
        fn = c["filename"]
        w = c.get("width")
        h = c.get("height")
        tier = c.get("tier", "UNKNOWN")
        print(f"  • {fn:<16} {w}x{h} ({tier}) -> target ~{w * args.scale}x{h * args.scale}")

    if args.dry_run or args.backend == "dry-run":
        print("\n[Dry Run] No files modified. To process, specify --backend realesrgan-ncnn or --backend replicate")
        realesr_bin = find_realesrgan_binary()
        print(f"Local realesrgan-ncnn binary detected: {'YES (' + realesr_bin + ')' if realesr_bin else 'NO'}")
        print(f"Replicate API Token in env: {'YES' if os.environ.get('REPLICATE_API_TOKEN') else 'NO'}")
        return

    # Process items
    processed = []
    comparisons = []
    os.makedirs(COMPARISONS_DIR, exist_ok=True)

    for c in candidates:
        fn = c["filename"]
        orig_path = os.path.join(PHOTOS_DIR, fn)
        backup_original(fn)

        comp_before = os.path.join(COMPARISONS_DIR, f"{os.path.splitext(fn)[0]}_orig.jpg")
        comp_after = os.path.join(COMPARISONS_DIR, f"{os.path.splitext(fn)[0]}_enhanced.jpg")
        shutil.copy2(orig_path, comp_before)

        print(f"Processing {fn}...")
        try:
            if args.backend == "realesrgan-ncnn":
                run_local_realesrgan(comp_before, comp_after, scale=args.scale)
            elif args.backend == "replicate":
                run_replicate_upscale(comp_before, comp_after, scale=args.scale)

            # Check new dimensions
            with Image.open(comp_after) as img:
                new_w, new_h = img.size

            comparisons.append({
                "filename": fn,
                "orig_w": c["width"],
                "orig_h": c["height"],
                "new_w": new_w,
                "new_h": new_h,
                "before_src": f"photos/comparisons/{os.path.basename(comp_before)}",
                "after_src": f"photos/comparisons/{os.path.basename(comp_after)}"
            })
            processed.append(fn)

        except Exception as e:
            print(f"  Error processing {fn}: {e}", file=sys.stderr)

    if comparisons:
        generate_comparison_html(comparisons)

    print(f"\nSuccessfully processed {len(processed)} photo(s).")


if __name__ == "__main__":
    main()
