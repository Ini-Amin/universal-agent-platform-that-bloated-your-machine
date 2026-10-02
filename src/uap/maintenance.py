"""Operator maintenance CLI. Opt-in retention/pruning of durable execution data.

Usage::

    python -m uap.maintenance prune --older-than-days 30
    python -m uap.maintenance prune --older-than-days 30 --limit 500

Nothing here runs on import, on server startup, or on a timer. Pruning only
happens when an operator invokes this command (or the equivalent HTTP endpoint
``POST /api/maintenance/prune``). An installation that never runs it keeps every
execution forever.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from uap.db.engine import get_session_factory
from uap.db.repositories import ExecutionRepository

__all__ = ["main", "prune"]


def prune(older_than_days: int, *, limit: int | None = None) -> int:
    """Delete terminal executions older than ``older_than_days``; return count.

    Only executions in a terminal status are removed; child rows cascade.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    factory = get_session_factory()
    with factory() as session:
        try:
            deleted = ExecutionRepository(session).prune(cutoff, limit=limit)
            session.commit()
        except Exception:
            session.rollback()
            raise
    return deleted


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m uap.maintenance``."""

    parser = argparse.ArgumentParser(prog="uap.maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prune", help="delete old terminal executions")
    p.add_argument("--older-than-days", type=int, required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--yes",
        action="store_true",
        help="required to proceed when --older-than-days is 0 (deletes EVERYTHING)",
    )
    args = parser.parse_args(argv)

    if args.command == "prune":
        # --older-than-days 0 means "cutoff = now", i.e. every terminal run is
        # eligible — including ones created seconds ago. That is occasionally
        # what an operator wants (wipe the table) and never what they want by
        # accident, so it needs an explicit second signal.
        if args.older_than_days == 0 and not args.yes:
            print(
                "refusing: --older-than-days 0 deletes EVERY terminal execution, "
                "including runs created moments ago.\n"
                "If that is really what you want, re-run with --yes.\n"
                "For normal cleanup use a real age, e.g. --older-than-days 30.",
                file=sys.stderr,
            )
            return 2

        deleted = prune(args.older_than_days, limit=args.limit)
        print(f"deleted {deleted} execution(s) older than {args.older_than_days} day(s)")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
