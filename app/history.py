"""Conversation history compilation with suffix token budget (SCHEMA §7)."""

from __future__ import annotations

import logging
from typing import Any

from google.cloud import firestore

from app.config import HISTORY_QUERY_LIMIT, MAX_SUFFIX_HISTORY_TOKENS

logger = logging.getLogger(__name__)


def estimate_tokens_locally(text: str) -> int:
    """
    Fast, local CPU-bound token estimator (0 ms latency).
    Uses fast encoding length check instead of character-by-character loops.
    Gemini tokenization averages:
      - ~3.8 characters per token for English/ASCII.
      - ~1.2 characters per token for Arabic/Non-ASCII characters (conservative).
    """
    if not text:
        return 0
    non_ascii_count = len(text) - len(text.encode("ascii", errors="ignore"))
    ascii_count = len(text) - non_ascii_count
    estimated = int((ascii_count / 3.8) + (non_ascii_count / 1.2))
    return max(1, estimated)


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


def summarize_dropped_turns(old_summary: str, dropped_turns_text: str) -> str:
    """Use Gemini to summarize newly dropped turns and merge them with the existing rolling summary."""
    try:
        from app.vertex_client import _get_default_model

        model = _get_default_model()
        prompt = (
            "You are a household operations assistant. Your task is to maintain a rolling summary of past conversation history.\n"
            "Here is the existing rolling summary of previous messages:\n"
            f"<existing_summary>\n{old_summary or 'None'}\n</existing_summary>\n\n"
            "Here are the newly dropped turns from the conversation history:\n"
            f"<new_dropped_turns>\n{dropped_turns_text}\n</new_dropped_turns>\n\n"
            "Please output a single, merged, concise summary of the key tasks, outings, and instructions discussed. Keep the summary under 150 words."
        )
        response = model.generate_content(
            prompt, generation_config={"temperature": 0.0, "max_output_tokens": 300}
        )
        summary = response.text.strip()
        logger.info("history_summary_updated length=%d", len(summary))
        return summary
    except Exception as e:
        logger.error("failed_summarizing_dropped_turns error=%s", e)
        return old_summary


def compile_conversation_history(
    db: firestore.Client,
    phone_e164: str,
) -> tuple[str, dict[str, int]]:
    """
    Query messages subcollection, trim to <= 3000 tokens.
    Returns (history_text, stats_dict).
    """
    conv_ref = db.collection("conversations").document(phone_e164)
    conv_snap = conv_ref.get()
    old_summary = ""
    if conv_snap.exists:
        old_summary = (conv_snap.to_dict() or {}).get("history_summary", "")

    messages_ref = conv_ref.collection("messages")
    query = messages_ref.order_by(
        "timestamp", direction=firestore.Query.DESCENDING
    ).limit(HISTORY_QUERY_LIMIT)
    docs = list(query.stream())
    turns_loaded = len(docs)
    # Chronological order (oldest first)
    turns = [doc.to_dict() or {} for doc in reversed(docs)]

    serialized = [_serialize_turn(t) for t in turns]
    turns_dropped = 0
    dropped_turns = []

    # Truncate any individual oversized turns first to protect the rest of the history
    for i in range(len(serialized)):
        if estimate_tokens_locally(serialized[i]) > 800:
            serialized[i] = _truncate_turn_text(serialized[i], max_tokens=800)

    # Fast local character heuristic to trim first (approx. 4 chars per token)
    char_limit = MAX_SUFFIX_HISTORY_TOKENS * 4
    while sum(len(t) for t in serialized) > char_limit and len(serialized) > 1:
        dropped_turns.append(serialized.pop(0))
        turns_dropped += 1

    history_text = "\n".join(serialized)
    token_count = estimate_tokens_locally(history_text)

    while token_count > MAX_SUFFIX_HISTORY_TOKENS and serialized:
        dropped_turns.append(serialized.pop(0))
        turns_dropped += 1
        history_text = "\n".join(serialized)
        token_count = estimate_tokens_locally(history_text)

    # Update rolling summary if turns were dropped
    current_summary = old_summary
    if turns_dropped > 0:
        new_dropped_text = "\n".join(dropped_turns)
        current_summary = summarize_dropped_turns(old_summary, new_dropped_text)
        try:
            conv_ref.update({"history_summary": current_summary})
        except Exception as exc:
            logger.error(
                "failed_updating_history_summary phone=%s error=%s", phone_e164, exc
            )

    # Prepend summary to history text
    if current_summary:
        history_text = (
            f"SUMMARY OF OLDER HISTORY:\n{current_summary}\n\nCONVERSATION HISTORY:\n"
            + history_text
        )

    stats = {
        "turns_loaded": turns_loaded,
        "turns_dropped": turns_dropped,
        "final_token_count": estimate_tokens_locally(history_text),
        "turns_included": len(serialized),
    }
    logger.info(
        "conversation_history_compiled phone=%s turns_loaded=%s turns_dropped=%s final_token_count=%s",
        phone_e164,
        turns_loaded,
        turns_dropped,
        stats["final_token_count"],
    )
    if turns_dropped > 5:
        logger.warning(
            "conversation_history_high_drop_rate phone=%s turns_dropped=%s",
            phone_e164,
            turns_dropped,
        )
    return history_text, stats
