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
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    HTTPError,
    RequestException,
    Timeout as RequestsTimeout,
)

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
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=LOG_FILE, maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError as exc:
        log.warning("Could not create rotating file handler: %s. Using console only.", exp)
    return log


logger = setup_logging()


class LinkedInError(Exception):
    pass

class ConfigError(LinkedInError):
    pass

class DocumentValidationError(LinkedInError):
    pass

class UploadInitError(LinkedInError):
    pass

class BinaryUploadError(LinkedInError):
    pass

class DocumentProcessingError(LinkedInError):
    pass

class PostCreationError(LinkedInError):
    pass


ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
PERSON_ID = os.environ.get("LINKEDIN_PERSON_ID")
LINKEDIN_VERSION = "202607"
DOCUMENT_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".doc", ".docx"}
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
POLL_TIMEOUT = float(os.environ.get("DOCUMENT_POLL_TIMEOUT", 180))
POLL_INITIAL_INTERVAL = float(os.environ.get("DOCUMENT_POLL_INITIAL", 1.0))
POLL_MAX_INTERVAL = float(os.environ.get("DOCUMENT_POLL_MAX", 12.0))
POLL_MULTIPLIER = float(os.environ.get("DOCUMENT_POLL_MULTIPLIER", 2.0))
POLL_JITTER = float(os.environ.get("DOCUMENT_POLL_JITTER", 0.25))
UPLOAD_MAX_RETRIES = int(os.environ.get("UPLOAD_MAX_RETRIES", 3))
UPLOAD_RETRY_BACKOFF = float(os.environ.get("UPLOAD_RETRY_BACKOFF", 2.0))


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
    if person_id.startswith("urn:li:person:"):
        return person_id
    return f"urn:li:person:{person_id}"


def require_credentials() -> None:
    missing = []
    if not ACCESS_TOKEN:
        missing.append("LINKEDIN_ACCESS_TOKEN")
    if not PERSON_ID:
        missing.append("LINKEDIN_PERSON_ID")
    if missing:
        raise ConfigError(f"Missing required environment variable(s): {', '.join(missing)}")


def find_companion_document(post_path: Path) -> Optional[Path]:
    for ext in DOCUMENT_EXTENSIONS:
        candidate = post_path.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


def validate_document(document_path: Path) -> None:
    if not document_path.is_file():
        raise DocumentValidationError(f"Document not found: {document_path}")
    if document_path.suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise DocumentValidationError(
            f"Unsupported document type: {document_path.suffix} "
            f"(allowed: {', '.join(sorted(DOCUMENT_EXTENSIONS))})"
        )
    try:
        size = document_path.stat().st_size
    except OSError as exc:
        raise DocumentValidationError(f"Cannot read document metadata for {document_path}: {exc}") from exp
    if size == 0:
        raise DocumentValidationError(f"Document is empty: {document_path}")
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentValidationError(
            f"Document exceeds LinkedIn 100 MB limit: {document_path} ({size / (1024 * 1024):.1f} MB)"
        )
    try:
        with open(document_path, "rb") as fh:
            fh.read(64)
    except OSError as exp:
        raise DocumentValidationError(f"Cannot read document file {document_path}: {exc}") from exp
    logger.debug("Document validation passed: %s (%.1f MB)", document_path.name, size / (1024 * 1024))


def get_next_post() -> tuple[Optional[Path], Optional[str], Optional[Path]]:
    posts_dir = Path("posts")
    if not posts_dir.is_dir():
        logger.warning("posts/ directory not found.")
        return None, None, None
    candidates = sorted(list(posts_dir.glob("*.txt")) + list(posts_dir.glob("*.md")))
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
    logger.debug("Selected post file: %s (%d chars)", post_path.name, len(content))
    return post_path, content, document_path


def _request_with_retry(method: str, url: str, *, max_retries: int = UPLOAD_MAX_RETRIES, backoff: float = UPLOAD_RETRY_BACKOFF, **kwargs) -> requests.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code >= 500:
                logger.warning("Attempt %d/%d: server error %s for %s %s – will retry", attempt, max_retries, resp.status_code, method, url)
                last_exc = HTTPError(f"Server error {resp.status_code}: {resp.text[:300]}", response=resp)
                if attempt < max_retries:
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                raise last_exc
            return resp
        except (RequestsConnectionError, RequestsTimeout) as exp:
            logger.warning("Attempt %d/%d: network error for %s %s – %s", attempt, max_retries, method, url, exp)
            last_exc = exp
            if attempt < max_retries:
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Request failed after retries with no exception captured")


def initialize_document_upload(owner: str) -> dict:
    url = "https://api.linkedin.com/rest/documents?action=initializeUpload"
    payload = {"initializeUploadRequest": {"owner": owner}}
    logger.info("Initializing document upload (owner=%s)", owner)
    try:
        resp = _request_with_retry("POST", url, json=payload, headers=_api_headers(), timeout=30)
    except RequestException as exp:
        logger.error("initializeUpload network failure: %s", exp)
        raise UploadInitError(f"initializeUpload network failure: {exc}") from exp
    if resp.status_code not in (200, 201):
        body = resp.text[:500]
        logger.error("initializeUpload failed: %s – %s", resp.status_code, body)
        if resp.status_code in (401, 403):
            raise UploadInitError(f"initializeUpload auth/permission error ({resp.status_code}). Check LINKEDIN_ACCESS_TOKEN and w_member_social scope. Response: {body}")
        raise UploadInitError(f"initializeUpload failed with status {resp.status_code}: {body}")
    try:
        value = resp.json()["value"]
        if "uploadUrl" not in value or "document" not in value:
            raise KeyError("uploadUrl or document missing from response")
    except (ValueError, KeyError) as exp:
        raise UploadInitError(f"Unexpected initializeUpload response structure: {exc}") from exp
    logger.info("Document URN: %s", value["document"])
    return value


def upload_document_binary(upload_url: str, file_path: Path) -> None:
    size = file_path.stat().st_size
    logger.info("Uploading binary: %s (%.1f MB)", file_path.name, size / (1024 * 1024))
    try:
        with open(file_path, "rb") as fh:
            resp = _request_with_retry("PUT", upload_url, data=fh, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, timeout=120)
    except OSError as exp:
        logger.error("Cannot open document for upload: %s", exp)
        raise BinaryUploadError(f"Cannot open document for upload: {exc}") from exp
    except RequestException as exp:
        logger.error("Binary upload network failure: %s", exp)
        raise BinaryUploadError(f"Binary upload network failure: {exc}") from exp
    if resp.status_code not in (200, 201):
        body = resp.text[:500]
        logger.error("Binary upload failed: %s – %s", resp.status_code, body)
        if resp.status_code == 401:
            raise BinaryUploadError("Binary upload unauthorized (401). Token may have expired.")
        if resp.status_code == 413:
            raise BinaryUploadError("Binary upload rejected (413 Payload Too Large). File may exceed LinkedIn limits.")
        raise BinaryUploadError(f"Binary upload failed with status {resp.status_code}: {body}")
    logger.info("Binary upload complete")


def get_document_status(document_urn: str) -> str:
    encoded = requests.utils.quote(document_urn, safe="")
    url = f"https://api.linkedin.com/rest/documents/{encoded}"
    try:
        resp = _request_with_retry("GET", url, headers=_api_headers(content_type=None), timeout=30)
        resp.raise_for_status()
        return resp.json().get("status", "UNKNOWN")
    except RequestException as exp:
        logger.warning("Failed to fetch document status: %s", exp)
        return "STATUS_FETCH_FAILED"


def wait_until_available(document_urn: str, *, timeout: float = POLL_TIMEOUT, initial_interval: float = POLL_INITIAL_INTERVAL, max_interval: float = POLL_MAX_INTERVAL, multiplier: float = POLL_MULTIPLIER, jitter: float = POLL_JITTER) -> str:
    deadline = time.monotonic() + timeout
    interval = initial_interval
    last_status: Optional[str] = None
    attempt = 0
    consecutive_fetch_failures = 0
    max_consecutive_fetch_failures = 5
    logger.info("Polling document status (timeout=%.0fs, initial=%.1fs, max=%.1fs)", timeout, initial_interval, max_interval)
    while True:
        attempt += 1
        if time.monotonic() >= deadline:
            msg = f"Document {document_urn} did not become AVAILABLE within {timeout}s (last status: {last_status}, attempts: {attempt})"
            logger.error(msg)
            raise DocumentProcessingError(msg)
        status = get_document_status(document_urn)
        if status == "STATUS_FETCH_FAILED":
            consecutive_fetch_failures += 1
            logger.warning("Status fetch failed (%d/%d consecutive)", consecutive_fetch_failures, max_consecutive_fetch_failures)
            if consecutive_fetch_failures >= max_consecutive_fetch_failures:
                raise DocumentProcessingError(f"Unable to retrieve document status after {max_consecutive_fetch_failures} consecutive failures")
        else:
            consecutive_fetch_failures = 0
            if status != last_status:
                logger.info("[%d] Document status → %s", attempt, status)
                last_status = status
            if status == "AVAILABLE":
                logger.info("Document is AVAILABLE after %d attempt(s)", attempt)
                return status
            if status == "PROCESSING_FAILED":
                msg = f"Document processing failed for {document_urn}. Possible causes: unsupported format, corrupt file, or LinkedIn internal error."
                logger.error(msg)
                raise DocumentProcessingError(msg)
        remaining = deadline - time.monotonic()
        sleep_time = max(0.1, min(interval, remaining, max_interval) * (1.0 + random.uniform(-jitter, jitter)))
        time.sleep(sleep_time)
        interval = min(interval * multiplier, max_interval)


def upload_and_prepare_document(document_path: Path, owner: str) -> str:
    validate_document(document_path)
    init = initialize_document_upload(owner)
    upload_document_binary(init["uploadUrl"], document_path)
    wait_until_available(init["document"])
    return init["document"]


def post_to_linkedin(text: str, *, document_urn: Optional[str] = None, document_title: Optional[str] = None) -> None:
    require_credentials()
    author = normalize_author(PERSON_ID)  # type: ignore[arg-type]
    payload: dict = {
        "author": author,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if document_urn:
        payload["content"] = {"media": {"id": document_urn, "title": document_title or "Document"}}
        logger.info("Creating native document post (urn=%s, title=%s)", document_urn, document_title)
    else:
        logger.info("Creating text-only post (author=%s)", author)
    try:
        response = _request_with_retry("POST", "https://api.linkedin.com/rest/posts", json=payload, headers=_api_headers(), timeout=30)
    except RequestException as exp:
        logger.error("Post creation network failure: %s", exp)
        raise PostCreationError(f"Post creation network failure: {exc}") from exp
    if response.status_code == 201:
        post_id = response.headers.get("x-restli-id", "unknown")
        logger.info("Successfully posted to LinkedIn! Post ID: %s", post_id)
        return
    body = response.text[:500]
    logger.error("Failed to post: %s – %s", response.status_code, body)
    if response.status_code in (401, 403):
        raise PostCreationError(f"Post creation auth/permission error ({response.status_code}). Check token scope (w_member_social) and that PERSON_ID matches the token owner. Response: {body}")
    if response.status_code == 422:
        raise PostCreationError(f"Post creation rejected (422). Often caused by invalid document URN or malformed payload. Response: {body}")
    raise PostCreationError(f"Post creation failed with status {response.status_code}: {body}")


def archive_files(*paths: Path) -> None:
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


def main() -> int:
    logger.info("LinkedIn Engineering Auto-Poster starting")
    try:
        require_credentials()
    except ConfigError as exp:
        logger.error("%s", exp)
        return 1
    text_path, commentary, document_path = get_next_post()
    if not commentary or not text_path:
        logger.info("Nothing to post. Exiting cleanly.")
        return 0
    logger.info("Posting: %s%s", text_path.name, f" + {document_path.name}" if document_path else "")
    document_urn: Optional[str] = None
    document_title: Optional[str] = None
    try:
        if document_path:
            owner = normalize_author(PERSON_ID)  # type: ignore[arg-type]
            document_urn = upload_and_prepare_document(document_path, owner)
            document_title = document_path.name
        post_to_linkedin(commentary, document_urn=document_urn, document_title=document_title)
        archive_files(text_path, document_path)
    except DocumentValidationError as exp:
        logger.error("Document validation failed – post aborted: %s", exp)
        return 1
    except UploadInitError as exp:
        logger.error("Upload initialization failed – post aborted: %s", exp)
        return 1
    except BinaryUploadError as exp:
        logger.error("Binary upload failed – post aborted: %s", exp)
        return 1
    except DocumentProcessingError as exp:
        logger.error("Document processing failed – post aborted: %s", exp)
        return 1
    except PostCreationError as exp:
        logger.error("Post creation failed – files NOT archived: %s", exp)
        return 1
    except LinkedInError as exp:
        logger.error("LinkedIn error – post aborted: %s", exp)
        return 1
    logger.info("Run completed successfully")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Unhandled error during execution")
        sys.exit(1)
