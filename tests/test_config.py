"""Tests for config module category and symbol handling."""

import pytest
import yaml

from tradestation.config import ConfigurationError, _parse_config, load_config
from tradestation.models import DownloadConfig, OutputFormat

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

    def test_interval_from_config(self, temp_data_dir):
        """interval is read from the config file."""
        data = {**BASE_CONFIG, "interval": 30}

        config = _write_and_load(temp_data_dir, data)
        assert config.interval == 30

        parsed = _parse_config(data)
        assert parsed.interval == 30

    @pytest.mark.parametrize("interval", [0, -1, 1441])
    def test_invalid_interval_raises_configuration_error(
        self, temp_data_dir, interval
    ):
        """An out-of-range interval raises ConfigurationError."""
        data = {**BASE_CONFIG, "interval": interval}

        with pytest.raises(ConfigurationError, match="interval must be between"):
            _write_and_load(temp_data_dir, data)
        with pytest.raises(ConfigurationError, match="interval must be between"):
            _parse_config(data)

    def test_non_integer_interval_raises_configuration_error(self, temp_data_dir):
        """A non-integer interval raises ConfigurationError."""
        data = {**BASE_CONFIG, "interval": 5.5}

        with pytest.raises(ConfigurationError, match="interval must be an integer"):
            _write_and_load(temp_data_dir, data)
        with pytest.raises(ConfigurationError, match="interval must be an integer"):
            _parse_config(data)


class TestConfigOutputFormats:
    """Tests for output_formats parsing."""

    def test_default_output_formats(self, temp_data_dir):
        data = BASE_CONFIG

        config = _write_and_load(temp_data_dir, data)
        assert config.output_formats == [OutputFormat.PARQUET]

        parsed = _parse_config(data)
        assert parsed.output_formats == [OutputFormat.PARQUET]

    def test_output_formats_from_config(self, temp_data_dir):
        data = {**BASE_CONFIG, "output_formats": ["csv", "ts_csv"]}

        config = _write_and_load(temp_data_dir, data)
        assert config.output_formats == [OutputFormat.CSV, OutputFormat.TS_CSV]

        parsed = _parse_config(data)
        assert parsed.output_formats == [OutputFormat.CSV, OutputFormat.TS_CSV]

    def test_output_formats_scalar_raises_configuration_error(self, temp_data_dir):
        """A scalar YAML value for output_formats raises ConfigurationError."""
        data = {**BASE_CONFIG, "output_formats": "csv"}

        with pytest.raises(ConfigurationError, match="output_formats must be a list"):
            _write_and_load(temp_data_dir, data)
        with pytest.raises(ConfigurationError, match="output_formats must be a list"):
            _parse_config(data)

    def test_invalid_output_format_raises_configuration_error(self, temp_data_dir):
        """An invalid output format raises ConfigurationError."""
        data = {**BASE_CONFIG, "output_formats": ["parquet", "invalid"]}

        with pytest.raises(ConfigurationError, match="Invalid output format"):
            _write_and_load(temp_data_dir, data)
        with pytest.raises(ConfigurationError, match="Invalid output format"):
            _parse_config(data)
