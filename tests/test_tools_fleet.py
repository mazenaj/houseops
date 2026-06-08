"""Tests for fleet operations and calendar tools (Module 1)."""

from __future__ import annotations

from datetime import datetime, date, time
from unittest.mock import MagicMock, patch


from app.config import RIYADH_TZ
from app.tools_fleet import (
    get_schedule,
    manage_outing,
    execute_pending_manage_outing,
    update_driver_availability,
    get_calendar_events,
    execute_fleet_tool_call,
)


def test_get_schedule(mock_firestore_client):
    """Test get_schedule fetches active drivers, availabilities, and outings."""
    # 1. Mock drivers collection
    mock_dr1 = MagicMock()
    mock_dr1.id = "dr_001"
    mock_dr1.to_dict.return_value = {"name": "Khidir", "active": True}

    mock_drivers_query = MagicMock()
    mock_drivers_query.stream.return_value = [mock_dr1]

    # 2. Mock driver_availability collection
    mock_av1 = MagicMock()
    mock_av1.id = "avail_001"
    mock_av1.to_dict.return_value = {
        "driver_id": "dr_001",
        "date": "2026-06-04",
        "slots": [{"start_time": "08:00", "end_time": "17:00", "status": "available"}],
    }
    mock_avail_query = MagicMock()
    mock_avail_query.stream.return_value = [mock_av1]

    # 3. Mock driver_schedule collection
    mock_out1 = MagicMock()
    mock_out1.id = "out_001"
    mock_out1.to_dict.return_value = {
        "assigned_driver": "dr_001",
        "start_time": datetime.combine(date(2026, 6, 4), time(10, 0)).replace(
            tzinfo=RIYADH_TZ
        ),
        "end_time": datetime.combine(date(2026, 6, 4), time(11, 0)).replace(
            tzinfo=RIYADH_TZ
        ),
        "destination": "Airport",
        "purpose": "Pickup guest",
        "status": "scheduled",
    }
    mock_schedule_query = MagicMock()
    mock_schedule_query.stream.return_value = [mock_out1]

    # Set up client collections calls
    def collection_side_effect(name):
        if name == "drivers":
            mock_col = MagicMock()
            mock_col.where.return_value = mock_drivers_query
            return mock_col
        elif name == "driver_availability":
            mock_col = MagicMock()
            mock_where1 = MagicMock()
            mock_col.where.return_value = mock_where1
            mock_where1.where.return_value = mock_avail_query
            return mock_col
        elif name == "driver_schedule":
            mock_col = MagicMock()
            mock_col.where.return_value.where.return_value = mock_schedule_query
            return mock_col
        return MagicMock()

    mock_firestore_client.collection.side_effect = collection_side_effect

    # Call get_schedule
    result = get_schedule(mock_firestore_client, "2026-06-04")

    assert result["ok"] is True
    assert len(result["drivers"]) == 1
    assert result["drivers"][0]["name"] == "Khidir"
    assert len(result["availabilities"]) == 1
    assert result["availabilities"][0]["date"] == "2026-06-04"
    assert len(result["outings"]) == 1
    assert result["outings"][0]["destination"] == "Airport"
    assert "2026-06-04T10:00:00" in result["outings"][0]["start_time"]


def test_manage_outing_create_requires_confirmation(mock_firestore_client):
    """Test create outing sets a pending confirmation."""
    phone = "+966500000001"

    mock_dr = MagicMock()
    mock_dr.exists = True
    mock_dr.to_dict.return_value = {"name": "Khidir"}
    mock_firestore_client.collection.return_value.document.return_value.get.return_value = mock_dr

    with patch("app.tools_fleet.set_pending_confirmation") as mock_set_pending:
        result = manage_outing(
            db=mock_firestore_client,
            action="create",
            phone_e164=phone,
            caller_member_id="mem_001",
            assigned_driver="dr_001",
            start_time="2026-06-04T10:00:00+03:00",
            end_time="2026-06-04T11:00:00+03:00",
            destination="Airport",
            purpose="Pickup guest",
        )

    assert result["ok"] is True
    assert result["pending_confirmation"] is True
    assert "Schedule driver Khidir" in result["summary"]
    mock_set_pending.assert_called_once()


def test_execute_pending_manage_outing_create(mock_firestore_client):
    """Test execute_pending_manage_outing for create action."""
    payload = {
        "action": "create",
        "assigned_driver": "dr_001",
        "start_time": "2026-06-04T10:00:00+03:00",
        "end_time": "2026-06-04T11:00:00+03:00",
        "destination": "Airport",
        "purpose": "Pickup guest",
        "requested_by": "mem_001",
        "outing_id": "out_test1",
    }

    mock_doc = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_doc

    result = execute_pending_manage_outing(mock_firestore_client, payload)

    assert result["ok"] is True
    assert result["outing_id"] == "out_test1"
    mock_doc.set.assert_called_once()
    saved_payload = mock_doc.set.call_args[0][0]
    assert saved_payload["destination"] == "Airport"
    assert isinstance(saved_payload["start_time"], datetime)


def test_execute_pending_manage_outing_cancel(mock_firestore_client):
    """Test execute_pending_manage_outing for cancel action."""
    payload = {
        "action": "cancel",
        "outing_id": "out_test1",
    }

    mock_doc = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_doc

    result = execute_pending_manage_outing(mock_firestore_client, payload)

    assert result["ok"] is True
    assert result["outing_id"] == "out_test1"
    assert result["status"] == "cancelled"
    mock_doc.update.assert_called_once()
    updates = mock_doc.update.call_args[0][0]
    assert updates["status"] == "cancelled"


def test_update_driver_availability(mock_firestore_client):
    """Test update_driver_availability performs write in a transaction."""
    slots = [{"start_time": "08:00", "end_time": "12:00", "status": "available"}]

    mock_transaction = MagicMock()
    mock_firestore_client.transaction.return_value = mock_transaction

    mock_doc = MagicMock()
    mock_doc.id = "avail_dr_001_20260604"
    mock_firestore_client.collection.return_value.document.return_value = mock_doc

    result = update_driver_availability(
        db=mock_firestore_client,
        driver_id="dr_001",
        date_str="2026-06-04",
        slots=slots,
        notes="All set",
        caller_member_id="mem_001",
    )

    assert result["ok"] is True
    assert result["availability_id"] == "avail_dr_001_20260604"
    # Check that set was called directly on the mock document reference object
    mock_doc.set.assert_called_once()


def test_get_calendar_events(mock_firestore_client):
    """Test get_calendar_events fetches and aggregates calendars."""
    # Mock principal member Jawaher
    mock_m1 = MagicMock()
    mock_m1.id = "mem_002"
    mock_m1.to_dict.return_value = {
        "name": "Jawaher",
        "role": "tier1",
        "active": True,
        "icloud_calendar_url": "webcal://example.com/jawaher.ics",
    }

    mock_members_query = MagicMock()
    mock_members_query.where.return_value.where.return_value = mock_members_query
    mock_members_query.stream.return_value = [mock_m1]

    mock_firestore_client.collection.return_value = mock_members_query

    # Mock fetch_icloud_events helper
    mock_events = [
        {
            "summary": "Doctor appointment",
            "location": "Riyadh Clinic",
            "description": "",
            "start": "2026-06-04T14:00:00+03:00",
            "end": "2026-06-04T15:00:00+03:00",
            "is_all_day": False,
        }
    ]

    with patch(
        "app.tools_fleet.fetch_icloud_events", return_value=mock_events
    ) as mock_fetch:
        result = get_calendar_events(mock_firestore_client, "2026-06-04")

    assert result["ok"] is True
    mock_fetch.assert_called_once_with(
        "webcal://example.com/jawaher.ics",
        date(2026, 6, 4),
        date(2026, 6, 4),
    )
    assert len(result["events"]) == 1
    assert result["events"][0]["owner_name"] == "Jawaher"
    assert result["events"][0]["summary"] == "Doctor appointment"


def test_execute_fleet_tool_call_routing(mock_firestore_client):
    """Test execute_fleet_tool_call routes calls and enforces RBAC."""
    # Tier 2 staff denied for manage_outing
    res1 = execute_fleet_tool_call(
        db=mock_firestore_client,
        tool_name="manage_outing",
        args={"action": "create"},
        caller_member_id="mem_staff_001",
        caller_tier="tier2",
        phone_e164="+966502644515",
    )
    assert res1["ok"] is False
    assert res1["error"] == "permission_denied"

    # Tier 1 principal allowed for manage_outing (requires confirmation)
    # Mock doc lookup for driver
    mock_dr = MagicMock()
    mock_dr.exists = True
    mock_dr.to_dict.return_value = {"name": "Khidir"}
    mock_firestore_client.collection.return_value.document.return_value.get.return_value = mock_dr

    with patch("app.tools_fleet.set_pending_confirmation") as mock_set_pending:
        res2 = execute_fleet_tool_call(
            db=mock_firestore_client,
            tool_name="manage_outing",
            args={
                "action": "create",
                "assigned_driver": "dr_001",
                "start_time": "2026-06-04T10:00:00+03:00",
                "end_time": "2026-06-04T11:00:00+03:00",
                "destination": "Airport",
                "purpose": "Errand",
            },
            caller_member_id="mem_test_001",
            caller_tier="tier1",
            phone_e164="+966500000001",
        )
    assert res2["ok"] is True
    assert res2["pending_confirmation"] is True
    mock_set_pending.assert_called_once()


def test_register_calendar_url(mock_firestore_client):
    """Test register_calendar_url stores calendar URLs for Tier 1 principals."""
    # Mock lookup
    mock_ref = MagicMock()
    mock_snap = MagicMock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {"role": "tier1"}
    mock_ref.get.return_value = mock_snap
    mock_firestore_client.collection.return_value.document.return_value = mock_ref

    result = execute_fleet_tool_call(
        db=mock_firestore_client,
        tool_name="register_calendar_url",
        args={
            "member_id": "mem_001",
            "url": "webcal://example.com/cal.ics",
        },
        caller_member_id="mem_001",
        caller_tier="tier1",
        phone_e164="+966500000001",
    )

    assert result["ok"] is True
    assert result["icloud_calendar_url"] == "webcal://example.com/cal.ics"
    mock_ref.update.assert_called_once()
    updates = mock_ref.update.call_args[0][0]
    assert updates["icloud_calendar_url"] == "webcal://example.com/cal.ics"
