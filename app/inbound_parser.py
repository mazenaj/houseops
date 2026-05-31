"""Normalize Meta WhatsApp webhook payloads to InboundMessage (SCHEMA §3)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.config import RIYADH_TZ
from app.models import InboundMessage, MediaBlock, TextBlock

logger = logging.getLogger(__name__)


def _parse_timestamp(ts: str | int | None) -> datetime:
    if ts is None:
        return datetime.now(RIYADH_TZ)
    if isinstance(ts, str):
        try:
            return datetime.fromtimestamp(int(ts), tz=RIYADH_TZ)
        except ValueError:
            return datetime.now(RIYADH_TZ)
    return datetime.fromtimestamp(int(ts), tz=RIYADH_TZ)


def normalize_webhook_message(
    message: dict[str, Any],
    phone_e164: str,
    member_id: str,
) -> InboundMessage | None:
    """
    Build uniform InboundMessage from a single Meta message object.
    Returns None if message has no processable content.
    """
    message_id = message.get("id")
    if not message_id:
        logger.warning("inbound_missing_message_id phone=%s", phone_e164)
        return None

    received_at = _parse_timestamp(message.get("timestamp"))
    content: list[TextBlock | MediaBlock] = []
    msg_type = message.get("type")

    if msg_type == "text":
        body = (message.get("text") or {}).get("body", "")
        if body:
            content.append(TextBlock(text=body))
    elif msg_type in ("image", "audio", "video", "document", "sticker"):
        media_obj = message.get(msg_type) or {}
        media_id = media_obj.get("id")
        mime_type = media_obj.get("mime_type", "application/octet-stream")
        caption = media_obj.get("caption")
        if caption:
            content.append(TextBlock(text=caption))
        if media_id:
            content.append(
                MediaBlock(
                    media_id=media_id,
                    mime_type=mime_type,
                    gcs_uri=None,
                    normalized_mime_type=None,
                )
            )
    elif msg_type == "interactive":
        interactive = message.get("interactive") or {}
        button_reply = interactive.get("button_reply") or {}
        list_reply = interactive.get("list_reply") or {}
        text = button_reply.get("title") or list_reply.get("title") or ""
        if text:
            content.append(TextBlock(text=text))
    else:
        logger.info("inbound_unsupported_type type=%s message_id=%s", msg_type, message_id)
        return None

    if not content:
        logger.warning("inbound_empty_content message_id=%s", message_id)
        return None

    inbound = InboundMessage(
        message_id=message_id,
        phone_e164=phone_e164,
        member_id=member_id,
        received_at=received_at,
        content=content,
    )
    logger.info(
        "inbound_normalized message_id=%s phone=%s blocks=%d",
        message_id,
        phone_e164,
        len(content),
    )
    return inbound


def extract_messages_from_payload(body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """
    Extract (phone_e164, message) pairs from Meta webhook JSON.
    phone is normalized to E.164 with leading +.
    """
    results: list[tuple[str, dict[str, Any]]] = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {c.get("wa_id"): c for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                wa_id = msg.get("from")
                if not wa_id:
                    continue
                phone = wa_id if wa_id.startswith("+") else f"+{wa_id}"
                results.append((phone, msg))
    return results
