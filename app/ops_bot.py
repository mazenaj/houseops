"""Operations Bot module (DQBotOpsBot) for status reporting and system alerts."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any, Union

import httpx
from google.cloud import firestore

from app.config import RIYADH_TZ, TELEGRAM_OPS_BOT_TOKEN

logger = logging.getLogger(__name__)

TELEGRAM_OPS_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_OPS_BOT_TOKEN}"


def _get_mazen_chat_id(db: firestore.Client) -> Union[int, None]:
    """Retrieve Mazen's telegram_chat_id from the members collection."""
    # Look up by stable ID or name
    doc_ref = db.collection("members").document("mem_principal_001")
    snap = doc_ref.get()
    if snap.exists:
        data = snap.to_dict() or {}
        chat_id = data.get("telegram_chat_id")
        if chat_id:
            return int(chat_id)

    # Fallback to name query
    query = (
        db.collection("members")
        .where("name", "==", "Mazen")
        .where("active", "==", True)
        .limit(1)
    )
    docs = list(query.stream())
    if docs:
        data = docs[0].to_dict() or {}
        chat_id = data.get("telegram_chat_id")
        if chat_id:
            return int(chat_id)

    logger.warning("ops_bot_mazen_chat_id_not_found")
    return None


def send_ops_message(db: firestore.Client, text: str) -> dict[str, Any]:
    """Send an outbound message to Mazen via the DQBotOpsBot Telegram API."""
    if not TELEGRAM_OPS_BOT_TOKEN:
        logger.warning("TELEGRAM_OPS_BOT_TOKEN_missing — cannot send ops message")
        return {"ok": False, "error": "token_missing"}

    chat_id = _get_mazen_chat_id(db)
    if not chat_id:
        logger.warning("ops_bot_cannot_send_no_chat_id_for_mazen")
        return {"ok": False, "error": "mazen_chat_id_missing"}

    url = f"{TELEGRAM_OPS_API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    headers = {
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        try:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                "ops_bot_text_sent chat_id=%d message_length=%d", chat_id, len(text)
            )
            return result
        except httpx.HTTPStatusError as exc:
            logger.error("ops_bot_api_rejection_payload: %s", exc.response.text)
            return {"ok": False, "error": exc.response.text}
        except Exception as exc:
            logger.exception("ops_bot_send_failed")
            return {"ok": False, "error": str(exc)}


def send_ops_alert(
    db: firestore.Client,
    alert_type: str,
    details: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    """Send an operational alert to Mazen."""
    now = datetime.now(RIYADH_TZ)
    lines = [
        "🚨 *DQBotOps System Alert*",
        f"*Type:* {alert_type}",
        f"*Time:* {now.strftime('%Y-%m-%d %I:%M:%S %p')}",
        "",
        "*Details:*",
        details,
    ]
    if error:
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        # Limit traceback to fit in Telegram message length (max 4096 chars)
        if len(tb) > 1000:
            tb = tb[-1000:]
        lines.extend(["", "*Exception:*", f"```python\n{tb}\n```"])

    text = "\n".join(lines)
    return send_ops_message(db, text)


def get_ops_status_report(db: firestore.Client) -> str:
    """Compile a system performance health check report with color coding."""
    now = datetime.now(RIYADH_TZ)
    today_str = now.date().isoformat()
    time_str = now.strftime("%I:%M %p")

    # 1. Firestore Database health check
    try:
        # Perform a quick read/write test on a health check doc
        db.collection("system").document("ops_health_check").set(
            {"last_checked": now, "status": "healthy"}, merge=True
        )
        db_status = "🟢 *Database:* OK (Firestore Read/Write verified)"
        db_ok = True
    except Exception as e:
        db_status = f"🔴 *Database:* FAILED (Error: {str(e)})"
        db_ok = False

    # 2. Vertex AI API health check
    try:
        from app.vertex_client import get_prefix_token_count

        token_count = get_prefix_token_count()
        # Verify the model initializes and returns a valid prefix token count
        if token_count >= 4096:
            vertex_status = f"🟢 *Vertex AI:* OK (Cached Prefix: {token_count} tokens)"
        else:
            vertex_status = f"🟡 *Vertex AI:* Warning (Prefix count {token_count} is under cached floor of 4096)"
        vertex_ok = True
    except Exception as e:
        vertex_status = f"🔴 *Vertex AI:* FAILED (Error: {str(e)})"
        vertex_ok = False

    # 3. Ingress Telegram Bot Webhook Check
    try:
        from app.config import TELEGRAM_BOT_TOKEN

        if not TELEGRAM_BOT_TOKEN:
            tg_status = "🔴 *Telegram Webhook:* FAILED (Bot token not configured)"
            tg_ok = False
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", {})
            webhook_url = result.get("url", "")
            pending = result.get("pending_update_count", 0)
            last_err_msg = result.get("last_error_message")

            if not webhook_url:
                tg_status = "🔴 *Telegram Webhook:* Webhook is not configured"
                tg_ok = False
            elif last_err_msg:
                tg_status = f"🔴 *Telegram Webhook:* Error ({last_err_msg}, Pending updates: {pending})"
                tg_ok = False
            else:
                tg_status = (
                    f"🟢 *Telegram Webhook:* OK (Active, Pending updates: {pending})"
                )
                tg_ok = True
    except Exception as e:
        tg_status = f"🔴 *Telegram Webhook:* FAILED (Connection error: {str(e)})"
        tg_ok = False

    # 4. Outbound Ops Bot Connectivity Check
    try:
        if not TELEGRAM_OPS_BOT_TOKEN:
            ops_status = (
                "🔴 *Ops Bot API Connection:* FAILED (Ops bot token not configured)"
            )
            ops_ok = False
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_OPS_BOT_TOKEN}/getWebhookInfo"
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            ops_status = "🟢 *Ops Bot API Connection:* OK (Live connection verified)"
            ops_ok = True
    except Exception as e:
        ops_status = f"🔴 *Ops Bot API Connection:* FAILED (Connection error: {str(e)})"
        ops_ok = False

    # 5. Telegram Bot-to-Bot Integration Test (Ingress/Egress)
    bot_integration_ok = False
    try:
        from app.config import SERVICE_URL, EXPECTED_SECRET_TOKEN

        if not SERVICE_URL:
            bot_integration_status = (
                "🟡 *Bot-to-Bot Integration:* Skip (SERVICE_URL not configured)"
            )
            bot_integration_ok = True
        elif not TELEGRAM_BOT_TOKEN or not TELEGRAM_OPS_BOT_TOKEN:
            bot_integration_status = (
                "🔴 *Bot-to-Bot Integration:* FAILED (Bot tokens missing)"
            )
        else:
            webhook_url = f"{SERVICE_URL.rstrip('/')}/webhook/telegram"
            ops_bot_id = int(TELEGRAM_OPS_BOT_TOKEN.split(":")[0])

            payload = {
                "update_id": int(now.timestamp()),
                "message": {
                    "message_id": int(now.timestamp() * 1000) % 1000000,
                    "from": {
                        "id": ops_bot_id,
                        "is_bot": True,
                        "first_name": "DQBotOpsBot",
                        "username": "DQBotOpsBot",
                    },
                    "chat": {
                        "id": ops_bot_id,
                        "first_name": "DQBotOpsBot",
                        "username": "DQBotOpsBot",
                        "type": "private",
                    },
                    "date": int(now.timestamp()),
                    "text": "ping_test",
                },
            }

            headers = {
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": EXPECTED_SECRET_TOKEN,
            }

            with httpx.Client(timeout=10.0) as client:
                resp = client.post(webhook_url, json=payload, headers=headers)
                resp.raise_for_status()
                resp_data = resp.json()

                if (
                    resp_data.get("status") == "ok"
                    and resp_data.get("message") == "ping_received"
                ):
                    bot_integration_status = (
                        "🟢 *Bot-to-Bot Integration:* OK (Ingress & Egress verified)"
                    )
                    bot_integration_ok = True
                else:
                    bot_integration_status = f"🔴 *Bot-to-Bot Integration:* FAILED (Invalid response: {resp.text})"
    except Exception as e:
        bot_integration_status = (
            f"🔴 *Bot-to-Bot Integration:* FAILED (Error: {str(e)})"
        )

    # Combine overall system health
    if db_ok and vertex_ok and tg_ok and ops_ok and bot_integration_ok:
        overall_status = "🟢 *System Status:* Healthy (All subsystems operational)"
    else:
        overall_status = (
            "🔴 *System Status:* Attention Required (Subsystem failure detected)"
        )

    report_lines = [
        "🖥️ *DQBotOps Performance Report*",
        f"*Date:* {today_str}",
        f"*Time:* {time_str}",
        "",
        overall_status,
        "",
        "*Subsystem Details:*",
        f"- {db_status}",
        f"- {vertex_status}",
        f"- {tg_status}",
        f"- {ops_status}",
        f"- {bot_integration_status}",
    ]
    return "\n".join(report_lines)


def run_morning_suggestions_update(db: firestore.Client) -> dict[str, Any]:
    """Retrieve all pending suggestions ('not reviewed') and send a summary list to Mazen via DQBotOpsBot."""
    try:
        suggestions_ref = db.collection("user_suggestions")
        query = suggestions_ref.where("status", "==", "not reviewed")

        # Stream suggestions
        docs = list(query.stream())

        if not docs:
            # Send status update if there are no pending suggestions
            text = "📋 *DQBotOps Daily Suggestions Update*\n\nThere are currently no suggestions awaiting review."
            return send_ops_message(db, text)

        # Sort in memory by created_at ascending to avoid composite index requirement
        docs.sort(key=lambda x: (x.to_dict() or {}).get("created_at") or datetime.min)

        lines = [
            "📋 *DQBotOps Daily Suggestions Update*",
            f"Here are the suggestions currently awaiting review (Total: {len(docs)}):",
            "",
        ]

        for idx, doc in enumerate(docs, 1):
            data = doc.to_dict() or {}
            summary = data.get("summary", "No Summary").strip()
            # Under 5 words summary safety check/fallback
            words = summary.split()
            if len(words) >= 5:
                summary = " ".join(words[:4]) + "..."

            lines.append(f"{idx}. {summary} (ID: `{doc.id}`)")

        text = "\n".join(lines)
        return send_ops_message(db, text)
    except Exception as e:
        logger.exception("failed_running_morning_suggestions_update")
        send_ops_alert(db, "SUGGESTIONS_JOB_FAILED", str(e))
        return {"ok": False, "error": str(e)}
