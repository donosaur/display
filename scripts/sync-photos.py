#!/usr/bin/env python3
"""Sync photos for the display kiosk.

Renames every image in photos/ to a content hash (first 10 hex characters of sha1 + extension),
reads dimensions honoring EXIF orientation, extracts capture date and geo location
(supporting Google Takeout exports and EXIF metadata fallback), tags photos visually
using the Claude API with a fixed vocabulary, computes local image features (luma,
saturation, warmth, palette, phash), precomputes pair compatibility scores,
writes compact photos/photos.json and formatted photos.debug.json, and reports images
whose longest edge is under 1080px.
"""

import argparse
import base64
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
import random
import re
import struct
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from PIL import Image, ImageOps
import numpy as np


def load_env_file():
    """Load key-value pairs from a repo-local .env file into os.environ if not already set."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            print(f"Warning: could not read .env file: {e}")


load_env_file()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_DIR = os.path.join(BASE_DIR, "site")
SITE_PHOTOS_DIR = os.path.join(SITE_DIR, "photos")
ROOT_PHOTOS_DIR = os.path.join(BASE_DIR, "photos")

PHOTOS_DIR = SITE_PHOTOS_DIR if os.path.isdir(SITE_PHOTOS_DIR) else ROOT_PHOTOS_DIR
MANIFEST_PATH = os.path.join(PHOTOS_DIR, "photos.json")
DEBUG_MANIFEST_PATH = os.path.join(ROOT_PHOTOS_DIR, "photos.debug.json")
TAGS_CACHE_PATH = os.path.join(ROOT_PHOTOS_DIR, ".tags-cache.json")
OVERRIDES_PATH = os.path.join(ROOT_PHOTOS_DIR, "overrides.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Pair scoring weights (constants for tuning)
WEIGHT_SAME_DAY = 0.40
WEIGHT_WITHIN_3_DAYS = 0.25
WEIGHT_SAME_MONTH_YEAR = 0.10
WEIGHT_GEO_2KM = 0.15
WEIGHT_SAME_EVENT = 0.10
WEIGHT_SAME_SCENE = 0.10
WEIGHT_SUBJECTS_JACCARD = 0.15
WEIGHT_COLOR_COMPAT = 0.15

# Penalty thresholds
HARD_PENALTY_PHASH_DIST = 10
SOFT_PENALTY_LUMA_DIFF = 0.45
SOFT_PENALTY_FACTOR = 0.50
MIN_PARTNER_SCORE = 0.20
MAX_PARTNERS = 10

# Fixed vocabulary for visual tagging
SCENES = [
    "indoor-home", "indoor-venue", "restaurant", "street-city", "park-garden",
    "beach-water", "mountains-nature", "travel-landmark", "stage-performance",
    "event-party", "vehicle-transit", "other"
]
SUBJECTS = [
    "people-group", "person-solo", "couple", "child", "baby", "cat", "dog",
    "food-drink", "architecture", "landscape", "art-object", "text-screenshot"
]
PEOPLE_COUNTS = ["0", "1", "2", "3-5", "6+"]
MOODS = ["calm", "joyful", "energetic", "formal", "moody"]
TIMES_OF_DAY = ["day", "golden-hour", "night", "indoor-unknown"]
EVENT_HINTS = [
    "birthday", "wedding", "holiday", "concert-performance", "trip", "everyday", "meal", "other"
]


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


def is_video_file(path_or_ext):
    if not path_or_ext:
        return False
    ext = os.path.splitext(path_or_ext)[1].lower() if "." in path_or_ext else path_or_ext.lower()
    return ext in VIDEO_EXTENSIONS


def get_video_metadata(path):
    """Extract width, height, duration (seconds), and creation_time using ffprobe."""
    ffprobe_bin = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not os.path.isfile(ffprobe_bin):
        return None
    try:
        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:stream_tags=rotate,creation_time:stream_side_data=rotation:format=duration:format_tags=creation_time",
            "-of", "json",
            path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None
        s = streams[0]
        w = s.get("width")
        h = s.get("height")
        if not w or not h:
            return None
        # Check rotation
        rotation = 0
        side_data = s.get("side_data_list", [])
        for sd in side_data:
            if "rotation" in sd:
                try:
                    rotation = int(sd["rotation"])
                except Exception:
                    pass
        if not rotation and "tags" in s and "rotate" in s["tags"]:
            try:
                rotation = int(s["tags"]["rotate"])
            except Exception:
                pass
        if abs(rotation) in (90, 270):
            w, h = h, w

        duration = None
        if "duration" in s and s["duration"] is not None:
            try:
                duration = float(s["duration"])
            except ValueError:
                pass
        if duration is None and "format" in data and "duration" in data["format"]:
            try:
                duration = float(data["format"]["duration"])
            except ValueError:
                pass

        creation_time = None
        fmt_tags = data.get("format", {}).get("tags", {})
        stream_tags = s.get("tags", {})
        raw_ct = fmt_tags.get("creation_time") or stream_tags.get("creation_time")
        if raw_ct:
            creation_time = raw_ct.strip().replace("Z", "")
            if len(creation_time) >= 19:
                creation_time = creation_time[:19]

        return w, h, (round(duration, 2) if duration else None), creation_time
    except Exception as e:
        print(f"Warning: ffprobe failed on {path}: {e}", file=sys.stderr)
        return None


def extract_video_frame(path, timestamp=1.0):
    """Extract a representative video frame as a PIL Image."""
    ffmpeg_bin = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not os.path.isfile(ffmpeg_bin):
        return None
    for ts in [str(timestamp), "0.5", "0.0"]:
        try:
            cmd = [
                ffmpeg_bin,
                "-ss", ts,
                "-i", path,
                "-vframes", "1",
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "-",
            ]
            res = subprocess.run(cmd, capture_output=True, check=True)
            if res.stdout:
                return Image.open(io.BytesIO(res.stdout)).convert("RGB")
        except Exception:
            continue
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


def compute_phash(target):
    """Compute 64-bit DCT perceptual hash as 16-character hex string."""
    try:
        if isinstance(target, Image.Image):
            img = target
        elif is_video_file(target):
            img = extract_video_frame(target)
            if img is None:
                return None
        else:
            with Image.open(target) as im:
                img = ImageOps.exif_transpose(im)
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


def compute_local_features(target):
    """Compute local image features (no API, Pillow + numpy):
    
    - luma: mean brightness (0-1)
    - sat: mean saturation (0-1)
    - warmth: mean of (R - B) / 255 (-1..1)
    - palette: 3 dominant colors via k-means on 64px thumbnail as hex
    - phash: 64-bit perceptual hash (16-char hex)
    """
    phash = compute_phash(target)

    if isinstance(target, Image.Image):
        img = target
    elif is_video_file(target):
        img = extract_video_frame(target)
        if img is None:
            return {"luma": 0.5, "sat": 0.0, "warmth": 0.0, "palette": ["#000000", "#555555", "#aaaaaa"], "phash": phash}
    else:
        with Image.open(target) as im:
            img = ImageOps.exif_transpose(im)

    if img.mode != "RGB":
        img = img.convert("RGB")

    # 64x64 thumbnail for fast feature calculation
    thumb = img.resize((64, 64), Image.Resampling.BILINEAR)
    pixels = np.asarray(thumb, dtype=np.float64)

    r = pixels[:, :, 0]
    g = pixels[:, :, 1]
    b = pixels[:, :, 2]

    # luma: mean brightness (0-1)
    luma = float(np.mean(0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0)

    # sat: mean saturation (0-1)
    cmax = np.max(pixels, axis=-1)
    cmin = np.min(pixels, axis=-1)
    delta = cmax - cmin
    sat_arr = np.zeros_like(cmax)
    mask = cmax > 0
    sat_arr[mask] = delta[mask] / cmax[mask]
    sat = float(np.mean(sat_arr))

    # warmth: mean of R minus B normalized to -1..1
    warmth = float(np.mean(r - b) / 255.0)

    # palette: 3 dominant colors via small k-means
    flat_pixels = pixels.reshape(-1, 3)
    lum_flat = 0.2126 * flat_pixels[:, 0] + 0.7152 * flat_pixels[:, 1] + 0.0722 * flat_pixels[:, 2]
    sorted_idx = np.argsort(lum_flat)
    centers = flat_pixels[sorted_idx[[len(sorted_idx) // 6, len(sorted_idx) // 2, 5 * len(sorted_idx) // 6]]].copy()

    labels = np.zeros(len(flat_pixels), dtype=int)
    for _ in range(12):
        dists = np.sum((flat_pixels[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        labels = np.argmin(dists, axis=1)
        new_centers = np.empty_like(centers)
        for k in range(3):
            cluster_pts = flat_pixels[labels == k]
            if len(cluster_pts) > 0:
                new_centers[k] = np.mean(cluster_pts, axis=0)
            else:
                new_centers[k] = centers[k]
        if np.allclose(centers, new_centers, atol=0.5):
            break
        centers = new_centers

    counts = [np.sum(labels == k) for k in range(3)]
    sorted_order = np.argsort(counts)[::-1]
    palette = [
        f"#{int(np.clip(round(centers[k][0]), 0, 255)):02x}{int(np.clip(round(centers[k][1]), 0, 255)):02x}{int(np.clip(round(centers[k][2]), 0, 255)):02x}"
        for k in sorted_order
    ]

    return {
        "luma": round(luma, 3),
        "sat": round(sat, 3),
        "warmth": round(warmth, 3),
        "palette": palette,
        "phash": phash,
    }


def parse_exif_metadata(path):
    """Extract capture date and GPS coordinates from EXIF metadata."""
    if is_video_file(path):
        vmeta = get_video_metadata(path)
        if vmeta and vmeta[3]:
            return vmeta[3], "Video Metadata", None, None
        return None, None, None, None

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
    """Find the sidecar JSON file for a given image in Takeout export."""
    dirname = os.path.dirname(img_path)
    basename = os.path.basename(img_path)
    stem, ext = os.path.splitext(basename)

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

    if basename in json_title_map:
        return json_title_map[basename]

    json_files = dir_json_map.get(dirname, [])
    for jf in json_files:
        jbase = os.path.basename(jf)
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

    for root, _, files in os.walk(takeout_dir):
        jsons_in_dir = []
        for f in files:
            full_path = os.path.join(root, f)
            ext = os.path.splitext(f)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                takeout_images.append(full_path)
            elif ext == ".json" and not f.startswith("metadata.json"):
                jsons_in_dir.append(full_path)
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

    takeout_matches = {}
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
                    if best_match and best_dist <= 8:
                        matched_file = best_match

            if not matched_file or matched_file in matched_local_files:
                continue

            sidecar_path = find_takeout_sidecar(t_img, dir_json_map, json_title_map)
            if not sidecar_path:
                continue

            with open(sidecar_path, "r", encoding="utf-8", errors="ignore") as jf:
                sdata = json.load(jf)

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


# ==================== VISUAL TAGGING ====================

def prepare_image_for_api(image_path):
    """Downscale photo or video frame to max 768px JPEG and return base64 encoded string."""
    if is_video_file(image_path):
        img = extract_video_frame(image_path)
    else:
        with Image.open(image_path) as im:
            img = ImageOps.exif_transpose(im)
            if img.mode != "RGB":
                img = img.convert("RGB")
    if img is None:
        return None
    w, h = img.size
    max_dim = max(w, h)
    if max_dim > 768:
        scale = 768.0 / max_dim
        img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def validate_and_clean_tags(raw):
    """Validate raw LLM JSON response against fixed vocabulary and clean fields."""
    if not isinstance(raw, dict):
        return None

    scene = raw.get("scene")
    if scene not in SCENES:
        scene = "other"

    raw_subjects = raw.get("subjects") or []
    if isinstance(raw_subjects, str):
        raw_subjects = [raw_subjects]
    subjects = [s for s in raw_subjects if s in SUBJECTS]

    pc = str(raw.get("people_count", "0"))
    if pc not in PEOPLE_COUNTS:
        if pc in ("3", "4", "5"):
            pc = "3-5"
        elif pc.isdigit() and int(pc) >= 6:
            pc = "6+"
        else:
            pc = "0"

    mood = raw.get("mood")
    if mood not in MOODS:
        mood = "calm"

    tod = raw.get("time_of_day")
    if tod not in TIMES_OF_DAY:
        tod = "indoor-unknown"

    is_bw = bool(raw.get("is_bw", False))

    event_hint = raw.get("event_hint")
    if event_hint not in EVENT_HINTS:
        event_hint = "everyday"

    caption_raw = str(raw.get("caption") or "")
    caption_words = caption_raw.strip().split()
    caption = " ".join(caption_words[:12]) if caption_words else None

    return {
        "scene": scene,
        "subjects": subjects,
        "people_count": pc,
        "mood": mood,
        "time_of_day": tod,
        "is_bw": is_bw,
        "event_hint": event_hint,
        "caption": caption,
    }


def tag_image_with_claude(image_path, api_key, model):
    """Call Claude API to tag an image with fixed vocabulary and retry on 429/5xx."""
    b64_img = prepare_image_for_api(image_path)
    prompt_text = (
        "Analyze this photo and provide visual tags in strict JSON format.\n"
        "You must choose field values ONLY from these allowed lists:\n"
        f'- "scene": one of {json.dumps(SCENES)}\n'
        f'- "subjects": array containing any of {json.dumps(SUBJECTS)}\n'
        f'- "people_count": one of {json.dumps(PEOPLE_COUNTS)}\n'
        f'- "mood": one of {json.dumps(MOODS)}\n'
        f'- "time_of_day": one of {json.dumps(TIMES_OF_DAY)}\n'
        f'- "is_bw": boolean (true if black & white or monochrome, false otherwise)\n'
        f'- "event_hint": one of {json.dumps(EVENT_HINTS)}\n'
        '- "caption": short descriptive caption (maximum 12 words)\n\n'
        "Respond with ONLY valid JSON adhering to this exact schema, with no markdown code blocks or preamble:\n"
        "{\n"
        '  "scene": "...",\n'
        '  "subjects": [...],\n'
        '  "people_count": "...",\n'
        '  "mood": "...",\n'
        '  "time_of_day": "...",\n'
        '  "is_bw": false,\n'
        '  "event_hint": "...",\n'
        '  "caption": "..."\n'
        "}"
    )

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 300,
        "temperature": 0.0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64_img,
                        },
                    },
                    {"type": "text", "text": prompt_text},
                ],
            }
        ],
    }

    retries = 5
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode("utf-8")
                data = json.loads(resp_body)
                text = ""
                for item in data.get("content", []):
                    if item.get("type") == "text":
                        text += item.get("text", "")
                text = text.strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?\s*", "", text)
                    text = re.sub(r"\s*```$", "", text)
                parsed = json.loads(text.strip())
                return validate_and_clean_tags(parsed)
        except urllib.error.HTTPError as e:
            status_code = e.code
            if status_code == 429 or status_code >= 500:
                retry_after = e.headers.get("retry-after")
                wait_time = float(retry_after) if retry_after else (2 ** attempt + random.uniform(0.1, 0.5))
                time.sleep(wait_time)
                continue
            else:
                error_body = e.read().decode("utf-8", errors="ignore")
                print(f"API Error {status_code} on {image_path}: {error_body}", file=sys.stderr)
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.uniform(0.1, 0.5))
                continue
            print(f"Failed to tag {image_path}: {e}", file=sys.stderr)
            return None

    return None


def run_visual_tagging(unique_filenames, retag=False):
    """Run visual tagging for photos, caching in photos/.tags-cache.json."""
    cache = {}
    if os.path.isfile(TAGS_CACHE_PATH) and not retag:
        try:
            with open(TAGS_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = (
        os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("CLAUDE_MODEL")
        or "claude-3-5-haiku-20241022"
    )

    needed = []
    for fn in unique_filenames:
        chash = os.path.splitext(fn)[0]
        if retag or chash not in cache:
            needed.append(fn)

    if not needed:
        print(f"All {len(unique_filenames)} photos already tagged in cache.")
        return cache

    if not api_key:
        if cache:
            print(
                f"Warning: ANTHROPIC_API_KEY not set. Using {len(cache)} cached tags; "
                f"{len(needed)} photos remain untagged."
            )
        else:
            print(
                "Warning: ANTHROPIC_API_KEY not set. Skipping visual tagging "
                "(pairing will use date, color, and duplicates)."
            )
        return cache

    print(f"Tagging {len(needed)} photos with Claude API ({model}, concurrency 4)...")
    completed = 0

    def _worker(fn):
        chash = os.path.splitext(fn)[0]
        fpath = os.path.join(PHOTOS_DIR, fn)
        tags = tag_image_with_claude(fpath, api_key, model)
        return chash, tags

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_worker, fn): fn for fn in needed}
        for future in concurrent.futures.as_completed(futures):
            fn = futures[future]
            try:
                chash, tags = future.result()
                if tags:
                    cache[chash] = tags
                    completed += 1
                    if completed % 10 == 0 or completed == len(needed):
                        print(f"  Tagged {completed}/{len(needed)} photos...")
            except Exception as e:
                print(f"  Error tagging {fn}: {e}", file=sys.stderr)

    tmp_tags_cache = TAGS_CACHE_PATH + ".tmp"
    with open(tmp_tags_cache, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_tags_cache, TAGS_CACHE_PATH)
    print(f"Saved {len(cache)} tagged photos to {TAGS_CACHE_PATH}.")

    return cache


# ==================== PAIR SCORING ====================

def parse_iso_date(dt_str):
    """Parse ISO 8601 date string to datetime object."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    dt_clean = dt_str.replace("Z", "").split(".")[0]
    try:
        return datetime.fromisoformat(dt_clean)
    except Exception:
        pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", dt_str)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    """Calculate great-circle distance in kilometers between two GPS coordinates."""
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return r * c


def hex_to_lab(hex_str):
    """Convert hex color (#rrggbb) to CIE-Lab coordinates."""
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def _srgb_to_linear(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    rl = _srgb_to_linear(r)
    gl = _srgb_to_linear(g)
    bl = _srgb_to_linear(b)

    # D65 CIE XYZ
    x = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375
    y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750
    z = rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041

    xn, yn, zn = 0.95047, 1.00000, 1.08883
    xr = x / xn
    yr = y / yn
    zr = z / zn

    def _f(t):
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t + 16.0 / 116.0)

    fx = _f(xr)
    fy = _f(yr)
    fz = _f(zr)

    l_val = 116.0 * fy - 16.0
    a_val = 500.0 * (fx - fy)
    b_val = 200.0 * (fy - fz)
    return l_val, a_val, b_val


def compute_palette_lab_dist(lab_pal1, lab_pal2):
    """Compute average mutual minimum Euclidean distance between two 3-color Lab palettes."""
    d1 = [min(math.sqrt((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2) for c2 in lab_pal2) for c1 in lab_pal1]
    d2 = [min(math.sqrt((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2) for c1 in lab_pal1) for c2 in lab_pal2]
    return (sum(d1) + sum(d2)) / (len(d1) + len(d2))


def load_overrides():
    """Load manual overrides from photos/overrides.json if present."""
    if not os.path.isfile(OVERRIDES_PATH):
        return {"tags": {}, "never_pair": set(), "always_pair": set(), "dates": {}}

    try:
        with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        tags_overrides = data.get("tags") or {}
        dates_overrides = data.get("dates") or {}

        def _norm_pair(pair):
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                return None
            return (min(str(pair[0]), str(pair[1])), max(str(pair[0]), str(pair[1])))

        never_pair = set()
        for p in data.get("never_pair") or []:
            np = _norm_pair(p)
            if np:
                never_pair.add(np)

        always_pair = set()
        for p in data.get("always_pair") or []:
            ap = _norm_pair(p)
            if ap:
                always_pair.add(ap)

        print(
            f"Loaded manual overrides from {OVERRIDES_PATH}: "
            f"{len(tags_overrides)} tag overrides, {len(dates_overrides)} date overrides, {len(never_pair)} never_pair, {len(always_pair)} always_pair."
        )
        return {"tags": tags_overrides, "never_pair": never_pair, "always_pair": always_pair, "dates": dates_overrides}
    except Exception as e:
        print(f"Warning: Failed to load overrides from {OVERRIDES_PATH}: {e}", file=sys.stderr)
        return {"tags": {}, "never_pair": set(), "always_pair": set(), "dates": {}}


def score_pair(p1, p2, d1, d2, lab_pal1, lab_pal2, never_pair=None, always_pair=None):
    """Compute pair score in 0..1 between two photos based on weighted criteria, penalties, and overrides."""
    never_pair = never_pair or set()
    always_pair = always_pair or set()

    p1_f, p2_f = p1["f"], p2["f"]
    p1_stem = os.path.splitext(p1_f)[0]
    p2_stem = os.path.splitext(p2_f)[0]

    pair_key = (min(p1_f, p2_f), max(p1_f, p2_f))
    pair_key_stem = (min(p1_stem, p2_stem), max(p1_stem, p2_stem))

    # Manual overrides take absolute precedence
    if pair_key in never_pair or pair_key_stem in never_pair:
        return 0.0

    if pair_key in always_pair or pair_key_stem in always_pair:
        return 1.0

    # Hard penalty 1: phash Hamming distance <= 10 (near-duplicate or burst shot)
    phash_dist = hamming_distance(p1["phash"], p2["phash"])
    if phash_dist <= HARD_PENALTY_PHASH_DIST:
        return 0.0

    t1 = p1.get("tags")
    t2 = p2.get("tags")

    # Hard penalty 2: one is_bw and the other color
    if t1 and t2:
        if t1.get("is_bw") != t2.get("is_bw"):
            return 0.0
    else:
        # Fallback to saturation if tags are absent
        is_bw1 = t1.get("is_bw") if t1 else (p1["sat"] < 0.05)
        is_bw2 = t2.get("is_bw") if t2 else (p2["sat"] < 0.05)
        if is_bw1 != is_bw2:
            return 0.0

    # Hard penalty 3: either has subject text-screenshot
    if t1 and "text-screenshot" in (t1.get("subjects") or []):
        return 0.0
    if t2 and "text-screenshot" in (t2.get("subjects") or []):
        return 0.0

    score = 0.0

    # 1. Date scoring
    if d1 and d2:
        if d1.year == d2.year and d1.month == d2.month and d1.day == d2.day:
            score += WEIGHT_SAME_DAY
        elif abs((d1.date() - d2.date()).days) <= 3:
            score += WEIGHT_WITHIN_3_DAYS
        elif d1.year == d2.year and d1.month == d2.month:
            score += WEIGHT_SAME_MONTH_YEAR

    # 2. Geo scoring (within 2 km)
    g1 = p1.get("geo")
    g2 = p2.get("geo")
    if g1 and g2 and len(g1) == 2 and len(g2) == 2:
        dist_km = haversine_km(g1[0], g1[1], g2[0], g2[1])
        if dist_km <= 2.0:
            score += WEIGHT_GEO_2KM

    # 3. Tags scoring
    if t1 and t2:
        # Same event_hint (excluding everyday/other)
        e1 = t1.get("event_hint")
        e2 = t2.get("event_hint")
        if e1 and e2 and e1 == e2 and e1 not in ("everyday", "other"):
            score += WEIGHT_SAME_EVENT

        # Same scene
        s1 = t1.get("scene")
        s2 = t2.get("scene")
        if s1 and s2 and s1 == s2 and s1 != "other":
            score += WEIGHT_SAME_SCENE

        # Subjects Jaccard similarity * 0.15
        subj1 = set(t1.get("subjects") or [])
        subj2 = set(t2.get("subjects") or [])
        union = subj1 | subj2
        if union:
            jaccard = len(subj1 & subj2) / len(union)
            score += jaccard * WEIGHT_SUBJECTS_JACCARD

    # 4. Color compatibility
    luma_diff = abs(p1["luma"] - p2["luma"])
    sat_diff = abs(p1["sat"] - p2["sat"])
    warmth_diff = abs(p1["warmth"] - p2["warmth"]) / 2.0  # warmth in [-1, 1], normalized diff in [0, 1]
    lab_dist = compute_palette_lab_dist(lab_pal1, lab_pal2)
    pal_dist_norm = min(1.0, lab_dist / 100.0)

    color_blend = 0.25 * luma_diff + 0.25 * sat_diff + 0.25 * warmth_diff + 0.25 * pal_dist_norm
    color_compat = max(0.0, 1.0 - color_blend)
    score += color_compat * WEIGHT_COLOR_COMPAT

    # Soft penalty: |luma diff| > 0.45 -> multiply by 0.5 (very dark next to very bright)
    if luma_diff > SOFT_PENALTY_LUMA_DIFF:
        score *= SOFT_PENALTY_FACTOR

    return max(0.0, min(1.0, score))


def score_color_alone(p1, p2, lab_pal1, lab_pal2, never_pair=None):
    """Compute color compatibility score in 0..1 alone between two photos, with hard penalties applied and no threshold."""
    never_pair = never_pair or set()

    p1_f, p2_f = p1["f"], p2["f"]
    p1_stem = os.path.splitext(p1_f)[0]
    p2_stem = os.path.splitext(p2_f)[0]

    pair_key = (min(p1_f, p2_f), max(p1_f, p2_f))
    pair_key_stem = (min(p1_stem, p2_stem), max(p1_stem, p2_stem))

    if pair_key in never_pair or pair_key_stem in never_pair:
        return 0.0

    # Hard penalty 1: phash Hamming distance <= 10
    phash_dist = hamming_distance(p1["phash"], p2["phash"])
    if phash_dist <= HARD_PENALTY_PHASH_DIST:
        return 0.0

    t1 = p1.get("tags")
    t2 = p2.get("tags")

    # Hard penalty 2: one is_bw and the other color
    if t1 and t2:
        if t1.get("is_bw") != t2.get("is_bw"):
            return 0.0
    else:
        is_bw1 = t1.get("is_bw") if t1 else (p1["sat"] < 0.05)
        is_bw2 = t2.get("is_bw") if t2 else (p2["sat"] < 0.05)
        if is_bw1 != is_bw2:
            return 0.0

    # Hard penalty 3: either has subject text-screenshot
    if t1 and "text-screenshot" in (t1.get("subjects") or []):
        return 0.0
    if t2 and "text-screenshot" in (t2.get("subjects") or []):
        return 0.0

    # Color compatibility formula
    luma_diff = abs(p1["luma"] - p2["luma"])
    sat_diff = abs(p1["sat"] - p2["sat"])
    warmth_diff = abs(p1["warmth"] - p2["warmth"]) / 2.0
    lab_dist = compute_palette_lab_dist(lab_pal1, lab_pal2)
    pal_dist_norm = min(1.0, lab_dist / 100.0)

    color_blend = 0.25 * luma_diff + 0.25 * sat_diff + 0.25 * warmth_diff + 0.25 * pal_dist_norm
    color_compat = max(0.0, 1.0 - color_blend)

    if luma_diff > SOFT_PENALTY_LUMA_DIFF:
        color_compat *= SOFT_PENALTY_FACTOR

    return max(0.0, min(1.0, color_compat))


def compute_all_pair_scores(manifest, overrides=None):
    """Compute pairwise scores for all photos, apply overrides, attach top 10 partners and top 5 color alts."""
    overrides = overrides or {}
    never_pair = overrides.get("never_pair", set())
    always_pair = overrides.get("always_pair", set())

    n = len(manifest)
    print(f"Scoring all {n * (n - 1) // 2} photo pairs...")

    # Pre-parse dates and Lab palettes
    parsed_dates = [parse_iso_date(p.get("date")) for p in manifest]
    lab_palettes = [[hex_to_lab(h) for h in p.get("palette", [])] for p in manifest]

    # Matrix of partner and alt scores
    partners_by_photo = [[] for _ in range(n)]
    alt_by_photo = [[] for _ in range(n)]

    for i in range(n):
        p1 = manifest[i]
        d1 = parsed_dates[i]
        lab1 = lab_palettes[i]

        for j in range(i + 1, n):
            p2 = manifest[j]
            d2 = parsed_dates[j]
            lab2 = lab_palettes[j]

            s = score_pair(p1, p2, d1, d2, lab1, lab2, never_pair=never_pair, always_pair=always_pair)
            if s >= MIN_PARTNER_SCORE:
                partners_by_photo[i].append((p2["f"], round(s, 3)))
                partners_by_photo[j].append((p1["f"], round(s, 3)))

            c = score_color_alone(p1, p2, lab1, lab2, never_pair=never_pair)
            if c > 0:
                alt_by_photo[i].append((p2["f"], round(c, 2)))
                alt_by_photo[j].append((p1["f"], round(c, 2)))

    # Sort each photo's partners descending by score and truncate to top MAX_PARTNERS
    # and sort each photo's alt descending by score and truncate to top 5
    for i in range(n):
        sorted_partners = sorted(partners_by_photo[i], key=lambda x: x[1], reverse=True)
        manifest[i]["partners"] = [[fn, sc] for fn, sc in sorted_partners[:MAX_PARTNERS]]

        sorted_alt = sorted(alt_by_photo[i], key=lambda x: x[1], reverse=True)
        manifest[i]["alt"] = [[fn, sc] for fn, sc in sorted_alt[:5]]

    # Ensure always_pair entries are set to 1.0 at the very top of both partner lists
    fn_to_idx = {p["f"]: idx for idx, p in enumerate(manifest)}
    stem_to_idx = {os.path.splitext(p["f"])[0]: idx for idx, p in enumerate(manifest)}

    for f1, f2 in always_pair:
        idx1 = fn_to_idx.get(f1) if f1 in fn_to_idx else stem_to_idx.get(f1)
        idx2 = fn_to_idx.get(f2) if f2 in fn_to_idx else stem_to_idx.get(f2)
        if idx1 is not None and idx2 is not None:
            p1_fn = manifest[idx1]["f"]
            p2_fn = manifest[idx2]["f"]

            # Place p2 at top of p1
            cur1 = [item for item in manifest[idx1]["partners"] if item[0] != p2_fn]
            manifest[idx1]["partners"] = [[p2_fn, 1.0]] + cur1[:MAX_PARTNERS - 1]

            # Place p1 at top of p2
            cur2 = [item for item in manifest[idx2]["partners"] if item[0] != p1_fn]
            manifest[idx2]["partners"] = [[p1_fn, 1.0]] + cur2[:MAX_PARTNERS - 1]

    # Stats
    has_partners = sum(1 for p in manifest if len(p.get("partners", [])) > 0)
    has_alts = sum(1 for p in manifest if len(p.get("alt", [])) > 0)
    total_partner_links = sum(len(p.get("partners", [])) for p in manifest)
    print(
        f"Partner scoring complete: {has_partners}/{n} photos have >= 1 partner (avg {total_partner_links / max(1, n):.1f}); "
        f"{has_alts}/{n} photos have >= 1 alt color partner."
    )


def sync_photos(takeout_dir=None, retag=False):
    if not os.path.isdir(PHOTOS_DIR):
        print(f"Error: Photos directory not found at {PHOTOS_DIR}", file=sys.stderr)
        sys.exit(1)

    # 1. Scan photos directory
    all_files = [f for f in os.listdir(PHOTOS_DIR) if not f.startswith(".")]
    media_files = [
        f for f in all_files
        if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS and not f.startswith("photos.")
    ]

    print(f"Found {len(media_files)} media files in {PHOTOS_DIR}...")

    # 2. Hash and rename
    renamed_entries = []
    for filename in sorted(media_files):
        old_path = os.path.join(PHOTOS_DIR, filename)
        with open(old_path, "rb") as f:
            content = f.read()
        sha1_hash = hashlib.sha1(content).hexdigest()[:10]
        ext = os.path.splitext(filename)[1].lower()
        new_filename = f"{sha1_hash}{ext}"
        new_path = os.path.join(PHOTOS_DIR, new_filename)

        if old_path != new_path:
            if os.path.exists(new_path):
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
        if is_video_file(filename):
            vmeta = get_video_metadata(filepath)
            if not vmeta:
                print(f"Warning: Could not read video metadata for {filename}", file=sys.stderr)
                continue
            w, h, duration, _ = vmeta
            local_photos.append({"f": filename, "w": w, "h": h, "duration": duration, "type": "video"})
        else:
            dims = get_image_dimensions(filepath)
            if not dims:
                print(f"Warning: Could not read dimensions for {filename}", file=sys.stderr)
                continue
            w, h = dims
            local_photos.append({"f": filename, "w": w, "h": h, "type": "image"})
        if max(w, h) < 1080:
            under_1080.append((filename, w, h))

    # 4. Optional Takeout matching
    takeout_data = {}
    if takeout_dir:
        takeout_data = load_takeout_metadata(takeout_dir, local_photos)

    # 5. Visual tagging
    tags_cache = run_visual_tagging(unique_filenames, retag=retag)

    # 6. Manual overrides
    overrides = load_overrides()

    # 7. Extract date, geo, tags, and local image features
    date_counts = {"Takeout": 0, "EXIF DateTimeOriginal": 0, "EXIF DateTime": 0, "None": 0}
    geo_counts = {"Takeout": 0, "EXIF GPS": 0, "None": 0}

    print(f"Computing local image features (luma, sat, warmth, palette, phash) for {len(local_photos)} photos...")
    manifest = []
    for p in local_photos:
        filename = p["f"]
        filepath = os.path.join(PHOTOS_DIR, filename)
        chash = os.path.splitext(filename)[0]

        # Parse local EXIF
        exif_date, exif_date_src, exif_geo, exif_geo_src = parse_exif_metadata(filepath)

        # Check Takeout
        t_info = takeout_data.get(filename, {})
        t_date = t_info.get("date")
        t_geo = t_info.get("geo")

        # Check manual date overrides
        ov_date = None
        if "dates" in overrides and isinstance(overrides["dates"], dict):
            ov_date = overrides["dates"].get(filename) or overrides["dates"].get(chash)

        final_date = None
        if ov_date:
            final_date = ov_date
            date_counts["Override"] = date_counts.get("Override", 0) + 1
        elif exif_date:
            final_date = exif_date
            date_counts[exif_date_src] += 1
        elif t_date:
            final_date = t_date
            date_counts["Takeout"] += 1
        else:
            date_counts["None"] += 1

        final_geo = None
        if t_geo:
            final_geo = t_geo
            geo_counts["Takeout"] += 1
        elif exif_geo:
            final_geo = exif_geo
            geo_counts[exif_geo_src] += 1
        else:
            geo_counts["None"] += 1

        tag_entry = tags_cache.get(chash)
        tags_obj = None
        caption_val = None
        if tag_entry:
            tags_obj = {k: v for k, v in tag_entry.items() if k != "caption"}
            caption_val = tag_entry.get("caption")

        # Apply manual tag overrides if specified in overrides.json
        fn_stem = os.path.splitext(filename)[0]
        ov_tags = overrides["tags"].get(filename) or overrides["tags"].get(fn_stem)
        if ov_tags and isinstance(ov_tags, dict):
            if tags_obj is None:
                tags_obj = {}
            for ok, ov in ov_tags.items():
                if ok == "caption":
                    caption_val = ov
                else:
                    tags_obj[ok] = ov

        features = compute_local_features(filepath)

        entry = {
            "f": filename,
            "w": p["w"],
            "h": p["h"],
            "date": final_date,
            "geo": final_geo,
            "tags": tags_obj,
            "caption": caption_val,
            "luma": features["luma"],
            "sat": features["sat"],
            "warmth": features["warmth"],
            "palette": features["palette"],
            "phash": features["phash"],
        }
        if p.get("type") == "video":
            entry["type"] = "video"
            if p.get("duration") is not None:
                entry["duration"] = p["duration"]
        manifest.append(entry)

    # Sort manifest deterministically by filename
    manifest.sort(key=lambda item: item["f"])

    # 8. Compute pair compatibility scores
    compute_all_pair_scores(manifest, overrides=overrides)

    # 9. Write compact photos.json (no indent) and formatted photos.debug.json atomically
    tmp_manifest = MANIFEST_PATH + ".tmp"
    with open(tmp_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, separators=(",", ":"))
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_manifest, MANIFEST_PATH)

    tmp_debug = DEBUG_MANIFEST_PATH + ".tmp"
    with open(tmp_debug, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_debug, DEBUG_MANIFEST_PATH)

    print(f"\nWrote {len(manifest)} photos to compact {MANIFEST_PATH} and debug {DEBUG_MANIFEST_PATH}.")

    # 10. Update build version meta tag in index.html
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate_paths = [
        os.path.join(base_dir, "site", "index.html"),
        os.path.join(base_dir, "index.html"),
    ]
    index_html_path = next((p for p in candidate_paths if os.path.isfile(p)), None)
    if index_html_path:
        with open(index_html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        html_for_hash = re.sub(r'<meta\s+name=["\']build["\']\s+content=["\'][^"\']*["\']\s*/?>\n?', '', html_content)
        sw_candidate_paths = [
            os.path.join(os.path.dirname(index_html_path), "sw.js"),
            os.path.join(base_dir, "site", "sw.js"),
            os.path.join(base_dir, "sw.js"),
        ]
        sw_js_path = next((p for p in sw_candidate_paths if p and os.path.isfile(p)), None)
        sw_content = ""
        if sw_js_path:
            with open(sw_js_path, "r", encoding="utf-8") as sf:
                sw_content = sf.read()

        combined = (html_for_hash.strip() + "\n" + sw_content.strip()).encode("utf-8")
        build_hash = hashlib.sha256(combined).hexdigest()[:10]

        if re.search(r'<meta\s+name=["\']build["\']', html_content):
            new_html_content = re.sub(
                r'<meta\s+name=["\']build["\']\s+content=["\'][^"\']*["\']\s*/?>',
                f'<meta name="build" content="{build_hash}">',
                html_content
            )
        else:
            new_html_content = re.sub(
                r'(<meta\s+name=["\']viewport["\'][^>]*>)',
                f'\\1\n<meta name="build" content="{build_hash}">',
                html_content
            )

        if new_html_content != html_content:
            with open(index_html_path, "w", encoding="utf-8") as f:
                f.write(new_html_content)
            print(f"Updated index.html build version to {build_hash}.")
        else:
            print(f"index.html build version unchanged ({build_hash}).")

    # 10. Print date and location summary
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

    # 11. Report low-resolution images
    print(f"\n{len(under_1080)} images have longest edge under 1080px (originals can be swapped):")
    for fname, w, h in sorted(under_1080, key=lambda x: max(x[1], x[2])):
        print(f"  {fname}: {w}×{h} (longest: {max(w, h)}px)")


def main():
    parser = argparse.ArgumentParser(description="Sync photos for the display kiosk.")
    parser.add_argument("--takeout", type=str, default=None, help="Path to Google Takeout album directory")
    parser.add_argument("--retag", action="store_true", help="Force redo of visual tagging with Claude API")
    args = parser.parse_args()

    sync_photos(takeout_dir=args.takeout, retag=args.retag)


if __name__ == "__main__":
    main()
