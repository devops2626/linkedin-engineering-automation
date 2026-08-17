#!/usr/bin/env python3
"""
LinkedIn Engineering Auto-Poster
Reads the next pending post from posts/, publishes it via the official
LinkedIn Posts API, then moves the file to archived/.
"""

import os
import sys
import glob
import shutil
from pathlib import Path

import requests

ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
PERSON_ID = os.environ.get("LINKEDIN_PERSON_ID")  # raw id or full urn:li:person:...

# LinkedIn API version (YYYYMM). Update as new versions are released.
LINKEDIN_VERSION = "202607"


def normalize_author(person_id: str) -> str:
    """Ensure author is a full Person URN."""
    if person_id.startswith("urn:li:person:"):
        return person_id
    return f"urn:li:person:{person_id}"


def get_next_post() -> tuple[Path | None, str | None]:
    """Return the path and content of the next pending post (lexicographic order)."""
    posts_dir = Path("posts")
    if not posts_dir.is_dir():
        print("posts/ directory not found.")
        return None, None

    # Only consider .txt and .md files that are not already archived
    candidates = sorted(
        list(posts_dir.glob("*.txt")) + list(posts_dir.glob("*.md"))
    )
    if not candidates:
        print("No pending posts found in posts/.")
        return None, None

    post_path = candidates[0]
    content = post_path.read_text(encoding="utf-8").strip()
    if not content:
        print(f"Post file is empty: {post_path}")
        return None, None

    return post_path, content


def post_to_linkedin(text: str) -> None:
    """Publish a text post to the authenticated member's feed."""
    if not ACCESS_TOKEN or not PERSON_ID:
        raise RuntimeError(
            "Missing LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_ID environment variables."
        )

    url = "https://api.linkedin.com/rest/posts"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": LINKEDIN_VERSION,
    }

    author = normalize_author(PERSON_ID)

    payload = {
        "author": author,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)

    if response.status_code == 201:
        post_id = response.headers.get("x-restli-id", "unknown")
        print(f"Successfully posted to LinkedIn! Post ID: {post_id}")
    else:
        print(f"Failed to post: {response.status_code}")
        print(response.text)
        raise RuntimeError(f"LinkedIn API error {response.status_code}")


def archive_post(post_path: Path) -> None:
    """Move the posted file into the archived/ directory."""
    archived_dir = Path("archived")
    archived_dir.mkdir(exist_ok=True)

    destination = archived_dir / post_path.name
    # Avoid overwriting if a file with the same name already exists
    if destination.exists():
        stem = post_path.stem
        suffix = post_path.suffix
        counter = 1
        while destination.exists():
            destination = archived_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    shutil.move(str(post_path), str(destination))
    print(f"Archived {post_path} → {destination}")


def main() -> int:
    path, content = get_next_post()
    if not content or not path:
        print("Nothing to post. Exiting cleanly.")
        return 0

    print(f"Posting: {path.name}")
    post_to_linkedin(content)
    archive_post(path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
