#!/usr/bin/env python3
"""
inspect-quality.py — Photo quality and resolution inspector for Display Kiosk.

Analyzes all photos in photos/ for:
- Resolution (width, height, megapixels, longest/shortest edge)
- Compression density (bytes per pixel)
- Sharpness / blur (Laplacian variance)
- Overall quality tier: CRITICAL, NEEDS_UPSCALE, MARGINAL, GOOD

Outputs terminal report or JSON summary for automated batch upscaling.
"""

import os
import sys
import json
import argparse
from PIL import Image
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
SITE_DIR = os.path.join(REPO_DIR, "site")
SITE_PHOTOS_DIR = os.path.join(SITE_DIR, "photos")
ROOT_PHOTOS_DIR = os.path.join(REPO_DIR, "photos")

PHOTOS_DIR = SITE_PHOTOS_DIR if os.path.isdir(SITE_PHOTOS_DIR) else ROOT_PHOTOS_DIR
MANIFEST_PATH = os.path.join(PHOTOS_DIR, "photos.json")
REPORT_PATH = os.path.join(ROOT_PHOTOS_DIR, "quality-report.json")


def analyze_photo(photo_meta, photos_dir):
    filename = photo_meta["f"]
    filepath = os.path.join(photos_dir, filename)
    if not os.path.isfile(filepath):
        return None

    file_size_bytes = os.path.getsize(filepath)
    file_size_kb = file_size_bytes / 1024.0

    w = photo_meta.get("w")
    h = photo_meta.get("h")
    if not w or not h:
        try:
            with Image.open(filepath) as img:
                w, h = img.size
        except Exception:
            return None

    pixels = w * h
    megapixels = pixels / 1_000_000.0
    longest_edge = max(w, h)
    shortest_edge = min(w, h)
    aspect_ratio = round(w / h, 2)
    bytes_per_pixel = file_size_bytes / pixels if pixels > 0 else 0.0

    # Sharpness via Laplacian variance
    is_video = photo_meta.get("type") == "video" or filepath.lower().endswith((".mp4", ".webm", ".mov", ".m4v"))
    lap_var = 0.0
    try:
        if is_video:
            import subprocess, shutil, io
            ffmpeg_bin = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
            if os.path.isfile(ffmpeg_bin):
                cmd = [ffmpeg_bin, "-ss", "0.5", "-i", filepath, "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
                res = subprocess.run(cmd, capture_output=True, check=True)
                with Image.open(io.BytesIO(res.stdout)) as img:
                    gray = np.array(img.convert("L"), dtype=np.float32)
                    if gray.shape[0] > 2 and gray.shape[1] > 2:
                        lap = (
                            gray[:-2, 1:-1]
                            + gray[2:, 1:-1]
                            + gray[1:-1, :-2]
                            + gray[1:-1, 2:]
                            - 4 * gray[1:-1, 1:-1]
                        )
                        lap_var = float(lap.var())
        else:
            with Image.open(filepath) as img:
                gray = np.array(img.convert("L"), dtype=np.float32)
                if gray.shape[0] > 2 and gray.shape[1] > 2:
                    lap = (
                        gray[:-2, 1:-1]
                        + gray[2:, 1:-1]
                        + gray[1:-1, :-2]
                        + gray[1:-1, 2:]
                        - 4 * gray[1:-1, 1:-1]
                    )
                    lap_var = float(lap.var())
    except Exception:
        lap_var = 0.0

    # Quality classification
    # A standard 1080p kiosk has a minimum dimension need of ~1080 on longest or shortest edge depending on orientation
    if longest_edge < 650:
        tier = "CRITICAL"
        reason = f"Severely undersized ({w}x{h}, {megapixels:.2f}MP)"
    elif longest_edge < 1080 or shortest_edge < 600:
        tier = "NEEDS_UPSCALE"
        reason = f"Below 1080p display threshold ({longest_edge}px max)"
    elif bytes_per_pixel < 0.10 and megapixels < 1.2:
        tier = "MARGINAL"
        reason = f"Heavy compression artifacts ({bytes_per_pixel:.3f} BPP)"
    elif lap_var < 45.0 and megapixels < 1.5:
        tier = "MARGINAL"
        reason = f"Noticeably soft / low edge definition (lap: {lap_var:.1f})"
    else:
        tier = "GOOD"
        reason = "Meets or exceeds display baseline"

    # Score calculation (0 to 100)
    # 1080p target = 100%, scaled by resolution, compression quality, and sharpness
    res_factor = min(1.0, longest_edge / 1080.0)
    bpp_factor = min(1.0, bytes_per_pixel / 0.25)
    sharp_factor = min(1.0, lap_var / 300.0)

    quality_score = round((res_factor * 0.65 + bpp_factor * 0.20 + sharp_factor * 0.15) * 100, 1)

    return {
        "filename": filename,
        "width": w,
        "height": h,
        "longest_edge": longest_edge,
        "shortest_edge": shortest_edge,
        "aspect_ratio": aspect_ratio,
        "megapixels": round(megapixels, 2),
        "size_kb": round(file_size_kb, 1),
        "bytes_per_pixel": round(bytes_per_pixel, 3),
        "laplacian_var": round(lap_var, 1),
        "tier": tier,
        "reason": reason,
        "quality_score": quality_score,
        "date": photo_meta.get("date"),
        "caption": photo_meta.get("caption"),
        "tags": photo_meta.get("tags"),
    }


def main():
    parser = argparse.ArgumentParser(description="Inspect photo quality and flag upscale candidates.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON to stdout")
    parser.add_argument("--save", action="store_true", default=True, help="Save report to photos/quality-report.json")
    parser.add_argument("--tier", choices=["ALL", "CRITICAL", "NEEDS_UPSCALE", "MARGINAL", "GOOD"], default="ALL", help="Filter by tier")
    parser.add_argument("--limit", type=int, default=50, help="Max items to print in table")
    args = parser.parse_args()

    if not os.path.isfile(MANIFEST_PATH):
        print(f"Error: Manifest not found at {MANIFEST_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        photos = json.load(f)

    analyzed = []
    for p in photos:
        res = analyze_photo(p, PHOTOS_DIR)
        if res:
            analyzed.append(res)

    # Sort primarily by tier severity, then quality score ascending (worst first)
    tier_order = {"CRITICAL": 0, "NEEDS_UPSCALE": 1, "MARGINAL": 2, "GOOD": 3}
    analyzed.sort(key=lambda x: (tier_order.get(x["tier"], 4), x["quality_score"], x["longest_edge"]))

    # Summary counts
    tier_counts = {"CRITICAL": 0, "NEEDS_UPSCALE": 0, "MARGINAL": 0, "GOOD": 0}
    for item in analyzed:
        tier_counts[item["tier"]] = tier_counts.get(item["tier"], 0) + 1

    report_data = {
        "total_photos": len(analyzed),
        "tier_counts": tier_counts,
        "candidates_count": tier_counts["CRITICAL"] + tier_counts["NEEDS_UPSCALE"],
        "photos": analyzed,
    }

    if args.save:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)

    if args.json:
        print(json.dumps(report_data, indent=2))
        return

    # Print human-readable report
    print("\n=======================================================")
    print("             PHOTO QUALITY AUDIT REPORT                ")
    print("=======================================================")
    print(f"Total Photos Analyzed: {len(analyzed)}")
    print(f"  - CRITICAL (<650px)       : {tier_counts['CRITICAL']:>3}  (Severely pixelated on kiosk)")
    print(f"  - NEEDS UPSCALE (<1080px) : {tier_counts['NEEDS_UPSCALE']:>3}  (Noticeably soft/stretched)")
    print(f"  - MARGINAL (1080px soft)  : {tier_counts['MARGINAL']:>3}  (Compressed or low detail)")
    print(f"  - GOOD (>=1080px clean)   : {tier_counts['GOOD']:>3}  (Ready for crisp display)")
    print(f"\nTotal Upscale Candidates  : {tier_counts['CRITICAL'] + tier_counts['NEEDS_UPSCALE']:>3}")
    print("=======================================================\n")

    filtered = analyzed
    if args.tier != "ALL":
        filtered = [x for x in analyzed if x["tier"] == args.tier]

    print(f"{'Filename':<16} {'Resolution':<12} {'MP':<6} {'Size':<8} {'BPP':<6} {'Score':<6} {'Tier':<14} {'Date':<11} {'Notes'}")
    print("-" * 105)

    for item in filtered[:args.limit]:
        fn = item["filename"]
        res_str = f"{item['width']}x{item['height']}"
        mp_str = f"{item['megapixels']}M"
        sz_str = f"{item['size_kb']}K"
        bpp_str = f"{item['bytes_per_pixel']:.2f}"
        score_str = f"{item['quality_score']}"
        tier_str = item["tier"]
        date_str = (item["date"] or "No Date")[:10]
        notes = item["reason"]
        print(f"{fn:<16} {res_str:<12} {mp_str:<6} {sz_str:<8} {bpp_str:<6} {score_str:<6} {tier_str:<14} {date_str:<11} {notes}")

    if len(filtered) > args.limit:
        print(f"... and {len(filtered) - args.limit} more photos.")

    print(f"\nReport written to: {REPORT_PATH}\n")


if __name__ == "__main__":
    main()
