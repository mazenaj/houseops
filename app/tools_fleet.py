"""MODULE 1 tools — Fleet & Logistics (Phase 1, SCHEMA §6)."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from concurrent.futures import ThreadPoolExecutor

from google.cloud import firestore

from app.config import RIYADH_TZ
from app.firestore_db import set_pending_confirmation
from app.icloud_calendar import fetch_tier1_calendar_events

logger = logging.getLogger(__name__)

FLEET_TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_schedule",
        "description": "List driver schedules and outings for a specific date or date range (YYYY-MM-DD or YYYY-MM-DD to YYYY-MM-DD).",
        "parameters": {
            "type": "object",
            "properties": {
                "date_range": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD or range 'YYYY-MM-DD to YYYY-MM-DD'",
                }
            },
            "required": ["date_range"],
        },
    },
    {
        "name": "manage_outing",
        "description": "Create or cancel a driver outing (Tier 1). Requires user confirmation before persist.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "cancel"],
                    "description": "Action to perform",
                },
                "outing_id": {
                    "type": "string",
                    "description": "Target outing_id (required for cancel, optional for create)",
                },
                "assigned_driver": {
                    "type": "string",
                    "description": "Target driver_id (required for create)",
                },
                "start_time": {
                    "type": "string",
                    "description": "ISO datetime string, e.g. YYYY-MM-DDTHH:MM:SS+03:00 (required for create)",
                },
                "end_time": {
                    "type": "string",
                    "description": "ISO datetime string, e.g. YYYY-MM-DDTHH:MM:SS+03:00 (required for create)",
                },
                "destination": {
                    "type": "string",
                    "description": "Destination name (optional)",
                },
                "purpose": {
                    "type": "string",
                    "description": "Errand description or purpose (optional)",
                },
                "passengers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of passenger names",
                },
                "notes": {"type": "string", "description": "Optional driver notes"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "update_driver_availability",
        "description": "Update a driver's hourly availability for a specific date (Tier 2 - Drivers).",
        "parameters": {
            "type": "object",
            "properties": {
                "driver_id": {"type": "string"},
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start_time": {"type": "string", "description": "HH:MM"},
                            "end_time": {"type": "string", "description": "HH:MM"},
                            "status": {
                                "type": "string",
                                "enum": ["available", "busy", "off"],
                            },
                        },
                        "required": ["start_time", "end_time", "status"],
                    },
                },
                "notes": {"type": "string"},
            },
            "required": ["driver_id", "date", "slots"],
        },
    },
    {
        "name": "get_calendar_events",
        "description": "Fetch public Apple iCloud Calendars of the Tier 1 principals for a specific date or range (YYYY-MM-DD or YYYY-MM-DD to YYYY-MM-DD).",
        "parameters": {
            "type": "object",
            "properties": {
                "date_range": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD or range 'YYYY-MM-DD to YYYY-MM-DD'",
                }
            },
            "required": ["date_range"],
        },
    },
    {
        "name": "register_calendar_url",
        "description": "Register or update a principal's shared Apple iCloud Calendar URL (Tier 1 only).",
        "parameters": {
            "type": "object",
            "properties": {
                "member_id": {
                    "type": "string",
                    "description": "The target member_id to update (must be a Tier 1 principal)",
                },
                "url": {
                    "type": "string",
                    "description": "The Apple iCloud shared calendar URL (starts with webcal:// or https://)",
                },
            },
            "required": ["member_id", "url"],
        },
    },
    {
        "name": "get_pooling_suggestions",
        "description": "Fetch ride pooling suggestions for a specific date (YYYY-MM-DD).",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD to check for ride pooling opportunities",
                }
            },
            "required": ["date"],
        },
    },
]


def parse_date_range(date_range_str: str) -> tuple[date, date]:
    """Parse YYYY-MM-DD or YYYY-MM-DD to YYYY-MM-DD."""
    date_range_str = date_range_str.strip()
    if " to " in date_range_str:
        start_part, end_part = date_range_str.split(" to ", 1)
        start_dt = date.fromisoformat(start_part.strip())
        end_dt = date.fromisoformat(end_part.strip())
    else:
        start_dt = date.fromisoformat(date_range_str)
        end_dt = start_dt
    return start_dt, end_dt


def get_schedule(db: firestore.Client, date_range: str) -> dict[str, Any]:
    """List drivers, their availabilities, and schedules concurrently to reduce latency."""
    try:
        start_date, end_date = parse_date_range(date_range)
    except Exception as e:
        return {"ok": False, "error": f"invalid_date_range_format: {e}"}

    # Convert start/end dates to Riyadh datetime bounds for timezone-aware Firestore queries
    start_dt = datetime.combine(start_date, time.min).replace(tzinfo=RIYADH_TZ)
    end_dt = datetime.combine(end_date, time.max).replace(tzinfo=RIYADH_TZ)

    curr_date = start_date
    date_strings = []
    while curr_date <= end_date:
        date_strings.append(curr_date.isoformat())
        curr_date += timedelta(days=1)

    # Parallelize queries across driver metadata, availabilities, and schedule outings
    with ThreadPoolExecutor() as executor:
        future_drivers = executor.submit(
            lambda: list(db.collection("drivers").where("active", "==", True).stream())
        )

        if date_strings:
            future_avail = executor.submit(
                lambda: list(
                    db.collection("driver_availability")
                    .where("date", ">=", date_strings[0])
                    .where("date", "<=", date_strings[-1])
                    .stream()
                )
            )
        else:
            future_avail = executor.submit(lambda: [])

        future_outings = executor.submit(
            lambda: list(
                db.collection("driver_schedule")
                .where("start_time", ">=", start_dt)
                .where("start_time", "<=", end_dt)
                .stream()
            )
        )

        drivers_docs = future_drivers.result()
        avail_docs = future_avail.result()
        outings_docs = future_outings.result()

    drivers = []
    driver_map = {}
    for doc in drivers_docs:
        ddata = doc.to_dict() or {}
        ddata["driver_id"] = doc.id
        drivers.append(ddata)
        driver_map[doc.id] = ddata.get("name", doc.id)

    availabilities = []
    for doc in avail_docs:
        adata = doc.to_dict() or {}
        adata["availability_id"] = doc.id
        availabilities.append(adata)

    outings = []
    for doc in outings_docs:
        odata = doc.to_dict() or {}
        odata["outing_id"] = doc.id
        if isinstance(odata.get("start_time"), datetime):
            odata["start_time"] = odata["start_time"].astimezone(RIYADH_TZ).isoformat()
        if isinstance(odata.get("end_time"), datetime):
            odata["end_time"] = odata["end_time"].astimezone(RIYADH_TZ).isoformat()
        driver_id = odata.get("assigned_driver")
        odata["assigned_driver_name"] = driver_map.get(driver_id, driver_id)
        outings.append(odata)

    return {
        "ok": True,
        "date_range": f"{start_date.isoformat()} to {end_date.isoformat()}",
        "drivers": drivers,
        "availabilities": availabilities,
        "outings": outings,
    }


def _build_outing_payload(
    action: str,
    assigned_driver: str,
    start_time: str,
    end_time: str,
    requested_by: str,
    destination: str | None = None,
    purpose: str | None = None,
    passengers: list[str] | None = None,
    notes: str | None = None,
    outing_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    oid = outing_id or f"out_{uuid.uuid4().hex[:8]}"

    start_dt = datetime.fromisoformat(start_time)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=RIYADH_TZ)
    end_dt = datetime.fromisoformat(end_time)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=RIYADH_TZ)

    payload = {
        "outing_id": oid,
        "start_time": start_dt,
        "end_time": end_dt,
        "destination": destination or "",
        "purpose": purpose or "",
        "assigned_driver": assigned_driver,
        "requested_by": requested_by,
        "status": "scheduled",
        "passengers": passengers or [],
        "notes": notes or "",
        "created_at": datetime.now(RIYADH_TZ),
    }
    return oid, payload


@firestore.transactional
def _txn_cancel_outing(
    transaction: firestore.Transaction,
    outing_ref: firestore.DocumentReference,
    now: datetime,
) -> dict[str, Any]:
    snap = outing_ref.get(transaction=transaction)
    if not snap.exists:
        return {"ok": False, "error": "outing_not_found"}
    odata = snap.to_dict() or {}
    if odata.get("status") == "cancelled":
        return {"ok": False, "error": "already_cancelled"}
    transaction.update(outing_ref, {"status": "cancelled", "updated_at": now})
    return {"ok": True}


@firestore.transactional
def _txn_create_outing(
    transaction: firestore.Transaction,
    outing_ref: firestore.DocumentReference,
    doc_payload: dict[str, Any],
) -> dict[str, Any]:
    _snap = outing_ref.get(transaction=transaction)
    # Check invariant if necessary, then set
    transaction.set(outing_ref, doc_payload, merge=True)
    return {"ok": True}


def manage_outing(
    db: firestore.Client,
    action: str,
    phone_e164: str,
    caller_member_id: str,
    outing_id: str | None = None,
    assigned_driver: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    destination: str | None = None,
    purpose: str | None = None,
    passengers: list[str] | None = None,
    notes: str | None = None,
    *,
    skip_confirmation: bool = False,
) -> dict[str, Any]:
    """Tier 1 only. Sets pending_confirmation for outing creation or cancellation."""
    if action == "cancel":
        if not outing_id:
            return {"ok": False, "error": "missing_outing_id_for_cancellation"}

        outing_ref = db.collection("driver_schedule").document(outing_id)
        snap = outing_ref.get()
        if not snap.exists:
            return {"ok": False, "error": "outing_not_found"}
        odata = snap.to_dict() or {}

        driver_name = odata.get("assigned_driver", "Unknown Driver")
        if odata.get("assigned_driver"):
            dr_snap = db.collection("drivers").document(odata["assigned_driver"]).get()
            if dr_snap.exists:
                driver_name = dr_snap.to_dict().get("name", driver_name)

        summary = f"Cancel outing to {odata.get('destination')} scheduled with driver {driver_name}."

        if skip_confirmation:
            transaction = db.transaction()
            res = _txn_cancel_outing(transaction, outing_ref, datetime.now(RIYADH_TZ))
            if not res.get("ok"):
                return res
            logger.info("outing_cancelled outing_id=%s", outing_id)
            return {"ok": True, "outing_id": outing_id, "status": "cancelled"}

        set_pending_confirmation(
            db,
            phone_e164,
            action="manage_outing",
            payload={
                "action": "cancel",
                "outing_id": outing_id,
            },
            summary=summary,
        )
        return {
            "ok": True,
            "pending_confirmation": True,
            "summary": summary,
            "message": "Awaiting user confirmation to cancel outing.",
        }

    elif action == "create":
        if not (assigned_driver and start_time and end_time):
            return {"ok": False, "error": "missing_required_fields_for_creation"}

        dr_snap = db.collection("drivers").document(assigned_driver).get()
        driver_name = (
            dr_snap.to_dict().get("name", assigned_driver)
            if dr_snap.exists
            else assigned_driver
        )

        oid, payload = _build_outing_payload(
            action=action,
            assigned_driver=assigned_driver,
            start_time=start_time,
            end_time=end_time,
            destination=destination,
            purpose=purpose,
            requested_by=caller_member_id,
            passengers=passengers,
            notes=notes,
            outing_id=outing_id,
        )

        start_dt = payload["start_time"].astimezone(RIYADH_TZ)
        time_str = start_dt.strftime("%Y-%m-%d at %I:%M %p")

        desc_parts = []
        if destination:
            desc_parts.append(f"to {destination}")
        if purpose:
            desc_parts.append(f"({purpose})")
        desc_str = " " + " ".join(desc_parts) if desc_parts else ""

        summary = f"Schedule driver {driver_name} for outing{desc_str} on {time_str}."

        if skip_confirmation:
            transaction = db.transaction()
            outing_ref = db.collection("driver_schedule").document(oid)
            _txn_create_outing(transaction, outing_ref, payload)
            logger.info("outing_created outing_id=%s", oid)

            ser_payload = dict(payload)
            ser_payload["start_time"] = ser_payload["start_time"].isoformat()
            ser_payload["end_time"] = ser_payload["end_time"].isoformat()
            ser_payload["created_at"] = ser_payload["created_at"].isoformat()
            return {"ok": True, "outing_id": oid, "outing": ser_payload}

        set_pending_confirmation(
            db,
            phone_e164,
            action="manage_outing",
            payload={
                "action": "create",
                "assigned_driver": assigned_driver,
                "start_time": start_time,
                "end_time": end_time,
                "destination": destination,
                "purpose": purpose,
                "requested_by": caller_member_id,
                "passengers": passengers,
                "notes": notes,
                "outing_id": oid,
            },
            summary=summary,
        )
        return {
            "ok": True,
            "pending_confirmation": True,
            "summary": summary,
            "message": "Awaiting user confirmation to create outing.",
        }

    else:
        return {"ok": False, "error": f"unsupported_action: {action}"}


def execute_pending_manage_outing(
    db: firestore.Client, payload: dict[str, Any]
) -> dict[str, Any]:
    """Execute the confirmed manage_outing action atomically inside a transaction (called from confirmation gate)."""
    action = payload.get("action")
    if action == "cancel":
        outing_id = payload["outing_id"]
        outing_ref = db.collection("driver_schedule").document(outing_id)
        transaction = db.transaction()
        res = _txn_cancel_outing(transaction, outing_ref, datetime.now(RIYADH_TZ))
        if res.get("ok"):
            return {"ok": True, "outing_id": outing_id, "status": "cancelled"}
        return res

    elif action == "create":
        oid, doc_payload = _build_outing_payload(
            action="create",
            assigned_driver=payload["assigned_driver"],
            start_time=payload["start_time"],
            end_time=payload["end_time"],
            destination=payload["destination"],
            purpose=payload["purpose"],
            requested_by=payload["requested_by"],
            passengers=payload.get("passengers"),
            notes=payload.get("notes"),
            outing_id=payload.get("outing_id"),
        )
        outing_ref = db.collection("driver_schedule").document(oid)
        transaction = db.transaction()
        res = _txn_create_outing(transaction, outing_ref, doc_payload)
        if res.get("ok"):
            return {"ok": True, "outing_id": oid, "status": "scheduled"}
        return res

    return {"ok": False, "error": "unknown_action"}


@firestore.transactional
def _txn_update_driver_availability(
    transaction: firestore.Transaction,
    avail_ref: firestore.DocumentReference,
    payload: dict[str, Any],
) -> dict[str, Any]:
    avail_ref.get(transaction=transaction)
    transaction.set(avail_ref, payload, merge=True)
    return {"ok": True}


def update_driver_availability(
    db: firestore.Client,
    driver_id: str,
    date_str: str,
    slots: list[dict[str, Any]],
    notes: str | None,
    caller_member_id: str,
) -> dict[str, Any]:
    """Tier 2/Drivers: Updates availability slots atomically inside a transaction (SCHEMA §6)."""
    avail_id = f"avail_{driver_id}_{date_str.replace('-', '')}"
    avail_ref = db.collection("driver_availability").document(avail_id)

    payload = {
        "availability_id": avail_id,
        "driver_id": driver_id,
        "date": date_str,
        "slots": slots,
        "notes": notes or "",
        "updated_by": caller_member_id,
        "updated_at": datetime.now(RIYADH_TZ),
    }

    transaction = db.transaction()
    _txn_update_driver_availability(transaction, avail_ref, payload)
    return {"ok": True, "availability_id": avail_id}


def get_calendar_events(db: firestore.Client, date_range: str) -> dict[str, Any]:
    """Fetch and aggregate public iCloud Calendar events for Tier 1 principals."""
    try:
        start_date, end_date = parse_date_range(date_range)
    except Exception as e:
        return {"ok": False, "error": f"invalid_date_range_format: {e}"}

    aggregated_events = fetch_tier1_calendar_events(db, start_date, end_date)

    return {
        "ok": True,
        "date_range": f"{start_date.isoformat()} to {end_date.isoformat()}",
        "events": aggregated_events,
    }


def execute_fleet_tool_call(
    db: firestore.Client,
    tool_name: str,
    args: dict[str, Any],
    caller_member_id: str,
    caller_tier: str,
    phone_e164: str,
) -> dict[str, Any]:
    """Dispatch Module 1 (fleet) tools; RBAC and visibility boundaries enforced here."""
    logger.info(
        "execute_fleet_tool_call tool=%s caller=%s tier=%s",
        tool_name,
        caller_member_id,
        caller_tier,
    )

    if tool_name in (
        "manage_outing",
        "get_calendar_events",
        "register_calendar_url",
        "get_pooling_suggestions",
    ):
        if caller_tier != "tier1":
            return {"ok": False, "error": "permission_denied"}

    if tool_name == "get_schedule":
        # Force today's date only for Tier 2 callers (SCHEMA §2 visibility restriction)
        date_range_arg = args.get(
            "date_range", datetime.now(RIYADH_TZ).date().isoformat()
        )
        if caller_tier == "tier2":
            date_range_arg = datetime.now(RIYADH_TZ).date().isoformat()
        return get_schedule(db, date_range_arg)

    if tool_name == "manage_outing":
        return manage_outing(
            db=db,
            action=args.get("action", ""),
            phone_e164=phone_e164,
            caller_member_id=caller_member_id,
            outing_id=args.get("outing_id"),
            assigned_driver=args.get("assigned_driver"),
            start_time=args.get("start_time"),
            end_time=args.get("end_time"),
            destination=args.get("destination"),
            purpose=args.get("purpose"),
            passengers=args.get("passengers"),
            notes=args.get("notes"),
        )

    if tool_name == "update_driver_availability":
        driver_id = args.get("driver_id", "")
        dr_snap = db.collection("drivers").document(driver_id).get()
        if not dr_snap.exists:
            return {"ok": False, "error": "driver_not_found"}
        dr_data = dr_snap.to_dict() or {}

        if caller_tier == "tier2" and dr_data.get("member_id") != caller_member_id:
            logger.warning(
                "update_driver_availability_denied caller=%s driver=%s",
                caller_member_id,
                driver_id,
            )
            return {"ok": False, "error": "permission_denied"}

        return update_driver_availability(
            db=db,
            driver_id=driver_id,
            date_str=args.get("date", datetime.now(RIYADH_TZ).date().isoformat()),
            slots=args.get("slots", []),
            notes=args.get("notes"),
            caller_member_id=caller_member_id,
        )

    if tool_name == "get_calendar_events":
        return get_calendar_events(
            db=db,
            date_range=args.get(
                "date_range", datetime.now(RIYADH_TZ).date().isoformat()
            ),
        )

    if tool_name == "register_calendar_url":
        return register_calendar_url(
            db=db,
            member_id=args.get("member_id", ""),
            url=args.get("url", ""),
        )

    if tool_name == "get_pooling_suggestions":
        return get_pooling_suggestions(
            db=db,
            date_str=args.get("date", datetime.now(RIYADH_TZ).date().isoformat()),
        )

    return {"ok": False, "error": "unknown_fleet_tool"}


def register_calendar_url(
    db: firestore.Client, member_id: str, url: str
) -> dict[str, Any]:
    """Store/update a member's iCloud calendar URL."""
    transaction = db.transaction()
    member_ref = db.collection("members").document(member_id)

    @firestore.transactional
    def _register_tx(tx):
        snap = member_ref.get(transaction=tx)
        if not snap.exists:
            return {"ok": False, "error": "member_not_found"}
        data = snap.to_dict() or {}
        if data.get("role") != "tier1":
            return {"ok": False, "error": "only_tier1_principals_can_have_calendars"}

        tx.update(
            member_ref,
            {
                "icloud_calendar_url": url,
                "updated_at": datetime.now(RIYADH_TZ),
            },
        )
        return {"ok": True}

    res = _register_tx(transaction)
    if not res.get("ok"):
        return res

    logger.info("calendar_url_registered member_id=%s url=%s", member_id, url)
    return {"ok": True, "member_id": member_id, "icloud_calendar_url": url}


def get_pooling_suggestions(db: firestore.Client, date_str: str) -> dict[str, Any]:
    """Fetch ride pooling suggestions for a specific date (YYYY-MM-DD)."""
    try:
        target_date = date.fromisoformat(date_str)
    except Exception as e:
        return {"ok": False, "error": f"invalid_date_format: {e}"}

    events = fetch_tier1_calendar_events(db, target_date, target_date)

    from app.workflow import find_pooling_suggestions

    suggestions = find_pooling_suggestions(db, events)

    return {
        "ok": True,
        "date": date_str,
        "suggestions": suggestions,
    }
