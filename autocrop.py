#!/usr/bin/env python3
"""
Autocrop: Extract individual photos from album page scans.

Uses GPT/Gemini Vision API to detect photo boundaries, rotation angles,
and date/location metadata from album page images.

Accepts a file or directory as input. Processes pages in parallel.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import xml.sax.saxutils

import piexif
from openai import OpenAI
from PIL import Image


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}

PROVIDERS = {
    "openai": {
        "base_url": None,
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash-preview-05-20",
    },
}

VISION_PROMPT_TEMPLATE = """\
You are analyzing a scanned photo of an album page. The page may contain one or more individual photographs glued/placed on it, possibly with handwritten captions, dates, or location names near them.
{countries_hint}
For each individual photograph you can see on this album page, provide:

1. **bbox** — bounding box as [x1, y1, x2, y2] in percentage of image dimensions (0-100). (x1,y1) is top-left, (x2,y2) is bottom-right of the photo area.
2. **top_side** — which side of the BOUNDING BOX contains the TOP of the photo? To determine this: find people's heads, sky, ceilings, or text orientation in the photo. Where those point is the top.
   - "top": heads/sky are at the top of the bbox (photo is upright in the scan)
   - "bottom": heads/sky are at the bottom of the bbox (photo is glued upside-down)
   - "left": heads/sky are at the left side of the bbox (photo is rotated 90° counter-clockwise)
   - "right": heads/sky are at the right side of the bbox (photo is rotated 90° clockwise)
   IMPORTANT: If multiple photos are on the same page, check each one independently — they may have different orientations. Pay special attention to upside-down photos where hair is at the bottom and feet/ground are at the top.
3. **date** — date of the photo if visible (from captions/handwriting near the photo). Use format "YYYY:MM:DD" if full date is known, "YYYY:MM" if only month/year, "YYYY" if only year, or null if unknown.
4. **location** — location ONLY if explicitly written as text near the photo (handwritten or printed caption). Do NOT guess or infer location from the photo content. If no location text is visible, return null. IMPORTANT: normalize the location to a geocodable format "City, Country" (e.g. "Караганда, Казахстан", "Tuscaloosa, USA", "Омутнинск, Россия"). Extract the city/town name from the handwritten text and add the country. Keep the original language of the text (do NOT translate to English). Drop extra words like "near", "at the dacha in", "center of", etc.
5. **caption** — any handwritten text, caption or inscription near/on the photo that relates to it (e.g. names, comments, quotes). Transcribe exactly as written. Do NOT include date or location here — those go in the "date" and "location" fields. Only include additional text like names, comments, quotes. null if none.

Return a JSON object with a single key "photos" containing an array of objects.
Example:
{{
  "photos": [
    {{
      "bbox": [5.2, 10.1, 48.5, 55.3],
      "top_side": "top",
      "date": "1985:07",
      "location": "Сочи, Россия",
      "caption": "Think..."
    }}
  ]
}}

Be precise with bounding boxes — they should tightly wrap each individual photo, not the captions. If there are no photos on the page, return {{"photos": []}}.
"""


def _build_vision_prompt(countries: str | None = None) -> str:
    """Build the vision prompt, optionally adding country context."""
    if countries:
        codes = [c.strip().upper() for c in countries.split(",")]
        hint = (
            f"\nThese photos were taken in: {', '.join(codes)}. "
            "Use this context when interpreting handwritten location names — "
            "they likely refer to places in these countries.\n"
        )
    else:
        hint = ""
    return VISION_PROMPT_TEMPLATE.format(countries_hint=hint)


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

_geocode_cache: dict[str, tuple[float, float] | None] = {}
_geocode_lock = threading.Lock()


def geocode_location(location: str, default_location: str | None = None,
                     country_codes: str | None = None) -> tuple[float, float] | None:
    """Convert location name to (lat, lon) using Nominatim. Thread-safe.

    Args:
        country_codes: comma-separated ISO 3166-1 alpha-2 codes (e.g. "kz,ru")
                       to restrict geocoding results. If the location doesn't
                       resolve within these countries, falls back to default_location.
    """
    cache_key = f"{location}|{default_location or ''}|{country_codes or ''}"
    with _geocode_lock:
        if cache_key in _geocode_cache:
            return _geocode_cache[cache_key]

    queries = [location]
    if default_location and default_location.lower() not in location.lower():
        queries.append(f"{location}, {default_location}")
        # Last resort: just the default location (city), so we at least get city-level GPS
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
                time.sleep(1)  # Nominatim: 1 req/sec
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
# Helpers
# ---------------------------------------------------------------------------

def collect_images(path: Path) -> list[Path]:
    """Collect image files from a file or directory."""
    if path.is_file():
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            return [path]
        print(f"Error: {path} is not a supported image file", file=sys.stderr)
        sys.exit(1)
    elif path.is_dir():
        files = sorted(
            f for f in path.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
        )
        return files
    else:
        print(f"Error: {path} does not exist", file=sys.stderr)
        sys.exit(1)


def encode_image_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_image_media_type(image_path: str) -> str:
    ext = Path(image_path).suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def parse_json_response(content: str) -> dict:
    """Parse JSON from response, handling markdown code fences and truncated output."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r"```(?:json)?\s*\n?(.*)", content, re.DOTALL)
    raw = match.group(1).rstrip("`").strip() if match else content.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fixed = raw
    open_braces = fixed.count("{") - fixed.count("}")
    open_brackets = fixed.count("[") - fixed.count("]")
    fixed = re.sub(r",\s*$", "", fixed)
    fixed += "]" * open_brackets + "}" * open_braces
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    raise ValueError(f"Could not parse JSON from response: {content[:500]}...")


# ---------------------------------------------------------------------------
# Vision API
# ---------------------------------------------------------------------------

def analyze_page(client: OpenAI, image_path: str, model: str, provider: str,
                 prompt: str | None = None) -> list[dict]:
    """Send album page image to Vision API and get photo coordinates."""
    b64 = encode_image_base64(image_path)
    media_type = get_image_media_type(image_path)
    vision_prompt = prompt or _build_vision_prompt()

    print(f"  Analyzing with {provider}/{model}...")

    kwargs = dict(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": vision_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}", "detail": "high"}},
            ],
        }],
        max_tokens=16384,
    )
    if provider == "openai":
        kwargs["response_format"] = {"type": "json_object"}

    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            print(f"  AI response:\n{content}\n")
            data = parse_json_response(content)
            return data.get("photos", [])
        except Exception as e:
            error_str = str(e)
            retry_match = re.search(r"retry\s*(?:in|after)\s*([\d.]+)s", error_str, re.IGNORECASE)
            if "429" in error_str or "rate" in error_str.lower() or "quota" in error_str.lower():
                wait = float(retry_match.group(1)) + 2 if retry_match else min(30 * (2 ** attempt), 300)
                print(f"  Rate limited. Waiting {wait:.0f}s ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            elif attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)
                print(f"  API error: {error_str[:200]}")
                print(f"  Retrying in {wait}s ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise

    print(f"  WARNING: All {max_retries} retries exhausted, skipping this page")
    return []


# ---------------------------------------------------------------------------
# Verification (double-pass)
# ---------------------------------------------------------------------------

def _bbox_iou(a: list, b: list) -> float:
    """Intersection over Union for two [x1,y1,x2,y2] bboxes (percentage coords)."""
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


def _match_photos(list_a: list[dict], list_b: list[dict]) -> list[tuple[int, int, float]]:
    """Match photos from two passes by best IoU. Returns [(idx_a, idx_b, iou)]."""
    matches = []
    used_b = set()
    for i, a in enumerate(list_a):
        best_j, best_iou = -1, 0
        for j, b in enumerate(list_b):
            if j in used_b:
                continue
            iou = _bbox_iou(a["bbox"], b["bbox"])
            if iou > best_iou:
                best_j, best_iou = j, iou
        if best_j >= 0 and best_iou > 0.3:
            matches.append((i, best_j, best_iou))
            used_b.add(best_j)
    return matches


def _average_bbox(a: list, b: list) -> list:
    """Average two bboxes."""
    return [(a[i] + b[i]) / 2 for i in range(4)]


def _photos_agree(a: dict, b: dict, iou_threshold: float = 0.75) -> bool:
    """Check if two photo detections agree on bbox and rotation."""
    iou = _bbox_iou(a["bbox"], b["bbox"])
    same_top = (a.get("top_side", "top").lower() == b.get("top_side", "top").lower())
    return iou >= iou_threshold and same_top


def analyze_page_verified(
    client: OpenAI, image_path: str, model: str, provider: str,
    prompt: str | None = None,
) -> list[dict]:
    """Double-pass analysis: run twice, compare, arbitrate if needed."""

    print(f"  Pass 1/2...")
    pass1 = analyze_page(client, image_path, model, provider, prompt)

    print(f"  Pass 2/2...")
    pass2 = analyze_page(client, image_path, model, provider, prompt)

    # If different number of photos detected, need arbitration
    if len(pass1) != len(pass2):
        print(f"  Mismatch: pass1={len(pass1)} photos, pass2={len(pass2)} photos. Arbitrating...")
        pass3 = analyze_page(client, image_path, model, provider, prompt)
        # Use the count that 2 out of 3 agree on
        counts = [len(pass1), len(pass2), len(pass3)]
        if counts[0] == counts[1]:
            return pass1
        elif counts[0] == counts[2]:
            return pass3
        elif counts[1] == counts[2]:
            return pass2
        else:
            # All different — take the middle count
            passes = sorted([(pass1, len(pass1)), (pass2, len(pass2)), (pass3, len(pass3))], key=lambda x: x[1])
            return passes[1][0]

    if not pass1:
        return []

    # Match photos between passes
    matches = _match_photos(pass1, pass2)
    result = []
    needs_arbitration = []

    for idx_a, idx_b, iou in matches:
        a, b = pass1[idx_a], pass2[idx_b]
        if _photos_agree(a, b):
            # Both agree — average the bbox for better precision
            merged = dict(a)
            merged["bbox"] = _average_bbox(a["bbox"], b["bbox"])
            # Prefer non-null metadata
            for key in ("date", "location", "caption"):
                if not merged.get(key) and b.get(key):
                    merged[key] = b[key]
            result.append(merged)
        else:
            needs_arbitration.append((idx_a, idx_b, a, b))

    if needs_arbitration:
        print(f"  {len(needs_arbitration)} photo(s) disagree, arbitrating...")
        pass3 = analyze_page(client, image_path, model, provider, prompt)
        matches3 = _match_photos(pass1, pass3)
        match3_map = {i: j for i, j, _ in matches3}

        for idx_a, idx_b, a, b in needs_arbitration:
            # Check if pass3 agrees with either pass1 or pass2
            if idx_a in match3_map:
                c = pass3[match3_map[idx_a]]
                iou_ac = _bbox_iou(a["bbox"], c["bbox"])
                iou_bc = _bbox_iou(b["bbox"], c["bbox"])
                same_top_ac = a.get("top_side", "top").lower() == c.get("top_side", "top").lower()
                same_top_bc = b.get("top_side", "top").lower() == c.get("top_side", "top").lower()

                # Score: IoU agreement + rotation agreement
                score_a = iou_ac + (1.0 if same_top_ac else 0)
                score_b = iou_bc + (1.0 if same_top_bc else 0)

                if score_a >= score_b:
                    winner = a
                    print(f"    Photo {idx_a+1}: pass1 wins (score {score_a:.2f} vs {score_b:.2f})")
                else:
                    winner = b
                    print(f"    Photo {idx_a+1}: pass2 wins (score {score_a:.2f} vs {score_b:.2f})")
            else:
                winner = a  # fallback to pass1
                print(f"    Photo {idx_a+1}: no match in pass3, using pass1")

            result.append(winner)
    else:
        print(f"  All {len(result)} photo(s) verified ✓")

    # Sort by position (top-to-bottom, left-to-right)
    result.sort(key=lambda p: (p["bbox"][1], p["bbox"][0]))
    return result


# ---------------------------------------------------------------------------
# Crop & EXIF
# ---------------------------------------------------------------------------

def crop_and_rotate(image: Image.Image, photo_info: dict) -> Image.Image:
    w, h = image.size
    bbox = photo_info["bbox"]
    x1 = max(0, min(int(bbox[0] / 100 * w), w))
    y1 = max(0, min(int(bbox[1] / 100 * h), h))
    x2 = max(0, min(int(bbox[2] / 100 * w), w))
    y2 = max(0, min(int(bbox[3] / 100 * h), h))
    cropped = image.crop((x1, y1, x2, y2))

    # Support both new "top_side" and legacy "rotation" fields
    top_side = photo_info.get("top_side", "").lower().strip()
    if top_side:
        # top_side tells us where the top of the photo currently is in the scan
        if top_side == "bottom":
            cropped = cropped.transpose(Image.ROTATE_180)
        elif top_side == "left":
            # Top points left → rotate 90° clockwise to fix
            cropped = cropped.transpose(Image.ROTATE_270)
        elif top_side == "right":
            # Top points right → rotate 90° counter-clockwise to fix
            cropped = cropped.transpose(Image.ROTATE_90)
        # "top" = already upright, no rotation needed
    else:
        # Fallback to rotation field (degrees)
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


XMP_NS_PREFIX = b'http://ns.adobe.com/xap/1.0/\x00'


def _jpeg_replace_xmp(jpeg_data: bytes, xmp_payload: bytes) -> bytes:
    """Remove existing XMP from JPEG and insert new XMP payload after SOI."""
    if len(jpeg_data) < 4 or jpeg_data[:2] != b'\xff\xd8':
        return jpeg_data

    output = bytearray(jpeg_data[:2])  # SOI
    pos = 2

    while pos < len(jpeg_data) - 1:
        if jpeg_data[pos] != 0xFF:
            output.extend(jpeg_data[pos:])
            break
        marker = jpeg_data[pos:pos + 2]
        if marker == b'\xff\xda':  # SOS — rest is image data
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
        # Skip existing XMP APP1 segments
        if marker == b'\xff\xe1' and XMP_NS_PREFIX in segment[4:4 + len(XMP_NS_PREFIX) + 4]:
            pos += 2 + length
            continue
        output.extend(segment)
        pos += 2 + length

    # Build new XMP APP1 segment and insert after SOI
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


def set_exif_metadata(image_path: str, date: str | None, location: str | None,
                      caption: str | None, default_location: str | None = None,
                      country_codes: str | None = None):
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

    if location and location.strip():
        coords = geocode_location(location.strip(), default_location, country_codes)
        if coords:
            lat, lon = coords
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = _decimal_to_dms(abs(lat))
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = _decimal_to_dms(abs(lon))
            print(f"    GPS: {lat:.4f}, {lon:.4f} ({location})")

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
# Process one page
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    """Result from processing one page."""
    count: int
    page_date: str | None  # best date found on this page (or None)
    # files that were saved without a date (path, location, caption)
    undated_files: list[tuple[str, str | None, str | None]]


def process_page(
    client: OpenAI, image_path: str, output_dir: str,
    model: str, provider: str, default_location: str | None = None,
    country_codes: str | None = None,
    page_num: int = 0, total_pages: int = 0, verify: bool = False,
    no_location_spread: bool = False,
) -> PageResult:
    """Process one album page. Returns PageResult with date info for post-processing."""
    progress = f"[{page_num}/{total_pages}] " if total_pages else ""
    print(f"{progress}Processing: {image_path}")
    prompt = _build_vision_prompt(country_codes)
    if verify:
        photos = analyze_page_verified(client, image_path, model, provider, prompt)
    else:
        photos = analyze_page(client, image_path, model, provider, prompt)

    if not photos:
        print("  No photos detected.")
        return PageResult(count=0, page_date=None, undated_files=[])

    print(f"  Found {len(photos)} photo(s)")
    image = Image.open(image_path)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    page_stem = Path(image_path).stem

    # Page-level fallbacks (within this page only)
    _null_values = {"", "null", "None"}
    page_dates = [p["date"].strip() for p in photos if p.get("date") and str(p["date"]).strip() not in _null_values]
    page_locations = [p["location"].strip() for p in photos if p.get("location") and str(p["location"]).strip() not in _null_values]
    fallback_date = page_dates[0] if page_dates else None
    fallback_location = default_location if no_location_spread else (page_locations[0] if page_locations else default_location)

    count = 0
    undated_files = []
    for i, photo_info in enumerate(photos, start=1):
        cropped = crop_and_rotate(image, photo_info)

        raw_date = photo_info.get("date")
        date = raw_date.strip() if raw_date and str(raw_date).strip() not in ("", "null", "None") else fallback_date
        raw_loc = photo_info.get("location")
        location = raw_loc.strip() if raw_loc and str(raw_loc).strip() not in ("", "null", "None") else fallback_location
        raw_caption = photo_info.get("caption")
        caption = raw_caption.strip() if raw_caption and str(raw_caption).strip() not in ("", "null", "None") else None

        if date:
            date_prefix = date.replace(":", "")
            out_name = f"{date_prefix}_{page_stem}_photo_{i:02d}.jpg"
        else:
            out_name = f"{page_stem}_photo_{i:02d}.jpg"

        out_path = os.path.join(output_dir, out_name)
        cropped.save(out_path, "JPEG", quality=95)
        set_exif_metadata(out_path, date, location, caption, default_location, country_codes)

        if not date:
            undated_files.append((out_path, location, caption))

        info_parts = []
        if date:
            info_parts.append(f"date={date}")
        if location:
            info_parts.append(f"location={location}")
        if caption:
            info_parts.append(f"caption={caption}")
        print(f"    Saved: {out_name} ({', '.join(info_parts) or 'no metadata'})")
        count += 1

    return PageResult(count=count, page_date=fallback_date, undated_files=undated_files)


def backfill_dates(page_results: list[tuple[int, PageResult]], output_dir: str,
                   default_location: str | None, country_codes: str | None = None):
    """Post-processing: fill dates for undated photos from nearest previous page.

    Returns list of (path, location, caption) for files that are still undated.
    """
    # Sort by page index (original order in album)
    page_results.sort(key=lambda x: x[0])

    last_known_date = None
    files_updated = 0
    still_undated: list[tuple[str, str | None, str | None]] = []

    for _idx, result in page_results:
        if result.page_date:
            last_known_date = result.page_date

        if not result.undated_files:
            continue

        if not last_known_date:
            still_undated.extend(result.undated_files)
            continue

        for out_path, location, caption in result.undated_files:
            # Rename file to include date
            date_prefix = last_known_date.replace(":", "")
            old_name = Path(out_path).name
            new_name = f"{date_prefix}_{old_name}"
            new_path = os.path.join(output_dir, new_name)
            os.rename(out_path, new_path)

            # Update EXIF with inherited date
            set_exif_metadata(new_path, last_known_date, location, caption, default_location, country_codes)
            print(f"  Backfilled date {last_known_date} -> {new_name}")
            files_updated += 1

    if files_updated:
        print(f"  Updated {files_updated} file(s) with inherited dates")
    return still_undated


def ask_dates_interactive(page_results: list[tuple[int, PageResult]],
                          output_dir: str, default_location: str | None,
                          country_codes: str | None = None):
    """Prompt user to enter dates for undated photos.

    Modifies page_results in place: removes dated files from undated_files
    and sets page_date so subsequent backfill can use them.
    """
    page_results.sort(key=lambda x: x[0])
    all_undated = []
    for _, result in page_results:
        for entry in result.undated_files:
            all_undated.append((entry, result))

    if not all_undated:
        return

    print()
    print(f"{len(all_undated)} photo(s) have no date. Enter date for each (YYYY, YYYY:MM, or YYYY:MM:DD).")
    print("Press Enter to skip, 'q' to stop.\n")

    files_updated = 0
    for (out_path, location, caption), result in all_undated:
        name = Path(out_path).name
        parts = [f"  File: {name}"]
        if caption:
            parts.append(f"  Caption: {caption}")
        if location:
            parts.append(f"  Location: {location}")
        print("\n".join(parts))

        try:
            user_input = input("  Date: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Stopped.")
            break

        if user_input.lower() == "q":
            print("  Stopped.")
            break

        if not user_input:
            continue

        # Validate format
        if not re.match(r"^\d{4}(:\d{2}(:\d{2})?)?$", user_input):
            print(f"  Invalid format, skipping. Use YYYY, YYYY:MM, or YYYY:MM:DD")
            continue

        exif_date = user_input
        date_prefix = user_input.replace(":", "")
        new_name = f"{date_prefix}_{name}"
        new_path = os.path.join(output_dir, new_name)
        os.rename(out_path, new_path)

        set_exif_metadata(new_path, exif_date, location, caption, default_location, country_codes)
        print(f"  -> {new_name}")
        files_updated += 1

        # Update PageResult so backfill can use this date
        result.undated_files.remove((out_path, location, caption))
        if not result.page_date:
            result.page_date = exif_date

    if files_updated:
        print(f"\nUpdated {files_updated} file(s) with manual dates")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract individual photos from album page scans using Vision API."
    )
    parser.add_argument("input", help="Image file or directory with album page images")
    parser.add_argument("-o", "--output", default=None, help="Output directory (default: <input>/cropped)")
    parser.add_argument("-p", "--provider", default="openai", choices=PROVIDERS.keys())
    parser.add_argument("-m", "--model", default=None, help="Vision model name")
    parser.add_argument("--api-key", default=None, help="API key (default: from env var)")
    parser.add_argument("--default-location", default=None, help="Fallback city/region for geocoding")
    parser.add_argument("--countries", default=None,
                        help="Restrict geocoding to these countries (comma-separated ISO codes, e.g. 'kz,ru')")
    parser.add_argument("-j", "--jobs", type=int, default=4, help="Parallel pages (default: 4)")
    parser.add_argument("--verify", action="store_true",
                        help="Double-pass verification: analyze each page twice, arbitrate on disagreement (2-3x API cost)")
    parser.add_argument("--no-location-spread", action="store_true",
                        help="Don't apply a recognized location from one photo to other photos on the same page")
    parser.add_argument("--ask-dates", action="store_true",
                        help="Interactively ask for dates of undated photos after processing; skipped photos are backfilled from entered dates")
    args = parser.parse_args()

    input_path = Path(args.input)
    image_files = collect_images(input_path)
    if not image_files:
        print(f"No image files found in {input_path}")
        sys.exit(1)

    if args.output:
        output_dir = args.output
    elif input_path.is_dir():
        output_dir = str(input_path / "cropped")
    else:
        output_dir = str(input_path.parent / "cropped")
    os.makedirs(output_dir, exist_ok=True)

    provider_cfg = PROVIDERS[args.provider]
    model = args.model or provider_cfg["default_model"]
    api_key = args.api_key or os.environ.get(provider_cfg["env_key"])
    if not api_key:
        print(f"Error: Set {provider_cfg['env_key']} env var or pass --api-key", file=sys.stderr)
        sys.exit(1)

    client_kwargs = {"api_key": api_key}
    if provider_cfg["base_url"]:
        client_kwargs["base_url"] = provider_cfg["base_url"]
    client = OpenAI(**client_kwargs)

    print("=" * 60)
    print("AUTOCROP")
    print("=" * 60)
    print(f"Input:            {input_path}")
    print(f"Output:           {output_dir}")
    print(f"Pages:            {len(image_files)}")
    print(f"Provider:         {args.provider}")
    print(f"Model:            {model}")
    print(f"Default location: {args.default_location or '(none)'}")
    print(f"Countries:        {args.countries or '(any)'}")
    print(f"Parallel jobs:    {args.jobs}")
    print(f"Verify (2-pass):  {'yes (2-3x API cost)' if args.verify else 'no'}")
    print("=" * 60)
    print()

    total = 0
    errors = 0
    page_results: list[tuple[int, PageResult]] = []

    n_pages = len(image_files)

    if args.jobs <= 1 or n_pages == 1:
        for idx, img_path in enumerate(image_files, 1):
            try:
                result = process_page(client, str(img_path), output_dir, model, args.provider, args.default_location, args.countries, idx, n_pages, args.verify, args.no_location_spread)
                total += result.count
                page_results.append((idx, result))
            except Exception as e:
                errors += 1
                print(f"  ERROR processing {img_path.name}: {e}")
                print(f"  Skipping this page and continuing...")
            print()
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(process_page, client, str(img_path), output_dir, model, args.provider, args.default_location, args.countries, idx, n_pages, args.verify, args.no_location_spread): (idx, img_path)
                for idx, img_path in enumerate(image_files, 1)
            }
            for future in as_completed(futures):
                idx, img_path = futures[future]
                try:
                    result = future.result()
                    total += result.count
                    page_results.append((idx, result))
                except Exception as e:
                    errors += 1
                    print(f"  ERROR processing {img_path.name}: {e}")
                    print(f"  Skipping this page and continuing...")
                print()

    # Post-processing: handle undated photos
    has_undated = any(r.undated_files for _, r in page_results)

    if args.ask_dates and has_undated:
        ask_dates_interactive(page_results, output_dir, args.default_location, args.countries)
        # Backfill remaining skipped photos from dates entered above
        still_has_undated = any(r.undated_files for _, r in page_results)
        has_any_date = any(r.page_date for _, r in page_results)
        if still_has_undated and has_any_date:
            print("Backfilling skipped photos from entered dates...")
            still_undated = backfill_dates(page_results, output_dir, args.default_location, args.countries)
            if still_undated:
                print(f"  {len(still_undated)} photo(s) still have no date")
            print()
    elif has_undated:
        has_any_date = any(r.page_date for _, r in page_results)
        if has_any_date:
            print("Backfilling dates from neighboring pages...")
            still_undated = backfill_dates(page_results, output_dir, args.default_location, args.countries)
            if still_undated:
                print(f"  {len(still_undated)} photo(s) still have no date")
            print()

    print(f"Done! Extracted {total} photo(s) into {output_dir}")
    if errors:
        print(f"  ({errors} page(s) failed)")


if __name__ == "__main__":
    main()
