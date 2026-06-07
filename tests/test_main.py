"""Integration tests for main.py endpoints (Telegram bot)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


def test_telegram_webhook_secret_invalid(client):
    """Test Telegram webhook with invalid secret token."""
    payload = {"update_id": 10001}
    with patch("main.verify_webhook_secret", return_value=False):
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "bad_secret"},
        )
        assert response.status_code == 403


def test_telegram_webhook_contact_onboarding_success(client, sample_member):
    """Test one-time contact share authentication success."""
    payload = {
        "update_id": 10002,
        "message": {
            "message_id": 999,
            "chat": {"id": 1221020259},
            "contact": {
                "phone_number": "966500000001",
                "first_name": "Test User",
            },
        },
    }
    with patch("main.verify_webhook_secret", return_value=True), patch(
        "main.get_db"
    ), patch("main.lookup_member_by_phone", return_value=sample_member), patch(
        "main.link_telegram_chat_id", return_value=True
    ) as mock_link, patch("main.send_text_message") as mock_send:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_link.assert_called_once_with(any_mock_value(), "+966500000001", 1221020259)
        mock_send.assert_called_once()
        assert "Welcome" in mock_send.call_args[0][1]


def test_telegram_webhook_contact_onboarding_unauthorized(client):
    """Test one-time contact share auth failure for unwhitelisted contact."""
    payload = {
        "update_id": 10003,
        "message": {
            "message_id": 998,
            "chat": {"id": 1221020259},
            "contact": {
                "phone_number": "966999999999",
                "first_name": "Unknown",
            },
        },
    }
    with patch("main.verify_webhook_secret", return_value=True), patch(
        "main.get_db"
    ), patch("main.lookup_member_by_phone", return_value=None), patch(
        "main.send_text_message"
    ) as mock_send:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_send.assert_called_once()
        assert "Access Denied" in mock_send.call_args[0][1]


def test_telegram_webhook_unauthorized_chat_id(client):
    """Test that message from unrecognized chat_id triggers contact share request."""
    payload = {
        "update_id": 10004,
        "message": {
            "message_id": 997,
            "chat": {"id": 1221020299},
            "text": "Hello",
        },
    }
    with patch("main.verify_webhook_secret", return_value=True), patch(
        "main.get_db"
    ), patch("main.lookup_member_by_telegram_chat_id", return_value=None), patch(
        "main.request_contact_share"
    ) as mock_request:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_request.assert_called_once_with(1221020299, any_mock_value())


def test_telegram_webhook_duplicate_message(client, sample_member):
    """Test duplicate message deduplication (idempotency)."""
    payload = {
        "update_id": 10005,
        "message": {
            "message_id": 996,
            "chat": {"id": 1221020259},
            "text": "Hello again",
        },
    }
    with patch("main.verify_webhook_secret", return_value=True), patch(
        "main.get_db"
    ), patch(
        "main.lookup_member_by_telegram_chat_id", return_value=sample_member
    ), patch("main.claim_idempotency_key", return_value=False) as mock_claim:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_claim.assert_called_once()


def test_telegram_webhook_valid_message(client, sample_member):
    """Test successful enqueuing of standard whitelisted message."""
    payload = {
        "update_id": 10006,
        "message": {
            "message_id": 995,
            "chat": {"id": 1221020259},
            "text": "This is a real message",
        },
    }
    with patch("main.verify_webhook_secret", return_value=True), patch(
        "main.get_db"
    ), patch(
        "main.lookup_member_by_telegram_chat_id", return_value=sample_member
    ), patch("main.claim_idempotency_key", return_value=True), patch(
        "main.enqueue_inbound_processing"
    ) as mock_enqueue:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_enqueue.assert_called_once()


@patch("main.get_db")
@patch("main.lookup_member_by_phone")
@patch("main.ingest_media_blocks")
@patch("main.run_confirmation_gate")
@patch("main.compile_conversation_history")
@patch("main.run_agent_turn")
@patch("main.send_text_message")
@patch("main.verify_job_secret")
def test_process_inbound_success(
    mock_verify_secret,
    mock_send,
    mock_agent_turn,
    mock_history,
    mock_gate,
    mock_media,
    mock_lookup_phone,
    mock_get_db,
    client,
    sample_text_message,
    sample_member,
):
    """Test process-inbound background task worker success path."""
    mock_verify_secret.return_value = True
    mock_lookup_phone.return_value = sample_member
    mock_media.return_value = (True, None)

    mock_gate_result = MagicMock()
    mock_gate_result.handled = False
    mock_gate_result.reply_text = None
    mock_gate_result.proceed_to_gemini = True
    mock_gate_result.session_note = None
    mock_gate.return_value = mock_gate_result

    mock_history.return_value = ("history text", {"final_token_count": 100})
    mock_agent_turn.return_value = ("Gemini reply message", {"total_tokens": 150})

    response = client.post(
        "/tasks/process-inbound",
        json=sample_text_message.model_dump_firestore(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    mock_send.assert_called_once_with(1221020259, "Gemini reply message")


def any_mock_value():
    """Helper mock matcher that matches anything."""

    class AnyValue:
        def __eq__(self, other):
            return True

    return AnyValue()


def test_cleanup_messages_invalid_secret(client):
    """Test cleanup-messages endpoint with invalid secret."""
    with patch("main.TELEGRAM_BOT_TOKEN", "mock_token"):
        response = client.post(
            "/jobs/cleanup-messages",
            headers={"X-HouseOps-Secret-Token": "wrong_secret"},
        )
        assert response.status_code == 403


def test_cleanup_messages_success(client):
    """Test cleanup-messages endpoint success path."""
    import hashlib
    from datetime import datetime, timedelta
    from app.config import RIYADH_TZ

    mock_token = "mock_token"
    expected_secret = hashlib.sha256(mock_token.encode("utf-8")).hexdigest()

    mock_db = MagicMock()
    mock_conv1 = MagicMock()
    mock_conv1.id = "+966506667785"
    mock_db.collection.return_value.stream.return_value = [mock_conv1]

    mock_msg1 = MagicMock()
    mock_msg_ref = MagicMock()
    mock_msg1.reference = mock_msg_ref
    mock_msg1.to_dict.return_value = {
        "telegram_chat_id": 1221020259,
        "telegram_message_id": 12345,
        "role": "assistant",
        "telegram_deleted": False,
        "timestamp": datetime.now(RIYADH_TZ) - timedelta(hours=25),
    }

    mock_db.collection.return_value.document.return_value.collection.return_value.where.return_value.stream.return_value = [
        mock_msg1
    ]

    with patch("main.TELEGRAM_BOT_TOKEN", mock_token), patch(
        "main.get_db", return_value=mock_db
    ), patch("main.delete_message", return_value=True) as mock_delete:
        response = client.post(
            "/jobs/cleanup-messages",
            headers={"X-HouseOps-Secret-Token": expected_secret},
        )
        assert response.status_code == 200
        mock_delete.assert_called_once_with(1221020259, 12345)
        mock_msg_ref.update.assert_called_with({"telegram_deleted": True})


def test_telegram_webhook_ops_bot_ping_test(client):
    """Test webhook with a ping test message from the whitelisted ops bot."""
    payload = {
        "update_id": 20001,
        "message": {
            "message_id": 888,
            "chat": {"id": 789012},
            "from": {
                "id": 789012,
                "is_bot": True,
                "first_name": "DQBotOpsBot",
                "username": "DQBotOpsBot",
            },
            "text": "ping_test",
        },
    }

    mock_db = MagicMock()
    # Mock principal chat ID lookup
    mock_member_doc = MagicMock()
    mock_member_doc.exists = True
    mock_member_doc.to_dict.return_value = {
        "telegram_chat_id": 123456789,
    }
    mock_db.collection.return_value.document.return_value.get.return_value = (
        mock_member_doc
    )

    with patch("main.OPS_BOT_USER_ID", 789012), patch(
        "main.verify_webhook_secret", return_value=True
    ), patch("main.get_db", return_value=mock_db), patch(
        "app.ops_bot.send_ops_message"
    ) as mock_send_ops:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        resp_data = response.json()
        assert resp_data["status"] == "ok"
        assert resp_data["message"] == "ping_received"
        # Verify it did not send a duplicate message to Mazen via ops bot
        mock_send_ops.assert_not_called()


def test_run_agent_turn_sanitization():
    """Test that run_agent_turn handles tool results containing datetime objects by sanitizing them."""
    from app.vertex_client import run_agent_turn
    from app.models import InboundMessage
    from datetime import datetime

    mock_db = MagicMock()
    mock_model = MagicMock()
    mock_response = MagicMock()
    mock_candidate = MagicMock()

    # 1. Round 1: Model requests a tool call
    mock_fc = MagicMock()
    mock_fc.name = "get_schedule"
    mock_fc.args = {"date_range": "2026-06-07"}

    mock_part = MagicMock()
    mock_part.function_call = mock_fc
    mock_part.text = None

    mock_candidate.content.parts = [mock_part]

    # 2. Round 2: Model returns text response
    mock_response2 = MagicMock()
    mock_candidate2 = MagicMock()
    mock_part2 = MagicMock()
    mock_part2.function_call = None
    mock_part2.text = "Here is the schedule."
    mock_candidate2.content.parts = [mock_part2]
    mock_response2.candidates = [mock_candidate2]

    # Model returns the tool request on first call, text on second call
    mock_response.usage_metadata = None
    mock_response2.usage_metadata = None
    mock_response.candidates = [mock_candidate]
    mock_model.generate_content.side_effect = [mock_response, mock_response2]

    inbound = InboundMessage(
        message_id="tg_msg_1",
        phone_e164="+966506667785",
        member_id="mem_principal_001",
        received_at=datetime.now(),
        content=[{"block_type": "text", "text": "schedule tomorrow"}],
    )

    with patch("app.vertex_client._get_model", return_value=mock_model), patch(
        "app.vertex_client.execute_tool_call"
    ) as mock_execute_tool:
        # Return a dictionary containing a datetime object
        dt_val = datetime(2026, 6, 7, 12, 0, 0)
        mock_execute_tool.return_value = {
            "ok": True,
            "datetime_field": dt_val,
        }

        # Run agent turn
        reply, usage = run_agent_turn(
            tier="tier1",
            member_id="mem_principal_001",
            phone_e164="+966506667785",
            session_context="context",
            history_text="history",
            inbound=inbound,
            db=mock_db,
        )

        assert reply == "Here is the schedule."
        mock_execute_tool.assert_called_once()
