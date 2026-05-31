"""MODULE 2 tools — Property & Duties (Phase 1 only, SCHEMA §6)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

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
]


def _validate_structural_enum(value: str, allowed: frozenset[str], field: str) -> str | None:
    if value not in allowed:
        return f"Invalid {field}: must be one of {sorted(allowed)} (English canonical values only)."
    return None


@firestore.transactional
def _txn_update_task_status(
    transaction: firestore.Transaction,
    task_ref: firestore.DocumentReference,
    status: str,
    feedback: str | None,
) -> dict[str, Any]:
    snap = task_ref.get(transaction=transaction)
    if not snap.exists:
        return {"ok": False, "error": "task_not_found"}
    data = snap.to_dict() or {}
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


def list_tasks(db: firestore.Client, member_id: str, date: str, caller_tier: str, caller_id: str) -> dict[str, Any]:
    if caller_tier == "tier2" and member_id != caller_id:
        logger.warning("list_tasks_denied tier2_cross_member caller=%s target=%s", caller_id, member_id)
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
    feedback: str | None,
    caller_tier: str,
    caller_id: str,
) -> dict[str, Any]:
    err = _validate_structural_enum(status, ALLOWED_STATUS, "status")
    if err:
        return {"ok": False, "error": err}

    task_ref = db.collection("staff_tasks").document(task_id)
    snap = task_ref.get()
    if not snap.exists:
        return {"ok": False, "error": "task_not_found"}
    data = snap.to_dict() or {}
    assigned_to = data.get("assigned_to")
    if caller_tier == "tier2" and assigned_to != caller_id:
        logger.warning("update_task_status_denied task_id=%s caller=%s", task_id, caller_id)
        return {"ok": False, "error": "permission_denied"}

    transaction = db.transaction()
    result = _txn_update_task_status(transaction, task_ref, status, feedback)
    logger.info("update_task_status task_id=%s result=%s", task_id, result)
    return result


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
    task_id = f"task_{due_date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
    payload = {
        "task_id": task_id,
        "template_id": None,
        "assigned_to": assigned_to,
        "task_description": task_description,
        "due_date": due_date,
        "frequency": "adhoc",
        "status": "pending",
        "feedback": None,
        "created_at": datetime.now(RIYADH_TZ),
    }
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


def execute_pending_create_adhoc(db: firestore.Client, payload: dict[str, Any]) -> dict[str, Any]:
    task_id = payload.get("task_id") or f"task_{uuid.uuid4().hex[:12]}"
    task_ref = db.collection("staff_tasks").document(task_id)
    doc_payload = {
        "task_id": task_id,
        "template_id": None,
        "assigned_to": payload["assigned_to"],
        "task_description": payload["task_description"],
        "due_date": payload["due_date"],
        "frequency": "adhoc",
        "status": "pending",
        "feedback": None,
        "created_at": datetime.now(RIYADH_TZ),
    }
    transaction = db.transaction()
    return _txn_create_adhoc_task(transaction, task_ref, doc_payload)


def execute_tool_call(
    db: firestore.Client,
    tool_name: str,
    args: dict[str, Any],
    caller_member_id: str,
    caller_tier: str,
    phone_e164: str,
) -> dict[str, Any]:
    """Dispatch Module 2 tool; RBAC enforced here."""
    logger.info(
        "execute_tool_call tool=%s caller=%s tier=%s",
        tool_name,
        caller_member_id,
        caller_tier,
    )

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
        if caller_tier != "tier1":
            return {"ok": False, "error": "permission_denied"}
        return create_adhoc_task(
            db,
            args.get("assigned_to", ""),
            args.get("task_description", ""),
            args.get("due_date", ""),
            phone_e164,
        )
    return {"ok": False, "error": f"unknown_tool:{tool_name}"}
