import pytest

from tc.db import connect, init_db
from tc.fixtures import load_golden


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
