"""Unit tests for app/confirmation_gate.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest

from app.confirmation_gate import (
    _classify_intent,
    _extract_inbound_text,
    _expire_if_needed,
    _pop_paused_confirmation,
    run_confirmation_gate,
)
from app.config import RIYADH_TZ
from app.firestore_db import set_pending_confirmation
from app.models import InboundMessage, PendingConfirmation, TextBlock


def test_extract_inbound_text():
    """Test extraction of text from inbound message."""
    message = InboundMessage(
        message_id="wamid.123",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[TextBlock(text="Hello world")],
    )
    result = _extract_inbound_text(message)
    assert result == "Hello world"


def test_extract_inbound_text_multiple_blocks():
    """Test extraction of text from message with multiple blocks."""
    message = InboundMessage(
        message_id="wamid.456",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[
            TextBlock(text="First"),
            TextBlock(text="Second"),
        ],
    )
    result = _extract_inbound_text(message)
    assert result == "First Second"


def test_extract_inbound_text_empty():
    """Test extraction when no text blocks present."""
    from app.models import MediaBlock

    message = InboundMessage(
        message_id="wamid.789",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[MediaBlock(media_id="media_123", mime_type="image/jpeg")],
    )
    result = _extract_inbound_text(message)
    assert result == ""


def test_classify_intent_confirm():
    """Test classification of confirm intent."""
    assert _classify_intent("yes") == "CONFIRM"
    assert _classify_intent("Yes") == "CONFIRM"
    assert _classify_intent("Y") == "CONFIRM"
    assert _classify_intent("confirm") == "CONFIRM"
    assert _classify_intent("ok") == "CONFIRM"
    assert _classify_intent("نعم") == "CONFIRM"


def test_classify_intent_reject():
    """Test classification of reject intent."""
    assert _classify_intent("no") == "REJECT"
    assert _classify_intent("No") == "REJECT"
    assert _classify_intent("N") == "REJECT"
    assert _classify_intent("cancel") == "REJECT"
    assert _classify_intent("stop") == "REJECT"
    assert _classify_intent("لا") == "REJECT"


def test_classify_intent_resume():
    """Test classification of resume intent."""
    assert _classify_intent("resume") == "RESUME"
    assert _classify_intent("Resume") == "RESUME"
    assert _classify_intent("continue") == "RESUME"
    assert _classify_intent("استئناف") == "RESUME"


def test_classify_intent_unrelated():
    """Test classification of unrelated intent."""
    assert _classify_intent("hello") == "UNRELATED"
    assert _classify_intent("what is the weather") == "UNRELATED"
    assert _classify_intent("") == "UNRELATED"
    assert _classify_intent(None) == "UNRELATED"


def test_expire_if_needed_expired(mock_firestore_client):
    """Test expiration of pending confirmation."""
    phone = "+966500000001"
    now = datetime.now(RIYADH_TZ)
    past = now - timedelta(hours=1)
    
    pending = PendingConfirmation(
        confirmation_id="conf_001",
        action="create_adhoc_task",
        payload={},
        summary="Test",
        status="active",
        created_at=past,
        expires_at=past,
    )
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    result = _expire_if_needed(mock_firestore_client, phone, pending)
    
    assert result is True
    mock_ref.update.assert_called_once()


def test_expire_if_needed_not_expired(mock_firestore_client):
    """Test when pending confirmation is not expired."""
    phone = "+966500000001"
    now = datetime.now(RIYADH_TZ)
    future = now + timedelta(hours=1)
    
    pending = PendingConfirmation(
        confirmation_id="conf_002",
        action="create_adhoc_task",
        payload={},
        summary="Test",
        status="active",
        created_at=now,
        expires_at=future,
    )
    
    result = _expire_if_needed(mock_firestore_client, phone, pending)
    
    assert result is False


def test_pop_paused_confirmation(mock_firestore_client):
    """Test restoring paused confirmation."""
    phone = "+966500000001"
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {
        "paused_confirmations": [
            {
                "confirmation_id": "conf_003",
                "action": "create_adhoc_task",
                "payload": {"task": "test"},
                "summary": "Paused task",
                "paused_at": datetime.now(RIYADH_TZ).isoformat(),
                "pause_reason": "user_pivot",
            }
        ],
    }
    mock_ref.get.return_value = mock_snap
    
    result = _pop_paused_confirmation(mock_firestore_client, phone)
    
    assert result is not None
    assert result.confirmation_id == "conf_003"
    assert result.action == "create_adhoc_task"
    mock_ref.update.assert_called_once()


def test_pop_paused_confirmation_empty_stack(mock_firestore_client):
    """Test when no paused confirmations exist."""
    phone = "+966500000001"
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"paused_confirmations": []}
    mock_ref.get.return_value = mock_snap
    
    result = _pop_paused_confirmation(mock_firestore_client, phone)
    
    assert result is None


def test_run_confirmation_gate_resume_command(mock_firestore_client, sample_text_message):
    """Test resume command handling."""
    phone = "+966500000001"
    
    message = InboundMessage(
        message_id="wamid.resume",
        phone_e164=phone,
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[TextBlock(text="resume")],
    )
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {
        "paused_confirmations": [
            {
                "confirmation_id": "conf_004",
                "action": "create_adhoc_task",
                "payload": {},
                "summary": "Test task",
                "paused_at": datetime.now(RIYADH_TZ).isoformat(),
                "pause_reason": "user_pivot",
            }
        ],
    }
    mock_ref.get.return_value = mock_snap
    
    result = run_confirmation_gate(mock_firestore_client, phone, message)
    
    assert result.handled is True
    assert result.proceed_to_gemini is False
    assert "Resumed" in result.reply_text


def test_run_confirmation_gate_no_pending(mock_firestore_client, sample_text_message):
    """Test when no pending confirmation exists."""
    phone = "+966500000001"
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"pending_confirmation": None}
    mock_ref.get.return_value = mock_snap
    
    result = run_confirmation_gate(mock_firestore_client, phone, sample_text_message)
    
    assert result.proceed_to_gemini is True
    assert result.handled is False


def test_run_confirmation_gate_confirm_action(mock_firestore_client):
    """Test confirmation of pending action."""
    phone = "+966500000001"
    now = datetime.now(RIYADH_TZ)
    
    message = InboundMessage(
        message_id="wamid.confirm",
        phone_e164=phone,
        member_id="mem_001",
        received_at=now,
        content=[TextBlock(text="yes")],
    )
    
    pending = PendingConfirmation(
        confirmation_id="conf_005",
        action="create_adhoc_task",
        payload={"task": "test"},
        summary="Create test task",
        status="active",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"pending_confirmation": pending.model_dump(mode="json")}
    mock_ref.get.return_value = mock_snap
    
    with patch("app.confirmation_gate.execute_pending_create_adhoc") as mock_execute:
        mock_execute.return_value = {"ok": True, "task_id": "task_123"}
        
        result = run_confirmation_gate(mock_firestore_client, phone, message)
    
    assert result.handled is True
    assert result.proceed_to_gemini is False
    # execute_pending_create_adhoc returns "Task created" message
    assert "Task created" in result.reply_text


def test_run_confirmation_gate_reject_action(mock_firestore_client):
    """Test rejection of pending action."""
    phone = "+966500000001"
    now = datetime.now(RIYADH_TZ)
    
    message = InboundMessage(
        message_id="wamid.reject",
        phone_e164=phone,
        member_id="mem_001",
        received_at=now,
        content=[TextBlock(text="no")],
    )
    
    pending = PendingConfirmation(
        confirmation_id="conf_006",
        action="create_adhoc_task",
        payload={},
        summary="Test task",
        status="active",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"pending_confirmation": pending.model_dump(mode="json")}
    mock_ref.get.return_value = mock_snap
    
    result = run_confirmation_gate(mock_firestore_client, phone, message)
    
    assert result.handled is True
    assert result.proceed_to_gemini is False
    assert "Cancelled" in result.reply_text


def test_run_confirmation_gate_emergency_keyword(mock_firestore_client):
    """Test emergency keyword discards pending confirmation."""
    phone = "+966500000001"
    now = datetime.now(RIYADH_TZ)
    
    message = InboundMessage(
        message_id="wamid.emergency",
        phone_e164=phone,
        member_id="mem_001",
        received_at=now,
        content=[TextBlock(text="There's a fire in the kitchen")],
    )
    
    pending = PendingConfirmation(
        confirmation_id="conf_007",
        action="create_adhoc_task",
        payload={},
        summary="Test task",
        status="active",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"pending_confirmation": pending.model_dump(mode="json")}
    mock_ref.get.return_value = mock_snap
    
    result = run_confirmation_gate(mock_firestore_client, phone, message)
    
    assert result.proceed_to_gemini is True
    assert result.handled is False
    assert result.session_note is not None
    assert "priority topic" in result.session_note


def test_run_confirmation_gate_unrelated_pause(mock_firestore_client):
    """Test unrelated message pauses pending confirmation."""
    phone = "+966500000001"
    now = datetime.now(RIYADH_TZ)
    
    message = InboundMessage(
        message_id="wamid.unrelated",
        phone_e164=phone,
        member_id="mem_001",
        received_at=now,
        content=[TextBlock(text="What's for dinner?")],
    )
    
    pending = PendingConfirmation(
        confirmation_id="conf_008",
        action="create_adhoc_task",
        payload={},
        summary="Test task",
        status="active",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"pending_confirmation": pending.model_dump(mode="json")}
    mock_ref.get.return_value = mock_snap
    
    result = run_confirmation_gate(mock_firestore_client, phone, message)
    
    assert result.proceed_to_gemini is True
    assert result.handled is False
    assert result.session_note is not None
    assert "paused" in result.session_note.lower()
