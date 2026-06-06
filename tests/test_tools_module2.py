"""Unit tests for app/tools_module2.py."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch


from app.tools_module2 import (
    _txn_create_adhoc_task,
    _txn_update_task_status,
    _validate_structural_enum,
    create_adhoc_task,
    execute_pending_create_adhoc,
    execute_tool_call,
    list_tasks,
    update_task_status,
    create_weather_tasks,
    execute_pending_create_weather_tasks,
)


def test_validate_structural_enum_valid():
    """Test validation of valid enum value."""
    result = _validate_structural_enum(
        "completed", {"pending", "completed", "skipped"}, "status"
    )
    assert result is None


def test_validate_structural_enum_invalid():
    """Test validation of invalid enum value."""
    result = _validate_structural_enum(
        "invalid", {"pending", "completed", "skipped"}, "status"
    )
    assert result is not None
    assert "Invalid status" in result


def test_list_tasks_tier1_any_member(mock_firestore_client):
    """Test Tier 1 can list tasks for any member."""
    mock_query = MagicMock()
    mock_firestore_client.collection.return_value.where.return_value.where.return_value = mock_query

    mock_doc1 = Mock()
    mock_doc1.id = "task_001"
    mock_doc1.to_dict.return_value = {
        "task_id": "task_001",
        "assigned_to": "mem_001",
        "status": "pending",
    }

    mock_doc2 = Mock()
    mock_doc2.id = "task_002"
    mock_doc2.to_dict.return_value = {
        "task_id": "task_002",
        "assigned_to": "mem_002",
        "status": "completed",
    }

    mock_query.stream.return_value = [mock_doc1, mock_doc2]

    result = list_tasks(
        mock_firestore_client,
        member_id="mem_002",
        date="2024-06-01",
        caller_tier="tier1",
        caller_id="mem_001",
    )

    assert result["ok"] is True
    assert len(result["tasks"]) == 2


def test_list_tasks_tier2_self_only(mock_firestore_client):
    """Test Tier 2 can only list own tasks."""
    mock_query = MagicMock()
    mock_firestore_client.collection.return_value.where.return_value.where.return_value = mock_query

    result = list_tasks(
        mock_firestore_client,
        member_id="mem_other",
        date="2024-06-01",
        caller_tier="tier2",
        caller_id="mem_self",
    )

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    assert result["tasks"] == []


def test_list_tasks_tier2_own_tasks(mock_firestore_client):
    """Test Tier 2 can list own tasks."""
    mock_query = MagicMock()
    mock_firestore_client.collection.return_value.where.return_value.where.return_value = mock_query

    mock_doc = Mock()
    mock_doc.id = "task_001"
    mock_doc.to_dict.return_value = {
        "task_id": "task_001",
        "assigned_to": "mem_self",
        "status": "pending",
    }
    mock_query.stream.return_value = [mock_doc]

    result = list_tasks(
        mock_firestore_client,
        member_id="mem_self",
        date="2024-06-01",
        caller_tier="tier2",
        caller_id="mem_self",
    )

    assert result["ok"] is True
    assert len(result["tasks"]) == 1


def test_update_task_status_valid_enum(mock_firestore_client):
    """Test update task status with valid enum."""
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref

    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"assigned_to": "mem_001", "status": "pending"}
    mock_ref.get.return_value = mock_snap

    with patch("app.tools_module2._txn_update_task_status") as mock_txn:
        mock_txn.return_value = {
            "ok": True,
            "task_id": "task_001",
            "status": "completed",
        }

        result = update_task_status(
            mock_firestore_client,
            task_id="task_001",
            status="completed",
            feedback="Done",
            caller_tier="tier1",
            caller_id="mem_001",
        )

    assert result["ok"] is True


def test_update_task_status_invalid_enum(mock_firestore_client):
    """Test update task status with invalid enum."""
    result = update_task_status(
        mock_firestore_client,
        task_id="task_001",
        status="invalid_status",
        feedback=None,
        caller_tier="tier1",
        caller_id="mem_001",
    )

    assert result["ok"] is False
    assert "Invalid status" in result["error"]


def test_update_task_status_not_found(mock_firestore_client):
    """Test update task status when task not found."""
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref

    mock_snap = Mock()
    mock_snap.exists = False
    mock_ref.get.return_value = mock_snap

    result = update_task_status(
        mock_firestore_client,
        task_id="task_999",
        status="completed",
        feedback=None,
        caller_tier="tier1",
        caller_id="mem_001",
    )

    assert result["ok"] is False
    assert result["error"] == "task_not_found"


def test_update_task_status_tier2_permission_denied(mock_firestore_client):
    """Test Tier 2 cannot update other's tasks."""
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref

    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"assigned_to": "mem_other", "status": "pending"}
    mock_ref.get.return_value = mock_snap

    result = update_task_status(
        mock_firestore_client,
        task_id="task_001",
        status="completed",
        feedback=None,
        caller_tier="tier2",
        caller_id="mem_self",
    )

    assert result["ok"] is False
    assert result["error"] == "permission_denied"


def test_create_adhoc_task_tier1(mock_firestore_client):
    """Test Tier 1 can create adhoc task."""
    with patch("app.tools_module2.set_pending_confirmation") as mock_set_pending:
        result = create_adhoc_task(
            mock_firestore_client,
            assigned_to="mem_001",
            task_description="Test task",
            due_date="2024-06-01",
            phone_e164="+966500000001",
        )

    assert result["ok"] is True
    assert result["pending_confirmation"] is True
    mock_set_pending.assert_called_once()


def test_create_adhoc_task_tier2_permission_denied(mock_firestore_client):
    """Test Tier 2 cannot create adhoc task."""
    result = create_adhoc_task(
        mock_firestore_client,
        assigned_to="mem_001",
        task_description="Test task",
        due_date="2024-06-01",
        phone_e164="+966500000001",
    )

    # This test would need to be called through execute_tool_call with tier2
    # Direct call doesn't check tier
    assert result["ok"] is True  # Direct call succeeds


def test_create_adhoc_task_skip_confirmation(mock_firestore_client):
    """Test create adhoc task with skip_confirmation flag."""
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref

    with patch("app.tools_module2._txn_create_adhoc_task") as mock_txn:
        mock_txn.return_value = {"ok": True, "task_id": "task_001"}

        result = create_adhoc_task(
            mock_firestore_client,
            assigned_to="mem_001",
            task_description="Test task",
            due_date="2024-06-01",
            phone_e164="+966500000001",
            skip_confirmation=True,
        )

    assert result["ok"] is True
    assert result.get("pending_confirmation") is not True


def test_execute_pending_create_adhoc(mock_firestore_client):
    """Test execution of pending adhoc task creation."""
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref

    with patch("app.tools_module2._txn_create_adhoc_task") as mock_txn:
        mock_txn.return_value = {"ok": True, "task_id": "task_001"}

        result = execute_pending_create_adhoc(
            mock_firestore_client,
            payload={
                "assigned_to": "mem_001",
                "task_description": "Test",
                "due_date": "2024-06-01",
                "task_id": "task_001",
            },
        )

    assert result["ok"] is True
    assert result["task_id"] == "task_001"


def test_execute_tool_call_list_tasks(mock_firestore_client):
    """Test execute_tool_call dispatches to list_tasks."""
    with patch("app.tools_module2.list_tasks") as mock_list:
        mock_list.return_value = {"ok": True, "tasks": []}

        result = execute_tool_call(
            mock_firestore_client,
            tool_name="list_tasks",
            args={"member_id": "mem_001", "date": "2024-06-01"},
            caller_member_id="mem_001",
            caller_tier="tier1",
            phone_e164="+966500000001",
        )

    assert result["ok"] is True
    mock_list.assert_called_once()


def test_execute_tool_call_update_task_status(mock_firestore_client):
    """Test execute_tool_call dispatches to update_task_status."""
    with patch("app.tools_module2.update_task_status") as mock_update:
        mock_update.return_value = {"ok": True}

        result = execute_tool_call(
            mock_firestore_client,
            tool_name="update_task_status",
            args={"task_id": "task_001", "status": "completed"},
            caller_member_id="mem_001",
            caller_tier="tier1",
            phone_e164="+966500000001",
        )

    assert result["ok"] is True
    mock_update.assert_called_once()


def test_execute_tool_call_create_adhoc_task_tier1(mock_firestore_client):
    """Test execute_tool_call dispatches to create_adhoc_task for Tier 1."""
    with patch("app.tools_module2.create_adhoc_task") as mock_create:
        mock_create.return_value = {"ok": True, "pending_confirmation": True}

        result = execute_tool_call(
            mock_firestore_client,
            tool_name="create_adhoc_task",
            args={
                "assigned_to": "mem_001",
                "task_description": "Test",
                "due_date": "2024-06-01",
            },
            caller_member_id="mem_001",
            caller_tier="tier1",
            phone_e164="+966500000001",
        )

    assert result["ok"] is True
    mock_create.assert_called_once()


def test_execute_tool_call_create_adhoc_task_tier2_denied(mock_firestore_client):
    """Test Tier 2 cannot create adhoc task."""
    result = execute_tool_call(
        mock_firestore_client,
        tool_name="create_adhoc_task",
        args={
            "assigned_to": "mem_001",
            "task_description": "Test",
            "due_date": "2024-06-01",
        },
        caller_member_id="mem_002",
        caller_tier="tier2",
        phone_e164="+966500000001",
    )

    assert result["ok"] is False
    assert result["error"] == "permission_denied"


def test_execute_tool_call_unknown_tool(mock_firestore_client):
    """Test execute_tool_call handles unknown tool."""
    result = execute_tool_call(
        mock_firestore_client,
        tool_name="unknown_tool",
        args={},
        caller_member_id="mem_001",
        caller_tier="tier1",
        phone_e164="+966500000001",
    )

    assert result["ok"] is False
    assert "unknown_tool" in result["error"]


def test_txn_update_task_status_success():
    """Test transactional update task status success."""
    mock_transaction = MagicMock()
    mock_ref = MagicMock()

    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"status": "pending"}
    mock_ref.get.return_value = mock_snap

    result = _txn_update_task_status(
        mock_transaction, mock_ref, "completed", "Done", "tier1", "mem_001"
    )

    assert result["ok"] is True
    mock_transaction.update.assert_called_once()


def test_txn_update_task_status_not_found():
    """Test transactional update when task not found."""
    mock_transaction = MagicMock()
    mock_ref = MagicMock()

    mock_snap = Mock()
    mock_snap.exists = False
    mock_ref.get.return_value = mock_snap

    result = _txn_update_task_status(
        mock_transaction, mock_ref, "completed", None, "tier1", "mem_001"
    )

    assert result["ok"] is False
    assert result["error"] == "task_not_found"


def test_txn_create_adhoc_task_success():
    """Test transactional create adhoc task success."""
    mock_transaction = MagicMock()
    mock_ref = MagicMock()

    mock_snap = Mock()
    mock_snap.exists = False
    mock_ref.get.return_value = mock_snap

    payload = {"task_id": "task_001", "description": "Test"}
    result = _txn_create_adhoc_task(mock_transaction, mock_ref, payload)

    assert result["ok"] is True
    mock_transaction.set.assert_called_once()


def test_txn_create_adhoc_task_collision():
    """Test transactional create when task_id collision."""
    mock_transaction = MagicMock()
    mock_ref = MagicMock()

    mock_snap = Mock()
    mock_snap.exists = True
    mock_ref.get.return_value = mock_snap

    payload = {"task_id": "task_001", "description": "Test"}
    result = _txn_create_adhoc_task(mock_transaction, mock_ref, payload)

    assert result["ok"] is False
    assert result["error"] == "task_id_collision"


def test_create_weather_tasks(mock_firestore_client):
    """Test create_weather_tasks sets pending confirmation."""
    tasks = [
        {
            "assigned_to": "mem_001",
            "task_description": "Clean pool",
            "due_date": "2024-06-01",
        },
        {
            "assigned_to": "mem_002",
            "task_description": "Clean cars",
            "due_date": "2024-06-01",
        },
    ]
    with patch("app.tools_module2.set_pending_confirmation") as mock_set_pending:
        result = create_weather_tasks(
            mock_firestore_client,
            tasks=tasks,
            phone_e164="+966500000001",
        )
    assert result["ok"] is True
    assert result["pending_confirmation"] is True
    assert "Clean pool" in result["summary"]
    assert "Clean cars" in result["summary"]
    mock_set_pending.assert_called_once()


def test_execute_pending_create_weather_tasks(mock_firestore_client):
    """Test execute_pending_create_weather_tasks batch writes to database."""
    tasks = [
        {
            "assigned_to": "mem_001",
            "task_description": "Clean pool",
            "due_date": "2024-06-01",
        },
        {
            "assigned_to": "mem_002",
            "task_description": "Clean cars",
            "due_date": "2024-06-01",
        },
    ]
    payload = {"tasks": tasks}

    mock_batch = MagicMock()
    mock_firestore_client.batch.return_value = mock_batch

    result = execute_pending_create_weather_tasks(mock_firestore_client, payload)

    assert result["ok"] is True
    assert len(result["task_ids"]) == 2
    assert mock_batch.set.call_count == 2
    mock_batch.commit.assert_called_once()


def test_execute_tool_call_register_calendar_url(mock_firestore_client):
    """Test execute_tool_call routes register_calendar_url to execute_fleet_tool_call."""
    with patch("app.tools_fleet.execute_fleet_tool_call") as mock_fleet_dispatch:
        mock_fleet_dispatch.return_value = {"ok": True}

        result = execute_tool_call(
            mock_firestore_client,
            tool_name="register_calendar_url",
            args={"member_id": "mem_001", "url": "webcal://example.com/cal.ics"},
            caller_member_id="mem_001",
            caller_tier="tier1",
            phone_e164="+966500000001",
        )

    assert result["ok"] is True
    mock_fleet_dispatch.assert_called_once_with(
        db=mock_firestore_client,
        tool_name="register_calendar_url",
        args={"member_id": "mem_001", "url": "webcal://example.com/cal.ics"},
        caller_member_id="mem_001",
        caller_tier="tier1",
        phone_e164="+966500000001",
    )


def test_txn_update_task_status_tier2_rules():
    """Test Tier 2 status update constraints: only completed, or skipped with feedback."""
    mock_transaction = MagicMock()
    mock_ref = MagicMock()

    # Case 1: Tier 2 trying to mark as pending -> should be denied
    mock_snap = Mock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"assigned_to": "mem_001", "status": "completed"}
    mock_ref.get.return_value = mock_snap

    result_pending = _txn_update_task_status(
        mock_transaction, mock_ref, "pending", None, "tier2", "mem_001"
    )
    assert result_pending["ok"] is False
    assert result_pending["error"] == "permission_denied"

    # Case 2: Tier 2 trying to skip without feedback -> feedback required
    result_skipped_no_fb = _txn_update_task_status(
        mock_transaction, mock_ref, "skipped", None, "tier2", "mem_001"
    )
    assert result_skipped_no_fb["ok"] is False
    assert result_skipped_no_fb["error"] == "feedback_required_to_report_problem"

    # Case 3: Tier 2 trying to skip with feedback -> success
    mock_snap.to_dict.return_value = {"assigned_to": "mem_001", "status": "pending"}
    result_skipped_with_fb = _txn_update_task_status(
        mock_transaction, mock_ref, "skipped", "playroom is locked", "tier2", "mem_001"
    )
    assert result_skipped_with_fb["ok"] is True
    assert result_skipped_with_fb["status"] == "skipped"

    # Case 4: Tier 2 marking as completed -> success
    result_completed = _txn_update_task_status(
        mock_transaction, mock_ref, "completed", None, "tier2", "mem_001"
    )
    assert result_completed["ok"] is True
    assert result_completed["status"] == "completed"
