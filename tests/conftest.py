"""conftest.py — pytest fixtures for the news pipeline unit tests.

The pipeline modules read required environment variables and build AWS
clients at import time, so every test session runs with stubbed env vars
and stubbed boto3 clients. No AWS access, no network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the project root importable regardless of where pytest is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Minimum env required by lambda_handler at import time (values are never
# used by the pure functions under test).
os.environ.setdefault("CONFIG_BUCKET", "test-config-bucket")
os.environ.setdefault("ARTICLES_TABLE", "test-articles")
os.environ.setdefault("REPORTS_TABLE", "test-reports")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")


@pytest.fixture
def filters() -> dict:
    """Filter config with both include and exclude keywords."""
    return {
        "include_keywords": ["ai", "machine learning"],
        "exclude_keywords": ["casino", "giveaway"],
    }