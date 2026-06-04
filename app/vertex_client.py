"""Vertex AI Gemini client — token counting and generation (SCHEMA §7)."""

from __future__ import annotations

import json
import logging
from typing import Any

import vertexai
from vertexai.generative_models import (
    Content,
    FunctionDeclaration,
    GenerationConfig,
    GenerativeModel,
    Part,
    Tool,
)

from app.config import GEMINI_MODEL, MIN_PREFIX_TOKENS, PROJECT_ID, VERTEX_LOCATION
from app.models import InboundMessage
from app.prompts import (
    CACHE_PADDING_BLOCK,
    FEW_SHOT_EXAMPLES,
    MODULE_1_SCHEMA,
    MODULE_2_SCHEMA,
    OPERATIONAL_RULES,
    RBAC_TIER_DESCRIPTIONS,
    STATIC_SYSTEM_PROMPT,
    TOOL_DECLARATIONS_TEXT,
)
from app.tools_module2 import MODULE2_TOOL_DECLARATIONS, execute_tool_call
from app.tools_fleet import FLEET_TOOL_DECLARATIONS

ALL_TOOL_DECLARATIONS = MODULE2_TOOL_DECLARATIONS + FLEET_TOOL_DECLARATIONS


logger = logging.getLogger(__name__)

_prefix_text: str = ""
_prefix_token_count: int = 0
_model_cache: dict[str, GenerativeModel] = {}


def _build_base_prefix() -> str:
    sections = [
        STATIC_SYSTEM_PROMPT,
        OPERATIONAL_RULES,
        MODULE_1_SCHEMA,
        MODULE_2_SCHEMA,
        TOOL_DECLARATIONS_TEXT,
        RBAC_TIER_DESCRIPTIONS,
        FEW_SHOT_EXAMPLES,
    ]
    return "\n".join(sections)


def count_tokens_text(text: str) -> int:
    """Count tokens via Vertex AI (fallback to char/4 estimate on failure)."""
    if not text:
        return 0
    try:
        _ensure_vertex_init()
        model = GenerativeModel(GEMINI_MODEL)
        result = model.count_tokens(text)
        count = result.total_tokens
        return int(count)
    except Exception as exc:
        estimate = max(1, len(text) // 4)
        logger.warning("count_tokens_fallback error=%s estimate=%s", exc, estimate)
        return estimate


def _ensure_vertex_init() -> None:
    if PROJECT_ID:
        vertexai.init(project=PROJECT_ID, location=VERTEX_LOCATION)


def initialize_prefix_at_startup() -> tuple[str, int]:
    """Build prefix, pad with CACHE_PADDING_BLOCK if under 4,096 tokens (SCHEMA §7)."""
    global _prefix_text, _prefix_token_count

    _ensure_vertex_init()
    base = _build_base_prefix()
    token_count = count_tokens_text(base)

    if token_count < MIN_PREFIX_TOKENS:
        padded = base + "\n" + CACHE_PADDING_BLOCK
        padded_count = count_tokens_text(padded)
        logger.info(
            "prefix_padding_applied base_tokens=%s padded_tokens=%s floor=%s",
            token_count,
            padded_count,
            MIN_PREFIX_TOKENS,
        )
        if padded_count < MIN_PREFIX_TOKENS:
            # Append repeated padding marker until floor met (edge case)
            extra = CACHE_PADDING_BLOCK * (
                2 + (MIN_PREFIX_TOKENS - padded_count) // 500
            )
            padded = padded + "\n" + extra
            padded_count = count_tokens_text(padded)
            logger.warning(
                "prefix_extra_padding_applied final_tokens=%s",
                padded_count,
            )
        _prefix_text = padded
        _prefix_token_count = padded_count
    else:
        _prefix_text = base
        _prefix_token_count = token_count
        logger.info("prefix_no_padding_needed tokens=%s", token_count)

    if _prefix_token_count < MIN_PREFIX_TOKENS:
        logger.error(
            "prefix_below_cache_floor tokens=%s required=%s — caching may be disabled",
            _prefix_token_count,
            MIN_PREFIX_TOKENS,
        )
    else:
        logger.info("prefix_cache_floor_met prefix_token_count=%s", _prefix_token_count)

    return _prefix_text, _prefix_token_count


def get_prefix_text() -> str:
    return _prefix_text or _build_base_prefix()


def get_prefix_token_count() -> int:
    return _prefix_token_count


def _get_model(tier: str) -> GenerativeModel:
    if tier in _model_cache:
        return _model_cache[tier]

    _ensure_vertex_init()
    declarations = [
        FunctionDeclaration(**decl)
        for decl in ALL_TOOL_DECLARATIONS
        if _tool_allowed(decl["name"], tier)
    ]
    tools = [Tool(function_declarations=declarations)] if declarations else []
    model = GenerativeModel(
        GEMINI_MODEL,
        system_instruction=[get_prefix_text()],
        tools=tools,
    )
    _model_cache[tier] = model
    logger.info(
        "generative_model_cached tier=%s tool_count=%s model=%s",
        tier,
        len(declarations),
        GEMINI_MODEL,
    )
    return model


def _tool_allowed(name: str, tier: str) -> bool:
    if tier == "tier1":
        return name in (
            "list_tasks",
            "update_task_status",
            "create_adhoc_task",
            "get_current_weather",
            "create_weather_tasks",
            "get_schedule",
            "manage_outing",
            "get_calendar_events",
            "register_calendar_url",
        )
    return name in (
        "list_tasks",
        "update_task_status",
        "get_schedule",
        "update_driver_availability",
    )


def inbound_to_parts(inbound: InboundMessage) -> list[Part]:
    """Map content[] blocks to Gemini Parts (SCHEMA §3)."""
    parts: list[Part] = []
    for block in inbound.content:
        if block.block_type == "text":
            parts.append(Part.from_text(block.text))
        elif block.block_type == "media":
            if not block.gcs_uri or not block.normalized_mime_type:
                logger.warning(
                    "skipping_media_part_missing_gcs message_id=%s media_id=%s",
                    inbound.message_id,
                    block.media_id,
                )
                continue
            parts.append(
                Part.from_uri(
                    uri=block.gcs_uri,
                    mime_type=block.normalized_mime_type,
                )
            )
    return parts


def build_suffix(
    session_context: str,
    history_text: str,
    inbound: InboundMessage,
) -> str:
    """Assemble suffix zone with SESSION CONTEXT delimiter (SCHEMA §7)."""
    user_message_lines: list[str] = []
    for block in inbound.content:
        if block.block_type == "text":
            user_message_lines.append(block.text)
        elif block.block_type == "media":
            user_message_lines.append(
                f"[media: {block.normalized_mime_type} @ {block.gcs_uri}]"
            )

    suffix_parts = [
        "--- SESSION CONTEXT ---",
        session_context,
        "--- CONVERSATION HISTORY ---",
        history_text or "(no prior turns)",
        "--- CURRENT USER MESSAGE ---",
        "\n".join(user_message_lines) or "(empty)",
    ]
    return "\n".join(suffix_parts)


def run_agent_turn(
    tier: str,
    member_id: str,
    phone_e164: str,
    session_context: str,
    history_text: str,
    inbound: InboundMessage,
    db: Any,
) -> tuple[str, dict[str, Any]]:
    """
    Execute Gemini with prefix/suffix assembly and tool loop (Module 2 only).
    Returns (reply_text, usage_metadata).
    """
    model = _get_model(tier)
    suffix = build_suffix(session_context, history_text, inbound)
    user_parts = inbound_to_parts(inbound)

    combined_parts: list[Part] = [Part.from_text(suffix)]
    combined_parts.extend(user_parts)
    contents: list[Content] = [Content(role="user", parts=combined_parts)]

    usage: dict[str, Any] = {}
    max_tool_rounds = 5

    for round_idx in range(max_tool_rounds):
        logger.info(
            "gemini_generate_start phone=%s round=%s model=%s",
            phone_e164,
            round_idx,
            GEMINI_MODEL,
        )
        response = model.generate_content(
            contents,
            generation_config=GenerationConfig(temperature=0.2, max_output_tokens=2048),
        )

        if hasattr(response, "usage_metadata") and response.usage_metadata:
            usage = {
                "prompt_token_count": getattr(
                    response.usage_metadata, "prompt_token_count", None
                ),
                "candidates_token_count": getattr(
                    response.usage_metadata, "candidates_token_count", None
                ),
                "cached_content_token_count": getattr(
                    response.usage_metadata, "cached_content_token_count", None
                ),
            }
            logger.info(
                "gemini_usage phone=%s round=%s usage=%s",
                phone_e164,
                round_idx,
                json.dumps(usage),
            )

        candidate = response.candidates[0] if response.candidates else None
        if not candidate or not candidate.content:
            return "I could not process that request. Please try again.", usage

        parts = candidate.content.parts
        function_calls = [p for p in parts if p.function_call and p.function_call.name]

        if not function_calls:
            text_parts = [p.text for p in parts if p.text]
            reply = "\n".join(text_parts).strip() or "Done."
            return reply, usage

        # Tool execution
        model_content = candidate.content
        contents.append(model_content)
        tool_response_parts: list[Part] = []

        for fc_part in function_calls:
            fc = fc_part.function_call
            name = fc.name
            args = dict(fc.args) if fc.args else {}
            logger.info(
                "gemini_tool_call phone=%s tool=%s args=%s",
                phone_e164,
                name,
                json.dumps(args, default=str),
            )
            result = execute_tool_call(
                db=db,
                tool_name=name,
                args=args,
                caller_member_id=member_id,
                caller_tier=tier,
                phone_e164=phone_e164,
            )
            tool_response_parts.append(
                Part.from_function_response(name=name, response=result)
            )

        contents.append(Content(role="user", parts=tool_response_parts))

    return (
        "I need more steps to complete that. Please try again with a simpler request.",
        usage,
    )
