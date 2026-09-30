from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.storage import Base, Store


def test_fresh_migration_matches_model_schema_and_is_repeatable(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "migrations.db").as_posix()
    monkeypatch.setenv("TRADINGBOT_DATABASE_URL", url)
    config = Config(str(Path("alembic.ini").resolve()))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    store = Store(url)
    assert set(inspect(store.engine).get_table_names()) == set(Base.metadata.tables) | {
        "alembic_version"
    }
    command.check(config)


def test_fixture_database_from_before_0002_gains_new_schema(tmp_path):
    from sqlalchemy import create_engine, text

    url = "sqlite:///" + (tmp_path / "old.db").as_posix()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE outcomes (id VARCHAR(80) PRIMARY KEY, report_id VARCHAR(36), body JSON)"
            )
        )
    engine.dispose()
    store = Store(url)
    store.initialize()
    columns = {c["name"] for c in inspect(store.engine).get_columns("outcomes")}
    assert "mature" in columns
    assert "monitor_observations" in inspect(store.engine).get_table_names()
    store.initialize()  # idempotent


def test_migration_0002_downgrades_and_upgrades(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "roundtrip.db").as_posix()
    monkeypatch.setenv("TRADINGBOT_DATABASE_URL", url)
    config = Config(str(Path("alembic.ini").resolve()))
    command.upgrade(config, "head")
    command.downgrade(config, "0001")
    store = Store(url)
    assert "monitor_observations" not in inspect(store.engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
