import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable (core/, parsers/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')
