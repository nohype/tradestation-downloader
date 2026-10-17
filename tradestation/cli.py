"""
Command-line interface for TradeStation data downloader.
"""

import argparse
import calendar
import logging
import math
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import ConfigurationError, load_config, load_credentials
from .downloader import TradeStationDownloader
from .models import (
    DEFAULT_SYMBOLS,
    Compression,
    OutputFormat,
    StorageFormat,
    get_symbols_by_categories,
    validate_symbol,
)


def _interval_type(value: str) -> int:
    """Argparse type validator for the --interval flag."""
    try:
        interval = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"interval must be an integer, got {value!r}"
        ) from exc
    if not 1 <= interval <= 1440:
        raise argparse.ArgumentTypeError(
            f"interval must be between 1 and 1440 minutes, got {interval}"
        )
    return interval

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def create_download_parser() -> argparse.ArgumentParser:
    """Create argument parser for download command."""
    parser = argparse.ArgumentParser(
        prog="tradestation-download",
        description="Download historical futures data from TradeStation API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Download all configured symbols (incremental)
  %(prog)s -s @ES @NQ @CL           Download specific symbols
  %(prog)s --full                   Full download (ignore existing data)
  %(prog)s --storage-format daily   Use daily partitioned storage
  %(prog)s --interval 5              Download 5-minute bars
  %(prog)s --list-symbols           List all default symbols
  %(prog)s --list-categories        List symbol categories
  %(prog)s --all-categories         Download symbols from all configured categories
  %(prog)s -s @ES --rollcheck       Check whether to roll each symbol's front contract
  %(prog)s orders --help            Orders mode: download account order history to CSV
""",
    )

    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        metavar="FILE",
        help="Path to configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "-s", "--symbols",
        nargs="+",
        metavar="SYMBOL",
        help="Specific symbols to download (overrides config)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full download (ignore existing data)",
    )
    parser.add_argument(
        "--storage-format",
        choices=["single", "daily", "monthly"],
        metavar="FORMAT",
        help="Storage format: single, daily, or monthly",
    )
    parser.add_argument(
        "--compression",
        choices=["zstd", "snappy", "gzip", "lz4", "none"],
        metavar="ALGO",
        help="Parquet compression: zstd (default), snappy, gzip, lz4, or none",
    )
    parser.add_argument(
        "--parquet",
        action="store_true",
        help="Store downloaded data as Parquet files (default when no output format is specified)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        dest="csv_output",
        help="Also write plain CSV .txt files in plain_data "
             "(Parquet is still written as canonical storage)",
    )
    parser.add_argument(
        "--ts-csv",
        action="store_true",
        dest="ts_csv_output",
        help="Also write TradeStation third-party ASCII files in plain_data "
             "(Parquet is still written as canonical storage)",
    )
    parser.add_argument(
        "--interval",
        type=_interval_type,
        metavar="MINUTES",
        help="Bar size in minutes: 1-1440 (overrides config)",
    )
    parser.add_argument(
        "--no-datetime-index",
        action="store_true",
        help="Save as @ES (raw) instead of @ES_index_1 (with datetime index)",
    )
    parser.add_argument(
        "--list-symbols",
        action="store_true",
        help="List all default symbols and exit",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List symbol categories and exit",
    )
    parser.add_argument(
        "--category",
        choices=list(DEFAULT_SYMBOLS.keys()),
        metavar="CAT",
        help="Download only symbols from this category",
    )
    parser.add_argument(
        "--all-categories",
        action="store_true",
        help="Download symbols from all categories configured in the config file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (debug) logging",
    )
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="Fetch symbol metadata from TradeStation API and derive session times "
             "from downloaded data, then exit",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Export downloaded data to CSV (.txt) files in a 'plain_data' "
             "sibling directory, then exit",
    )
    parser.add_argument(
        "--export-ts-csv",
        action="store_true",
        help="Export downloaded data to TradeStation third-party ASCII format "
             "files (<symbol>_ts.txt) in a 'plain_data' sibling directory, then exit",
    )
    parser.add_argument(
        "--rollcheck",
        action="store_true",
        help="Check whether to roll each symbol's front contract to the next, then exit",
    )
    parser.add_argument(
        "--roll-days",
        type=int,
        default=6,
        metavar="N",
        help="With --rollcheck: also roll when the current contract's expiration "
             "or first notice date is within N days (default: 6)",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Number of parallel download workers (default: 4, use 1 for sequential)",
    )

    return parser


def _make_orders_date_type(flag: str):
    """Return an argparse type callable for --start or --end dates."""

    def parse(value: str) -> date:
        lowered = value.lower()
        if lowered == "earliest":
            if flag != "start":
                raise argparse.ArgumentTypeError(
                    f"'earliest' is only valid for --start, got {value!r}"
                )
            return datetime.now(UTC).date() - timedelta(days=729)
        if lowered == "latest":
            if flag != "end":
                raise argparse.ArgumentTypeError(
                    f"'latest' is only valid for --end, got {value!r}"
                )
            return datetime.now(UTC).date()
        valid = "YYYY-MM-DD, YYYY-MM, YYYY" + (
            ", or 'earliest'" if flag == "start" else ", or 'latest'"
        )
        try:
            m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
            if m:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            m = re.fullmatch(r"(\d{4})-(\d{2})", value)
            if m:
                year, month = int(m.group(1)), int(m.group(2))
                if flag == "start":
                    return date(year, month, 1)
                return date(year, month, calendar.monthrange(year, month)[1])
            m = re.fullmatch(r"(\d{4})", value)
            if m:
                year = int(m.group(1))
                return date(year, 1, 1) if flag == "start" else date(year, 12, 31)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"invalid {flag} date {value!r}. Expected one of: {valid}"
            ) from exc
        raise argparse.ArgumentTypeError(
            f"invalid {flag} date {value!r}. Expected one of: {valid}"
        )

    return parse


def create_orders_parser() -> argparse.ArgumentParser:
    """Create argument parser for the 'orders' sub-mode."""
    parser = argparse.ArgumentParser(
        prog="tradestation-download orders",
        description="Download historical orders for the logged-in TradeStation account to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  Download all available order history
  %(prog)s -s 2024-01-01 -e 2025-01-31      Download orders for a date range
  %(prog)s -s 2024 -e 2024-06               Year / year-month date forms
  %(prog)s --full -o my_orders.csv          Discard cache, re-download, custom output
  %(prog)s -a 12345678 -s 2025-01-01 -e latest
  %(prog)s --include-open                   Also include still-open orders

Convert output to TradingDiary Pro format: orders-to-tdpro FILE
""",
    )

    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        metavar="FILE",
        help="Path to configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Discard existing local order cache and re-download everything",
    )
    parser.add_argument(
        "-s", "--start",
        type=_make_orders_date_type("start"),
        default="earliest",
        metavar="DATE",
        help="Start date: YYYY-MM-DD, YYYY-MM, YYYY, or 'earliest' "
             "(default: earliest, i.e. today minus 729 days)",
    )
    parser.add_argument(
        "-e", "--end",
        type=_make_orders_date_type("end"),
        default="latest",
        metavar="DATE",
        help="End date: YYYY-MM-DD, YYYY-MM, YYYY, or 'latest' (default: latest)",
    )
    parser.add_argument(
        "-o", "--out",
        default=None,
        metavar="FILE",
        help="Output CSV path (default: orders/<account>_<start>_<end>.csv)",
    )
    parser.add_argument(
        "--include-open",
        action="store_true",
        help="Include open orders (default: only closed orders are downloaded)",
    )
    parser.add_argument(
        "-a", "--account",
        default=None,
        metavar="ID",
        help="TradeStation account ID (required when the login has multiple accounts)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (debug) logging",
    )

    return parser


def _multiplier_type(value: str) -> tuple[str, str]:
    """Argparse type for -m/--multiplier ROOT=VALUE; returns a (root, value) pair."""
    root, sep, num = value.partition("=")
    root = root.strip().upper()  # TradeStation roots are uppercase
    if not sep or not root:
        raise argparse.ArgumentTypeError(
            f"multiplier must be in ROOT=VALUE form, got {value!r}"
        )
    try:
        amount = float(num)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"multiplier value must be a number, got {value!r}"
        ) from exc
    if not math.isfinite(amount) or amount <= 0:
        raise argparse.ArgumentTypeError(
            f"multiplier value must be a finite positive number, got {value!r}"
        )
    return root, num.strip()


def create_tdpro_parser() -> argparse.ArgumentParser:
    """Create argument parser for the orders-to-tdpro converter."""
    parser = argparse.ArgumentParser(
        prog="orders-to-tdpro",
        description="Convert a tradestation-downloader orders CSV to "
                    "TradingDiary Pro import format (futures)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s orders/12345678_2025-01-01_2025-12-31.csv
                                  Convert to <stem>_tdpro.csv next to the input
  %(prog)s orders.csv -o out.csv  Custom output path
  %(prog)s orders.csv -m ES=50 -m MGC=10
                                  Add/override contract multipliers
""",
    )

    parser.add_argument(
        "file",
        metavar="FILE",
        help="Orders CSV produced by 'tradestation-download orders'",
    )
    parser.add_argument(
        "-o", "--out",
        default=None,
        metavar="FILE",
        help="Output CSV path (default: <input_stem>_tdpro.csv next to input)",
    )
    parser.add_argument(
        "-m", "--multiplier",
        action="append",
        type=_multiplier_type,
        default=[],
        metavar="ROOT=VALUE",
        help="Add or override a contract point value; repeatable",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (debug) logging",
    )

    return parser


def print_symbols() -> None:
    """Print all default symbols organized by category."""
    print("\nDefault US Futures Symbols")
    print("=" * 50)

    for category, symbols in DEFAULT_SYMBOLS.items():
        print(f"\n{category.upper().replace('_', ' ')}:")
        for symbol in symbols:
            print(f"  {symbol}")

    total = sum(len(s) for s in DEFAULT_SYMBOLS.values())
    print(f"\nTotal: {total} symbols")


def print_categories() -> None:
    """Print available symbol categories."""
    print("\nAvailable Symbol Categories")
    print("=" * 40)

    for category, symbols in DEFAULT_SYMBOLS.items():
        print(f"  {category:<15} ({len(symbols)} symbols)")

    print("\nUse --category <name> to download a specific category")


def run_download(args: argparse.Namespace) -> int:
    """Run the download command."""
    # Handle list commands
    if args.list_symbols:
        print_symbols()
        return 0

    if args.list_categories:
        print_categories()
        return 0

    if args.metadata:
        from .metadata import run_metadata
        return run_metadata(args.config)

    if args.export_csv or args.export_ts_csv:
        from .csv_export import run_export_csv, run_export_ts_csv
        results = []
        if args.export_csv:
            results.append(run_export_csv(args.config, args.symbols))
        if args.export_ts_csv:
            results.append(run_export_ts_csv(args.config, args.symbols))
        return 0 if all(result == 0 for result in results) else 1

    # Load configuration
    try:
        config = load_config(args.config)
    except ConfigurationError as e:
        logger.error(str(e))
        return 1

    # Resolve symbols
    if args.symbols:
        config.symbols = args.symbols
    elif args.category:
        config.symbols = DEFAULT_SYMBOLS[args.category]
    elif args.all_categories:
        if not config.categories:
            logger.error("--all-categories was set but no categories are configured")
            return 1
        config.symbols = get_symbols_by_categories(config.categories)
    else:
        if not config.symbols:
            logger.error("No symbols are configured")
            return 1

    # Validate symbols
    for symbol in config.symbols:
        validate_symbol(symbol)

    # Resolve requested output formats. CLI flags override the config; when no
    # format flag is passed, preserve whatever the config/YAML already set.
    if args.parquet or args.csv_output or args.ts_csv_output:
        requested_formats = []
        if args.parquet:
            requested_formats.append(OutputFormat.PARQUET)
        if args.csv_output:
            requested_formats.append(OutputFormat.CSV)
        if args.ts_csv_output:
            requested_formats.append(OutputFormat.TS_CSV)
        config.output_formats = requested_formats

    # Remember the config file path so the downloader can export after downloading.
    config.config_path = args.config

    if args.rollcheck:
        from .rollcheck import run_rollcheck
        config.roll_days = args.roll_days
        return run_rollcheck(config)

    # Override storage format if provided
    if args.storage_format:
        config.storage_format = StorageFormat.from_string(args.storage_format)

    # Override compression if provided
    if args.compression:
        config.compression = Compression.from_string(args.compression)

    # Override interval if provided
    if args.interval is not None:
        config.interval = args.interval

    # Override datetime_index if provided
    if args.no_datetime_index:
        config.datetime_index = False

    # Override workers if provided
    if args.workers:
        config.max_workers = args.workers

    # Run downloader
    try:
        downloader = TradeStationDownloader(config)
        downloader.download_all(incremental=not args.full)
        return 0 if downloader.stats.errors == 0 else 1
    except KeyboardInterrupt:
        logger.info("\nDownload interrupted by user")
        return 130
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        if args.verbose:
            raise
        return 1


def run_orders_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Run the 'orders' sub-mode."""
    from .orders import (
        ORDERS_ROOT,
        AccountResolutionError,
        OrdersError,
        resolve_account_id,
        run_orders,
    )

    start: date = args.start
    end: date = args.end
    if start > end:
        parser.error(
            f"--start ({start.isoformat()}) must be on or before "
            f"--end ({end.isoformat()})"
        )

    try:
        try:
            config = load_credentials(args.config)
        except ConfigurationError as e:
            logger.error(str(e))
            return 1

        from .auth import AuthenticationError, TradeStationAuth
        auth = TradeStationAuth(
            config.client_id, config.client_secret, config.refresh_token
        )

        try:
            account_id = resolve_account_id(auth, args.account)
        except (AccountResolutionError, AuthenticationError, OrdersError) as e:
            logger.error(str(e))
            return 1

        if args.out:
            output_path = Path(args.out)
        else:
            output_path = ORDERS_ROOT / f"{account_id}_{start.isoformat()}_{end.isoformat()}.csv"
        logger.info("Date range: %s to %s", start.isoformat(), end.isoformat())
        if args.include_open:
            logger.info("Including open orders")
        try:
            return run_orders(
                config, auth, account_id, start, end,
                full=args.full, include_open=args.include_open,
                output_path=output_path,
            )
        except Exception as e:
            logger.error("Unexpected error: %s", e)
            if args.verbose:
                raise
            return 1
    except KeyboardInterrupt:
        logger.info("\nOrders download interrupted by user")
        return 130


def main_download() -> None:
    """Entry point for download CLI; also dispatches 'orders' mode."""
    if sys.argv[1:2] == ["orders"]:
        sys.exit(main_orders())

    parser = create_download_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    sys.exit(run_download(args))


def main_orders() -> int:
    """Entry point for the 'orders' sub-mode."""
    parser = create_orders_parser()
    args = parser.parse_args(sys.argv[2:])

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    return run_orders_mode(parser, args)


def run_tdpro_mode(args: argparse.Namespace) -> int:
    """Run the orders-to-tdpro converter."""
    from .tdpro import TdproError, convert_orders_csv

    input_path = Path(args.file)
    if args.out:
        output_path = Path(args.out)
    else:
        output_path = input_path.with_name(input_path.stem + "_tdpro.csv")

    # args.multiplier is a list of (root, value) pairs from _multiplier_type
    multipliers = dict(args.multiplier)

    try:
        n_trades, n_orders = convert_orders_csv(input_path, output_path, multipliers)
    except TdproError as e:
        logger.error(str(e))
        return 1
    except KeyboardInterrupt:
        logger.info("\nConversion interrupted by user")
        return 130
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        if args.verbose:
            raise
        return 1

    print(f"Wrote {output_path} ({n_trades:,} trades from {n_orders:,} orders)")
    return 0


def main_tdpro() -> None:
    """Entry point for the orders-to-tdpro converter CLI."""
    parser = create_tdpro_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    sys.exit(run_tdpro_mode(args))


def main_auth() -> None:
    """Entry point for auth setup CLI."""
    from .auth_setup import main

    main()
