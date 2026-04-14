#!/usr/bin/env python3
"""
AI Parse: Analyze album page scans using Vision AI.

Uses GPT/Gemini Vision API to detect photo boundaries, rotation angles,
and date/location metadata. Saves results to autocrop_meta.json.

Use crop_exif.py to produce cropped photos from the metadata.
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

from PIL import Image

from crop_exif import save_cropped_photos

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


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
        "default_model": "gemini-3-flash-preview",
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
# Analyze a single page (AI call only, no cropping)
# ---------------------------------------------------------------------------

_NULL_VALUES = {"", "null", "None"}


def _clean(val: str | None) -> str | None:
    """Return stripped value or None if null-like."""
    if val and str(val).strip() not in _NULL_VALUES:
        return str(val).strip()
    return None


def analyze_single_page(
    client: OpenAI, image_path: str, model: str, provider: str,
    country_codes: str | None = None,
    page_num: int = 0, total_pages: int = 0,
) -> list[dict]:
    """Run Vision AI on a single page. Returns list of photo dicts from AI."""
    progress = f"[{page_num}/{total_pages}] " if total_pages else ""
    print(f"{progress}Processing: {image_path}")
    prompt = _build_vision_prompt(country_codes)
    photos = analyze_page(client, image_path, model, provider, prompt)
    if not photos:
        print("  No photos detected.")
    else:
        print(f"  Found {len(photos)} photo(s)")
    return photos


def _resolve_page_metadata(
    photos: list[dict],
    default_location: str | None,
    country_codes: str | None,
    no_location_spread: bool,
) -> list[dict]:
    """Apply page-level fallbacks and geocode locations. Returns enriched photo dicts.

    Each returned dict has: bbox, top_side, date, location (coords string or None),
    location_name (display name or None), caption, skip.
    """
    page_dates = [_clean(p.get("date")) for p in photos]
    page_dates_valid = [d for d in page_dates if d]
    page_locs = [_clean(p.get("location")) for p in photos]
    page_locs_valid = [loc for loc in page_locs if loc]

    fallback_date = page_dates_valid[0] if page_dates_valid else None
    fallback_location = default_location if no_location_spread else (
        page_locs_valid[0] if page_locs_valid else default_location
    )

    result = []
    for photo in photos:
        date = _clean(photo.get("date")) or fallback_date
        location_text = _clean(photo.get("location")) or fallback_location
        caption = _clean(photo.get("caption"))

        # Geocode location text → coords
        location_coords = None
        location_name = location_text
        if location_text:
            coords = geocode_location(location_text, default_location, country_codes)
            if coords:
                location_coords = f"{coords[0]:.6f}, {coords[1]:.6f}"
                # location_name stays as the original text

        result.append({
            "bbox": photo["bbox"],
            "top_side": photo.get("top_side", "top"),
            "date": date,
            "location": location_coords,
            "location_name": location_name,
            "caption": caption,
            "skip": False,
        })
    return result


def _backfill_dates_in_metadata(pages: list[dict]):
    """Backfill dates across pages in the metadata structure (modifies in place)."""
    last_known_date = None
    for page in pages:
        # Find first date on this page
        page_date = None
        for photo in page["photos"]:
            if photo.get("date"):
                page_date = photo["date"]
                break
        if page_date:
            last_known_date = page_date
        elif last_known_date:
            # Backfill undated photos on this page
            for photo in page["photos"]:
                if not photo.get("date"):
                    photo["date"] = last_known_date


# ---------------------------------------------------------------------------
# Operation modes
# ---------------------------------------------------------------------------

METADATA_FILENAME = "autocrop_meta.json"


def create_metadata(
    client: OpenAI, image_files: list[Path], input_dir: Path,
    model: str, provider: str, default_location: str | None,
    country_codes: str | None, no_location_spread: bool, jobs: int,
):
    """Analyze pages and save metadata JSON (no cropping)."""
    n_pages = len(image_files)
    pages: list[dict] = [None] * n_pages  # type: ignore[list-item]
    errors = 0

    def _process_one(idx: int, img_path: Path) -> tuple[int, list[dict] | None]:
        try:
            raw_photos = analyze_single_page(
                client, str(img_path), model, provider,
                country_codes, idx + 1, n_pages,
            )
            if not raw_photos:
                return idx, None
            resolved = _resolve_page_metadata(
                raw_photos, default_location, country_codes, no_location_spread,
            )
            return idx, resolved
        except Exception as e:
            print(f"  ERROR processing {img_path.name}: {e}")
            return idx, None

    if jobs <= 1 or n_pages == 1:
        for idx, img_path in enumerate(image_files):
            i, result = _process_one(idx, img_path)
            if result is None:
                errors += 1
                pages[i] = {"source": img_path.name, "photos": []}
            else:
                pages[i] = {"source": img_path.name, "photos": result}
            print()
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(_process_one, idx, img_path): (idx, img_path)
                for idx, img_path in enumerate(image_files)
            }
            for future in as_completed(futures):
                idx, img_path = futures[future]
                try:
                    i, result = future.result()
                    if result is None:
                        errors += 1
                        pages[i] = {"source": img_path.name, "photos": []}
                    else:
                        pages[i] = {"source": img_path.name, "photos": result}
                except Exception as e:
                    errors += 1
                    pages[idx] = {"source": img_path.name, "photos": []}
                    print(f"  ERROR processing {img_path.name}: {e}")
                print()

    # Backfill dates across pages
    _backfill_dates_in_metadata(pages)

    total_photos = sum(len(p["photos"]) for p in pages)
    meta = {"version": 2, "pages": pages}
    meta_path = input_dir / METADATA_FILENAME
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"Saved metadata for {total_photos} photo(s) across {n_pages} page(s)")
    print(f"  -> {meta_path}")
    if errors:
        print(f"  ({errors} page(s) failed)")


def auto_apply(
    client: OpenAI, image_files: list[Path], output_dir: str,
    model: str, provider: str, default_location: str | None,
    country_codes: str | None, no_location_spread: bool, jobs: int,
):
    """Legacy mode: analyze + crop + save in one shot."""
    n_pages = len(image_files)
    total = 0
    errors = 0

    def _process_one(idx: int, img_path: Path) -> int:
        raw_photos = analyze_single_page(
            client, str(img_path), model, provider,
            country_codes, idx + 1, n_pages,
        )
        if not raw_photos:
            return 0
        resolved = _resolve_page_metadata(
            raw_photos, default_location, country_codes, no_location_spread,
        )
        return save_cropped_photos(str(img_path), resolved, output_dir)

    if jobs <= 1 or n_pages == 1:
        for idx, img_path in enumerate(image_files):
            try:
                total += _process_one(idx, img_path)
            except Exception as e:
                errors += 1
                print(f"  ERROR processing {img_path.name}: {e}")
            print()
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(_process_one, idx, img_path): (idx, img_path)
                for idx, img_path in enumerate(image_files)
            }
            for future in as_completed(futures):
                idx, img_path = futures[future]
                try:
                    total += future.result()
                except Exception as e:
                    errors += 1
                    print(f"  ERROR processing {img_path.name}: {e}")
                print()

    print(f"Done! Extracted {total} photo(s) into {output_dir}")
    if errors:
        print(f"  ({errors} page(s) failed)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze album page scans using Vision AI. Saves results to autocrop_meta.json."
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
    parser.add_argument("--no-location-spread", action="store_true",
                        help="Don't apply a recognized location from one photo to other photos on the same page")

    parser.add_argument("--auto-apply", action="store_true",
                        help="Analyze + crop + save in one shot (legacy behavior)")
    args = parser.parse_args()

    input_path = Path(args.input)

    # Resolve output directory
    if args.output:
        output_dir = args.output
    elif input_path.is_dir():
        output_dir = str(input_path / "cropped")
    else:
        output_dir = str(input_path.parent / "cropped")

    # --- Collect images, create client ---
    image_files = collect_images(input_path)
    if not image_files:
        print(f"No image files found in {input_path}")
        sys.exit(1)

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

    mode_label = "auto-apply" if args.auto_apply else "create-metadata"
    print("=" * 60)
    print(f"AI PARSE — {mode_label}")
    print("=" * 60)
    print(f"Input:            {input_path}")
    print(f"Output:           {output_dir}")
    print(f"Pages:            {len(image_files)}")
    print(f"Provider:         {args.provider}")
    print(f"Model:            {model}")
    print(f"Default location: {args.default_location or '(none)'}")
    print(f"Countries:        {args.countries or '(any)'}")
    print(f"Parallel jobs:    {args.jobs}")
    print("=" * 60)
    print()

    os.makedirs(output_dir, exist_ok=True)

    if args.auto_apply:
        auto_apply(client, image_files, output_dir, model, args.provider,
                   args.default_location, args.countries,
                   args.no_location_spread, args.jobs)
    else:
        in_dir = input_path if input_path.is_dir() else input_path.parent
        create_metadata(client, image_files, in_dir, model, args.provider,
                        args.default_location, args.countries,
                        args.no_location_spread, args.jobs)


if __name__ == "__main__":
    main()
