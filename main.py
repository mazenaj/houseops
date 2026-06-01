"""
Household Operations Engine — Phase 1 entrypoint (SCHEMA.md).

Fast path: POST /webhook/whatsapp
Heavy path: POST /tasks/process-inbound
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.cloud_tasks import enqueue_inbound_processing
from app.config import RIYADH_TZ, WHATSAPP_VERIFY_TOKEN
from app.confirmation_gate import run_confirmation_gate
from app.firestore_db import (
    ensure_conversation_doc,
    get_db,
    inbound_to_content_blocks,
    lookup_member_by_phone,
    write_message_turn,
)
from app.history import compile_conversation_history
from app.idempotency import claim_idempotency_key
from app.inbound_parser import extract_messages_from_payload, normalize_webhook_message
from app.media_ingest import ingest_media_blocks
from app.models import InboundMessage
from app.vertex_client import get_prefix_token_count, initialize_prefix_at_startup, run_agent_turn
from app.whatsapp import send_text_message, verify_signature

# Cloud Logging friendly stdout logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("houseops")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("application_startup phase=1")
    prefix_text, token_count = initialize_prefix_at_startup()
    app.state.prefix_token_count = token_count
    app.state.prefix_chars = len(prefix_text)
    logger.info(
        "startup_prefix_ready prefix_token_count=%s prefix_chars=%s",
        token_count,
        len(prefix_text),
    )
    yield
    logger.info("application_shutdown")


app = FastAPI(title="HouseOps", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "phase": 1,
        "prefix_token_count": get_prefix_token_count(),
    }


from fastapi import Query

@app.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Meta webhook subscription verification."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        logger.info("whatsapp_webhook_verified")
        return PlainTextResponse(content=hub_challenge or "")
    logger.warning("whatsapp_webhook_verify_failed mode=%s", hub_mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    """
    Fast path: signature verify → idempotency → member allowlist →
    InboundMessage envelope → Cloud Tasks enqueue → 200 OK.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(raw_body, signature):
        logger.warning("webhook_signature_invalid")
        raise HTTPException(status_code=401, detail="Invalid signature")

    import json

    try:
        body = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        body = {}

    # Status updates (delivered/read) — acknowledge without processing
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if value.get("statuses") and not value.get("messages"):
                logger.info("webhook_status_ack count=%s", len(value.get("statuses", [])))
                return Response(status_code=200)

    db = get_db()
    now = datetime.now(RIYADH_TZ)
    processed = 0

    for phone_e164, message in extract_messages_from_payload(body):
        message_id = message.get("id")
        if not message_id:
            continue

        if not claim_idempotency_key(db, message_id, now):
            logger.info("webhook_duplicate_skipped message_id=%s", message_id)
            continue

        member = lookup_member_by_phone(db, phone_e164)
        if not member:
            logger.info("webhook_unauthorized phone=%s message_id=%s", phone_e164, message_id)
            continue

        inbound = normalize_webhook_message(message, phone_e164, member.member_id)
        if not inbound:
            logger.warning("webhook_normalize_failed message_id=%s", message_id)
            continue

        try:
            enqueue_inbound_processing(inbound)
            processed += 1
            logger.info(
                "webhook_enqueued message_id=%s phone=%s member_id=%s",
                message_id,
                phone_e164,
                member.member_id,
            )
        except Exception as exc:
            logger.exception(
                "webhook_enqueue_failed message_id=%s error=%s",
                message_id,
                exc,
            )

    logger.info("webhook_batch_complete processed=%s", processed)
    return Response(status_code=200)


def _build_session_context(
    member_name: str,
    role: str,
    capabilities: list[str],
    conv_state: dict[str, Any],
    gate_note: str | None = None,
) -> str:
    now = datetime.now(RIYADH_TZ)
    lines = [
        f"Current datetime: {now.isoformat()}",
        f"Speaker: {member_name}",
        f"Role: {role}",
        f"Capabilities: {', '.join(capabilities) or 'none'}",
        f"active_module: {conv_state.get('active_module', 'property_management')}",
    ]
    pending = conv_state.get("pending_confirmation")
    if pending:
        lines.append(f"pending_confirmation: {pending.get('summary', 'active')}")
    elif gate_note:
        lines.append(gate_note)
    else:
        paused = conv_state.get("paused_confirmations") or []
        if paused:
            lines.append(f"paused_confirmations: {len(paused)} item(s) on stack")
    if gate_note and pending:
        lines.append(gate_note)
    return "\n".join(lines)


@app.post("/tasks/process-inbound")
async def process_inbound(request: Request) -> JSONResponse:
    """
    Heavy path: media ingest → confirmation gate → Gemini (Module 2) →
    persist message turns → WhatsApp reply.
    """
    raw_body = await request.body()
    try:
        inbound = InboundMessage.model_validate_json(raw_body)
    except Exception as exc:
        logger.exception("process_inbound_invalid_envelope error=%s", exc)
        raise HTTPException(status_code=400, detail="Invalid InboundMessage") from exc

    logger.info(
        "process_inbound_start message_id=%s phone=%s member_id=%s blocks=%s",
        inbound.message_id,
        inbound.phone_e164,
        inbound.member_id,
        len(inbound.content),
    )

    db = get_db()
    member = lookup_member_by_phone(db, inbound.phone_e164)
    if not member or member.member_id != inbound.member_id:
        logger.warning(
            "process_inbound_member_mismatch phone=%s",
            inbound.phone_e164,
        )
        return JSONResponse({"status": "skipped", "reason": "unauthorized"}, status_code=200)

    ensure_conversation_doc(db, inbound.phone_e164, member.member_id)

    # §9.1 Media ingest before Gemini
    media_ok, media_error = ingest_media_blocks(inbound)
    if not media_ok:
        logger.warning(
            "process_inbound_media_failed message_id=%s reason=%s",
            inbound.message_id,
            media_error,
        )
        if media_error:
            send_text_message(inbound.phone_e164, media_error)
        return JSONResponse({"status": "media_failed"}, status_code=200)

    # §9.3 Confirmation gate (before Gemini)
    gate = run_confirmation_gate(db, inbound.phone_e164, inbound)
    conv_state = (
        db.collection("conversations").document(inbound.phone_e164).get().to_dict() or {}
    )

    if gate.handled and gate.reply_text and not gate.proceed_to_gemini:
        send_text_message(inbound.phone_e164, gate.reply_text)
        write_message_turn(
            db,
            inbound.phone_e164,
            inbound.message_id,
            "user",
            inbound_to_content_blocks(inbound),
        )
        reply_id = f"reply_{uuid.uuid4().hex[:12]}"
        write_message_turn(
            db,
            inbound.phone_e164,
            reply_id,
            "assistant",
            [{"block_type": "text", "text": gate.reply_text}],
        )
        logger.info(
            "process_inbound_gate_reply message_id=%s reply_id=%s",
            inbound.message_id,
            reply_id,
        )
        return JSONResponse({"status": "gate_handled"})

    session_context = _build_session_context(
        member.name,
        member.role,
        member.capabilities,
        conv_state,
        gate_note=gate.session_note,
    )

    history_text, history_stats = compile_conversation_history(db, inbound.phone_e164)
    logger.info(
        "process_inbound_history_stats message_id=%s stats=%s suffix_history_tokens=%s",
        inbound.message_id,
        history_stats,
        history_stats.get("final_token_count"),
    )

    if not gate.proceed_to_gemini:
        return JSONResponse({"status": "no_gemini"})

    reply_text, usage = run_agent_turn(
        tier=member.role,
        member_id=member.member_id,
        phone_e164=inbound.phone_e164,
        session_context=session_context,
        history_text=history_text,
        inbound=inbound,
        db=db,
    )

    # Preemption hold line (§9.3 rule 3)
    if gate.session_note and "paused" in gate.session_note.lower():
        reply_text += "\n\nYour pending request is on hold — reply 'resume' to continue."

    try:
        send_text_message(inbound.phone_e164, reply_text)
    except Exception as exc:
        logger.exception(
            "whatsapp_send_failed message_id=%s error=%s",
            inbound.message_id,
            exc,
        )

    write_message_turn(
        db,
        inbound.phone_e164,
        inbound.message_id,
        "user",
        inbound_to_content_blocks(inbound),
    )
    reply_id = f"reply_{uuid.uuid4().hex[:12]}"
    write_message_turn(
        db,
        inbound.phone_e164,
        reply_id,
        "assistant",
        [{"block_type": "text", "text": reply_text}],
    )

    logger.info(
        "process_inbound_complete message_id=%s reply_id=%s usage=%s",
        inbound.message_id,
        reply_id,
        usage,
    )
    return JSONResponse(
        {
            "status": "ok",
            "reply_id": reply_id,
            "history_stats": history_stats,
            "usage": usage,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, log_level="info")
