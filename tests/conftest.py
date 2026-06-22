"""Test fixtures. Sets an isolated temp DB BEFORE app modules import config."""
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="btt-test-"))
os.environ["BTT_DATA_DIR"] = str(_tmp)
os.environ["BTT_DB_PATH"] = str(_tmp / "test.sqlite")
os.environ["BTT_ENABLE_NETWORK"] = "0"
os.environ["BTT_NETWORK"] = "testnet"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@pytest.fixture()
def session() -> Session:
    """Fresh in-memory DB per test, for service/unit tests."""
    from app.db import Base
    from app import models  # noqa: F401  (register tables)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Maker()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def client():
    """Route-level client. Runs lifespan (init_db) against the temp file DB."""
    from app.main import app

    with TestClient(app) as c:
        yield c
