"""Google Cloud Tasks enqueue for inbound processing."""

from __future__ import annotations

import json
import logging

from google.cloud import tasks_v2

from app.config import (
    INBOUND_QUEUE,
    PROJECT_ID,
    SERVICE_URL,
    TASKS_LOCATION,
    TASKS_SERVICE_ACCOUNT,
)
from app.models import InboundMessage

logger = logging.getLogger(__name__)

_client: tasks_v2.CloudTasksClient | None = None


def get_tasks_client() -> tasks_v2.CloudTasksClient:
    global _client
    if _client is None:
        _client = tasks_v2.CloudTasksClient()
        logger.info("cloud_tasks_client_initialized")
    return _client


def enqueue_inbound_processing(inbound: InboundMessage) -> str:
    """Enqueue InboundMessage to inbound-message-processing queue."""
    if not PROJECT_ID:
        raise RuntimeError("GCP_PROJECT_ID is not configured")
    if not SERVICE_URL:
        raise RuntimeError("SERVICE_URL is not configured for Cloud Tasks target")
    if not TASKS_SERVICE_ACCOUNT:
        raise RuntimeError("TASKS_SERVICE_ACCOUNT is not configured for Cloud Tasks OIDC")

    client = get_tasks_client()
    parent = client.queue_path(PROJECT_ID, TASKS_LOCATION, INBOUND_QUEUE)
    service_url = SERVICE_URL.rstrip("/")
    url = f"{service_url}/tasks/process-inbound"
    payload = json.dumps(inbound.model_dump_firestore()).encode("utf-8")

    task: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": payload,
            "oidc_token": {
                "service_account_email": TASKS_SERVICE_ACCOUNT,
                "audience": service_url,
            },
        }
    }
    logger.info(
        "inbound_task_oidc_configured service_account=%s audience=%s",
        TASKS_SERVICE_ACCOUNT,
        service_url,
    )

    response = client.create_task(request={"parent": parent, "task": task})
    logger.info(
        "inbound_task_enqueued message_id=%s task_name=%s queue=%s",
        inbound.message_id,
        response.name,
        INBOUND_QUEUE,
    )
    return response.name
