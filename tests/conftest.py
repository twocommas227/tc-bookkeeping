import pytest

from tc.db import connect, init_db
from tc.fixtures import load_golden


@pytest.fixture(autouse=True)
def _isolated_archive_dir(tmp_path, monkeypatch):
    """Imported originals are archived to TC_UPLOAD_DIR: never into the repo during tests."""
    monkeypatch.setenv("TC_UPLOAD_DIR", str(tmp_path / "uploads"))


@pytest.fixture
def empty_db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def golden_db():
    conn = connect(":memory:")
    init_db(conn)
    load_golden(conn)
    yield conn
    conn.close()
