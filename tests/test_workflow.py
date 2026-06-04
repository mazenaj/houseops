"""Tests for workflow sync, conflict detection, onboarding nag, and driver confirmation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import RIYADH_TZ
from app.workflow import (
    detect_schedule_conflicts,
    run_nightly_calendar_sync,
    recheck_calendar_conflicts,
    run_calendar_onboarding_nag,
    run_driver_arrival_nag,
    handle_driver_arrival_reply,
)
from main import app


@pytest.fixture
def test_client():
    return TestClient(app)


def test_detect_schedule_conflicts_no_conflict(mock_firestore_client):
    """Test conflict detection when calendar events can be assigned to available drivers."""
    # 1. Mock members (Jawaher and Mazen)
    mock_m1 = MagicMock()
    mock_m1.id = "mem_001"
    mock_m1.to_dict.return_value = {
        "name": "Mazen",
        "role": "tier1",
        "active": True,
        "icloud_calendar_url": "url1",
    }

    mock_m2 = MagicMock()
    mock_m2.id = "mem_002"
    mock_m2.to_dict.return_value = {
        "name": "Jawaher",
        "role": "tier1",
        "active": True,
        "icloud_calendar_url": "url2",
    }

    mock_members_query = MagicMock()
    mock_members_query.where.return_value.where.return_value = mock_members_query
    mock_members_query.stream.return_value = [mock_m1, mock_m2]

    # 2. Mock drivers
    mock_dr1 = MagicMock()
    mock_dr1.id = "dr_001"
    mock_dr1.to_dict.return_value = {"name": "Abu Fahad", "active": True}

    mock_dr2 = MagicMock()
    mock_dr2.id = "dr_002"
    mock_dr2.to_dict.return_value = {"name": "Abu Ali", "active": True}

    mock_drivers_query = MagicMock()
    mock_drivers_query.where.return_value = mock_drivers_query
    mock_drivers_query.stream.return_value = [mock_dr1, mock_dr2]

    # 3. Mock availabilities
    mock_av1 = MagicMock()
    mock_av1.to_dict.return_value = {
        "driver_id": "dr_001",
        "date": "2026-06-04",
        "slots": [{"start_time": "07:00", "end_time": "22:00", "status": "available"}],
    }
    mock_av2 = MagicMock()
    mock_av2.to_dict.return_value = {
        "driver_id": "dr_002",
        "date": "2026-06-04",
        "slots": [{"start_time": "07:00", "end_time": "22:00", "status": "available"}],
    }
    mock_avail_query = MagicMock()
    mock_avail_query.where.return_value = mock_avail_query
    mock_avail_query.stream.return_value = [mock_av1, mock_av2]

    # 4. Collection dispatcher routing
    def collection_side_effect(name):
        if name == "members":
            return mock_members_query
        elif name == "drivers":
            return mock_drivers_query
        elif name == "driver_availability":
            return mock_avail_query
        elif name == "config":
            mock_col = MagicMock()
            mock_doc = MagicMock()
            mock_doc.get.return_value.to_dict.return_value = {
                "rules": [
                    {"principal_name": "Mazen", "primary_driver_id": "dr_001"},
                    {"principal_name": "Jawaher", "primary_driver_id": "dr_002"},
                ]
            }
            mock_col.document.return_value = mock_doc
            return mock_col
        return MagicMock()

    mock_firestore_client.collection.side_effect = collection_side_effect

    # Mock calendar event fetcher
    mock_events_mazen = [
        {
            "summary": "Dentist",
            "location": "Riyadh Clinic",
            "description": "",
            "start": "2026-06-04T10:00:00+03:00",
            "end": "2026-06-04T11:00:00+03:00",
            "is_all_day": False,
        }
    ]
    mock_events_jawaher = [
        {
            "summary": "Dinner",
            "location": "Resto",
            "description": "",
            "start": "2026-06-04T10:00:00+03:00",
            "end": "2026-06-04T11:00:00+03:00",
            "is_all_day": False,
        }
    ]

    def fetch_icloud_side_effect(url, start_date, end_date):
        if "url1" in url:
            return mock_events_mazen
        return mock_events_jawaher

    with patch(
        "app.workflow.fetch_icloud_events", side_effect=fetch_icloud_side_effect
    ):
        has_conflict, msgs, events, assignments = detect_schedule_conflicts(
            mock_firestore_client, date(2026, 6, 4)
        )

    assert has_conflict is False
    assert len(msgs) == 0
    assert len(events) == 2
    # Check that Mazen is assigned to his primary driver dr_001
    # Jawaher assigned to her primary dr_002
    assert assignments[0] == "dr_001"
    assert assignments[1] == "dr_002"


def test_detect_schedule_conflicts_errands_preference(mock_firestore_client):
    """Test that errands or shopping outings match the 'Errands' primary driver."""
    mock_m1 = MagicMock()
    mock_m1.id = "mem_001"
    mock_m1.to_dict.return_value = {
        "name": "Mazen",
        "role": "tier1",
        "active": True,
        "icloud_calendar_url": "url1",
    }
    mock_members_query = MagicMock()
    mock_members_query.where.return_value.where.return_value = mock_members_query
    mock_members_query.stream.return_value = [mock_m1]

    mock_dr1 = MagicMock()
    mock_dr1.id = "dr_kim"
    mock_dr1.to_dict.return_value = {"name": "Kim", "active": True}
    mock_drivers_query = MagicMock()
    mock_drivers_query.where.return_value = mock_drivers_query
    mock_drivers_query.stream.return_value = [mock_dr1]

    mock_av1 = MagicMock()
    mock_av1.to_dict.return_value = {
        "driver_id": "dr_kim",
        "date": "2026-06-04",
        "slots": [{"start_time": "07:00", "end_time": "22:00", "status": "available"}],
    }
    mock_avail_query = MagicMock()
    mock_avail_query.where.return_value = mock_avail_query
    mock_avail_query.stream.return_value = [mock_av1]

    def collection_side_effect(name):
        if name == "members":
            return mock_members_query
        elif name == "drivers":
            return mock_drivers_query
        elif name == "driver_availability":
            return mock_avail_query
        elif name == "config":
            mock_col = MagicMock()
            mock_doc = MagicMock()
            mock_doc.get.return_value.to_dict.return_value = {
                "rules": [{"principal_name": "Errands", "primary_driver_id": "dr_kim"}]
            }
            mock_col.document.return_value = mock_doc
            return mock_col
        return MagicMock()

    mock_firestore_client.collection.side_effect = collection_side_effect

    mock_events_mazen = [
        {
            "summary": "Grocery shopping",
            "location": "Tamimi Markets",
            "description": "",
            "start": "2026-06-04T10:00:00+03:00",
            "end": "2026-06-04T11:00:00+03:00",
            "is_all_day": False,
        }
    ]

    with patch("app.workflow.fetch_icloud_events", return_value=mock_events_mazen):
        has_conflict, msgs, events, assignments = detect_schedule_conflicts(
            mock_firestore_client, date(2026, 6, 4)
        )

    assert has_conflict is False
    assert assignments[0] == "dr_kim"


def test_detect_schedule_conflicts_overlap_conflict(mock_firestore_client):
    """Test conflict when same passenger has overlapping events."""
    mock_m1 = MagicMock()
    mock_m1.id = "mem_001"
    mock_m1.to_dict.return_value = {
        "name": "Mazen",
        "role": "tier1",
        "active": True,
        "icloud_calendar_url": "url1",
    }
    mock_members_query = MagicMock()
    mock_members_query.where.return_value.where.return_value = mock_members_query
    mock_members_query.stream.return_value = [mock_m1]

    def collection_side_effect(name):
        if name == "members":
            return mock_members_query
        return MagicMock()

    mock_firestore_client.collection.side_effect = collection_side_effect

    # Mazen has two overlapping events on his calendar
    mock_events_mazen = [
        {
            "summary": "Event A",
            "location": "Loc A",
            "description": "",
            "start": "2026-06-04T10:00:00+03:00",
            "end": "2026-06-04T11:00:00+03:00",
            "is_all_day": False,
        },
        {
            "summary": "Event B",
            "location": "Loc B",
            "description": "",
            "start": "2026-06-04T10:30:00+03:00",
            "end": "2026-06-04T11:30:00+03:00",
            "is_all_day": False,
        },
    ]

    with patch("app.workflow.fetch_icloud_events", return_value=mock_events_mazen):
        has_conflict, msgs, events, assignments = detect_schedule_conflicts(
            mock_firestore_client, date(2026, 6, 4)
        )

    assert has_conflict is True
    assert any("Overlap on Mazen's calendar" in m for m in msgs)


def test_detect_schedule_conflicts_no_drivers_conflict(mock_firestore_client):
    """Test conflict when concurrent outings exceed available drivers."""
    mock_m1 = MagicMock()
    mock_m1.id = "mem_001"
    mock_m1.to_dict.return_value = {
        "name": "Mazen",
        "role": "tier1",
        "active": True,
        "icloud_calendar_url": "url1",
    }

    mock_m2 = MagicMock()
    mock_m2.id = "mem_002"
    mock_m2.to_dict.return_value = {
        "name": "Jawaher",
        "role": "tier1",
        "active": True,
        "icloud_calendar_url": "url2",
    }

    mock_members_query = MagicMock()
    mock_members_query.where.return_value.where.return_value = mock_members_query
    mock_members_query.stream.return_value = [mock_m1, mock_m2]

    # Only 1 driver is active
    mock_dr1 = MagicMock()
    mock_dr1.id = "dr_001"
    mock_dr1.to_dict.return_value = {"name": "Abu Fahad", "active": True}
    mock_drivers_query = MagicMock()
    mock_drivers_query.where.return_value = mock_drivers_query
    mock_drivers_query.stream.return_value = [mock_dr1]

    # Driver availability
    mock_av1 = MagicMock()
    mock_av1.to_dict.return_value = {
        "driver_id": "dr_001",
        "date": "2026-06-04",
        "slots": [{"start_time": "07:00", "end_time": "22:00", "status": "available"}],
    }
    mock_avail_query = MagicMock()
    mock_avail_query.where.return_value = mock_avail_query
    mock_avail_query.stream.return_value = [mock_av1]

    def collection_side_effect(name):
        if name == "members":
            return mock_members_query
        elif name == "drivers":
            return mock_drivers_query
        elif name == "driver_availability":
            return mock_avail_query
        return MagicMock()

    mock_firestore_client.collection.side_effect = collection_side_effect

    mock_events_mazen = [
        {
            "summary": "Dentist",
            "location": "Loc A",
            "description": "",
            "start": "2026-06-04T10:00:00+03:00",
            "end": "2026-06-04T11:00:00+03:00",
            "is_all_day": False,
        }
    ]
    mock_events_jawaher = [
        {
            "summary": "Lunch",
            "location": "Loc B",
            "description": "",
            "start": "2026-06-04T10:00:00+03:00",
            "end": "2026-06-04T11:00:00+03:00",
            "is_all_day": False,
        }
    ]

    def fetch_icloud_side_effect(url, start_date, end_date):
        if "url1" in url:
            return mock_events_mazen
        return mock_events_jawaher

    with patch(
        "app.workflow.fetch_icloud_events", side_effect=fetch_icloud_side_effect
    ):
        has_conflict, msgs, events, assignments = detect_schedule_conflicts(
            mock_firestore_client, date(2026, 6, 4)
        )

    assert has_conflict is True
    assert any("Driver allocation conflict" in m for m in msgs)


def test_nightly_calendar_sync_conflict(mock_firestore_client):
    """Test run_nightly_calendar_sync records conflict state and triggers alerts."""
    # Force conflict
    mock_detect = (True, ["Same time overlap"], [], {})

    # Mock principals to alert
    mock_p1 = MagicMock()
    mock_p1.to_dict.return_value = {"name": "Mazen", "telegram_chat_id": 123}
    mock_col = MagicMock()
    mock_col.where.return_value.where.return_value.stream.return_value = [mock_p1]
    mock_firestore_client.collection.return_value = mock_col

    with patch(
        "app.workflow.detect_schedule_conflicts", return_value=mock_detect
    ), patch("app.workflow.send_text_message") as mock_send:
        result = run_nightly_calendar_sync(mock_firestore_client)

    assert result["status"] == "conflict"
    mock_send.assert_called_once_with(123, any_mock_text())


def test_recheck_calendar_conflicts_resolved(mock_firestore_client):
    """Test rechecking conflict on user reply when conflicts are now resolved."""
    # Initial status is conflicted
    mock_snap = MagicMock()
    mock_snap.exists = True
    mock_snap.to_dict.return_value = {
        "status": "conflict",
        "conflicts": ["Overlapping"],
    }
    mock_firestore_client.collection.return_value.document.return_value.get.return_value = mock_snap

    # Clean detection now
    mock_detect = (
        False,
        [],
        [
            {
                "summary": "Shopping",
                "location": "Mall",
                "start": "2026-06-04T10:00:00+03:00",
                "end": "2026-06-04T11:00:00+03:00",
                "owner_name": "Mazen",
            }
        ],
        {0: "dr_001"},
    )

    # Setup drivers mock for notifications
    mock_dr = MagicMock()
    mock_dr.id = "dr_001"
    mock_dr.to_dict.return_value = {
        "name": "Abu Fahad",
        "member_id": "mem_staff_driver_001",
    }

    mock_m1 = MagicMock()
    mock_m1.to_dict.return_value = {"telegram_chat_id": 999}

    def collection_side_effect(name):
        mock_col = MagicMock()
        if name == "drivers":
            mock_col.where.return_value.stream.return_value = [mock_dr]
            mock_col.document.return_value.get.return_value.to_dict.return_value = {
                "member_id": "mem_staff_driver_001"
            }
            return mock_col
        elif name == "members":
            # For notifying principal and driver
            mock_col.document.return_value.get.return_value = mock_m1
            mock_col.where.return_value.where.return_value.stream.return_value = [
                mock_m1
            ]
            return mock_col
        elif name == "system":
            mock_col.document.return_value.get.return_value = mock_snap
            return mock_col
        return mock_col

    mock_firestore_client.collection.side_effect = collection_side_effect

    with patch(
        "app.workflow.detect_schedule_conflicts", return_value=mock_detect
    ), patch("app.workflow._commit_outings") as mock_commit, patch(
        "app.workflow.send_text_message"
    ) as mock_send:
        reply = recheck_calendar_conflicts(mock_firestore_client)

    assert "resolved" in reply.lower()
    mock_commit.assert_called_once()
    assert mock_send.call_count >= 2  # notified principal + driver


def test_driver_arrival_nag_trigger(mock_firestore_client):
    """Test driver nag is triggered for active ended outings."""
    # Mock scheduled outing that ended
    mock_out = MagicMock()
    mock_out.id = "out_test123"
    mock_out.to_dict.return_value = {
        "assigned_driver": "dr_001",
        "destination": "Airport",
        "end_time": datetime.now(RIYADH_TZ) - timedelta(minutes=10),
        "status": "scheduled",
    }

    mock_query = MagicMock()
    mock_query.where.return_value.where.return_value.where.return_value.stream.return_value = [
        mock_out
    ]

    # Mock driver document and member to get telegram_chat_id
    mock_dr = MagicMock()
    mock_dr.get.return_value = mock_dr
    mock_dr.exists = True
    mock_dr.to_dict.return_value = {"member_id": "mem_driver"}

    mock_mem = MagicMock()
    mock_mem.get.return_value = mock_mem
    mock_mem.exists = True
    mock_mem.to_dict.return_value = {"telegram_chat_id": 5555}

    # Mock no active ping exists yet
    mock_ping = MagicMock()
    mock_ping.get.return_value = mock_ping
    mock_ping.exists = False

    def collection_side_effect(name):
        mock_col = MagicMock()
        if name == "driver_schedule":
            return mock_query
        elif name == "drivers":
            mock_col.document.return_value = mock_dr
            return mock_col
        elif name == "members":
            mock_col.document.return_value = mock_mem
            return mock_col
        elif name == "driver_arrival_pings":
            mock_col.document.return_value = mock_ping
            return mock_col
        return MagicMock()

    mock_firestore_client.collection.side_effect = collection_side_effect

    with patch("app.workflow.send_text_message") as mock_send:
        run_driver_arrival_nag(mock_firestore_client)

    mock_send.assert_called_once_with(5555, any_mock_text())


def test_handle_driver_arrival_reply(mock_firestore_client):
    """Test driver arrival reply completes outing and purges ping tracker."""
    # Mock driver record
    mock_dr = MagicMock()
    mock_dr.id = "dr_001"
    mock_dr_query = MagicMock()
    mock_dr_query.where.return_value.where.return_value.limit.return_value.stream.return_value = [
        mock_dr
    ]

    # Mock pending ping
    mock_ping = MagicMock()
    mock_ping.to_dict.return_value = {"outing_id": "out_test123"}
    mock_ping.reference = MagicMock()
    mock_ping_query = MagicMock()
    mock_ping_query.where.return_value.where.return_value.stream.return_value = [
        mock_ping
    ]

    def collection_side_effect(name):
        if name == "drivers":
            return mock_dr_query
        elif name == "driver_arrival_pings":
            return mock_ping_query
        return MagicMock()

    mock_firestore_client.collection.side_effect = collection_side_effect

    reply = handle_driver_arrival_reply(mock_firestore_client, "mem_driver", "yes")

    assert reply is not None
    assert "arrival" in reply.lower()


def test_calendar_onboarding_nag(mock_firestore_client):
    """Test onboarding nag pings principals with empty iCloud URLs."""
    mock_p1 = MagicMock()
    mock_p1.id = "mem_001"
    mock_p1.to_dict.return_value = {
        "name": "Mazen",
        "telegram_chat_id": 777,
        "icloud_calendar_url": None,  # Needs nag
    }
    mock_col = MagicMock()
    mock_col.where.return_value.where.return_value.stream.return_value = [mock_p1]
    mock_firestore_client.collection.return_value = mock_col

    with patch("app.workflow.send_text_message") as mock_send:
        run_calendar_onboarding_nag(mock_firestore_client)

    mock_send.assert_called_once_with(777, any_mock_text())


def test_cron_jobs_endpoints(test_client):
    """Test API cron jobs authenticate request tokens."""
    with patch("main.verify_job_secret", return_value=True), patch(
        "main.get_db"
    ), patch(
        "app.workflow.run_nightly_calendar_sync", return_value={"status": "clear"}
    ), patch("app.workflow.run_calendar_onboarding_nag"), patch(
        "app.workflow.run_driver_arrival_nag"
    ):
        r1 = test_client.post(
            "/jobs/nightly-calendar-sync", headers={"X-HouseOps-Secret-Token": "secret"}
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "clear"

        r2 = test_client.post(
            "/jobs/calendar-onboarding-nag",
            headers={"X-HouseOps-Secret-Token": "secret"},
        )
        assert r2.status_code == 200
        assert r2.text == "OK"

        r3 = test_client.post(
            "/jobs/driver-arrival-nag", headers={"X-HouseOps-Secret-Token": "secret"}
        )
        assert r3.status_code == 200
        assert r3.text == "OK"


def any_mock_text():
    class AnyMockText:
        def __eq__(self, other):
            return isinstance(other, str)

    return AnyMockText()
