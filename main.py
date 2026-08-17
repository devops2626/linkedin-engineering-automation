#!/usr/bin/env python3
"""
LinkedIn Engineering Auto-Poster

Reads the next pending post from posts/, publishes it via the official
LinkedIn Posts API (text-only or native document), then moves the file(s)
to archived/.

Document support:
  Place a document next to the text file with the same stem, e.g.:
    posts/post-01-architecture.txt
    posts/post-01-architecture.pdf
  Supported extensions: .pdf .ppt .pptx .doc .docx
"""

from __future__ import annotations

import os
import sys
import time
import random
import logging
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Logging configuration (console + rotating file)
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
LOG_FILE = LOG_DIR / "auto_poster.log"

LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", 5 * 1024 * 1024))  # 5 MB
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", 5))

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> logging.Logger:
    """Configure logger with console + rotating file handlers."""
    log = logging.getLogger("linkedin.auto_poster")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    if log.handlers:
        return log

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
        log.debug(
            "Rotating file handler enabled → %s (maxBytes=%d, backupCount=%d)",
            LOG_FILE,
            LOG_MAX_BYTES,
            LOG_BACKUP_COUNT,
        )
    except OSError as exc:
        log.warning("Could not create rotating file handler: %s. Using console only.", exc)

    return log


logger = setup_logging()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
PERSON_ID = os.environ.get("LINKEDIN_PERSON_ID")  # raw id or full urn:li:person:...

LINKEDIN_VERSION = "202607"

DOCUMENT_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".doc", ".docx"}

# Polling defaults (can be overridden via env)
POLL_TIMEOUT = float(os.environ.get("DOCUMENT_POLL_TIMEOUT", 180))
POLL_INITIAL_INTERVAL = float(os.environ.get("DOCUMENT_POLL_INITIAL", 1.0))
POLL_MAX_INTERVAL = float(os.environ.get("DOCUMENT_POLL_MAX", 12.0))
POLL_MULTIPLIER = float(os.environ.get("DOCUMENT_POLL_MULTIPLIER", 2.0))
POLL_JITTER = float(os.environ.get("DOCUMENT_POLL_JITTER", 0.25))


def _api_headers(content_type: str | None = "application/json") -> dict:
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": LINKEDIN_VERSION,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def normalize_author(person_id: str) -> str:
    """Ensure author is a full Person URN."""
    if person_id.startswith("urn:li:person:"):
        return person_id
    return f"urn:li:person:{person_id}"


def find_companion_document(post_path: Path) -> Optional[Path]:
    """
    Look for a document file that shares the same stem as the post text file.

    Example:
        posts/post-01-architecture.txt  →  posts/post-01-architecture.pdf
    """
    for ext in DOCUMENT_EXTENSIONS:
        candidate = post_path.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


def get_next_post() -> tuple[Optional[Path], Optional[str], Optional[Path]]:
    """
    Return (text_path, commentary, document_path).

    document_path is None for pure text posts.
    """
    posts_dir = Path("posts")
    if not posts_dir.is_dir():
        logger.warning("posts/ directory not found.")
        return None, None, None

    candidates = sorted(
        list(posts_dir.glob("*.txt")) + list(posts_dir.glob("*.md"))
    )
    if not candidates:
        logger.info("No pending posts found in posts/.")
        return None, None, None

    post_path = candidates[0]
    content = post_path.read_text(encoding="utf-8").strip()
    if not content:
        logger.warning("Post file is empty: %s", post_path)
        return None, None, None

    document_path = find_companion_document(post_path)
    if document_path:
        logger.info("Found companion document: %s", document_path.name)
    else:
        logger.debug("No companion document for %s (text-only post)", post_path.name)

    logger.debug("Selected post file: %s (%d chars)", post_path.name, len(content))
    return post_path, content, document_path


# ---------------------------------------------------------------------------
# Document upload lifecycle
# ---------------------------------------------------------------------------

def initialize_document_upload(owner: str) -> dict:
    """Step 1 – register the upload and obtain uploadUrl + document URN."""
    url = "https://api.linkedin.com/rest/documents?action=initializeUpload"
    payload = {"initializeUploadRequest": {"owner": owner}}

    logger.info("Initializing document upload (owner=%s)", owner)
    resp = requests.post(url, json=payload, headers=_api_headers(), timeout=30)
    if resp.status_code not in (200, 201):
        logger.error("initializeUpload failed: %s – %s", resp.status_code, resp.text)
        raise RuntimeError(f"initializeUpload failed: {resp.status_code}")

    value = resp.json()["value"]
    logger.info("Document URN: %s", value["document"])
    return value


def upload_document_binary(upload_url: str, file_path: Path) -> None:
    """Step 2 – PUT the binary file to the temporary upload URL."""
    logger.info("Uploading binary: %s (%d bytes)", file_path.name, file_path.stat().st_size)

    with open(file_path, "rb") as fh:
        # LinkedIn expects a raw binary upload; no Content-Type required
        resp = requests.put(
            upload_url,
            data=fh,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            timeout=120,
        )

    if resp.status_code not in (200, 201):
        logger.error("Binary upload failed: %s – %s", resp.status_code, resp.text)
        raise RuntimeError(f"Binary upload failed: {resp.status_code}")

    logger.info("Binary upload complete")


def get_document_status(document_urn: str) -> str:
    """Fetch current processing status of a document asset."""
    encoded = requests.utils.quote(document_urn, safe="")
    url = f"https://api.linkedin.com/rest/documents/{encoded}"
    resp = requests.get(url, headers=_api_headers(content_type=None), timeout=30)
    resp.raise_for_status()
    return resp.json().get("status", "UNKNOWN")


def wait_until_available(
    document_urn: str,
    *,
    timeout: float = POLL_TIMEOUT,
    initial_interval: float = POLL_INITIAL_INTERVAL,
    max_interval: float = POLL_MAX_INTERVAL,
    multiplier: float = POLL_MULTIPLIER,
    jitter: float = POLL_JITTER,
) -> str:
    """
    Poll document status with exponential backoff + jitter until AVAILABLE.

    Raises TimeoutError or RuntimeError on failure.
    """
    deadline = time.monotonic() + timeout
    interval = initial_interval
    last_status: Optional[str] = None
    attempt = 0

    logger.info(
        "Polling document status (timeout=%.0fs, initial=%.1fs, max=%.1fs)",
        timeout,
        initial_interval,
        max_interval,
    )

    while True:
        attempt += 1
        now = time.monotonic()

        if now >= deadline:
            msg = (
                f"Document {document_urn} did not become AVAILABLE "
                f"within {timeout}s (last status: {last_status}, attempts: {attempt})"
            )
            logger.error(msg)
            raise TimeoutError(msg)

        status = get_document_status(document_urn)

        if status != last_status:
            logger.info("[%d] Document status → %s", attempt, status)
            last_status = status
        else:
            logger.debug("[%d] Still %s …", attempt, status)

        if status == "AVAILABLE":
            logger.info("Document is AVAILABLE after %d attempt(s)", attempt)
            return status

        if status == "PROCESSING_FAILED":
            msg = f"Document processing failed for {document_urn}"
            logger.error(msg)
            raise RuntimeError(msg)

        remaining = deadline - time.monotonic()
        sleep_time = min(interval, remaining, max_interval)
        jitter_factor = 1.0 + random.uniform(-jitter, jitter)
        sleep_time = max(0.1, sleep_time * jitter_factor)

        logger.debug("Sleeping %.2fs before next poll", sleep_time)
        time.sleep(sleep_time)

        interval = min(interval * multiplier, max_interval)


def upload_and_prepare_document(document_path: Path, owner: str) -> str:
    """
    Full document lifecycle:
      1. initializeUpload
      2. binary PUT
      3. poll until AVAILABLE

    Returns the document URN ready for use in a post.
    """
    init = initialize_document_upload(owner)
    upload_url = init["uploadUrl"]
    document_urn = init["document"]

    upload_document_binary(upload_url, document_path)
    wait_until_available(document_urn)

    return document_urn


# ---------------------------------------------------------------------------
# Post creation
# ---------------------------------------------------------------------------

def post_to_linkedin(
    text: str,
    *,
    document_urn: Optional[str] = None,
    document_title: Optional[str] = None,
) -> None:
    """
    Publish a post (text-only or with a native document).

    When document_urn is supplied the post becomes a native document post.
    """
    if not ACCESS_TOKEN or not PERSON_ID:
        raise RuntimeError(
            "Missing LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_ID environment variables."
        )

    author = normalize_author(PERSON_ID)

    payload: dict = {
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

    if document_urn:
        payload["content"] = {
            "media": {
                "id": document_urn,
                "title": document_title or "Document",
            }
        }
        logger.info("Creating native document post (urn=%s, title=%s)", document_urn, document_title)
    else:
        logger.info("Creating text-only post (author=%s)", author)

    response = requests.post(
        "https://api.linkedin.com/rest/posts",
        json=payload,
        headers=_api_headers(),
        timeout=30,
    )

    if response.status_code == 201:
        post_id = response.headers.get("x-restli-id", "unknown")
        logger.info("Successfully posted to LinkedIn! Post ID: %s", post_id)
    else:
        logger.error("Failed to post: %s – %s", response.status_code, response.text)
        raise RuntimeError(f"LinkedIn API error {response.status_code}")


def archive_files(*paths: Path) -> None:
    """Move one or more files into the archived/ directory."""
    archived_dir = Path("archived")
    archived_dir.mkdir(exist_ok=True)

    for path in paths:
        if path is None or not path.exists():
            continue

        destination = archived_dir / path.name
        if destination.exists():
            stem = path.stem
            suffix = path.suffix
            counter = 1
            while destination.exists():
                destination = archived_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        shutil.move(str(path), str(destination))
        logger.info("Archived %s → %s", path, destination)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logger.info("LinkedIn Engineering Auto-Poster starting")

    text_path, commentary, document_path = get_next_post()
    if not commentary or not text_path:
        logger.info("Nothing to post. Exiting cleanly.")
        return 0

    logger.info("Posting: %s%s", text_path.name, f" + {document_path.name}" if document_path else "")

    document_urn: Optional[str] = None
    document_title: Optional[str] = None

    if document_path:
        owner = normalize_author(PERSON_ID)  # type: ignore[arg-type]
        document_urn = upload_and_prepare_document(document_path, owner)
        document_title = document_path.name

    post_to_linkedin(
        commentary,
        document_urn=document_urn,
        document_title=document_title,
    )

    # Archive both the text file and the document (if any)
    archive_files(text_path, document_path)

    logger.info("Run completed successfully")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Unhandled error during execution")
        sys.exit(1)
