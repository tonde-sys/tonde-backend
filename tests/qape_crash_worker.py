"""Test-only child process; parent SIGKILLs it at the requested durable boundary."""
import asyncio
import sys
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.schemas.ticket import CreateTicketRequest
from app.services.ticket_service import TicketService
import app.services.ticket_service as ticket_module
import app.services.qape_engine as engine_module


async def main():
    stage, url, agency_id, service_id, user_id, key = sys.argv[1:]
    engine = create_async_engine(url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        data = CreateTicketRequest(agency_id=agency_id, service_id=service_id, client_action_id=key)
        if stage == 'denied':
            original = engine_module.reserve
            async def hold_after_redis_denial(*args, **kwargs):
                decision = await original(*args, **kwargs)
                if decision.state != 'DENIED':
                    raise AssertionError('Test requires a real Redis DENIED result')
                print('READY:DENIED', flush=True)
                await asyncio.sleep(120)
                return decision
            engine_module.reserve = hold_after_redis_denial
        elif stage == 'quota_after_commit':
            from app.models.qape_operation import QapeCreateOperation
            from app.services.qape_engine import Decision, DecisionCode
            async def allow_preflight(*args, **kwargs):
                # Force the final transactional guard to observe a concurrent
                # creation that the preflight did not see.
                return Decision(DecisionCode.ALLOW)
            engine_module.QapeDecisionEngine.preflight = allow_preflight
            original_commit = db.commit
            async def hold_after_quota_refusal_commit():
                refused = any(isinstance(row, QapeCreateOperation) and
                    row.decision_code == 'DAILY_QUOTA_REACHED' for row in db.identity_map.values())
                await original_commit()
                if refused:
                    print('READY:QUOTA_AFTER_COMMIT', flush=True)
                    await asyncio.sleep(120)
            db.commit = hold_after_quota_refusal_commit
        elif stage == 'reserved':
            original = ticket_module.lock_for_creation_commit
            async def hold_before_final_commit(*args, **kwargs):
                row = await original(*args, **kwargs)
                print('READY:RESERVED', flush=True)
                await asyncio.sleep(120)
                return row
            ticket_module.lock_for_creation_commit = hold_before_final_commit
        else:
            original = TicketService._replay_qape_committed
            async def hold_after_final_commit(self, row):
                print('READY:COMMITTED', flush=True)
                await asyncio.sleep(120)
                return await original(self, row)
            TicketService._replay_qape_committed = hold_after_final_commit
        await TicketService(db).create_ticket(data, user_id, None)
    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
