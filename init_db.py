#!/usr/bin/env python3
"""
Seed Phase 1 Firestore data: members (required for webhook auth) and sample staff_tasks.

Usage:
  export GCP_PROJECT_ID=your-project
  python init_db.py

Override phones via environment (E.164 with leading +):
  PRINCIPAL_PHONE=+966500000001 STAFF_PHONE=+966500000002 python init_db.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

from google.cloud import firestore

from app.config import RIYADH_TZ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("init_db")

PRINCIPAL_PHONE = os.environ.get("PRINCIPAL_PHONE", "+966500000001")
STAFF_PHONE = os.environ.get("STAFF_PHONE", "+966500000002")


def _now() -> datetime:
    return datetime.now(RIYADH_TZ)


def seed_members(db: firestore.Client) -> list[str]:
    """Upsert Tier 1 principal and Tier 2 housemaid for Phase 1 testing."""
    now = _now()
    today = now.date().isoformat()

    members = [
        {
            "member_id": "mem_principal_001",
            "phone_e164": PRINCIPAL_PHONE,
            "name": "Principal (Test)",
            "role": "tier1",
            "capabilities": [],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_staff_001",
            "phone_e164": STAFF_PHONE,
            "name": "Fatima (Test Staff)",
            "role": "tier2",
            "capabilities": ["housemaid"],
            "active": True,
            "preferred_language": "ar",
            "created_at": now,
            "updated_at": now,
        },
    ]

    ids: list[str] = []
    for doc in members:
        ref = db.collection("members").document(doc["member_id"])
        ref.set(doc, merge=True)
        ids.append(doc["member_id"])
        logger.info(
            "member_seeded member_id=%s phone=%s role=%s",
            doc["member_id"],
            doc["phone_e164"],
            doc["role"],
        )

    # Optional sample tasks for staff member (today)
    staff_id = "mem_staff_001"
    sample_tasks = [
        {
            "task_id": f"task_{today.replace('-', '')}_001",
            "template_id": None,
            "assigned_to": staff_id,
            "task_description": "Clean guest bathroom",
            "due_date": today,
            "frequency": "daily",
            "status": "pending",
            "feedback": None,
            "created_at": now,
        },
        {
            "task_id": f"task_{today.replace('-', '')}_002",
            "template_id": None,
            "assigned_to": staff_id,
            "task_description": "Laundry — whites",
            "due_date": today,
            "frequency": "daily",
            "status": "pending",
            "feedback": None,
            "created_at": now,
        },
    ]
    for task in sample_tasks:
        db.collection("staff_tasks").document(task["task_id"]).set(task, merge=True)
        logger.info("staff_task_seeded task_id=%s assigned_to=%s", task["task_id"], staff_id)

    return ids


def main() -> int:
    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        logger.error("Set GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT before running init_db.py")
        return 1

    logger.info("init_db_start project=%s principal_phone=%s staff_phone=%s", project, PRINCIPAL_PHONE, STAFF_PHONE)
    db = firestore.Client(project=project)
    seed_members(db)
    logger.info(
        "init_db_complete — register these numbers in Meta WhatsApp Business or update "
        "PRINCIPAL_PHONE / STAFF_PHONE to match your test devices"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
