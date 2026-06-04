"""Unit tests for app/models.py."""

from __future__ import annotations

from datetime import datetime


from app.config import RIYADH_TZ
from app.models import (
    ContentBlock,
    InboundMessage,
    MediaBlock,
    Member,
    PendingConfirmation,
    TextBlock,
)


def test_text_block_creation():
    """Test TextBlock model creation."""
    block = TextBlock(text="Hello world")
    assert block.block_type == "text"
    assert block.text == "Hello world"


def test_media_block_creation():
    """Test MediaBlock model creation."""
    block = MediaBlock(
        media_id="media_123",
        mime_type="image/jpeg",
        gcs_uri="gs://bucket/path.jpg",
        normalized_mime_type="image/jpeg",
    )
    assert block.block_type == "media"
    assert block.media_id == "media_123"
    assert block.mime_type == "image/jpeg"
    assert block.gcs_uri == "gs://bucket/path.jpg"
    assert block.normalized_mime_type == "image/jpeg"


def test_media_block_optional_fields():
    """Test MediaBlock with optional fields as None."""
    block = MediaBlock(media_id="media_123", mime_type="image/jpeg")
    assert block.gcs_uri is None
    assert block.normalized_mime_type is None


def test_inbound_message_creation():
    """Test InboundMessage model creation."""
    now = datetime.now(RIYADH_TZ)
    message = InboundMessage(
        message_id="wamid.123",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=now,
        content=[TextBlock(text="Test")],
    )
    assert message.message_id == "wamid.123"
    assert message.phone_e164 == "+966500000001"
    assert message.member_id == "mem_001"
    assert message.received_at == now
    assert len(message.content) == 1
    assert message.content[0].text == "Test"


def test_inbound_message_mixed_content():
    """Test InboundMessage with mixed text and media content."""
    now = datetime.now(RIYADH_TZ)
    message = InboundMessage(
        message_id="wamid.456",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=now,
        content=[
            TextBlock(text="Caption"),
            MediaBlock(media_id="media_123", mime_type="image/jpeg"),
        ],
    )
    assert len(message.content) == 2
    assert message.content[0].block_type == "text"
    assert message.content[1].block_type == "media"


def test_inbound_message_model_dump_firestore():
    """Test InboundMessage serialization for Firestore."""
    now = datetime.now(RIYADH_TZ)
    message = InboundMessage(
        message_id="wamid.789",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=now,
        content=[TextBlock(text="Test")],
    )
    dumped = message.model_dump_firestore()
    assert dumped["message_id"] == "wamid.789"
    assert dumped["phone_e164"] == "+966500000001"
    assert dumped["member_id"] == "mem_001"
    assert "received_at" in dumped
    assert isinstance(dumped["content"], list)


def test_member_creation():
    """Test Member model creation."""
    member = Member(
        member_id="mem_001",
        phone_e164="+966500000001",
        name="Test User",
        role="tier1",
        capabilities=["admin"],
        active=True,
        preferred_language="en",
    )
    assert member.member_id == "mem_001"
    assert member.phone_e164 == "+966500000001"
    assert member.name == "Test User"
    assert member.role == "tier1"
    assert member.capabilities == ["admin"]
    assert member.active is True
    assert member.preferred_language == "en"


def test_member_defaults():
    """Test Member model with default values."""
    member = Member(
        member_id="mem_002",
        phone_e164="+966500000002",
        name="Default User",
        role="tier2",
    )
    assert member.capabilities == []
    assert member.active is True
    assert member.preferred_language == "en"


def test_pending_confirmation_creation():
    """Test PendingConfirmation model creation."""
    now = datetime.now(RIYADH_TZ)
    expires = now.replace(hour=now.hour + 1)
    confirmation = PendingConfirmation(
        confirmation_id="conf_001",
        action="create_adhoc_task",
        payload={"task": "test"},
        summary="Create test task",
        status="active",
        created_at=now,
        expires_at=expires,
    )
    assert confirmation.confirmation_id == "conf_001"
    assert confirmation.action == "create_adhoc_task"
    assert confirmation.payload == {"task": "test"}
    assert confirmation.summary == "Create test task"
    assert confirmation.status == "active"
    assert confirmation.created_at == now
    assert confirmation.expires_at == expires


def test_pending_confirmation_default_status():
    """Test PendingConfirmation with default status."""
    now = datetime.now(RIYADH_TZ)
    confirmation = PendingConfirmation(
        confirmation_id="conf_002",
        action="test_action",
        payload={},
        summary="Test",
        created_at=now,
        expires_at=now,
    )
    assert confirmation.status == "active"


def test_content_block_union():
    """Test ContentBlock union type."""
    text_block: ContentBlock = TextBlock(text="Hello")
    media_block: ContentBlock = MediaBlock(media_id="123", mime_type="image/jpeg")

    assert text_block.block_type == "text"
    assert media_block.block_type == "media"


def test_member_role_validation():
    """Test Member role enum validation."""
    member1 = Member(
        member_id="mem_001",
        phone_e164="+966500000001",
        name="User 1",
        role="tier1",
    )
    assert member1.role == "tier1"

    member2 = Member(
        member_id="mem_002",
        phone_e164="+966500000002",
        name="User 2",
        role="tier2",
    )
    assert member2.role == "tier2"
