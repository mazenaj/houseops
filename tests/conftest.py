"""Pytest fixtures for HouseOps testing."""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock, Mock

import pytest
from google.cloud.firestore import Client as FirestoreClient

from app.config import RIYADH_TZ
from app.models import InboundMessage, Member, MediaBlock, TextBlock


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
def sample_staff_member():
    """Sample staff member fixture (Tier 2)."""
    return Member(
        member_id="mem_staff_001",
        phone_e164="+966502644515",
        name="Lee (Nanny)",
        role="tier2",
        capabilities=["housemaid"],
        active=True,
        preferred_language="en",
        telegram_chat_id=1221020260,
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


@pytest.fixture
def sample_media_message():
    """Sample media inbound message."""
    now = datetime.now(RIYADH_TZ)
    return InboundMessage(
        message_id=f"wamid.{uuid.uuid4().hex}",
        phone_e164="+966500000001",
        member_id="mem_test_001",
        received_at=now,
        content=[
            TextBlock(text="Here is a photo"),
            MediaBlock(
                media_id="media_123",
                mime_type="image/jpeg",
                gcs_uri=None,
                normalized_mime_type=None,
            ),
        ],
    )


@pytest.fixture
def sample_webhook_payload():
    """Sample WhatsApp webhook payload."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000001"}],
                            "messages": [
                                {
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "from": "966500000001",
                                    "timestamp": "1717000000",
                                    "type": "text",
                                    "text": {"body": "Test message"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


@pytest.fixture
def mock_cloud_tasks_client():
    """Mock Cloud Tasks client."""
    from google.cloud import tasks_v2

    client = MagicMock(spec=tasks_v2.CloudTasksClient)
    mock_response = Mock()
    mock_response.name = "projects/test/locations/me-central1/queues/test/tasks/task123"
    client.create_task.return_value = mock_response
    return client


@pytest.fixture
def mock_storage_client():
    """Mock Cloud Storage client."""
    from google.cloud import storage

    client = MagicMock(spec=storage.Client)
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob
    return client


@pytest.fixture
def mock_httpx_client():
    """Mock httpx client for WhatsApp API calls."""
    client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"url": "https://example.com/media"}
    mock_response.raise_for_status = MagicMock()
    client.get.return_value = mock_response
    client.post.return_value = mock_response
    return client
