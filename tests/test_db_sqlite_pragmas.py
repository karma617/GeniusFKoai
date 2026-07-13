from pathlib import Path

from core.db import SQLITE_BUSY_TIMEOUT_MS, create_configured_engine


def test_sqlite_engine_uses_wal_pragmas(tmp_path):
    db_path = Path(tmp_path) / "wal-test.db"
    engine = create_configured_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    try:
        with engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
            synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()
            busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
            temp_store = connection.exec_driver_sql("PRAGMA temp_store").scalar()
    finally:
        engine.dispose()

    assert str(journal_mode).lower() == "wal"
    assert int(synchronous) == 1
    assert int(busy_timeout) == SQLITE_BUSY_TIMEOUT_MS
    assert int(temp_store) == 2
