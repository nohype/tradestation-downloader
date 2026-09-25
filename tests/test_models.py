"""Tests for models module."""

import pytest

import tradestation.models
from tradestation.models import (
    DEFAULT_SYMBOLS,
    DownloadConfig,
    StorageFormat,
    get_all_symbols,
    get_symbols_by_categories,
    get_symbols_by_category,
)


class TestStorageFormat:
    """Tests for StorageFormat enum."""

    def test_values(self):
        assert StorageFormat.SINGLE.value == "single"
        assert StorageFormat.DAILY.value == "daily"
        assert StorageFormat.MONTHLY.value == "monthly"

    def test_from_string(self):
        assert StorageFormat.from_string("single") == StorageFormat.SINGLE
        assert StorageFormat.from_string("DAILY") == StorageFormat.DAILY
        assert StorageFormat.from_string("Monthly") == StorageFormat.MONTHLY

    def test_from_string_invalid(self):
        with pytest.raises(ValueError, match="Invalid storage format"):
            StorageFormat.from_string("invalid")


class TestDownloadConfig:
    """Tests for DownloadConfig dataclass."""

    def test_defaults(self):
        config = DownloadConfig(
            client_id="id",
            client_secret="secret",
            refresh_token="token",
        )
        assert config.data_dir == "./data"
        assert config.storage_format == StorageFormat.SINGLE
        assert config.interval == 1
        assert config.unit == "Minute"

    def test_storage_format_string_conversion(self):
        config = DownloadConfig(
            client_id="id",
            client_secret="secret",
            refresh_token="token",
            storage_format="monthly",
        )
        assert config.storage_format == StorageFormat.MONTHLY

    def test_categories(self):
        config_without = DownloadConfig(
            client_id="id",
            client_secret="secret",
            refresh_token="token",
        )
        assert config_without.categories == []

        config_with = DownloadConfig(
            client_id="id",
            client_secret="secret",
            refresh_token="token",
            categories=["index", "energy"],
        )
        assert config_with.categories == ["index", "energy"]


class TestSymbols:
    """Tests for symbol utilities."""

    def test_get_all_symbols(self):
        symbols = get_all_symbols()
        assert len(symbols) > 50
        assert "@ES" in symbols
        assert "@NQ" in symbols

    def test_get_symbols_by_category(self):
        index_symbols = get_symbols_by_category("index")
        assert "@ES" in index_symbols
        assert "@NQ" in index_symbols

    def test_get_symbols_by_category_invalid(self):
        with pytest.raises(ValueError, match="Unknown category"):
            get_symbols_by_category("invalid_category")

    def test_get_symbols_by_categories(self):
        symbols = get_symbols_by_categories(["index", "energy"])
        expected = DEFAULT_SYMBOLS["index"] + DEFAULT_SYMBOLS["energy"]
        assert symbols == expected
        assert len(symbols) == len(set(symbols))

    def test_get_symbols_by_categories_empty(self):
        assert get_symbols_by_categories([]) == []

    def test_get_symbols_by_categories_invalid(self):
        with pytest.raises(ValueError, match="Unknown category"):
            get_symbols_by_categories(["index", "invalid_category"])

    def test_get_symbols_by_categories_duplicate_categories(self):
        symbols = get_symbols_by_categories(["index", "index"])
        assert symbols == DEFAULT_SYMBOLS["index"]

    def test_get_symbols_by_categories_overlapping_symbols(self):
        symbols = get_symbols_by_categories(["index", "index", "energy", "index"])
        expected = DEFAULT_SYMBOLS["index"] + DEFAULT_SYMBOLS["energy"]
        assert symbols == expected
        assert len(symbols) == len(set(symbols))

    def test_default_symbols_categories(self):
        expected_categories = [
            "index", "micro_index", "energy", "micro_energy",
            "metals", "micro_metals", "treasuries", "grains",
            "softs", "meats", "currencies", "volatility", "crypto",
        ]
        assert set(DEFAULT_SYMBOLS.keys()) == set(expected_categories)


class TestContinuousSuffixRemoved:
    """The automatic =11INC suffix machinery is removed from models."""

    def test_apply_continuous_suffix_removed(self):
        """apply_continuous_suffix no longer exists on the models module."""
        import tradestation.models

        assert not hasattr(tradestation.models, "apply_continuous_suffix")

    def test_continuous_suffix_constant_removed(self):
        """CONTINUOUS_SUFFIX no longer exists on the models module."""
        import tradestation.models

        assert not hasattr(tradestation.models, "CONTINUOUS_SUFFIX")


class TestBaseSymbol:
    """base_symbol reduces a stored symbol to the root shared by its variations.

    Used by the TS CSV export metadata fallback: @MNG=11ORC, @MNG=106XC and
    @MNGV26 are all variations of the same underlying symbol MNG, so any
    sufficient metadata entry for one can serve the others.
    """

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("@MNG=11ORC", "MNG"),  # continuous spec variation
            ("@MNG=106XC", "MNG"),  # other continuous spec variation
            ("@MNGV26", "MNG"),  # contract-month form (V26 = Oct 2026)
            ("@MNG", "MNG"),  # plain @-prefixed root
            ("ESZ25", "ES"),  # unprefixed contract (Z25 = Dec 2025)
            ("@ES", "ES"),
            ("@6EZ26", "6E"),  # currency root keeps its leading digit
            ("@CL", "CL"),
            ("MNG", "MNG"),  # already a bare root
        ],
    )
    def test_base_symbol_examples(self, symbol, expected):
        """Pinned symbol -> base symbol mappings from the task 2 spec."""
        assert tradestation.models.base_symbol(symbol) == expected

    def test_degenerate_month_code_only_symbol_is_empty(self):
        """'@V26' strips to '' (month code + year is the whole root).

        Deliberate, documented behavior: the regex strips the trailing futures
        month code + 1-2 digit year, and there is no root left underneath.
        """
        assert tradestation.models.base_symbol("@V26") == ""
