"""QAPE historical creation backfill. Default: SELECT-only dry-run.

Example: python -m scripts.qape_backfill --batch-size 200 --max-batches 2
Resume with --after-id <last_id> --as-of <unchanged report as_of>.
Apply requires --apply, --confirm-database <current_database()> and --as-of
copied from a reviewed dry-run report. Keep --as-of unchanged when resuming.
After ALL apply batches, stop historical writes and run --reconcile, which
rescans from ID zero with a NEW cutoff. Only reconciliation_required=false
means no recoverable creation is missing at that snapshot; ambiguous tickets
remain excluded and reported. Do not treat a partial scan as reconciliation.
Never run against production without a separately approved change window.
"""
import argparse
import asyncio
import json
from datetime import datetime

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine
from app.services.qape_backfill import run_backfill


async def main(args):
    if args.reconcile and (args.apply or args.after_id or args.as_of or args.max_batches):
        raise SystemExit("Reconciliation requires a fresh full-range dry-run (no cursor, cutoff or batch cap)")
    async with AsyncSessionLocal() as session:
        database = (await session.execute(text("SELECT current_database()"))).scalar_one()
        await session.rollback()
        if args.apply and args.confirm_database != database:
            raise SystemExit(f"Apply refused: supply --confirm-database {database!r} explicitly")
        if args.apply and not args.as_of:
            raise SystemExit("Apply refused: supply --as-of from a reviewed dry-run report")
        report = await run_backfill(session, dry_run=not args.apply, batch_size=args.batch_size,
                                    after_id=args.after_id,
                                    as_of=datetime.fromisoformat(args.as_of) if args.as_of else None,
                                    max_batches=args.max_batches)
        print(json.dumps({"database": database, **report.to_dict()}, ensure_ascii=False, indent=2))
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Explicit write mode (dry-run is default)")
    parser.add_argument("--reconcile", action="store_true", help="Final fresh full-range dry-run after quiescing historical writes")
    parser.add_argument("--confirm-database", help="Required on apply: exact current_database() name")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--after-id", help="Resume cursor from last completed batch")
    parser.add_argument("--as-of", help="UTC ISO timestamp from original report (required with --after-id)")
    args = parser.parse_args()
    asyncio.run(main(args))
