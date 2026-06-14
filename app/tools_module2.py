"""MODULE 2 tools — Property & Duties (Phase 1 only, SCHEMA §6)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Union
import httpx

from google.cloud import firestore

from app.config import RIYADH_TZ
from app.firestore_db import set_pending_confirmation

logger = logging.getLogger(__name__)

ALLOWED_STATUS = frozenset({"pending", "completed", "skipped"})

MODULE2_TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "list_tasks",
        "description": "List staff tasks for a member on an ISO date (YYYY-MM-DD).",
        "parameters": {
            "type": "object",
            "properties": {
                "member_id": {"type": "string", "description": "Target member_id"},
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["member_id", "date"],
        },
    },
    {
        "name": "update_task_status",
        "description": "Update staff task status atomically. Use task_id from list_tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "completed", "skipped"],
                },
                "feedback": {"type": "string"},
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "create_adhoc_task",
        "description": "Create an adhoc staff task (Tier 1). Requires user confirmation before persist.",
        "parameters": {
            "type": "object",
            "properties": {
                "assigned_to": {"type": "string"},
                "task_description": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["assigned_to", "task_description", "due_date"],
        },
    },
    {
        "name": "get_current_weather",
        "description": "Retrieve current weather conditions (temperature, humidity, wind speed) for a specified location (defaults to Riyadh, Saudi Arabia).",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The city/location name (default: Riyadh)",
                }
            },
        },
    },
    {
        "name": "create_weather_tasks",
        "description": "Create a batch of weather-dependent tasks (Tier 1). Requires user confirmation before persist.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "assigned_to": {
                                "type": "string",
                                "description": "Target member_id",
                            },
                            "task_description": {
                                "type": "string",
                                "description": "Description of the weather task",
                            },
                            "due_date": {
                                "type": "string",
                                "description": "ISO date YYYY-MM-DD",
                            },
                        },
                        "required": ["assigned_to", "task_description", "due_date"],
                    },
                }
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "submit_suggestion",
        "description": "Log a user suggestion/improvement for the system (available to all tiers).",
        "parameters": {
            "type": "object",
            "properties": {
                "suggestion": {
                    "type": "string",
                    "description": "The full detailed suggestion text",
                },
                "summary": {
                    "type": "string",
                    "description": "A very short summary of the suggestion (under 5 words)",
                },
            },
            "required": ["suggestion", "summary"],
        },
    },
    {
        "name": "review_suggestion",
        "description": "Review and update the status of a user suggestion (Tier 1 only).",
        "parameters": {
            "type": "object",
            "properties": {
                "suggestion_id": {
                    "type": "string",
                    "description": "The ID of the suggestion to review",
                },
                "status": {
                    "type": "string",
                    "enum": ["accepted", "rejected"],
                    "description": "The new status: accepted or rejected",
                },
            },
            "required": ["suggestion_id", "status"],
        },
    },
]


def _validate_structural_enum(
    value: str, allowed: frozenset[str], field: str
) -> Union[str, None]:
    if value not in allowed:
        return f"Invalid {field}: must be one of {sorted(allowed)} (English canonical values only)."
    return None


@firestore.transactional
def _txn_update_task_status(
    transaction: firestore.Transaction,
    task_ref: firestore.DocumentReference,
    status: str,
    feedback: Union[str, None],
    caller_tier: str,
    caller_id: str,
) -> dict[str, Any]:
    snap = task_ref.get(transaction=transaction)
    if not snap.exists:
        return {"ok": False, "error": "task_not_found"}
    data = snap.to_dict() or {}
    assigned_to = data.get("assigned_to")
    if caller_tier == "tier2" and assigned_to != caller_id:
        logger.warning(
            "update_task_status_denied task_id=%s caller=%s", task_ref.id, caller_id
        )
        return {"ok": False, "error": "permission_denied"}

    # Tier 2 users can only mark tasks complete or notify of a problem (skipped with feedback)
    if caller_tier == "tier2":
        if status == "pending":
            return {"ok": False, "error": "permission_denied"}
        if status == "skipped" and (not feedback or not feedback.strip()):
            return {"ok": False, "error": "feedback_required_to_report_problem"}

    current = data.get("status")
    if current not in ("pending", "completed", "skipped"):
        return {"ok": False, "error": "invalid_current_status"}
    updates: dict[str, Any] = {
        "status": status,
        "updated_at": datetime.now(RIYADH_TZ),
    }
    if feedback is not None:
        updates["feedback"] = feedback
    if status == "completed":
        updates["completed_at"] = datetime.now(RIYADH_TZ)
    transaction.update(task_ref, updates)
    return {"ok": True, "task_id": task_ref.id, "status": status}


@firestore.transactional
def _txn_create_adhoc_task(
    transaction: firestore.Transaction,
    task_ref: firestore.DocumentReference,
    payload: dict[str, Any],
) -> dict[str, Any]:
    snap = task_ref.get(transaction=transaction)
    if snap.exists:
        return {"ok": False, "error": "task_id_collision"}
    transaction.set(task_ref, payload)
    return {"ok": True, "task_id": task_ref.id}


@firestore.transactional
def _txn_create_weather_tasks(
    transaction: firestore.Transaction,
    db: firestore.Client,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    created_ids = []
    payloads = []
    for task in tasks:
        task_id, doc_payload = _build_task_document_payload(
            task["assigned_to"],
            task["task_description"],
            task["due_date"],
        )
        task_ref = db.collection("staff_tasks").document(task_id)
        snap = task_ref.get(transaction=transaction)
        if snap.exists:
            return {"ok": False, "error": "task_id_collision"}
        payloads.append((task_ref, doc_payload, task_id))
    for task_ref, doc_payload, task_id in payloads:
        transaction.set(task_ref, doc_payload)
        created_ids.append(task_id)
    return {"ok": True, "task_ids": created_ids}


def list_tasks(
    db: firestore.Client, member_id: str, date: str, caller_tier: str, caller_id: str
) -> dict[str, Any]:
    if caller_tier == "tier2" and member_id != caller_id:
        logger.warning(
            "list_tasks_denied tier2_cross_member caller=%s target=%s",
            caller_id,
            member_id,
        )
        return {"ok": False, "error": "permission_denied", "tasks": []}

    query = (
        db.collection("staff_tasks")
        .where("assigned_to", "==", member_id)
        .where("due_date", "==", date)
    )
    tasks = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        data["task_id"] = data.get("task_id", doc.id)
        tasks.append(data)
    logger.info("list_tasks member_id=%s date=%s count=%s", member_id, date, len(tasks))
    return {"ok": True, "tasks": tasks}


def update_task_status(
    db: firestore.Client,
    task_id: str,
    status: str,
    feedback: Union[str, None],
    caller_tier: str,
    caller_id: str,
) -> dict[str, Any]:
    err = _validate_structural_enum(status, ALLOWED_STATUS, "status")
    if err:
        return {"ok": False, "error": err}

    task_ref = db.collection("staff_tasks").document(task_id)
    transaction = db.transaction()
    result = _txn_update_task_status(
        transaction, task_ref, status, feedback, caller_tier, caller_id
    )
    logger.info("update_task_status task_id=%s result=%s", task_id, result)
    return result


def _build_task_document_payload(
    assigned_to: str,
    task_description: str,
    due_date: str,
    task_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Helper to build consistent Firestore document payloads for adhoc/weather tasks."""
    tid = task_id or f"task_{due_date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
    payload = {
        "task_id": tid,
        "template_id": None,
        "assigned_to": assigned_to,
        "task_description": task_description,
        "due_date": due_date,
        "frequency": "adhoc",
        "status": "pending",
        "feedback": None,
        "created_at": datetime.now(RIYADH_TZ),
    }
    return tid, payload


def create_adhoc_task(
    db: firestore.Client,
    assigned_to: str,
    task_description: str,
    due_date: str,
    phone_e164: str,
    *,
    skip_confirmation: bool = False,
) -> dict[str, Any]:
    """Tier 1 only. Sets pending_confirmation unless skip_confirmation (gate confirm path)."""
    task_id, payload = _build_task_document_payload(
        assigned_to, task_description, due_date
    )
    summary = (
        f"Create adhoc task for {assigned_to}: {task_description} (due {due_date}). "
        "Reply YES to confirm or NO to cancel."
    )

    if skip_confirmation:
        task_ref = db.collection("staff_tasks").document(task_id)
        transaction = db.transaction()
        result = _txn_create_adhoc_task(transaction, task_ref, payload)
        logger.info("create_adhoc_task_committed task_id=%s", task_id)
        return {**result, "task": payload}

    set_pending_confirmation(
        db,
        phone_e164,
        action="create_adhoc_task",
        payload={
            "assigned_to": assigned_to,
            "task_description": task_description,
            "due_date": due_date,
            "task_id": task_id,
        },
        summary=summary,
    )
    return {
        "ok": True,
        "pending_confirmation": True,
        "summary": summary,
        "message": "Awaiting user confirmation before creating task.",
    }


def execute_pending_create_adhoc(
    db: firestore.Client, payload: dict[str, Any]
) -> dict[str, Any]:
    task_id, doc_payload = _build_task_document_payload(
        payload["assigned_to"],
        payload["task_description"],
        payload["due_date"],
        payload.get("task_id"),
    )
    task_ref = db.collection("staff_tasks").document(task_id)
    transaction = db.transaction()
    return _txn_create_adhoc_task(transaction, task_ref, doc_payload)


def execute_tool_call(
    db: firestore.Client,
    tool_name: str,
    args: dict[str, Any],
    caller_member_id: str,
    caller_tier: str,
    phone_e164: str,
    caller_name: str | None = None,
) -> dict[str, Any]:
    """Dispatch Module 2 tool; RBAC enforced here."""
    logger.info(
        "execute_tool_call tool=%s caller=%s tier=%s",
        tool_name,
        caller_member_id,
        caller_tier,
    )

    # 1. Enforce Tier 1 permissions upfront for restricted tools
    if tool_name in (
        "create_adhoc_task",
        "get_current_weather",
        "create_weather_tasks",
        "review_suggestion",
    ):
        if caller_tier != "tier1":
            return {"ok": False, "error": "permission_denied"}

    # 1b. Route Fleet & Calendar tools to tools_fleet module
    if tool_name in (
        "get_schedule",
        "manage_outing",
        "update_driver_availability",
        "get_calendar_events",
        "register_calendar_url",
        "get_pooling_suggestions",
    ):
        from app.tools_fleet import execute_fleet_tool_call

        return execute_fleet_tool_call(
            db=db,
            tool_name=tool_name,
            args=args,
            caller_member_id=caller_member_id,
            caller_tier=caller_tier,
            phone_e164=phone_e164,
        )

    # 2. Dispatch tool execution
    if tool_name == "list_tasks":
        return list_tasks(
            db,
            args.get("member_id", caller_member_id),
            args.get("date", datetime.now(RIYADH_TZ).date().isoformat()),
            caller_tier,
            caller_member_id,
        )
    if tool_name == "update_task_status":
        return update_task_status(
            db,
            args.get("task_id", ""),
            args.get("status", ""),
            args.get("feedback"),
            caller_tier,
            caller_member_id,
        )
    if tool_name == "create_adhoc_task":
        return create_adhoc_task(
            db,
            args.get("assigned_to", ""),
            args.get("task_description", ""),
            args.get("due_date", ""),
            phone_e164,
        )
    if tool_name == "get_current_weather":
        return get_current_weather(args.get("location", "Riyadh"))
    if tool_name == "create_weather_tasks":
        return create_weather_tasks(
            db,
            args.get("tasks", []),
            phone_e164,
        )
    if tool_name == "submit_suggestion":
        return submit_suggestion(
            db,
            args.get("suggestion", ""),
            args.get("summary", ""),
            caller_member_id,
            caller_name,
        )
    if tool_name == "review_suggestion":
        return review_suggestion(
            db,
            args.get("suggestion_id", ""),
            args.get("status", ""),
            caller_tier,
        )
    return {"ok": False, "error": f"unknown_tool:{tool_name}"}


def get_current_weather(location: str = "Riyadh") -> dict[str, Any]:
    """Fetch current weather details for a given location using Open-Meteo."""
    lat, lon = 24.7136, 46.6753

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    try:
        resp = httpx.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        curr = data.get("current", {})
        return {
            "ok": True,
            "location": location.capitalize(),
            "temperature": f"{curr.get('temperature_2m')}°C",
            "feels_like": f"{curr.get('apparent_temperature')}°C",
            "humidity": f"{curr.get('relative_humidity_2m')}%",
            "wind_speed": f"{curr.get('wind_speed_10m')} km/h",
            "precipitation": f"{curr.get('precipitation')} mm",
        }
    except Exception as e:
        logger.exception("get_current_weather_failed location=%s", location)
        return {"ok": False, "error": f"Failed to retrieve weather: {str(e)}"}


def create_weather_tasks(
    db: firestore.Client,
    tasks: list[dict[str, Any]],
    phone_e164: str,
) -> dict[str, Any]:
    """Tier 1 only. Sets pending_confirmation for a batch of weather tasks."""
    if not tasks:
        return {"ok": False, "error": "no_tasks_provided"}

    summary_lines = ["Create the following weather-dependent tasks:"]
    for idx, t in enumerate(tasks, 1):
        summary_lines.append(
            f"{idx}. {t['task_description']} for {t['assigned_to']} (due {t['due_date']})"
        )
    summary_lines.append("Reply YES to confirm or NO to cancel.")
    summary = "\n".join(summary_lines)

    set_pending_confirmation(
        db,
        phone_e164,
        action="create_weather_tasks",
        payload={"tasks": tasks},
        summary=summary,
    )
    return {
        "ok": True,
        "pending_confirmation": True,
        "summary": summary,
        "message": "Awaiting user confirmation before creating weather tasks.",
    }


def execute_pending_create_weather_tasks(
    db: firestore.Client, payload: dict[str, Any]
) -> dict[str, Any]:
    tasks = payload.get("tasks") or []
    if not tasks:
        return {"ok": False, "error": "no_tasks_provided"}
    transaction = db.transaction()
    return _txn_create_weather_tasks(transaction, db, tasks)


def submit_suggestion(
    db: firestore.Client,
    suggestion: str,
    summary: str,
    caller_member_id: str,
    caller_name: str | None = None,
) -> dict[str, Any]:
    """Logs a user suggestion to Firestore in the user_suggestions collection."""
    # Enforce word limit on summary via automatic truncation
    words = summary.strip().split()
    if len(words) >= 5:
        summary = " ".join(words[:4]) + "..."

    suggestion_id = f"sug_{uuid.uuid4().hex[:8]}"
    now = datetime.now(RIYADH_TZ)

    # Retrieve user name from members
    if caller_name:
        member_name = caller_name
    else:
        member_snap = db.collection("members").document(caller_member_id).get()
        member_name = caller_member_id
        if member_snap.exists:
            member_name = (member_snap.to_dict() or {}).get("name", caller_member_id)

    payload = {
        "suggestion_id": suggestion_id,
        "suggestion": suggestion,
        "summary": summary.strip(),
        "initiating_user": member_name,
        "member_id": caller_member_id,
        "date": now.date().isoformat(),
        "status": "not reviewed",
        "created_at": now,
    }
    db.collection("user_suggestions").document(suggestion_id).set(payload)
    logger.info("suggestion_submitted id=%s by=%s", suggestion_id, caller_member_id)
    return {"ok": True, "suggestion_id": suggestion_id, "status": "not reviewed"}


def review_suggestion(
    db: firestore.Client,
    suggestion_id: str,
    status: str,
    caller_tier: str,
) -> dict[str, Any]:
    """Updates the status of a user suggestion (Tier 1 only)."""
    if caller_tier != "tier1":
        return {"ok": False, "error": "permission_denied"}

    if status not in ("accepted", "rejected"):
        return {"ok": False, "error": f"invalid_status: {status}"}

    transaction = db.transaction()
    sug_ref = db.collection("user_suggestions").document(suggestion_id)

    @firestore.transactional
    def _review_tx(tx):
        snap = sug_ref.get(transaction=tx)
        if not snap.exists:
            return {"ok": False, "error": "suggestion_not_found"}

        tx.update(
            sug_ref,
            {
                "status": status,
                "updated_at": datetime.now(RIYADH_TZ),
            },
        )
        return {"ok": True}

    res = _review_tx(transaction)
    if not res.get("ok"):
        return res

    logger.info("suggestion_reviewed id=%s status=%s", suggestion_id, status)
    return {"ok": True, "suggestion_id": suggestion_id, "status": status}
