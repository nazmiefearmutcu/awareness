"""Unit tests for the Google Drive storage client module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from awareness.storage import gdrive


@pytest.fixture
def mock_settings(tmp_path: Path):
    with patch("awareness.storage.gdrive.get_settings") as mock_get:
        mock_set = MagicMock()
        mock_set.data_dir = tmp_path
        mock_get.return_value = mock_set
        yield mock_set


def test_gdrive_auth_lifecycle(mock_settings) -> None:
    # Initially not authorized
    assert not gdrive.is_authorized()
    assert gdrive.load_auth() is None

    # Save auth and verify authorized
    auth_data = {
        "client_id": "test_id",
        "client_secret": "test_secret",
        "refresh_token": "test_refresh",
        "access_token": "test_access",
    }
    gdrive.save_auth(auth_data)
    assert gdrive.is_authorized()

    loaded = gdrive.load_auth()
    assert loaded == auth_data


@patch("httpx.post")
def test_refresh_access_token(mock_post, mock_settings) -> None:
    auth_data = {
        "client_id": "test_id",
        "client_secret": "test_secret",
        "refresh_token": "test_refresh",
    }
    gdrive.save_auth(auth_data)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"access_token": "new_access_token"}
    mock_post.return_value = mock_resp

    access_token = gdrive.refresh_access_token(auth_data)
    assert access_token == "new_access_token"
    assert mock_post.called

    # Check updated in storage
    loaded = gdrive.load_auth()
    assert loaded["access_token"] == "new_access_token"


@patch("httpx.get")
@patch("httpx.post")
def test_get_or_create_folder_finds_existing(mock_post, mock_get) -> None:
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {"files": [{"id": "folder_123", "name": "Awareness Captures"}]}
    mock_get.return_value = mock_get_resp

    folder_id = gdrive._get_or_create_folder("dummy_token")
    assert folder_id == "folder_123"
    assert not mock_post.called


@patch("httpx.get")
@patch("httpx.post")
def test_get_or_create_folder_creates_new(mock_post, mock_get) -> None:
    # 1st call: Search returns no files
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {"files": []}
    mock_get.return_value = mock_get_resp

    # 2nd call: Create returns new folder ID
    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = {"id": "new_folder_789"}
    mock_post.return_value = mock_post_resp

    folder_id = gdrive._get_or_create_folder("dummy_token")
    assert folder_id == "new_folder_789"
    assert mock_post.called


@patch("awareness.storage.gdrive.refresh_access_token")
@patch("awareness.storage.gdrive._get_or_create_folder")
@patch("httpx.post")
def test_upload_file(mock_post, mock_folder, mock_refresh, mock_settings, tmp_path: Path) -> None:
    # Save auth data
    gdrive.save_auth({
        "client_id": "test_id",
        "client_secret": "test_secret",
        "refresh_token": "test_refresh",
    })

    mock_refresh.return_value = "access_token_123"
    mock_folder.return_value = "folder_123"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "uploaded_file_456"}
    mock_post.return_value = mock_resp

    test_file = tmp_path / "chunk_123.jsonl"
    test_file.write_text('{"doc_id": "123"}', encoding="utf-8")

    file_id = gdrive.upload_file(test_file)
    assert file_id == "uploaded_file_456"
    assert mock_post.called
