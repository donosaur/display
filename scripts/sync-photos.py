#!/usr/bin/env python3
"""Sync photos for the display kiosk.

Renames every image in photos/ to a content hash (first 10 hex characters of sha1 + extension),
reads dimensions honoring EXIF orientation, writes photos/photos.json,
and reports images whose longest edge is under 1080px.
"""

import hashlib
import json
import os
import struct
import sys

PHOTOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photos")
MANIFEST_PATH = os.path.join(PHOTOS_DIR, "photos.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def get_dimensions_pillow(path):
    try:
        from PIL import Image, ImageOps
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
    # Try Pillow first if available
    dims = get_dimensions_pillow(path)
    if dims:
        return dims
    # Fallback to pure-Python JPEG parser
    try:
        with open(path, "rb") as f:
            data = f.read()
        dims = get_jpeg_dimensions(data)
        if dims:
            return dims
    except Exception:
        pass
    return None


def sync_photos():
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

    # De-duplicate entries if any identical files were merged
    unique_filenames = sorted(list(set(renamed_entries)))

    # 3. Read dimensions
    manifest = []
    under_1080 = []
    for filename in unique_filenames:
        filepath = os.path.join(PHOTOS_DIR, filename)
        dims = get_image_dimensions(filepath)
        if not dims:
            print(f"Warning: Could not read dimensions for {filename}", file=sys.stderr)
            continue
        w, h = dims
        manifest.append({"f": filename, "w": w, "h": h})
        if max(w, h) < 1080:
            under_1080.append((filename, w, h))

    # Sort manifest deterministically by filename
    manifest.sort(key=lambda item: item["f"])

    # 4. Write photos.json
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(manifest)} photos to {MANIFEST_PATH}.")

    # 5. Report low-resolution images
    print(f"\n{len(under_1080)} images have longest edge under 1080px (originals can be swapped):")
    for fname, w, h in sorted(under_1080, key=lambda x: max(x[1], x[2])):
        print(f"  {fname}: {w}×{h} (longest: {max(w, h)}px)")


if __name__ == "__main__":
    sync_photos()
