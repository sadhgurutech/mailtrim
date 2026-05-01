"""Shared pytest fixtures for the mailtrim test suite.

``clean_db``  — provides an isolated in-memory SQLite database for every test
that touches the storage layer.  Using in-memory SQLite means:

* No file-system side effects — nothing written to ~/.mailtrim
* Each test starts with an empty schema — zero shared state between tests
* No dependency on cfg.DB_PATH or MAILTRIM_DIR environment variables

Usage
-----
Add ``clean_db`` to any test function or class that calls
``get_session()``, ``BlocklistRepo``, ``EmailRepo``, etc.

To apply automatically to all tests in a module, add an autouse fixture
that depends on ``clean_db``::

    @pytest.fixture(autouse=True)
    def _use_clean_db(clean_db):
        pass
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def clean_db(monkeypatch):
    """Inject a fresh in-memory SQLite engine into the storage module.

    Resets ``_engine`` and ``_SessionLocal`` to a brand-new in-memory
    database before the test and restores the originals (via monkeypatch)
    after it completes — regardless of whether the test passes or fails.
    """
    import mailtrim.core.storage as storage

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    storage.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr(storage, "_engine", engine)
    monkeypatch.setattr(storage, "_SessionLocal", session_factory)

    yield engine

    engine.dispose()
