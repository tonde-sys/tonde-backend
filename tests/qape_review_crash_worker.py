"""Test-only child: parent kills after the human decision commits durably."""
import asyncio
import sys
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models.qape import AbuseEvent
from app.models.user import User
from app.services.qape_review import QapeReviewService


async def main():
    url, org_id, operation_id, actor_id = sys.argv[1:]
    engine = create_async_engine(url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        original_commit = db.commit
        async def hold_after_review_commit():
            reviewing = any(isinstance(row, AbuseEvent) and
                row.event_type == 'QAPE_HUMAN_REVIEW' for row in db.new)
            await original_commit()
            if reviewing:
                print('READY:REVIEW_COMMITTED', flush=True)
                await asyncio.sleep(120)
        db.commit = hold_after_review_commit
        await QapeReviewService(db).decide(await db.get(User, actor_id),
                                           org_id, operation_id, 'REJECT', 'Refus motivé', 1)
    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
