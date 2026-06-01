"""Unit tests for app/inbound_parser.py (Telegram updates)."""

from __future__ import annotations

import pytest
from datetime import datetime

from app.inbound_parser import (
    _parse_timestamp,
    normalize_telegram_message,
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


def test_normalize_telegram_text_message():
    """Test normalization of Telegram text-only message."""
    update = {
        "update_id": 10001,
        "message": {
            "message_id": 999,
            "date": 1717000000,
            "text": "Hello world",
            "chat": {"id": 1221020259},
        },
    }
    result = normalize_telegram_message(update, "mem_001", "+966506667785")
    assert result is not None
    assert result.message_id == "tg_msg_999"
    assert result.phone_e164 == "+966506667785"
    assert result.member_id == "mem_001"
    assert len(result.content) == 1
    assert result.content[0].block_type == "text"
    assert result.content[0].text == "Hello world"


def test_normalize_telegram_callback_query():
    """Test normalization of Telegram callback query (button click)."""
    update = {
        "update_id": 10002,
        "callback_query": {
            "id": "cb123",
            "message": {
                "message_id": 888,
                "chat": {"id": 1221020259},
            },
            "data": "approve_task_123",
        },
    }
    result = normalize_telegram_message(update, "mem_001", "+966506667785")
    assert result is not None
    assert result.message_id == "tg_cb_cb123"
    assert len(result.content) == 1
    assert result.content[0].block_type == "text"
    assert result.content[0].text == "approve_task_123"


def test_normalize_telegram_photo_message():
    """Test normalization of Telegram photo message (uses largest photo size)."""
    update = {
        "update_id": 10003,
        "message": {
            "message_id": 777,
            "date": 1717000000,
            "photo": [
                {"file_id": "small_id", "file_size": 100},
                {"file_id": "large_id", "file_size": 1000},
            ],
            "caption": "Check this photo",
            "chat": {"id": 1221020259},
        },
    }
    result = normalize_telegram_message(update, "mem_001", "+966506667785")
    assert result is not None
    assert len(result.content) == 2
    assert result.content[0].block_type == "text"
    assert result.content[0].text == "Check this photo"
    assert result.content[1].block_type == "media"
    assert result.content[1].media_id == "large_id"
    assert result.content[1].mime_type == "image/jpeg"


def test_normalize_telegram_voice_message():
    """Test normalization of Telegram voice message."""
    update = {
        "update_id": 10004,
        "message": {
            "message_id": 666,
            "date": 1717000000,
            "voice": {
                "file_id": "voice_123",
                "mime_type": "audio/ogg",
            },
            "chat": {"id": 1221020259},
        },
    }
    result = normalize_telegram_message(update, "mem_001", "+966506667785")
    assert result is not None
    assert len(result.content) == 1
    assert result.content[0].block_type == "media"
    assert result.content[0].media_id == "voice_123"
    assert result.content[0].mime_type == "audio/ogg"


def test_normalize_telegram_unsupported_message():
    """Test that unsupported types return None."""
    update = {
        "update_id": 10005,
        "message": {
            "message_id": 555,
            "date": 1717000000,
            # no text, photo, voice, document, etc.
            "chat": {"id": 1221020259},
        },
    }
    result = normalize_telegram_message(update, "mem_001", "+966506667785")
    assert result is None
