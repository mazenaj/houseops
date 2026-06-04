"""Webhook idempotency with 24h TTL and stale-key overwrite (SCHEMA §1)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from app.config import IDEMPOTENCY_TTL_HOURS

logger = logging.getLogger(__name__)

COLLECTION = "webhook_idempotency"


def claim_idempotency_key(
    db: firestore.Client,
    message_id: str,
    now: datetime,
) -> bool:
    """
    Returns True if this webhook should be processed (claim succeeded).
    Returns False for genuine duplicates within the live window.
    """
    ref = db.collection(COLLECTION).document(message_id)
    payload = {
        "message_id": message_id,
        "received_at": now,
        "expires_at": now + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
    }
    try:
        ref.create(payload)
        logger.info(
            "idempotency_claimed message_id=%s expires_at=%s",
            message_id,
            payload["expires_at"].isoformat(),
        )
        return True
    except AlreadyExists:
        logger.info(
            "idempotency_conflict message_id=%s — running TTL stale fallback",
            message_id,
        )
        doc = ref.get()
        if not doc.exists:
            # Race: document deleted between create conflict and get — reclaim
            ref.set(payload)
            logger.warning(
                "idempotency_reclaim_after_missing_doc message_id=%s",
                message_id,
            )
            return True
        data = doc.to_dict() or {}
        expires_at = data.get("expires_at")
        if expires_at is None:
            logger.warning(
                "idempotency_missing_expires_at message_id=%s — treating as stale",
                message_id,
            )
            ref.set(payload)
            return True
        # Firestore returns datetime; normalize if needed
        if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=now.tzinfo)
        if now > expires_at:
            ref.set(payload)
            logger.info(
                "idempotency_stale_overwrite message_id=%s old_expires=%s",
                message_id,
                expires_at.isoformat()
                if hasattr(expires_at, "isoformat")
                else expires_at,
            )
            return True
        logger.info(
            "idempotency_duplicate_skipped message_id=%s expires_at=%s",
            message_id,
            expires_at.isoformat() if hasattr(expires_at, "isoformat") else expires_at,
        )
        return False


def release_idempotency_key(db: firestore.Client, message_id: str):
    """Delete a claimed idempotency key from the database (e.g. if task enqueuing fails)."""
    db.collection(COLLECTION).document(message_id).delete()
    logger.info("idempotency_released message_id=%s", message_id)
