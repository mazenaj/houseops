"""Unit tests for media download, MIME validation, and duration probing."""

from __future__ import annotations

from unittest.mock import patch
from app.models import MediaBlock
from app.media_ingest import (
    sniff_magic,
    normalize_mime,
    _probe_ogg_duration_sec,
    _ingest_single_media_block,
    NORMALIZED_VOICE,
)


def test_sniff_magic():
    """Test magic bytes container detection."""
    assert sniff_magic(b"OggSxxxx") == "ogg"
    assert sniff_magic(b"\xff\xd8\xffxxxx") == "jpeg"
    assert sniff_magic(b"\x89PNG\r\n\x1a\n") == "png"
    assert sniff_magic(b"RIFFxxxxWEBP") == "webp"
    assert sniff_magic(b"%PDFxxxx") == "pdf"
    assert sniff_magic(b"unknown_bytes") is None


def test_normalize_mime():
    """Test MIME normalization priority logic (magic bytes prioritized)."""
    # 1. Magic bytes match should override generic type hints
    assert normalize_mime("audio/ogg", "jpeg") == ("image/jpeg", ".jpg")
    assert normalize_mime("image/jpeg", "ogg") == (NORMALIZED_VOICE, ".ogg")

    # 2. Fallback matching
    assert normalize_mime("audio/ogg", None) == (NORMALIZED_VOICE, ".ogg")
    assert normalize_mime("application/octet-stream", None, is_voice_hint=True) == (
        NORMALIZED_VOICE,
        ".ogg",
    )
    assert normalize_mime("image/png", None) == ("image/png", ".png")
    assert normalize_mime("unknown/mime", None) is None


def test_probe_ogg_duration_sec():
    """Test Ogg page duration estimation."""
    # Invalid data should return None
    assert _probe_ogg_duration_sec(b"not_ogg_data") is None

    # Valid-looking Ogg page headers (minimal mockup)
    # Page 1: header size 27, segments 0, granule 0
    page1 = b"OggS" + b"\x00" * 22 + b"\x00"
    # Page 2: granule = 96000 (which at 48000Hz is 2.0 seconds)
    import struct

    granule_bytes = struct.pack("<q", 96000)
    page2 = b"OggS" + b"\x00\x02" + granule_bytes + b"\x00" * 12 + b"\x00"

    data = page1 + page2
    assert _probe_ogg_duration_sec(data) == 2.0


@patch("app.media_ingest.get_media_url")
@patch("app.media_ingest.stream_download_meta")
@patch("app.media_ingest.upload_to_gcs")
def test_ingest_single_media_block_valid_image(mock_upload, mock_download, mock_url):
    """Test successful ingestion of a valid image."""
    mock_url.return_value = "https://meta.cdn/image.jpg"
    mock_download.return_value = (b"\xff\xd8\xff_image_content", 100, None)
    mock_upload.return_value = "gs://bucket/inbound/123.jpg"

    block = MediaBlock(media_id="msg_1", mime_type="image/jpeg")
    success, err, gcs_uri, mime = _ingest_single_media_block(
        "msg_1", "+96650", 0, block
    )

    assert success is True
    assert err is None
    assert gcs_uri == "gs://bucket/inbound/123.jpg"
    assert mime == "image/jpeg"


@patch("app.media_ingest.get_media_url")
@patch("app.media_ingest.stream_download_meta")
def test_ingest_single_media_block_invalid_magic_ogg(mock_download, mock_url):
    """Test rejection of voice note that lacks OggS magic bytes."""
    mock_url.return_value = "https://meta.cdn/voice.ogg"
    # Download content that does NOT start with OggS
    mock_download.return_value = (b"BAD_HEADER_xxxx", 100, None)

    block = MediaBlock(media_id="msg_2", mime_type="audio/ogg")
    success, err, gcs_uri, mime = _ingest_single_media_block(
        "msg_2", "+96650", 0, block
    )

    assert success is False
    assert "Could not process that audio file" in err
    assert gcs_uri is None
    assert mime is None


@patch("app.media_ingest.get_media_url")
@patch("app.media_ingest.stream_download_meta")
def test_ingest_single_media_block_duration_probe_failed(mock_download, mock_url):
    """Test rejection of voice note where duration probe fails."""
    mock_url.return_value = "https://meta.cdn/voice.ogg"
    # Has OggS but has a malformed format causing duration extraction to fail
    mock_download.return_value = (
        b"OggS_but_corrupted_data_without_valid_granules",
        100,
        None,
    )

    block = MediaBlock(media_id="msg_3", mime_type="audio/ogg")
    success, err, gcs_uri, mime = _ingest_single_media_block(
        "msg_3", "+96650", 0, block
    )

    assert success is False
    assert "Could not process that audio file" in err


@patch("app.media_ingest.get_media_url")
@patch("app.media_ingest.stream_download_meta")
def test_ingest_single_media_block_duration_exceeded(mock_download, mock_url):
    """Test rejection of voice note exceeding maximum duration limit."""
    mock_url.return_value = "https://meta.cdn/voice.ogg"

    # 360,000 granule at 48k is 7.5 seconds (mock limit 5.0 seconds for test or actual 300.0)
    # Wait, MAX_AUDIO_DURATION_SEC in app.config is 300.0 (5 min).
    # Let's generate Ogg bytes with duration > 300.0 (e.g. 15,000,000 granules = 312.5 seconds)
    import struct

    page1 = b"OggS" + b"\x00" * 22 + b"\x00"
    granule_bytes = struct.pack("<q", 15000000)
    page2 = b"OggS" + b"\x00\x02" + granule_bytes + b"\x00" * 12 + b"\x00"

    mock_download.return_value = (page1 + page2, 200, None)

    block = MediaBlock(media_id="msg_4", mime_type="audio/ogg")
    success, err, gcs_uri, mime = _ingest_single_media_block(
        "msg_4", "+96650", 0, block
    )

    assert success is False
    assert "Voice note too long" in err
