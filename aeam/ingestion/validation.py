"""
aeam/ingestion/validation.py

Upload validation for the Enterprise Ingress API (Phase B1.2).

Pure, stateless validation — no I/O, no blob writes, no registry access.
Checks file presence, size, extension, and MIME type against the format set
declared in the B1 Enterprise Data Layer Blueprint (Task 2): PDF, DOCX,
Markdown, CSV, Excel, Images, Logs, JSON, XML, Audio, Video. Database/REST/
Sheets/SharePoint/Confluence sources are connector-based, not file uploads,
and are out of scope for this validator.

This module never parses file contents — only metadata (name/size/type).
"""

from __future__ import annotations

# 100 MB — generous enough for real enterprise documents/datasets while
# keeping the ingress request bounded; large media may need a later
# chunked-upload path, out of scope for B1.2.
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024

# Extension -> category, drives both validation and (later) classification.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "md": "markdown", "markdown": "markdown",
    "csv": "csv",
    "xlsx": "excel", "xls": "excel",
    "png": "image", "jpg": "image", "jpeg": "image",
    "gif": "image", "webp": "image", "tiff": "image", "bmp": "image",
    "log": "log", "txt": "log",
    "json": "json",
    "xml": "xml",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "flac": "audio", "ogg": "audio",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video", "webm": "video",
}

# MIME types accepted PER CATEGORY (not a flat global set) — a `.csv` upload
# reporting a `.pdf`'s MIME type must still be rejected, so the check below
# looks up only the set for the category the *extension* already resolved to.
# Intentionally broad within each category: many clients send vendor/legacy
# variants for the same format.
_MIME_BY_CATEGORY: dict[str, set[str]] = {
    "pdf": {"application/pdf"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "markdown": {"text/markdown", "text/x-markdown"},
    "csv": {"text/csv", "application/csv"},
    "excel": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    },
    "image": {"image/png", "image/jpeg", "image/gif", "image/webp", "image/tiff", "image/bmp"},
    "log": {"text/plain", "text/x-log"},
    "json": {"application/json"},
    "xml": {"application/xml", "text/xml"},
    "audio": {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/flac", "audio/ogg"},
    "video": {"video/mp4", "video/quicktime", "video/x-msvideo", "video/x-matroska", "video/webm"},
}

#: Flattened view of every accepted MIME type across all categories —
#: informational (e.g. for API docs), never used for the per-category check.
SUPPORTED_MIME_TYPES: set[str] = {m for mimes in _MIME_BY_CATEGORY.values() for m in mimes}

# Client-sent values that carry no real signal — MIME check is skipped when
# the content_type is one of these, and extension becomes the sole authority.
_GENERIC_MIME: frozenset[str] = frozenset({"", "application/octet-stream", "binary/octet-stream"})


class IngestValidationError(ValueError):
    """
    Raised when an upload fails validation.

    Args:
        reason: Machine-stable short code (e.g. ``"unsupported_extension"``),
                used by the API layer to build a structured 422 response.
        detail: Human-readable explanation.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


def _extension_of(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].strip().lower()


def validate_upload(
    filename: str | None,
    content_type: str | None,
    size: int,
) -> str:
    """
    Validate an upload's metadata and return its detected category.

    Args:
        filename:     The client-supplied filename. Must be present and carry
                      a supported extension.
        content_type: The client-supplied MIME type, or ``None``/empty.
                      Generic values (``application/octet-stream`` etc.) are
                      ignored in favour of the extension.
        size:         The upload size in bytes, as already read into memory
                      (or reported by the client) — must be > 0 and within
                      :data:`MAX_UPLOAD_BYTES`.

    Returns:
        The detected format category (e.g. ``"pdf"``, ``"csv"``, ``"image"``)
        from :data:`SUPPORTED_EXTENSIONS` — used to tag the created job, not
        to route processing (no parsing happens in this phase).

    Raises:
        IngestValidationError: On any validation failure, with a stable
                               ``reason`` code and human ``detail``.
    """
    if not filename or not filename.strip():
        raise IngestValidationError("file_missing", "No file was provided.")

    if size <= 0:
        raise IngestValidationError("empty_file", "The uploaded file is empty.")

    if size > MAX_UPLOAD_BYTES:
        raise IngestValidationError(
            "file_too_large",
            f"File is {size} bytes, which exceeds the {MAX_UPLOAD_BYTES}-byte limit.",
        )

    ext = _extension_of(filename.strip())
    category = SUPPORTED_EXTENSIONS.get(ext)
    if category is None:
        raise IngestValidationError(
            "unsupported_extension",
            f"'.{ext or '<none>'}' is not a supported file type. "
            f"Supported: {', '.join(sorted(set(SUPPORTED_EXTENSIONS.values())))}.",
        )

    normalized_type = (content_type or "").strip().lower()
    if normalized_type and normalized_type not in _GENERIC_MIME:
        # Checked against the SPECIFIC category's MIME set, not the flattened
        # global set — a `.csv` claiming `application/pdf` must still fail,
        # even though that MIME is valid for a *different* category.
        if normalized_type not in _MIME_BY_CATEGORY.get(category, set()):
            raise IngestValidationError(
                "unsupported_mime_type",
                f"MIME type '{content_type}' is not supported for a '.{ext}' file.",
            )

    return category
