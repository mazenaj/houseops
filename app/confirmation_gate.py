"""Pre-agent confirmation gate (SCHEMA §9.3)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Union

from google.cloud import firestore

from app.config import RIYADH_TZ
from app.firestore_db import (
    clear_pending_confirmation,
    get_conversation_ref,
    load_conversation_state,
    parse_pending_confirmation,
    pause_pending_confirmation,
    lookup_member_by_phone,
)
from app.models import InboundMessage, Member, PendingConfirmation
from app.tools_module2 import (
    execute_pending_create_adhoc,
    execute_pending_create_weather_tasks,
)
from app.tools_fleet import execute_pending_manage_outing
from app.workflow import handle_driver_arrival_reply, recheck_calendar_conflicts


logger = logging.getLogger(__name__)

CONFIRM_PATTERNS = re.compile(
    r"^(yes|y|confirm|ok|okay|نعم|ايوه|أجل)\b",
    re.IGNORECASE,
)
REJECT_PATTERNS = re.compile(
    r"^(no|n|cancel|stop|لا|إلغاء)\b",
    re.IGNORECASE,
)
RESUME_PATTERNS = re.compile(
    r"^(resume|continue|استئناف)\b",
    re.IGNORECASE,
)
EMERGENCY_KEYWORDS = re.compile(
    r"(flat tire|accident|leak|urgent|fire|flooding|" r"إطار|حادث|تسرب|عاجل|حريق)",
    re.IGNORECASE,
)


def _extract_inbound_text(inbound: InboundMessage) -> str:
    parts = [b.text for b in inbound.content if b.block_type == "text"]
    return " ".join(parts).strip()


def _classify_intent(text: str) -> str:
    """CONFIRM | REJECT | UNRELATED | RESUME"""
    if not text:
        return "UNRELATED"
    if RESUME_PATTERNS.search(text):
        return "RESUME"
    if CONFIRM_PATTERNS.search(text):
        return "CONFIRM"
    if REJECT_PATTERNS.search(text):
        return "REJECT"
    return "UNRELATED"


def _expire_if_needed(
    db: firestore.Client,
    phone_e164: str,
    pending: PendingConfirmation,
) -> bool:
    """Return True if expired and cleared."""
    now = datetime.now(RIYADH_TZ)
    if pending.expires_at and now > pending.expires_at:
        get_conversation_ref(db, phone_e164).update(
            {
                "pending_confirmation": None,
                "updated_at": now,
            }
        )
        logger.info(
            "pending_confirmation_expired phone=%s confirmation_id=%s",
            phone_e164,
            pending.confirmation_id,
        )
        return True
    return False


def _pop_paused_confirmation(
    db: firestore.Client, phone_e164: str
) -> Union[PendingConfirmation, None]:
    ref = get_conversation_ref(db, phone_e164)
    snap = ref.get()
    if not snap.exists:
        return None
    state = snap.to_dict() or {}
    stack = list(state.get("paused_confirmations") or [])
    if not stack:
        return None
    item = stack.pop()
    now = datetime.now(RIYADH_TZ)
    pending = PendingConfirmation(
        confirmation_id=item["confirmation_id"],
        action=item["action"],
        payload=item["payload"],
        summary=item["summary"],
        status="active",
        created_at=datetime.fromisoformat(item["paused_at"])
        if isinstance(item.get("paused_at"), str)
        else item.get("paused_at", now),
        expires_at=now + timedelta(minutes=30),
    )
    ref.update(
        {
            "paused_confirmations": stack,
            "pending_confirmation": pending.model_dump(mode="json"),
            "updated_at": now,
        }
    )
    logger.info(
        "paused_confirmation_restored phone=%s action=%s", phone_e164, pending.action
    )
    return pending


def _execute_confirmed_action(
    db: firestore.Client,
    pending: PendingConfirmation,
) -> dict[str, Any]:
    if pending.action == "create_adhoc_task":
        return execute_pending_create_adhoc(db, pending.payload)
    if pending.action == "create_weather_tasks":
        return execute_pending_create_weather_tasks(db, pending.payload)
    if pending.action == "manage_outing":
        return execute_pending_manage_outing(db, pending.payload)
    logger.warning("unknown_confirmation_action action=%s", pending.action)
    return {"ok": False, "error": "unknown_action"}


class GateResult:
    def __init__(
        self,
        proceed_to_gemini: bool,
        reply_text: Union[str, None] = None,
        session_note: Union[str, None] = None,
        handled: bool = False,
        resumed_payload: Union[dict[str, Any], None] = None,
    ):
        self.proceed_to_gemini = proceed_to_gemini
        self.reply_text = reply_text
        self.session_note = session_note
        self.handled = handled
        self.resumed_payload = resumed_payload


def run_confirmation_gate(
    db: firestore.Client,
    phone_e164: str,
    inbound: InboundMessage,
    state: dict[str, Any] | None = None,
    member: Member | None = None,
) -> GateResult:
    """
    Pre-agent gate before Gemini (SCHEMA §9.3).
    Returns whether to proceed to Gemini and optional immediate reply.
    """
    if state is None:
        state = load_conversation_state(db, phone_e164)
    text = _extract_inbound_text(inbound)
    intent = _classify_intent(text)

    # 1. Resolve member role for interceptors
    if member is None:
        member = lookup_member_by_phone(db, phone_e164)
    if member:
        # A. Intercept driver arrival confirmations (Tier 2/Drivers)
        arrival_reply = handle_driver_arrival_reply(db, member.member_id, text)
        if arrival_reply:
            return GateResult(
                proceed_to_gemini=False, reply_text=arrival_reply, handled=True
            )

        # B. Intercept Tier 1 replies when next day's schedule has conflicts
        if member.role == "tier1":
            text_lower = text.strip().lower()
            words = text_lower.split()
            short_keywords = {
                "done",
                "fixed",
                "clear",
                "resolved",
                "yes",
                "y",
                "نعم",
                "تم",
            }
            long_substrings = (
                "calendar",
                "check",
                "recheck",
                "update",
                "revised",
                "confirm",
            )

            is_related = any(w in short_keywords for w in words) or any(
                sub in text_lower for sub in long_substrings
            )
            if is_related:
                recheck_reply = recheck_calendar_conflicts(db)
                if recheck_reply:
                    return GateResult(
                        proceed_to_gemini=False, reply_text=recheck_reply, handled=True
                    )

    # Resume command
    if intent == "RESUME":
        restored = _pop_paused_confirmation(db, phone_e164)
        if restored:
            return GateResult(
                proceed_to_gemini=False,
                reply_text=f"Resumed: {restored.summary}",
                handled=True,
            )
        return GateResult(
            proceed_to_gemini=False,
            reply_text="No paused request to resume.",
            handled=True,
        )

    pending = parse_pending_confirmation(state.get("pending_confirmation"))
    if not pending or pending.status != "active":
        return GateResult(proceed_to_gemini=True)

    if _expire_if_needed(db, phone_e164, pending):
        return GateResult(proceed_to_gemini=True)

    if pending.action == "resume_paused_agent_turn":
        if intent in ("CONFIRM", "RESUME"):
            clear_pending_confirmation(db, phone_e164)
            logger.info("resuming_paused_agent_turn phone=%s", phone_e164)
            return GateResult(
                proceed_to_gemini=True,
                resumed_payload=pending.payload,
                session_note="Resuming agent turn after high token usage authorization.",
            )
        elif intent == "REJECT":
            clear_pending_confirmation(db, phone_e164)
            logger.info("reject_resuming_paused_agent_turn phone=%s", phone_e164)
            return GateResult(
                proceed_to_gemini=False,
                reply_text="Cancelled. How can I help you?",
                handled=True,
            )

    if intent == "CONFIRM":
        result = _execute_confirmed_action(db, pending)
        clear_pending_confirmation(db, phone_e164)
        if result.get("ok"):
            reply = "Confirmed. Your request has been completed."
            if pending.action == "create_adhoc_task":
                reply = f"Task created (ID: {result.get('task_id')})."
            elif pending.action == "create_weather_tasks":
                reply = (
                    f"Weather tasks created ({len(result.get('task_ids', []))} tasks)."
                )
        else:
            reply = f"Could not complete the request: {result.get('error', 'unknown error')}"
        logger.info(
            "confirmation_gate_confirm phone=%s action=%s result=%s",
            phone_e164,
            pending.action,
            result,
        )
        return GateResult(proceed_to_gemini=False, reply_text=reply, handled=True)

    if intent == "REJECT":
        clear_pending_confirmation(db, phone_e164)
        logger.info(
            "confirmation_gate_reject phone=%s action=%s", phone_e164, pending.action
        )
        return GateResult(
            proceed_to_gemini=False,
            reply_text="Cancelled. How can I help you?",
            handled=True,
        )

    # UNRELATED — preempt (default pause)
    is_emergency = bool(EMERGENCY_KEYWORDS.search(text))
    if is_emergency:
        get_conversation_ref(db, phone_e164).update(
            {
                "pending_confirmation": None,
                "updated_at": datetime.now(RIYADH_TZ),
            }
        )
        logger.info(
            "confirmation_gate_emergency_discard phone=%s confirmation_id=%s",
            phone_e164,
            pending.confirmation_id,
        )
        note = (
            f"Previous confirmation discarded due to priority topic: {pending.summary}"
        )
        return GateResult(proceed_to_gemini=True, session_note=note)

    pause_pending_confirmation(db, phone_e164, pending, pause_reason="user_pivot")
    note = (
        f"Previous confirmation paused: {pending.summary}. "
        "Handle the new request first. User may reply 'resume' to continue."
    )
    logger.info(
        "confirmation_gate_preempt phone=%s confirmation_id=%s",
        phone_e164,
        pending.confirmation_id,
    )
    return GateResult(
        proceed_to_gemini=True,
        session_note=note,
        reply_text=None,
    )
