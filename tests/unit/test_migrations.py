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
