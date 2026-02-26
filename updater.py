#!/usr/bin/env python3
"""
PaperRouter Auto-Updater

Checks for new releases on GitHub and applies updates without requiring git.

Usage:
    python updater.py                  # Check for updates
    python updater.py --check-only     # Check only (no prompt)
    python updater.py --apply          # Download and apply latest update
    python updater.py --json           # Machine-readable output

The web GUI calls this via /api/update/check and /api/update/apply endpoints.
"""

import sys
import os
import json
import shutil
import tempfile
import zipfile
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
VERSION_FILE = SCRIPT_DIR / "VERSION"
GITHUB_REPO = "OtherLeadingBrand/PaperRouter"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Directories and files to preserve during update (relative to project root).
# Everything else is overwritten with the new release contents.
PRESERVE = {
    ".venv",
    ".git",
    ".gitignore",
    ".harness.pid",
    ".claude",
    "__pycache__",
    "downloads",
    "download_metadata.json",
}


def get_local_version():
    """Read the local VERSION file and return its contents."""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "0.0.0"


def parse_version(v):
    """Parse a version string like '0.2.0-alpha' or 'v0.3.0' into a comparable tuple.

    Sorting rules:
        - Numeric parts compared left to right (0.3.0 > 0.2.0)
        - A pre-release tag (e.g. '-alpha') sorts BEFORE the same version without one
          (0.2.0-alpha < 0.2.0)
    """
    v = v.lstrip("v").strip()
    parts = v.split("-", 1)
    main = parts[0]
    pre = parts[1] if len(parts) > 1 else None

    nums = []
    for segment in main.split("."):
        try:
            nums.append(int(segment))
        except ValueError:
            nums.append(0)
    # Pad to at least 3 components
    while len(nums) < 3:
        nums.append(0)

    # Pre-release sorts before release: (0,2,0, False, 'alpha') < (0,2,0, True, '')
    return tuple(nums) + (pre is None, pre or "")


def check_for_update():
    """Check GitHub for a newer release.

    Returns a dict with update info if one is available, or None if up-to-date.
    Returns None silently on any network/API error (offline-safe).
    """
    try:
        import requests

        resp = requests.get(GITHUB_API_URL, timeout=5, headers={
            "Accept": "application/vnd.github.v3+json",
        })
        if resp.status_code == 404:
            return None  # No releases published yet
        resp.raise_for_status()
        data = resp.json()

        remote_tag = data.get("tag_name", "")
        local_ver = get_local_version()

        if parse_version(remote_tag) > parse_version(local_ver):
            return {
                "update_available": True,
                "current": local_ver,
                "latest": remote_tag,
                "name": data.get("name", remote_tag),
                "notes": data.get("body", ""),
                "url": data.get("html_url", ""),
                "zipball_url": data.get("zipball_url", ""),
                "published_at": data.get("published_at", ""),
            }
        return None
    except Exception:
        return None


def apply_update(zipball_url=None):
    """Download and apply an update from GitHub.

    Returns (success: bool, message: str).
    """
    import requests

    if not zipball_url:
        info = check_for_update()
        if not info:
            return False, "Already up to date."
        zipball_url = info["zipball_url"]

    # Download the release zip
    print("Downloading update...")
    resp = requests.get(zipball_url, timeout=120, stream=True)
    resp.raise_for_status()

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "update.zip"
        total = 0
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                total += len(chunk)
        print(f"Downloaded {total / 1024:.0f} KB")

        # Extract
        print("Extracting...")
        extract_dir = Path(tmpdir) / "extracted"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        # GitHub wraps release zips in a top-level dir like "Owner-Repo-sha/"
        contents = list(extract_dir.iterdir())
        if len(contents) == 1 and contents[0].is_dir():
            source_dir = contents[0]
        else:
            source_dir = extract_dir

        # Copy new files over, preserving user data
        updated_files = 0
        print("Applying update...")
        for item in source_dir.iterdir():
            if item.name in PRESERVE:
                continue
            dest = SCRIPT_DIR / item.name
            try:
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
                updated_files += 1
            except PermissionError as e:
                print(f"  Warning: Could not update {item.name}: {e}")

    new_ver = get_local_version()
    print(f"Updated {updated_files} items. Now at version {new_ver}.")
    return True, new_ver


def main():
    import argparse

    parser = argparse.ArgumentParser(description="PaperRouter Auto-Updater")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check for updates and print result (no prompt to apply)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Download and apply the latest update",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    args = parser.parse_args()

    if args.apply:
        ok, msg = apply_update()
        if args.json:
            print(json.dumps({"ok": ok, "message": msg}))
        else:
            if ok:
                print(f"Updated to {msg}")
                print("Please restart PaperRouter to use the new version.")
            else:
                print(msg)
        sys.exit(0 if ok else 1)

    # Default: check for updates
    info = check_for_update()

    if args.json:
        print(json.dumps(info or {"update_available": False}))
    else:
        if info:
            print(f"Update available: {info['current']} -> {info['latest']}")
            if info.get("name") and info["name"] != info["latest"]:
                print(f"  Release: {info['name']}")
            if info.get("notes"):
                # Show first 3 lines of release notes
                lines = info["notes"].strip().splitlines()[:3]
                for line in lines:
                    print(f"  {line}")
            if not args.check_only:
                print("\nRun 'python updater.py --apply' to update.")
                print("Or update from the web GUI Settings panel.")
        else:
            local = get_local_version()
            print(f"PaperRouter v{local} is up to date.")


if __name__ == "__main__":
    main()
