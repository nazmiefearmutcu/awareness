"""Tests to verify PostgreSQL backend compatibility and SQLite fallback behavior of StateDB."""

from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from awareness.storage.state import StateDB, Base, JobRow, TaskRow
from awareness.schemas.jobs import JobState, JobKind, JobStatus, TaskState, TaskStatus
from awareness.schemas.doc import SourceKind


def test_sqlite_fallback_and_pragmas():
    # SQLite URL should set pragmas and connect args
    db = StateDB("sqlite:///:memory:")
    # Verify the internal url property has the sqlite prefix
    assert db.url.startswith("sqlite:")

    # Initialize tables
    db.init()

    # Verify basic connection works and tables are created
    with db.session() as s:
        # Check standard metadata works
        assert s.query(JobRow).count() == 0


def test_postgres_engine_creation_and_bypass():
    # Verify that postgresql+psycopg URLs are accepted and bypass SQLite PRAGMAs
    with patch("awareness.storage.state.create_engine") as mock_create_engine, \
         patch("awareness.storage.state.event.listens_for") as mock_listens_for:
        
        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"
        mock_create_engine.return_value = mock_engine

        db = StateDB("postgresql+psycopg://user:pass@localhost:5432/db")
        
        # Verify custom engine was created with pool tuning for Postgres
        # (W6C-bug2: pool_pre_ping + sane pool sizing; SQLite stays untouched)
        mock_create_engine.assert_called_once_with(
            "postgresql+psycopg://user:pass@localhost:5432/db",
            future=True,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
        # Verify event listener for SQLite connect pragmas was NOT registered
        mock_listens_for.assert_not_called()


def test_sqlite_pragmas_registration():
    # Verify that sqlite URLs register the connect pragmas
    with patch("awareness.storage.state.create_engine") as mock_create_engine, \
         patch("awareness.storage.state.event.listens_for") as mock_listens_for:
        
        mock_engine = MagicMock()
        mock_engine.dialect.name = "sqlite"
        mock_create_engine.return_value = mock_engine

        db = StateDB("sqlite:///:memory:")
        
        # Verify engine was created with connect_args for SQLite
        mock_create_engine.assert_called_once_with(
            "sqlite:///:memory:",
            future=True,
            connect_args={"timeout": 30, "check_same_thread": False}
        )
        # Verify event listener for SQLite connect was registered
        mock_listens_for.assert_called_once_with(mock_engine, "connect")


def test_claim_pending_tasks_with_skip_locked():
    # On non-sqlite databases, claim_pending_tasks must use with_for_update(skip_locked=True)
    with patch("awareness.storage.state.create_engine") as mock_create_engine, \
         patch("awareness.storage.state.event.listens_for") as mock_listens_for:
        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"
        mock_create_engine.return_value = mock_engine

        db = StateDB("postgresql+psycopg://user:pass@localhost:5432/db")
        
        mock_session = MagicMock()
        db._sessionmaker = MagicMock(return_value=mock_session)
        db._initialized = True

        # Mock the query builder chain
        mock_query = MagicMock()
        mock_query.where.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.with_for_update.return_value = mock_query
        mock_query.limit.return_value = mock_query

        with patch("awareness.storage.state.select", return_value=mock_query):
            db._do_claim_pending_tasks("job_123", 10)
            
            # Verify with_for_update(skip_locked=True) was called
            mock_query.with_for_update.assert_called_once_with(skip_locked=True)


def test_claim_pending_tasks_without_skip_locked_on_sqlite():
    # On SQLite databases, claim_pending_tasks must NOT use with_for_update(skip_locked=True)
    with patch("awareness.storage.state.create_engine") as mock_create_engine, \
         patch("awareness.storage.state.event.listens_for") as mock_listens_for:
        mock_engine = MagicMock()
        mock_engine.dialect.name = "sqlite"
        mock_create_engine.return_value = mock_engine

        db = StateDB("sqlite:///:memory:")
        
        mock_session = MagicMock()
        db._sessionmaker = MagicMock(return_value=mock_session)
        db._initialized = True

        # Mock the query builder chain
        mock_query = MagicMock()
        mock_query.where.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.with_for_update.return_value = mock_query
        mock_query.limit.return_value = mock_query

        with patch("awareness.storage.state.select", return_value=mock_query):
            db._do_claim_pending_tasks("job_123", 10)
            
            # Verify with_for_update was NOT called (since SQLite doesn't support skip locked)
            mock_query.with_for_update.assert_not_called()
