import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixture_html():
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return load


@pytest.fixture
def source():
    from news_qa.sources import get_source

    return get_source("wral")


@pytest.fixture
def conn(tmp_path):
    """A migrated, seeded database isolated to one test."""
    from news_qa import db

    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()
