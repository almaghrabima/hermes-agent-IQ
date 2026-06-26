"""Fixtures shared across kanban tests."""
import pytest
from hermes_cli import kanban_db


@pytest.fixture
def kanban_conn(tmp_path):
    """Yield a fresh kanban DB connection backed by a per-test temp file.

    The global ``_hermetic_environment`` autouse fixture (tests/conftest.py)
    already redirects HERMES_HOME, but we use an explicit ``db_path`` here so
    the module-level ``_INITIALIZED_PATHS`` cache never collides between tests
    that run in the same process.
    """
    db_file = tmp_path / "kanban_test.db"
    conn = kanban_db.connect(db_path=db_file)
    try:
        yield conn
    finally:
        conn.close()
