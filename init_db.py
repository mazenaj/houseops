#!/usr/bin/env python3
"""
Seed Phase 1 Firestore data: members (required for webhook auth) and sample staff_tasks.

Usage:
  export GCP_PROJECT_ID=your-project
  python init_db.py

Override principal phone via environment (E.164 with leading +):
  PRINCIPAL_PHONE=+9665XXXXXXXX python init_db.py
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

PRINCIPAL_PHONE = os.environ.get("PRINCIPAL_PHONE", "+966506667785")


def _now() -> datetime:
    return datetime.now(RIYADH_TZ)


def seed_members(db: firestore.Client) -> list[str]:
    """Upsert Tier 1 principal and Tier 2 nanny for Phase 1 testing."""
    now = _now()
    today = now.date().isoformat()

    members = [
        {
            "member_id": "mem_principal_001",
            "phone_e164": PRINCIPAL_PHONE,
            "name": "Principal (Mazen)",
            "role": "tier1",
            "capabilities": [],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_principal_002",
            "phone_e164": "+966555012331",
            "name": "Jawaher",
            "role": "tier1",
            "capabilities": [],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_staff_nanny_001",
            "phone_e164": "+966502644515",
            "name": "Lee (Nanny)",
            "role": "tier2",
            "capabilities": ["housemaid"],  # Handled within Phase 1 property_management scope
            "active": True,
            "preferred_language": "en",
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
    staff_id = "mem_staff_nanny_001"
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

    logger.info("init_db_start project=%s principal_phone=%s nanny_phone=+966502644515", project, PRINCIPAL_PHONE)
    db = firestore.Client(project=project)
    seed_members(db)
    logger.info(
        "init_db_complete — set PRINCIPAL_PHONE to your WhatsApp number before running; "
        "Lee (Nanny) is registered at +966502644515"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
