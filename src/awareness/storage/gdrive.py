"""Google Drive API Client for cloud storage uploads."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from awareness.config import get_settings
from awareness.obs.logging import get_logger

logger = get_logger("storage.gdrive")

AUTH_FILE_NAME = "gdrive_auth.json"

# M-04: upload retry policy. 429/5xx/transport errors are retried with
# exponential backoff (up to 3 attempts); a 401 refreshes the access token
# once and retries. Bodies above _STREAM_THRESHOLD_BYTES are streamed from
# disk instead of being read fully into memory.
_MAX_UPLOAD_ATTEMPTS = 3
_STREAM_THRESHOLD_BYTES = 8 * 1024 * 1024
_UPLOAD_BACKOFF_SECONDS = (1.0, 2.0)


def get_auth_file_path() -> Path:
    settings = get_settings()
    return settings.data_dir / "state" / AUTH_FILE_NAME


def is_authorized() -> bool:
    """Return True if Google Drive auth file exists."""
    return get_auth_file_path().exists()


def load_auth() -> dict[str, Any] | None:
    path = get_auth_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("gdrive_auth_load_failed", error=str(e))
        return None


def save_auth(data: dict[str, Any]) -> None:
    path = get_auth_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def refresh_access_token(auth_data: dict[str, Any]) -> str | None:
    """Refresh and return a valid Google Drive Access Token."""
    url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": auth_data.get("client_id"),
        "client_secret": auth_data.get("client_secret"),
        "refresh_token": auth_data.get("refresh_token"),
        "grant_type": "refresh_token",
    }
    try:
        r = httpx.post(url, data=payload, timeout=10.0)
        if r.status_code == 200:
            res = r.json()
            access_token = res.get("access_token")
            if access_token:
                # Update saved auth with new access token if needed (though it expires)
                auth_data["access_token"] = access_token
                save_auth(auth_data)
                return access_token
        logger.error("gdrive_token_refresh_failed", status=r.status_code, response=r.text)
    except Exception as e:
        logger.exception("gdrive_token_refresh_exception", error=str(e))
    return None


def _folder_name() -> str:
    """The configured Drive folder name (defaults to 'Awareness Captures')."""
    try:
        return get_settings().gdrive_folder_name or "Awareness Captures"
    except Exception:
        return "Awareness Captures"


def _get_or_create_folder(access_token: str) -> str | None:
    """Search for the configured captures folder. Create it if missing."""
    headers = {"Authorization": f"Bearer {access_token}"}
    folder_name = _folder_name()
    # Escape single quotes for the Drive query language.
    safe_name = folder_name.replace("'", "\\'")

    # 1. Search for existing folder
    search_url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        "spaces": "drive",
        "fields": "files(id, name)",
    }
    try:
        r = httpx.get(search_url, headers=headers, params=params, timeout=10.0)
        if r.status_code == 200:
            files = r.json().get("files", [])
            if files:
                return str(files[0]["id"])
        else:
            logger.error("gdrive_folder_search_failed", status=r.status_code, response=r.text)
            return None
    except Exception as e:
        logger.exception("gdrive_folder_search_exception", error=str(e))
        return None

    # 2. Create the folder if not found
    create_url = "https://www.googleapis.com/drive/v3/files"
    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    try:
        r = httpx.post(create_url, headers=headers, json=folder_metadata, timeout=10.0)
        if r.status_code == 200:
            folder_id = r.json().get("id")
            if folder_id:
                logger.info("gdrive_folder_created", folder_id=folder_id)
                return str(folder_id)
        logger.error("gdrive_folder_creation_failed", status=r.status_code, response=r.text)
    except Exception as e:
        logger.exception("gdrive_folder_creation_exception", error=str(e))
    return None


def _file_mime(file_path: Path) -> str:
    """Content type for an uploaded corpus chunk."""
    return "application/gzip" if file_path.suffix == ".gz" else "application/x-ndjson"


def _build_multipart_body(
    metadata: dict[str, Any], file_bytes: bytes, file_mime: str, boundary: str
) -> bytes:
    """Assemble a multipart/related body as bytes so binary (gzip) chunks
    upload intact and carry the correct content type."""
    return b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            json.dumps(metadata).encode("utf-8"),
            f"\r\n--{boundary}\r\n".encode(),
            f"Content-Type: {file_mime}\r\n\r\n".encode(),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )


def _multipart_head(metadata: dict[str, Any], file_mime: str, boundary: str) -> bytes:
    return b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            json.dumps(metadata).encode("utf-8"),
            f"\r\n--{boundary}\r\n".encode(),
            f"Content-Type: {file_mime}\r\n\r\n".encode(),
        ]
    )


_MULTIPART_TAIL = b"\r\n--%s--\r\n"


def _multipart_stream(
    metadata: dict[str, Any], file_path: Path, file_mime: str, boundary: str
) -> Iterator[bytes]:
    """Yield a multipart/related body from disk in bounded chunks (M-04).

    The body is the same shape as :func:`_build_multipart_body` but the file
    bytes are streamed 256 KiB at a time so large (multi-GB) chunks never sit
    fully in memory. The caller must set Content-Length (computed from the
    parts + file size).
    """
    yield _multipart_head(metadata, file_mime, boundary)
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(256 * 1024)
            if not chunk:
                break
            yield chunk
    yield _MULTIPART_TAIL % boundary.encode()


def _multipart_stream_length(metadata: dict[str, Any], file_path: Path, file_mime: str, boundary: str) -> int:
    return (
        len(_multipart_head(metadata, file_mime, boundary))
        + file_path.stat().st_size
        + len(_MULTIPART_TAIL % boundary.encode())
    )


def upload_file(file_path: Path) -> str | None:
    """Upload a local file to the 'Awareness Captures' folder on Google Drive.

    Retries 429/5xx/transport errors with backoff, refreshes the token once on
    401, and streams the body from disk for chunks above
    :data:`_STREAM_THRESHOLD_BYTES` (M-04).
    """
    if not file_path.exists():
        logger.error("gdrive_upload_file_not_found", path=str(file_path))
        return None

    auth_data = load_auth()
    if not auth_data:
        logger.warning("gdrive_upload_unauthorized")
        return None

    access_token = refresh_access_token(auth_data)
    if not access_token:
        logger.error("gdrive_upload_refresh_failed")
        return None

    folder_id = _get_or_create_folder(access_token)
    if not folder_id:
        logger.error("gdrive_upload_folder_resolved_failed")
        return None

    filename = file_path.name
    file_size = file_path.stat().st_size
    boundary = "awareness_gdrive_upload_boundary"
    metadata = {"name": filename, "parents": [folder_id]}
    file_mime = _file_mime(file_path)

    upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
    token_refreshed = False
    for attempt in range(_MAX_UPLOAD_ATTEMPTS):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        }
        try:
            if file_size > _STREAM_THRESHOLD_BYTES:
                headers["Content-Length"] = str(
                    _multipart_stream_length(metadata, file_path, file_mime, boundary)
                )
                r = httpx.post(
                    upload_url,
                    headers=headers,
                    content=_multipart_stream(metadata, file_path, file_mime, boundary),
                    timeout=120.0,
                )
            else:
                try:
                    file_bytes = file_path.read_bytes()
                except Exception as e:
                    logger.exception("gdrive_upload_read_failed", path=str(file_path), error=str(e))
                    return None
                multipart_body = _build_multipart_body(metadata, file_bytes, file_mime, boundary)
                r = httpx.post(upload_url, headers=headers, content=multipart_body, timeout=30.0)
        except (httpx.TimeoutException, httpx.TransportError, httpx.NetworkError) as e:
            logger.warning(
                "gdrive_upload_transport_error",
                attempt=attempt + 1,
                filename=filename,
                error=str(e),
            )
            if attempt < _MAX_UPLOAD_ATTEMPTS - 1:
                time.sleep(_UPLOAD_BACKOFF_SECONDS[min(attempt, len(_UPLOAD_BACKOFF_SECONDS) - 1)])
                continue
            return None

        if r.status_code == 401 and not token_refreshed:
            # Token expired mid-run (or raced the refresh): refresh once and retry.
            fresh = load_auth()
            refreshed = refresh_access_token(fresh) if fresh else None
            if not refreshed:
                logger.error("gdrive_upload_refresh_retry_failed")
                return None
            access_token = refreshed
            token_refreshed = True
            continue
        if r.status_code == 429 or r.status_code >= 500:
            logger.warning(
                "gdrive_upload_retryable_status",
                attempt=attempt + 1,
                filename=filename,
                status=r.status_code,
            )
            if attempt < _MAX_UPLOAD_ATTEMPTS - 1:
                time.sleep(_UPLOAD_BACKOFF_SECONDS[min(attempt, len(_UPLOAD_BACKOFF_SECONDS) - 1)])
                continue
            logger.error("gdrive_upload_api_failed", status=r.status_code, response=r.text)
            return None
        if r.status_code == 200:
            file_id = r.json().get("id")
            logger.info("gdrive_upload_success", filename=filename, file_id=file_id)
            return file_id
        logger.error("gdrive_upload_api_failed", status=r.status_code, response=r.text)
        return None
    return None
