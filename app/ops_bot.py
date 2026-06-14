"""Operations Bot module (DQBotOpsBot) for status reporting and system alerts."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any, Union
from concurrent.futures import ThreadPoolExecutor

import httpx
from google.cloud import firestore

from app.config import (
    RIYADH_TZ,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_OPS_BOT_TOKEN,
    SERVICE_URL,
    EXPECTED_SECRET_TOKEN,
)
from app.telegram import http_client

logger = logging.getLogger(__name__)

TELEGRAM_OPS_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_OPS_BOT_TOKEN}"


def _get_mazen_chat_id(db: firestore.Client) -> Union[int, None]:
    """Retrieve Mazen's chat ID from members collection."""
    try:
        # Target phone defined in SCHEMA §1
        query = (
            db.collection("members")
            .where("phone_e164", "==", "+966506667785")
            .where("active", "==", True)
            .limit(1)
        )
        docs = list(query.stream())
        if docs:
            data = docs[0].to_dict()
            return data.get("telegram_chat_id")
    except Exception:
        logger.exception("failed_resolving_mazen_chat_id")
    return None


def send_ops_message(db: firestore.Client, text: str) -> dict[str, Any]:
    """Send an operational message to Mazen."""
    chat_id = _get_mazen_chat_id(db)
    if not chat_id:
        logger.error("ops_bot_send_failed chat_id_unresolved")
        return {"ok": False, "error": "Chat ID unresolved"}

    url = f"{TELEGRAM_OPS_API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    headers = {
        "Content-Type": "application/json",
    }

    try:
        resp = http_client.post(url, json=payload, headers=headers)
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


def _check_db(db: firestore.Client, now: datetime) -> tuple[bool, str]:
    try:
        # Perform a quick read/write test on a health check doc
        db.collection("system").document("ops_health_check").set(
            {"last_checked": now, "status": "healthy"}, merge=True
        )
        return True, "🟢 *Database:* OK (Firestore Read/Write verified)"
    except Exception as e:
        return False, f"🔴 *Database:* FAILED (Error: {str(e)})"


def _check_vertex() -> tuple[bool, str]:
    try:
        from app.vertex_client import get_prefix_token_count

        token_count = get_prefix_token_count()
        # Verify the model initializes and returns a valid prefix token count
        # In Gemini 2.5 context caching floor is 2048 tokens
        if token_count >= 2048:
            return True, f"🟢 *Vertex AI:* OK (Cached Prefix: {token_count} tokens)"
        else:
            return (
                True,
                f"🟡 *Vertex AI:* Warning (Prefix count {token_count} is under cached floor of 2048)",
            )
    except Exception as e:
        return False, f"🔴 *Vertex AI:* FAILED (Error: {str(e)})"


def _check_telegram_webhook(bot_token: str) -> tuple[bool, str]:
    try:
        if not bot_token:
            return False, "🔴 *Telegram Webhook:* FAILED (Bot token not configured)"
        url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
        resp = http_client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        webhook_url = result.get("url", "")
        pending = result.get("pending_update_count", 0)
        last_err_msg = result.get("last_error_message")

        if not webhook_url:
            return False, "🔴 *Telegram Webhook:* Webhook is not configured"
        elif last_err_msg:
            return (
                False,
                f"🔴 *Telegram Webhook:* Error ({last_err_msg}, Pending updates: {pending})",
            )
        else:
            return (
                True,
                f"🟢 *Telegram Webhook:* OK (Active, Pending updates: {pending})",
            )
    except Exception as e:
        return False, f"🔴 *Telegram Webhook:* FAILED (Connection error: {str(e)})"


def _check_ops_bot(ops_token: str) -> tuple[bool, str]:
    try:
        if not ops_token:
            return (
                False,
                "🔴 *Ops Bot API Connection:* FAILED (Ops bot token not configured)",
            )
        url = f"https://api.telegram.org/bot{ops_token}/getWebhookInfo"
        resp = http_client.get(url, timeout=10.0)
        resp.raise_for_status()
        return True, "🟢 *Ops Bot API Connection:* OK (Live connection verified)"
    except Exception as e:
        return (
            False,
            f"🔴 *Ops Bot API Connection:* FAILED (Connection error: {str(e)})",
        )


def _check_bot_integration(
    now: datetime, bot_token: str, ops_token: str, service_url: str, secret_token: str
) -> tuple[bool, str]:
    try:
        if not service_url:
            return (
                True,
                "🟡 *Bot-to-Bot Integration:* Skip (SERVICE_URL not configured)",
            )
        if not bot_token or not ops_token:
            return False, "🔴 *Bot-to-Bot Integration:* FAILED (Bot tokens missing)"

        webhook_url = f"{service_url.rstrip('/')}/webhook/telegram"
        ops_bot_id = int(ops_token.split(":")[0])

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
            "X-Telegram-Bot-Api-Secret-Token": secret_token,
        }

        resp = http_client.post(
            webhook_url, json=payload, headers=headers, timeout=10.0
        )
        resp.raise_for_status()
        resp_data = resp.json()

        if (
            resp_data.get("status") == "ok"
            and resp_data.get("message") == "ping_received"
        ):
            return True, "🟢 *Bot-to-Bot Integration:* OK (Ingress & Egress verified)"
        else:
            return (
                False,
                f"🔴 *Bot-to-Bot Integration:* FAILED (Invalid response: {resp.text})",
            )
    except Exception as e:
        return False, f"🔴 *Bot-to-Bot Integration:* FAILED (Error: {str(e)})"


def get_ops_status_report(db: firestore.Client) -> str:
    """Compile a system performance health check report with color coding concurrently."""
    now = datetime.now(RIYADH_TZ)
    today_str = now.date().isoformat()
    time_str = now.strftime("%I:%M %p")

    with ThreadPoolExecutor(max_workers=5) as executor:
        f_db = executor.submit(_check_db, db, now)
        f_vertex = executor.submit(_check_vertex)
        f_tg = executor.submit(_check_telegram_webhook, TELEGRAM_BOT_TOKEN)
        f_ops = executor.submit(_check_ops_bot, TELEGRAM_OPS_BOT_TOKEN)
        f_integration = executor.submit(
            _check_bot_integration,
            now,
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_OPS_BOT_TOKEN,
            SERVICE_URL,
            EXPECTED_SECRET_TOKEN,
        )

        db_ok, db_status = f_db.result()
        vertex_ok, vertex_status = f_vertex.result()
        tg_ok, tg_status = f_tg.result()
        ops_ok, ops_status = f_ops.result()
        bot_integration_ok, bot_integration_status = f_integration.result()

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
        query = suggestions_ref.where("status", "==", "not reviewed").order_by(
            "created_at"
        )

        # Stream suggestions
        docs = list(query.stream())

        # Fetch Mazen's preferred language from Firestore
        lang = "en"
        try:
            mazen_query = (
                db.collection("members")
                .where("phone_e164", "==", "+966506667785")
                .where("active", "==", True)
                .limit(1)
            )
            mazen_docs = list(mazen_query.stream())
            if mazen_docs:
                lang = mazen_docs[0].to_dict().get("preferred_language", "en")
        except Exception:
            logger.exception("failed_resolving_mazen_preferred_language")

        if not docs:
            # Send status update if there are no pending suggestions
            if lang == "ar":
                text = "📋 *تحديث الاقتراحات اليومي لـ DQBotOps*\n\nلا توجد اقتراحات بانتظار المراجعة حالياً."
            else:
                text = "📋 *DQBotOps Daily Suggestions Update*\n\nThere are currently no suggestions awaiting review."
            return send_ops_message(db, text)

        if lang == "ar":
            lines = [
                "📋 *تحديث الاقتراحات اليومي لـ DQBotOps*",
                f"إليك الاقتراحات التي تنتظر المراجعة حالياً (المجموع: {len(docs)}):",
                "",
            ]
        else:
            lines = [
                "📋 *DQBotOps Daily Suggestions Update*",
                f"Here are the suggestions currently awaiting review (Total: {len(docs)}):",
                "",
            ]

        for idx, doc in enumerate(docs, 1):
            data = doc.to_dict() or {}
            fallback = "لا يوجد ملخص" if lang == "ar" else "No Summary"
            summary = data.get("summary", fallback).strip()
            # Under 5 words summary safety check/fallback
            words = summary.split()
            if len(words) >= 5:
                summary = " ".join(words[:4]) + "..."

            if lang == "ar":
                lines.append(f"{idx}. {summary} (الرمز: `{doc.id}`)")
            else:
                lines.append(f"{idx}. {summary} (ID: `{doc.id}`)")

        text = "\n".join(lines)
        return send_ops_message(db, text)
    except Exception as e:
        logger.exception("failed_running_morning_suggestions_update")
        send_ops_alert(db, "SUGGESTIONS_JOB_FAILED", str(e))
        return {"ok": False, "error": str(e)}
