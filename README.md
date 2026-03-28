# Autocrop

Extract individual photos from album page scans and enhance them using AI.

Got a stack of old photo albums? Snap a picture of each page, point this tool at the folder, and get back individual photos — cropped, rotated, dated, geotagged, colorized, and restored.

## How it works

```
Album page scan ──▶ autocrop.py ──▶ Individual photos ──▶ enhance.py ──▶ Restored photos
                    (Vision AI)     with EXIF metadata     (Gemini)      colorized & clean
```

1. **`autocrop.py`** sends each page to a Vision AI model (Gemini or GPT-4o) which returns bounding boxes, orientation, dates, locations, and captions
2. The script crops each photo, rotates it upright, geocodes locations to GPS coordinates, and writes EXIF metadata
3. **`enhance.py`** sends each cropped photo to Gemini's image generation model which restores it in one shot: removes album corners, fixes scratches, improves contrast, and colorizes B&W photos

## Quick start

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="your-key"

# Step 1: Extract photos from album pages
python autocrop.py ./album_pages/ -p gemini --default-location "New York"

# Step 2: Enhance extracted photos
python enhance.py ./album_pages/cropped/ -o ./enhanced/
```

## `autocrop.py` — Detect & crop

```bash
# Single page
python autocrop.py page.jpg -p gemini

# Directory of pages, 4 in parallel
python autocrop.py ./album_pages/ -p gemini -j 4

# With double-pass verification (recommended, 2-3x API cost)
python autocrop.py ./album_pages/ -p gemini --verify

# With fallback location for geocoding
python autocrop.py ./album_pages/ -p gemini --default-location "Berlin"
```

**Features:**
- Photo detection via Gemini or GPT-4o Vision API
- Reads handwritten dates, locations, and captions
- GPS geocoding via OpenStreetMap Nominatim
- EXIF metadata (date, GPS, caption) — works with Google Photos, Apple Photos, etc.
- Double-pass verification (`--verify`) to catch crop/rotation errors
- Date inheritance: undated photos get the date from the nearest dated page
- Parallel page processing (`-j N`)

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output DIR` | Output directory (default: `<input>/cropped`) |
| `-p, --provider` | `openai` or `gemini` (default: `openai`) |
| `-m, --model` | Vision model name |
| `--api-key` | API key (default: from env var) |
| `--default-location` | Fallback city/region for geocoding |
| `-j, --jobs N` | Parallel pages (default: 4) |
| `--verify` | Double-pass verification with arbitration (2-3x API cost) |

## `enhance.py` — Restore & colorize

Sends each photo to Gemini which restores it in one API call: removes album corner holders, fixes scratches and defects, improves contrast, and colorizes B&W photos.

```bash
# Single photo
python enhance.py photo.jpg

# Directory, 3 parallel workers
python enhance.py ./cropped/ -j 3 --rpm 10

# Batch mode (50% cheaper, slower)
python enhance.py ./cropped/ --batch

# Pro model for higher quality
python enhance.py ./cropped/ -m gemini-3-pro-image-preview
```

**Two modes:**
- **Real-time** (default): parallel workers with rate limiting, results immediately
- **Batch** (`--batch`): submits all photos as one job, 50% cheaper, waits for completion. Failed photos are automatically retried.

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output DIR` | Output directory (default: `<input>/enhanced`) |
| `-m, --model` | Gemini image model (default: `gemini-3.1-flash-image-preview`) |
| `--api-key` | Gemini API key (default: `GEMINI_API_KEY` env var) |
| `-j, --jobs N` | Parallel workers, real-time mode only (default: 3) |
| `--rpm N` | Max requests per minute, real-time mode only (default: 10) |
| `--batch` | Use Batch API (50% cheaper). `-j` and `--rpm` are ignored |
| `--poll N` | Batch poll interval in seconds (default: 30) |

## Cost estimate

All processing happens via API calls — no local GPU needed.

**autocrop.py** (Gemini Flash):
- ~$0.0003 per page (~3K input tokens)
- With `--verify`: ~$0.0006-0.0009 per page
- 100-page album: **~$0.03-0.09**

**enhance.py** (Gemini Flash Image):
- ~$0.04 per photo (image generation)
- With `--batch`: ~$0.02 per photo (50% off)
- 200 photos: **~$4-8**

## Rate limits (Gemini image generation)

| Tier | RPM | RPD |
|------|-----|-----|
| Free | 2 | 50 |
| Tier 1 (paid) | 10 | 500 |
| Tier 2 | 20 | 2,000 |

## Supported providers (autocrop)

| Provider | Default Model | Env Variable |
|----------|--------------|-------------|
| OpenAI | `gpt-4o` | `OPENAI_API_KEY` |
| Gemini | `gemini-2.5-flash-preview-05-20` | `GEMINI_API_KEY` |

Any model name can be passed via `-m`.

## Installation

```bash
pip install -r requirements.txt
```

Requires Python 3.10+. No GPU needed.

## License

MIT
