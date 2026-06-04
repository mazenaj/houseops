"""Telegram Bot API — signature verification, outbound sends, and media downloads."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Union

import httpx

from app.config import TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def verify_webhook_secret(secret_header: Union[str, None]) -> bool:
    """Validate X-Telegram-Bot-Api-Secret-Token header."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("telegram_bot_token_missing — skipping signature verification")
        return True
    if not secret_header:
        logger.warning("telegram_secret_header_missing")
        return False
    # Use SHA256 of the bot token as the secret token to avoid extra env variables
    expected = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).hexdigest()
    valid = hmac.compare_digest(secret_header, expected)
    if not valid:
        logger.warning("telegram_secret_token_mismatch")
    return valid


def get_media_url(file_id: str) -> str:
    """Resolve authenticated Telegram media download URL."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"{TELEGRAM_API_BASE}/getFile"
    payload = {"file_id": file_id}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    file_path = data.get("result", {}).get("file_path")
    if not file_path:
        raise ValueError(f"No file path returned for file_id={file_id}")
    media_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    logger.info("media_url_resolved file_id=%s", file_id)
    return media_url


def send_text_message(chat_id: int, text: str, inline_keyboard: Union[list, None] = None) -> dict[str, Any]:
    """Send a text reply to the specified chat (supports optional inline keyboard)."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Telegram credentials not configured")
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    headers = {
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        try:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("telegram_api_rejection_payload: %s", exc.response.text)
            raise
        result = resp.json()
    logger.info("telegram_text_sent chat_id=%d message_length=%d", chat_id, len(text))
    return result


def request_contact_share(chat_id: int, text: str = "Please share your phone number to log in:") -> dict[str, Any]:
    """Request the user's verified phone number using a contact share button."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Telegram credentials not configured")
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {
            "keyboard": [[{"text": "📱 Share Phone Number", "request_contact": True}]],
            "one_time_keyboard": True,
            "resize_keyboard": True
        }
    }
    headers = {
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        try:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("telegram_api_rejection_payload: %s", exc.response.text)
            raise
        result = resp.json()
    logger.info("telegram_contact_request_sent chat_id=%d", chat_id)
    return result


def delete_message(chat_id: int, message_id: int) -> bool:
    """Delete a Telegram message by its message_id."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Telegram credentials not configured")
    url = f"{TELEGRAM_API_BASE}/deleteMessage"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
    }
    headers = {
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        try:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info("telegram_message_deleted chat_id=%d message_id=%d", chat_id, message_id)
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "telegram_delete_failed chat_id=%d message_id=%d response=%s",
                chat_id,
                message_id,
                exc.response.text,
            )
            return False
        except Exception as exc:
            logger.exception("telegram_delete_exception chat_id=%d message_id=%d", chat_id, message_id)
            return False
