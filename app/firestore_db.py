"""Firestore access helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Union

from google.cloud import firestore

from app.config import CONFIRMATION_TTL_MINUTES, RIYADH_TZ
from app.models import InboundMessage, Member, PausedConfirmation, PendingConfirmation

logger = logging.getLogger(__name__)

_db: Union[firestore.Client, None] = None


def get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
        logger.info("firestore_client_initialized")
    return _db


def lookup_member_by_phone(db: firestore.Client, phone_e164: str) -> Union[Member, None]:
    """Find active member by phone_e164."""
    query = (
        db.collection("members")
        .where("phone_e164", "==", phone_e164)
        .where("active", "==", True)
        .limit(1)
    )
    docs = list(query.stream())
    if not docs:
        logger.info("member_not_found phone=%s", phone_e164)
        return None
    data = docs[0].to_dict() or {}
    data["member_id"] = data.get("member_id", docs[0].id)
    member = Member(**data)
    logger.info("member_found phone=%s member_id=%s role=%s", phone_e164, member.member_id, member.role)
    return member


def lookup_member_by_telegram_chat_id(db: firestore.Client, chat_id: int) -> Union[Member, None]:
    """Find active member by telegram_chat_id."""
    query = (
        db.collection("members")
        .where("telegram_chat_id", "==", chat_id)
        .where("active", "==", True)
        .limit(1)
    )
    docs = list(query.stream())
    if not docs:
        logger.info("member_not_found telegram_chat_id=%d", chat_id)
        return None
    data = docs[0].to_dict() or {}
    data["member_id"] = data.get("member_id", docs[0].id)
    member = Member(**data)
    logger.info("member_found telegram_chat_id=%d member_id=%s role=%s", chat_id, member.member_id, member.role)
    return member


def link_telegram_chat_id(db: firestore.Client, phone_e164: str, chat_id: int) -> bool:
    """Link telegram_chat_id to a member looked up by phone."""
    query = (
        db.collection("members")
        .where("phone_e164", "==", phone_e164)
        .where("active", "==", True)
        .limit(1)
    )
    docs = list(query.stream())
    if not docs:
        logger.warning("link_telegram_failed member_not_found phone=%s", phone_e164)
        return False
    ref = docs[0].reference
    ref.update({"telegram_chat_id": chat_id, "updated_at": datetime.now(RIYADH_TZ)})
    logger.info("telegram_chat_id_linked phone=%s telegram_chat_id=%d", phone_e164, chat_id)
    return True


def get_conversation_ref(db: firestore.Client, phone_e164: str) -> firestore.DocumentReference:
    return db.collection("conversations").document(phone_e164)


def load_conversation_state(
    db: firestore.Client, phone_e164: str
) -> dict[str, Any]:
    ref = get_conversation_ref(db, phone_e164)
    snap = ref.get()
    if not snap.exists:
        return {}
    return snap.to_dict() or {}


def ensure_conversation_doc(
    db: firestore.Client,
    phone_e164: str,
    member_id: str,
) -> None:
    ref = get_conversation_ref(db, phone_e164)
    snap = ref.get()
    now = datetime.now(RIYADH_TZ)
    if not snap.exists:
        ref.set(
            {
                "phone_e164": phone_e164,
                "member_id": member_id,
                "active_module": "property_management",
                "pending_confirmation": None,
                "paused_confirmations": [],
                "updated_at": now,
            }
        )
        logger.info("conversation_created phone=%s", phone_e164)
    else:
        ref.update({"member_id": member_id, "updated_at": now})


def parse_pending_confirmation(data: Union[dict[str, Any], None]) -> Union[PendingConfirmation, None]:
    if not data:
        return None
    try:
        return PendingConfirmation(**data)
    except Exception as exc:
        logger.warning("pending_confirmation_parse_failed error=%s", exc)
        return None


def clear_pending_confirmation(db: firestore.Client, phone_e164: str) -> None:
    get_conversation_ref(db, phone_e164).update(
        {"pending_confirmation": None, "updated_at": datetime.now(RIYADH_TZ)}
    )
    logger.info("pending_confirmation_cleared phone=%s", phone_e164)


def set_pending_confirmation(
    db: firestore.Client,
    phone_e164: str,
    action: str,
    payload: dict[str, Any],
    summary: str,
) -> PendingConfirmation:
    now = datetime.now(RIYADH_TZ)
    pending = PendingConfirmation(
        confirmation_id=str(uuid.uuid4()),
        action=action,
        payload=payload,
        summary=summary,
        status="active",
        created_at=now,
        expires_at=now + timedelta(minutes=CONFIRMATION_TTL_MINUTES),
    )
    get_conversation_ref(db, phone_e164).update(
        {
            "pending_confirmation": pending.model_dump(mode="json"),
            "updated_at": now,
        }
    )
    logger.info(
        "pending_confirmation_set phone=%s action=%s confirmation_id=%s",
        phone_e164,
        action,
        pending.confirmation_id,
    )
    return pending


def pause_pending_confirmation(
    db: firestore.Client,
    phone_e164: str,
    pending: PendingConfirmation,
    pause_reason: str = "user_pivot",
) -> None:
    ref = get_conversation_ref(db, phone_e164)
    snap = ref.get()
    state = snap.to_dict() or {} if snap.exists else {}
    stack = list(state.get("paused_confirmations") or [])
    stack.append(
        PausedConfirmation(
            confirmation_id=pending.confirmation_id,
            action=pending.action,
            payload=pending.payload,
            summary=pending.summary,
            paused_at=datetime.now(RIYADH_TZ),
            pause_reason=pause_reason,
        ).model_dump(mode="json")
    )
    ref.update(
        {
            "pending_confirmation": None,
            "paused_confirmations": stack,
            "updated_at": datetime.now(RIYADH_TZ),
        }
    )
    logger.info(
        "pending_confirmation_paused phone=%s confirmation_id=%s reason=%s",
        phone_e164,
        pending.confirmation_id,
        pause_reason,
    )


def write_message_turn(
    db: firestore.Client,
    phone_e164: str,
    message_id: str,
    role: str,
    content_blocks: list[dict[str, Any]],
    source_language: Union[str, None] = None,
    telegram_chat_id: Union[int, None] = None,
    telegram_message_id: Union[int, None] = None,
) -> None:
    """Atomic create() on messages subcollection (SCHEMA §3)."""
    ref = (
        db.collection("conversations")
        .document(phone_e164)
        .collection("messages")
        .document(message_id)
    )
    payload: dict[str, Any] = {
        "message_id": message_id,
        "role": role,
        "content_blocks": content_blocks,
        "timestamp": datetime.now(RIYADH_TZ),
    }
    if source_language and role == "user":
        payload["source_language"] = source_language
    if telegram_chat_id is not None:
        payload["telegram_chat_id"] = telegram_chat_id
    if telegram_message_id is not None:
        payload["telegram_message_id"] = telegram_message_id
    try:
        ref.create(payload)
        logger.info(
            "message_turn_created phone=%s message_id=%s role=%s",
            phone_e164,
            message_id,
            role,
        )
    except Exception as exc:
        if "AlreadyExists" in type(exc).__name__ or "already exists" in str(exc).lower():
            logger.warning(
                "message_turn_duplicate phone=%s message_id=%s",
                phone_e164,
                message_id,
            )
        else:
            raise


def inbound_to_content_blocks(inbound: InboundMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in inbound.content:
        if block.block_type == "text":
            blocks.append({"block_type": "text", "text": block.text})
        else:
            blocks.append(
                {
                    "block_type": "media",
                    "media_id": block.media_id,
                    "mime_type": block.mime_type,
                    "gcs_uri": block.gcs_uri,
                    "normalized_mime_type": block.normalized_mime_type,
                }
            )
    return blocks
