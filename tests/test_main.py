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
    with patch("main.verify_secret_token", return_value=False):
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
    with patch("main.verify_secret_token", return_value=True), patch(
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
    with patch("main.verify_secret_token", return_value=True), patch(
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
    with patch("main.verify_secret_token", return_value=True), patch(
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
    with patch("main.verify_secret_token", return_value=True), patch(
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
    with patch("main.verify_secret_token", return_value=True), patch(
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


def test_telegram_webhook_enqueue_failure_releases_key(client, sample_member):
    """Test that if enqueuing fails, the idempotency key is released."""
    payload = {
        "update_id": 10006,
        "message": {
            "message_id": 995,
            "chat": {"id": 1221020259},
            "text": "This is a real message",
        },
    }
    with patch("main.verify_secret_token", return_value=True), patch(
        "main.get_db"
    ), patch(
        "main.lookup_member_by_telegram_chat_id", return_value=sample_member
    ), patch("main.claim_idempotency_key", return_value=True), patch(
        "main.enqueue_inbound_processing", side_effect=Exception("Queue failure")
    ), patch("main.release_idempotency_key") as mock_release:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 500
        mock_release.assert_called_once()


def test_telegram_webhook_normalization_failure_releases_key(client, sample_member):
    """Test that if normalization returns None, the idempotency key is released."""
    payload = {
        "update_id": 10006,
        "message": {
            "message_id": 995,
            "chat": {"id": 1221020259},
            "text": "This is a real message",
        },
    }
    with patch("main.verify_secret_token", return_value=True), patch(
        "main.get_db"
    ), patch(
        "main.lookup_member_by_telegram_chat_id", return_value=sample_member
    ), patch("main.claim_idempotency_key", return_value=True), patch(
        "main.normalize_telegram_message", return_value=None
    ), patch("main.release_idempotency_key") as mock_release:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_release.assert_called_once()


@patch("main.get_db")
@patch("main.lookup_member_by_phone")
@patch("main.ingest_media_blocks")
@patch("main.run_confirmation_gate")
@patch("main.compile_conversation_history")
@patch("main.run_agent_turn")
@patch("main.send_text_message")
@patch("main.verify_internal_token")
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
    mock_verify_secret.return_value = True
    mock_lookup_phone.return_value = sample_member
    mock_media.return_value = (True, None)

    # Mock DB collections for parallel lookup
    mock_db = MagicMock()
    mock_get_db.return_value = mock_db

    mock_members_col = MagicMock()
    mock_member_doc = MagicMock()
    mock_member_snap = MagicMock()
    mock_member_snap.exists = True
    mock_member_snap.id = sample_member.member_id
    mock_member_snap.to_dict.return_value = sample_member.model_dump()
    mock_member_doc.get.return_value = mock_member_snap
    mock_members_col.document.return_value = mock_member_doc

    mock_convs_col = MagicMock()
    mock_conv_doc = MagicMock()
    mock_conv_snap = MagicMock()
    mock_conv_snap.exists = True
    mock_conv_snap.to_dict.return_value = {"member_id": sample_member.member_id}
    mock_conv_doc.get.return_value = mock_conv_snap
    mock_convs_col.document.return_value = mock_conv_doc

    def collection_side_effect(name):
        if name == "members":
            return mock_members_col
        elif name == "conversations":
            return mock_convs_col
        return MagicMock()

    mock_db.collection.side_effect = collection_side_effect

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

    mock_db.collection_group.return_value.where.return_value.stream.return_value = [
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
        "main.verify_secret_token", return_value=True
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


def test_run_agent_turn_warning_and_resume():
    """Test that run_agent_turn pauses at 250k tokens and can be resumed with resumed_state."""
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

    # Usage metadata has 260k tokens
    mock_metadata = MagicMock()
    mock_metadata.prompt_token_count = 250000
    mock_metadata.candidates_token_count = 10000
    mock_metadata.cached_content_token_count = 0
    mock_response.usage_metadata = mock_metadata
    mock_response.candidates = [mock_candidate]
    mock_model.generate_content.return_value = mock_response

    inbound = InboundMessage(
        message_id="tg_msg_1",
        phone_e164="+966506667785",
        member_id="mem_principal_001",
        received_at=datetime.now(),
        content=[{"block_type": "text", "text": "schedule tomorrow"}],
    )

    with patch("app.vertex_client._get_model", return_value=mock_model), patch(
        "app.vertex_client.execute_tool_call"
    ) as mock_execute_tool, patch(
        "app.firestore_db.set_pending_confirmation"
    ) as mock_set_pending:
        mock_execute_tool.return_value = {"ok": True}

        # Run agent turn (should trigger 250k warning and pause)
        reply, usage = run_agent_turn(
            tier="tier1",
            member_id="mem_principal_001",
            phone_e164="+966506667785",
            session_context="context",
            history_text="history",
            inbound=inbound,
            db=mock_db,
        )

        assert "High Resource Usage Warning" in reply
        assert "260,000 tokens" in reply
        mock_set_pending.assert_called_once()
        args, kwargs = mock_set_pending.call_args
        saved_payload = kwargs["payload"]
        assert saved_payload["cumulative_prompt"] == 250000
        assert saved_payload["cumulative_candidates"] == 10000
        assert saved_payload["rounds_executed"] == 1
        assert saved_payload["authorized_250k"] is True

        # Now, simulate resuming from the saved state.
        # Round 2: Model returns the final text
        mock_response2 = MagicMock()
        mock_candidate2 = MagicMock()
        mock_part2 = MagicMock()
        mock_part2.function_call = None
        mock_part2.text = "Finished task."
        mock_candidate2.content.parts = [mock_part2]
        mock_response2.candidates = [mock_candidate2]
        mock_response2.usage_metadata = None
        mock_model.generate_content.return_value = mock_response2

        # Reset model call count to track execution
        mock_model.generate_content.reset_mock()

        reply_resumed, usage_resumed = run_agent_turn(
            tier="tier1",
            member_id="mem_principal_001",
            phone_e164="+966506667785",
            session_context="context",
            history_text="history",
            inbound=inbound,
            db=mock_db,
            resumed_state=saved_payload,
        )

        assert reply_resumed == "Finished task."
        # Verify it only called generate_content once (starting from round 2)
        mock_model.generate_content.assert_called_once()


def test_telegram_webhook_onboarding_asks_language(client, sample_member):
    """Test contact onboarding welcomes user and prompts for language preference."""
    payload = {
        "update_id": 10004,
        "message": {
            "message_id": 997,
            "chat": {"id": 1221020259},
            "contact": {
                "phone_number": "966500000001",
                "first_name": "Test User",
            },
        },
    }
    with patch("main.verify_secret_token", return_value=True), patch(
        "main.get_db"
    ), patch("main.lookup_member_by_phone", return_value=sample_member), patch(
        "main.link_telegram_chat_id", return_value=True
    ), patch("main.send_text_message") as mock_send:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_send.assert_called_once()
        text = mock_send.call_args[0][1]
        assert "Welcome to DQ Villa Bot" in text
        assert "select your preferred language" in text.lower()
        inline_kb = mock_send.call_args[1]["inline_keyboard"]
        assert inline_kb[0][0]["text"] == "English"
        assert inline_kb[0][1]["text"] == "العربية"


def test_telegram_webhook_pref_lang_callback(client, sample_member):
    """Test callback query pref_lang_ar updates preference and replies in Arabic."""
    payload = {
        "update_id": 10005,
        "callback_query": {
            "id": "cb123",
            "data": "pref_lang_ar",
            "message": {
                "chat": {"id": 1221020259},
                "message_id": 996,
            },
        },
    }
    with patch("main.verify_secret_token", return_value=True), patch(
        "main.get_db"
    ), patch(
        "main.lookup_member_by_telegram_chat_id", return_value=sample_member
    ), patch(
        "app.firestore_db.update_member_preferred_language"
    ) as mock_update_lang, patch(
        "app.telegram.answer_callback_query"
    ) as mock_answer, patch("main.send_text_message") as mock_send:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_answer.assert_called_once_with("cb123")
        mock_update_lang.assert_called_once_with(
            mock_update_lang.call_args[0][0], sample_member.phone_e164, "ar"
        )
        mock_send.assert_called_once_with(
            1221020259, "تم تحديد اللغة المفضلة إلى العربية."
        )


def test_telegram_webhook_language_command(client, sample_member):
    """Test text command 'اللغة' intercepts in fast path and prompts language selector."""
    payload = {
        "update_id": 10006,
        "message": {
            "message_id": 995,
            "chat": {"id": 1221020259},
            "text": "اللغة",
        },
    }
    with patch("main.verify_secret_token", return_value=True), patch(
        "main.get_db"
    ), patch(
        "main.lookup_member_by_telegram_chat_id", return_value=sample_member
    ), patch("main.send_text_message") as mock_send:
        response = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
        )
        assert response.status_code == 200
        mock_send.assert_called_once()
        text = mock_send.call_args[0][1]
        assert "Please select your preferred language" in text
        inline_kb = mock_send.call_args[1]["inline_keyboard"]
        assert inline_kb[0][0]["callback_data"] == "pref_lang_en"
        assert inline_kb[0][1]["callback_data"] == "pref_lang_ar"


def test_process_inbound_other_language_blocked(client, sample_member):
    """Test that messages in unsupported languages (e.g. Spanish) return fallback request and block Gemini."""
    from app.models import InboundMessage

    inbound = InboundMessage(
        message_id="tg_msg_994",
        phone_e164="+966500000001",
        member_id="mem_test_001",
        received_at="2026-06-09T12:00:00+03:00",
        content=[{"block_type": "text", "text": "Hola amigo como estas"}],
    )
    with patch("main.verify_internal_token", return_value=True), patch(
        "main.get_db"
    ) as mock_get_db, patch(
        "main.lookup_member_by_telegram_chat_id", return_value=sample_member
    ), patch("app.vertex_client.detect_language", return_value="Other"), patch(
        "main.send_text_message"
    ) as mock_send, patch("main.write_message_turn") as mock_write_turn:
        # Configure sample member chat_id
        sample_member.telegram_chat_id = 1221020259

        # Set up mock database instance
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        # Mock point lookups
        mock_snap_member = MagicMock()
        mock_snap_member.exists = True
        mock_snap_member.id = "mem_test_001"
        mock_snap_member.to_dict.return_value = sample_member.model_dump()

        mock_member_doc = MagicMock()
        mock_member_doc.get.return_value = mock_snap_member

        mock_snap_conv = MagicMock()
        mock_snap_conv.exists = True
        mock_snap_conv.to_dict.return_value = {"member_id": "mem_test_001"}

        mock_conv_doc = MagicMock()
        mock_conv_doc.get.return_value = mock_snap_conv

        def collection_side_effect(name):
            mock_coll = MagicMock()
            if name == "members":
                mock_coll.document.return_value = mock_member_doc
            elif name == "conversations":
                mock_coll.document.return_value = mock_conv_doc
            return mock_coll

        mock_db.collection.side_effect = collection_side_effect

        # Mock history
        with patch("main.compile_conversation_history", return_value=("", MagicMock())):
            response = client.post(
                "/tasks/process-inbound",
                content=inbound.model_dump_json(),
                headers={"X-HouseOps-Secret-Token": "secret"},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "language_blocked"
            mock_send.assert_called_once()
            assert (
                "Please communicate in English, Arabic, Urdu, or Tagalog"
                in mock_send.call_args[0][1]
            )
            # Asserts both user turn and assistant reply are written to log
            assert mock_write_turn.call_count == 2


def test_process_inbound_oidc_failures(client):
    """Test process-inbound endpoint rejected when OIDC token is missing or service account is wrong."""
    from app.models import InboundMessage

    inbound = InboundMessage(
        message_id="tg_msg_990",
        phone_e164="+966500000001",
        member_id="mem_test_001",
        received_at="2026-06-09T12:00:00+03:00",
        content=[{"block_type": "text", "text": "hello"}],
    )
    with patch(
        "main.TASKS_SERVICE_ACCOUNT", "expected-task-runner@gcp.iam.gserviceaccount.com"
    ), patch("main.verify_internal_token", return_value=True):
        # 1. Missing Authorization header
        response = client.post(
            "/tasks/process-inbound",
            content=inbound.model_dump_json(),
            headers={"X-HouseOps-Secret-Token": "secret"},
        )
        assert response.status_code == 401
        assert "Missing OIDC token" in response.json()["detail"]

        # 2. Invalid/malformed Bearer token causing id_token.verify_oauth2_token error
        with patch(
            "google.oauth2.id_token.verify_oauth2_token",
            side_effect=ValueError("Token invalid"),
        ):
            response = client.post(
                "/tasks/process-inbound",
                content=inbound.model_dump_json(),
                headers={
                    "X-HouseOps-Secret-Token": "secret",
                    "Authorization": "Bearer bad-token",
                },
            )
            assert response.status_code == 403
            assert "OIDC verification failed" in response.json()["detail"]
