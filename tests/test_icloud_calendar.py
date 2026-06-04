"""Tests for the iCloud calendar client."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from app.icloud_calendar import fetch_icloud_events

MOCK_ICAL = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//iCloud Web Calendar 1.0//EN
BEGIN:VEVENT
UID:event-1@example.com
DTSTART;TZID=Asia/Riyadh:20260604T100000
DTEND;TZID=Asia/Riyadh:20260604T110000
SUMMARY:Dentist appointment
LOCATION:Riyadh Dental Clinic
DESCRIPTION:Regular checkup
END:VEVENT
BEGIN:VEVENT
UID:event-2@example.com
DTSTART:20260604T120000Z
DTEND:20260604T130000Z
SUMMARY:Lunch with Mazen
LOCATION:Downtown Riyadh
DESCRIPTION:Discuss driver scheduling
END:VEVENT
BEGIN:VEVENT
UID:event-3@example.com
DTSTART;VALUE=DATE:20260605
DTEND;VALUE=DATE:20260606
SUMMARY:Jawaher Birthday
LOCATION:
DESCRIPTION:
END:VEVENT
END:VCALENDAR
"""


def test_fetch_icloud_events_success(mocker):
    # Mock httpx.get
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = MOCK_ICAL
    mock_response.raise_for_status = MagicMock()

    mock_get = mocker.patch("httpx.get", return_value=mock_response)

    url = "webcal://p63-calendarws.icloud.com/ca/subscribe/1/abc"
    start_date = date(2026, 6, 4)
    end_date = date(2026, 6, 5)

    events = fetch_icloud_events(url, start_date, end_date)

    # Verify normalization and HTTP call
    mock_get.assert_called_once_with(
        "https://p63-calendarws.icloud.com/ca/subscribe/1/abc", timeout=10.0
    )

    assert len(events) == 3

    # Event 1: Local timezone
    ev1 = events[0]
    assert ev1["summary"] == "Dentist appointment"
    assert ev1["location"] == "Riyadh Dental Clinic"
    assert ev1["description"] == "Regular checkup"
    assert ev1["is_all_day"] is False
    assert ev1["start"].startswith("2026-06-04T10:00:00")
    assert "+03:00" in ev1["start"]

    # Event 2: UTC timezone (12:00 UTC -> 15:00 Riyadh)
    ev2 = events[1]
    assert ev2["summary"] == "Lunch with Mazen"
    assert ev2["location"] == "Downtown Riyadh"
    assert ev2["is_all_day"] is False
    assert ev2["start"].startswith("2026-06-04T15:00:00")
    assert "+03:00" in ev2["start"]

    # Event 3: All-day event on June 5
    ev3 = events[2]
    assert ev3["summary"] == "Jawaher Birthday"
    assert ev3["is_all_day"] is True
    assert ev3["start"] == "2026-06-05"


def test_fetch_icloud_events_http_error(mocker):
    mocker.patch("httpx.get", side_effect=Exception("Connection error"))

    url = "https://example.com/bad.ics"
    events = fetch_icloud_events(url, date(2026, 6, 4), date(2026, 6, 4))
    assert events == []


def test_fetch_icloud_events_missing_dtend(mocker):
    ical_no_dtend = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-no-dtend@example.com
DTSTART;TZID=Asia/Riyadh:20260604T100000
SUMMARY:Meeting with no end time
END:VEVENT
END:VCALENDAR
"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = ical_no_dtend
    mock_response.raise_for_status = MagicMock()
    mocker.patch("httpx.get", return_value=mock_response)

    events = fetch_icloud_events(
        "https://example.com/cal.ics", date(2026, 6, 4), date(2026, 6, 4)
    )
    assert len(events) == 1
    ev = events[0]
    assert ev["summary"] == "Meeting with no end time"
    # Starts at 10:00, ends at 11:00 (1 hour default duration)
    assert ev["start"].startswith("2026-06-04T10:00:00")
    assert ev["end"].startswith("2026-06-04T11:00:00")
