from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from application import gopay_payment_state as state
from core.db import AccountModel, TaskModel, create_configured_engine


def _isolated_state(monkeypatch, tmp_path):
    engine = create_configured_engine(
        f"sqlite:///{tmp_path / 'state.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    AccountModel.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(state, "engine", engine)
    monkeypatch.setattr(state, "_TABLES_READY", False)
    return engine


def test_existing_payment_table_adds_proxy_column(monkeypatch, tmp_path):
    engine = create_configured_engine(
        f"sqlite:///{tmp_path / 'migration.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    AccountModel.__table__.create(engine, checkfirst=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE gopay_payment_attempts (key TEXT PRIMARY KEY)"
        )
    monkeypatch.setattr(state, "engine", engine)
    monkeypatch.setattr(state, "_TABLES_READY", False)
    state._ensure_tables()
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(gopay_payment_attempts)"
            ).all()
        }
    assert "proxy" in columns


def test_terminal_owner_releases_preparing_attempt(monkeypatch, tmp_path):
    engine = _isolated_state(monkeypatch, tmp_path)
    TaskModel.__table__.create(engine, checkfirst=True)
    with Session(engine) as session:
        session.add(
            TaskModel(
                id="task-a",
                type="gopay_pay_chatgpt",
                platform="chatgpt",
                status="failed",
            )
        )
        session.commit()
    key = "chatgpt:43"
    state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=43,
        task_id="task-a",
        proxy="http://proxy-one",
    )
    state.update_payment_attempt(key, task_id="task-a", status="preparing")

    claimed = state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=43,
        task_id="task-b",
        proxy="http://proxy-one",
    )

    assert claimed["action"] == "start"
    assert claimed["status"] == "claimed"
    persisted = state.get_payment_attempt(key)
    assert persisted["task_id"] == "task-b"
    assert persisted["status"] == "claimed"
    assert persisted["gopay_account_id"] == 0


def test_payment_attempt_claim_is_atomic(monkeypatch, tmp_path):
    _isolated_state(monkeypatch, tmp_path)

    def claim(task):
        return state.claim_payment_attempt(
            key="chatgpt:42",
            chatgpt_account_id=42,
            task_id=task,
            proxy="http://proxy-one",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("task-a", "task-b")))
    assert sorted(item["action"] for item in results) == ["busy", "start"]


def test_uncertain_attempt_only_reconciles_original_snap(monkeypatch, tmp_path):
    engine = _isolated_state(monkeypatch, tmp_path)
    key = "chatgpt:7"
    state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=7,
        task_id="task-a",
        proxy="http://proxy-one",
    )
    url = "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111"
    state.update_payment_attempt(
        key,
        task_id="task-a",
        status="uncertain",
        midtrans_url=url,
        snap_id=state.extract_snap_id(url),
        uncertain=True,
    )
    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE gopay_payment_attempts SET lease_expires_at = ? WHERE key = ?",
            (expired, key),
        )

    claimed = state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=7,
        task_id="task-b",
        proxy="http://proxy-one",
    )
    assert claimed["action"] == "reconcile"
    assert claimed["midtrans_url"] == url
    assert claimed["snap_id"] == "11111111-1111-1111-1111-111111111111"


def test_payment_attempt_rejects_proxy_change(monkeypatch, tmp_path):
    _isolated_state(monkeypatch, tmp_path)
    state.claim_payment_attempt(
        key="chatgpt:8",
        chatgpt_account_id=8,
        task_id="task-a",
        proxy="http://proxy-one",
    )
    result = state.claim_payment_attempt(
        key="chatgpt:8",
        chatgpt_account_id=8,
        task_id="task-a",
        proxy="http://proxy-two",
    )
    assert result["action"] == "proxy_mismatch"


def test_failed_precharge_attempt_allows_new_fixed_proxy(monkeypatch, tmp_path):
    _isolated_state(monkeypatch, tmp_path)
    key = "chatgpt:81"
    state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=81,
        task_id="task-a",
        proxy="http://proxy-one",
    )
    state.update_payment_attempt(
        key,
        task_id="task-a",
        status="failed_precharge",
        uncertain=False,
    )

    result = state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=81,
        task_id="task-b",
        proxy="http://proxy-two",
    )

    assert result["action"] == "start"
    assert result["status"] == "claimed"
    assert result["proxy"] == "http://proxy-two"


def test_settled_attempt_resumes_without_new_checkout(monkeypatch, tmp_path):
    engine = _isolated_state(monkeypatch, tmp_path)
    key = "chatgpt:9"
    state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=9,
        task_id="task-a",
        proxy="http://proxy-one",
    )
    state.update_payment_attempt(key, task_id="task-a", status="settled", uncertain=False)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE gopay_payment_attempts SET lease_expires_at = ? WHERE key = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), key),
        )
    result = state.claim_payment_attempt(
        key=key,
        chatgpt_account_id=9,
        task_id="task-b",
        proxy="http://proxy-one",
    )
    assert result["action"] == "reconcile"
    assert result["status"] == "settled"


def test_gopay_account_lease_is_exclusive(monkeypatch, tmp_path):
    engine = _isolated_state(monkeypatch, tmp_path)
    with Session(engine) as session:
        session.add(AccountModel(platform="gopay", email="+620000", password="123456"))
        session.commit()

    def acquire(owner):
        return state.acquire_gopay_lease(account_id=1, owner_key=owner, task_id=owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(acquire, ("owner-a", "owner-b")))
    assert sorted(outcomes) == [False, True]
