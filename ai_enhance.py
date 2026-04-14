#!/usr/bin/env python3
"""
Enhance photos via Gemini image generation API.

Accepts a file or directory as input.

Two modes:
  - Real-time (default): parallel workers with rate limiting
  - Batch (--batch): submit all photos as a batch job, wait for results (50% cheaper)

Usage:
    python enhance.py photo.jpg                         # single file, real-time
    python enhance.py ./cropped/                        # directory, real-time
    python enhance.py ./cropped/ -j 5 --rpm 10          # 5 workers, 10 RPM
    python enhance.py ./cropped/ --batch                # batch mode (50% cheaper)
    python enhance.py ./cropped/ --batch --poll 60      # batch, poll every 60s
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import piexif
from PIL import Image

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}

ENHANCE_PROMPT = """\
You are a professional photo restoration expert.
Please restore and enhance this old photograph:

1. Remove any album corner holders (triangular paper holders in corners)
2. Remove scratches, spots, and other physical damage
3. Improve contrast and brightness levels
4. Colorize the photo naturally if it's black and white
5. Keep the original composition and framing intact
6. Make it look like a high-quality modern photograph

Return ONLY the restored image, no text."""


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket rate limiter for API calls."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm
        self.lock = threading.Lock()
        self.last_call = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_call
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self.last_call = time.monotonic()


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
        return sorted(
            f for f in path.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
        )
    else:
        print(f"Error: {path} does not exist", file=sys.stderr)
        sys.exit(1)


def get_mime_type(image_path: str) -> str:
    """Determine MIME type from file extension."""
    ext = Path(image_path).suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".tiff": "image/tiff", ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


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


def _jpeg_extract_xmp(jpeg_data: bytes) -> bytes | None:
    """Extract raw XMP payload (without namespace prefix) from JPEG data."""
    if len(jpeg_data) < 4 or jpeg_data[:2] != b'\xff\xd8':
        return None
    pos = 2
    while pos < len(jpeg_data) - 1:
        if jpeg_data[pos] != 0xFF:
            break
        marker = jpeg_data[pos:pos + 2]
        if marker == b'\xff\xda':
            break
        if marker[1] in (0x00, 0x01) or 0xd0 <= marker[1] <= 0xd9:
            pos += 2
            continue
        if pos + 4 > len(jpeg_data):
            break
        length = int.from_bytes(jpeg_data[pos + 2:pos + 4], 'big')
        ns_start = pos + 4
        ns_end = ns_start + len(XMP_NS_PREFIX)
        if marker == b'\xff\xe1' and jpeg_data[ns_start:ns_end] == XMP_NS_PREFIX:
            return jpeg_data[ns_end:pos + 2 + length]
        pos += 2 + length
    return None


def preserve_xmp(source_path: str, dest_path: str):
    """Copy XMP metadata from source to dest JPEG."""
    try:
        with open(source_path, 'rb') as f:
            src_data = f.read()
        xmp_payload = _jpeg_extract_xmp(src_data)
        if not xmp_payload:
            return
        with open(dest_path, 'rb') as f:
            dest_data = f.read()
        result = _jpeg_replace_xmp(dest_data, xmp_payload)
        with open(dest_path, 'wb') as f:
            f.write(result)
    except Exception:
        pass


def preserve_exif(source_path: str, dest_path: str):
    """Copy EXIF and XMP metadata from source to dest image."""
    try:
        exif_dict = piexif.load(source_path)
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, dest_path)
    except Exception:
        pass
    preserve_xmp(source_path, dest_path)


# ---------------------------------------------------------------------------
# Real-time enhancement (one photo at a time)
# ---------------------------------------------------------------------------

def enhance_single(
    client: genai.Client,
    image_path: str,
    output_path: str,
    model: str,
    rate_limiter: RateLimiter,
    max_retries: int = 5,
) -> bool:
    """Enhance one photo via Gemini. Returns True on success."""
    with open(image_path, "rb") as f:
        image_data = f.read()

    name = Path(image_path).name
    idx = getattr(rate_limiter, '_counter', None)
    if idx is not None:
        with rate_limiter.lock:
            rate_limiter._counter += 1
            current = rate_limiter._counter
        total_str = f"[{current}/{rate_limiter._total}] " if rate_limiter._total else ""
    else:
        total_str = ""
    print(f"  {total_str}[{name}] Enhancing ({len(image_data) / 1024:.0f} KB)...")

    for attempt in range(max_retries):
        rate_limiter.wait()
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    ENHANCE_PROMPT,
                    types.Part.from_bytes(data=image_data, mime_type=get_mime_type(image_path)),
                ],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                ),
            )

            if not response.candidates or not response.candidates[0].content or not response.candidates[0].content.parts:
                # Log raw response for debugging
                block_reason = getattr(response, 'prompt_feedback', None)
                finish_reason = response.candidates[0].finish_reason if response.candidates else None
                print(f"  [{name}] Empty response (attempt {attempt + 1}/{max_retries})")
                print(f"    block_reason={block_reason}, finish_reason={finish_reason}")
                print(f"    raw: {str(response)[:500]}")
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                return False

            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    result_data = part.inline_data.data
                    result_image = Image.open(io.BytesIO(result_data))
                    if result_image.mode != "RGB":
                        result_image = result_image.convert("RGB")
                    result_image.save(output_path, "JPEG", quality=95)
                    preserve_exif(image_path, output_path)
                    print(f"  [{name}] OK ({len(result_data) / 1024:.0f} KB)")
                    return True
                elif part.text:
                    print(f"  [{name}] Model text: {part.text[:200]}")

            print(f"  [{name}] Warning: no image in response")
            return False

        except Exception as e:
            error_str = str(e)
            retry_match = re.search(r"retry\s*(?:in|after)\s*([\d.]+)s", error_str, re.IGNORECASE)
            if "429" in error_str or "rate" in error_str.lower() or "quota" in error_str.lower():
                wait = float(retry_match.group(1)) + 2 if retry_match else min(30 * (2 ** attempt), 300)
                print(f"  [{name}] Rate limited. Waiting {wait:.0f}s ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            elif attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)
                print(f"  [{name}] Error: {error_str[:200]}")
                print(f"  [{name}] Retrying in {wait}s ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                print(f"  [{name}] Failed after {max_retries} attempts")
                return False

    return False


def run_realtime(client, image_files, output_dir, model, jobs, rpm):
    """Run real-time enhancement with parallel workers."""
    rate_limiter = RateLimiter(rpm)
    rate_limiter._counter = 0
    rate_limiter._total = len(image_files)
    success = 0
    failed = 0

    if jobs <= 1 or len(image_files) == 1:
        for img_path in image_files:
            out_path = os.path.join(output_dir, img_path.name)
            if enhance_single(client, str(img_path), out_path, model, rate_limiter):
                success += 1
            else:
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {}
            for img_path in image_files:
                out_path = os.path.join(output_dir, img_path.name)
                future = pool.submit(enhance_single, client, str(img_path), out_path, model, rate_limiter)
                futures[future] = img_path
            for future in as_completed(futures):
                try:
                    if future.result():
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    print(f"  ERROR: {futures[future].name}: {e}")

    return success, failed


# ---------------------------------------------------------------------------
# Batch enhancement
# ---------------------------------------------------------------------------

def build_batch_request(image_path: str, key: str) -> dict:
    """Build a single batch request entry for a photo."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    mime = get_mime_type(image_path)
    return {
        "key": key,
        "request": {
            "contents": [{
                "parts": [
                    {"text": ENHANCE_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": image_b64}},
                ],
            }],
            "generation_config": {
                "response_modalities": ["TEXT", "IMAGE"],
            },
        },
    }


def run_batch(client, image_files, output_dir, model, poll_interval):
    """Submit batch job, wait for completion, download results."""

    # Map key -> original path
    key_to_path = {}
    for i, img_path in enumerate(image_files):
        key_to_path[f"photo_{i:04d}_{img_path.stem}"] = img_path

    # Build JSONL file
    print("Building batch request file...")
    jsonl_path = os.path.join(output_dir, "_batch_request.jsonl")
    with open(jsonl_path, "w") as f:
        for key, img_path in key_to_path.items():
            entry = build_batch_request(str(img_path), key)
            f.write(json.dumps(entry) + "\n")

    jsonl_size = os.path.getsize(jsonl_path) / (1024 * 1024)
    print(f"  Request file: {jsonl_path} ({jsonl_size:.1f} MB, {len(key_to_path)} photos)")

    # Upload
    print("Uploading batch request...")
    uploaded_file = client.files.upload(
        file=jsonl_path,
        config=types.UploadFileConfig(
            display_name="enhance-batch-request",
            mime_type="jsonl",
        ),
    )
    print(f"  Uploaded: {uploaded_file.name}")

    # Create batch job
    print(f"Creating batch job with model {model}...")
    batch_job = client.batches.create(
        model=model,
        src=uploaded_file.name,
        config={"display_name": f"enhance-{int(time.time())}"},
    )
    print(f"  Job: {batch_job.name}")
    print(f"  Polling every {poll_interval}s...")
    print()

    # Poll for completion
    completed_states = {
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
    }

    start_time = time.monotonic()
    while True:
        job = client.batches.get(name=batch_job.name)
        state = job.state.name if hasattr(job.state, "name") else str(job.state)
        elapsed = time.monotonic() - start_time
        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))

        if state in completed_states:
            print(f"  [{elapsed_str}] Job finished: {state}")
            break

        print(f"  [{elapsed_str}] Status: {state}...")
        time.sleep(poll_interval)

    if state != "JOB_STATE_SUCCEEDED":
        print(f"  Batch job failed with state: {state}")
        # Clean up JSONL
        try:
            os.remove(jsonl_path)
        except OSError:
            pass
        return 0, len(key_to_path), list(key_to_path.values())

    # Download results
    print("Downloading results...")
    dest_file = job.dest.file_name if hasattr(job.dest, "file_name") else job.dest
    result_content = client.files.download(file=dest_file)

    # Parse results JSONL
    success = 0
    failed = 0
    failed_paths = []

    # result_content may be bytes or file-like
    if isinstance(result_content, bytes):
        lines = result_content.decode("utf-8").strip().split("\n")
    else:
        lines = result_content.read().decode("utf-8").strip().split("\n")

    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            failed += 1
            continue

        key = entry.get("key", "")
        original_path = key_to_path.get(key)
        if not original_path:
            failed += 1
            continue

        # Extract image from response
        image_saved = False
        response = entry.get("response", {})
        candidates = response.get("candidates", [])
        for candidate in candidates:
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                inline_data = part.get("inline_data") or part.get("inlineData")
                if inline_data:
                    mime = inline_data.get("mime_type") or inline_data.get("mimeType", "")
                    if mime.startswith("image/"):
                        img_data = base64.b64decode(inline_data["data"])
                        out_path = os.path.join(output_dir, original_path.name)
                        result_image = Image.open(io.BytesIO(img_data))
                        if result_image.mode != "RGB":
                            result_image = result_image.convert("RGB")
                        result_image.save(out_path, "JPEG", quality=95)
                        preserve_exif(str(original_path), out_path)
                        print(f"  [{original_path.name}] OK ({len(img_data) / 1024:.0f} KB)")
                        image_saved = True
                        break
            if image_saved:
                break

        if image_saved:
            success += 1
        else:
            failed += 1
            failed_paths.append(original_path)
            print(f"  [{original_path.name}] No image in batch response")

    # Clean up JSONL
    try:
        os.remove(jsonl_path)
    except OSError:
        pass

    return success, failed, failed_paths


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Enhance photos via Gemini (restore, colorize, remove defects)."
    )
    parser.add_argument("input", help="Image file or directory with photos to enhance")
    parser.add_argument("-o", "--output", default=None, help="Output directory (default: <input>/enhanced)")
    parser.add_argument("-m", "--model", default="gemini-3.1-flash-image-preview",
                        help="Gemini image model (default: gemini-3.1-flash-image-preview)")
    parser.add_argument("--api-key", default=None, help="Gemini API key (default: GEMINI_API_KEY env var)")

    # Real-time options
    rt_group = parser.add_argument_group("real-time mode (default)")
    rt_group.add_argument("-j", "--jobs", type=int, default=3, help="Parallel workers (default: 3)")
    rt_group.add_argument("--rpm", type=int, default=10, help="Max requests per minute (default: 10)")

    # Batch options
    batch_group = parser.add_argument_group("batch mode")
    batch_group.add_argument("--batch", action="store_true", help="Use Batch API (50%% cheaper, slower)")
    batch_group.add_argument("--poll", type=int, default=30, help="Batch poll interval in seconds (default: 30)")

    args = parser.parse_args()

    if args.batch and (args.jobs != 3 or args.rpm != 10):
        print("Note: -j/--rpm are ignored in --batch mode", file=sys.stderr)

    input_path = Path(args.input)
    image_files = collect_images(input_path)
    if not image_files:
        print(f"No image files found in {input_path}")
        sys.exit(1)

    if genai is None:
        print("Error: google-genai package required for enhancement. Install with: pip install google-genai", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_dir = args.output
    elif input_path.is_dir():
        output_dir = str(input_path / "enhanced")
    else:
        output_dir = str(input_path.parent / "enhanced")
    os.makedirs(output_dir, exist_ok=True)

    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: Set GEMINI_API_KEY env var or pass --api-key", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    mode = "batch" if args.batch else "real-time"

    print("=" * 60)
    print("ENHANCE")
    print("=" * 60)
    print(f"Input:      {input_path}")
    print(f"Output:     {output_dir}")
    print(f"Photos:     {len(image_files)}")
    print(f"Model:      {args.model}")
    print(f"Mode:       {mode}")
    if args.batch:
        print(f"Poll:       every {args.poll}s")
        print(f"Pricing:    50% off standard rate")
    else:
        print(f"Workers:    {args.jobs}")
        print(f"Rate limit: {args.rpm} RPM")
    print("=" * 60)
    print()

    start_time = time.monotonic()

    failed_paths = []
    if args.batch:
        success, failed, failed_paths = run_batch(client, image_files, output_dir, args.model, args.poll)
    else:
        success, failed = run_realtime(client, image_files, output_dir, args.model, args.jobs, args.rpm)

    # Auto-retry failed photos
    max_retry_rounds = 3
    retry_round = 0
    while failed_paths and retry_round < max_retry_rounds:
        retry_round += 1
        print()
        if len(failed_paths) > 5:
            print(f"Retry round {retry_round}: {len(failed_paths)} failed photo(s) via batch...")
            retry_success, retry_failed, failed_paths = run_batch(
                client, failed_paths, output_dir, args.model, args.poll,
            )
        else:
            print(f"Retry round {retry_round}: {len(failed_paths)} failed photo(s) via real-time...")
            retry_success, retry_failed = run_realtime(
                client, failed_paths, output_dir, args.model, jobs=1, rpm=args.rpm,
            )
            failed_paths = []  # real-time doesn't track failed paths
        success += retry_success
        failed -= retry_success

    elapsed = time.monotonic() - start_time
    elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))

    print()
    print(f"Done! Enhanced {success}/{len(image_files)} photo(s) in {elapsed_str}")
    print(f"Output: {output_dir}")
    if failed > 0:
        print(f"  ({failed} photo(s) failed)")


if __name__ == "__main__":
    main()
