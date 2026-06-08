"""Pytest fixtures for HouseOps testing."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "mock_token")
os.environ.setdefault("TELEGRAM_OPS_BOT_TOKEN", "mock_ops_token")

import uuid  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from google.cloud.firestore import Client as FirestoreClient  # noqa: E402

from app.config import RIYADH_TZ  # noqa: E402
from app.models import InboundMessage, Member, TextBlock  # noqa: E402


@pytest.fixture
def mock_firestore_client():
    """Mock Firestore client."""
    client = MagicMock(spec=FirestoreClient)
    return client


@pytest.fixture
def sample_member():
    """Sample member fixture."""
    return Member(
        member_id="mem_test_001",
        phone_e164="+966500000001",
        name="Test User",
        role="tier1",
        capabilities=[],
        active=True,
        preferred_language="en",
        telegram_chat_id=1221020259,
    )


@pytest.fixture
def sample_text_message():
    """Sample text-only inbound message."""
    now = datetime.now(RIYADH_TZ)
    return InboundMessage(
        message_id=f"wamid.{uuid.uuid4().hex}",
        phone_e164="+966500000001",
        member_id="mem_test_001",
        received_at=now,
        content=[TextBlock(text="Hello, this is a test message")],
    )
