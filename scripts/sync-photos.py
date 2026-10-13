#!/usr/bin/env python3
"""Sync photos for the display kiosk.

Renames every image in photos/ to a content hash (first 10 hex characters of sha1 + extension),
reads dimensions honoring EXIF orientation, extracts capture date and geo location
(supporting Google Takeout exports and EXIF metadata fallback), writes photos/photos.json,
and reports images whose longest edge is under 1080px.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import struct
import sys

# Ensure PIL and numpy can be imported from local .venv if run with system python
try:
    from PIL import Image, ImageOps
except ImportError:
    venv_site = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".venv", "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages"
    )
    if os.path.isdir(venv_site) and venv_site not in sys.path:
        sys.path.insert(0, venv_site)
    from PIL import Image, ImageOps

try:
    import numpy as np
except ImportError:
    venv_site = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".venv", "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages"
    )
    if os.path.isdir(venv_site) and venv_site not in sys.path:
        sys.path.insert(0, venv_site)
    import numpy as np

PHOTOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photos")
MANIFEST_PATH = os.path.join(PHOTOS_DIR, "photos.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def get_dimensions_pillow(path):
    try:
        with Image.open(path) as img:
            transposed = ImageOps.exif_transpose(img)
            return transposed.width, transposed.height
    except Exception:
        return None


def get_jpeg_dimensions(data):
    """Pure-Python JPEG dimension and EXIF orientation parser."""
    if len(data) < 2 or data[:2] != b"\xff\xd8":
        return None
    idx = 2
    orientation = 1
    width, height = None, None
    length = len(data)
    while idx < length:
        if data[idx] != 0xff:
            idx += 1
            continue
        marker = data[idx + 1]
        idx += 2
        if marker in (0xd8, 0xd9):  # SOI, EOI
            continue
        if marker == 0xda:  # SOS (Start of Scan) - image data starts
            break
        if idx + 2 > length:
            break
        chunk_len = struct.unpack(">H", data[idx:idx + 2])[0]
        chunk_data = data[idx + 2:idx + chunk_len]

        # APP1 - Exif
        if marker == 0xe1 and chunk_data.startswith(b"Exif\x00\x00"):
            tiff_data = chunk_data[6:]
            if len(tiff_data) >= 8:
                endian = tiff_data[:2]
                fmt = "<" if endian == b"II" else (">" if endian == b"MM" else None)
                if fmt:
                    magic, ifd_offset = struct.unpack(fmt + "HI", tiff_data[2:8])
                    if magic == 42 and ifd_offset < len(tiff_data):
                        num_entries = struct.unpack(fmt + "H", tiff_data[ifd_offset:ifd_offset + 2])[0]
                        for i in range(num_entries):
                            entry_offset = ifd_offset + 2 + i * 12
                            if entry_offset + 12 > len(tiff_data):
                                break
                            tag, typ, cnt, val = struct.unpack(fmt + "HHI4s", tiff_data[entry_offset:entry_offset + 12])
                            if tag == 0x0112:  # Orientation tag
                                orientation = struct.unpack(fmt + "H", val[:2])[0]
                                break

        # SOF markers: SOF0..SOF3, SOF5..SOF7, SOF9..SOF11, SOF13..SOF15
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            if len(chunk_data) >= 5:
                h, w = struct.unpack(">HH", chunk_data[1:5])
                width, height = w, h

        idx += chunk_len

    if width and height:
        # Orientations 5, 6, 7, 8 swap width and height
        if orientation in (5, 6, 7, 8):
            width, height = height, width
        return width, height
    return None


def get_image_dimensions(path):
    dims = get_dimensions_pillow(path)
    if dims:
        return dims
    try:
        with open(path, "rb") as f:
            data = f.read()
        dims = get_jpeg_dimensions(data)
        if dims:
            return dims
    except Exception:
        pass
    return None


# Precomputed 32x32 DCT matrix for 64-bit pHash
_DCT_MATRIX = None

def _get_dct_matrix(n=32):
    global _DCT_MATRIX
    if _DCT_MATRIX is None:
        grid_n = np.arange(n)
        grid_k = np.arange(n)[:, None]
        c = np.cos(np.pi * grid_k * (2 * grid_n + 1) / (2 * n))
        c[0, :] *= 1.0 / np.sqrt(2.0)
        c *= np.sqrt(2.0 / n)
        _DCT_MATRIX = c
    return _DCT_MATRIX


def compute_phash(image_path):
    """Compute 64-bit DCT perceptual hash as 16-character hex string."""
    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("L").resize((32, 32), Image.Resampling.BILINEAR)
            pixels = np.asarray(img, dtype=np.float64)

        c = _get_dct_matrix(32)
        dct = c @ pixels @ c.T
        low_freq = dct[:8, :8]
        med = float(np.median(low_freq))
        bits = (low_freq > med).flatten()
        hash_int = 0
        for b in bits:
            hash_int = (hash_int << 1) | int(b)
        return f"{hash_int:016x}"
    except Exception:
        return None


def hamming_distance(h1, h2):
    """Hamming distance between two 16-hex perceptual hashes."""
    if not h1 or not h2:
        return 64
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def parse_exif_metadata(path):
    """Extract capture date and GPS coordinates from EXIF metadata."""
    date_val = None
    date_src = None
    geo_val = None
    geo_src = None

    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                exif_ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
                gps_ifd = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}

                # 1. Date: DateTimeOriginal (36867) -> DateTime (306)
                dt_orig = exif_ifd.get(36867) or exif_ifd.get(36868)
                dt_any = exif.get(306)

                raw_dt = None
                if dt_orig and isinstance(dt_orig, str):
                    raw_dt = dt_orig.strip("\x00").strip()
                    date_src = "EXIF DateTimeOriginal"
                elif dt_any and isinstance(dt_any, str):
                    raw_dt = dt_any.strip("\x00").strip()
                    date_src = "EXIF DateTime"

                if raw_dt:
                    m = re.match(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", raw_dt)
                    if m:
                        date_val = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}"
                    else:
                        date_val = raw_dt

                # 2. GPS coordinates
                if gps_ifd:
                    def _coord_to_deg(val):
                        if not val or len(val) < 3:
                            return 0.0
                        d, m, s = float(val[0]), float(val[1]), float(val[2])
                        return d + (m / 60.0) + (s / 3600.0)

                    lat_ref = gps_ifd.get(1)
                    lat_val = gps_ifd.get(2)
                    lng_ref = gps_ifd.get(3)
                    lng_val = gps_ifd.get(4)

                    if lat_val and lng_val and lat_ref and lng_ref:
                        lat = _coord_to_deg(lat_val) * (-1 if lat_ref in ["S", "s"] else 1)
                        lng = _coord_to_deg(lng_val) * (-1 if lng_ref in ["W", "w"] else 1)
                        if abs(lat) > 1e-4 or abs(lng) > 1e-4:
                            geo_val = [round(lat, 3), round(lng, 3)]
                            geo_src = "EXIF GPS"
    except Exception:
        pass

    return date_val, date_src, geo_val, geo_src


def find_takeout_sidecar(img_path, dir_json_map, json_title_map):
    """Find the sidecar JSON file for a given image in Takeout export.
    
    Handles truncated filenames, supplemental-metadata.json, and title lookup.
    """
    dirname = os.path.dirname(img_path)
    basename = os.path.basename(img_path)
    stem, ext = os.path.splitext(basename)

    # 1. Exact conventional names
    candidates = [
        f"{basename}.json",
        f"{basename}.supplemental-metadata.json",
        f"{stem}.json",
        f"{stem}.supplemental-metadata.json",
    ]
    for c in candidates:
        full_c = os.path.join(dirname, c)
        if os.path.isfile(full_c):
            return full_c

    # 2. Check title mapping (Takeout JSONs usually contain original filename in 'title')
    if basename in json_title_map:
        return json_title_map[basename]

    # 3. Truncated variants in same directory
    json_files = dir_json_map.get(dirname, [])
    for jf in json_files:
        jbase = os.path.basename(jf)
        # Takeout often truncates filenames at ~47-51 chars before adding .json or .supple...
        prefix_len = min(len(stem), 40)
        if prefix_len >= 8 and jbase.startswith(stem[:prefix_len]):
            return jf

    return None


def load_takeout_metadata(takeout_dir, local_photos):
    """Scan Takeout directory, match images to local photos, and extract date & geo."""
    if not takeout_dir or not os.path.isdir(takeout_dir):
        return {}

    print(f"Scanning Takeout directory: {takeout_dir}...")
    takeout_images = []
    dir_json_map = {}
    json_title_map = {}

    # Gather images and json files in Takeout directory
    for root, _, files in os.walk(takeout_dir):
        jsons_in_dir = []
        for f in files:
            full_path = os.path.join(root, f)
            ext = os.path.splitext(f)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                takeout_images.append(full_path)
            elif ext == ".json" and not f.startswith("metadata.json"):
                jsons_in_dir.append(full_path)
                # Read title from JSON for mapping
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as jf:
                        data = json.load(jf)
                        title = data.get("title")
                        if title:
                            json_title_map[title] = full_path
                except Exception:
                    pass
        if jsons_in_dir:
            dir_json_map[root] = jsons_in_dir

    print(f"Found {len(takeout_images)} images and {len(json_title_map)} metadata files in Takeout.")

    # Build lookup for local photos: sha1 -> photo_info, and compute phash for fallback
    local_by_sha1_10 = {}
    local_phashes = {}

    print("Computing hashes for local photos...")
    for p in local_photos:
        filename = p["f"]
        filepath = os.path.join(PHOTOS_DIR, filename)
        sha1_prefix = os.path.splitext(filename)[0]
        local_by_sha1_10[sha1_prefix] = filename
        phash = compute_phash(filepath)
        if phash:
            local_phashes[filename] = phash

    # Match takeout photos to local photos
    takeout_matches = {}  # local_filename -> {date, geo}
    matched_local_files = set()

    for t_img in takeout_images:
        try:
            with open(t_img, "rb") as f:
                content = f.read()
            t_sha1 = hashlib.sha1(content).hexdigest()
            t_sha1_10 = t_sha1[:10]

            matched_file = None
            if t_sha1_10 in local_by_sha1_10:
                matched_file = local_by_sha1_10[t_sha1_10]
            else:
                # Fallback: perceptual hash match
                t_phash = compute_phash(t_img)
                if t_phash:
                    best_match = None
                    best_dist = 64
                    for lf, lp in local_phashes.items():
                        if lf in matched_local_files:
                            continue
                        dist = hamming_distance(t_phash, lp)
                        if dist < best_dist:
                            best_dist = dist
                            best_match = lf
                    # If very close (re-encoded copy of exact same image)
                    if best_match and best_dist <= 8:
                        matched_file = best_match

            if not matched_file or matched_file in matched_local_files:
                continue

            # Read sidecar
            sidecar_path = find_takeout_sidecar(t_img, dir_json_map, json_title_map)
            if not sidecar_path:
                continue

            with open(sidecar_path, "r", encoding="utf-8", errors="ignore") as jf:
                sdata = json.load(jf)

            # Date from timestamp
            ts_str = sdata.get("photoTakenTime", {}).get("timestamp")
            t_date = None
            if ts_str:
                try:
                    ts = int(ts_str)
                    if ts > 0:
                        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        t_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    pass

            # Geo
            t_geo = None
            geo_obj = sdata.get("geoData") or sdata.get("geoDataExif") or {}
            lat = float(geo_obj.get("latitude", 0.0))
            lng = float(geo_obj.get("longitude", 0.0))
            if abs(lat) > 1e-4 or abs(lng) > 1e-4:
                t_geo = [round(lat, 3), round(lng, 3)]

            takeout_matches[matched_file] = {
                "date": t_date,
                "geo": t_geo,
            }
            matched_local_files.add(matched_file)
        except Exception:
            continue

    print(f"Matched {len(takeout_matches)} photos from Takeout.")
    return takeout_matches


def sync_photos(takeout_dir=None):
    if not os.path.isdir(PHOTOS_DIR):
        print(f"Error: Photos directory not found at {PHOTOS_DIR}", file=sys.stderr)
        sys.exit(1)

    # 1. Scan photos directory
    all_files = [f for f in os.listdir(PHOTOS_DIR) if not f.startswith(".")]
    image_files = [
        f for f in all_files
        if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS and f != "photos.json"
    ]

    print(f"Found {len(image_files)} image files in {PHOTOS_DIR}...")

    # 2. Hash and rename
    renamed_entries = []
    for filename in sorted(image_files):
        old_path = os.path.join(PHOTOS_DIR, filename)
        with open(old_path, "rb") as f:
            content = f.read()
        sha1_hash = hashlib.sha1(content).hexdigest()[:10]
        ext = os.path.splitext(filename)[1].lower()
        new_filename = f"{sha1_hash}{ext}"
        new_path = os.path.join(PHOTOS_DIR, new_filename)

        if old_path != new_path:
            if os.path.exists(new_path):
                # Target already exists (e.g. duplicate content), remove duplicate
                os.remove(old_path)
            else:
                os.rename(old_path, new_path)

        renamed_entries.append(new_filename)

    unique_filenames = sorted(list(set(renamed_entries)))

    # 3. Read dimensions
    local_photos = []
    under_1080 = []
    for filename in unique_filenames:
        filepath = os.path.join(PHOTOS_DIR, filename)
        dims = get_image_dimensions(filepath)
        if not dims:
            print(f"Warning: Could not read dimensions for {filename}", file=sys.stderr)
            continue
        w, h = dims
        local_photos.append({"f": filename, "w": w, "h": h})
        if max(w, h) < 1080:
            under_1080.append((filename, w, h))

    # 4. Optional Takeout matching
    takeout_data = {}
    if takeout_dir:
        takeout_data = load_takeout_metadata(takeout_dir, local_photos)

    # 5. Extract date and geo per photo
    date_counts = {"Takeout": 0, "EXIF DateTimeOriginal": 0, "EXIF DateTime": 0, "None": 0}
    geo_counts = {"Takeout": 0, "EXIF GPS": 0, "None": 0}

    manifest = []
    for p in local_photos:
        filename = p["f"]
        filepath = os.path.join(PHOTOS_DIR, filename)

        # Parse local EXIF
        exif_date, exif_date_src, exif_geo, exif_geo_src = parse_exif_metadata(filepath)

        # Check Takeout
        t_info = takeout_data.get(filename, {})
        t_date = t_info.get("date")
        t_geo = t_info.get("geo")

        # Determine date: prefer local EXIF (contains local time), or Takeout timestamp
        final_date = None
        if exif_date:
            final_date = exif_date
            date_counts[exif_date_src] += 1
        elif t_date:
            final_date = t_date
            date_counts["Takeout"] += 1
        else:
            date_counts["None"] += 1

        # Determine geo: Takeout or EXIF
        final_geo = None
        if t_geo:
            final_geo = t_geo
            geo_counts["Takeout"] += 1
        elif exif_geo:
            final_geo = exif_geo
            geo_counts[exif_geo_src] += 1
        else:
            geo_counts["None"] += 1

        entry = {
            "f": filename,
            "w": p["w"],
            "h": p["h"],
            "date": final_date,
            "geo": final_geo,
        }
        manifest.append(entry)

    # Sort manifest deterministically by filename
    manifest.sort(key=lambda item: item["f"])

    # 6. Write photos.json
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"\nWrote {len(manifest)} photos to {MANIFEST_PATH}.")

    # 7. Print date and location summary
    total = len(manifest)
    total_with_date = total - date_counts["None"]
    total_with_geo = total - geo_counts["None"]

    print("\nDate & Location Summary:")
    print(f"  Total photos: {total}")
    print(f"  With date: {total_with_date} / {total}")
    for src, count in date_counts.items():
        if src != "None":
            print(f"    - {src}: {count}")
    print(f"    - No date: {date_counts['None']}")

    print(f"  With geo: {total_with_geo} / {total}")
    for src, count in geo_counts.items():
        if src != "None":
            print(f"    - {src}: {count}")
    print(f"    - No geo: {geo_counts['None']}")

    # 8. Report low-resolution images
    print(f"\n{len(under_1080)} images have longest edge under 1080px (originals can be swapped):")
    for fname, w, h in sorted(under_1080, key=lambda x: max(x[1], x[2])):
        print(f"  {fname}: {w}×{h} (longest: {max(w, h)}px)")


def main():
    parser = argparse.ArgumentParser(description="Sync photos for the display kiosk.")
    parser.add_argument("--takeout", type=str, default=None, help="Path to Google Takeout album directory")
    args = parser.parse_args()

    sync_photos(takeout_dir=args.takeout)


if __name__ == "__main__":
    main()
