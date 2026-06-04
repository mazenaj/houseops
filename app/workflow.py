"""Workflow orchestration for calendar syncing, driver dispatch, onboarding, and driver confirmations."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from google.cloud import firestore

from app.config import RIYADH_TZ
from app.icloud_calendar import fetch_icloud_events
from app.telegram import send_text_message

logger = logging.getLogger(__name__)


def detect_schedule_conflicts(
    db: firestore.Client,
    target_date: date,
) -> tuple[bool, list[str], list[dict[str, Any]], dict[int, str]]:
    """
    Check tomorrow's calendar events for conflicts.
    Returns (has_conflict, list_of_conflict_descriptions, parsed_events, driver_assignments).
    """
    # 1. Fetch Tier 1 principals
    principals_query = db.collection("members").where("role", "==", "tier1").where("active", "==", True)
    principals = list(principals_query.stream())
    
    events = []
    for doc in principals:
        pdata = doc.to_dict() or {}
        name = pdata.get("name", doc.id)
        cal_url = pdata.get("icloud_calendar_url")
        if cal_url:
            user_events = fetch_icloud_events(cal_url, target_date, target_date)
            for ev in user_events:
                ev["owner_name"] = name
                # Exclude all-day events from transit scheduling
                if not ev.get("is_all_day"):
                    events.append(ev)

    conflict_messages = []

    # Check rule 1: Same-principal overlaps (physically impossible)
    passenger_events = {}
    for ev in events:
        owner = ev.get("owner_name", "Unknown")
        if owner not in passenger_events:
            passenger_events[owner] = []
        passenger_events[owner].append(ev)

    for owner, evs in passenger_events.items():
        for i in range(len(evs)):
            for j in range(i + 1, len(evs)):
                start_a = datetime.fromisoformat(evs[i]["start"])
                end_a = datetime.fromisoformat(evs[i]["end"])
                start_b = datetime.fromisoformat(evs[j]["start"])
                end_b = datetime.fromisoformat(evs[j]["end"])
                if max(start_a, start_b) < min(end_a, end_b):
                    conflict_messages.append(
                        f"Overlap on {owner}'s calendar: '{evs[i]['summary']}' and '{evs[j]['summary']}' overlap."
                    )

    # 2. Fetch active drivers & availabilities
    drivers_query = db.collection("drivers").where("active", "==", True)
    drivers = [dict(doc.to_dict(), driver_id=doc.id) for doc in drivers_query.stream()]

    # Fetch availabilities for target date and the day after (to handle midnight crossovers)
    date_str = target_date.isoformat()
    next_date_str = (target_date + timedelta(days=1)).isoformat()
    avail_query = db.collection("driver_availability").where("date", "in", [date_str, next_date_str])
    availabilities = [doc.to_dict() or {} for doc in avail_query.stream()]

    # Load dispatch rules
    dispatch_rules_ref = db.collection("config").document("dispatch_rules")
    dispatch_rules = dispatch_rules_ref.get().to_dict() or {"rules": []}

    # Bipartite matching with preferences
    driver_avail_intervals = {}
    for avail in availabilities:
        dr_id = avail.get("driver_id")
        avail_date_str = avail.get("date")
        if dr_id and avail_date_str:
            avail_date = date.fromisoformat(avail_date_str)
            if dr_id not in driver_avail_intervals:
                driver_avail_intervals[dr_id] = []
            for slot in avail.get("slots", []):
                if slot.get("status") == "available":
                    try:
                        s_t = time.fromisoformat(slot["start_time"])
                        e_t = time.fromisoformat(slot["end_time"])
                        slot_start = datetime.combine(avail_date, s_t).replace(tzinfo=RIYADH_TZ)
                        slot_end = datetime.combine(avail_date, e_t).replace(tzinfo=RIYADH_TZ)
                        if slot_end < slot_start:
                            slot_end += timedelta(days=1)
                        driver_avail_intervals[dr_id].append((slot_start, slot_end))
                    except Exception:
                        continue

    # Merge overlapping or contiguous slots per driver
    merged_driver_intervals = {}
    for dr_id, intervals in driver_avail_intervals.items():
        if not intervals:
            merged_driver_intervals[dr_id] = []
            continue
        sorted_intervals = sorted(intervals, key=lambda x: x[0])
        merged = [sorted_intervals[0]]
        for current in sorted_intervals[1:]:
            prev_start, prev_end = merged[-1]
            curr_start, curr_end = current
            if curr_start <= prev_end:
                merged[-1] = (prev_start, max(prev_end, curr_end))
            else:
                merged.append(current)
        merged_driver_intervals[dr_id] = merged

    def is_driver_available(dr_id: str, ev_start: datetime, ev_end: datetime) -> bool:
        ev_start_tz = ev_start.astimezone(RIYADH_TZ)
        ev_end_tz = ev_end.astimezone(RIYADH_TZ)
        intervals = merged_driver_intervals.get(dr_id, [])
        for start_dt, end_dt in intervals:
            if start_dt <= ev_start_tz and ev_end_tz <= end_dt:
                return True
        return False

    pref_map = {}
    for rule in dispatch_rules.get("rules", []):
        pref_map[rule.get("principal_name")] = rule.get("primary_driver_id")

    assignments = {} # event_idx -> driver_id

    def backtrack(ev_idx: int) -> bool:
        if ev_idx >= len(events):
            return True
        ev = events[ev_idx]
        ev_start = datetime.fromisoformat(ev["start"])
        ev_end = datetime.fromisoformat(ev["end"])
        
        owner = ev.get("owner_name")
        pref_driver = pref_map.get(owner)
        
        # Priority mapping for Errands/Shopping
        summary = ev.get("summary", "").lower()
        if any(kw in summary for kw in ("errand", "shop", "grocer", "purchase")):
            pref_driver = pref_map.get("Errands", pref_driver)
        
        ordered_drivers = list(drivers)
        if pref_driver:
            ordered_drivers.sort(key=lambda d: 0 if d["driver_id"] == pref_driver else 1)

        for dr in ordered_drivers:
            dr_id = dr["driver_id"]
            
            # Check overlap in current assignments
            overlap = False
            for assigned_ev_idx, assigned_dr_id in assignments.items():
                if assigned_dr_id == dr_id:
                    other_ev = events[assigned_ev_idx]
                    o_start = datetime.fromisoformat(other_ev["start"])
                    o_end = datetime.fromisoformat(other_ev["end"])
                    if max(ev_start, o_start) < min(ev_end, o_end):
                        overlap = True
                        break
            if overlap:
                continue

            if is_driver_available(dr_id, ev_start, ev_end):
                assignments[ev_idx] = dr_id
                if backtrack(ev_idx + 1):
                    return True
                del assignments[ev_idx]
        return False

    # Run matching if no overlaps found on same passenger calendars
    if not conflict_messages and events:
        matched = backtrack(0)
        if not matched:
            conflict_messages.append(
                "Driver allocation conflict: Not enough available drivers to cover all concurrent outings."
            )

    has_conflict = len(conflict_messages) > 0
    return has_conflict, conflict_messages, events, assignments


def run_nightly_calendar_sync(db: firestore.Client) -> dict[str, Any]:
    """Runs at 8 PM local time. Syncs next day calendar, sets status, pings on conflict."""
    tomorrow_dt = (datetime.now(RIYADH_TZ) + timedelta(days=1)).date()
    tomorrow_str = tomorrow_dt.isoformat()

    has_conflict, conflict_msgs, events, assignments = detect_schedule_conflicts(db, tomorrow_dt)
    
    status_doc = db.collection("system").document(f"schedule_{tomorrow_str}")
    
    if has_conflict:
        status_doc.set({
            "status": "conflict",
            "date": tomorrow_str,
            "conflicts": conflict_msgs,
            "updated_at": datetime.now(RIYADH_TZ),
        })
        
        # Notify all Tier 1 principals
        alert_text = (
            f"⚠️ Conflict detected in tomorrow's ({tomorrow_str}) calendar schedule:\n"
            + "\n".join(f"- {msg}" for msg in conflict_msgs)
            + "\n\nPlease revise your calendar and inform me once done (replying to this chat will trigger a check)."
        )
        _notify_tier1_users(db, alert_text)
        return {"status": "conflict", "conflicts": conflict_msgs}
    else:
        # Schedule the outings in database
        _commit_outings(db, events, assignments)
        
        status_doc.set({
            "status": "clear",
            "date": tomorrow_str,
            "updated_at": datetime.now(RIYADH_TZ),
        })
        
        # Notify principals and drivers
        _notify_clear_schedule(db, tomorrow_dt, events, assignments)
        return {"status": "clear", "events_count": len(events)}


def recheck_calendar_conflicts(db: firestore.Client) -> str | None:
    """Run conflict re-checks on user replies if next day is conflicted. Returns message to user if handled."""
    tomorrow_dt = (datetime.now(RIYADH_TZ) + timedelta(days=1)).date()
    tomorrow_str = tomorrow_dt.isoformat()
    
    status_doc_ref = db.collection("system").document(f"schedule_{tomorrow_str}")
    status_snap = status_doc_ref.get()
    if not status_snap.exists:
        return None
        
    state = status_snap.to_dict() or {}
    if state.get("status") != "conflict":
        return None  # No active conflict to resolve
        
    logger.info("rechecking_conflicts_on_user_reply date=%s", tomorrow_str)
    has_conflict, conflict_msgs, events, assignments = detect_schedule_conflicts(db, tomorrow_dt)
    
    if has_conflict:
        # Conflict continues, update log
        status_doc_ref.update({
            "conflicts": conflict_msgs,
            "updated_at": datetime.now(RIYADH_TZ),
        })
        
        # Build reply alert
        alert_text = (
            f"⚠️ Conflicts are still present in tomorrow's ({tomorrow_str}) calendar:\n"
            + "\n".join(f"- {msg}" for msg in conflict_msgs)
            + "\n\nPlease revise your Apple Cloud Calendars and reply again."
        )
        
        # Notify other Tier 1 users
        _notify_tier1_users(db, alert_text)
        return alert_text
    else:
        # Clear!
        _commit_outings(db, events, assignments)
        status_doc_ref.update({
            "status": "clear",
            "conflicts": [],
            "updated_at": datetime.now(RIYADH_TZ),
        })
        
        # Notify everyone
        _notify_clear_schedule(db, tomorrow_dt, events, assignments)
        
        return f"🎉 Conflicts resolved! Tomorrow's outings are clear and drivers have been notified."


def _commit_outings(db: firestore.Client, events: list[dict[str, Any]], assignments: dict[int, str]):
    """Write outings to driver_schedule collection."""
    batch = db.batch()
    for idx, ev in enumerate(events):
        driver_id = assignments.get(idx)
        if not driver_id:
            continue
            
        start_time = datetime.fromisoformat(ev["start"])
        end_time = datetime.fromisoformat(ev["end"])
        
        oid = f"out_{start_time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
        outing_ref = db.collection("driver_schedule").document(oid)
        
        payload = {
            "outing_id": oid,
            "start_time": start_time,
            "end_time": end_time,
            "destination": ev.get("location") or "iCloud Event Location",
            "purpose": ev.get("summary") or "Transit Outing",
            "assigned_driver": driver_id,
            "requested_by": "iCloud Calendar Sync",
            "status": "scheduled",
            "passengers": [ev["owner_name"]],
            "notes": ev.get("description") or "",
            "created_at": datetime.now(RIYADH_TZ),
        }
        batch.set(outing_ref, payload, merge=True)
    batch.commit()
    logger.info("committed_calendar_outings count=%d", len(events))


def _notify_tier1_users(db: firestore.Client, text: str, exclude_chat_id: int | None = None):
    principals = db.collection("members").where("role", "==", "tier1").where("active", "==", True).stream()
    for doc in principals:
        pdata = doc.to_dict() or {}
        chat_id = pdata.get("telegram_chat_id")
        if chat_id and chat_id != exclude_chat_id:
            try:
                send_text_message(chat_id, text)
            except Exception as e:
                logger.error("failed_notifying_principal name=%s chat=%s error=%s", pdata.get("name"), chat_id, e)


def _notify_clear_schedule(
    db: firestore.Client,
    target_date: date,
    events: list[dict[str, Any]],
    assignments: dict[int, str],
):
    # Get active drivers
    drivers_query = db.collection("drivers").where("active", "==", True).stream()
    driver_map = {doc.id: doc.to_dict().get("name", doc.id) for doc in drivers_query}

    # Format aggregated schedule for principals
    schedule_lines = [f"📅 Driver Outings Schedule for tomorrow ({target_date.isoformat()}):"]
    driver_lines: dict[str, list[str]] = {dr_id: [] for dr_id in driver_map}

    for idx, ev in enumerate(events):
        dr_id = assignments.get(idx)
        if not dr_id:
            continue
        start_dt = datetime.fromisoformat(ev["start"]).astimezone(RIYADH_TZ)
        end_dt = datetime.fromisoformat(ev["end"]).astimezone(RIYADH_TZ)
        time_str = f"{start_dt.strftime('%I:%M %p')} - {end_dt.strftime('%I:%M %p')}"
        
        line = f"- {time_str}: {ev['owner_name']} ➡️ {ev.get('location') or 'Destination'} ({ev.get('summary')})"
        schedule_lines.append(f"{line} [Driver: {driver_map.get(dr_id, dr_id)}]")
        if dr_id not in driver_lines:
            driver_lines[dr_id] = []
        driver_lines[dr_id].append(line)

    if not events:
        schedule_lines.append("No outings scheduled.")

    # Notify principals
    principals_text = "\n".join(schedule_lines)
    _notify_tier1_users(db, principals_text)

    # Notify drivers individually
    for dr_id, outings in driver_lines.items():
        # Find driver member chat
        dr_doc_snap = db.collection("drivers").document(dr_id).get()
        if not dr_doc_snap.exists:
            continue
        mem_id = dr_doc_snap.to_dict().get("member_id")
        if not mem_id:
            continue
            
        mem_snap = db.collection("members").document(mem_id).get()
        if mem_snap.exists:
            chat_id = mem_snap.to_dict().get("telegram_chat_id")
            if chat_id:
                driver_schedule_text = (
                    f"🚐 Your schedule for tomorrow ({target_date.isoformat()}):\n"
                    + ("\n".join(outings) if outings else "No outings scheduled.")
                )
                try:
                    send_text_message(chat_id, driver_schedule_text)
                except Exception as e:
                    logger.error("failed_notifying_driver dr_id=%s error=%s", dr_id, e)


def run_calendar_onboarding_nag(db: firestore.Client):
    """Runs daily at 10 AM. Nag principals who have not registered their calendar URLs."""
    principals = db.collection("members").where("role", "==", "tier1").where("active", "==", True).stream()
    for doc in principals:
        pdata = doc.to_dict() or {}
        if not pdata.get("icloud_calendar_url"):
            chat_id = pdata.get("telegram_chat_id")
            if chat_id:
                nag_text = (
                    f"Hi {pdata.get('name', 'Principal')}! 👋\n"
                    "This is a reminder to share your shared Apple iCloud Calendar URL for transit scheduling.\n\n"
                    "You can share it by copying the public shared WebCAL link and replying to this bot.\n"
                    "If you need help setting it up, reply with 'Help shared calendar'."
                )
                try:
                    send_text_message(chat_id, nag_text)
                except Exception as e:
                    logger.error("failed_sending_onboarding_nag member=%s error=%s", doc.id, e)


def run_driver_arrival_nag(db: firestore.Client):
    """Runs every 5 minutes. Check completed outings and pings drivers for arrival confirmation."""
    now = datetime.now(RIYADH_TZ)
    # Check all active pings or outings that ended within the last 24 hours
    lookback_limit = now - timedelta(hours=24)
    
    outings = (
        db.collection("driver_schedule")
        .where("status", "==", "scheduled")
        .where("end_time", ">=", lookback_limit)
        .where("end_time", "<", now)
        .stream()
    )

    for doc in outings:
        odata = doc.to_dict() or {}
        oid = doc.id
        driver_id = odata.get("assigned_driver")
        if not driver_id:
            continue
            
        # Get driver chat_id
        dr_snap = db.collection("drivers").document(driver_id).get()
        if not dr_snap.exists:
            continue
        mem_id = dr_snap.to_dict().get("member_id")
        if not mem_id:
            continue
        mem_snap = db.collection("members").document(mem_id).get()
        if not mem_snap.exists:
            continue
        chat_id = mem_snap.to_dict().get("telegram_chat_id")
        if not chat_id:
            continue

        # Check ping status
        ping_ref = db.collection("driver_arrival_pings").document(oid)
        ping_snap = ping_ref.get()
        
        last_pinged = None
        created_at = now
        alert_sent = False
        if ping_snap.exists:
            pdata = ping_snap.to_dict() or {}
            last_pinged = pdata.get("last_pinged_at")
            # Convert to datetime
            if isinstance(last_pinged, str):
                last_pinged = datetime.fromisoformat(last_pinged)
            created_at = pdata.get("created_at", last_pinged or now)
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            alert_sent = pdata.get("alert_sent", False)
            
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=RIYADH_TZ)
        if last_pinged and last_pinged.tzinfo is None:
            last_pinged = last_pinged.replace(tzinfo=RIYADH_TZ)

        # Check if driver has failed to reply within 30 minutes
        if ping_snap.exists and not alert_sent and (now - created_at) >= timedelta(minutes=30):
            driver_name = dr_snap.to_dict().get("name", driver_id)
            destination = odata.get("destination", "Destination")
            end_time = odata.get("end_time")
            if isinstance(end_time, datetime):
                end_time_str = end_time.astimezone(RIYADH_TZ).strftime('%I:%M %p')
            else:
                end_time_str = str(end_time)
                
            alert_msg = (
                f"Driver *{driver_name}* has not confirmed arrival back home "
                f"for outing to *{destination}* (ended at {end_time_str}) "
                f"for over 30 minutes."
            )
            # Send alert to normal channel (Tier 1 principals)
            _notify_tier1_users(db, f"⚠️ Delayed Driver Arrival:\n{alert_msg}")
            ping_ref.update({"alert_sent": True})
            alert_sent = True

        # Check if we should ping (never pinged, or last ping was >= 5 minutes ago)
        should_ping = False
        if not last_pinged:
            should_ping = True
        elif now - last_pinged >= timedelta(minutes=5):
            should_ping = True

        if should_ping:
            ping_text = (
                f"🚐 Outing to {odata.get('destination')} is complete.\n"
                "Please confirm your safe arrival back home by replying YES."
            )
            try:
                send_text_message(chat_id, ping_text)
                ping_ref.set({
                    "outing_id": oid,
                    "driver_id": driver_id,
                    "telegram_chat_id": chat_id,
                    "last_pinged_at": now,
                    "created_at": created_at,
                    "alert_sent": alert_sent,
                    "status": "awaiting_confirmation",
                }, merge=True)
                logger.info("sent_driver_arrival_nag outing_id=%s driver_id=%s", oid, driver_id)
            except Exception as e:
                logger.error("failed_sending_driver_arrival_nag outing_id=%s error=%s", oid, e)


def handle_driver_arrival_reply(db: firestore.Client, driver_member_id: str, text: str) -> str | None:
    """Interceptors: Checks if a driver is replying YES to a pending arrival confirmation."""
    text_clean = text.strip().lower()
    if text_clean not in ("yes", "y", "arrived", "confirm", "confirm arrival", "نعم", "تم"):
        return None

    # Find driver record
    dr_query = db.collection("drivers").where("member_id", "==", driver_member_id).where("active", "==", True).limit(1).stream()
    drivers = list(dr_query)
    if not drivers:
        return None
    driver_id = drivers[0].id

    # Check for active pings for this driver
    pings = (
        db.collection("driver_arrival_pings")
        .where("driver_id", "==", driver_id)
        .where("status", "==", "awaiting_confirmation")
        .stream()
    )

    pings_list = list(pings)
    if not pings_list:
        return None

    batch = db.batch()
    # Confirm arrival and update outings
    for p in pings_list:
        pdata = p.to_dict() or {}
        oid = pdata.get("outing_id")
        if oid:
            # Mark outing as completed
            outing_ref = db.collection("driver_schedule").document(oid)
            batch.update(outing_ref, {
                "status": "completed",
                "completed_at": datetime.now(RIYADH_TZ),
            })
        # Delete the ping
        batch.delete(p.reference)
        
    batch.commit()
    logger.info("driver_arrival_confirmed driver_id=%s ping_count=%d", driver_id, len(pings_list))
    return "Thank you for confirming your arrival. The outing is marked as completed."
