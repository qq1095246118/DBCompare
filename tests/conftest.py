import json
from pathlib import Path

import pytest


@pytest.fixture
def db_rows() -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "db_rows.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))
