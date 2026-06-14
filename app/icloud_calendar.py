"""iCloud Calendar client for fetching and parsing shared calendar events."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
import icalendar
import recurring_ical_events
from concurrent.futures import ThreadPoolExecutor
from app.config import RIYADH_TZ
from app.telegram import http_client

logger = logging.getLogger(__name__)

# Simple in-memory cache for calendar events to reduce CPU/network overhead
_calendar_cache: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
CACHE_TTL = timedelta(minutes=5)


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

    # Check cache first
    cache_key = f"{http_url}_{start_date.isoformat()}_{end_date.isoformat()}"
    now = datetime.now()
    if cache_key in _calendar_cache:
        cached_time, cached_events = _calendar_cache[cache_key]
        if now - cached_time < CACHE_TTL:
            logger.info("returning_cached_icloud_events url=%s", http_url)
            return cached_events

    logger.info("fetching_icloud_calendar url=%s", http_url)
    try:
        # Enforce file size limit (5MB) on stream to prevent DoS/memory exhaustion
        with http_client.stream("GET", http_url, timeout=10.0) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > 5 * 1024 * 1024:
                logger.error(
                    "calendar_file_too_large content_length=%s", content_length
                )
                return []

            chunks = []
            downloaded = 0
            for chunk in resp.iter_text():
                downloaded += len(chunk.encode("utf-8"))
                if downloaded > 5 * 1024 * 1024:
                    logger.error("calendar_file_exceeded_size_limit")
                    return []
                chunks.append(chunk)
            ical_data = "".join(chunks)
    except Exception as e:
        logger.error("failed_fetching_icloud_calendar url=%s error=%s", http_url, e)
        return []

    try:
        calendar = icalendar.Calendar.from_ical(ical_data)
    except Exception as e:
        logger.error("failed_parsing_ical_data url=%s error=%s", http_url, e)
        return []

    events = recurring_ical_events.of(calendar).between(
        start_date, end_date + timedelta(days=1)
    )

    parsed_events = []
    for event in events:
        summary = str(event.get("SUMMARY", "")).strip()
        location = str(event.get("LOCATION", "")).strip()
        description = str(event.get("DESCRIPTION", "")).strip()
        uid = str(event.get("UID", "")).strip()

        # Parse start and end times, normalizing to Riyadh time if they are datetime objects
        dtstart = event.get("DTSTART")
        dtend = event.get("DTEND")

        start_val = dtstart.dt if dtstart else None
        end_val = dtend.dt if dtend else None

        if start_val and (not end_val or end_val == start_val):
            if isinstance(start_val, datetime):
                end_val = start_val + timedelta(hours=1)
            elif isinstance(start_val, date):
                end_val = start_val

        # Check if they are datetime or date
        is_all_day = False
        start_iso = ""
        end_iso = ""

        if isinstance(start_val, datetime):
            if start_val.tzinfo is not None:
                start_val = start_val.astimezone(RIYADH_TZ)
            else:
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
                "uid": uid,
            }
        )

    # Sort events by start time
    parsed_events.sort(key=lambda x: x["start"])
    logger.info("icloud_events_parsed url=%s count=%d", http_url, len(parsed_events))

    # Save to cache
    _calendar_cache[cache_key] = (datetime.now(), parsed_events)
    return parsed_events


def fetch_tier1_calendar_events(
    db: Any, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    """Fetch and aggregate active Tier 1 principals' public iCloud calendar events concurrently."""
    principals_query = (
        db.collection("members")
        .where("role", "==", "tier1")
        .where("active", "==", True)
    )
    aggregated_events = []

    # Fetch members synchronously first
    principals = list(principals_query.stream())

    # Parallel fetch across all member URLs
    with ThreadPoolExecutor() as executor:
        futures = []
        for doc in principals:
            pdata = doc.to_dict() or {}
            name = pdata.get("name", doc.id)
            cal_url = pdata.get("icloud_calendar_url")
            if cal_url:
                futures.append(
                    executor.submit(
                        lambda n, url: (
                            n,
                            fetch_icloud_events(url, start_date, end_date),
                        ),
                        name,
                        cal_url,
                    )
                )

        for f in futures:
            try:
                name, events = f.result()
                for ev in events:
                    ev["owner_name"] = name
                    aggregated_events.append(ev)
            except Exception as e:
                logger.error("failed_fetching_calendar member=%s error=%s", name, e)

    aggregated_events.sort(key=lambda x: x["start"])
    return aggregated_events
