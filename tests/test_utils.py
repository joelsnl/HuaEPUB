"""Tests for core.utils."""

from core.utils import format_eta


class TestFormatEta:
    def test_seconds(self):
        assert format_eta(0) == "0s"
        assert format_eta(45) == "45s"

    def test_minutes(self):
        assert format_eta(60) == "1m 0s"
        assert format_eta(200) == "3m 20s"

    def test_hours(self):
        assert format_eta(3600) == "1h 0m"
        assert format_eta(3720) == "1h 2m"

    def test_negative_clamped(self):
        assert format_eta(-5) == "0s"
