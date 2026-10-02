"""Tests for core.utils helpers."""

import os

from core.utils import (
    format_bytes, format_count, format_eta, format_ratio, plural,
    safe_filename, extract_urls, looks_like_url, sanitize_runtime_env,
)


class TestFormatEta:
    def test_seconds(self):
        assert format_eta(0) == "0s"
        assert format_eta(45) == "45s"

    def test_minutes(self):
        assert format_eta(60) == "1m"
        assert format_eta(200) == "3m 20s"

    def test_hours(self):
        assert format_eta(3600) == "1h"
        assert format_eta(3720) == "1h 2m"

    def test_negative_clamped(self):
        assert format_eta(-5) == "0s"


class TestNumberPresentation:
    def test_counts_and_ratios_group_thousands(self):
        assert format_count(51399) == "51,399"
        assert format_ratio(4, 20) == "4/20"
        assert format_ratio(1204, 51399) == "1,204/51,399"
        assert plural(1, "novel") == "1 novel"
        assert plural(3, "novel") == "3 novels"
        assert plural(0, "chapter") == "0 chapters"

    def test_bytes(self):
        assert format_bytes(500) == "500 B"
        assert format_bytes(2048) == "2 KB"
        assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
        assert format_bytes(20 * 1024 * 1024) == "20 MB"
        assert format_bytes(3 * 1024 ** 3) == "3.00 GB"


class TestSafeFilename:
    def test_keeps_full_english_title(self):
        assert safe_filename("The Legendary Mechanic") == "The Legendary Mechanic"

    def test_strips_illegal_chars(self):
        assert ":" not in safe_filename('Foo: Bar/Baz?')
        assert safe_filename('Foo: Bar') == "Foo Bar"

    def test_empty_fallback(self):
        assert safe_filename("") == "novel"
        assert safe_filename("???") == "novel"

    def test_truncates_long(self):
        long = "A" * 200
        assert len(safe_filename(long, max_length=50)) <= 50


class TestExtractUrls:
    def test_block_of_urls(self):
        text = """
        https://twkan.com/book/1.html
        https://69shuba.com/book/2/
        not a url
        https://twkan.com/book/1.html
        """
        urls = extract_urls(text)
        assert urls == [
            "https://twkan.com/book/1.html",
            "https://69shuba.com/book/2/",
        ]

    def test_strips_trailing_punctuation(self):
        assert extract_urls("see https://example.com/book/1.html.") == [
            "https://example.com/book/1.html"
        ]

    def test_looks_like_url(self):
        assert looks_like_url("https://twkan.com/book/1.html")
        assert looks_like_url("https://a.com/1\nhttps://b.com/2")
        assert not looks_like_url("just some text without links")


class TestSanitizeRuntimeEnv:
    def test_clears_missing_ssl_cert_file(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", r"C:\Users\x\AppData\Local\Temp\_MEI123\certifi\cacert.pem")
        monkeypatch.setenv("CURL_CA_BUNDLE", r"C:\Users\x\AppData\Local\Temp\_MEI123\certifi\cacert.pem")
        cleared = sanitize_runtime_env()
        assert "SSL_CERT_FILE" in cleared
        assert "CURL_CA_BUNDLE" in cleared
        assert "SSL_CERT_FILE" not in os.environ
        assert "CURL_CA_BUNDLE" not in os.environ

    def test_keeps_existing_cert_file(self, monkeypatch, tmp_path):
        cert = tmp_path / "cacert.pem"
        cert.write_text("x")
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        cleared = sanitize_runtime_env()
        assert "SSL_CERT_FILE" not in cleared
        assert os.environ.get("SSL_CERT_FILE") == str(cert)
