"""Tests for config module category and symbol handling."""

import pytest
import yaml

from tradestation.config import ConfigurationError, _parse_config, load_config
from tradestation.models import DownloadConfig

BASE_CONFIG = {
    "tradestation": {
        "client_id": "client_id",
        "client_secret": "client_secret",
        "refresh_token": "refresh_token",
    }
}


def _write_and_load(temp_data_dir, data):
    """Write data to a temporary YAML file and load it."""
    config_path = temp_data_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return load_config(str(config_path))


class TestConfigSymbolsAndCategories:
    """Tests for symbol and category parsing via load_config and _parse_config."""

    def test_both_symbols_and_categories(self, temp_data_dir):
        data = {
            **BASE_CONFIG,
            "symbols": ["@ES", "@NQ"],
            "categories": ["index", "energy"],
        }

        config = _write_and_load(temp_data_dir, data)
        assert isinstance(config, DownloadConfig)
        assert config.symbols == ["@ES", "@NQ"]
        assert config.categories == ["index", "energy"]

        parsed = _parse_config(data)
        assert isinstance(parsed, DownloadConfig)
        assert parsed.symbols == ["@ES", "@NQ"]
        assert parsed.categories == ["index", "energy"]

    def test_only_symbols(self, temp_data_dir):
        data = {**BASE_CONFIG, "symbols": ["@CL", "@GC"]}

        config = _write_and_load(temp_data_dir, data)
        assert isinstance(config, DownloadConfig)
        assert config.symbols == ["@CL", "@GC"]
        assert config.categories == []

        parsed = _parse_config(data)
        assert isinstance(parsed, DownloadConfig)
        assert parsed.symbols == ["@CL", "@GC"]
        assert parsed.categories == []

    def test_only_categories(self, temp_data_dir):
        data = {**BASE_CONFIG, "categories": ["metals", "crypto"]}

        config = _write_and_load(temp_data_dir, data)
        assert isinstance(config, DownloadConfig)
        assert config.symbols == []
        assert config.categories == ["metals", "crypto"]

        parsed = _parse_config(data)
        assert isinstance(parsed, DownloadConfig)
        assert parsed.symbols == []
        assert parsed.categories == ["metals", "crypto"]

    def test_neither_symbols_nor_categories(self, temp_data_dir):
        data = BASE_CONFIG

        config = _write_and_load(temp_data_dir, data)
        assert isinstance(config, DownloadConfig)
        assert config.symbols == []
        assert config.categories == []

        parsed = _parse_config(data)
        assert isinstance(parsed, DownloadConfig)
        assert parsed.symbols == []
        assert parsed.categories == []

    def test_unknown_category_raises_configuration_error(self, temp_data_dir):
        """An unknown category raises ConfigurationError with valid options."""
        data = {**BASE_CONFIG, "categories": ["indexx"]}

        with pytest.raises(ConfigurationError, match="Unknown category: 'indexx'"):
            _write_and_load(temp_data_dir, data)
        with pytest.raises(ConfigurationError, match="Unknown category: 'indexx'"):
            _parse_config(data)

    def test_categories_scalar_raises_configuration_error(self, temp_data_dir):
        """A scalar YAML value for categories raises ConfigurationError."""
        data = {**BASE_CONFIG, "categories": "index"}

        with pytest.raises(ConfigurationError, match="categories must be a list"):
            _write_and_load(temp_data_dir, data)
        with pytest.raises(ConfigurationError, match="categories must be a list"):
            _parse_config(data)

    def test_symbols_scalar_raises_configuration_error(self, temp_data_dir):
        """A scalar YAML value for symbols raises ConfigurationError."""
        data = {**BASE_CONFIG, "symbols": "@ES"}

        with pytest.raises(ConfigurationError, match="symbols must be a list"):
            _write_and_load(temp_data_dir, data)
        with pytest.raises(ConfigurationError, match="symbols must be a list"):
            _parse_config(data)

    def test_null_symbols_and_categories_treated_as_empty(self, temp_data_dir):
        """Null YAML values for symbols and categories are treated as empty lists."""
        data = {**BASE_CONFIG, "symbols": None, "categories": None}

        config = _write_and_load(temp_data_dir, data)
        assert isinstance(config, DownloadConfig)
        assert config.symbols == []
        assert config.categories == []

        parsed = _parse_config(data)
        assert isinstance(parsed, DownloadConfig)
        assert parsed.symbols == []
        assert parsed.categories == []
