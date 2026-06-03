"""Unit tests for app/idempotency.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock

import pytest
from google.api_core.exceptions import AlreadyExists

from app.config import IDEMPOTENCY_TTL_HOURS
from app.idempotency import claim_idempotency_key, release_idempotency_key


def test_claim_idempotency_key_success(mock_firestore_client):
    """Test successful idempotency key claim."""
    message_id = "wamid.test123"
    now = datetime.now()
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    mock_ref.create.return_value = None
    
    result = claim_idempotency_key(mock_firestore_client, message_id, now)
    
    assert result is True
    mock_ref.create.assert_called_once()
    call_args = mock_ref.create.call_args[0][0]
    assert call_args["message_id"] == message_id
    assert call_args["received_at"] == now
    assert call_args["expires_at"] == now + timedelta(hours=IDEMPOTENCY_TTL_HOURS)


def test_claim_idempotency_key_duplicate(mock_firestore_client):
    """Test duplicate idempotency key is rejected."""
    message_id = "wamid.duplicate"
    now = datetime.now()
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    mock_ref.create.side_effect = AlreadyExists("Document already exists")
    
    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "message_id": message_id,
        "received_at": now,
        "expires_at": now + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
    }
    mock_ref.get.return_value = mock_doc
    
    result = claim_idempotency_key(mock_firestore_client, message_id, now)
    
    assert result is False
    mock_ref.create.assert_called_once()
    mock_ref.get.assert_called_once()


def test_claim_idempotency_key_stale_overwrite(mock_firestore_client):
    """Test stale idempotency key is overwritten."""
    message_id = "wamid.stale"
    now = datetime.now()
    past = now - timedelta(hours=25)
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    mock_ref.create.side_effect = AlreadyExists("Document already exists")
    
    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "message_id": message_id,
        "received_at": past,
        "expires_at": past + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
    }
    mock_ref.get.return_value = mock_doc
    
    result = claim_idempotency_key(mock_firestore_client, message_id, now)
    
    assert result is True
    mock_ref.create.assert_called_once()
    mock_ref.get.assert_called_once()
    mock_ref.set.assert_called_once()


def test_claim_idempotency_key_missing_expires_at(mock_firestore_client):
    """Test handling of idempotency key missing expires_at field."""
    message_id = "wamid.no_expires"
    now = datetime.now()
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    mock_ref.create.side_effect = AlreadyExists("Document already exists")
    
    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "message_id": message_id,
        "received_at": now,
        "expires_at": None,
    }
    mock_ref.get.return_value = mock_doc
    
    result = claim_idempotency_key(mock_firestore_client, message_id, now)
    
    assert result is True
    mock_ref.set.assert_called_once()


def test_claim_idempotency_key_doc_deleted_race(mock_firestore_client):
    """Test handling of race condition where doc is deleted between create and get."""
    message_id = "wamid.race"
    now = datetime.now()
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    mock_ref.create.side_effect = AlreadyExists("Document already exists")
    
    mock_doc = Mock()
    mock_doc.exists = False
    mock_ref.get.return_value = mock_doc
    
    result = claim_idempotency_key(mock_firestore_client, message_id, now)
    
    assert result is True
    mock_ref.set.assert_called_once()


def test_claim_idempotency_key_timezone_normalization(mock_firestore_client):
    """Test timezone normalization for expires_at comparison."""
    message_id = "wamid.tz_test"
    now = datetime.now()
    past = now - timedelta(hours=25)
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    mock_ref.create.side_effect = AlreadyExists("Document already exists")
    
    from datetime import timezone
    naive_past = past.replace(tzinfo=None)
    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "message_id": message_id,
        "received_at": naive_past,
        "expires_at": naive_past + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
    }
    mock_ref.get.return_value = mock_doc
    
    result = claim_idempotency_key(mock_firestore_client, message_id, now)
    
    assert result is True
    mock_ref.set.assert_called_once()


def test_claim_idempotency_key_collection_name(mock_firestore_client):
    """Test correct collection name is used."""
    message_id = "wamid.collection_test"
    now = datetime.now()
    
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    mock_ref.create.return_value = None
    
    claim_idempotency_key(mock_firestore_client, message_id, now)
    
    mock_firestore_client.collection.assert_called_once_with("webhook_idempotency")
    mock_firestore_client.collection.return_value.document.assert_called_once_with(message_id)


def test_release_idempotency_key(mock_firestore_client):
    """Test releasing/deleting an idempotency key."""
    message_id = "wamid.release"
    mock_ref = MagicMock()
    mock_firestore_client.collection.return_value.document.return_value = mock_ref
    
    release_idempotency_key(mock_firestore_client, message_id)
    
    mock_firestore_client.collection.assert_called_with("webhook_idempotency")
    mock_firestore_client.collection.return_value.document.assert_called_with(message_id)
    mock_ref.delete.assert_called_once()
