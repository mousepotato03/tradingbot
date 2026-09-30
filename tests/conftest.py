import pytest

from app.config import Settings
from app.storage import Store


@pytest.fixture
def store():
    value = Store("sqlite:///:memory:")
    value.initialize()
    yield value
    value.engine.dispose()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, mode="fixture", artifact_dir=tmp_path / "artifacts")
