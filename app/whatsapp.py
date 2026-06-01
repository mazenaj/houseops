"""WhatsApp Cloud API — signature verification and outbound sends."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Union

import httpx

from app.config import WHATSAPP_APP_SECRET, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TOKEN

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


def verify_signature(raw_body: bytes, signature_header: Union[str, None]) -> bool:
    """Validate X-Hub-Signature-256 from Meta."""
    if not WHATSAPP_APP_SECRET:
        logger.warning("whatsapp_app_secret_missing — skipping signature verification")
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("whatsapp_signature_missing_or_invalid")
        return False
    expected = signature_header.split("=", 1)[1]
    digest = hmac.new(
        WHATSAPP_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    valid = hmac.compare_digest(digest, expected)
    if not valid:
        logger.warning("whatsapp_signature_mismatch")
    return valid


def get_media_url(media_id: str) -> str:
    """Resolve authenticated Meta media download URL."""
    if not WHATSAPP_TOKEN:
        raise RuntimeError("WHATSAPP_TOKEN is not configured")
    url = f"{GRAPH_API_BASE}/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    media_url = data.get("url")
    if not media_url:
        raise ValueError(f"No media URL returned for media_id={media_id}")
    logger.info("media_url_resolved media_id=%s", media_id)
    return media_url


def send_text_message(phone_e164: str, text: str) -> dict[str, Any]:
    """Send a text reply within the 24h customer service window."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WhatsApp credentials not configured")
    to = phone_e164.lstrip("+")
    url = f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},
    }
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        try:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("meta_api_rejection_payload: %s", exc.response.text)
            raise
        result = resp.json()
    logger.info("whatsapp_text_sent phone=%s message_length=%d", phone_e164, len(text))
    return result
