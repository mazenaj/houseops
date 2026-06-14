"""
Household Operations Engine — Phase 1 entrypoint (SCHEMA.md).

Fast path: POST /webhook/telegram
Heavy path: POST /tasks/process-inbound
"""

from __future__ import annotations

import logging
import sys
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.cloud_tasks import enqueue_inbound_processing
from app.config import (
    RIYADH_TZ,
    TELEGRAM_BOT_TOKEN,
    OPS_BOT_USER_ID,
    TASKS_SERVICE_ACCOUNT,
    SERVICE_URL,
)

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
    verify_secret_token,
    verify_internal_token,
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
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(
            status_code=500, detail="TELEGRAM_BOT_TOKEN is not configured"
        )
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
    if not verify_secret_token(secret_header):
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
        cb_data = cb.get("data", "")
        if cb_data.startswith("pref_lang_"):
            lang = cb_data.split("_")[-1]  # "en" or "ar"
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            from app.telegram import answer_callback_query

            await run_in_threadpool(answer_callback_query, cb.get("id"))
            if chat_id:
                member = await run_in_threadpool(
                    lookup_member_by_telegram_chat_id, db, chat_id
                )
                if member:
                    from app.firestore_db import update_member_preferred_language

                    await run_in_threadpool(
                        update_member_preferred_language,
                        db,
                        member.phone_e164,
                        lang,
                    )
                    if lang == "ar":
                        reply_text = "تم تحديد اللغة المفضلة إلى العربية."
                    elif lang == "ur":
                        reply_text = "پسندیدہ زبان اردو کے طور پر سیٹ کی گئی ہے۔"
                    elif lang == "tl":
                        reply_text = "Itinakda ang ginustong wika sa Tagalog."
                    else:
                        reply_text = "Preferred language set to English."

                    await run_in_threadpool(
                        ensure_conversation_doc, db, member.phone_e164, member.member_id
                    )

                    user_msg_id = f"tg_cb_{cb.get('id', '')}"
                    await run_in_threadpool(
                        write_message_turn,
                        db,
                        member.phone_e164,
                        user_msg_id,
                        "user",
                        [{"block_type": "text", "text": f"Selected language: {lang}"}],
                        telegram_chat_id=chat_id,
                        telegram_message_id=None,
                    )

                    tg_res = await run_in_threadpool(
                        send_text_message, chat_id, reply_text
                    )
                    tg_msg_id = (
                        tg_res.get("result", {}).get("message_id") if tg_res else None
                    )
                    reply_id = f"reply_{uuid.uuid4().hex[:12]}"
                    await run_in_threadpool(
                        write_message_turn,
                        db,
                        member.phone_e164,
                        reply_id,
                        "assistant",
                        [{"block_type": "text", "text": reply_text}],
                        telegram_chat_id=chat_id,
                        telegram_message_id=tg_msg_id,
                    )
            return Response(status_code=200)

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
                welcome_text = (
                    "Welcome to DQ Villa Bot! 🎉\n\n"
                    "Please select your preferred language / الرجاء اختيار لغتك المفضلة / "
                    "براہ کرم اپنی پسندیدہ زبان منتخب کریں / Mangyaring piliin ang iyong ginustong wika:"
                )
                inline_keyboard = [
                    [
                        {"text": "English", "callback_data": "pref_lang_en"},
                        {"text": "العربية", "callback_data": "pref_lang_ar"},
                    ],
                    [
                        {"text": "Urdu (اردو)", "callback_data": "pref_lang_ur"},
                        {"text": "Tagalog", "callback_data": "pref_lang_tl"},
                    ],
                ]

                await run_in_threadpool(
                    ensure_conversation_doc, db, phone, member.member_id
                )

                user_msg_id = f"tg_msg_{body['message'].get('message_id', '')}"
                await run_in_threadpool(
                    write_message_turn,
                    db,
                    phone,
                    user_msg_id,
                    "user",
                    [
                        {
                            "block_type": "text",
                            "text": f"Shared contact for phone: {phone}",
                        }
                    ],
                    telegram_chat_id=chat_id,
                    telegram_message_id=body["message"].get("message_id"),
                )

                tg_res = await run_in_threadpool(
                    send_text_message,
                    chat_id,
                    welcome_text,
                    inline_keyboard=inline_keyboard,
                )
                tg_msg_id = (
                    tg_res.get("result", {}).get("message_id") if tg_res else None
                )
                reply_id = f"reply_{uuid.uuid4().hex[:12]}"
                await run_in_threadpool(
                    write_message_turn,
                    db,
                    phone,
                    reply_id,
                    "assistant",
                    [{"block_type": "text", "text": welcome_text}],
                    telegram_chat_id=chat_id,
                    telegram_message_id=tg_msg_id,
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

    # 3.1 Intercept settings/language command
    text = ""
    if "message" in body and "text" in body["message"]:
        text = body["message"]["text"].strip().lower()

    if text in (
        "/language",
        "language",
        "اللغة",
        "/lang",
        "/settings",
        "settings",
        "الاعدادات",
    ):
        prompt_text = (
            "Please select your preferred language / الرجاء اختيار لغتك المفضلة / "
            "براہ کرم اپنی پسندیدہ زبان منتخب کریں / Mangyaring piliin ang iyong ginustong wika:"
        )
        inline_keyboard = [
            [
                {"text": "English", "callback_data": "pref_lang_en"},
                {"text": "العربية", "callback_data": "pref_lang_ar"},
            ],
            [
                {"text": "Urdu (اردو)", "callback_data": "pref_lang_ur"},
                {"text": "Tagalog", "callback_data": "pref_lang_tl"},
            ],
        ]

        await run_in_threadpool(
            ensure_conversation_doc, db, member.phone_e164, member.member_id
        )

        user_msg_id = f"tg_msg_{body['message'].get('message_id', '')}"
        await run_in_threadpool(
            write_message_turn,
            db,
            member.phone_e164,
            user_msg_id,
            "user",
            [{"block_type": "text", "text": body["message"].get("text", "")}],
            telegram_chat_id=chat_id,
            telegram_message_id=body["message"].get("message_id"),
        )

        tg_res = await run_in_threadpool(
            send_text_message,
            chat_id,
            prompt_text,
            inline_keyboard=inline_keyboard,
        )
        tg_msg_id = tg_res.get("result", {}).get("message_id") if tg_res else None
        reply_id = f"reply_{uuid.uuid4().hex[:12]}"
        await run_in_threadpool(
            write_message_turn,
            db,
            member.phone_e164,
            reply_id,
            "assistant",
            [{"block_type": "text", "text": prompt_text}],
            telegram_chat_id=chat_id,
            telegram_message_id=tg_msg_id,
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
    enqueued_successfully = False
    try:
        inbound = normalize_telegram_message(body, member.member_id, member.phone_e164)
        if not inbound:
            return Response(status_code=200)

        # 6. Enqueue to Cloud Tasks
        await run_in_threadpool(enqueue_inbound_processing, inbound)
        enqueued_successfully = True
        logger.info(
            "webhook_enqueued message_id=%s chat_id=%d member_id=%s",
            inbound.message_id,
            chat_id,
            member.member_id,
        )
    except Exception as exc:
        logger.exception(
            "webhook_enqueue_failed message_id=%s error=%s", message_id, exc
        )
        return Response(content="Enqueuing failed, please retry", status_code=500)
    finally:
        if not enqueued_successfully:
            try:
                await run_in_threadpool(release_idempotency_key, db, message_id)
            except Exception as del_exc:
                logger.exception(
                    "webhook_idempotency_release_failed message_id=%s error=%s",
                    message_id,
                    del_exc,
                )


def _build_session_context(
    member_id: str,
    member_name: str,
    role: str,
    capabilities: list[str],
    conv_state: dict[str, Any],
    gate_note: str | None = None,
    preferred_language: str = "en",
) -> str:
    now = datetime.now(RIYADH_TZ)
    lines = [
        f"Current datetime: {now.isoformat()}",
        f"Speaker member_id: {member_id}",
        f"Speaker name: {member_name}",
        f"Role: {role}",
        f"Capabilities: {', '.join(capabilities) or 'none'}",
        f"active_module: {conv_state.get('active_module', 'property_management')}",
        f"Preferred language: {preferred_language}",
    ]
    if preferred_language == "ar":
        lines.append(
            "IMPORTANT: The user preferred language is Arabic (ar). You MUST communicate, converse, and reply to the user in Arabic. Keep the responses concise, respectful, and action-oriented in Arabic. Remember that all tool call arguments must remain in English as per the translation boundary."
        )
    elif preferred_language == "ur":
        lines.append(
            "IMPORTANT: The user preferred language is Urdu (ur). You MUST communicate, converse, and reply to the user in Urdu. Keep the responses concise, respectful, and action-oriented in Urdu. Remember that all tool call arguments must remain in English as per the translation boundary."
        )
    elif preferred_language == "tl":
        lines.append(
            "IMPORTANT: The user preferred language is Tagalog (tl). You MUST communicate, converse, and reply to the user in Tagalog. Keep the responses concise, respectful, and action-oriented in Tagalog. Remember that all tool call arguments must remain in English as per the translation boundary."
        )
    else:
        lines.append(
            "IMPORTANT: The user preferred language is English (en). You MUST communicate, converse, and reply to the user in English."
        )

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
    if TASKS_SERVICE_ACCOUNT:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            logger.warning("process_inbound_oidc_missing")
            raise HTTPException(
                status_code=401, detail="Unauthorized: Missing OIDC token"
            )
        token = auth_header.split(" ", 1)[1]
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests

            audience = SERVICE_URL.rstrip("/") if SERVICE_URL else None
            id_info = id_token.verify_oauth2_token(
                token, google_requests.Request(), audience=audience
            )
            email = id_info.get("email")
            if not email or email != TASKS_SERVICE_ACCOUNT:
                logger.warning(
                    "process_inbound_oidc_mismatch expected=%s actual=%s",
                    TASKS_SERVICE_ACCOUNT,
                    email,
                )
                raise HTTPException(
                    status_code=403, detail="Forbidden: OIDC service account mismatch"
                )
        except Exception as exc:
            logger.warning("process_inbound_oidc_failed error=%s", exc)
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: OIDC verification failed: {str(exc)}",
            )

    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
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
    from app.models import Member

    # Parallelize Firestore point lookups and message queries
    member_ref = db.collection("members").document(inbound.member_id)
    conv_doc_ref = db.collection("conversations").document(inbound.phone_e164)

    member_task = run_in_threadpool(member_ref.get)
    conv_task = run_in_threadpool(conv_doc_ref.get)
    history_task = run_in_threadpool(
        compile_conversation_history, db, inbound.phone_e164
    )

    member_snap, conv_snap, (history_text, history_stats) = await asyncio.gather(
        member_task, conv_task, history_task
    )

    member = None
    if member_snap.exists:
        mdata = member_snap.to_dict() or {}
        mdata["member_id"] = member_snap.id
        member = Member(**mdata)

    # Added explicit member.active check (Audit finding 2)
    if not member or not member.active or member.phone_e164 != inbound.phone_e164:
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

    # Ensure conversation doc exists and update it only when necessary (Audit finding 4)
    if not conv_snap.exists:
        await run_in_threadpool(
            ensure_conversation_doc, db, inbound.phone_e164, member.member_id, conv_snap
        )
    else:
        existing_member_id = conv_snap.to_dict().get("member_id")
        if existing_member_id != member.member_id:
            await run_in_threadpool(
                conv_doc_ref.update,
                {"member_id": member.member_id, "updated_at": datetime.now(RIYADH_TZ)},
            )

    # Check language of inbound message (fast-exit on other languages)
    inbound_text = " ".join(
        block.text for block in inbound.content if block.block_type == "text"
    ).strip()
    if inbound_text:
        from app.vertex_client import detect_language

        lang = await run_in_threadpool(detect_language, inbound_text)
        if lang == "Other":
            reply_text = (
                "Please communicate in English, Arabic, Urdu, or Tagalog. / "
                "الرجاء التواصل باللغة الإنجليزية، العربية، الأردية، أو التاغالوغية."
            )
            tg_msg_id = None
            if member.telegram_chat_id:
                tg_res = await run_in_threadpool(
                    send_text_message, member.telegram_chat_id, reply_text
                )
                tg_msg_id = (
                    tg_res.get("result", {}).get("message_id") if tg_res else None
                )

                # Persist turn in history
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
            return JSONResponse({"status": "language_blocked"})

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

    # §9.3 Confirmation gate (before Gemini) using pre-loaded snapshots/state (Audit finding 3)
    conv_state = conv_snap.to_dict() or {}
    gate = await run_in_threadpool(
        run_confirmation_gate, db, inbound.phone_e164, inbound, conv_state, member
    )

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
        preferred_language=member.preferred_language,
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
        resumed_state=gate.resumed_payload,
        caller_name=member.name,
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
        if member.preferred_language == "ar":
            inline_keyboard = [
                [
                    {"text": "✅ نعم، تأكيد", "callback_data": "yes"},
                    {"text": "❌ لا، إلغاء", "callback_data": "no"},
                ]
            ]
        else:
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


def _execute_message_cleanup(db: Any, cutoff: datetime, now: datetime) -> None:
    import time
    from datetime import timedelta

    deleted_count = 0
    skipped_count = 0

    try:
        messages = (
            db.collection_group("messages").where("timestamp", "<", cutoff).stream()
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

            time.sleep(0.05)

            success = delete_message(int(chat_id), int(msg_id))
            ref.update({"telegram_deleted": True})
            if success:
                deleted_count += 1
            else:
                skipped_count += 1

        logger.info(
            "cleanup_messages_job_complete deleted=%d skipped=%d",
            deleted_count,
            skipped_count,
        )
    except Exception as exc:
        logger.exception("cleanup_messages_job_failed error=%s", exc)


@app.post("/jobs/cleanup-messages")
async def cleanup_messages(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
        logger.warning("cleanup_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    from datetime import timedelta

    db = get_db()
    now = datetime.now(RIYADH_TZ)
    cutoff = now - timedelta(hours=24)
    logger.info("cleanup_messages_job_submitted cutoff=%s", cutoff.isoformat())

    background_tasks.add_task(_execute_message_cleanup, db, cutoff, now)

    return Response(
        content="Cleanup job submitted to background tasks",
        media_type="text/plain",
    )


@app.post("/jobs/nightly-calendar-sync")
async def nightly_calendar_sync(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
        logger.warning("nightly_sync_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.workflow import run_nightly_calendar_sync

    result = await run_in_threadpool(run_nightly_calendar_sync, db)
    return JSONResponse(result)


@app.post("/jobs/calendar-onboarding-nag")
async def calendar_onboarding_nag(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
        logger.warning("onboarding_nag_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.workflow import run_calendar_onboarding_nag

    await run_in_threadpool(run_calendar_onboarding_nag, db)
    return Response(content="OK", media_type="text/plain")


@app.post("/jobs/driver-arrival-nag")
async def driver_arrival_nag(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
        logger.warning("driver_nag_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.workflow import run_driver_arrival_nag

    await run_in_threadpool(run_driver_arrival_nag, db)
    return Response(content="OK", media_type="text/plain")


@app.post("/jobs/ops-status-update")
async def ops_status_update(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
        logger.warning("ops_status_update_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.ops_bot import get_ops_status_report, send_ops_message

    report = await run_in_threadpool(get_ops_status_report, db)
    await run_in_threadpool(send_ops_message, db, report)
    return Response(content="OK", media_type="text/plain")


@app.post("/jobs/daily-weather-tasks")
async def daily_weather_tasks(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
        logger.warning("daily_weather_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.workflow import run_daily_weather_tasks_job

    await run_in_threadpool(run_daily_weather_tasks_job, db)
    return Response(content="OK", media_type="text/plain")


@app.post("/jobs/morning-suggestions-update")
async def morning_suggestions_update(request: Request) -> Response:
    secret_header = request.headers.get("X-HouseOps-Secret-Token")
    if not verify_internal_token(secret_header):
        logger.warning("morning_suggestions_job_secret_invalid")
        raise HTTPException(status_code=403, detail="Forbidden: Secret token invalid")

    db = get_db()
    from app.ops_bot import run_morning_suggestions_update

    await run_in_threadpool(run_morning_suggestions_update, db)
    return Response(content="OK", media_type="text/plain")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, log_level="info")
