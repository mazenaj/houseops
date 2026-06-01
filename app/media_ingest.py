"""Media download, MIME sanitization, and GCS upload (SCHEMA §9.1)."""

from __future__ import annotations

import logging
import struct
from io import BytesIO
from typing import BinaryIO, Union, Tuple

import httpx
from google.cloud import storage

from app.config import (
    GCS_BUCKET,
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DURATION_SEC,
    MAX_DOCUMENT_BYTES,
    MAX_IMAGE_BYTES,
)
from app.models import InboundMessage, MediaBlock
from app.telegram import get_media_url

logger = logging.getLogger(__name__)

# Magic byte signatures
OGG_MAGIC = b"OggS"
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WEBP_MAGIC = b"RIFF"
PDF_MAGIC = b"%PDF"

NORMALIZED_VOICE = "audio/ogg; codecs=opus"


def _size_limit_for_mime(mime_type: str, normalized: Union[str, None] = None) -> int:
    check = (normalized or mime_type or "").lower()
    if check.startswith("audio/"):
        return MAX_AUDIO_BYTES
    if check.startswith("image/"):
        return MAX_IMAGE_BYTES
    if "pdf" in check or check.startswith("application/"):
        return MAX_DOCUMENT_BYTES
    return MAX_DOCUMENT_BYTES


def sniff_magic(data: bytes) -> Union[str, None]:
    """Return detected container type from magic bytes."""
    if len(data) >= 4 and data[:4] == OGG_MAGIC:
        return "ogg"
    if len(data) >= 3 and data[:3] == JPEG_MAGIC[:3]:
        return "jpeg"
    if len(data) >= 8 and data[:8] == PNG_MAGIC:
        return "png"
    if len(data) >= 12 and data[:4] == WEBP_MAGIC and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 4 and data[:4] == PDF_MAGIC:
        return "pdf"
    return None


def normalize_mime(
    raw_mime: str,
    magic: Union[str, None],
    is_voice_hint: bool = False,
) -> Union[Tuple[str, str], None]:
    """
    Return (normalized_mime_type, extension) or None if unknown.
    Voice notes always resolve to audio/ogg; codecs=opus.
    """
    raw_lower = (raw_mime or "").lower()
    if magic == "ogg" or is_voice_hint or "ogg" in raw_lower or raw_lower in (
        "application/octet-stream",
        "audio/webm",
        "audio/ogg",
    ):
        if magic == "ogg" or is_voice_hint or "audio" in raw_lower or raw_lower == "application/octet-stream":
            return NORMALIZED_VOICE, ".ogg"
    if magic == "jpeg" or "jpeg" in raw_lower or "jpg" in raw_lower:
        return "image/jpeg", ".jpg"
    if magic == "png" or "png" in raw_lower:
        return "image/png", ".png"
    if magic == "webp" or "webp" in raw_lower:
        return "image/webp", ".webp"
    if magic == "pdf" or "pdf" in raw_lower:
        return "application/pdf", ".pdf"
    if magic:
        mapping = {
            "ogg": (NORMALIZED_VOICE, ".ogg"),
            "jpeg": ("image/jpeg", ".jpg"),
            "png": ("image/png", ".png"),
            "webp": ("image/webp", ".webp"),
            "pdf": ("application/pdf", ".pdf"),
        }
        return mapping.get(magic)
    return None


def _probe_ogg_duration_sec(data: bytes) -> Union[float, None]:
    """Lightweight Ogg page scan for approximate duration (best-effort)."""
    try:
        offset = 0
        last_granule = 0
        sample_rate = 48000
        while offset < len(data) - 27:
            if data[offset : offset + 4] != OGG_MAGIC:
                break
            page_segments = data[offset + 26]
            header_size = 27 + page_segments
            segment_table_end = offset + header_size
            if segment_table_end > len(data):
                break
            granule = struct.unpack("<q", data[offset + 6 : offset + 14])[0]
            if granule > 0:
                last_granule = granule
            segment_data_size = sum(data[offset + 27 : segment_table_end])
            offset = segment_table_end + segment_data_size
        if last_granule > 0:
            return last_granule / sample_rate
    except Exception as exc:
        logger.debug("ogg_duration_probe_failed error=%s", exc)
    return None


def stream_download_meta(
    media_url: str,
    max_bytes: int,
) -> Tuple[bytes, int, Union[str, None]]:
    """
    Stream download from Meta CDN with byte guard.
    Returns (full_content, bytes_downloaded, reject_reason).
    """
    headers = {}
    chunks: list[bytes] = []
    total = 0
    reject_reason: Union[str, None] = None

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        with client.stream("GET", media_url, headers=headers) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                logger.warning(
                    "media_rejected_content_length content_length=%s max=%s",
                    content_length,
                    max_bytes,
                )
                return b"", 0, "content_length_exceeded"

            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "media_rejected_streaming_bytes bytes=%s max=%s",
                        total,
                        max_bytes,
                    )
                    return b"", total, "max_bytes_exceeded"
                chunks.append(chunk)

    return b"".join(chunks), total, reject_reason


def upload_to_gcs(
    bucket_name: str,
    object_path: str,
    data: bytes,
    content_type: str,
) -> str:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    blob.upload_from_string(data, content_type=content_type)
    uri = f"gs://{bucket_name}/{object_path}"
    logger.info("gcs_upload_complete path=%s bytes=%d content_type=%s", object_path, len(data), content_type)
    return uri


def ingest_media_blocks(inbound: InboundMessage) -> Tuple[bool, Union[str, None]]:
    """
    For each media block with null gcs_uri: download, sanitize MIME, upload to GCS.
    Returns (success, user_facing_error_message).
    """
    if not GCS_BUCKET:
        logger.error("GCS_BUCKET not configured")
        return False, "Media storage is not configured."

    for index, block in enumerate(inbound.content):
        if block.block_type != "media" or block.gcs_uri:
            continue

        media_block: MediaBlock = block
        is_voice = media_block.mime_type.startswith("audio/") or media_block.mime_type == "application/octet-stream"
        max_bytes = _size_limit_for_mime(media_block.mime_type)

        try:
            media_url = get_media_url(media_block.media_id)
            data, bytes_downloaded, reject = stream_download_meta(media_url, max_bytes)
            if reject:
                logger.warning(
                    "media_download_rejected message_id=%s index=%s reason=%s bytes=%s",
                    inbound.message_id,
                    index,
                    reject,
                    bytes_downloaded,
                )
                if "audio" in media_block.mime_type or is_voice:
                    return False, "Voice note too long (max 5 min). Please send a shorter message or type your update."
                return False, "File too large. Please send a smaller attachment or type your message."

            if not data:
                return False, "Could not download media. Please retry."

            magic = sniff_magic(data[:512])
            normalized = normalize_mime(media_block.mime_type, magic, is_voice_hint=is_voice)
            if not normalized:
                logger.warning(
                    "mime_normalization_failed message_id=%s raw_mime=%s magic=%s",
                    inbound.message_id,
                    media_block.mime_type,
                    magic,
                )
                return False, "Could not process that audio file — please retry or type your message."

            norm_mime, ext = normalized

            if norm_mime == NORMALIZED_VOICE:
                duration = _probe_ogg_duration_sec(data)
                if duration and duration > MAX_AUDIO_DURATION_SEC:
                    logger.warning(
                        "media_rejected_duration message_id=%s duration_sec=%s",
                        inbound.message_id,
                        duration,
                    )
                    return False, "Voice note too long (max 5 min). Please send a shorter message or type your update."

            object_path = f"inbound/{inbound.phone_e164}/{inbound.message_id}/{index}{ext}"
            gcs_uri = upload_to_gcs(GCS_BUCKET, object_path, data, norm_mime)

            media_block.gcs_uri = gcs_uri
            media_block.normalized_mime_type = norm_mime
            logger.info(
                "media_ingest_complete message_id=%s index=%s gcs_uri=%s normalized_mime=%s bytes=%s",
                inbound.message_id,
                index,
                gcs_uri,
                norm_mime,
                bytes_downloaded,
            )
        except Exception as exc:
            logger.exception(
                "media_ingest_failed message_id=%s index=%s error=%s",
                inbound.message_id,
                index,
                exc,
            )
            return False, "Could not process media. Please retry or type your message."

    return True, None
