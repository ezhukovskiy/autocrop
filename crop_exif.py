#!/usr/bin/env python3
"""Crop photos from album page scans and write EXIF metadata.

Reads autocrop_meta.json and produces cropped, rotated JPEG files
with date, GPS, and caption metadata embedded.

Usage:
    python crop_exif.py ./album/
    python crop_exif.py ./album/ -o ./output/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import xml.sax.saxutils

import piexif
from PIL import Image

METADATA_FILENAME = "autocrop_meta.json"


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

_geocode_cache: dict[str, tuple[float, float] | None] = {}
_geocode_lock = threading.Lock()


def geocode_location(location: str, default_location: str | None = None,
                     country_codes: str | None = None) -> tuple[float, float] | None:
    """Convert location name to (lat, lon) using Nominatim. Thread-safe."""
    cache_key = f"{location}|{default_location or ''}|{country_codes or ''}"
    with _geocode_lock:
        if cache_key in _geocode_cache:
            return _geocode_cache[cache_key]

    queries = [location]
    if default_location and default_location.lower() not in location.lower():
        queries.append(f"{location}, {default_location}")
        queries.append(default_location)

    for query in queries:
        try:
            params = {"q": query, "format": "json", "limit": 1}
            if country_codes:
                params["countrycodes"] = country_codes
            url = f"https://nominatim.openstreetmap.org/search?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": "autocrop-script/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data:
                result = (float(data[0]["lat"]), float(data[0]["lon"]))
                with _geocode_lock:
                    _geocode_cache[cache_key] = result
                if query != location:
                    print(f"    Geocoding: '{location}' not found, using '{query}'")
                time.sleep(1)
                return result
            time.sleep(1)
        except Exception as e:
            print(f"    Warning: geocoding failed for '{query}': {e}")

    with _geocode_lock:
        _geocode_cache[cache_key] = None
    return None


def _decimal_to_dms(decimal: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    d = int(decimal)
    m_float = (decimal - d) * 60
    m = int(m_float)
    s = int(round((m_float - m) * 60 * 1000))
    return ((d, 1), (m, 1), (s, 1000))


# ---------------------------------------------------------------------------
# Crop & rotate
# ---------------------------------------------------------------------------

def crop_and_rotate(image: Image.Image, photo_info: dict) -> Image.Image:
    w, h = image.size
    bbox = photo_info["bbox"]
    x1 = max(0, min(int(bbox[0] / 100 * w), w))
    y1 = max(0, min(int(bbox[1] / 100 * h), h))
    x2 = max(0, min(int(bbox[2] / 100 * w), w))
    y2 = max(0, min(int(bbox[3] / 100 * h), h))
    cropped = image.crop((x1, y1, x2, y2))

    top_side = photo_info.get("top_side", "").lower().strip()
    if top_side:
        if top_side == "bottom":
            cropped = cropped.transpose(Image.ROTATE_180)
        elif top_side == "left":
            cropped = cropped.transpose(Image.ROTATE_270)
        elif top_side == "right":
            cropped = cropped.transpose(Image.ROTATE_90)
    else:
        rotation = photo_info.get("rotation", 0)
        if rotation:
            rot = rotation % 360
            if rot == 90:
                cropped = cropped.transpose(Image.ROTATE_270)
            elif rot == 180:
                cropped = cropped.transpose(Image.ROTATE_180)
            elif rot == 270:
                cropped = cropped.transpose(Image.ROTATE_90)
            else:
                cropped = cropped.rotate(-rotation, expand=True, resample=Image.BICUBIC)
    return cropped


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------

XMP_NS_PREFIX = b'http://ns.adobe.com/xap/1.0/\x00'


def _jpeg_replace_xmp(jpeg_data: bytes, xmp_payload: bytes) -> bytes:
    """Remove existing XMP from JPEG and insert new XMP payload after SOI."""
    if len(jpeg_data) < 4 or jpeg_data[:2] != b'\xff\xd8':
        return jpeg_data

    output = bytearray(jpeg_data[:2])
    pos = 2

    while pos < len(jpeg_data) - 1:
        if jpeg_data[pos] != 0xFF:
            output.extend(jpeg_data[pos:])
            break
        marker = jpeg_data[pos:pos + 2]
        if marker == b'\xff\xda':
            output.extend(jpeg_data[pos:])
            break
        if marker[1] in (0x00, 0x01) or 0xd0 <= marker[1] <= 0xd9:
            output.extend(marker)
            pos += 2
            continue
        if pos + 4 > len(jpeg_data):
            output.extend(jpeg_data[pos:])
            break
        length = int.from_bytes(jpeg_data[pos + 2:pos + 4], 'big')
        segment = jpeg_data[pos:pos + 2 + length]
        if marker == b'\xff\xe1' and XMP_NS_PREFIX in segment[4:4 + len(XMP_NS_PREFIX) + 4]:
            pos += 2 + length
            continue
        output.extend(segment)
        pos += 2 + length

    full_payload = XMP_NS_PREFIX + xmp_payload
    xmp_seg = b'\xff\xe1' + (len(full_payload) + 2).to_bytes(2, 'big') + full_payload
    return bytes(output[:2]) + xmp_seg + bytes(output[2:])


def set_xmp_description(image_path: str, description: str):
    """Write XMP dc:description into a JPEG file."""
    escaped = xml.sax.saxutils.escape(description)
    xmp_xml = (
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '   <dc:description>\n'
        '    <rdf:Alt>\n'
        f'     <rdf:li xml:lang="x-default">{escaped}</rdf:li>\n'
        '    </rdf:Alt>\n'
        '   </dc:description>\n'
        '  </rdf:Description>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    ).encode('utf-8')

    try:
        with open(image_path, 'rb') as f:
            data = f.read()
        result = _jpeg_replace_xmp(data, xmp_xml)
        with open(image_path, 'wb') as f:
            f.write(result)
    except Exception as e:
        print(f"    Warning: could not write XMP: {e}")


# ---------------------------------------------------------------------------
# EXIF
# ---------------------------------------------------------------------------

def set_exif_metadata(image_path: str, date: str | None, location: str | None,
                      caption: str | None, default_location: str | None = None,
                      country_codes: str | None = None,
                      coords: tuple[float, float] | None = None):
    """Write EXIF metadata to a JPEG file.

    If `coords` is given (lat, lon), GPS is written directly (no geocoding).
    Otherwise, `location` string is geocoded via Nominatim.
    """
    try:
        exif_dict = piexif.load(image_path)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    if date:
        parts = date.split(":")
        if len(parts) == 1:
            exif_date = f"{parts[0]}:01:01 00:00:00"
        elif len(parts) == 2:
            exif_date = f"{parts[0]}:{parts[1]}:01 00:00:00"
        else:
            exif_date = f"{parts[0]}:{parts[1]}:{parts[2]} 00:00:00"
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_date.encode()
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_date.encode()
        exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_date.encode()

    gps_coords = coords
    if not gps_coords and location and location.strip():
        gps_coords = geocode_location(location.strip(), default_location, country_codes)

    if gps_coords:
        lat, lon = gps_coords
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = _decimal_to_dms(abs(lat))
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = _decimal_to_dms(abs(lon))
        label = location or f"{lat:.4f},{lon:.4f}"
        print(f"    GPS: {lat:.4f}, {lon:.4f} ({label})")

    if caption:
        exif_dict["0th"][piexif.ImageIFD.ImageDescription] = caption.encode("utf-8")

    try:
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, image_path)
    except Exception as e:
        print(f"    Warning: could not write EXIF: {e}")

    if caption:
        set_xmp_description(image_path, caption)


# ---------------------------------------------------------------------------
# Save cropped photos
# ---------------------------------------------------------------------------

def save_cropped_photos(
    image_path: str, photos: list[dict], output_dir: str,
) -> int:
    """Crop, rotate, and save photos with EXIF metadata. Returns count of saved photos."""
    image = Image.open(image_path)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    page_stem = Path(image_path).stem
    count = 0

    for i, photo in enumerate(photos, start=1):
        if photo.get("skip"):
            continue

        cropped = crop_and_rotate(image, photo)

        date = photo.get("date")
        caption = photo.get("caption")
        location_coords_str = photo.get("location")
        location_name = photo.get("location_name")

        if date:
            # Pad to YYYYMMDD for correct Finder sorting
            parts = date.split(":")
            year = parts[0] if len(parts) > 0 else "0000"
            month = parts[1] if len(parts) > 1 else "00"
            day = parts[2] if len(parts) > 2 else "00"
            date_prefix = f"{year}{month}{day}"
            out_name = f"{date_prefix}_{page_stem}_photo_{i:02d}.jpg"
        else:
            out_name = f"{page_stem}_photo_{i:02d}.jpg"

        out_path = os.path.join(output_dir, out_name)
        cropped.save(out_path, "JPEG", quality=95)

        coords = None
        if location_coords_str:
            parts = location_coords_str.split(",")
            if len(parts) == 2:
                try:
                    coords = (float(parts[0].strip()), float(parts[1].strip()))
                except ValueError:
                    pass

        set_exif_metadata(out_path, date=date, location=location_name,
                          caption=caption, coords=coords)

        info_parts = []
        if date:
            info_parts.append(f"date={date}")
        if location_name:
            info_parts.append(f"location={location_name}")
        if caption:
            info_parts.append(f"caption={caption}")
        print(f"    Saved: {out_name} ({', '.join(info_parts) or 'no metadata'})")
        count += 1

    return count


# ---------------------------------------------------------------------------
# Apply metadata
# ---------------------------------------------------------------------------

def apply_metadata(input_dir: Path, output_dir: str):
    """Read metadata JSON and produce cropped photos."""
    meta_path = input_dir / METADATA_FILENAME
    if not meta_path.exists():
        print(f"Error: {meta_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    os.makedirs(output_dir, exist_ok=True)
    total = 0

    for page in meta["pages"]:
        source = page["source"]
        photos = page["photos"]
        if not photos:
            continue

        image_path = str(input_dir / source)
        if not os.path.exists(image_path):
            print(f"  Warning: source image not found: {image_path}")
            continue

        print(f"Processing: {source}")
        count = save_cropped_photos(image_path, photos, output_dir)
        total += count
        print()

    print(f"Done! Extracted {total} photo(s) into {output_dir}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Crop photos from album page scans using autocrop_meta.json."
    )
    parser.add_argument("input", help="Directory with album page images and autocrop_meta.json")
    parser.add_argument("-o", "--output", default=None,
                        help="Output directory (default: <input>/cropped)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_dir():
        print(f"Error: {input_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output or str(input_path / "cropped")

    print("=" * 60)
    print("CROP & EXIF")
    print("=" * 60)
    print(f"Input:  {input_path}")
    print(f"Output: {output_dir}")
    print("=" * 60)
    print()

    apply_metadata(input_path, output_dir)


if __name__ == "__main__":
    main()
