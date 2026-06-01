"""Unit tests for app/inbound_parser.py."""

from __future__ import annotations

import pytest

from app.inbound_parser import (
    _parse_timestamp,
    extract_messages_from_payload,
    normalize_webhook_message,
)
from app.models import InboundMessage, MediaBlock, TextBlock


def test_parse_timestamp_with_string():
    """Test timestamp parsing with string input."""
    ts = "1717000000"
    result = _parse_timestamp(ts)
    assert result is not None
    assert result.year == 2024


def test_parse_timestamp_with_int():
    """Test timestamp parsing with int input."""
    ts = 1717000000
    result = _parse_timestamp(ts)
    assert result is not None
    assert result.year == 2024


def test_parse_timestamp_none():
    """Test timestamp parsing with None input."""
    result = _parse_timestamp(None)
    assert result is not None


def test_parse_timestamp_invalid_string():
    """Test timestamp parsing with invalid string."""
    result = _parse_timestamp("invalid")
    assert result is not None


def test_normalize_text_message():
    """Test normalization of text-only message."""
    message = {
        "id": "wamid.123",
        "timestamp": "1717000000",
        "type": "text",
        "text": {"body": "Hello world"},
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert result.message_id == "wamid.123"
    assert result.phone_e164 == "+966500000001"
    assert result.member_id == "mem_001"
    assert len(result.content) == 1
    assert result.content[0].block_type == "text"
    assert result.content[0].text == "Hello world"


def test_normalize_image_message():
    """Test normalization of image message."""
    message = {
        "id": "wamid.456",
        "timestamp": "1717000000",
        "type": "image",
        "image": {"id": "media_123", "mime_type": "image/jpeg"},
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert len(result.content) == 1
    assert result.content[0].block_type == "media"
    assert result.content[0].media_id == "media_123"
    assert result.content[0].mime_type == "image/jpeg"


def test_normalize_image_with_caption():
    """Test normalization of image message with caption."""
    message = {
        "id": "wamid.789",
        "timestamp": "1717000000",
        "type": "image",
        "image": {
            "id": "media_456",
            "mime_type": "image/jpeg",
            "caption": "Here's a photo",
        },
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert len(result.content) == 2
    assert result.content[0].block_type == "text"
    assert result.content[0].text == "Here's a photo"
    assert result.content[1].block_type == "media"


def test_normalize_audio_message():
    """Test normalization of audio message."""
    message = {
        "id": "wamid.audio123",
        "timestamp": "1717000000",
        "type": "audio",
        "audio": {"id": "media_audio", "mime_type": "audio/ogg"},
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert len(result.content) == 1
    assert result.content[0].block_type == "media"
    assert result.content[0].media_id == "media_audio"


def test_normalize_video_message():
    """Test normalization of video message."""
    message = {
        "id": "wamid.video123",
        "timestamp": "1717000000",
        "type": "video",
        "video": {"id": "media_video", "mime_type": "video/mp4"},
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert result.content[0].block_type == "media"


def test_normalize_document_message():
    """Test normalization of document message."""
    message = {
        "id": "wamid.doc123",
        "timestamp": "1717000000",
        "type": "document",
        "document": {"id": "media_doc", "mime_type": "application/pdf"},
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert result.content[0].block_type == "media"


def test_normalize_interactive_button_reply():
    """Test normalization of interactive button reply."""
    message = {
        "id": "wamid.inter123",
        "timestamp": "1717000000",
        "type": "interactive",
        "interactive": {
            "button_reply": {"title": "Confirm", "id": "btn_1"},
        },
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert len(result.content) == 1
    assert result.content[0].block_type == "text"
    assert result.content[0].text == "Confirm"


def test_normalize_interactive_list_reply():
    """Test normalization of interactive list reply."""
    message = {
        "id": "wamid.list123",
        "timestamp": "1717000000",
        "type": "interactive",
        "interactive": {
            "list_reply": {"title": "Option A", "id": "opt_1"},
        },
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is not None
    assert result.content[0].text == "Option A"


def test_normalize_message_missing_id():
    """Test normalization fails when message_id is missing."""
    message = {
        "timestamp": "1717000000",
        "type": "text",
        "text": {"body": "Test"},
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is None


def test_normalize_unsupported_type():
    """Test normalization fails for unsupported message type."""
    message = {
        "id": "wamid.999",
        "timestamp": "1717000000",
        "type": "unknown_type",
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is None


def test_normalize_empty_text():
    """Test normalization fails when text body is empty."""
    message = {
        "id": "wamid.empty",
        "timestamp": "1717000000",
        "type": "text",
        "text": {"body": ""},
    }
    result = normalize_webhook_message(message, "+966500000001", "mem_001")
    assert result is None


def test_extract_messages_from_payload():
    """Test extraction of messages from webhook payload."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000001"}],
                            "messages": [
                                {
                                    "id": "wamid.111",
                                    "from": "966500000001",
                                    "type": "text",
                                    "text": {"body": "Test"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    results = extract_messages_from_payload(payload)
    assert len(results) == 1
    phone, message = results[0]
    assert phone == "+966500000001"
    assert message["id"] == "wamid.111"


def test_extract_messages_phone_normalization():
    """Test phone number normalization (adding + prefix)."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000001"}],
                            "messages": [
                                {
                                    "id": "wamid.222",
                                    "from": "966500000001",
                                    "type": "text",
                                    "text": {"body": "Test"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    results = extract_messages_from_payload(payload)
    phone, _ = results[0]
    assert phone.startswith("+")


def test_extract_messages_with_plus_prefix():
    """Test phone number already has + prefix."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000001"}],
                            "messages": [
                                {
                                    "id": "wamid.333",
                                    "from": "+966500000001",
                                    "type": "text",
                                    "text": {"body": "Test"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    results = extract_messages_from_payload(payload)
    phone, _ = results[0]
    assert phone == "+966500000001"


def test_extract_messages_multiple():
    """Test extraction of multiple messages."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": "966500000001"},
                                {"wa_id": "966500000002"},
                            ],
                            "messages": [
                                {
                                    "id": "wamid.444",
                                    "from": "966500000001",
                                    "type": "text",
                                    "text": {"body": "Test 1"},
                                },
                                {
                                    "id": "wamid.555",
                                    "from": "966500000002",
                                    "type": "text",
                                    "text": {"body": "Test 2"},
                                },
                            ],
                        }
                    }
                ]
            }
        ]
    }
    results = extract_messages_from_payload(payload)
    assert len(results) == 2


def test_extract_messages_empty_payload():
    """Test extraction with empty payload."""
    payload = {"entry": []}
    results = extract_messages_from_payload(payload)
    assert len(results) == 0


def test_extract_messages_missing_from():
    """Test extraction when message missing 'from' field."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000001"}],
                            "messages": [{"id": "wamid.666", "type": "text"}],
                        }
                    }
                ]
            }
        ]
    }
    results = extract_messages_from_payload(payload)
    assert len(results) == 0
