"""Application configuration from environment variables."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

# Timezone per SCHEMA §1
RIYADH_TZ = ZoneInfo("Asia/Riyadh")

# Implicit cache floor (§7)
MIN_PREFIX_TOKENS = 4096
MAX_SUFFIX_HISTORY_TOKENS = 3000
HISTORY_QUERY_LIMIT = 20

# Confirmation TTL (§9.3)
CONFIRMATION_TTL_MINUTES = 30

# Idempotency window (§1)
IDEMPOTENCY_TTL_HOURS = 24

# Media limits (§9.1)
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_AUDIO_DURATION_SEC = 300

# GCP
PROJECT_ID = os.environ.get(
    "GCP_PROJECT_ID", os.environ.get("GOOGLE_CLOUD_PROJECT", "")
)
REGION = os.environ.get("GCP_REGION", "me-central1")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")

# Vertex AI
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", REGION)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Telegram Bot API
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_OPS_BOT_TOKEN = os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "")


def _get_bot_user_id(token: str) -> int | None:
    if not token or ":" not in token:
        return None
    try:
        return int(token.split(":")[0])
    except ValueError:
        return None


MAIN_BOT_USER_ID = _get_bot_user_id(TELEGRAM_BOT_TOKEN)
OPS_BOT_USER_ID = _get_bot_user_id(TELEGRAM_OPS_BOT_TOKEN)


# Cloud Tasks
TASKS_LOCATION = os.environ.get("TASKS_LOCATION", REGION)
INBOUND_QUEUE = os.environ.get("INBOUND_QUEUE", "inbound-message-processing")
SERVICE_URL = os.environ.get(
    "SERVICE_URL", ""
)  # Cloud Run service URL for task targets
TASKS_SERVICE_ACCOUNT = os.environ.get("TASKS_SERVICE_ACCOUNT", "")

# Phase 1 active module
ACTIVE_MODULE = "property_management"
