"""Conversation history compilation with suffix token budget (SCHEMA §7)."""

from __future__ import annotations

import logging
from typing import Any

from google.cloud import firestore

from app.config import HISTORY_QUERY_LIMIT, MAX_SUFFIX_HISTORY_TOKENS
from app.vertex_client import count_tokens_text

logger = logging.getLogger(__name__)


def _serialize_turn(doc: dict[str, Any]) -> str:
    role = doc.get("role", "user")
    blocks = doc.get("content_blocks", [])
    parts: list[str] = []
    for block in blocks:
        if block.get("block_type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("block_type") == "media":
            mime = block.get("normalized_mime_type") or block.get("mime_type", "media")
            uri = block.get("gcs_uri") or block.get("media_id", "")
            parts.append(f"[{mime}: {uri}]")
    body = " ".join(parts).strip()
    return f"{role.upper()}: {body}"


def _truncate_turn_text(text: str, max_tokens: int = 800) -> str:
    """Truncate a single oversized turn using local char approximation (SCHEMA §7 step 5)."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…[truncated]"


def compile_conversation_history(
    db: firestore.Client,
    phone_e164: str,
) -> tuple[str, dict[str, int]]:
    """
    Query messages subcollection, trim to <= 3000 tokens.
    Returns (history_text, stats_dict).
    """
    messages_ref = (
        db.collection("conversations").document(phone_e164).collection("messages")
    )
    query = messages_ref.order_by(
        "timestamp", direction=firestore.Query.DESCENDING
    ).limit(HISTORY_QUERY_LIMIT)
    docs = list(query.stream())
    turns_loaded = len(docs)
    # Chronological order (oldest first)
    turns = [doc.to_dict() or {} for doc in reversed(docs)]

    serialized = [_serialize_turn(t) for t in turns]
    turns_dropped = 0

    # Single-turn truncation if needed
    if (
        len(serialized) == 1
        and count_tokens_text(serialized[0]) > MAX_SUFFIX_HISTORY_TOKENS
    ):
        serialized[0] = _truncate_turn_text(serialized[0], max_tokens=800)

    history_text = "\n".join(serialized)
    token_count = count_tokens_text(history_text)

    while token_count > MAX_SUFFIX_HISTORY_TOKENS and serialized:
        serialized.pop(0)
        turns_dropped += 1
        history_text = "\n".join(serialized)
        token_count = count_tokens_text(history_text)

    stats = {
        "turns_loaded": turns_loaded,
        "turns_dropped": turns_dropped,
        "final_token_count": token_count,
        "turns_included": len(serialized),
    }
    logger.info(
        "conversation_history_compiled phone=%s turns_loaded=%s turns_dropped=%s final_token_count=%s",
        phone_e164,
        turns_loaded,
        turns_dropped,
        token_count,
    )
    if turns_dropped > 5:
        logger.warning(
            "conversation_history_high_drop_rate phone=%s turns_dropped=%s",
            phone_e164,
            turns_dropped,
        )
    return history_text, stats
