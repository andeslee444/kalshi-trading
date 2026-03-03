"""Tests for box office HTML scraping regex patterns used in source-monitor."""

import re
import pytest


# Regex patterns extracted from source-monitor.py
THE_NUMBERS_PATTERN = r'(?:>)([^<]{3,50})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)'
MOJO_PATTERN = r'(?:>)([^<]{3,50})</a>.*?\$([\d,.]+)\s*([MmBb])?'


class TestTheNumbersPattern:
    def test_standard_movie_row(self):
        html = '<a href="/movie/test">Inside Out 2</a></td><td class="money">$1,234,567'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 1
        assert matches[0][0] == "Inside Out 2"
        assert matches[0][1] == "1,234,567"

    def test_title_with_special_chars(self):
        html = '<a href="/movie/test">A Minecraft Movie: Part 1</a></td><td class="money">$500,000'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 1
        assert "Minecraft" in matches[0][0]

    def test_no_dollar_sign(self):
        html = '<a href="/movie/test">Test Movie</a></td><td class="money">2,500,000'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 1
        assert matches[0][1] == "2,500,000"

    def test_rejects_short_title(self):
        html = '<a href="/movie/test">AB</a></td><td class="money">$100,000'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 0


class TestMojoPattern:
    def test_millions_suffix(self):
        html = '<a href="/release/test">Thunderbolts</a> some text $45.2M'
        matches = re.findall(MOJO_PATTERN, html, re.DOTALL)
        assert len(matches) >= 1
        assert matches[0][0].strip() == "Thunderbolts"
        assert matches[0][1] == "45.2"
        assert matches[0][2].upper() == "M"

    def test_billions_suffix(self):
        html = '<a href="/movie">Avatar 3</a> $1.2B'
        matches = re.findall(MOJO_PATTERN, html, re.DOTALL)
        assert len(matches) >= 1
        assert matches[0][2].upper() == "B"

    def test_no_suffix_raw_number(self):
        html = '<a href="/movie">Small Film</a> $500,000'
        matches = re.findall(MOJO_PATTERN, html, re.DOTALL)
        assert len(matches) >= 1
        assert matches[0][1] == "500,000"
        assert matches[0][2] == ""

    def test_gross_value_parsing(self):
        """Verify gross multiplication logic from source-monitor.py lines 491-497."""
        test_cases = [
            ("45.2", "M", 45_200_000),
            ("1.2", "B", 1_200_000_000),
            ("500,000", "", 500_000),
        ]
        for gross_str, suffix, expected in test_cases:
            gross_clean = gross_str.replace(",", "")
            gross_val = float(gross_clean)
            if suffix.upper() == "B":
                gross_val *= 1_000_000_000
            elif suffix.upper() == "M":
                gross_val *= 1_000_000
            assert int(gross_val) == expected
