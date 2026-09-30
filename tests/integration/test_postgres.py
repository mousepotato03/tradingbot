import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.config import Settings
from app.engine import ResearchEngine
from app.models import ResearchRequest
from app.storage import Base, Store


def test_postgres_migration_transactions_queue_and_report(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an empty dedicated PostgreSQL test database")
    monkeypatch.setenv("TRADINGBOT_DATABASE_URL", url)
    config = Config(str(Path("alembic.ini").resolve()))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    store = Store(url)
    assert set(inspect(store.engine).get_table_names()) == set(Base.metadata.tables) | {
        "alembic_version"
    }
    command.check(config)
    settings = Settings(_env_file=None, database_url=url, mode="fixture")
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    assert store.claim_job(7200) == run_id
    assert store.claim_job(7200) is None
    report = ResearchEngine(settings, store).run(run_id)
    assert store.get_report(run_id) == report
    assert store.get_run(run_id)["status"] == "COMPLETED"
    assert store.previous("TEST").run_id == run_id
