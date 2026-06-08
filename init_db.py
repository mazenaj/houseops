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
    """Upsert Tier 1 principals, Tier 2 staff (drivers, nannies, chef, maid), and children."""
    now = _now()
    today = now.date().isoformat()
    from datetime import timedelta

    tomorrow = (now + timedelta(days=1)).date().isoformat()

    members = [
        {
            "member_id": "mem_principal_001",
            "phone_e164": PRINCIPAL_PHONE,
            "name": "Mazen",
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
            "member_id": "mem_child_001",
            "phone_e164": None,
            "name": "Abdulrahman",
            "nickname": "Mano",
            "role": "child",
            "capabilities": [],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_child_002",
            "phone_e164": None,
            "name": "Adel",
            "nickname": "Bingo",
            "role": "child",
            "capabilities": [],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_driver_001",
            "phone_e164": "+966569300454",
            "name": "Khidir",
            "role": "tier2",
            "capabilities": ["driver"],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_driver_002",
            "phone_e164": "+966558456441",
            "name": "Emad",
            "role": "tier2",
            "capabilities": ["driver"],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_driver_003",
            "phone_e164": "+966539818027",
            "name": "Kim",
            "role": "tier2",
            "capabilities": ["driver"],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_staff_nanny_001",
            "phone_e164": "+966502644515",
            "name": "Lee",
            "role": "tier2",
            "capabilities": ["housemaid"],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_staff_nanny_002",
            "phone_e164": "+966500000008",
            "name": "Elmie",
            "role": "tier2",
            "capabilities": ["housemaid"],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_staff_chef_001",
            "phone_e164": "+966504124874",
            "name": "Therese",
            "role": "tier2",
            "capabilities": ["chef"],
            "active": True,
            "preferred_language": "en",
            "created_at": now,
            "updated_at": now,
        },
        {
            "member_id": "mem_staff_maid_001",
            "phone_e164": "+966542823357",
            "name": "Rhea",
            "role": "tier2",
            "capabilities": ["housemaid"],
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

    # Delete duplicate mem_child_003 if it exists
    db.collection("members").document("mem_child_003").delete()
    logger.info("member_deleted member_id=mem_child_003 (duplicate of Adel/Bingo)")

    # Seed the drivers collection
    drivers = [
        {
            "driver_id": "dr_khidir",
            "member_id": "mem_driver_001",
            "name": "Khidir",
            "roles": ["driver"],
            "default_vehicle": "Mercedes V Class",
            "active": True,
        },
        {
            "driver_id": "dr_emad",
            "member_id": "mem_driver_002",
            "name": "Emad",
            "roles": ["driver"],
            "default_vehicle": "Lexus LX",
            "active": True,
        },
        {
            "driver_id": "dr_kim",
            "member_id": "mem_driver_003",
            "name": "Kim",
            "roles": ["driver"],
            "default_vehicle": "Toyota Rush",
            "active": True,
        },
    ]
    for dr in drivers:
        db.collection("drivers").document(dr["driver_id"]).set(dr, merge=True)
        logger.info("driver_seeded driver_id=%s name=%s", dr["driver_id"], dr["name"])

    # Seed driver availability for today and tomorrow for all drivers
    availabilities = []
    for dr in drivers:
        dr_id = dr["driver_id"]
        availabilities.extend(
            [
                {
                    "availability_id": f"avail_{dr_id}_{today.replace('-', '')}",
                    "driver_id": dr_id,
                    "date": today,
                    "slots": [
                        {
                            "start_time": "07:00",
                            "end_time": "22:00",
                            "status": "available",
                        }
                    ],
                    "notes": "Regular shift",
                    "updated_by": "mem_principal_001",
                    "updated_at": now,
                },
                {
                    "availability_id": f"avail_{dr_id}_{tomorrow.replace('-', '')}",
                    "driver_id": dr_id,
                    "date": tomorrow,
                    "slots": [
                        {
                            "start_time": "07:00",
                            "end_time": "22:00",
                            "status": "available",
                        }
                    ],
                    "notes": "Regular shift",
                    "updated_by": "mem_principal_001",
                    "updated_at": now,
                },
            ]
        )

    for av in availabilities:
        db.collection("driver_availability").document(av["availability_id"]).set(
            av, merge=True
        )
        logger.info(
            "driver_availability_seeded driver_id=%s date=%s",
            av["driver_id"],
            av["date"],
        )

    dispatch_rules = {
        "rules": [
            {"principal_name": "Mazen", "primary_driver_id": "dr_emad"},
            {"principal_name": "Jawaher", "primary_driver_id": "dr_khidir"},
            {"principal_name": "Abdulrahman", "primary_driver_id": "dr_khidir"},
            {"principal_name": "Mano", "primary_driver_id": "dr_khidir"},
            {"principal_name": "Adel", "primary_driver_id": "dr_khidir"},
            {"principal_name": "Bingo", "primary_driver_id": "dr_khidir"},
            {"principal_name": "Errands", "primary_driver_id": "dr_kim"},
        ]
    }
    db.collection("config").document("dispatch_rules").set(dispatch_rules, merge=True)
    logger.info("dispatch_rules_seeded")

    # Optional sample tasks for Lee (Nanny)
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
        logger.info(
            "staff_task_seeded task_id=%s assigned_to=%s", task["task_id"], staff_id
        )

    return ids


def seed_pets(db: firestore.Client) -> None:
    """Seed pets (dogs Wiggie and Nejma)."""
    pets = [
        {
            "pet_id": "pet_wiggie",
            "name": "Wiggie",
            "species": "dog",
            "active": True,
        },
        {
            "pet_id": "pet_nejma",
            "name": "Nejma",
            "species": "dog",
            "active": True,
        },
    ]
    for pet in pets:
        db.collection("pets").document(pet["pet_id"]).set(pet, merge=True)
        logger.info("pet_seeded pet_id=%s name=%s", pet["pet_id"], pet["name"])


def main() -> int:
    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        logger.error(
            "Set GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT before running init_db.py"
        )
        return 1

    logger.info(
        "init_db_start project=%s principal_phone=%s nanny_phone=+966502644515",
        project,
        PRINCIPAL_PHONE,
    )
    db = firestore.Client(project=project)
    seed_members(db)
    seed_pets(db)
    logger.info(
        "init_db_complete — set PRINCIPAL_PHONE to your WhatsApp number before running; "
        "Lee (Nanny) is registered at +966502644515"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
