#!/usr/bin/env python3
"""
LinkedIn Engineering Auto-Poster

Supports:
  - Text-only posts
  - Native document posts (PDF/PPT/DOC companion files)
  - First-comment support (.comment companion files)

Content layout examples:
  posts/post-01.txt
  posts/post-02.txt + posts/post-02.pdf
  posts/post-03.txt + posts/post-03.comment
  posts/post-04.txt + posts/post-04.pdf + posts/post-04.comment
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
from urllib.parse import quote

import requests
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    HTTPError,
    RequestException,
    Timeout as RequestsTimeout,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
LOG_FILE = LOG_DIR / "auto_poster.log"
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", 5 * 1024 * 1024))
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", 5))
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> logging.Logger:
    log = logging.getLogger("linkedin.auto_poster")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    if log.handlers:
        return log
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    log.addHandler(ch)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        log.addHandler(fh)
    except OSError as exc:
        log.warning("Rotating file handler unavailable: %s", exc)
    return log


logger = setup_logging()

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------
class LinkedInError(Exception):
    """Base class for all expected, handled failures."""


class ConfigError(LinkedInError):
    """Missing or invalid configuration / credentials."""


class DocumentValidationError(LinkedInError):
    """Local document file is missing, empty, too large, or unreadable."""


class UploadInitError(LinkedInError):
    """initializeUpload step failed (auth, network, or API error)."""


class BinaryUploadError(LinkedInError):
    """Binary PUT to the temporary upload URL failed."""


class DocumentProcessingError(LinkedInError):
    """Document never reached AVAILABLE (timeout or PROCESSING_FAILED)."""


class PostCreationError(LinkedInError):
    """Final /rest/posts call failed."""


class RateLimitError(LinkedInError):
    """LinkedIn returned 429 Too Many Requests."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
PERSON_ID = os.environ.get("LINKEDIN_PERSON_ID")
LINKEDIN_VERSION = "202607"
DOCUMENT_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".doc", ".docx"}
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024  # LinkedIn hard limit

POLL_TIMEOUT = float(os.environ.get("DOCUMENT_POLL_TIMEOUT", 180))
POLL_INITIAL = float(os.environ.get("DOCUMENT_POLL_INITIAL", 1.0))
POLL_MAX = float(os.environ.get("DOCUMENT_POLL_MAX", 12.0))
POLL_MULTIPLIER = float(os.environ.get("DOCUMENT_POLL_MULTIPLIER", 2.0))
POLL_JITTER = float(os.environ.get("DOCUMENT_POLL_JITTER", 0.25))

# Retry settings for transient network / 5xx / 429 failures
UPLOAD_MAX_RETRIES = int(os.environ.get("UPLOAD_MAX_RETRIES", 3))
UPLOAD_RETRY_BACKOFF = float(os.environ.get("UPLOAD_RETRY_BACKOFF", 2.0))


def api_headers(content_type: Optional[str] = "application/json") -> dict:
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": LINKEDIN_VERSION,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def normalize_author(person_id: str) -> str:
    if person_id.startswith("urn:li:person:"):
        return person_id
    return f"urn:li:person:{person_id}"


def require_credentials() -> None:
    missing = [
        name
        for name, value in [
            ("LINKEDIN_ACCESS_TOKEN", ACCESS_TOKEN),
            ("LINKEDIN_PERSON_ID", PERSON_ID),
        ]
        if not value
    ]
    if missing:
        raise ConfigError(f"Missing required environment variable(s): {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Content discovery
# ---------------------------------------------------------------------------
def find_companion_document(post_path: Path) -> Optional[Path]:
    for ext in DOCUMENT_EXTENSIONS:
        candidate = post_path.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


def find_companion_comment(post_path: Path) -> Optional[Path]:
    candidate = post_path.with_suffix(".comment")
    return candidate if candidate.is_file() else None


def get_next_post() -> tuple[Optional[Path], Optional[str], Optional[Path], Optional[Path]]:
    """Return (text_path, commentary, document_path, comment_path)."""
    posts_dir = Path("posts")
    if not posts_dir.is_dir():
        logger.warning("posts/ directory not found.")
        return None, None, None, None

    candidates = sorted(list(posts_dir.glob("*.txt")) + list(posts_dir.glob("*.md")))
    if not candidates:
        logger.info("No pending posts found in posts/.")
        return None, None, None, None

    post_path = candidates[0]
    content = post_path.read_text(encoding="utf-8").strip()
    if not content:
        logger.warning("Post file is empty: %s", post_path)
        return None, None, None, None

    document_path = find_companion_document(post_path)
    comment_path = find_companion_comment(post_path)
    if document_path:
        logger.info("Found companion document: %s", document_path.name)
    if comment_path:
        logger.info("Found companion first-comment: %s", comment_path.name)

    return post_path, content, document_path, comment_path


def validate_document(document_path: Path) -> None:
    """Fail fast on local problems before any network call."""
    if not document_path.is_file():
        raise DocumentValidationError(f"Document not found: {document_path}")

    if document_path.suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise DocumentValidationError(
            f"Unsupported document type '{document_path.suffix}'. "
            f"Allowed: {', '.join(sorted(DOCUMENT_EXTENSIONS))}"
        )

    try:
        size = document_path.stat().st_size
    except OSError as exc:
        raise DocumentValidationError(f"Cannot stat document: {exc}") from exc

    if size == 0:
        raise DocumentValidationError(f"Document is empty: {document_path}")

    if size > MAX_DOCUMENT_BYTES:
        raise DocumentValidationError(
            f"Document exceeds LinkedIn 100 MB limit: {document_path} "
            f"({size / (1024 * 1024):.1f} MB)"
        )

    try:
        with open(document_path, "rb") as fh:
            fh.read(64)
    except OSError as exc:
        raise DocumentValidationError(f"Cannot read document: {exc}") from exp

    logger.debug("Document validated: %s (%.1f MB)", document_path.name, size / (1024 * 1024))


# ---------------------------------------------------------------------------
# HTTP helper with retry for transient failures
# ---------------------------------------------------------------------------
def _is_retryable_status(status_code: int) -> bool:
    """5xx and 429 are worth retrying; most 4xx are permanent."""
    return status_code >= 500 or status_code == 429


def request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = UPLOAD_MAX_RETRIES,
    backoff: float = UPLOAD_RETRY_BACKOFF,
    **kwargs,
) -> requests.Response:
    """
    Perform an HTTP request, retrying on network errors, 5xx, and 429.

    Raises the last exception after exhausting retries.
    Does NOT retry permanent client errors (most 4xx).
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else backoff * (2 ** (attempt - 1))
                )
                logger.warning(
                    "Attempt %d/%d: rate-limited (429). Waiting %.1fs before retry.",
                    attempt,
                    max_retries,
                    wait,
                )
                last_exc = RateLimitError(f"Rate limited (429) on {method} {url}")
                if attempt < max_retries:
                    time.sleep(wait)
                    continue
                raise last_exc

            if _is_retryable_status(resp.status_code):
                logger.warning(
                    "Attempt %d/%d: server error %s for %s %s",
                    attempt,
                    max_retries,
                    resp.status_code,
                    method,
                    url,
                )
                last_exc = HTTPError(
                    f"Server error {resp.status_code}",
                    response=resp,
                )
                if attempt < max_retries:
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                raise last_exc

            return resp

        except (RequestsConnectionError, RequestsTimeout) as exc:
            logger.warning(
                "Attempt %d/%d: network/timeout error – %s",
                attempt,
                max_retries,
                exc,
            )
            last_exc = exc
            if attempt < max_retries:
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("request_with_retry exhausted without result")


# ---------------------------------------------------------------------------
# Document upload lifecycle
# ---------------------------------------------------------------------------
def initialize_document_upload(owner: str) -> dict:
    """Step 1 – obtain a temporary uploadUrl and document URN."""
    url = "https://api.linkedin.com/rest/documents?action=initializeUpload"
    payload = {"initializeUploadRequest": {"owner": owner}}

    logger.info("Initializing document upload (owner=%s)", owner)

    try:
        resp = request_with_retry("POST", url, json=payload, headers=api_headers(), timeout=30)
    except RateLimitError:
        raise
    except RequestException as exc:
        raise UploadInitError(f"initializeUpload network failure: {exc}") from exc

    if resp.status_code not in (200, 201):
        body = (resp.text or "")[:500]
        if resp.status_code in (401, 403):
            raise UploadInitError(
                f"Auth/permission error ({resp.status_code}). "
                f"Check token scope (w_member_social) and expiry. Body: {body}"
            )
        raise UploadInitError(f"initializeUpload failed ({resp.status_code}): {body}")

    try:
        value = resp.json()["value"]
        if "uploadUrl" not in value or "document" not in value:
            raise KeyError("response missing 'uploadUrl' or 'document'")
    except (ValueError, KeyError, TypeError) as exc:
        raise UploadInitError(f"Unexpected initializeUpload response shape: {exc}") from exp

    logger.info("Document URN: %s", value["document"])
    return value


def upload_document_binary(upload_url: str, file_path: Path) -> None:
    """
    Step 2 – stream the binary file to the temporary upload URL.

    The file is re-opened on every attempt so retries always start from byte 0.
    """
    size = file_path.stat().st_size
    logger.info("Uploading binary: %s (%.1f MB)", file_path.name, size / (1024 * 1024))

    last_exc: Optional[Exception] = None

    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        try:
            with open(file_path, "rb") as fh:
                resp = requests.put(
                    upload_url,
                    data=fh,
                    headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                    timeout=120,
                )

            if resp.status_code in (200, 201):
                logger.info("Binary upload complete (attempt %d)", attempt)
                return

            body = (resp.text or "")[:500]

            if resp.status_code == 401:
                raise BinaryUploadError(
                    "Unauthorized (401) during binary upload. Token may have expired."
                )
            if resp.status_code == 413:
                raise BinaryUploadError(
                    "Payload too large (413). File exceeds LinkedIn size limits."
                )
            if resp.status_code in (403, 404):
                raise BinaryUploadError(
                    f"Binary upload rejected ({resp.status_code}): {body}"
                )

            if _is_retryable_status(resp.status_code):
                logger.warning(
                    "Binary upload attempt %d/%d failed with %s – will retry",
                    attempt,
                    UPLOAD_MAX_RETRIES,
                    resp.status_code,
                )
                last_exc = BinaryUploadError(
                    f"Binary upload failed ({resp.status_code}): {body}"
                )
                if attempt < UPLOAD_MAX_RETRIES:
                    time.sleep(UPLOAD_RETRY_BACKOFF * (2 ** (attempt - 1)))
                    continue
                raise last_exc

            raise BinaryUploadError(f"Binary upload failed ({resp.status_code}): {body}")

        except BinaryUploadError:
            raise
        except OSError as exc:
            raise BinaryUploadError(f"Cannot open/read document for upload: {exc}") from exp
        except (RequestsConnectionError, RequestsTimeout) as exc:
            logger.warning(
                "Binary upload attempt %d/%d network/timeout error: %s",
                attempt,
                UPLOAD_MAX_RETRIES,
                exp,
            )
            last_exc = BinaryUploadError(f"Binary upload network failure: {exc}")
            if attempt < UPLOAD_MAX_RETRIES:
                time.sleep(UPLOAD_RETRY_BACKOFF * (2 ** (attempt - 1)))
                continue
            raise last_exc from exp

    if last_exc:
        raise last_exc
    raise BinaryUploadError("Binary upload failed after all retries")


def get_document_status(document_urn: str) -> str:
    """Fetch current processing status. Returns STATUS_FETCH_FAILED on network errors."""
    encoded = quote(document_urn, safe="")
    url = f"https://api.linkedin.com/rest/documents/{encoded}"
    try:
        resp = request_with_retry("GET", url, headers=api_headers(None), timeout=30)
        if resp.status_code >= 400:
            logger.warning("Status fetch returned %s: %s", resp.status_code, resp.text[:200])
            return "STATUS_FETCH_FAILED"
        return resp.json().get("status", "UNKNOWN")
    except RequestException as exc:
        logger.warning("Failed to fetch document status: %s", exp)
        return "STATUS_FETCH_FAILED"


def wait_until_available(document_urn: str) -> str:
    """
    Step 3 – poll until AVAILABLE (or fail).

    Uses exponential backoff + jitter. Aborts after consecutive status-fetch
    failures or on PROCESSING_FAILED / timeout.
    """
    deadline = time.monotonic() + POLL_TIMEOUT
    interval = POLL_INITIAL
    last_status: Optional[str] = None
    attempt = 0
    consecutive_failures = 0
    max_consecutive_failures = 5

    logger.info("Polling document status (timeout=%.0fs)", POLL_TIMEOUT)

    while True:
        attempt += 1

        if time.monotonic() >= deadline:
            raise DocumentProcessingError(
                f"Document {document_urn} did not become AVAILABLE within "
                f"{POLL_TIMEOUT:.0f}s (last status={last_status}, attempts={attempt})"
            )

        status = get_document_status(document_urn)

        if status == "STATUS_FETCH_FAILED":
            consecutive_failures += 1
            logger.warning(
                "Status fetch failure %d/%d",
                consecutive_failures,
                max_consecutive_failures,
            )
            if consecutive_failures >= max_consecutive_failures:
                raise DocumentProcessingError(
                    f"Unable to retrieve document status after "
                    f"{max_consecutive_failures} consecutive failures"
                )
        else:
            consecutive_failures = 0
            if status != last_status:
                logger.info("[%d] Document status → %s", attempt, status)
                last_status = status

            if status == "AVAILABLE":
                logger.info("Document AVAILABLE after %d attempt(s)", attempt)
                return status

            if status == "PROCESSING_FAILED":
                raise DocumentProcessingError(
                    f"LinkedIn reported PROCESSING_FAILED for {document_urn}. "
                    "The file may be corrupt, password-protected, or exceed page limits."
                )

        remaining = deadline - time.monotonic()
        sleep_time = min(interval, remaining, POLL_MAX)
        sleep_time = max(0.1, sleep_time * (1.0 + random.uniform(-POLL_JITTER, POLL_JITTER)))
        time.sleep(sleep_time)
        interval = min(interval * POLL_MULTIPLIER, POLL_MAX)


def upload_and_prepare_document(document_path: Path, owner: str) -> str:
    """
    Full document lifecycle with clear failure boundaries:

      1. Local validation (no network)
      2. initializeUpload
      3. Binary PUT (with retries, file re-opened each attempt)
      4. Poll until AVAILABLE

    Returns the document URN ready for the posts API.
    On any failure the exception propagates; nothing is archived.
    """
    validate_document(document_path)

    init = initialize_document_upload(owner)
    upload_url = init["uploadUrl"]
    document_urn = init["document"]

    try:
        upload_document_binary(upload_url, document_path)
    except BinaryUploadError:
        logger.error(
            "Binary upload failed for %s. Document URN %s was registered but "
            "never completed – it will remain unused on LinkedIn's side.",
            document_path.name,
            document_urn,
        )
        raise

    try:
        wait_until_available(document_urn)
    except DocumentProcessingError:
        logger.error(
            "Document processing did not complete for %s (URN %s). "
            "The binary was uploaded but LinkedIn never marked it AVAILABLE.",
            document_path.name,
            document_urn,
        )
        raise

    return document_urn


# ---------------------------------------------------------------------------
# Post + first comment
# ---------------------------------------------------------------------------
def post_to_linkedin(
    text: str,
    *,
    document_urn: Optional[str] = None,
    document_title: Optional[str] = None,
) -> str:
    """Publish post; returns the post URN from x-restli-id."""
    require_credentials()
    author = normalize_author(PERSON_ID)  # type: ignore[arg-type]

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
        logger.info("Creating native document post (urn=%s)", document_urn)
    else:
        logger.info("Creating text-only post")

    try:
        response = request_with_retry(
            "POST",
            "https://api.linkedin.com/rest/posts",
            json=payload,
            headers=api_headers(),
            timeout=30,
        )
    except RateLimitError:
        raise
    except RequestException as exp:
        raise PostCreationError(f"Post creation network failure: {exc}") from exp

    if response.status_code == 201:
        post_id = response.headers.get("x-restli-id", "unknown")
        logger.info("Successfully posted to LinkedIn! Post ID: %s", post_id)
        return post_id

    body = (response.text or "")[:500]
    logger.error("Failed to post: %s – %s", response.status_code, body)

    if response.status_code in (401, 403):
        raise PostCreationError(
            f"Auth/permission error ({response.status_code}). "
            f"Check token and person ID. Body: {body}"
        )
    if response.status_code == 422:
        raise PostCreationError(f"Unprocessable entity (422): {body}")
    if response.status_code == 429:
        raise RateLimitError(f"Rate limited while creating post: {body}")

    raise PostCreationError(f"Post creation failed ({response.status_code}): {body}")


def post_first_comment(post_urn: str, comment_text: str) -> None:
    """
    Post a first comment on an existing post.

    Non-fatal: any failure is logged as a warning so the main post
    is still considered successful and files are archived.
    """
    author = normalize_author(PERSON_ID)  # type: ignore[arg-type]
    encoded_urn = quote(post_urn, safe="")
    url = f"https://api.linkedin.com/rest/socialActions/{encoded_urn}/comments"
    payload = {
        "actor": author,
        "object": post_urn,
        "message": {"text": comment_text},
    }

    logger.info("Posting first comment on %s", post_urn)
    try:
        resp = request_with_retry("POST", url, json=payload, headers=api_headers(), timeout=30)
    except RequestException as exp:
        logger.warning("First-comment network failure (non-fatal): %s", exp)
        return

    if resp.status_code in (200, 201):
        logger.info("First comment posted successfully")
    else:
        logger.warning(
            "Failed to post first comment (non-fatal): %s – %s",
            resp.status_code,
            (resp.text or "")[:300],
        )


def archive_files(*paths: Optional[Path]) -> None:
    """Move files into archived/. Failures here are logged but do not fail the run."""
    archived_dir = Path("archived")
    archived_dir.mkdir(exist_ok=True)

    for path in paths:
        if path is None or not path.exists():
            continue

        destination = archived_dir / path.name
        if destination.exists():
            stem, suffix, counter = path.stem, path.suffix, 1
            while destination.exists():
                destination = archived_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        try:
            shutil.move(str(path), str(destination))
            logger.info("Archived %s → %s", path, destination)
        except OSError as exp:
            logger.error("Failed to archive %s: %s", path, exp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    logger.info("LinkedIn Engineering Auto-Poster starting")

    try:
        require_credentials()
    except ConfigError as exp:
        logger.error("%s", exp)
        return 1

    text_path, commentary, document_path, comment_path = get_next_post()
    if not commentary or not text_path:
        logger.info("Nothing to post. Exiting cleanly.")
        return 0

    extras = []
    if document_path:
        extras.append(document_path.name)
    if comment_path:
        extras.append(comment_path.name)
    logger.info(
        "Posting: %s%s",
        text_path.name,
        (" + " + " + ".join(extras)) if extras else "",
    )

    document_urn: Optional[str] = None
    document_title: Optional[str] = None

    try:
        if document_path:
            owner = normalize_author(PERSON_ID)  # type: ignore[arg-type]
            document_urn = upload_and_prepare_document(document_path, owner)
            document_title = document_path.name

        post_urn = post_to_linkedin(
            commentary,
            document_urn=document_urn,
            document_title=document_title,
        )

        if comment_path and post_urn and post_urn != "unknown":
            comment_text = comment_path.read_text(encoding="utf-8").strip()
            if comment_text:
                post_first_comment(post_urn, comment_text)
            else:
                logger.warning("Comment file is empty: %s", comment_path)

        archive_files(text_path, document_path, comment_path)

    except DocumentValidationError as exp:
        logger.error("Document validation failed – nothing uploaded, files kept in posts/: %s", exp)
        return 1
    except UploadInitError as exp:
        logger.error("Upload initialization failed – files kept in posts/: %s", exp)
        return 1
    except BinaryUploadError as exp:
        logger.error("Binary upload failed – files kept in posts/: %s", exp)
        return 1
    except DocumentProcessingError as exp:
        logger.error("Document processing failed – files kept in posts/: %s", exp)
        return 1
    except RateLimitError as exp:
        logger.error("Rate limited by LinkedIn – files kept in posts/: %s", exp)
        return 1
    except PostCreationError as exp:
        logger.error(
            "Post creation failed – document may have been uploaded but post was not published. "
            "Files kept in posts/: %s",
            exp,
        )
        return 1
    except LinkedInError as exp:
        logger.error("LinkedIn error – files kept in posts/: %s", exp)
        return 1

    logger.info("Run completed successfully")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Unhandled error during execution")
        sys.exit(1)
