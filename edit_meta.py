#!/usr/bin/env python3
"""
Interactive metadata editor for cropped/enhanced photos.

Displays a table of photos with their date, location, and caption,
then accepts commands to modify metadata in bulk.

Usage:
    python edit_meta.py ./cropped/
    python edit_meta.py ./enhanced/ --countries "kz,ru"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import piexif

from autocrop import IMAGE_EXTENSIONS, set_exif_metadata, set_xmp_description, _decimal_to_dms


# ---------------------------------------------------------------------------
# Read metadata
# ---------------------------------------------------------------------------

def _trim_date(raw: str) -> str:
    """Trim padding from EXIF date to show original precision.

    '1989:05:01 00:00:00' → '1989:05'  (day=01 was padding)
    '1989:05:09 00:00:00' → '1989:05:09'
    '1989:01:01 00:00:00' → '1989'     (month+day=01:01 was padding)
    """
    date_part = raw.split(" ")[0] if " " in raw else raw
    parts = date_part.split(":")
    if len(parts) == 3:
        if parts[1] == "01" and parts[2] == "01":
            return parts[0]
        if parts[2] == "01":
            return f"{parts[0]}:{parts[1]}"
    return date_part


def _dms_to_decimal(dms: tuple, ref: bytes) -> float:
    """Convert EXIF DMS tuple to decimal degrees."""
    d = dms[0][0] / dms[0][1]
    m = dms[1][0] / dms[1][1]
    s = dms[2][0] / dms[2][1]
    decimal = d + m / 60 + s / 3600
    if ref in (b"S", b"W"):
        decimal = -decimal
    return decimal


def read_file_metadata(image_path: str) -> dict:
    """Read date, GPS coordinates, and caption from EXIF."""
    result: dict = {"date": None, "gps": None, "caption": None, "location": None}
    try:
        exif_dict = piexif.load(image_path)

        # Date
        dt = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        if dt:
            raw = dt.decode("utf-8") if isinstance(dt, bytes) else str(dt)
            result["date"] = _trim_date(raw.strip())

        # GPS
        gps = exif_dict.get("GPS", {})
        if piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps:
            lat = _dms_to_decimal(
                gps[piexif.GPSIFD.GPSLatitude],
                gps.get(piexif.GPSIFD.GPSLatitudeRef, b"N"),
            )
            lon = _dms_to_decimal(
                gps[piexif.GPSIFD.GPSLongitude],
                gps.get(piexif.GPSIFD.GPSLongitudeRef, b"E"),
            )
            result["gps"] = (round(lat, 4), round(lon, 4))

        # Caption
        desc = exif_dict.get("0th", {}).get(piexif.ImageIFD.ImageDescription)
        if desc:
            text = desc.decode("utf-8") if isinstance(desc, bytes) else str(desc)
            if text.strip():
                result["caption"] = text.strip()
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Reverse geocoding
# ---------------------------------------------------------------------------

_reverse_cache: dict[tuple[float, float], str] = {}


def _reverse_geocode(lat: float, lon: float) -> str | None:
    """Reverse geocode (lat, lon) → 'City, Country' via Nominatim."""
    key = (round(lat, 3), round(lon, 3))
    if key in _reverse_cache:
        return _reverse_cache[key]
    try:
        params = urllib.parse.urlencode({
            "lat": lat, "lon": lon, "format": "json", "zoom": 10,
            "accept-language": "en",
        })
        url = f"https://nominatim.openstreetmap.org/reverse?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "autocrop-script/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        addr = data.get("address", {})
        city = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("county") or "")
        country = addr.get("country", "")
        if city and country:
            name = f"{city}, {country}"
        elif city:
            name = city
        else:
            name = data.get("display_name", "?").split(",")[0]
        _reverse_cache[key] = name
        return name
    except Exception:
        return None


def resolve_locations(files_meta: list[tuple[Path, dict]]):
    """Resolve GPS coordinates to location names for all files."""
    # Collect unique GPS coords
    gps_to_indices: dict[tuple[float, float], list[int]] = {}
    for i, (_, meta) in enumerate(files_meta):
        if meta["gps"]:
            gps_to_indices.setdefault(meta["gps"], []).append(i)

    if not gps_to_indices:
        return

    total = len(gps_to_indices)
    print(f"Resolving {total} unique location(s)...", end="", flush=True)

    resolved: list[tuple[tuple[float, float], str]] = []
    done = 0
    for coords, indices in gps_to_indices.items():
        name = _reverse_geocode(*coords)
        done += 1
        print(f"\rResolving {total} unique location(s)... {done}/{total}", end="", flush=True)
        if name:
            resolved.append((coords, name))
            for i in indices:
                files_meta[i][1]["location"] = name
        if done < total:
            time.sleep(1)  # Nominatim: 1 req/sec

    print()  # newline after progress

    # Print GPS → Location mapping
    if resolved:
        print()
        for coords, name in resolved:
            print(f"  {coords[0]:>9.4f}, {coords[1]:>9.4f}  →  {name}")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

LOC_WIDTH = 30


def display_table(files_meta: list[tuple[Path, dict]]):
    """Print numbered table of photos with metadata."""
    max_name = max((len(f.name) for f, _ in files_meta), default=10)
    max_name = max(max_name, 4)
    num_width = len(str(len(files_meta)))

    header = (f"{'#':>{num_width}}  "
              f"{'File':<{max_name}}  "
              f"{'Date':<12}  "
              f"{'Location':<{LOC_WIDTH}}  "
              f"Caption")
    print(header)
    print("-" * max(len(header), 80))

    for i, (path, meta) in enumerate(files_meta, 1):
        date_str = meta["date"] or "—"
        loc_raw = meta.get("location") or "—"
        if len(loc_raw) > LOC_WIDTH:
            loc_str = loc_raw[:LOC_WIDTH - 1] + "…"
        else:
            loc_str = loc_raw
        caption_str = meta["caption"] or "—"
        if len(caption_str) > 50:
            caption_str = caption_str[:47] + "..."
        print(f"{i:>{num_width}}  "
              f"{path.name:<{max_name}}  "
              f"{date_str:<12}  "
              f"{loc_str:<{LOC_WIDTH}}  "
              f"{caption_str}")


_COORDS_RE = re.compile(r"^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$")


def _parse_coords(value: str) -> tuple[float, float] | None:
    """Try to parse 'lat, lon' from value. Returns (lat, lon) or None."""
    m = _COORDS_RE.match(value.strip())
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def _write_gps(image_path: str, lat: float, lon: float):
    """Write GPS coordinates directly to EXIF."""
    try:
        exif_dict = piexif.load(image_path)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
    exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = _decimal_to_dms(abs(lat))
    exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
    exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = _decimal_to_dms(abs(lon))
    try:
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, image_path)
    except Exception as e:
        print(f"    Warning: could not write GPS: {e}")


# ---------------------------------------------------------------------------
# Parse & apply
# ---------------------------------------------------------------------------

def parse_command(cmd: str, num_files: int) -> tuple[int, str, str] | None:
    """Parse command like '3D 1989:06' → (2, 'D', '1989:06'). Returns None on error."""
    match = re.match(r"^(\d+)([DLCdlc])\s+(.+)$", cmd)
    if not match:
        print(f"  Invalid command format. Use: <number><D|L|C> <value>")
        return None

    idx = int(match.group(1))
    field = match.group(2).upper()
    value = match.group(3).strip()

    if idx < 1 or idx > num_files:
        print(f"  Invalid photo number: {idx} (must be 1-{num_files})")
        return None

    if field == "D":
        if not re.match(r"^\d{4}(:\d{2}(:\d{2})?)?$", value):
            print(f"  Invalid date format. Use: YYYY, YYYY:MM, or YYYY:MM:DD")
            return None

    return (idx - 1, field, value)


def apply_changes(changes: dict[int, dict[str, str]],
                  files_meta: list[tuple[Path, dict]],
                  country_codes: str | None) -> list[Path]:
    """Apply collected changes to files. Returns list of changed file paths."""
    changed_paths: list[Path] = []
    for idx, edits in sorted(changes.items()):
        path, meta = files_meta[idx]
        name = path.name

        # Merge: use new value if edited, otherwise keep current
        date = edits.get("D", meta["date"])
        location = edits.get("L")
        caption = edits.get("C", meta["caption"])

        current_path = path

        # Rename if date changed and filename has a date prefix
        if "D" in edits:
            new_prefix = edits["D"].replace(":", "")
            date_match = re.match(r"^(\d{4,8})_(.*)", name)
            if date_match:
                new_name = f"{new_prefix}_{date_match.group(2)}"
            else:
                new_name = f"{new_prefix}_{name}"
            new_path = path.parent / new_name
            os.rename(path, new_path)
            current_path = new_path
            print(f"  [{name}] renamed -> {new_name}")

        # Check if location is raw coordinates
        coords = _parse_coords(location) if location else None

        if coords:
            # Write GPS directly (skip geocoding)
            set_exif_metadata(
                str(current_path),
                date=date,
                location=None,
                caption=caption,
                default_location=None,
                country_codes=country_codes,
            )
            _write_gps(str(current_path), *coords)
            print(f"    GPS: {coords[0]:.6f}, {coords[1]:.6f} (direct)")
        else:
            # Write EXIF (set_exif_metadata handles geocoding for location)
            set_exif_metadata(
                str(current_path),
                date=date,
                location=location,
                caption=caption,
                default_location=None,
                country_codes=country_codes,
            )

        # Write XMP if caption changed
        if "C" in edits:
            set_xmp_description(str(current_path), edits["C"])

        parts = []
        if "D" in edits:
            parts.append(f"date={edits['D']}")
        if "L" in edits:
            parts.append(f"location={edits['L']}")
        if "C" in edits:
            parts.append(f"caption={edits['C'][:40]}")
        print(f"  [{current_path.name}] updated: {', '.join(parts)}")
        changed_paths.append(current_path)
    return changed_paths


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Interactive metadata editor for photos."
    )
    parser.add_argument("input", help="Directory with photos")
    parser.add_argument("--countries", default=None,
                        help="Restrict geocoding to these countries (comma-separated ISO codes, e.g. 'kz,ru')")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_dir():
        print(f"Error: {input_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    image_files = sorted(
        f for f in input_path.iterdir()
        if f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    )
    if not image_files:
        print(f"No image files found in {input_path}")
        sys.exit(1)

    # Read metadata
    files_meta: list[tuple[Path, dict]] = []
    for f in image_files:
        meta = read_file_metadata(str(f))
        files_meta.append((f, meta))

    # Resolve GPS → location names (with progress)
    resolve_locations(files_meta)

    # Display table
    print()
    display_table(files_meta)
    print()

    # Collect commands
    changes: dict[int, dict[str, str]] = {}
    print("Enter commands: <number><D|L|C> <value>  (empty line to apply)")
    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not cmd:
            break

        parsed = parse_command(cmd, len(files_meta))
        if parsed is None:
            continue

        idx, field, value = parsed
        changes.setdefault(idx, {})[field] = value

    if not changes:
        print("No changes.")
        return

    # Apply
    print(f"\nApplying {sum(len(v) for v in changes.values())} change(s) to {len(changes)} file(s)...")
    changed_paths = apply_changes(changes, files_meta, args.countries)

    # Re-read and display changed files
    if changed_paths:
        changed_meta: list[tuple[Path, dict]] = []
        for p in changed_paths:
            meta = read_file_metadata(str(p))
            changed_meta.append((p, meta))
        resolve_locations(changed_meta)
        print()
        display_table(changed_meta)
    print("\nDone!")


if __name__ == "__main__":
    main()
