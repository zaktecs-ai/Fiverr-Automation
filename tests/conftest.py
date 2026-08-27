"""Shared pytest fixtures."""
import os
import sys
from pathlib import Path

# Ensure the project root is importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture
def sample_record():
    """A representative maps record before enrichment."""
    return {
        "business_name": "Smile Dental",
        "category": "Dentist",
        "phone": "(214) 555-1234",
        "website": "https://www.smiledental.com/?utm_source=test",
        "full_address": "123 Main St, Dallas, TX 75201",
        "city": "Dallas",
        "state": "TX",
        "postal_code": "75201",
        "country": "US",
        "rating": 4.7,
        "review_count": 120,
        "place_id": "0x0000000000000000:0x1234567890abcdef",
        "source_query": "dentists in Dallas, TX",
        "source_keyword": "dentists",
        "source_location": "Dallas, TX",
    }
