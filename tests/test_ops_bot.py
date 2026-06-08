"""Tests for the Operations Bot (DQBotOpsBot) status reports, alerts, and endpoints."""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from app.config import RIYADH_TZ
from app.ops_bot import send_ops_message, send_ops_alert, get_ops_status_report


@pytest.fixture
def client():
    # Set raise_server_exceptions=False so FastAPI global exception handlers are used
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_db():
    db = MagicMock()

    # Mock Mazen member lookup
    mock_member_doc = MagicMock()
    mock_member_doc.exists = True
    mock_member_doc.to_dict.return_value = {
        "member_id": "mem_principal_001",
        "name": "Mazen",
        "telegram_chat_id": 123456789,
        "active": True,
    }
    db.collection.return_value.document.return_value.get.return_value = mock_member_doc

    return db


@patch("app.ops_bot.httpx.Client")
@patch("app.ops_bot.TELEGRAM_OPS_BOT_TOKEN", "mock_token")
def test_send_ops_message_success(mock_httpx_client_class, mock_db):
    """Test successful outbound message transmission via Ops Bot."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "result": {"message_id": 555}}
    mock_client.post.return_value = mock_response
    mock_httpx_client_class.return_value.__enter__.return_value = mock_client

    res = send_ops_message(mock_db, "System is doing great!")

    assert res["ok"] is True
    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert kwargs["json"]["chat_id"] == 123456789
    assert kwargs["json"]["text"] == "System is doing great!"


@patch("app.ops_bot.send_ops_message")
def test_send_ops_alert(mock_send_ops_message, mock_db):
    """Test formatting and dispatch of alerts to the Ops Bot."""
    send_ops_alert(mock_db, "TEST_ALERT", "Test alert details")

    mock_send_ops_message.assert_called_once()
    alert_text = mock_send_ops_message.call_args[0][1]
    assert "🚨 *DQBotOps System Alert*" in alert_text
    assert "*Type:* TEST_ALERT" in alert_text
    assert "Test alert details" in alert_text


@patch("app.ops_bot.httpx.get")
@patch("app.vertex_client.get_prefix_token_count")
@patch("app.config.TELEGRAM_BOT_TOKEN", "mock_token")
@patch("app.ops_bot.TELEGRAM_OPS_BOT_TOKEN", "mock_token")
def test_get_ops_status_report_success(mock_token_count, mock_httpx_get, mock_db):
    """Test building system performance health reports on successful checks."""
    mock_token_count.return_value = 4096

    # Mock httpx responses for the Telegram call
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "ok": True,
        "result": {
            "url": "https://service-url.run.app/webhook/telegram",
            "pending_update_count": 0,
        },
    }
    mock_httpx_get.return_value = mock_resp

    report = get_ops_status_report(mock_db)

    assert "🖥️ *DQBotOps Performance Report*" in report
    assert "🟢 *System Status:* Healthy" in report
    assert "Database:* OK" in report
    assert "Vertex AI:* OK" in report
    assert "Telegram Webhook:* OK" in report


@patch("app.ops_bot.httpx.get")
@patch("app.vertex_client.get_prefix_token_count")
def test_get_ops_status_report_failure(mock_token_count, mock_httpx_get, mock_db):
    """Test system performance reports with failed subsystems."""
    mock_token_count.side_effect = Exception("Vertex AI connection failed")
    mock_httpx_get.side_effect = Exception("Connection timed out")

    # Database set error
    mock_db.collection.side_effect = Exception("Write permission denied")

    report = get_ops_status_report(mock_db)

    assert "🖥️ *DQBotOps Performance Report*" in report
    assert "🔴 *System Status:* Attention Required" in report
    assert "Database:* FAILED" in report
    assert "Vertex AI:* FAILED" in report
    assert "Telegram Webhook:* FAILED" in report


def test_ops_status_update_endpoint(client, mock_db):
    """Test endpoint auth and execution for periodic ops status update cron."""
    with patch("main.get_db", return_value=mock_db), patch(
        "main.verify_secret_token", return_value=True
    ), patch(
        "app.ops_bot.get_ops_status_report", return_value="Test Report"
    ) as mock_report, patch("app.ops_bot.send_ops_message") as mock_send:
        response = client.post(
            "/jobs/ops-status-update", headers={"X-HouseOps-Secret-Token": "secret"}
        )
        assert response.status_code == 200
        assert response.text == "OK"
        mock_report.assert_called_once()
        mock_send.assert_called_once_with(mock_db, "Test Report")


def test_global_exception_handler(client, mock_db):
    """Test that unexpected crashes triggers an alert sent to the Ops Bot."""
    # Let's trigger a crash inside /health by patching get_prefix_token_count to raise Exception
    with patch("main.get_db", return_value=mock_db), patch(
        "main.get_prefix_token_count",
        side_effect=Exception("Database connection timed out"),
    ), patch("app.ops_bot.send_ops_alert") as mock_alert:
        response = client.get("/health")
        assert response.status_code == 500
        mock_alert.assert_called_once()
        args, kwargs = mock_alert.call_args
        assert args[1] == "SYSTEM_CRASH"
        assert "Database connection timed out" in str(kwargs.get("error") or args[2])


def test_driver_arrival_nag_timeout_alert_normal_channel(mock_db):
    """Test that run_driver_arrival_nag triggers a delayed arrival alert to the NORMAL channel (not Ops Bot)."""
    from app.workflow import run_driver_arrival_nag

    now = datetime.now(RIYADH_TZ)
    # Ping was created 35 minutes ago, and no alert has been sent yet
    created_at = now - timedelta(minutes=35)

    mock_ping_doc = MagicMock()
    mock_ping_doc.exists = True
    mock_ping_doc.to_dict.return_value = {
        "outing_id": "out_123",
        "driver_id": "dr_emad",
        "last_pinged_at": created_at.isoformat(),
        "created_at": created_at.isoformat(),
        "status": "awaiting_confirmation",
        "alert_sent": False,
    }
    mock_ping_doc.get.return_value = mock_ping_doc

    mock_outing_doc = MagicMock()
    mock_outing_doc.id = "out_123"
    mock_outing_doc.to_dict.return_value = {
        "outing_id": "out_123",
        "assigned_driver": "dr_emad",
        "destination": "Airport",
        "end_time": now - timedelta(minutes=40),
        "status": "scheduled",
    }

    mock_driver_doc = MagicMock()
    mock_driver_doc.exists = True
    mock_driver_doc.to_dict.return_value = {
        "member_id": "mem_driver_002",
        "name": "Emad",
        "active": True,
    }

    mock_member_doc = MagicMock()
    mock_member_doc.exists = True
    mock_member_doc.to_dict.return_value = {
        "telegram_chat_id": 987654321,
        "active": True,
    }

    def mock_collection_routing(collection_name):
        mock_coll = MagicMock()
        if collection_name == "driver_schedule":
            # For the stream search
            mock_coll.where.return_value.where.return_value.where.return_value.stream.return_value = [
                mock_outing_doc
            ]
        elif collection_name == "driver_arrival_pings":
            mock_coll.document.return_value = mock_ping_doc
        elif collection_name == "drivers":
            mock_coll.document.return_value.get.return_value = mock_driver_doc
        elif collection_name == "members":
            mock_coll.document.return_value.get.return_value = mock_member_doc
        return mock_coll

    mock_db.collection.side_effect = mock_collection_routing

    with patch("app.workflow.send_text_message"), patch(
        "app.workflow._notify_tier1_users"
    ) as mock_notify, patch("app.ops_bot.send_ops_alert") as mock_ops_alert:
        run_driver_arrival_nag(mock_db)

        # Verify that ops alert was NOT sent
        mock_ops_alert.assert_not_called()

        # Verify that normal channel alert WAS sent
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        assert "Delayed Driver Arrival" in args[1]
        assert "Emad" in args[1]
        assert "Airport" in args[1]

        # Verify that ping document is updated with alert_sent = True
        mock_ping_doc.update.assert_any_call({"alert_sent": True})


@patch("app.ops_bot.httpx.get")
@patch("app.ops_bot.httpx.Client")
@patch("app.vertex_client.get_prefix_token_count")
@patch("app.config.SERVICE_URL", "https://mock-service.run.app")
@patch("app.config.TELEGRAM_BOT_TOKEN", "123456:bottoken")
@patch("app.ops_bot.TELEGRAM_OPS_BOT_TOKEN", "789012:opsbottoken")
def test_get_ops_status_report_bot_integration_success(
    mock_token_count, mock_httpx_client_class, mock_httpx_get, mock_db
):
    """Test get_ops_status_report with a successful bot-to-bot integration check."""
    mock_token_count.return_value = 4096

    # Mock getWebhookInfo for main bot and ops bot
    mock_get_resp = MagicMock()
    mock_get_resp.json.return_value = {
        "ok": True,
        "result": {
            "url": "https://service-url.run.app/webhook/telegram",
            "pending_update_count": 0,
        },
    }
    mock_httpx_get.return_value = mock_get_resp

    # Mock POST to webhook
    mock_client = MagicMock()
    mock_post_resp = MagicMock()
    mock_post_resp.json.return_value = {"status": "ok", "message": "ping_received"}
    mock_client.post.return_value = mock_post_resp
    mock_httpx_client_class.return_value.__enter__.return_value = mock_client

    report = get_ops_status_report(mock_db)

    assert "Bot-to-Bot Integration:* OK" in report
    assert "🟢 *System Status:* Healthy" in report
    mock_client.post.assert_called_once()


def test_check_resource_usage_alert(mock_db):
    """Test that _check_resource_usage_alert correctly calls send_ops_alert under limit conditions."""
    from app.vertex_client import _check_resource_usage_alert

    with patch("app.ops_bot.send_ops_alert") as mock_ops_alert:
        # Case 1: Total tokens below 250k threshold -> no alert
        _check_resource_usage_alert(
            db=mock_db,
            phone_e164="+966506667785",
            member_id="mem_principal_001",
            rounds_executed=2,
            cumulative_prompt=200000,
            cumulative_cached=0,
            cumulative_candidates=40000,
        )
        mock_ops_alert.assert_not_called()

        # Case 2: Total tokens >= 250k threshold -> triggers alert
        _check_resource_usage_alert(
            db=mock_db,
            phone_e164="+966506667785",
            member_id="mem_principal_001",
            rounds_executed=3,
            cumulative_prompt=210000,
            cumulative_cached=0,
            cumulative_candidates=41000,
        )
        mock_ops_alert.assert_called_once()
        args, kwargs = mock_ops_alert.call_args
        assert args[1] == "HIGH_RESOURCE_USAGE"
        assert "Cumulative prompt tokens: 210000" in args[2]
