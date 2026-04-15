# Autocrop

Extract individual photos from album page scans and enhance them using AI.

Got a stack of old photo albums? Snap a picture of each page, point this tool at the folder, and get back individual photos — cropped, rotated, dated, geotagged, colorized, and restored.

## How it works

```
Album page scan ──▶ ai_parse.py ──▶ autocrop_meta.json ──▶ editor.py ──▶ crop_exif.py ──▶ ai_enhance.py ──▶ upload_gphoto.py
                    (Vision AI)     (bbox + metadata)      (web editor)   (crop+EXIF)      (restore)        (Google Photos)
```

1. **`ai_parse.py`** sends each page to a Vision AI model (Gemini or GPT-4o) which returns bounding boxes, orientation, dates, locations, and captions — saved to `autocrop_meta.json`
2. **`editor.py`** serves a web-based editor to visually review and adjust bounding boxes, rotation, dates, locations (with map), and captions
3. **`crop_exif.py`** reads the metadata file and produces cropped photos with EXIF metadata
4. **`ai_enhance.py`** sends each cropped photo to Gemini which restores it: removes album corners, fixes scratches, improves contrast, and colorizes B&W photos
5. **`upload_gphoto.py`** uploads photos to Google Photos with descriptions via API

## Quick start

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="your-key"

# Step 1: Analyze pages and create metadata
python ai_parse.py ./album_pages/ -p gemini --default-location "New York"

# Step 2: Review and adjust in web editor
python editor.py ./album_pages/

# Step 3: Apply metadata to produce cropped photos (editor does this via Apply button, or run manually)
python crop_exif.py ./album_pages/

# Step 4: Enhance extracted photos
python ai_enhance.py ./album_pages/cropped/ -o ./enhanced/
```

Or use `--auto-apply` for the legacy one-shot workflow:

```bash
python ai_parse.py ./album_pages/ -p gemini --auto-apply
```

### Individual photos (no album pages)

If your photos are already scanned or photographed one by one, skip the AI detection step and go straight to the editor:

```bash
# Step 1: Review and adjust in web editor
python editor.py ./my_photos/

# Step 2: Enhance
python ai_enhance.py ./my_photos/cropped/ -o ./enhanced/
```

The editor will treat each image as a single full-page photo and pre-fill date, location, and caption from EXIF. You can fine-tune the crop, fix rotation, and edit metadata — then hit Apply. Originals stay untouched; results go to `cropped/`.

## `ai_parse.py` — Detect photos using AI

### Modes

**Default: create-metadata** — analyze pages and save results to `autocrop_meta.json` (no cropping):

```bash
python ai_parse.py ./album_pages/ -p gemini --default-location "Berlin"
```

**Auto-apply** — legacy one-shot mode (analyze + crop + save in one step):

```bash
python ai_parse.py ./album_pages/ -p gemini --auto-apply
```

### Examples

```bash
# Single page
python ai_parse.py page.jpg -p gemini

# Directory of pages, 4 in parallel
python ai_parse.py ./album_pages/ -p gemini -j 4

# With fallback location for geocoding
python ai_parse.py ./album_pages/ -p gemini --default-location "Berlin"

# Restrict geocoding to specific countries
python ai_parse.py ./album_pages/ -p gemini --default-location "Karaganda" --countries "kz,ru"
```

**Features:**
- Photo detection via Gemini or GPT-4o Vision API
- Reads handwritten dates, locations, and captions
- GPS geocoding via OpenStreetMap Nominatim
- Date inheritance: undated photos get the date from the nearest dated page
- Parallel page processing (`-j N`)

**Options:**

| Option | Description |
|--------|-------------|
| `--auto-apply` | Analyze + crop + save in one shot (legacy behavior) |
| `-o, --output DIR` | Output directory (default: `<input>/cropped`) |
| `-p, --provider` | `openai` or `gemini` (default: `openai`) |
| `-m, --model` | Vision model name |
| `--api-key` | API key (default: from env var) |
| `--default-location` | Fallback city/region for geocoding |
| `--countries` | Restrict geocoding to these countries (comma-separated [ISO codes](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2), e.g. `kz,ru,ua,us`) |
| `--no-location-spread` | Don't apply a recognized location from one photo to other photos on the same page |
| `-j, --jobs N` | Parallel pages (default: 4) |

## `crop_exif.py` — Crop photos and write EXIF

Reads `autocrop_meta.json` and produces cropped, rotated JPEG files with EXIF metadata (date, GPS, caption).

```bash
python crop_exif.py ./album_pages/
python crop_exif.py ./album_pages/ -o ./output/
```

No AI calls, no API key needed. Works with the metadata file created by `ai_parse.py` or `editor.py`.

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output DIR` | Output directory (default: `<input>/cropped`) |

## `editor.py` — Web-based metadata editor

Visual editor for reviewing and adjusting photo detection results before cropping.

```bash
python editor.py ./album_pages/
python editor.py ./album_pages/ -o ./output/ --port 8080
```

Works with or without `autocrop_meta.json` — if no metadata exists, each image is treated as a single full-page photo with date, location, and caption pre-filled from the source file's EXIF. GPS coordinates are automatically resolved to place names in the background via reverse geocoding.

When `autocrop_meta.json` exists but photos have empty fields, the editor also backfills date, location, and caption from the source image EXIF.

Opens a web app at `http://localhost:8080` with two modes accessible via header toggle:

**Cut photos** — adjust bounding boxes:
- Drag corners and edges to resize photo boundaries
- Drag inside a box to move it
- Double-click to add a new photo
- Delete key to remove selected photo (at least 1 photo per page required)
- First photo auto-selected on page load

**Edit photos** — edit photo details:
- Preview cropped and rotated photos
- Rotate photos left/right
- Edit date and caption (changing a value suggests applying it to other photos with the same old value)
- Select location on an interactive map (OpenStreetMap)
- Save Draft or Apply to produce final cropped photos
- Warns about unsaved changes on page close

Navigation: Google-style page selector in header, arrow keys for prev/next page.

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output DIR` | Output directory for cropped photos (default: `<input>/cropped`) |
| `--port N` | HTTP server port (default: 8080) |
| `--no-browser` | Don't open browser automatically |

## `ai_enhance.py` — Restore & colorize

Sends each photo to Gemini which restores it in one API call: removes album corner holders, fixes scratches and defects, improves contrast, and colorizes B&W photos.

```bash
# Single photo
python ai_enhance.py photo.jpg

# Directory, 3 parallel workers
python ai_enhance.py ./cropped/ -j 3 --rpm 10

# Batch mode (50% cheaper, slower)
python ai_enhance.py ./cropped/ --batch

# Pro model for higher quality
python ai_enhance.py ./cropped/ -m gemini-3-pro-image-preview
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

## `edit_meta.py` — Fix metadata interactively (CLI)

Terminal-based metadata editor for already-cropped photos.

```bash
python edit_meta.py ./cropped/
python edit_meta.py ./enhanced/ --countries "kz,ru"
```

The script displays a numbered table of all photos with their metadata, then accepts commands:

```
 #  File                              Date        Loc  Caption
 1  198905_page1_photo_01.jpg         1989:05       ✓  Walking with mom
 2  198905_page1_photo_02.jpg         1989:05       ✓  —
 3  page2_photo_01.jpg                —             —  Think...

Enter commands: <number><D|L|C> <value>  (empty line to apply)
> 3D 1989:06
> 3L Berlin, Germany
> 3L 49.5186, 72.8238
> 1C Walking in the park
>
```

| Command | Description |
|---------|-------------|
| `3D 1989:06` | Set date for photo #3 (also renames the file) |
| `3L Berlin, Germany` | Set location by name (geocodes to GPS coordinates) |
| `3L 49.5186, 72.8238` | Set location by coordinates (writes GPS directly, no geocoding) |
| `1C Walking in the park` | Set caption |

**Options:**

| Option | Description |
|--------|-------------|
| `--countries` | Restrict geocoding to these countries (comma-separated [ISO codes](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2)) |

## Cost estimate

All processing happens via API calls — no local GPU needed.

**ai_parse.py** (Gemini Flash):
- ~$0.0003 per page (~3K input tokens)
- 100-page album: **~$0.03**

**ai_enhance.py** (Gemini Flash Image):
- ~$0.04 per photo (image generation)
- With `--batch`: ~$0.02 per photo (50% off)
- 200 photos: **~$4-8**

## Rate limits (Gemini image generation)

| Tier | RPM | RPD |
|------|-----|-----|
| Free | 2 | 50 |
| Tier 1 (paid) | 10 | 500 |
| Tier 2 | 20 | 2,000 |

## Supported providers (ai_parse)

| Provider | Default Model | Env Variable |
|----------|--------------|-------------|
| OpenAI | `gpt-4o` | `OPENAI_API_KEY` |
| Gemini | `gemini-3-flash-preview` | `GEMINI_API_KEY` |

Any model name can be passed via `-m`.

## `upload_gphoto.py` — Upload to Google Photos

Google Photos ignores XMP `dc:description` from EXIF when importing files directly — captions end up in the "Other" field instead of the description. Uploading via Google Photos API solves this: descriptions are set properly and visible under the photo.

> **Note:** Apple Photos reads XMP descriptions correctly, so this script is only needed for Google Photos.

```bash
python upload_gphoto.py ./cropped/
python upload_gphoto.py ./cropped/ --album "Family Album"
python upload_gphoto.py ./cropped/ --dry-run
```

**Options:**

| Option | Description |
|--------|-------------|
| `--album NAME` | Create or find album and add photos to it |
| `--credentials PATH` | Path to `credentials.json` (default: `./credentials.json`) |
| `--token PATH` | Path to `token.json` (default: `./token.json`) |
| `--dry-run` | List files and descriptions without uploading |

### Google Cloud setup

The script uses Google Photos Library API with OAuth 2.0. One-time setup:

1. **Create project**: go to [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate), name it anything, click Create (no billing required)
2. **Enable API**: go to [Photos Library API](https://console.cloud.google.com/apis/library/photoslibrary.googleapis.com), click Enable
3. **OAuth consent screen**: go to [consent screen settings](https://console.cloud.google.com/apis/credentials/consent):
   - User type: **External**, click Create
   - Fill in app name, support email, developer email
   - Scopes: add `https://www.googleapis.com/auth/photoslibrary`
   - Test users: add your Gmail address
   - Leave the app in **Testing** status (do not publish)
4. **Create credentials**: go to [credentials page](https://console.cloud.google.com/apis/credentials):
   - Click **+ Create Credentials** → **OAuth client ID**
   - Application type: **Desktop app**
   - Click Create, then **Download JSON**
   - Rename the file to `credentials.json` and place it in the project directory

On first run, the script opens a browser for Google login. You'll see "Google hasn't verified this app" — click **Advanced** → **Go to \<app name\> (unsafe)**. After authorization, the token is saved to `token.json` and subsequent runs are automatic.

## Installation

```bash
pip install -r requirements.txt
```

Requires Python 3.9+. No GPU needed.

## License

MIT
