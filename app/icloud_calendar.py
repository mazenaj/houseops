"""iCloud Calendar client for fetching and parsing shared calendar events."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
import httpx
import icalendar
import recurring_ical_events
from app.config import RIYADH_TZ

logger = logging.getLogger(__name__)


def fetch_icloud_events(
    calendar_url: str,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    """
    Fetch a public/shared iCloud calendar (.ics) and extract events within a date range (inclusive).
    Supports recurrences using recurring_ical_events.
    """
    if not calendar_url:
        return []

    # Normalize webcal:// to https://
    http_url = calendar_url
    if http_url.startswith("webcal://"):
        http_url = "https://" + http_url[9:]

    logger.info("fetching_icloud_calendar url=%s", http_url)
    try:
        response = httpx.get(http_url, timeout=10.0)
        response.raise_for_status()
        ical_data = response.text
    except Exception as e:
        logger.error("failed_fetching_icloud_calendar url=%s error=%s", http_url, e)
        return []

    try:
        calendar = icalendar.Calendar.from_ical(ical_data)
    except Exception as e:
        logger.error("failed_parsing_ical_data url=%s error=%s", http_url, e)
        return []

    # recurring_ical_events.of(calendar).between(start, end) expects datetime or date.
    # We pass date/datetime objects. Let's pass date objects.
    # Note: recurring-ical-events 'between' is start-inclusive and end-exclusive by default,
    # but we want inclusive. Let's adjust end_date to end_date + 1 day to be safe.
    from datetime import timedelta

    events = recurring_ical_events.of(calendar).between(
        start_date, end_date + timedelta(days=1)
    )

    parsed_events = []
    for event in events:
        summary = str(event.get("SUMMARY", "")).strip()
        location = str(event.get("LOCATION", "")).strip()
        description = str(event.get("DESCRIPTION", "")).strip()

        # Parse start and end times, normalizing to Riyadh time if they are datetime objects
        dtstart = event.get("DTSTART")
        dtend = event.get("DTEND")

        start_val = dtstart.dt if dtstart else None
        end_val = dtend.dt if dtend else None

        if start_val and (not end_val or end_val == start_val):
            from datetime import timedelta

            if isinstance(start_val, datetime):
                end_val = start_val + timedelta(hours=1)
            elif isinstance(start_val, date):
                end_val = start_val

        # Check if they are datetime or date
        is_all_day = False
        start_iso = ""
        end_iso = ""

        if isinstance(start_val, datetime):
            # Normalize to Riyadh Time
            if start_val.tzinfo is not None:
                start_val = start_val.astimezone(RIYADH_TZ)
            else:
                # Naive assumes Riyadh
                start_val = start_val.replace(tzinfo=RIYADH_TZ)
            start_iso = start_val.isoformat()
        elif isinstance(start_val, date):
            is_all_day = True
            start_iso = start_val.isoformat()

        if isinstance(end_val, datetime):
            if end_val.tzinfo is not None:
                end_val = end_val.astimezone(RIYADH_TZ)
            else:
                end_val = end_val.replace(tzinfo=RIYADH_TZ)
            end_iso = end_val.isoformat()
        elif isinstance(end_val, date):
            end_iso = end_val.isoformat()

        parsed_events.append(
            {
                "summary": summary,
                "location": location,
                "description": description,
                "start": start_iso,
                "end": end_iso,
                "is_all_day": is_all_day,
            }
        )

    # Sort events by start time
    parsed_events.sort(key=lambda x: x["start"])
    logger.info("icloud_events_parsed url=%s count=%d", http_url, len(parsed_events))
    return parsed_events
