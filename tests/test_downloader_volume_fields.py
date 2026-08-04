"""Red-phase tests for the new up/down volume and tick fields in _bars_to_dataframe."""

from datetime import datetime

import pandas.api.types as ptypes

from tradestation.downloader import _OUTPUT_COLUMNS, TradeStationDownloader

SAMPLE_BARS = [
    {
        "High": "6370",
        "Low": "6368.75",
        "Open": "6369.75",
        "Close": "6370",
        "TimeStamp": "2025-02-05T11:56:00Z",
        "TotalVolume": "213",
        "DownTicks": 71,
        "DownVolume": 103,
        "OpenInterest": "0",
        "IsRealtime": False,
        "IsEndOfHistory": False,
        "TotalTicks": 162,
        "UnchangedTicks": 0,
        "UnchangedVolume": 0,
        "UpTicks": 91,
        "UpVolume": 110,
        "Epoch": 1738756560000,
        "BarStatus": "Closed",
    },
    {
        "High": "6370",
        "Low": "6369.25",
        "Open": "6369.75",
        "Close": "6370",
        "TimeStamp": "2025-02-05T11:57:00Z",
        "TotalVolume": "175",
        "DownTicks": 67,
        "DownVolume": 88,
        "OpenInterest": "0",
        "IsRealtime": False,
        "IsEndOfHistory": False,
        "TotalTicks": 113,
        "UnchangedTicks": 0,
        "UnchangedVolume": 0,
        "UpTicks": 46,
        "UpVolume": 87,
        "Epoch": 1738756620000,
        "BarStatus": "Closed",
    },
]

START_DATE = datetime(2025, 1, 1)

BASE_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]
NEW_COLUMNS = ["up_volume", "down_volume", "up_ticks", "down_ticks"]
EXPECTED_COLUMNS = BASE_COLUMNS + NEW_COLUMNS


class TestBarsToDataFrameVolumeFields:
    """Tests for up/down volume and tick field handling in _bars_to_dataframe."""

    def test_bars_contain_up_down_volume(self):
        """All expected columns, including the new volume/tick fields, are present."""
        df = TradeStationDownloader._bars_to_dataframe(SAMPLE_BARS, START_DATE)
        assert list(df.columns) == EXPECTED_COLUMNS, (
            f"Expected columns {EXPECTED_COLUMNS}, got {list(df.columns)}"
        )

    def test_up_down_volume_values_correct(self):
        """The new fields contain the exact sample values from the API response."""
        df = TradeStationDownloader._bars_to_dataframe(SAMPLE_BARS, START_DATE)
        for col in NEW_COLUMNS + ["volume"]:
            assert col in df.columns, f"Missing column: {col}"
        assert df["up_volume"].iloc[0] == 110
        assert df["down_volume"].iloc[0] == 103
        assert df["up_ticks"].iloc[0] == 91
        assert df["down_ticks"].iloc[0] == 71
        assert df["volume"].iloc[0] == 213

    def test_volume_still_present(self):
        """Aggregate volume (TotalVolume) is kept when the new fields are added."""
        df = TradeStationDownloader._bars_to_dataframe(SAMPLE_BARS, START_DATE)
        assert "volume" in df.columns, "Aggregate volume column was removed"
        assert df["volume"].iloc[0] == 213, "Aggregate volume value is incorrect"
        for col in NEW_COLUMNS:
            assert col in df.columns, (
                f"Expected new column {col} alongside the aggregate volume"
            )

    def test_up_plus_down_equals_total(self):
        """UpVolume + DownVolume equals TotalVolume for every bar."""
        df = TradeStationDownloader._bars_to_dataframe(SAMPLE_BARS, START_DATE)
        for col in NEW_COLUMNS + ["volume"]:
            assert col in df.columns, f"Missing column: {col}"
        assert (df["up_volume"] + df["down_volume"] == df["volume"]).all()

    def test_bars_without_up_down_volume(self):
        """Missing new fields do not crash and do not create extra columns."""
        bars = [
            {
                "High": "6370",
                "Low": "6368.75",
                "Open": "6369.75",
                "Close": "6370",
                "TimeStamp": "2025-02-05T11:56:00Z",
                "TotalVolume": "213",
            }
        ]
        df = TradeStationDownloader._bars_to_dataframe(bars, START_DATE)
        assert list(df.columns) == BASE_COLUMNS, (
            f"Expected base columns {BASE_COLUMNS}, got {list(df.columns)}"
        )
        for col in NEW_COLUMNS:
            assert col not in df.columns, (
                f"Unexpected column {col} when new fields are missing"
            )

        # The output schema must declare the new fields so they are used when provided.
        for col in NEW_COLUMNS:
            assert col in _OUTPUT_COLUMNS, (
                f"Expected {col} to be declared in _OUTPUT_COLUMNS"
            )

    def test_up_down_volume_numeric_type(self):
        """The new fields are returned as numeric types, not strings."""
        df = TradeStationDownloader._bars_to_dataframe(SAMPLE_BARS, START_DATE)
        for col in NEW_COLUMNS:
            assert col in df.columns, f"Missing column: {col}"
            assert ptypes.is_numeric_dtype(df[col]), (
                f"Column {col} should be numeric, got {df[col].dtype}"
            )
