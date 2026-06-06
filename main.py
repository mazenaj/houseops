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
from typing import Any, Union

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.cloud_tasks import enqueue_inbound_processing
from app.config import RIYADH_TZ, TELEGRAM_BOT_TOKEN, OPS_BOT_USER_ID
from app.confirmation_gate import run_confirmation_gate
from app.firestore_db import (
    ensure_conversation_doc,
    get_db,
    inbound_to_content_blocks,
    lookup_member_by_phone,
    lookup_member_by_telegram_chat_id,
    link_telegram_chat_id,
    write_message_turn,
)
from app.history import compile_conversation_history
from app.idempotency import claim_idempotency_key, release_idempotency_key
from app.inbound_parser import normalize_telegram_message
from app.media_ingest import ingest_media_blocks
from app.models import InboundMessage
from app.vertex_client import (
    get_prefix_token_count,
    initialize_prefix_at_startup,
    run_agent_turn,
)
from app.telegram import (
    send_text_message,
    verify_webhook_secret,
    request_contact_share,
    delete_message,
)

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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, StarletteHTTPException):
        return await http_exception_handler(request, exc)

    logger.exception("unhandled_error_occurred path=%s error=%s", request.url.path, exc)
    try:
        from app.ops_bot import send_ops_alert

        send_ops_alert(
            get_db(),
            "SYSTEM_CRASH",
            f"Unhandled exception on path {request.url.path}",
            error=exc,
        )
    except Exception as alert_exc:
        logger.exception("ops_alert_sending_failed error=%s", alert_exc)

    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "phase": 1,
        "prefix_token_count": get_prefix_token_count(),
    }


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    """
    Fast path: webhook secret verify → login flow / member lookup →
    idempotency → InboundMessage envelope → Cloud Tasks enqueue → 200 OK.
    """
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not verify_webhook_secret(secret_header):
        logger.warning("webhook_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Webhook secret invalid")

    import json

    raw_body = await request.body()
    try:
        body = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        body = {}

    db = get_db()
    now = datetime.now(RIYADH_TZ)

    # 1. Resolve Chat ID and Message ID for onboarding & routing
    chat_id = None
    message_id = None

    if "callback_query" in body:
        cb = body["callback_query"]
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        message_id = f"tg_cb_{cb.get('id', '')}"
    elif "message" in body:
        msg = body["message"]
        chat_id = msg.get("chat", {}).get("id")
        message_id = f"tg_msg_{msg.get('message_id', '')}"

    if not chat_id:
        logger.warning("webhook_missing_chat_id")
        return Response(status_code=200)

    # 2. Handle verified Contact Share (Onboarding / Auth)
    if "message" in body and "contact" in body["message"]:
        contact = body["message"]["contact"]
        raw_phone = contact.get("phone_number", "")
        if raw_phone:
            # Normalize E.164 phone number
            phone = raw_phone if raw_phone.startswith("+") else f"+{raw_phone}"
            logger.info("webhook_contact_received phone=%s chat_id=%d", phone, chat_id)

            member = await run_in_threadpool(lookup_member_by_phone, db, phone)
            if member:
                await run_in_threadpool(link_telegram_chat_id, db, phone, chat_id)
                await run_in_threadpool(
                    send_text_message,
                    chat_id,
                    "Welcome to DQ Villa Bot! 🎉",
                )
            else:
                await run_in_threadpool(
                    send_text_message,
                    chat_id,
                    f"Access Denied: The phone number {phone} is not whitelisted in the HouseOps system.",
                )
        return Response(status_code=200)

    # 3. Authenticate standard message by Chat ID
    member = await run_in_threadpool(lookup_member_by_telegram_chat_id, db, chat_id)
    if not member and OPS_BOT_USER_ID and chat_id == OPS_BOT_USER_ID:
        from app.models import Member

        member = Member(
            member_id="bot_ops",
            phone_e164="+00000000001",
            name="DQBotOpsBot",
            role="tier2",
            capabilities=[],
            active=True,
            preferred_language="en",
        )
        logger.info("webhook_ops_bot_authenticated")

    if not member:
        logger.info(
            "webhook_unauthorized_chat_id chat_id=%d — requesting contact share",
            chat_id,
        )
        await run_in_threadpool(
            request_contact_share,
            chat_id,
            "Please share your number to proceed",
        )
        return Response(status_code=200)

    # Special Bot-to-Bot Integration Ping Check
    if chat_id == OPS_BOT_USER_ID:
        text = ""
        if "message" in body and "text" in body["message"]:
            text = body["message"]["text"]
        if text == "ping_test":
            logger.info("webhook_received_ops_bot_ping")
            # We no longer send a separate egress test success message to Mazen to avoid duplication.
            # The status report will compile and show the integration check status in a single consolidated message.
            return JSONResponse(
                status_code=200, content={"status": "ok", "message": "ping_received"}
            )

    # 4. Deduplicate message
    if not message_id:
        return Response(status_code=200)

    if not await run_in_threadpool(claim_idempotency_key, db, message_id, now):
        logger.info("webhook_duplicate_skipped message_id=%s", message_id)
        return Response(status_code=200)

    # 5. Normalize update to uniform InboundMessage
    inbound = normalize_telegram_message(body, member.member_id, member.phone_e164)
    if not inbound:
        return Response(status_code=200)

    # 6. Enqueue to Cloud Tasks
    try:
        await run_in_threadpool(enqueue_inbound_processing, inbound)
        logger.info(
            "webhook_enqueued message_id=%s chat_id=%d member_id=%s",
            inbound.message_id,
            chat_id,
            member.member_id,
        )
    except Exception as exc:
        logger.exception(
            "webhook_enqueue_failed message_id=%s error=%s", inbound.message_id, exc
        )
        try:
            await run_in_threadpool(release_idempotency_key, db, message_id)
        except Exception as del_exc:
            logger.exception(
                "webhook_idempotency_release_failed message_id=%s error=%s",
                message_id,
                del_exc,
            )
        return Response(content="Enqueuing failed, please retry", status_code=500)


def _build_session_context(
    member_id: str,
    member_name: str,
    role: str,
    capabilities: list[str],
    conv_state: dict[str, Any],
    gate_note: str | None = None,
) -> str:
    now = datetime.now(RIYADH_TZ)
    lines = [
        f"Current datetime: {now.isoformat()}",
        f"Speaker member_id: {member_id}",
        f"Speaker name: {member_name}",
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
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_job_secret(secret_header):
        logger.warning("process_inbound_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

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
        return JSONResponse(
            {"status": "skipped", "reason": "unauthorized"}, status_code=200
        )

    user_tg_id = None
    if inbound.message_id.startswith("tg_msg_"):
        try:
            user_tg_id = int(inbound.message_id[7:])
        except ValueError:
            pass

    await run_in_threadpool(
        ensure_conversation_doc, db, inbound.phone_e164, member.member_id
    )

    # §9.1 Media ingest before Gemini
    media_ok, media_error = await run_in_threadpool(ingest_media_blocks, inbound)
    if not media_ok:
        logger.warning(
            "process_inbound_media_failed message_id=%s reason=%s",
            inbound.message_id,
            media_error,
        )
        if media_error and member.telegram_chat_id:
            await run_in_threadpool(
                send_text_message, member.telegram_chat_id, media_error
            )
        return JSONResponse({"status": "media_failed"}, status_code=200)

    # §9.3 Confirmation gate (before Gemini)
    gate = await run_in_threadpool(
        run_confirmation_gate, db, inbound.phone_e164, inbound
    )
    conv_doc_ref = db.collection("conversations").document(inbound.phone_e164)
    conv_snap = await run_in_threadpool(conv_doc_ref.get)
    conv_state = conv_snap.to_dict() or {}

    if (
        gate.handled
        and gate.reply_text
        and not gate.proceed_to_gemini
        and member.telegram_chat_id
    ):
        tg_res = await run_in_threadpool(
            send_text_message, member.telegram_chat_id, gate.reply_text
        )
        tg_msg_id = tg_res.get("result", {}).get("message_id") if tg_res else None

        await run_in_threadpool(
            write_message_turn,
            db,
            inbound.phone_e164,
            inbound.message_id,
            "user",
            inbound_to_content_blocks(inbound),
            telegram_chat_id=member.telegram_chat_id,
            telegram_message_id=user_tg_id,
        )
        reply_id = f"reply_{uuid.uuid4().hex[:12]}"
        await run_in_threadpool(
            write_message_turn,
            db,
            inbound.phone_e164,
            reply_id,
            "assistant",
            [{"block_type": "text", "text": gate.reply_text}],
            telegram_chat_id=member.telegram_chat_id,
            telegram_message_id=tg_msg_id,
        )
        logger.info(
            "process_inbound_gate_reply message_id=%s reply_id=%s tg_msg_id=%s",
            inbound.message_id,
            reply_id,
            tg_msg_id,
        )
        return JSONResponse({"status": "gate_handled"})

    session_context = _build_session_context(
        member.member_id,
        member.name,
        member.role,
        member.capabilities,
        conv_state,
        gate_note=gate.session_note,
    )

    history_text, history_stats = await run_in_threadpool(
        compile_conversation_history, db, inbound.phone_e164
    )
    logger.info(
        "process_inbound_history_stats message_id=%s stats=%s suffix_history_tokens=%s",
        inbound.message_id,
        history_stats,
        history_stats.get("final_token_count"),
    )

    if not gate.proceed_to_gemini:
        return JSONResponse({"status": "no_gemini"})

    reply_text, usage = await run_in_threadpool(
        run_agent_turn,
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
        reply_text += (
            "\n\nYour pending request is on hold — reply 'resume' to continue."
        )

    # Check if there is an active pending confirmation to attach inline buttons
    updated_state_snap = await run_in_threadpool(conv_doc_ref.get)
    updated_state = updated_state_snap.to_dict() or {}
    pending = updated_state.get("pending_confirmation")

    inline_keyboard = None
    if pending and pending.get("status") == "active":
        inline_keyboard = [
            [
                {"text": "✅ Yes, Confirm", "callback_data": "yes"},
                {"text": "❌ No, Cancel", "callback_data": "no"},
            ]
        ]

    tg_msg_id = None
    try:
        if member.telegram_chat_id:
            if inline_keyboard:
                tg_res = await run_in_threadpool(
                    send_text_message,
                    member.telegram_chat_id,
                    reply_text,
                    inline_keyboard=inline_keyboard,
                )
            else:
                tg_res = await run_in_threadpool(
                    send_text_message, member.telegram_chat_id, reply_text
                )
            tg_msg_id = tg_res.get("result", {}).get("message_id") if tg_res else None
    except Exception as exc:
        logger.exception(
            "telegram_send_failed message_id=%s error=%s",
            inbound.message_id,
            exc,
        )

    await run_in_threadpool(
        write_message_turn,
        db,
        inbound.phone_e164,
        inbound.message_id,
        "user",
        inbound_to_content_blocks(inbound),
        telegram_chat_id=member.telegram_chat_id,
        telegram_message_id=user_tg_id,
    )
    reply_id = f"reply_{uuid.uuid4().hex[:12]}"
    await run_in_threadpool(
        write_message_turn,
        db,
        inbound.phone_e164,
        reply_id,
        "assistant",
        [{"block_type": "text", "text": reply_text}],
        telegram_chat_id=member.telegram_chat_id,
        telegram_message_id=tg_msg_id,
    )

    logger.info(
        "process_inbound_complete message_id=%s reply_id=%s tg_msg_id=%s usage=%s",
        inbound.message_id,
        reply_id,
        tg_msg_id,
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


def verify_job_secret(secret_header: Union[str, None]) -> bool:
    """Validate X-HouseOps-Secret-Token header."""
    import hashlib
    import hmac

    if not TELEGRAM_BOT_TOKEN:
        return True
    if not secret_header:
        return False
    expected = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).hexdigest()
    return hmac.compare_digest(secret_header, expected)


def _execute_message_cleanup(
    db: Any, cutoff: datetime, now: datetime
) -> tuple[int, int]:
    from datetime import timedelta

    deleted_count = 0
    skipped_count = 0

    conversations = db.collection("conversations").stream()
    for conv in conversations:
        phone_e164 = conv.id
        messages = (
            db.collection("conversations")
            .document(phone_e164)
            .collection("messages")
            .where("timestamp", "<", cutoff)
            .stream()
        )
        for msg_doc in messages:
            ref = msg_doc.reference
            data = msg_doc.to_dict() or {}

            if data.get("telegram_deleted"):
                continue

            chat_id = data.get("telegram_chat_id")
            msg_id = data.get("telegram_message_id")
            role = data.get("role", "user")

            if not chat_id or not msg_id:
                ref.update({"telegram_deleted": True})
                skipped_count += 1
                continue

            # Check 48 hour limit for user message deletion
            timestamp = data.get("timestamp")
            if role == "user" and timestamp and (now - timestamp) > timedelta(hours=48):
                ref.update({"telegram_deleted": True})
                skipped_count += 1
                logger.info(
                    "cleanup_messages_user_msg_expired_48h chat_id=%s message_id=%s",
                    chat_id,
                    msg_id,
                )
                continue

            success = delete_message(int(chat_id), int(msg_id))
            ref.update({"telegram_deleted": True})
            if success:
                deleted_count += 1
            else:
                skipped_count += 1

    return deleted_count, skipped_count


@app.post("/jobs/cleanup-messages")
async def cleanup_messages(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_job_secret(secret_header):
        logger.warning("cleanup_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    from datetime import timedelta

    db = get_db()
    now = datetime.now(RIYADH_TZ)
    cutoff = now - timedelta(hours=24)
    logger.info("cleanup_messages_job_start cutoff=%s", cutoff.isoformat())

    try:
        deleted_count, skipped_count = await run_in_threadpool(
            _execute_message_cleanup, db, cutoff, now
        )
    except Exception as exc:
        logger.exception("cleanup_messages_job_failed error=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info(
        "cleanup_messages_job_complete deleted=%d skipped=%d",
        deleted_count,
        skipped_count,
    )
    return Response(
        content=f"OK: deleted={deleted_count}, skipped={skipped_count}",
        media_type="text/plain",
    )


@app.post("/jobs/nightly-calendar-sync")
async def nightly_calendar_sync(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_job_secret(secret_header):
        logger.warning("nightly_sync_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.workflow import run_nightly_calendar_sync

    result = await run_in_threadpool(run_nightly_calendar_sync, db)
    return JSONResponse(result)


@app.post("/jobs/calendar-onboarding-nag")
async def calendar_onboarding_nag(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_job_secret(secret_header):
        logger.warning("onboarding_nag_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.workflow import run_calendar_onboarding_nag

    await run_in_threadpool(run_calendar_onboarding_nag, db)
    return Response(content="OK", media_type="text/plain")


@app.post("/jobs/driver-arrival-nag")
async def driver_arrival_nag(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_job_secret(secret_header):
        logger.warning("driver_nag_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.workflow import run_driver_arrival_nag

    await run_in_threadpool(run_driver_arrival_nag, db)
    return Response(content="OK", media_type="text/plain")


@app.post("/jobs/ops-status-update")
async def ops_status_update(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_job_secret(secret_header):
        logger.warning("ops_status_update_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.ops_bot import get_ops_status_report, send_ops_message

    report = await run_in_threadpool(get_ops_status_report, db)
    await run_in_threadpool(send_ops_message, db, report)
    return Response(content="OK", media_type="text/plain")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, log_level="info")
