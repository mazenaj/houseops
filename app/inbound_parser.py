"""Normalize Telegram Bot API webhook payloads to InboundMessage (SCHEMA §3)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Union

from app.config import RIYADH_TZ
from app.models import InboundMessage, MediaBlock, TextBlock

logger = logging.getLogger(__name__)


def _parse_timestamp(date_val: Union[str, int, None]) -> datetime:
    if date_val is None:
        return datetime.now(RIYADH_TZ)
    try:
        return datetime.fromtimestamp(int(date_val), tz=RIYADH_TZ)
    except ValueError:
        return datetime.now(RIYADH_TZ)


def normalize_telegram_message(
    update: dict[str, Any],
    member_id: str,
    phone_e164: str,
) -> Union[InboundMessage, None]:
    """
    Build uniform InboundMessage from a single Telegram update object (text, media, or callback_query).
    Returns None if the update has no processable content.
    """
    # 1. Handle Callback Query (from inline buttons)
    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = str(cb.get("id", ""))
        if not cb_id.isalnum():
            logger.warning("invalid_callback_query_id id=%s", cb_id)
            return None
        message = cb.get("message", {})
        data = cb.get("data", "")
        if not data:
            return None

        # Treat button click payload as text content for the engine
        content = [TextBlock(text=data)]
        inbound = InboundMessage(
            message_id=f"tg_cb_{cb_id}",
            phone_e164=phone_e164,
            member_id=member_id,
            received_at=datetime.now(RIYADH_TZ),
            content=content,
        )
        logger.info("telegram_callback_normalized message_id=%s data=%s", inbound.message_id, data)
        return inbound

    # 2. Handle standard Message
    message = update.get("message")
    if not message:
        return None

    message_id = message.get("message_id")
    if message_id is None:
        return None
    try:
        # Validate that message_id is an integer (Telegram message IDs are always numeric)
        message_id = int(message_id)
    except (ValueError, TypeError):
        logger.warning("invalid_message_id id=%s", message_id)
        return None

    received_at = _parse_timestamp(message.get("date"))
    content: list[TextBlock | MediaBlock] = []

    # Text message
    text = message.get("text")
    if text:
        content.append(TextBlock(text=text))

    # Media with optional caption
    caption = message.get("caption")
    if caption:
        content.append(TextBlock(text=caption))

    # Photo (Telegram sends an array of sizes, we take the largest one at the end)
    if "photo" in message and message["photo"]:
        photo_sizes = message["photo"]
        largest_photo = photo_sizes[-1]
        file_id = largest_photo.get("file_id")
        if file_id:
            content.append(
                MediaBlock(
                    media_id=file_id,
                    mime_type="image/jpeg",
                    gcs_uri=None,
                    normalized_mime_type=None,
                )
            )

    # Voice message
    elif "voice" in message:
        voice = message["voice"]
        file_id = voice.get("file_id")
        mime_type = voice.get("mime_type", "audio/ogg")
        if file_id:
            content.append(
                MediaBlock(
                    media_id=file_id,
                    mime_type=mime_type,
                    gcs_uri=None,
                    normalized_mime_type=None,
                )
            )

    # Document / file attachment
    elif "document" in message:
        doc = message["document"]
        file_id = doc.get("file_id")
        mime_type = doc.get("mime_type", "application/octet-stream")
        if file_id:
            content.append(
                MediaBlock(
                    media_id=file_id,
                    mime_type=mime_type,
                    gcs_uri=None,
                    normalized_mime_type=None,
                )
            )

    # Audio file
    elif "audio" in message:
        audio = message["audio"]
        file_id = audio.get("file_id")
        mime_type = audio.get("mime_type", "audio/mpeg")
        if file_id:
            content.append(
                MediaBlock(
                    media_id=file_id,
                    mime_type=mime_type,
                    gcs_uri=None,
                    normalized_mime_type=None,
                )
            )

    # Video file
    elif "video" in message:
        video = message["video"]
        file_id = video.get("file_id")
        mime_type = video.get("mime_type", "video/mp4")
        if file_id:
            content.append(
                MediaBlock(
                    media_id=file_id,
                    mime_type=mime_type,
                    gcs_uri=None,
                    normalized_mime_type=None,
                )
            )

    if not content:
        logger.info("telegram_message_no_processable_content message_id=%d", message_id)
        return None

    inbound = InboundMessage(
        message_id=f"tg_msg_{message_id}",
        phone_e164=phone_e164,
        member_id=member_id,
        received_at=received_at,
        content=content,
    )
    logger.info(
        "telegram_message_normalized message_id=%s phone=%s blocks=%d",
        inbound.message_id,
        phone_e164,
        len(content),
    )
    return inbound
