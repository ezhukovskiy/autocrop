#!/usr/bin/env python3
"""Upload photos to Google Photos with descriptions from EXIF."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import piexif
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SCOPES = ["https://www.googleapis.com/auth/photoslibrary"]
API_BASE = "https://photoslibrary.googleapis.com/v1"


def authenticate(credentials_path: str, token_path: str) -> Credentials:
    """Authenticate with Google Photos API via OAuth 2.0."""
    creds = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if not os.path.exists(credentials_path):
            print(f"Error: {credentials_path} not found.", file=sys.stderr)
            print("Download OAuth credentials from Google Cloud Console.", file=sys.stderr)
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(token_path, "w") as f:
        f.write(creds.to_json())

    return creds


def read_description(image_path: str) -> str | None:
    """Read caption/description from EXIF ImageDescription tag."""
    try:
        exif_dict = piexif.load(image_path)
        desc = exif_dict.get("0th", {}).get(piexif.ImageIFD.ImageDescription)
        if desc:
            text = desc.decode("utf-8") if isinstance(desc, bytes) else str(desc)
            if text.strip():
                return text.strip()
    except Exception:
        pass
    return None


def get_session(creds: Credentials) -> requests.Session:
    """Create an authorized requests session."""
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {creds.token}"})
    return session


def upload_bytes(session: requests.Session, filepath: Path) -> str:
    """Upload raw image bytes to Google Photos. Returns upload token."""
    with open(filepath, "rb") as f:
        data = f.read()

    resp = session.post(
        f"{API_BASE}/uploads",
        data=data,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-File-Name": filepath.name,
            "X-Goog-Upload-Protocol": "raw",
        },
    )
    resp.raise_for_status()
    return resp.text  # upload token


def create_media_item(
    session: requests.Session,
    upload_token: str,
    description: str | None = None,
    album_id: str | None = None,
) -> dict:
    """Create a media item from an upload token."""
    new_item = {"simpleMediaItem": {"uploadToken": upload_token}}
    if description:
        new_item["description"] = description

    body = {"newMediaItems": [new_item]}
    if album_id:
        body["albumId"] = album_id

    resp = session.post(f"{API_BASE}/mediaItems:batchCreate", json=body)
    resp.raise_for_status()
    result = resp.json()

    items = result.get("newMediaItemResults", [])
    if items and items[0].get("status", {}).get("message") == "Success":
        return items[0].get("mediaItem", {})
    elif items:
        raise RuntimeError(f"Upload failed: {items[0].get('status', {})}")
    else:
        raise RuntimeError(f"Unexpected response: {result}")


def get_or_create_album(session: requests.Session, title: str) -> str:
    """Find an existing album by title or create a new one. Returns album ID."""
    # Search existing albums
    next_page = None
    while True:
        params = {"pageSize": 50}
        if next_page:
            params["pageToken"] = next_page
        resp = session.get(f"{API_BASE}/albums", params=params)
        resp.raise_for_status()
        data = resp.json()

        for album in data.get("albums", []):
            if album.get("title") == title:
                print(f"  Found existing album: {title}")
                return album["id"]

        next_page = data.get("nextPageToken")
        if not next_page:
            break

    # Create new album
    resp = session.post(f"{API_BASE}/albums", json={"album": {"title": title}})
    resp.raise_for_status()
    album = resp.json()
    print(f"  Created album: {title}")
    return album["id"]


def main():
    parser = argparse.ArgumentParser(description="Upload photos to Google Photos with descriptions")
    parser.add_argument("input", help="Directory with photos to upload")
    parser.add_argument("--album", help="Album name (create if doesn't exist)")
    parser.add_argument("--credentials", default="credentials.json", help="Path to credentials.json")
    parser.add_argument("--token", default="token.json", help="Path to token.json")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    if not input_dir.is_dir():
        print(f"Error: {input_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Collect images
    images = sorted(
        f for f in input_dir.iterdir()
        if f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    )

    if not images:
        print("No images found.")
        return

    print(f"Found {len(images)} image(s) in {input_dir}\n")

    # Dry run — just list files and descriptions
    if args.dry_run:
        for img in images:
            desc = read_description(str(img))
            desc_str = f'"{desc}"' if desc else "—"
            print(f"  {img.name}  →  {desc_str}")
        return

    # Authenticate
    print("Authenticating...")
    creds = authenticate(args.credentials, args.token)
    session = get_session(creds)
    print("  OK\n")

    # Album
    album_id = None
    if args.album:
        album_id = get_or_create_album(session, args.album)
        print()

    # Upload
    uploaded = 0
    failed = 0
    for i, img in enumerate(images):
        desc = read_description(str(img))
        desc_str = f'"{desc}"' if desc else "no description"
        print(f"[{i+1}/{len(images)}] {img.name} ({desc_str})...", end=" ", flush=True)

        try:
            # Refresh token if needed
            if creds.expired:
                creds.refresh(Request())
                session.headers.update({"Authorization": f"Bearer {creds.token}"})

            token = upload_bytes(session, img)
            create_media_item(session, token, desc, album_id)
            print("✓")
            uploaded += 1
        except Exception as e:
            print(f"✗ {e}")
            failed += 1

        # Rate limiting
        if i < len(images) - 1:
            time.sleep(1)

    print(f"\nDone: {uploaded} uploaded, {failed} failed.")


if __name__ == "__main__":
    main()
