"""Pydantic models for uniform InboundMessage envelope (SCHEMA §3)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


class ContentBlockType(str, Enum):
    TEXT = "text"
    MEDIA = "media"


class TextBlock(BaseModel):
    block_type: Literal["text"] = "text"
    text: str


class MediaBlock(BaseModel):
    block_type: Literal["media"] = "media"
    media_id: str
    mime_type: str
    gcs_uri: Union[str, None] = None
    normalized_mime_type: Union[str, None] = None


ContentBlock = Union[TextBlock, MediaBlock]


class InboundMessage(BaseModel):
    message_id: str
    phone_e164: str
    member_id: str
    received_at: datetime
    content: list[Union[TextBlock, MediaBlock]] = Field(default_factory=list)

    def model_dump_firestore(self) -> dict[str, Any]:
        """Serialize for Cloud Tasks JSON payload."""
        return self.model_dump(mode="json")


class Member(BaseModel):
    member_id: str
    phone_e164: str
    name: str
    role: Literal["tier1", "tier2"]
    capabilities: list[str] = Field(default_factory=list)
    active: bool = True
    preferred_language: str = "en"
    telegram_chat_id: Union[int, None] = None


class PendingConfirmation(BaseModel):
    confirmation_id: str
    action: str
    payload: dict[str, Any]
    summary: str
    status: Literal["active", "paused", "expired"] = "active"
    created_at: datetime
    expires_at: datetime


class PausedConfirmation(BaseModel):
    confirmation_id: str
    action: str
    payload: dict[str, Any]
    summary: str
    paused_at: datetime
    pause_reason: str
