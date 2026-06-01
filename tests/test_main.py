"""Integration tests for main.py endpoints."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    """Test client for FastAPI app."""
    return TestClient(app)


def test_health_endpoint(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["phase"] == 1
    assert "prefix_token_count" in data


def test_whatsapp_verify_success(client):
    """Test WhatsApp webhook verification with valid token."""
    with patch("main.WHATSAPP_VERIFY_TOKEN", "test_token"):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test_token",
                "hub.challenge": "challenge123",
            },
        )
        assert response.status_code == 200
        assert response.text == "challenge123"


def test_whatsapp_verify_invalid_token(client):
    """Test WhatsApp webhook verification with invalid token."""
    with patch("main.WHATSAPP_VERIFY_TOKEN", "correct_token"):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "challenge123",
            },
        )
        assert response.status_code == 403


def test_whatsapp_verify_missing_mode(client):
    """Test WhatsApp webhook verification missing hub.mode."""
    response = client.get("/webhook/whatsapp")
    assert response.status_code == 403


def test_whatsapp_webhook_status_update(client):
    """Test WhatsApp webhook with status update (no messages)."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [{"id": "wamid.123", "status": "delivered"}],
                        }
                    }
                ]
            }
        ]
    }
    
    with patch("main.verify_signature", return_value=True):
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200


def test_whatsapp_webhook_invalid_signature(client):
    """Test WhatsApp webhook with invalid signature."""
    payload = {"entry": [{"changes": [{"value": {"messages": []}}]}]}
    
    with patch("main.verify_signature", return_value=False):
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 401


def test_whatsapp_webhook_valid_message(client):
    """Test WhatsApp webhook with valid message."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000001"}],
                            "messages": [
                                {
                                    "id": "wamid.test123",
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
    
    with patch("main.verify_signature", return_value=True), \
         patch("main.claim_idempotency_key", return_value=True), \
         patch("main.get_db") as mock_get_db, \
         patch("main.lookup_member_by_phone") as mock_lookup, \
         patch("main.normalize_webhook_message") as mock_normalize, \
         patch("main.enqueue_inbound_processing") as mock_enqueue:
        
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        
        mock_member = Mock()
        mock_member.member_id = "mem_001"
        mock_lookup.return_value = mock_member
        
        mock_inbound = Mock()
        mock_inbound.message_id = "wamid.test123"
        mock_normalize.return_value = mock_inbound
        
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200
    mock_enqueue.assert_called_once()


def test_whatsapp_webhook_duplicate_message(client):
    """Test WhatsApp webhook with duplicate message (idempotency)."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966500000001"}],
                            "messages": [
                                {
                                    "id": "wamid.duplicate",
                                    "from": "966500000001",
                                    "timestamp": "1717000000",
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
    
    with patch("main.verify_signature", return_value=True), \
         patch("main.get_db") as mock_get_db, \
         patch("main.claim_idempotency_key", return_value=False):
        
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200


def test_whatsapp_webhook_unauthorized_phone(client):
    """Test WhatsApp webhook from unauthorized phone number."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "966999999999"}],
                            "messages": [
                                {
                                    "id": "wamid.unauth",
                                    "from": "966999999999",
                                    "timestamp": "1717000000",
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
    
    with patch("main.verify_signature", return_value=True), \
         patch("main.get_db") as mock_get_db, \
         patch("main.claim_idempotency_key", return_value=True), \
         patch("main.lookup_member_by_phone", return_value=None):
        
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        
        response = client.post(
            "/webhook/whatsapp",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200


def test_process_inbound_valid_envelope(client):
    """Test process-inbound endpoint with valid envelope."""
    from app.models import InboundMessage, TextBlock
    from datetime import datetime
    from app.config import RIYADH_TZ
    
    inbound = InboundMessage(
        message_id="wamid.process123",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[TextBlock(text="Test")],
    )
    
    with patch("main.get_db") as mock_get_db, \
         patch("main.lookup_member_by_phone") as mock_lookup, \
         patch("main.ensure_conversation_doc"), \
         patch("main.ingest_media_blocks", return_value=(True, None)), \
         patch("main.run_confirmation_gate") as mock_gate, \
         patch("main.compile_conversation_history", return_value=("", {})), \
         patch("main.run_agent_turn", return_value=("Reply", {})), \
         patch("main.send_text_message"), \
         patch("main.write_message_turn"):
        
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        
        mock_member = Mock()
        mock_member.member_id = "mem_001"
        mock_member.name = "Test User"
        mock_member.role = "tier1"
        mock_member.capabilities = []
        mock_lookup.return_value = mock_member
        
        mock_gate_result = Mock()
        mock_gate_result.handled = False
        mock_gate_result.proceed_to_gemini = True
        mock_gate_result.session_note = None
        mock_gate.return_value = mock_gate_result
        
        response = client.post(
            "/tasks/process-inbound",
            content=inbound.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_process_inbound_invalid_envelope(client):
    """Test process-inbound endpoint with invalid envelope."""
    response = client.post(
        "/tasks/process-inbound",
        content="invalid json",
        headers={"Content-Type": "application/json"},
    )
    
    assert response.status_code == 400


def test_process_inbound_member_mismatch(client):
    """Test process-inbound with member ID mismatch."""
    from app.models import InboundMessage, TextBlock
    from datetime import datetime
    from app.config import RIYADH_TZ
    
    inbound = InboundMessage(
        message_id="wamid.mismatch",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[TextBlock(text="Test")],
    )
    
    with patch("main.get_db") as mock_get_db, \
         patch("main.lookup_member_by_phone") as mock_lookup:
        
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_member = Mock()
        mock_member.member_id = "mem_002"  # Different from inbound.member_id
        mock_lookup.return_value = mock_member
        
        response = client.post(
            "/tasks/process-inbound",
            content=inbound.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "skipped"
    assert data["reason"] == "unauthorized"


def test_process_inbound_media_failed(client):
    """Test process-inbound when media ingest fails."""
    from app.models import InboundMessage, TextBlock
    from datetime import datetime
    from app.config import RIYADH_TZ
    
    inbound = InboundMessage(
        message_id="wamid.media_fail",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[TextBlock(text="Test")],
    )
    
    with patch("main.get_db") as mock_get_db, \
         patch("main.lookup_member_by_phone") as mock_lookup, \
         patch("main.ensure_conversation_doc"), \
         patch("main.ingest_media_blocks", return_value=(False, "Media error")), \
         patch("main.send_text_message"):
        
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        
        mock_member = Mock()
        mock_member.member_id = "mem_001"
        mock_lookup.return_value = mock_member
        
        response = client.post(
            "/tasks/process-inbound",
            content=inbound.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "media_failed"


def test_process_inbound_gate_handled(client):
    """Test process-inbound when confirmation gate handles the request."""
    from app.models import InboundMessage, TextBlock
    from datetime import datetime
    from app.config import RIYADH_TZ
    
    inbound = InboundMessage(
        message_id="wamid.gate_handled",
        phone_e164="+966500000001",
        member_id="mem_001",
        received_at=datetime.now(RIYADH_TZ),
        content=[TextBlock(text="yes")],
    )
    
    with patch("main.get_db") as mock_get_db, \
         patch("main.lookup_member_by_phone") as mock_lookup, \
         patch("main.ensure_conversation_doc"), \
         patch("main.ingest_media_blocks", return_value=(True, None)), \
         patch("main.run_confirmation_gate") as mock_gate, \
         patch("main.send_text_message"), \
         patch("main.write_message_turn"):
        
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        
        mock_member = Mock()
        mock_member.member_id = "mem_001"
        mock_member.name = "Test User"
        mock_member.role = "tier1"
        mock_member.capabilities = []
        mock_lookup.return_value = mock_member
        
        mock_gate_result = Mock()
        mock_gate_result.handled = True
        mock_gate_result.proceed_to_gemini = False
        mock_gate_result.reply_text = "Confirmed"
        mock_gate_result.session_note = None
        mock_gate.return_value = mock_gate_result
        
        response = client.post(
            "/tasks/process-inbound",
            content=inbound.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "gate_handled"
