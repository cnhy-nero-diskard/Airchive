"""Command-line entry point.

One entry point, one subcommand per operator task (design D15). Everything is
read-only except `poll`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from airchive import __version__
from airchive.config import load_dotenv

_EXIT_OK = 0
_EXIT_FAILURE = 1
_EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airchive",
        description="LG ThinQ air-conditioner telemetry collector.",
    )
    parser.add_argument("--version", action="version", version=f"airchive {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    discover = sub.add_parser(
        "discover",
        help="List devices and report profiles, energy profile, state, and today's usage",
    )
    discover.add_argument(
        "--device-id", help="Report on this device only, instead of every air conditioner"
    )

    validate = sub.add_parser(
        "validate-counter",
        help="Sample the current-day energy counter repeatedly and report its behavior",
    )
    validate.add_argument(
        "--duration-minutes", type=int, default=180, help="How long to sample (default: 180)"
    )
    validate.add_argument(
        "--interval-seconds", type=int, default=60, help="Seconds between samples (default: 60)"
    )

    check = sub.add_parser(
        "check-firestore",
        help="Write/read/delete round trip against Firestore to prove connectivity",
    )
    check.add_argument(
        "--pause-seconds",
        type=int,
        help="Wait this long before deleting the scratch document (non-interactive runs)",
    )
    check.add_argument(
        "--keep", action="store_true", help="Leave the scratch document in place"
    )

    poll = sub.add_parser("poll", help="Run polling cycles and persist observations")
    poll.add_argument(
        "--once", action="store_true", help="Run exactly one cycle and exit (scheduled-job mode)"
    )

    latest = sub.add_parser("latest", help="List the most recent stored observations")
    latest.add_argument("--limit", type=int, default=10, help="How many to list (default: 10)")

    sub.add_parser("health", help="Report the current collector health record")

    anomalies = sub.add_parser(
        "anomalies", help="List observations whose quality indicates a problem"
    )
    anomalies.add_argument("--since", help="ISO-8601 start of the range (inclusive)")
    anomalies.add_argument("--until", help="ISO-8601 end of the range (exclusive)")
    anomalies.add_argument("--limit", type=int, default=50, help="Maximum rows (default: 50)")

    sub.add_parser(
        "compare", help="Diff the latest stored observation against a fresh live reading"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return _EXIT_USAGE

    load_dotenv()

    if args.command == "discover":
        from airchive.commands import discover as discover_cmd

        return discover_cmd.run(device_id=args.device_id)

    if args.command == "validate-counter":
        from airchive.commands import validate_counter

        return validate_counter.run(
            duration_minutes=args.duration_minutes, interval_seconds=args.interval_seconds
        )

    if args.command == "check-firestore":
        from airchive.commands import check_firestore

        return check_firestore.run(pause_seconds=args.pause_seconds, keep=args.keep)

    raise NotImplementedError(f"{args.command} is not implemented yet")


if __name__ == "__main__":
    sys.exit(main())
